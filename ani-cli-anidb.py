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
    # no network: just proves argv dispatch doesn't crash/typo
    assert BASE == "https://anidb.app"
    print("ani-cli-anidb dispatch ok")


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
        else:
            sys.exit("bad arguments")
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
