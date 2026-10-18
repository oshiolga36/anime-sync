#!/usr/bin/env python3
"""9anime (9anime.org.lv) backend for ~/scripts/ani-cli. Same CLI shape as
the other helpers so sync_mode() can call any of them interchangeably.

Added 2026-10-18 for NEW-EPISODE latency. The other providers were not
down, they were behind: animegg reliably has back catalogue but lags on
fresh releases (and for some shows carries only a dubbed player), so the
newest episode of several shows fell through to animeheaven's placeholders
every run. 9anime publishes within a day or so.

The catch is the stream host. 9anime offers four servers per episode and
two of them (HD-1/HD-2, via gogoanime.me.uk) resolve to megaplay.buzz,
which has served an encrypted "enc" blob instead of a plaintext m3u8 since
September - the same wall anikoto hit. The "Omega" server is vidmoly, which
serves a normal master playlist. So this helper deliberately picks by host
rather than taking the first server offered.

    /search?keyword=<q>    -> /anime/<slug>/ + title
    /anime/<slug>/         -> <slug>-episode-<n> links
    /<slug>-episode-<n>/   -> base64 <option> values, each an iframe tag;
                              decode and keep the usable host
    <vidmoly embed>        -> file: "<master .m3u8>", needs a Referer

Usage:
    ani-cli-nineanime.py search <query>                 -> id\\tname per line
    ani-cli-nineanime.py episodes <id> <sub|dub>        -> episode number per line
    ani-cli-nineanime.py video <id> <episode> <sub|dub> -> "<res>p >url>referrer"
"""
import base64
import html as _html
import re
import sys

from curl_cffi import requests

BASE = "https://9anime.org.lv"

# Stream hosts we can actually read, best first. megaplay.buzz is excluded on
# purpose: it returns an encrypted blob, so preferring it would make this
# provider look broken while a working server sat further down the list.
USABLE_HOSTS = ("vidmoly.biz", "vidmoly.to")


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
        raise RuntimeError(f"9anime returned HTTP {r.status_code} for {url}")
    if "Just a moment" in r.text[:4000]:
        raise RuntimeError("blocked by cloudflare")
    return r


def cmd_search(query: str) -> None:
    # This is WordPress: "/search?keyword=" renders the homepage, whose
    # sidebar is a popular-shows widget using the same <a class="series">
    # markup as a real hit - so that endpoint returns One Piece for every
    # query. "/?s=" is the one that actually searches, and its results are
    # <article> elements.
    page = _get(_session(), f"{BASE}/", params={"s": query}).text
    seen = set()
    for slug, title in re.findall(
        r'<article[^>]*>[\s\S]{0,400}?href="[^"]*?/anime/([^"/]+)/"[\s\S]{0,200}?title="([^"]+)"',
        page,
    ):
        if slug not in seen:
            seen.add(slug)
            print(f"{slug}\t{_html.unescape(title).strip()}")


def _episodes(s, slug: str):
    page = _get(s, f"{BASE}/anime/{slug}/").text
    # the page also links related shows, so anchor on this slug exactly
    nums = re.findall(rf'/{re.escape(slug)}-episode-([0-9.]+)/', page)
    return sorted(set(nums), key=float)


def cmd_episodes(slug: str, _lang: str) -> None:
    for n in _episodes(_session(), slug):
        print(n)


