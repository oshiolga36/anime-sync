// Telegram build notification. The message is passed via an env var and the sh
// body is single-quoted, so the token is never interpolated into a Groovy string
// and `set +x` keeps it out of the build log (Jenkins runs sh with -x by default).
// Never fails the build: a notification problem must not mask the sync result.
def notify(String text) {
    withEnv(["MSG=${text}"]) {
        sh '''
            set +x
            [ -r /srv/bot-secrets/bot.env ] || exit 0
            . /srv/bot-secrets/bot.env
            # skip silently while TELEGRAM_CHAT_ID is unset or still the placeholder
            case "$TELEGRAM_CHAT_ID" in ''|*[!0-9-]*) exit 0 ;; esac
            [ -n "$TELEGRAM_TOKEN" ] || exit 0
            curl -sS -m 30 -o /dev/null \
                -X POST "https://api.telegram.org/bot$TELEGRAM_TOKEN/sendMessage" \
                --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" \
                --data-urlencode "text=$MSG" || echo "telegram notify failed (ignored)"
        '''
    }
}

// Body of the notification: which titles were downloaded / failed.
// Falls back to empty (terse header only) if the sync never wrote a summary.
def summaryBody() {
    return sh(script: 'python3 summary_text.py 2>/dev/null || true', returnStdout: true).trim()
}

// Did this run download anything or hit an error? Idle runs are not news.
def changed() {
    return sh(script: 'python3 summary_text.py --changed', returnStatus: true) == 0
}

// Scheduled runs stay quiet when nothing happened (12 builds/day, mostly idle).
// Manual runs always report back — you pressed the button, silence is ambiguous.
def scheduled() {
    return currentBuild.getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause').size() > 0
}

pipeline {
    agent any

    // Every 2h, same cadence as the old crontab line. H spreads the load off :00.
    triggers { cron('H H/2 * * *') }

    environment {
        // State the sync writes (last downloaded episode per show). Kept OUTSIDE
        // the workspace: Jenkins may wipe the workspace, and losing this makes
        // the next run re-download the whole library.
        STATE = '/var/jenkins_home/anime-state'
        PYTHONIOENCODING = 'utf-8'
    }

    options {
        disableConcurrentBuilds()          // two syncs writing the same files = corrupt state
        buildDiscarder(logRotator(numToKeepStr: '30'))
        timeout(time: 2, unit: 'HOURS')    // a hung download must not block the next run forever
    }

    stages {
        stage('deps') {
            steps {
                // Same upgrade the cron line did, into $HOME/.local (persisted volume).
                // rich/pyyaml are imported directly by the scripts and are NOT
                // guaranteed transitive deps of anipy — pin them here explicitly.
                sh 'pip install --user --break-system-packages --upgrade anipy-api anipy-cli rich pyyaml'
            }
        }

        stage('sync') {
            steps {
                // The old crontab chained with `;` so the consolidator ran even when
                // the sync died — which is exactly when half-downloaded episodes are
                // sitting unorganised. catchError keeps that behaviour: the build
                // still goes red, but the consolidate stage still runs.
                catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
                    sh '''
                        mkdir -p "$STATE"
                        # stale summary from the previous build must not be reported as this one's
                        rm -f summary.json
                        [ -f "$STATE/watchlist.json" ] && cp "$STATE/watchlist.json" watchlist.json
                        python3 anilist_sync.py
                    '''
                }
            }
        }

        stage('consolidate') {
            steps {
                sh 'python3 jellyfin_consolidator.py'
            }
        }
    }

    post {
        // Runs even when the sync fails partway: episodes already downloaded are
        // recorded, so a retry does not fetch them again.
        always {
            sh 'mkdir -p "$STATE"; [ -f watchlist.json ] && cp watchlist.json "$STATE/watchlist.json" || true'
        }
        success {
            script {
                def took = currentBuild.durationString.replace(' and counting', '')
                if (changed()) {
                    notify("🍿 New episodes are in — took ${took}\n\n${summaryBody()}")
                } else if (!scheduled()) {
                    // manual run with nothing to do: confirm it ran, don't pretend it did work
                    notify("😴 All caught up — nothing new (${took})\n\n${summaryBody()}")
                }
            }
        }
        failure {
            notify("🔥 Sync broke — build ${env.BUILD_NUMBER}\n\n${summaryBody()}\n\nLog: ${env.BUILD_URL}console")
        }
        aborted {
            notify("⏱ Sync stopped early — build ${env.BUILD_NUMBER} hit the 2h limit or was cancelled\n\n${summaryBody()}")
        }
    }
}
