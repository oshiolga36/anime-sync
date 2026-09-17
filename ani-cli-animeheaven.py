#!/usr/bin/env python3
"""animeheaven.me backend for ~/scripts/ani-cli. Same CLI shape as the other
helpers so sync_mode() can call any of them interchangeably.

Added 2026-09-14 because the other four had all stopped covering a set of
shows: AllAnime is dead, anidb.app has been 503 for a week, animehub never
carried them (Re:ZERO S4, Bumpkin II, Tanya S2, BLEACH TYBW, GHOST IN THE
SHELL), and anikoto's stream host switched to encrypted sources.

By far the simplest of the five: episodes are plain progressive MP4s, no
HLS, no encryption, no per-segment auth. The only quirk is navigation -
episode links do not carry a URL. Each is a 32-hex key; the site sets it as
a `key` cookie and then `gate.php` serves that episode. So:

    /search.php?s=<query>   -> anime.php?<id> + title
    /anime.php?<id>         -> (key, episode number) pairs, newest first
    cookie key=<key> + /gate.php -> <source src="https://../video.mp4?...">

Usage:
    ani-cli-animeheaven.py search <query>                 -> id\\tname per line
    ani-cli-animeheaven.py episodes <id> <sub|dub>        -> episode number per line
    ani-cli-animeheaven.py video <id> <episode> <sub|dub> -> "<res>p >url>referrer"
"""
import html as _html
import re
import sys

from curl_cffi import requests

BASE = "https://animeheaven.me"


def _session() -> requests.Session:
    s = requests.Session(impersonate="chrome124")
    s.headers.update({"Referer": BASE + "/"})
    return s


def _get(s, url, **kw):
    """GET that fails loudly: a silent empty result reads as 'no such show'
    rather than 'the site is down', which is how an outage gets misreported."""
    r = s.get(url, timeout=kw.pop("timeout", 25), **kw)
    if r.status_code != 200:
        raise RuntimeError(f"animeheaven.me returned HTTP {r.status_code} for {url}")
    if "Just a moment" in r.text[:4000]:
        raise RuntimeError("blocked by cloudflare")
    return r


def cmd_search(query: str) -> None:
    page = _get(_session(), f"{BASE}/search.php", params={"s": query}).text
    seen = set()
    for aid, name in re.findall(r"href='anime\.php\?([^']+)'[^>]*class='c'>([^<]+)<", page):
        if aid in seen:
            continue
        seen.add(aid)
        print(f"{aid}\t{_html.unescape(name).strip()}")


def _episodes(s, aid: str):
    """[(episode_number, key)] for one show, as listed (newest first)."""
    page = _get(s, f"{BASE}/anime.php?{aid}").text
    pairs = re.findall(
        # markup is inconsistent between show pages: some write gatea("key"),
    # others gatea( "key") with a space. Requiring no space silently produced
    # zero episodes for those shows, which reads as "provider lacks the show".
    r'gatea\(\s*"([0-9a-f]{32})"\s*\)[\s\S]{0,400}?watch2[^>]*>\s*([0-9.]+)\s*<', page
    )
    return [(num, key) for key, num in pairs]


def cmd_episodes(aid: str, _lang: str) -> None:
    for num, _key in sorted(_episodes(_session(), aid), key=lambda p: float(p[0])):
        print(num)


def cmd_video(aid: str, episode: str, _lang: str) -> None:
    s = _session()
    want = str(episode).lstrip("0") or "0"
    key = next((k for n, k in _episodes(s, aid) if n == str(episode) or n.lstrip("0") == want), None)
    if key is None:
        raise RuntimeError(f"episode {episode} not found")

    # the episode is selected by cookie, not by URL - gate.php serves whatever
    # `key` currently points at, so it must be set on this same session
    s.cookies.set("key", key, domain="animeheaven.me")
    page = _get(s, f"{BASE}/gate.php").text
    # the page lists the real source first, then "&error" reporting mirrors
    urls = [u for u in re.findall(r"<source[^>]+src=['\"]([^'\"]+)", page)
            if ".mp4" in u and "&error" not in u]
    if not urls:
        raise RuntimeError(f"no video source for episode {episode}")
    # single progressive MP4; no resolution ladder is advertised
    print(f"0p >{urls[0]}>{BASE}/")


def demo() -> None:
    # no network: the two parses that silently yield "nothing" if they drift
    rx = r'gatea\(\s*"([0-9a-f]{32})"\s*\)[\s\S]{0,400}?watch2[^>]*>\s*([0-9.]+)\s*<'
    for markup, want in (
        ("""<a onclick='gatea("a0acfc85571bd150a4dd117180b0071f")'><div class='watch2 bc '>8</div></a>""",
         [("a0acfc85571bd150a4dd117180b0071f", "8")]),
        # the spaced variant, which silently matched nothing before
        ("""<a onclick='gatea( "63813ff6a43deb149031dc4497e25bec")'><div class= ' watch2 bc ' >11</div></a>""",
         [("63813ff6a43deb149031dc4497e25bec", "11")]),
    ):
        assert re.findall(rx, markup) == want, markup
    hits = re.findall(r"href='anime\.php\?([^']+)'[^>]*class='c'>([^<]+)<",
                      """<a href='anime.php?07v8r' class='c'>Bleach: TYBW</a>""")
    assert hits == [("07v8r", "Bleach: TYBW")], hits
    print("ani-cli-animeheaven parsing ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
        sys.exit(0)

    if len(sys.argv) < 2:
        sys.exit("usage: ani-cli-animeheaven.py <search|episodes|video> ...")

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