def cmd_video(slug: str, episode: str, lang: str) -> None:
    s = _session()
    ep_url = f"{BASE}/{slug}-episode-{episode}/"
    page = _get(s, ep_url).text

    # Entries are per-language here: the page declares its own Sub/Dub rather
    # than offering both. Downloading a dub as if it were a sub is silent and
    # only noticed on playback, so refuse instead of guessing.
    want = "Dub" if lang == "dub" else "Sub"
    declared = re.search(r'<i class="status (Sub|Dub)">', page)
    if declared and declared.group(1) != want:
        raise RuntimeError(f"episode {episode} is {declared.group(1)}, not {want}")

    embeds = []
    for enc in re.findall(r'<option[^>]*value="([A-Za-z0-9+/=]{20,})"', page):
        try:
            decoded = base64.b64decode(enc + "===").decode("utf-8", "ignore")
        except Exception:
            continue
        m = re.search(r'src="([^"]+)"', decoded)
        if m:
            embeds.append(_html.unescape(m.group(1)))
    if not embeds:
        raise RuntimeError(f"no servers listed for episode {episode}")

    usable = [u for u in embeds if any(h in u for h in USABLE_HOSTS)]
    if not usable:
        raise RuntimeError(
            "only encrypted servers offered for episode "
            f"{episode} ({', '.join(sorted({u.split('/')[2] for u in embeds}))})"
        )

    last = None
    for embed in usable:
        try:
            body = _get(s, embed, headers={"Referer": BASE + "/"}).text
            origin = "/".join(embed.split("/")[:3])
            master = next(
                (u for u in re.findall(r"""file:\s*["']([^"']+)["']""", body)
                 if ".m3u8" in u or "urlset" in u), None)
            if not master:
                continue
            pl = _get(s, master, headers={"Referer": origin + "/"}).text
            out = [(int(h), u.strip() if u.strip().startswith("http")
                    else master.rsplit("/", 1)[0] + "/" + u.strip())
                   for h, u in re.findall(r'RESOLUTION=\d+x(\d+)[^\n]*\n([^\n#]+)', pl)]
            if not out:
                out = [(0, master)]
            for height, url in sorted(out, reverse=True):
                print(f"{height}p >{url}>{origin}/")
            return
        except Exception as e:
            last = e
    raise RuntimeError(f"no playable server for episode {episode}" + (f" ({last})" if last else ""))


def demo() -> None:
    # no network: the parses that silently yield "nothing" if the site drifts
    rx = r'<article[^>]*>[\s\S]{0,400}?href="[^"]*?/anime/([^"/]+)/"[\s\S]{0,200}?title="([^"]+)"'
    assert re.findall(rx, '<article class="bs"><a href="/anime/overgeared/">'
                          '<img title="Overgeared"/></a></article>') == [("overgeared", "Overgeared")]
    # the popular-shows sidebar is NOT in an <article>, and must not match -
    # matching it returned One Piece as the top hit for every single query
    assert re.findall(rx, '<div class="imgseries"><a class="series" href="/anime/one-piece/">'
                          '<img title="One Piece"/></a></div>') == []

    # vidmoly writes file: '...' with single quotes; requiring double found none
    assert re.findall(r"""file:\s*["']([^"']+)["']""",
                      "file: 'https://x/master.m3u8'") == ["https://x/master.m3u8"]

    nums = re.findall(r'/overgeared-episode-([0-9.]+)/',
                      '<a href="/overgeared-episode-4/"><a href="/overgeared-episode-12/">')
    assert nums == ["4", "12"], nums

    # a server list is base64-encoded iframe HTML; the encrypted host must lose
    enc = base64.b64encode(b'<iframe src="https://vidmoly.biz/embed-x.html">').decode()
    got = re.findall(r'<option[^>]*value="([A-Za-z0-9+/=]{20,})"', f'<option value="{enc}">Omega<')
    assert got == [enc], got
    assert any(h in base64.b64decode(got[0] + "===").decode() for h in USABLE_HOSTS)
    assert "megaplay.buzz" not in USABLE_HOSTS

    variants = re.findall(r'RESOLUTION=\d+x(\d+)[^\n]*\n([^\n#]+)',
                          '#EXT-X-STREAM-INF:RESOLUTION=1600x900\nhttps://x/index.m3u8\n')
    assert variants == [("900", "https://x/index.m3u8")], variants
    print("ani-cli-nineanime parsing ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
        sys.exit(0)

    if len(sys.argv) < 2:
        sys.exit("usage: ani-cli-nineanime.py <search|episodes|video> ...")

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
