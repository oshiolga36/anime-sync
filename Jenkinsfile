pipeline {
    agent any

    // ponytail: manual-only, no triggers block. Re-add
    // `triggers { cron('H H/2 * * *') }` here to go back to every-2h.

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
                sh '''
                    mkdir -p "$STATE"
                    [ -f "$STATE/watchlist.json" ] && cp "$STATE/watchlist.json" watchlist.json
                    python3 anilist_sync.py
                '''
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
    }
}
