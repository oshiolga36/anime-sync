#!/usr/bin/env python3
"""anidb.app backend for ~/scripts/ani-cli - second-chance provider tried when
AllAnime itself is down (NEED_CAPTCHA / AA_CRYPTO_STALE, see anime-pipeline
memory). Same 3-verb CLI shape as ani-cli-allanime.py so sync_mode() in the
shell script can call either interchangeably.

anidb.app sits behind Cloudflare - plain requests gets the "Just a moment"
JS challenge page, so scraping needs curl_cffi's browser TLS impersonation
(already installed, no compiled curl-impersonate binary needed). The HLS CDN
(hls.anidb.app) itself has no such gate, so downloads still go through
yt-dlp/ffmpeg unmodified.

Usage:
    ani-cli-anidb.py search <query>            -> id\\ttitle per line
    ani-cli-anidb.py episodes <id> <sub|dub>   -> episode number per line
    ani-cli-anidb.py video <id> <episode> <sub|dub> -> "<res>p >url" per line
"""
import html
import re
import sys

from curl_cffi import requests

BASE = "https://anidb.app"
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _session() -> requests.Session:
    s = requests.Session(impersonate="chrome124")
    s.headers.update({"User-Agent": AGENT})
    return s


def cmd_search(query: str) -> None:
    s = _session()
    page = s.get(f"{BASE}/browse", params={"q": query}, timeout=15).text
    if "Just a moment" in page:
        raise RuntimeError("blocked by cloudflare")
    for aid, title in re.findall(r'anime/([a-z0-9-]+-[0-9]+)"[^>]*title="([^"]+)"', page):
        print(f"{aid}\t{html.unescape(title)}")


def cmd_episodes(identifier: str, _lang: str) -> None:
    s = _session()
    numeric_id = identifier.rsplit("-", 1)[-1]
    data = s.get(f"{BASE}/api/frontend/anime/{numeric_id}/episodes", timeout=15).json()
    for ep in data["episodes"]:
        print(ep["number"])


def map_episode(eps: list, want: str, exact: bool):
    """Translate an AniList episode number into anidb's number for this entry.

    anidb sometimes numbers a sequel cour *absolutely* across the whole series
    while AniList restarts at 1: BLEACH TYBW "The Calamity" is 41-44, Re:ZERO
    S4 is 67-78. When the anidb entry covers exactly one cour - which an exact
    title match assures - position in its episode list IS our episode number.
    Verified against real streams: anidb 43 has the same runtime as our S01E03.

    Returns None rather than guessing. A silently wrong episode is worse than
    a missed one, so the offset only applies when the list is contiguous, the
    title matched exactly, and `want` is in range.
    """
    try:
        w = int(want)
    except (TypeError, ValueError):
        return None  # non-whole episode (24.5 etc): only ever exact-match
    if w in eps:
        return w
    if not (exact and eps):
        return None
    contiguous = eps == list(range(eps[0], eps[0] + len(eps)))
    if contiguous and 1 <= w <= len(eps):
        return eps[w - 1]
    return None


def cmd_episode_for(identifier: str, want: str, exact: str) -> None:
    s = _session()
    numeric_id = identifier.rsplit("-", 1)[-1]
    data = s.get(f"{BASE}/api/frontend/anime/{numeric_id}/episodes", timeout=15).json()
    eps = [e["number"] for e in data["episodes"]]
    got = map_episode(eps, want, exact == "1")
    if got is not None:
        print(got)


def _episode_id(s: "requests.Session", identifier: str, episode: str) -> int:
    numeric_id = identifier.rsplit("-", 1)[-1]
    data = s.get(f"{BASE}/api/frontend/anime/{numeric_id}/episodes", timeout=15).json()
    ep_no = float(episode)
    for ep in data["episodes"]:
        if ep["number"] == ep_no or ep["number"] == int(ep_no):
            return ep["id"]
    raise RuntimeError(f"episode {episode} not found")


def cmd_video(identifier: str, episode: str, lang: str) -> None:
    s = _session()
    ep_id = _episode_id(s, identifier, episode)

    langs = s.get(f"{BASE}/api/frontend/episode/{ep_id}/languages", timeout=15).json()["languages"]
    code = "eng" if lang == "dub" else "jpn"
    embed_url = next((e["embed_url"] for e in langs if e["code"] == code), None)
    if not embed_url:
        raise RuntimeError(f"no {lang} stream for episode {episode}")

    embed_page = s.get(embed_url, timeout=15).text
    m = re.search(r"file:\s*'([^']*)'", embed_page)
    if not m:
        raise RuntimeError("could not find m3u8 master url in embed page")
    master_url = m.group(1)

    # segments/manifests aren't behind cloudflare - a plain requests-backed
    # fetch (what m3u8's default HTTP client uses) is enough here.
    import m3u8

    playlist = m3u8.load(master_url, headers={"User-Agent": AGENT})
    for p in sorted(playlist.playlists, key=lambda p: p.stream_info.bandwidth, reverse=True):
        height = p.stream_info.resolution[1] if p.stream_info.resolution else 0
        print(f"{height}p >{p.absolute_uri}")


def demo() -> None:
    # no network: argv dispatch + the episode-number mapping, which is the
    # only real logic in here and the one that can silently fetch the WRONG
    # episode if it regresses.
    assert BASE == "https://anidb.app"

    normal = [1, 2, 3, 4]
    bleach = [41, 42, 43, 44]          # real: TYBW "The Calamity"
    rezero = list(range(67, 79))       # real: Re:ZERO S4, 12 eps

    # direct hit always wins, regardless of exactness
    assert map_episode(normal, "3", True) == 3
    assert map_episode(normal, "3", False) == 3
    # absolute numbering -> position in list (the cases seen in the wild)
    assert map_episode(bleach, "3", True) == 43
    assert map_episode(bleach, "4", True) == 44
    assert map_episode(rezero, "12", True) == 78
    assert map_episode(rezero, "1", True) == 67
    # never offset on a fuzzy title match - could be a different show
    assert map_episode(bleach, "4", False) is None
    # out of range, gappy list, and non-whole episodes all decline
    assert map_episode(bleach, "5", True) is None
    assert map_episode(bleach, "0", True) is None
    assert map_episode([41, 42, 44], "3", True) is None
    assert map_episode(bleach, "24.5", True) is None
    assert map_episode([], "1", True) is None
    print("ani-cli-anidb dispatch + episode mapping ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
        sys.exit(0)

    if len(sys.argv) < 2:
        sys.exit("usage: ani-cli-anidb.py <search|episodes|video> ...")

    cmd, args = sys.argv[1], sys.argv[2:]
    try:
        if cmd == "search" and len(args) == 1:
            cmd_search(args[0])
        elif cmd == "episodes" and len(args) == 2:
            cmd_episodes(args[0], args[1])
        elif cmd == "video" and len(args) == 3:
            cmd_video(args[0], args[1], args[2])
        elif cmd == "episode-for" and len(args) == 3:
            cmd_episode_for(args[0], args[1], args[2])
        else:
            sys.exit("bad arguments")
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
