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

// new / mixed / errors / idle / unknown — see summary_text.py
def syncState() {
    return sh(script: 'python3 summary_text.py --state', returnStdout: true).trim()
}

// ['notify'|'suppress'|'recovered'|'quiet', <recovered titles>] from post/always
def alert() {
    def lines = readFile('alert.txt').trim().split('\n')
    return [lines[0], lines.size() > 1 ? lines[1] : '']
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
                // yt-dlp is for ani-cli's downloads (ffmpeg alone can't handle
                // every embed host's auth/HLS quirks). curl_cffi is for
                // ani-cli-anidb.py - anidb.app sits behind Cloudflare and plain
                // requests/curl gets the JS challenge.
                //
                // anipy-api is PINNED, deliberately. It is no longer the sync
                // driver (see the sync stage) - it is kept only because
                // ani-cli-allanime.py imports AllAnimeProvider from it directly.
                // An unpinned `--upgrade` here is what silently broke this job
                // on 2026-08-08: 3.9.0 dropped 'allanime' from its provider
                // registry, so anilist_sync.py failed all 15 shows with
                // "Provider 'allanime' not found" every run for a day. Never
                // let an unattended pipeline float its own dependency.
                sh 'pip install --user --break-system-packages anipy-api==3.9.0 && pip install --user --break-system-packages --upgrade rich pyyaml yt-dlp curl_cffi'
            }
        }

        // ani-cli is the sync driver as of 2026-08-09, replacing anilist_sync.py.
        // anipy-api 3.9.0 removed 'allanime' from its provider registry, so
        // anilist_sync.py (which resolves providers by name) errored on every
        // show. ani-cli reaches AllAnime by importing the provider class
        // directly, which still works, and falls back to anidb.app when the
        // site itself is captcha-gated - so it survives both failure modes that
        // have taken this job down. anilist_sync.py is kept in the repo but is
        // no longer wired in; see README for re-enabling it.
        //
        // It writes summary.json in the same {"stats":..,"shows":[..]} shape
        // anilist_sync.py did, so every notification/grading path below is
        // unchanged.
        stage('sync') {
            steps {
                // The old crontab chained with `;` so the consolidator ran even when
                // the sync died — which is exactly when half-downloaded episodes are
                // sitting unorganised. catchError keeps that behaviour: the build
                // still goes red, but the consolidate stage still runs.
                catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
                    sh '''
                        set +x
                        mkdir -p "$STATE"
                        # stale summary from the previous build must not be reported as this one's
                        rm -f summary.json
                        [ -f "$STATE/watchlist.json" ] && cp "$STATE/watchlist.json" watchlist.json
                        PATH="$HOME/.local/bin:$PATH" \
                        ANI_CLI_ALLANIME_HELPER="$(pwd)/ani-cli-allanime.py" \
                        ANI_CLI_ANIDB_HELPER="$(pwd)/ani-cli-anidb.py" \
                        ANI_CLI_MAIN_WATCHLIST="$(pwd)/watchlist.json" \
                        ANI_CLI_FALLBACK_SUMMARY="$(pwd)/summary.json" \
                        ANI_CLI_SKIP_CONSOLIDATE=1 \
                        ./ani-cli --sync
                    '''
                }
                // anilist_sync exits 0 even when individual shows fail, so the shell
                // result alone reports a green build over a broken sync. Grade the
                // build on what actually happened.
                script {
                    if (syncState() in ['errors', 'mixed']) {
                        unstable('one or more shows failed to sync')
                    }
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
            // Computed once here (post `always` runs before the status branches)
            // because it UPDATES the stored signature — calling it per branch
            // would compare a signature against itself and suppress everything.
            sh 'python3 summary_text.py --check-alert > alert.txt 2>/dev/null || echo notify > alert.txt'
        }
        success {
            script {
                def took = currentBuild.durationString.replace(' and counting', '')
                def (verdict, fixed) = alert()
                if (verdict == 'recovered') {
                    notify("💚 Downloads are working again — ${fixed}\n\n${summaryBody()}")
                } else if (syncState() == 'new') {
                    notify("🍿 New episodes are in — took ${took}\n\n${summaryBody()}")
                } else if (!scheduled()) {
                    // manual run with nothing to do: confirm it ran, don't pretend it did work
                    notify("😴 All caught up — nothing new (${took})\n\n${summaryBody()}")
                }
            }
        }
        unstable {
            script {
                def (verdict, _) = alert()
                // Same shows failing the same way as last run: stay quiet. 12 identical
                // alerts a day is how people learn to ignore alerts. A manual run always
                // answers, and any CHANGE in what is broken breaks through.
                if (verdict != 'suppress' || !scheduled()) {
                    def head = syncState() == 'mixed' ? "⚠️ Some episodes arrived, some shows failed"
                                                      : "⚠️ Nothing downloaded — shows are failing"
                    notify("${head}\n\n${summaryBody()}\n\nLog: ${env.BUILD_URL}console")
                }
            }
        }
        failure {
            script {
                notify("🔥 Sync broke — build ${env.BUILD_NUMBER}\n\n${summaryBody()}\n\nLog: ${env.BUILD_URL}console")
            }
        }
        aborted {
            notify("⏱ Sync stopped early — build ${env.BUILD_NUMBER} hit the 2h limit or was cancelled\n\n${summaryBody()}")
        }
    }
}
