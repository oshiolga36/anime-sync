#!/usr/bin/env python3
"""Pull the AniList watching list and download new episodes.

Replaces sync_mode() in the ani-cli shell script. The provider helpers are
unchanged and are still driven through their 3-verb CLI contract
(search / episodes / video, plus subs on anikoto), so a provider can still be
added or debugged on its own.

Why this exists: the shell version was 1,430 lines wrapping nine inline
`python3 -c` programs, and the bugs it produced were symptoms of that shape -
a variable that was never assigned, a function that was never called, a
duplicated call from a bad merge. None of those survive a linter.

Every rule below was paid for by a real incident; the comments say which, so
nobody "simplifies" one away later. Run --selfcheck for the offline asserts.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ----------------------------------------------------------------- config

def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


ANIME_ROOT = Path(env("ANIME_ROOT", "/data/media/Plex/Anime"))
STATE_DIR = Path(env("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "ani-cli"
WATERMARK = Path(env("ANI_CLI_WATERMARK", str(STATE_DIR / "sync_watermark.json")))
MAIN_WATCHLIST = Path(env("ANI_CLI_MAIN_WATCHLIST", str(Path.home() / "scripts/anime-state/watchlist.json")))
GAPS_FILE = Path(env("ANI_CLI_GAPS", str(STATE_DIR / "episode_gaps.json")))
HEALTH_FILE = Path(env("ANI_CLI_PROVIDER_HEALTH", str(STATE_DIR / "provider_health.json")))
SUMMARY = Path(env("ANI_CLI_FALLBACK_SUMMARY", str(STATE_DIR / "summary.json")))
ANIPY_CONFIG = Path(env("ANIPY_CONFIG", str(Path.home() / ".config/anipy-cli/config.yaml")))
CONSOLIDATOR = Path(env("ANI_CLI_CONSOLIDATOR", str(HERE / "jellyfin_consolidator.py")))

# Order matters: measured success rates, best first (2026-11-01: nineanime
# 26/26, animegg 13/13, animehub 9 ok / 13 fail).
PROVIDERS = env("ANI_CLI_PROVIDERS", "nineanime animegg animehub").split()
SUB_PROVIDER = "anikoto"          # video is encrypted, subtitle tracks are not
LIST_PROVIDER = "allanime"        # episode numbering matches AniList; video is dead

MIN_SECONDS = int(env("ANI_CLI_MIN_SECONDS", "120"))
GAP_AFTER = int(env("ANI_CLI_GAP_AFTER", "3"))
SUB_LANG = env("ANI_CLI_SUB_LANG", "en")
WANT_SUBS = env("ANI_CLI_SUBS", "1") == "1"
LANG = env("ANI_CLI_MODE", "sub")


def log(msg: str) -> None:
    print(f"\33[2K\r\033[1;34m{msg}\033[0m", flush=True)


def warn(msg: str) -> None:
    print(f"\33[2K\r\033[1;33m{msg}\033[0m", file=sys.stderr, flush=True)


def err(msg: str) -> None:
    print(f"\33[2K\r\033[1;31m{msg}\033[0m", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ state

def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


DRY_RUN = False


def save_json(path: Path, data: dict) -> None:
    """Atomic. The shell version's set_watermark truncated in place with no
    error handling and discarded its exit status, so a crash mid-write made
    every later run re-download the same episodes, silently."""
    if DRY_RUN:
        # --dry-run must not touch state: gap counters and the health file are
        # written from deep inside the decision path, so without this a "safe"
        # test run silently advances real counters.
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


# ---------------------------------------------------------------- aliases

def alias_lookup(want: str, filename: str = "anidb_aliases.yaml") -> str:
    """Exact string match, never a regex built from the title: titles contain
    regex metacharacters, and the shell version's sed form silently failed to
    match "[Oshi no Ko] Season 2" and "Fate/stay night: ... (TV)"."""
    path = ANIME_ROOT / filename
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return ""
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        # both keys and values contain ':' (Re:ZERO), so try each split point
        pos = line.find(":")
        while pos > 0:
            key = line[:pos].strip().strip('"')
            val = line[pos + 1:].strip().strip('"')
            if key == want and val:
                return val
            nxt = line.find(":", pos + 1)
            if nxt < 0:
                break
            pos = nxt
    return ""


SEASON_MARKER = re.compile(
    r"(season[ _-]*\d+|\d+(st|nd|rd|th)[ _-]*season|\bs\d{1,2}\b|\b(ii|iii|iv)\b"
    r"|part[ _-]*\d+|\d+(st|nd|rd|th)[ _-]*cour)",
    re.IGNORECASE,
)


def has_season_marker(name: str) -> bool:
    return bool(SEASON_MARKER.search(name))


def sanitize(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]', " ", name)).strip()


def resolve_folder(title: str) -> str:
    """Download folder for a show. An alias must not strip the season: the
    folder name is the ONLY season signal the consolidator gets, and a
    collapsed alias made it guess season 1 and overwrite a real episode
    (2026-09-06)."""
    mapped = alias_lookup(title, "aliases.yaml")
    if mapped and (not has_season_marker(title) or has_season_marker(mapped)):
        return mapped
    return sanitize(title)


def provider_title(provider: str, title: str) -> str:
    """Per-provider override first ("<provider>|<AniList title>"), then the
    shared alias. One shared mapping cannot serve every provider: animeheaven
    filed Tanya as "Saga of Tanya the Evil II" while anidb wanted the AniList
    spelling."""
    return alias_lookup(f"{provider}|{title}") or alias_lookup(title) or title


# --------------------------------------------------------- title matching

_DASHES = str.maketrans({"—": "-", "–": "-", "‒": "-", "―": "-",
                         "’": "'", "‘": "'", "“": '"', "”": '"'})


def title_key(name: str) -> str:
    """Fold the punctuation sites disagree on. AniList writes "Blood War - The
    Calamity" with a hyphen, 9anime with an en dash; that one character made
    the exact match fail and the only other candidate was the Calamity MOVIE,
    a different work."""
    return re.sub(r"\s+", " ", name.translate(_DASHES)).strip().lower()


QUALIFIERS = ("movie", "ova", "special", "recap")


def pick_match(hits: list[tuple[str, str]], want: str) -> list[tuple[str, bool]]:
    """Every acceptable candidate, exact matches first.

    Providers list the same show more than once (anikoto carries BLEACH "The
    Calamity" twice, one with every episode and one a single-episode stub), so
    returning only the first made success depend on search order.

    A non-exact hit is accepted ONLY when the search returned exactly one
    result - providers rank by popularity, not relevance, so "first of many"
    is how someone else's episode gets filed under this show.
    """
    key = title_key(want)
    exact = [(i, True) for i, n in hits if title_key(n) == key]
    if exact:
        return exact
    if len(hits) != 1:
        return []
    ident, name = hits[0]
    # A lone "<wanted> Movie"/"OVA" is a different work, not a near-spelling.
    nk = title_key(name)
    if nk.startswith(key + " ") and nk.rsplit(" ", 1)[-1] in QUALIFIERS \
            and key.rsplit(" ", 1)[-1] not in QUALIFIERS:
        return []
    return [(ident, False)]


# -------------------------------------------------------------- providers

def helper(provider: str) -> Path:
    override = env(f"ANI_CLI_{provider.upper()}_HELPER")
    return Path(override) if override else HERE / f"ani-cli-{provider}.py"


def run_helper(provider: str, *args: str, timeout: int = 120) -> tuple[str, str]:
    """(stdout, stderr). Never raises: a provider blowing up must not take the
    run down, but the stderr is kept so an outage is never reported as 'this
    show does not exist' - a confusion that has shipped three times."""
    try:
        p = subprocess.run([sys.executable, str(helper(provider)), *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.stdout, p.stderr
    except (subprocess.TimeoutExpired, OSError) as e:
        return "", f"error: {provider} helper failed: {e}"


def p_search(provider: str, query: str) -> tuple[list[tuple[str, str]], str]:
    out, e = run_helper(provider, "search", query)
    hits = [tuple(l.split("\t", 1)) for l in out.splitlines() if "\t" in l]
    return hits, e  # type: ignore[return-value]


def p_episodes(provider: str, ident: str) -> tuple[list[str], str]:
    out, e = run_helper(provider, "episodes", ident, LANG)
    return out.split(), e


def p_video(provider: str, ident: str, ep: str) -> tuple[list[tuple[int, str, str]], str]:
    """[(height, url, referrer)] best first."""
    out, e = run_helper(provider, "video", ident, ep, LANG)
    streams = []
    for line in out.splitlines():
        m = re.match(r"(\d+)p\s*>([^>]+)>?(.*)", line)
        if m:
            streams.append((int(m.group(1)), m.group(2), m.group(3)))
    streams.sort(key=lambda s: s[0], reverse=True)
    return streams, e


def record_health(provider: str, ok: bool) -> None:
    """The credibility verdict is the score, not 'a URL resolved' - a provider
    returning 3-second placeholders has not worked."""
    d = load_json(HEALTH_FILE)
    e = d.setdefault(provider, {})
    e["last_try"] = int(time.time())
    if ok:
        e["ok"] = e.get("ok", 0) + 1
        e["last_ok"] = int(time.time())
        e.pop("first_fail", None)
    else:
        e["fail"] = e.get("fail", 0) + 1
        e.setdefault("first_fail", int(time.time()))
    save_json(HEALTH_FILE, d)


def resolve_episode(title: str, ep: str, want_exact: bool,
                    errors: list[str]) -> tuple[str, list[tuple[int, str, str]]]:
    """(provider, streams) from the first provider that can serve this episode."""
    for provider in PROVIDERS:
        pt = provider_title(provider, title)
        hits, e = p_search(provider, pt)
        if e:
            errors.append(e.strip())
        for ident, exact in pick_match(hits, pt):
            if want_exact and not exact:
                continue
            eps, e = p_episodes(provider, ident)
            if e:
                errors.append(e.strip())
            if ep not in eps:
                continue
            streams, e = p_video(provider, ident, ep)
            if e:
                errors.append(e.strip())
            if streams:
                return provider, streams
    return "", []


# --------------------------------------------------------------- download

def duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None


def has_video_stream(path: Path) -> bool:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60).stdout
        return "video" in out
    except (OSError, subprocess.TimeoutExpired):
        return False


def sibling_median(show_dir: Path, exclude: Path) -> float | None:
    """Median runtime of up to 5 existing episodes, looking in the canonical
    folder too: a season-suffixed download dir ("X Season 2") never contains
    what the consolidator filed under "X/Season 02", so such a show looked
    brand new every run and the comparison was blind."""
    dirs = [show_dir]
    canon = SEASON_MARKER.sub("", show_dir.name).strip(" -_")
    if canon and canon != show_dir.name and (ANIME_ROOT / canon).is_dir():
        dirs.append(ANIME_ROOT / canon)
    seen = []
    for d in dirs:
        for f in sorted(d.rglob("*.mp4")):
            if f != exclude and len(seen) < 5:
                dur = duration(f)
                if dur:
                    seen.append(dur)
    if not seen:
        return None
    seen.sort()
    return seen[len(seen) // 2]


def credible(path: Path, show_dir: Path) -> tuple[bool, str]:
    """Every silently-wrong file this project has shipped failed one of these:
    a 3-second placeholder, a 9-minute [Mini] cut in place of a 24-minute
    series, a fragment-skipped partial, an HTML error page saved as .mp4."""
    if not shutil.which("ffprobe"):
        return True, ""                      # cannot tell; do not block
    dur = duration(path)
    if dur is None:
        return False, "unreadable"
    if not has_video_stream(path):
        return False, "no video stream"
    if dur < MIN_SECONDS:
        # absolute floor, checked before any comparison: a brand new show has
        # no siblings, and that blind spot let seven placeholders through
        return False, f"runtime {int(dur)}s is below the {MIN_SECONDS}s floor"
    ref = sibling_median(show_dir, path)
    if ref and dur < ref * 0.5:
        # lower bound only: double-length premieres and finales are routine,
        # and an upper bound produced 9 false positives against this library
        return False, f"runtime {int(dur)}s is far short of this show's {int(ref)}s"
    return True, ""


def download(url: str, referrer: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["yt-dlp", url]
    if referrer:
        cmd += ["--referer", referrer]
    # these flags are load-bearing: without them yt-dlp silently skips failed
    # fragments and produces a short file that looks like a real episode
    cmd += ["--no-skip-unavailable-fragments", "--fragment-retries", "infinite",
            "-N", "16", "-o", str(dest)]
    try:
        return subprocess.run(cmd, timeout=3600).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def fetch_subs(title: str, ep: str, dest_stem: Path) -> bool:
    """These streams carry no embedded subtitle track at all - the player is
    expected to overlay a .vtt - so without this an episode has no subtitles."""
    if not WANT_SUBS:
        return False
    pt = provider_title(SUB_PROVIDER, title)
    hits, _ = p_search(SUB_PROVIDER, pt)
    cands = pick_match(hits, pt)
    if not cands:
        return False
    out, _ = run_helper(SUB_PROVIDER, "subs", cands[0][0], ep, LANG)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == SUB_LANG:
            url, ref = parts[1], (parts[2] if len(parts) > 2 else "")
            target = dest_stem.with_suffix(f".{SUB_LANG}.vtt")
            cmd = ["curl", "-sfL", "--max-time", "60", "-o", str(target), url]
            if ref:
                cmd[1:1] = ["-H", f"Referer: {ref}"]
            try:
                if subprocess.run(cmd, timeout=120).returncode == 0 and target.stat().st_size:
                    return True
            except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
                pass
            target.unlink(missing_ok=True)
    return False


# ------------------------------------------------------------------- gaps

def gap_bump(aid: str, ep: str) -> int:
    d = load_json(GAPS_FILE)
    e = d.setdefault(aid, {})
    fails = e.setdefault("fails", {})
    fails[ep] = fails.get(ep, 0) + 1
    save_json(GAPS_FILE, d)
    return fails[ep]


def gap_record(aid: str, ep: str) -> None:
    d = load_json(GAPS_FILE)
    e = d.setdefault(aid, {})
    e.setdefault("gaps", {})[ep] = {"since": int(time.time())}
    save_json(GAPS_FILE, d)


def gap_clear(aid: str, ep: str) -> None:
    d = load_json(GAPS_FILE)
    e = d.get(aid) or {}
    e.get("fails", {}).pop(ep, None)
    e.get("gaps", {}).pop(ep, None)
    save_json(GAPS_FILE, d)


def gaps_for(aid: str) -> list[str]:
    return list((load_json(GAPS_FILE).get(aid) or {}).get("gaps", {}).keys())


# ---------------------------------------------------------------- anilist

ANILIST_UA = "Mozilla/5.0 (X11; Linux x86_64) ani-cli-sync"


def anilist_token() -> str:
    try:
        for line in ANIPY_CONFIG.read_text().splitlines():
            if line.strip().startswith("anilist_token:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def anilist_watching(token: str) -> list[tuple[str, str]]:
    """[(anilist_id, title)]. The custom User-Agent is required: AniList's WAF
    403s urllib's default."""
    import urllib.request

    def post(query: str, variables: dict) -> dict:
        req = urllib.request.Request(
            "https://graphql.anilist.co",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": ANILIST_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    viewer = post("query { Viewer { id } }", {})["data"]["Viewer"]["id"]
    q = """query ($userId: Int) {
      MediaListCollection(userId: $userId, type: ANIME, status: CURRENT) {
        lists { entries { media { id title { romaji english } } } } } }"""
    data = post(q, {"userId": viewer})
    out = []
    for lst in data["data"]["MediaListCollection"]["lists"]:
        for e in lst["entries"]:
            m = e["media"]
            out.append((str(m["id"]), m["title"]["english"] or m["title"]["romaji"]))
    return out


# ------------------------------------------------------------------- sync

def episode_list(title: str, errors: list[str]) -> list[str]:
    """What has AIRED, not what a provider happens to carry.

    AllAnime is asked first - it still answers and its numbering matches
    AniList - but never as the only source. Two failure modes to avoid:

      - If AllAnime is the ONLY source and it stops answering, the list is
        empty, the loop never runs, the show reports "skip", the job reads as
        idle and scheduled runs notify nothing: a total outage, no alert.
      - If a PROVIDER's list is used instead, it reports only what that site
        carries. Chitose has 13 episodes aired and 3 available on nineanime;
        taking 3 would mark the show up to date and silently hide ten aired
        episodes. The fallback is for keeping the pipeline alive, not for
        redefining what "complete" means.
    """
    wl = load_json(MAIN_WATCHLIST)
    ident = ""
    for e in wl.values():
        if isinstance(e, dict) and e.get("provider") == "allanime" and e.get("identifier"):
            if title_key(e.get("folder_name", "")) == title_key(title):
                ident = e["identifier"]
                break
    if not ident:
        # not in the shared watchlist (nothing creates entries there any more),
        # so ask AllAnime by title like the shell version did
        hits, e = p_search(LIST_PROVIDER, title)
        if e:
            errors.append(e.strip())
        chosen = pick_match(hits, title)
        if chosen:
            ident = chosen[0][0]
        elif hits:
            ident = hits[0][0]      # AllAnime's own search is already relevance-ranked
    if ident:
        eps, e = p_episodes(LIST_PROVIDER, ident)
        if eps:
            return sorted(eps, key=float)
        if e:
            errors.append(e.strip())

    for provider in PROVIDERS:
        pt = provider_title(provider, title)
        hits, _ = p_search(provider, pt)
        for pid, _exact in pick_match(hits, pt):
            eps, _ = p_episodes(provider, pid)
            if eps:
                warn(f"  allanime gave no episode list; using {provider}'s "
                     f"({len(eps)} episodes - may understate what has aired)")
                return sorted(eps, key=float)
    return []


def watermark_for(aid: str) -> float:
    def val(d: dict):
        v = d.get(aid, -1)
        return v.get("last_downloaded", -1) if isinstance(v, dict) else v
    return max(float(val(load_json(WATERMARK))), float(val(load_json(MAIN_WATCHLIST))))


def sync(dry_run: bool = False) -> int:
    global DRY_RUN
    DRY_RUN = dry_run
    token = anilist_token()
    if not token:
        err(f"anilist_token not set in {ANIPY_CONFIG}")
        return 1
    log("Fetching AniList WATCHING list...")
    try:
        shows = anilist_watching(token)
    except Exception as e:                                  # noqa: BLE001
        err(f"AniList query failed: {e}")
        return 1
    if not shows:
        err("No shows in WATCHING list")
        return 1

    results: list[tuple] = []

    for aid, title in shows:
        log(f"Syncing: {title}")
        folder = resolve_folder(title)
        show_dir = ANIME_ROOT / folder
        errors: list[str] = []

        eps = episode_list(title, errors)
        if not eps:
            err(f"  no provider returned an episode list for {title}")
            results.append(("failed", title, "", "no episode list from any provider"))
            continue

        last = watermark_for(aid)
        max_ep = max(float(e) for e in eps)
        if last > max_ep:
            # AniList switched the entry to absolute numbering, or the
            # watermark came from a provider that numbers differently. Nothing
            # is ever > last, so the show reads "up to date" forever - that
            # silence cost seven weeks on Slime S4.
            err(f"  watermark {last:g} is past every listed episode (max {max_ep:g}) - numbering mismatch")
            results.append(("failed", title, f"{max_ep:g}",
                            f"watermark {last:g} exceeds provider max {max_ep:g} (numbering mismatch)"))
            continue

        known_gaps = gaps_for(aid)
        if known_gaps:
            log(f"  retrying recorded gap(s): {' '.join(known_gaps)}")
        todo, seen = [], set()
        for ep in known_gaps + eps:                 # gaps bypass the watermark
            if ep in seen:
                continue
            seen.add(ep)
            if ep in known_gaps or float(ep) > last:
                todo.append(ep)

        attempted = False
        for ep in todo:
            is_gap = ep in known_gaps
            attempted = True
            log(f"  downloading episode {ep}")
            provider, streams = resolve_episode(title, ep, want_exact=True, errors=errors)
            if not streams:
                provider, streams = resolve_episode(title, ep, want_exact=False, errors=errors)
            if not streams:
                detail = errors[-1] if errors else "no provider has this episode"
                if _handle_failure(aid, title, ep, detail, is_gap, eps, results):
                    continue
                break

            height, url, ref = streams[0]
            dest = show_dir / f"{folder}_{int(float(ep)):02d}.mp4"
            if dry_run:
                log(f"    [dry-run] would download {height}p from {provider}")
                results.append(("ok", title, ep, ""))
                continue
            if not download(url, ref, dest):
                record_health(provider, False)
                if _handle_failure(aid, title, ep, "download failed", is_gap, eps, results):
                    continue
                break
            ok, why = credible(dest, show_dir)
            if not ok:
                err(f"  discarded episode {ep}: {why}")
                dest.unlink(missing_ok=True)
                record_health(provider, False)
                if _handle_failure(aid, title, ep, why, is_gap, eps, results):
                    continue
                break

            record_health(provider, True)
            gap_clear(aid, ep)
            log(f"  got episode {ep} from {provider}")
            if fetch_subs(title, ep, dest.with_suffix("")):
                log(f"  fetched {SUB_LANG} subtitles")
            results.append(("ok", title, ep, ""))
            if float(ep) > last:
                last = float(ep)
                wm = load_json(WATERMARK)
                wm[aid] = int(last) if last.is_integer() else last
                save_json(WATERMARK, wm)

        if not attempted:
            results.append(("skip", title, "", ""))

    write_summary(results)
    if not dry_run and env("ANI_CLI_SKIP_CONSOLIDATE") != "1" and CONSOLIDATOR.exists():
        log("Sync pass done. Running jellyfin consolidator...")
        subprocess.run([sys.executable, str(CONSOLIDATOR)],
                       env={**os.environ, "ANIME_ROOT": str(ANIME_ROOT)})
    else:
        log("Sync pass done.")
    return 0


def _handle_failure(aid: str, title: str, ep: str, detail: str, is_gap: bool,
                    eps: list[str], results: list) -> bool:
    """True = carry on with this show, False = stop here.

    Stopping is the safe default. A gap is only recorded once the episode has
    failed GAP_AFTER separate runs AND a later episode provably resolves right
    now - an outage can never satisfy the second condition, which is what
    stops "the site is down" being written off as "this episode is missing".
    """
    if is_gap:
        # re-report every run: staying silent made a recorded gap vanish from
        # the summary after the run that created it
        results.append(("gap", title, ep, detail))
        return True
    n = gap_bump(aid, ep)
    if n >= GAP_AFTER:
        later = [e for e in eps if float(e) > float(ep)]
        if later and resolve_episode(title, later[0], want_exact=False, errors=[])[1]:
            gap_record(aid, ep)
            warn(f"  episode {ep} unavailable after {n} runs but later episodes are "
                 f"- recording a gap and continuing")
            results.append(("gap", title, ep, detail))
            return True
    warn(f"  {detail} for episode {ep}, stopping this show here")
    results.append(("failed", title, ep, detail))
    return False


def write_summary(results: list[tuple]) -> None:
    shows: dict[str, dict] = {}
    stats = {"downloaded": 0, "failed": 0, "skipped": 0, "gaps": 0}
    for status, title, ep, reason in results:
        if status == "skip":
            stats["skipped"] += 1
            continue
        e = shows.setdefault(title, {"title": title, "episodes": [], "errors": []})
        if status == "ok":
            e["episodes"].append(int(float(ep)))
            stats["downloaded"] += 1
        elif status == "gap":
            e.setdefault("gaps", []).append(int(float(ep)))
            stats["gaps"] += 1
        else:
            e["errors"].append(reason or ep or "failed")
            stats["failed"] += 1
    out = []
    for e in shows.values():
        e["status"] = ("downloaded" if e["episodes"]
                       else "gap" if e.get("gaps") else "failed")
        if e["status"] == "failed":
            e["detail"] = e["errors"][0] if e["errors"] else "failed"
        out.append(e)
    save_json(SUMMARY, {"stats": stats, "shows": out})


# ------------------------------------------------------------- selfcheck

def selfcheck() -> None:
    # title folding: the en dash that cost BLEACH ep9 several days
    assert title_key("BLEACH: TYBW - The Calamity") == title_key("Bleach: TYBW – The Calamity")
    assert title_key("Show   Name") == "show name"

    # exact beats the movie entry; the movie must never win
    hits = [("movie", "Bleach: TYBW – The Calamity Movie"), ("series", "Bleach: TYBW – The Calamity")]
    assert pick_match(hits, "BLEACH: TYBW - The Calamity") == [("series", True)]
    # a lone Movie/OVA entry is a different work, not a near-spelling
    assert pick_match([("m", "Some Show Movie")], "Some Show") == []
    assert pick_match([("o", "Some Show OVA")], "Some Show") == []
    # but asking for the movie still works, and an ordinary single hit stands
    assert pick_match([("m", "Some Show Movie")], "Some Show Movie") == [("m", True)]
    assert pick_match([("x", "Renamed Show")], "Some Show") == [("x", False)]
    # never guess between several non-exact hits: providers rank by popularity
    assert pick_match([("a", "Food Wars"), ("b", "Other")], "Dara-san") == []

    # an alias must not strip the season - that overwrote a real episode
    assert has_season_marker("Slime Season 4") and not has_season_marker("Slime")
    assert has_season_marker("Chitose 2nd Cour") and has_season_marker("Bumpkin II")

    # stream parsing, including an empty referrer field
    assert re.match(r"(\d+)p\s*>([^>]+)>?(.*)", "1080p >http://x/a.m3u8>http://ref/").groups() \
        == ("1080", "http://x/a.m3u8", "http://ref/")

    # summary shape: a gap is never counted as a failure
    import tempfile
    global SUMMARY
    with tempfile.TemporaryDirectory() as d:
        SUMMARY = Path(d) / "s.json"
        write_summary([("ok", "A", "6", ""), ("gap", "A", "5", "x"),
                       ("failed", "B", "1", "boom"), ("skip", "C", "", "")])
        s = json.loads(SUMMARY.read_text())
        assert s["stats"] == {"downloaded": 1, "failed": 1, "skipped": 1, "gaps": 1}, s["stats"]
        a = next(x for x in s["shows"] if x["title"] == "A")
        assert a["status"] == "downloaded" and a["gaps"] == [5]
    print("anime_sync selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
        sys.exit(0)
    sys.exit(sync(dry_run="--dry-run" in sys.argv))
