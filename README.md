# anime-sync

Pulls your AniList "Watching" list, downloads new episodes, and organizes
them into a Jellyfin/Plex-friendly library layout. Includes a standalone
fallback path that keeps working even when the main scraping backend
(AllAnime) is rate-limited, captcha-gated, or otherwise down.

```
AniList (WATCHING list)
        │
        ▼
anilist_sync.py  ──uses──▶ anipy_api ──▶ AllAnime
        │
        ▼
jellyfin_consolidator.py  ──▶  Canonical/Season NN/Show - SNNENN.ext
```

If the main path above fails (AllAnime captcha-gates you, or an upstream
`anipy-api` release breaks), `ani-cli` steps in as a second, independent
path against the same AniList account, and falls back to `anidb.app` if
AllAnime itself is unreachable:

```
AniList (WATCHING list)
        │
        ▼
ani-cli --sync ──uses──▶ ani-cli-allanime.py ──▶ AllAnime
        │                        │
        │                  (on failure)
        │                        ▼
        │                ani-cli-anidb.py ──▶ anidb.app
        ▼
jellyfin_consolidator.py
```

Both paths write into the same shared state (`watchlist.json`), so
whichever one actually gets an episode down is the one that "wins" —
the other one won't re-download it.

## Components

| File | Role |
|---|---|
| `anilist_sync.py` | Main path. Uses [`anipy-api`](https://github.com/sdaqo/anipy-cli) to pull your AniList WATCHING list and download new episodes via AllAnime. |
| `jellyfin_consolidator.py` | Renames/moves downloaded files into `Canonical/Season NN/Canonical - SNNENN.ext`, using `aliases.yaml` (title → canonical folder) and `seasons.yaml` (folder → forced season override) if present. |
| `ani-cli` | Standalone fallback, modeled on [pystardust/ani-cli](https://github.com/pystardust/ani-cli)'s UX. Also works as a normal interactive `ani-cli` (search/play via fzf+mpv) independent of any of this. |
| `ani-cli-allanime.py` | Helper: wraps `anipy_api`'s `AllAnimeProvider` as a 3-verb CLI (`search` / `episodes` / `video`) so `ani-cli` gets the exact same scraping mechanics as `anilist_sync.py` without reimplementing AllAnime's AES-GCM signed-request crypto in shell. |
| `ani-cli-anidb.py` | Helper: same 3-verb CLI, scraping `anidb.app` instead. Used only when AllAnime itself returns an error (captcha, crypto/token rejection, etc). Uses `curl_cffi` (browser TLS impersonation) since anidb.app sits behind Cloudflare. |
| `summary_text.py` | Formats a run's `summary.json` (or `fallback_summary.json`) into human-readable text, for logs and Telegram notifications. |
| `Jenkinsfile` | Runs the whole thing on a schedule, with fallback automation and Telegram alerts (see below). |

## Requirements

- Python 3.10+
- `pip install anipy-api anipy-cli rich pyyaml yt-dlp curl_cffi`
- `ffmpeg` (yt-dlp uses it as a muxing fallback)
- `fzf`, `mpv` — only needed for `ani-cli`'s interactive (non-`--sync`) mode

## Setup

1. **AniList token.** `anilist_sync.py` and `ani-cli --sync` both need a
   config anipy-cli itself understands. Easiest path: run any `anipy-cli`
   command once and follow its login flow, or drop a token directly into
   `~/.config/anipy-cli/config.yaml`:
   ```yaml
   anilist_token: "your-token-here"
   ```
   `ani-cli` reads the same file (`ANIPY_CONFIG` env var to override).

2. **Library root.** Set `ANIME_ROOT` to wherever your anime library lives
   — this is used by the consolidator, `anilist_sync.py`'s download path,
   and `ani-cli --sync`.

3. **Optional curation files**, at the root of your library:
   - `aliases.yaml` — maps alternate titles to the folder you actually want
     used, e.g. when AniList's title differs from your existing folder name.
     `anilist_sync.py` appends to this automatically as it discovers shows;
     you only need to hand-edit it to fix a bad guess.
   - `seasons.yaml` — forces a season number for folders the consolidator's
     regex-based guesser gets wrong (common with Japanese season markers
     like "San no Shou" or "2-nensei-hen").

## Running it without Jenkins

Nothing here requires Jenkins — it's just three scripts you can run by hand
or from any scheduler (cron, systemd timer, etc).

```sh
# main path
python3 anilist_sync.py
python3 jellyfin_consolidator.py

# fallback path (standalone, no Jenkins) - same AniList account, same library
./ani-cli --sync
```

`ani-cli --sync` runs a full pass on its own: pulls WATCHING, downloads
anything new (AllAnime first, anidb.app if AllAnime errors), and runs the
consolidator itself when it's done. Useful as a cron job in its own right,
or as a manual "is the main pipeline actually stuck?" check.

Interactive mode still works exactly like upstream ani-cli — run it with
no flags for the usual fzf search → pick episode → mpv flow.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ANIPY_CONFIG` | `~/.config/anipy-cli/config.yaml` | Where the AniList token lives |
| `ANIME_ROOT` | *(none — set this)* | Library root (both `--sync` and the consolidator) |
| `ANI_CLI_ALLANIME_HELPER` | `~/scripts/ani-cli-allanime.py` | Path to the AllAnime helper |
| `ANI_CLI_ANIDB_HELPER` | `~/scripts/ani-cli-anidb.py` | Path to the anidb.app helper |
| `ANI_CLI_MAIN_WATCHLIST` | `~/scripts/anime-state/watchlist.json` | Shared state `anilist_sync.py` also reads/writes |
| `ANI_CLI_CONSOLIDATOR` | `~/scripts/anime-sync/jellyfin_consolidator.py` | Consolidator script `--sync` runs at the end |
| `ANI_CLI_SKIP_CONSOLIDATE` | `0` | Set to `1` to skip `--sync`'s own consolidator call (e.g. if something else runs it right after) |
| `ANI_CLI_FALLBACK_SUMMARY` | `~/.local/state/ani-cli/fallback_summary.json` | Where `--sync` writes its run summary |
| `ANI_CLI_MODE` | `sub` | `sub` or `dub` |
| `ANI_CLI_QUALITY` | `best` | Passed straight through to quality selection |

## Running it with Jenkins

The `Jenkinsfile` automates the whole thing on a 2-hour cron and adds:

- **Automatic fallback.** If the main `sync` stage hard-fails, or exits
  clean but every show failed/mixed results came back (soft failure —
  e.g. a captcha or a broken upstream release), the `fallback-sync` stage
  runs `ani-cli --sync` automatically, no manual intervention.
- **Telegram notifications**, including what the fallback actually grabbed
  (not just "it ran").
- **State reconciliation.** Both paths write to the same `watchlist.json`
  (mounted outside the workspace so it survives workspace wipes), so a show
  the fallback recovers won't get retried against a still-broken AllAnime on
  the next scheduled run.

### Pipeline stages

1. **deps** — installs/upgrades all Python deps into `$HOME/.local`.
2. **sync** — runs `anilist_sync.py`. Graded `unstable` if any show came
   back as an error rather than a hard pipeline failure (the script itself
   always exits 0).
3. **fallback-sync** *(conditional)* — runs only when `sync` failed or was
   graded unstable. Runs `ani-cli --sync` against the same watchlist.
4. **consolidate** — runs the Jellyfin consolidator once, regardless of how
   the earlier stages went (so partially-downloaded episodes still get
   organized).

### Jenkins setup

- Mount a persistent directory for state, e.g. `/var/jenkins_home/anime-state`
  (set via the `STATE` environment block in the `Jenkinsfile`) — this is
  what survives workspace wipes between builds.
- Mount `/srv/bot-secrets/bot.env` (read-only) with:
  ```sh
  TELEGRAM_TOKEN=...
  TELEGRAM_CHAT_ID=...
  ```
  Telegram notification is best-effort — a missing/misconfigured secrets
  file just silently skips notifying, it never fails the build.
- The container needs `curl`, `python3`, `ffmpeg`, and network access to
  AniList, AllAnime/mkissa, and anidb.app.
- Trigger builds via the standard Jenkins REST API (crumb + basic auth),
  same as any other job — nothing anime-sync-specific about that part.

## Known limitations

- **Blind-search ceiling.** Both fallback paths (AllAnime and anidb) prefer
  an already-known/trusted identifier from `watchlist.json`, but fall back
  to a title search for shows the main pipeline hasn't mapped yet. A search
  match isn't guaranteed to be the right show for very generic titles —
  interactive use lets you pick from the list; unattended `--sync` takes the
  best available match.
- **anidb.app ranks by franchise popularity, not exact match** — a new/niche
  show's title can land behind older same-name franchise entries in search
  results. `ani-cli` prefers an exact (case-insensitive) title match over
  "first result" for this reason, but an exact match isn't guaranteed to
  exist if AniList and anidb.app disagree on the title text.
- **anidb.app sits behind Cloudflare.** `ani-cli-anidb.py` uses `curl_cffi`'s
  browser TLS impersonation to get through it; if anidb.app tightens its
  protection further, this may need revisiting.
- Neither fallback path knows anything the other doesn't share via
  `watchlist.json` — if you run `ani-cli --sync` completely detached from
  the main pipeline's state file, you lose the "don't redownload what's
  already there" guarantee across paths (each still tracks its own state
  independently, so at worst you get a duplicate download that the
  consolidator will resolve by keeping the larger file).
