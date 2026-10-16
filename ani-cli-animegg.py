#!/usr/bin/env python3
"""animegg.org backend for ~/scripts/ani-cli. Same CLI shape as the other
helpers so sync_mode() can call any of them interchangeably.

Added 2026-10-16, when every other provider had failed for a set of shows:
AllAnime dead since August, anidb.app shut down (it now redirects to a
placeholder landing page at anilab.so), anikoto's stream host encrypted,
animeheaven serving 3-second placeholders for everything, and animehub
simply lacking the titles. animegg had all of them.

Mechanically the friendliest of the six: progressive MP4s with real quality
labels, no HLS, no encryption, no cookie dance. The chain is

    /search?q=<query>        -> /series/<slug> + <h2> title
    /series/<slug>           -> /<slug>-episode-<n> links
    /<slug>-episode-<n>      -> iframe /embed/<id>, inside a
                                <div id="subbed-..."> or "dubbed-..."
    /embed/<id>              -> {file: "/play/<id>/video.mp4?for=<token>",
                                 label: "1080p"}

The `for=` token is minted per request, so the play URL must come from the
same fetch that produced it; it is not stable. The play URL then 302s to a
vidcache host, which yt-dlp follows on its own.

Usage:
    ani-cli-animegg.py search <query>                 -> id\\tname per line
    ani-cli-animegg.py episodes <id> <sub|dub>        -> episode number per line
    ani-cli-animegg.py video <id> <episode> <sub|dub> -> "<res>p >url>referrer"
"""
import html as _html
import re
import sys

from curl_cffi import requests

BASE = "https://www.animegg.org"


def _session() -> requests.Session:
    s = requests.Session(impersonate="chrome124")
    s.headers.update({"Referer": BASE + "/"})
    return s


def _get(s, url, **kw):
    """GET that fails loudly: a silent empty result reads as 'no such show'
    rather than 'the site is down', which is how an outage gets misreported
    as a title-matching problem."""
    r = s.get(url, timeout=kw.pop("timeout", 25), **kw)
    if r.status_code != 200:
        raise RuntimeError(f"animegg.org returned HTTP {r.status_code} for {url}")
    if "Just a moment" in r.text[:4000]:
        raise RuntimeError("blocked by cloudflare")
    return r


def cmd_search(query: str) -> None:
    page = _get(_session(), f"{BASE}/search", params={"q": query}).text
    # each hit: <a href="/series/<slug>" class="mse"> ... <h2>Title</h2>
    for slug, name in re.findall(
        r'href="/series/([^"]+)"[\s\S]{0,600}?<h2>\s*([^<]+?)\s*</h2>', page
    ):
        print(f"{slug}\t{_html.unescape(name).strip()}")


def _episodes(s, slug: str):
    page = _get(s, f"{BASE}/series/{slug}").text
    # links carry #subbed/#dubbed anchors; the episode number is what matters
    nums = re.findall(rf'href="/{re.escape(slug)}-episode-([0-9.]+)(?:#\w+)?"', page)
    seen, out = set(), []
    for n in nums:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return sorted(out, key=float)


def cmd_episodes(slug: str, _lang: str) -> None:
    for n in _episodes(_session(), slug):
        print(n)


def cmd_video(slug: str, episode: str, lang: str) -> None:
    s = _session()
    ep_url = f"{BASE}/{slug}-episode-{episode}"
    page = _get(s, ep_url).text

    # The subbed and dubbed players are separate iframes, each inside a div
    # whose id starts with the language. Picking the wrong one silently gives
    # the wrong audio, so match the container rather than taking the first.
    want = "dubbed" if lang == "dub" else "subbed"
    blocks = re.findall(
        r'<div id="(subbed|dubbed)-[^"]*"[^>]*>([\s\S]{0,600}?)</div>', page
    )
    embed = None
    for kind, body in blocks:
        if kind == want:
            m = re.search(r'<iframe[^>]+src="([^"]+)"', body)
            if m:
                embed = m.group(1)
                break
    if embed is None:
        raise RuntimeError(f"no {want} player for episode {episode}")
    if embed.startswith("/"):
        embed = BASE + embed

    body = _get(s, embed, headers={"Referer": ep_url}).text
    # {file: "/play/<id>/video.mp4?for=<token>", label: "1080p"}
    srcs = re.findall(r'file:\s*"(/play/[^"]+)"\s*,\s*label:\s*"(\d+)p?"', body)
    if not srcs:
        raise RuntimeError(f"no video source for episode {episode}")
    for path, height in sorted(srcs, key=lambda r: int(r[1]), reverse=True):
        print(f"{height}p >{BASE}{path}>{embed}")


def demo() -> None:
    # no network: the three parses that silently yield "nothing" if they drift
    hits = re.findall(r'href="/series/([^"]+)"[\s\S]{0,600}?<h2>\s*([^<]+?)\s*</h2>',
                      '<a href="/series/the-ramparts-of-ice" class="mse"><div><h2>The Ramparts of Ice</h2>')
    assert hits == [("the-ramparts-of-ice", "The Ramparts of Ice")], hits

    nums = re.findall(r'href="/overgeared-episode-([0-9.]+)(?:#\w+)?"',
                      '<a href="/overgeared-episode-1#subbed"><a href="/overgeared-episode-12">')
    assert nums == ["1", "12"], nums

    srcs = re.findall(r'file:\s*"(/play/[^"]+)"\s*,\s*label:\s*"(\d+)p?"',
                      '{file: "/play/550908/video.mp4?for=1017", label: "1080p", bk: "x"}')
    assert srcs == [("/play/550908/video.mp4?for=1017", "1080")], srcs

    # the language container must be matched, not the first iframe on the page
    blocks = re.findall(r'<div id="(subbed|dubbed)-[^"]*"[^>]*>([\s\S]{0,600}?)</div>',
                        '<div id="subbed-Animegg"><iframe src="/embed/1"></iframe></div>'
                        '<div id="dubbed-Animegg"><iframe src="/embed/2"></iframe></div>')
    assert [k for k, _ in blocks] == ["subbed", "dubbed"], blocks
    assert 'src="/embed/2"' in dict(blocks)["dubbed"]
    print("ani-cli-animegg parsing ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
        sys.exit(0)

    if len(sys.argv) < 2:
        sys.exit("usage: ani-cli-animegg.py <search|episodes|video> ...")

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
