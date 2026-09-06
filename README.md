# anime-sync

Pulls your AniList "Watching" list, downloads new episodes, and organizes
them into a Jellyfin/Plex-friendly library layout. Streaming sources break
often, so it tries several providers in turn rather than trusting any one.

```
AniList (WATCHING list)
        │
        ▼
ani-cli --sync
        │
        ├─▶ ani-cli-allanime.py ──▶ AllAnime     (dead since 2026-08; kept for episode lists)
        ├─▶ ani-cli-animehub.py ──▶ animehub     (fast, per-season numbering)
        ├─▶ ani-cli-anikoto.py  ──▶ anikoto.cz   (widest current-season catalogue)
        └─▶ ani-cli-anidb.py    ──▶ anidb.app    (absolute numbering, translated)
        │
        ▼
jellyfin_consolidator.py  ──▶  Canonical/Season NN/Show - SNNENN.ext
```

Each provider is tried in order until one returns a stream, so a single
site going down (or dying outright, as AllAnime did) doesn't stop the
pipeline. Progress is tracked per AniList id, not per provider, so
whichever source gets an episode, the others won't re-download it.

`anilist_sync.py` was the original driver via `anipy-api`. It is still in
the repo but **no longer wired in**: anipy-api 3.9.0 dropped `allanime`
from its provider registry, which broke it. See "Running it with Jenkins".

## Components

