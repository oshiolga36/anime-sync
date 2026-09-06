#!/usr/bin/env python3
"""Interactive setup and control panel for anime-sync.

Standalone on purpose: the Jenkins pipeline and the cron path both drive
`ani-cli --sync` directly and know nothing about this file. It exists so a
user with neither Jenkins nor a hand-written crontab can get set up, see
what state they are in, run a sync, and schedule one.

Deliberately a numbered menu rather than a curses app: this is the tool you
run BEFORE the dependencies are installed, so it must work on a bare
terminal, over SSH, and without rich (which it uses only if importable).

Run:  ./anime-sync-tui.py
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANI_CLI = HERE / "ani-cli"
CONSOLIDATOR = HERE / "jellyfin_consolidator.py"
ANIPY_CONFIG = Path(os.environ.get("ANIPY_CONFIG", Path.home() / ".config/anipy-cli/config.yaml"))
TUI_CONFIG = Path.home() / ".config/anime-sync/tui.json"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "ani-cli"
WATERMARK = STATE_DIR / "sync_watermark.json"

# rich is optional - a setup tool must run before deps exist. ponytail: the
# plain-text path is the real one, colour is a nicety layered on top.
try:
    from rich.console import Console
    from rich.table import Table

    _c = Console()

    def say(msg=""):
        _c.print(msg)

    def table(title, cols, rows):
        t = Table(title=title, header_style="bold")
        for col in cols:
            t.add_column(col)
        for r in rows:
            t.add_row(*[str(x) for x in r])
        _c.print(t)
except ImportError:  # noqa: BLE001
    def say(msg=""):
        print(str(msg).replace("[bold]", "").replace("[/bold]", ""))

    def table(title, cols, rows):
        print(f"\n{title}")
        widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c))
                  for i, c in enumerate(cols)]
        print("  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
        print("  ".join("-" * w for w in widths))
        for r in rows:
            print("  ".join(str(x).ljust(w) for x, w in zip(r, widths)))


# ---------------------------------------------------------------- config

def load_cfg() -> dict:
    try:
        return json.loads(TUI_CONFIG.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cfg(cfg: dict) -> None:
    TUI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    TUI_CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


def anilist_token() -> str:
    """Read the token out of anipy-cli's config, which ani-cli --sync also reads."""
    try:
        for line in ANIPY_CONFIG.read_text().splitlines():
            if line.strip().startswith("anilist_token:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def set_anilist_token(token: str) -> None:
    ANIPY_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    lines, done = [], False
    if ANIPY_CONFIG.exists():
        for line in ANIPY_CONFIG.read_text().splitlines():
            if line.strip().startswith("anilist_token:"):
                lines.append(f'anilist_token: "{token}"')
                done = True
            else:
                lines.append(line)
    if not done:
        lines.append(f'anilist_token: "{token}"')
    ANIPY_CONFIG.write_text("\n".join(lines) + "\n")


def sync_env(cfg: dict) -> dict:
    """The exact environment `ani-cli --sync` needs. Mirrors the Jenkinsfile."""
    env = dict(os.environ)
    env["ANIME_ROOT"] = cfg.get("anime_root", "")
    env["ANI_CLI_ALLANIME_HELPER"] = str(HERE / "ani-cli-allanime.py")
    env["ANI_CLI_ANIDB_HELPER"] = str(HERE / "ani-cli-anidb.py")
    env["ANI_CLI_ANIMEHUB_HELPER"] = str(HERE / "ani-cli-animehub.py")
    env["ANI_CLI_CONSOLIDATOR"] = str(CONSOLIDATOR)
    if cfg.get("watchlist"):
        env["ANI_CLI_MAIN_WATCHLIST"] = cfg["watchlist"]
    return env


# ---------------------------------------------------------------- checks

def doctor(cfg: dict) -> bool:
    rows, ok = [], True

    def add(name, good, detail):
        nonlocal ok
        rows.append(("OK " if good else "-- ", name, detail))
        if not good:
            ok = False

    for mod in ("anipy_api", "curl_cffi", "m3u8", "yaml"):
        try:
            __import__(mod)
            add(f"python: {mod}", True, "importable")
        except ImportError:
            add(f"python: {mod}", False, f"pip install {'pyyaml' if mod == 'yaml' else mod}")

    for exe, why in (("yt-dlp", "downloader"), ("ffmpeg", "muxing fallback"), ("python3", "required")):
        p = shutil.which(exe)
        add(f"binary: {exe}", bool(p), p or f"missing ({why})")

    add("ani-cli present", ANI_CLI.exists(), str(ANI_CLI))
    tok = anilist_token()
    add("AniList token", bool(tok), f"{ANIPY_CONFIG} ({'set' if tok else 'not set'})")

    root = cfg.get("anime_root", "")
    add("library root", bool(root) and Path(root).is_dir(),
        root or "not configured - option 2")

    table("Setup check", ("", "item", "detail"), rows)
    say("\n[bold]All good.[/bold]" if ok else "\nFix the '--' rows above, then re-check.")
    return ok


SEASON_RE = r"\s*(?:\b(?:Season|Part)\s*\d+|\bS\d{1,2}\b|\d+(?:nd|rd|th)\s+Season)"


def count_files(root: str, folder_name: str) -> str:
    """Episodes on disk for a show. Approximate by nature: the consolidator
    renames a downloaded 'X Season 2' folder to canonical 'X/Season 02', so
    the watchlist's folder_name often no longer exists. Try the literal name
    first, then the season-stripped canonical. Returns '?' when neither is
    found rather than '0' - reporting "no files" for a show that is actually
    complete is the same misleading-signal bug this project keeps hitting."""
    import re

    if not root:
        return "?"
    # folder_name still holds characters the filesystem never gets (BLEACH's colon)
    safe = "".join(c for c in folder_name if c not in '\\/*?:"<>|').strip()
    exact = Path(root) / safe
    if exact.is_dir():
        return str(len(list(exact.rglob("*.mp4"))))
    canon = re.sub(SEASON_RE, "", safe, flags=re.IGNORECASE).strip()
    if canon and canon != safe:
        d = Path(root) / canon
        if d.is_dir():
            # whole franchise folder - all seasons, so mark it approximate
            return f"~{len(list(d.rglob('*.mp4')))}"
    return "?"


def status(cfg: dict) -> None:
    root = cfg.get("anime_root", "")
    wl_path = cfg.get("watchlist") or str(HERE / "watchlist.json")
    try:
        wl = json.loads(Path(wl_path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        wl = {}
    try:
        marks = json.loads(WATERMARK.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        marks = {}

    if not wl and not marks:
        say("No state yet - run a sync first (option 3).")
        return

    rows = []
    for aid, entry in sorted(wl.items(), key=lambda kv: str(kv[1].get("folder_name", ""))):
        if not isinstance(entry, dict):
            continue
        name = entry.get("folder_name", aid)
        have = entry.get("last_downloaded", -1)
        mark = marks.get(aid, "-")
        rows.append((name[:44], have, mark, count_files(root, name)))

    table(f"Watchlist ({len(rows)} shows)  root={root or 'unset'}",
          ("show", "last ep", "ani-cli mark", "files"), rows)
    say("\n'last ep' is what the pipeline believes it has; 'files' is what is on disk")
    say("(~N counts the whole franchise folder, ? means the folder was not found).")
    say("A show whose 'last ep' far exceeds its files is usually a stalled watermark.")


# ---------------------------------------------------------------- actions

def run_sync(cfg: dict, consolidate: bool = True) -> None:
    if not cfg.get("anime_root"):
        say("Set the library root first (option 2).")
        return
    if not anilist_token():
        say("Set your AniList token first (option 2).")
        return
    env = sync_env(cfg)
    if not consolidate:
        env["ANI_CLI_SKIP_CONSOLIDATE"] = "1"
    say(f"\nRunning: {ANI_CLI} --sync   (Ctrl-C to stop)\n")
    try:
        subprocess.run([str(ANI_CLI), "--sync"], env=env, check=False)
    except KeyboardInterrupt:
        say("\nStopped. Downloads already finished are kept.")
    except OSError as e:
        say(f"Could not run {ANI_CLI}: {e}")


def cron_line(cfg: dict) -> str:
    env = sync_env(cfg)
    keys = ("ANIME_ROOT", "ANI_CLI_ALLANIME_HELPER", "ANI_CLI_ANIDB_HELPER",
            "ANI_CLI_ANIMEHUB_HELPER", "ANI_CLI_CONSOLIDATOR", "ANI_CLI_MAIN_WATCHLIST")
    assigns = " ".join(f'{k}="{env[k]}"' for k in keys if env.get(k))
    log = Path.home() / ".local/state/ani-cli/sync.log"
    return f'0 */2 * * * {assigns} {ANI_CLI} --sync >> {log} 2>&1'


def schedule(cfg: dict) -> None:
    if not cfg.get("anime_root"):
        say("Set the library root first (option 2).")
        return
    line = cron_line(cfg)
    say("\nEvery 2 hours, same cadence as the Jenkins job:\n")
    say(f"  {line}\n")
    if input("Install into your crontab now? [y/N] ").strip().lower() != "y":
        say("Not installed. Copy the line above into `crontab -e` whenever you like.")
        return
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        say("No `crontab` command found - use a systemd timer instead.")
        return
    if str(ANI_CLI) in cur:
        say("An ani-cli entry is already in your crontab; leaving it alone.")
        return
    new = (cur.rstrip("\n") + "\n" if cur.strip() else "") + line + "\n"
    p = subprocess.run(["crontab", "-"], input=new, text=True)
    say("Installed." if p.returncode == 0 else "crontab refused the entry.")


def configure(cfg: dict) -> dict:
    say("\nBlank keeps the current value.\n")
    cur_root = cfg.get("anime_root", "")
    root = input(f"Library root [{cur_root or 'e.g. /srv/media/Anime'}]: ").strip() or cur_root
    if root:
        p = Path(root).expanduser()
        if not p.is_dir():
            if input(f"{p} does not exist. Create it? [y/N] ").strip().lower() == "y":
                p.mkdir(parents=True, exist_ok=True)
            else:
                say("Keeping it unset until the directory exists.")
                p = None
        if p:
            cfg["anime_root"] = str(p)

    tok = anilist_token()
    say(f"\nAniList token is currently {'set' if tok else 'NOT set'}.")
    say("Get one at: https://anilist.co/settings/developer (create a client, then")
    say("use its implicit-grant token). anipy-cli's own login writes the same file.")
    new_tok = input("Token (blank to keep): ").strip()
    if new_tok:
        set_anilist_token(new_tok)
        say(f"Written to {ANIPY_CONFIG}")

    wl = input(f"\nShared watchlist.json path [{cfg.get('watchlist', '(default)')}]: ").strip()
    if wl:
        cfg["watchlist"] = wl

    save_cfg(cfg)
    say(f"\nSaved to {TUI_CONFIG}")
    return cfg


MENU = """
[bold]anime-sync[/bold]

  1  Check setup
  2  Configure (library root, AniList token)
  3  Sync now
  4  Sync now, skip consolidation
  5  Library status
  6  Schedule (cron)
  0  Quit
"""


def main() -> int:
    if not ANI_CLI.exists():
        say(f"ani-cli not found next to this script ({ANI_CLI}). Run it from the repo.")
        return 1
    cfg = load_cfg()
    if not cfg.get("anime_root"):
        say("First run - let's configure.")
        cfg = configure(cfg)
    while True:
        say(MENU)
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            say()
            return 0
        actions = {
            "1": lambda: doctor(cfg),
            "2": lambda: cfg.update(configure(cfg)),
            "3": lambda: run_sync(cfg, consolidate=True),
            "4": lambda: run_sync(cfg, consolidate=False),
            "5": lambda: status(cfg),
            "6": lambda: schedule(cfg),
        }
        if choice in ("0", "q", "quit", "exit"):
            return 0
        if choice in actions:
            actions[choice]()
        else:
            say("Pick a number from the menu.")


def demo() -> None:
    # no network, no side effects: the parsing/composition that can silently
    # produce a wrong command line or a wrong token file.
    import tempfile

    global ANIPY_CONFIG
    with tempfile.TemporaryDirectory() as d:
        ANIPY_CONFIG = Path(d) / "config.yaml"
        set_anilist_token("abc123")
        assert anilist_token() == "abc123", anilist_token()
        set_anilist_token("def456")                      # replaces, does not append
        assert anilist_token() == "def456"
        assert ANIPY_CONFIG.read_text().count("anilist_token") == 1

    env = sync_env({"anime_root": "/tmp/x"})
    assert env["ANIME_ROOT"] == "/tmp/x"
    assert env["ANI_CLI_ANIMEHUB_HELPER"].endswith("ani-cli-animehub.py")
    assert "ANI_CLI_MAIN_WATCHLIST" not in sync_env({"anime_root": "/tmp/x"})

    line = cron_line({"anime_root": "/tmp/x"})
    assert line.startswith("0 */2 * * * ") and "--sync" in line and 'ANIME_ROOT="/tmp/x"' in line
    print("anime-sync-tui selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
        sys.exit(0)
    sys.exit(main())