| File | Role |
|---|---|
| `ani-cli` | The sync driver. `--sync` pulls your WATCHING list and downloads new episodes, trying each provider in turn. Also works as a normal interactive ani-cli (fzf + mpv). |
| `anilist_sync.py` | The original `anipy-api` driver. Kept for reference; **not wired in** (anipy-api 3.9.0 removed the `allanime` provider it resolves by name). |
| `jellyfin_consolidator.py` | Renames/moves downloaded files into `Canonical/Season NN/Canonical - SNNENN.ext`, using `aliases.yaml` (title → canonical folder) and `seasons.yaml` (folder → forced season override) if present. |
| `ani-cli-allanime.py` | Helper: wraps `anipy_api`'s `AllAnimeProvider` as a 3-verb CLI (`search` / `episodes` / `video`) reusing anipy-api's AES-GCM signed-request crypto rather than reimplementing it in shell. AllAnime is dead for video; still consulted for episode lists. |
| `ani-cli-animehub.py` | Helper: same 3-verb CLI for `animehub`, anipy-api's own AllAnime replacement. Currently the primary source. Numbers episodes per-season from 1, like AniList. |
| `ani-cli-anikoto.py` | Helper: same 3-verb CLI for `anikoto.cz`. Widest current-season catalogue - it carried BLEACH TYBW "The Calamity" when no other provider had it. Endpoints mirror [ani-cli-rs](https://github.com/vorlie/ani-cli-rs), which is actively maintained against this site. |
| `ani-cli-anidb.py` | Helper: same 3-verb CLI, scraping `anidb.app` instead. Used only when AllAnime itself returns an error (captcha, crypto/token rejection, etc). Uses `curl_cffi` (browser TLS impersonation) since anidb.app sits behind Cloudflare. |
| `anime-sync-tui.py` | Optional standalone TUI: setup, status, run, schedule. Nothing else depends on it. |
| `summary_text.py` | Formats a run's `summary.json` (or `fallback_summary.json`) into human-readable text, for logs and Telegram notifications. |
| `Jenkinsfile` | Runs the whole thing on a schedule, with fallback automation and Telegram alerts (see below). |

## Requirements

- Python 3.10+
- `pip install anipy-api rich pyyaml yt-dlp curl_cffi`
- `ffmpeg` (yt-dlp uses it as a muxing fallback)
- `fzf`, `mpv` - only needed for `ani-cli`'s interactive (non-`--sync`) mode

## Setup

1. **AniList token.** `ani-cli --sync` needs a
   config anipy-cli itself understands. Easiest path: run any `anipy-cli`
   command once and follow its login flow, or drop a token directly into
   `~/.config/anipy-cli/config.yaml`:
   ```yaml
   anilist_token: "your-token-here"
   ```
   `ani-cli` reads the same file (`ANIPY_CONFIG` env var to override).

2. **Library root.** Set `ANIME_ROOT` to wherever your anime library lives
   - this is used by the consolidator, `anilist_sync.py`'s download path,
   and `ani-cli --sync`.

3. **Optional curation files**, at the root of your library:
   - `aliases.yaml` - maps alternate titles to the folder you actually want
     used, e.g. when AniList's title differs from your existing folder name.
     `anilist_sync.py` appends to this automatically as it discovers shows;
     you only need to hand-edit it to fix a bad guess.
   - `seasons.yaml` - forces a season number for folders the consolidator's
     regex-based guesser gets wrong (common with Japanese season markers
     like "San no Shou" or "2-nensei-hen").

## Quick start (interactive)

If you don't want to hand-edit configs or write a crontab, run the TUI:

```sh
./anime-sync-tui.py
```

It's a standalone control panel - the Jenkins pipeline and the cron path
don't know it exists, so using it is entirely optional. It offers:

```
  1  Check setup          dependency + config + path check, tells you what's missing
  2  Configure            library root and AniList token (writes anipy-cli's config)
  3  Sync now             runs a full pass with live output
  4  Sync now, no consolidate
  5  Library status       per-show: what the pipeline thinks it has vs files on disk
  6  Schedule (cron)      shows the cron line, optionally installs it
```

Start with **1** - it names the exact `pip install` for anything absent. It
degrades to plain text when `rich` isn't installed yet, since it's the tool
you run *before* installing dependencies. Its own settings live in
`~/.config/anime-sync/tui.json`; the AniList token goes to the same
`~/.config/anipy-cli/config.yaml` everything else reads.

In the status view, `~N` means the count is for the whole franchise folder
(the consolidator merges seasons) and `?` means the folder wasn't found -
neither is reported as `0`, so a healthy show never looks empty.

## Running it without Jenkins

Nothing here requires Jenkins - it's just three scripts you can run by hand
or from any scheduler (cron, systemd timer, etc).

```sh
./ani-cli --sync          # pull WATCHING, download new episodes, consolidate
python3 jellyfin_consolidator.py   # only needed if you skipped consolidation
```

`ani-cli --sync` runs a full pass on its own: pulls WATCHING, downloads
anything new (trying each provider in turn), and runs the consolidator
itself when it's done. Useful as a cron job in its own right,
or as a manual "is the main pipeline actually stuck?" check.

Interactive mode still works exactly like upstream ani-cli - run it with
no flags for the usual fzf search → pick episode → mpv flow.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ANIPY_CONFIG` | `~/.config/anipy-cli/config.yaml` | Where the AniList token lives |
| `ANIME_ROOT` | *(none - set this)* | Library root (both `--sync` and the consolidator) |
| `ANI_CLI_ALLANIME_HELPER` | *(next to `ani-cli`)* | Path to the AllAnime helper |
| `ANI_CLI_ANIDB_HELPER` | *(next to `ani-cli`)* | Path to the anidb.app helper |
| `ANI_CLI_ANIMEHUB_HELPER` | *(next to `ani-cli`)* | Path to the animehub helper |
| `ANI_CLI_ANIKOTO_HELPER` | *(next to `ani-cli`)* | Path to the anikoto.cz helper |
| `ANI_CLI_MAIN_WATCHLIST` | `~/scripts/anime-state/watchlist.json` | Shared state `anilist_sync.py` also reads/writes |
| `ANI_CLI_CONSOLIDATOR` | `~/scripts/anime-sync/jellyfin_consolidator.py` | Consolidator script `--sync` runs at the end |
| `ANI_CLI_SKIP_CONSOLIDATE` | `0` | Set to `1` to skip `--sync`'s own consolidator call (e.g. if something else runs it right after) |
| `ANI_CLI_FALLBACK_SUMMARY` | `~/.local/state/ani-cli/fallback_summary.json` | Where `--sync` writes its run summary |
| `ANI_CLI_MODE` | `sub` | `sub` or `dub` |
| `ANI_CLI_QUALITY` | `best` | Passed straight through to quality selection |

## Running it with Jenkins

The `Jenkinsfile` automates the whole thing on a 2-hour cron and adds:

- **Telegram notifications** saying what actually downloaded or failed.
- **Alert suppression.** Scheduled runs stay quiet when nothing happened or
  when the same shows fail the same way as last time; any *change* in what
  is broken breaks through, and a manual run always answers.
- **Durable state.** `watchlist.json` lives outside the workspace, so a
  Jenkins workspace wipe doesn't make the next run re-download everything.

### Pipeline stages

1. **deps** - installs the Python deps into `$HOME/.local`. `anipy-api` is
   **pinned**: an unattended `--upgrade` here is what broke this job when
   3.9.0 dropped the `allanime` provider.
2. **sync** - runs `./ani-cli --sync`, writing `summary.json`. Graded
   `unstable` if any show failed, since the script exits 0 either way.
3. **consolidate** - runs the Jellyfin consolidator regardless of how sync
   went, so partially-downloaded episodes still get organized.

There is no separate fallback stage: provider fallback happens inside
`ani-cli --sync` itself, so the same behaviour applies however you run it.

### Jenkins setup

- Mount a persistent directory for state, e.g. `/var/jenkins_home/anime-state`
  (set via the `STATE` environment block in the `Jenkinsfile`) - this is
  what survives workspace wipes between builds.
- Mount `/srv/bot-secrets/bot.env` (read-only) with:
  ```sh
  TELEGRAM_TOKEN=...
  TELEGRAM_CHAT_ID=...
  ```
  Telegram notification is best-effort - a missing/misconfigured secrets
  file just silently skips notifying, it never fails the build.
- The container needs `curl`, `python3`, `ffmpeg`, and network access to
  AniList, AllAnime/mkissa, and anidb.app.
- Trigger builds via the standard Jenkins REST API (crumb + basic auth),
  same as any other job - nothing anime-sync-specific about that part.

### Setting up the Telegram bot

1. Message [`@BotFather`](https://t.me/BotFather) on Telegram, send
   `/newbot`, and follow the prompts (pick a name, pick a username ending
   in `bot`). It replies with a token that looks like
   `123456789:AAF...` - that's your `TELEGRAM_TOKEN`.
2. Send your new bot any message first (bots can't message you until you've
   messaged them at least once).
3. Get your chat ID: message [`@userinfobot`](https://t.me/userinfobot) (or
   `@RawDataBot`) and it'll reply with your numeric `id` - that's your
   `TELEGRAM_CHAT_ID`. (For a group chat instead of DMs: add your bot to the
   group, send a message, then check
   `https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates` for the
   group's `chat.id`, which will be negative.)
4. Put both values in `bot.env`:
   ```sh
   TELEGRAM_TOKEN=123456789:AAF...
   TELEGRAM_CHAT_ID=123456789
   ```
5. Mount that file read-only into the Jenkins container at
   `/srv/bot-secrets/bot.env` (or point the `notify()` function in the
   `Jenkinsfile` at wherever you put it). No further setup needed - the
   pipeline reads it fresh on every notification attempt.

## Known limitations

- **Provider titles disagree with AniList.** Providers index shows under
  romaji or differently-worded season names ("... Master Swordsman II" vs
  "... Season 2"), and they rank search results by popularity, not
  relevance - searching "Dara-san of the Reiwa Era" returns *Food Wars*
  first. `ani-cli` therefore requires an exact title match, and accepts a
  non-exact one **only when the search returned a single hit**. Anything
  ambiguous is declined and logged rather than guessed at, because filing
  someone else's episode under your show is worse than missing one. Add a
  mapping to `anidb_aliases.yaml` (AniList title → provider title) to fix a
  show that keeps being declined.
- **Absolute episode numbering.** anidb sometimes numbers a sequel cour
  across the whole series (BLEACH TYBW is 41-44, Re:ZERO S4 is 67-78) while
  AniList restarts at 1. This is translated automatically, but only when the
  title matched exactly and the episode list is contiguous - a gappy list is
  declined instead of mis-mapped. animehub numbers per-season, so it is
  unaffected.
- **anidb.app sits behind Cloudflare.** `ani-cli-anidb.py` uses `curl_cffi`'s
  browser TLS impersonation to get through it; if anidb.app tightens its
  protection further, this may need revisiting.
- **`aliases.yaml` serves two masters.** The consolidator reads it as
  folder → canonical, where collapsing "X Season 4" → "X" is correct. The
  downloader reads it to pick a download folder, where that same mapping
  destroys the only season signal the consolidator gets. `ani-cli` now
  refuses an alias that strips a season the title had - but if you hand-edit
  that file, check both consumers.

## Acknowledgments

This project is heavily inspired by, and directly builds on top of, two
existing tools rather than reimplementing their hard parts from scratch:

- [**anipy-cli**](https://github.com/sdaqo/anipy-cli) by sdaqo - `anilist_sync.py`
  and the `ani-cli-allanime.py` helper are thin wrappers around its
  `AllAnimeProvider` and AniList integration. All of the actual AllAnime
  scraping and signed-request crypto is anipy-cli's, used as-is rather than
  re-derived.
- [**ani-cli**](https://github.com/pystardust/ani-cli) by pystardust and
  contributors - the `ani-cli` script here is a direct port of its UX,
  structure, and a good deal of its actual shell code (menu flow, player
  detection, quality selection, download handling), adapted to call
  anipy-cli's provider instead of scraping directly, plus the AniList
  `--sync` mode and anidb.app fallback added on top.

Both are licensed GPL-3.0, which is why this repository is too - see
[`LICENSE`](LICENSE).

## License

[GPL-3.0](LICENSE), consistent with both projects this builds on.
