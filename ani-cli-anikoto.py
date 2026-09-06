#!/usr/bin/env python3
"""anikoto.cz backend for ~/scripts/ani-cli. Same 3-verb CLI shape as the
other helpers so sync_mode() can call any of them interchangeably.

Added 2026-09-06 as a fourth provider because the other three had all
stopped covering current-season shows: AllAnime is dead for video, anidb.app
has been 503 for days, and animehub simply lacks some cours (it carries
BLEACH TYBW parts 1-3 but not "The Calamity"). anikoto.cz had every episode.

Endpoints were taken from vorlie/ani-cli-rs (`src/anikoto_cz.rs`), which is
actively maintained against this site - worth re-reading there first if this
breaks. The chain is:

    /ajax/anime/search?keyword=      -> watch slug
    /watch/<slug>                    -> numeric show id (data-id)
    /ajax/episode/list/<show_id>     -> per-episode opaque tokens
    /ajax/server/list?servers=<tok>  -> server tokens, split by sub/dub
    /ajax/server?get=<server_tok>    -> embed url (megaplay.buzz/...)
    <embed>                          -> data-id
    <embed origin>/stream/getSources -> plaintext m3u8 (no decryption)

Both the embed and the CDN require a Referer, and the HLS segments are
disguised with a .jpg extension - ffmpeg refuses those by default
(`allowed_segment_extensions`), so downloads must go through yt-dlp. The
referrer is emitted as the third field for ani-cli's download() to pass on.

Usage:
    ani-cli-anikoto.py search <query>                 -> id\\tname per line
    ani-cli-anikoto.py episodes <id> <sub|dub>        -> episode number per line
    ani-cli-anikoto.py video <id> <episode> <sub|dub> -> "<res>p >url>referrer" per line
    ani-cli-anikoto.py subs  <id> <episode> <sub|dub> -> "<lang>\turl\treferer" per track
"""
import html as _html
import re
import sys

from curl_cffi import requests

BASE = "https://anikoto.cz"


def _session() -> requests.Session:
    s = requests.Session(impersonate="chrome124")
    s.headers.update({"Referer": BASE + "/", "X-Requested-With": "XMLHttpRequest"})
    return s


def _get(s, url, **kw):
    """GET that fails loudly - a silent empty result is indistinguishable from
    'this show does not exist' and gets reported as a matching problem."""
    r = s.get(url, timeout=kw.pop("timeout", 25), **kw)
    if r.status_code != 200:
        raise RuntimeError(f"anikoto.cz returned HTTP {r.status_code} for {url}")
    return r


def _result(payload, what):
    if payload.get("status") != 200:
        raise RuntimeError(f"anikoto.cz {what}: status {payload.get('status')}")
    res = payload.get("result")
    if isinstance(res, dict):
        res = res.get("html", res)
    if not res:
        raise RuntimeError(f"anikoto.cz {what}: empty result")
    return res


def cmd_search(query: str) -> None:
    s = _session()
    page = _result(_get(s, f"{BASE}/ajax/anime/search", params={"keyword": query}).json(), "search")
    if not isinstance(page, str):
        raise RuntimeError("search returned no markup")
    # each hit: <a class="item" href=".../watch/<slug>"> ... <div class="name
    # d-title" data-jp="<romaji>">English title</div>. Emit the English title,
    # since that is what AniList gives us to match against.
    for slug, name in re.findall(
        r'href="[^"]*?/watch/([^"/?#]+)"[\s\S]{0,900}?class="name[^"]*"[^>]*>([^<]+)<', page
    ):
        print(f"{slug}\t{_html.unescape(name).strip()}")


def _show_id(s, slug: str) -> str:
    h = _get(s, f"{BASE}/watch/{slug}", headers={"X-Requested-With": ""}).text
    m = re.search(r'data-id="(\d+)"', h)
    if not m:
        raise RuntimeError(f"no show id on watch page for {slug}")
    return m.group(1)


def _episodes(s, slug: str):
    """[(episode_number, opaque_token)] in site order."""
    markup = _result(
        _get(s, f"{BASE}/ajax/episode/list/{_show_id(s, slug)}",
             params={"style": "grid", "vrf": ""}).json(), "episode list")
    eps = re.findall(r'data-num="([0-9.]+)"[^>]*data-ids?="([^"]+)"', markup)
    if not eps:
        eps = [(n, i) for i, n in re.findall(r'data-ids?="([^"]+)"[^>]*data-num="([0-9.]+)"', markup)]
    return eps


def cmd_episodes(slug: str, _lang: str) -> None:
    for num, _tok in _episodes(_session(), slug):
        print(num)


def _sources(s, slug: str, episode: str, lang: str):
    """(sources_json, embed_origin) for one episode, trying each server."""
    tok = next((t for n, t in _episodes(s, slug) if n == str(episode) or n == str(episode).lstrip("0")), None)
    if tok is None:
        raise RuntimeError(f"episode {episode} not found")

    servers = _result(_get(s, f"{BASE}/ajax/server/list", params={"servers": tok}).json(), "server list")
    want = "dub" if lang == "dub" else "sub"
    chosen = next((b for b in re.split(r'data-type="', servers) if b.startswith(want)), None)
    if chosen is None:
        raise RuntimeError(f"no {want} servers for episode {episode}")

    last = None
    for st in re.findall(r'data-link-id="([^"]+)"', chosen):
        try:
            res = _get(s, f"{BASE}/ajax/server", params={"get": st}).json().get("result") or {}
            embed = res.get("url")
            if not embed:
                continue
            origin = "/".join(embed.split("/")[:3])
            page = _get(s, embed, headers={"Referer": BASE + "/"}).text
            m = re.search(r'data-id="(\d+)"', page)
            if not m:
                continue
            data = _get(s, f"{origin}/stream/getSources", params={"id": m.group(1)},
                        headers={"Referer": origin + "/"}).json()
            # Accept a response carrying EITHER a playable source or subtitle
            # tracks. As of 2026-09-06 this host returns an encrypted "enc"
            # blob instead of a plaintext "sources" URL, but "tracks" stayed
            # in the clear - so subtitles still work even while video does not.
            if (data.get("sources") or {}).get("file") or data.get("tracks"):
                return data, origin
        except Exception as e:
            last = e
    raise RuntimeError(f"no playable server for episode {episode}" + (f" ({last})" if last else ""))


def cmd_subs(slug: str, episode: str, lang: str) -> None:
    """Emit "<lang>\t<url>\t<referer>" per external subtitle track. These streams carry no
    embedded subtitle stream at all - the player is expected to overlay these
    - so without them a downloaded episode has no subtitles whatsoever."""
    data, origin = _sources(_session(), slug, episode, lang)
    for tr in data.get("tracks") or []:
        if (tr.get("kind") or "").lower() not in ("captions", "subtitles"):
            continue  # skip thumbnail/preview tracks
        url = tr.get("file")
        if not url:
            continue
        # Labels are inconsistent between episodes of the same show - one says
        # "English", the next "eng" - so normalise both full names and ISO
        # 639-2 codes down to the 2-letter code callers ask for.
        label = (tr.get("label") or "und").strip().lower()
        names = {"english": "en", "german": "de", "spanish": "es", "french": "fr",
                 "italian": "it", "portuguese": "pt", "russian": "ru",
                 "japanese": "ja", "arabic": "ar"}
        iso3 = {"eng": "en", "ger": "de", "deu": "de", "spa": "es", "fre": "fr",
                "fra": "fr", "ita": "it", "por": "pt", "rus": "ru", "jpn": "ja",
                "ara": "ar"}
        code = names.get(label) or iso3.get(label[:3]) or label[:3]
        # the subtitle CDN 403s without a Referer, exactly like the video one
        print(f"{code}\t{url}\t{origin}/")


def cmd_video(slug: str, episode: str, lang: str) -> None:
    s = _session()
    data, origin = _sources(s, slug, episode, lang)
    master = (data.get("sources") or {}).get("file")
    if not master:
        raise RuntimeError(
            "anikoto returned encrypted sources (no plaintext m3u8); "
            "video unavailable from this provider" if data.get("enc")
            else "anikoto returned no video source")
    # the master playlist itself needs the referer, hence fetching it here
    pl = _get(s, master, headers={"Referer": origin + "/"}).text
    base = master.rsplit("/", 1)[0]
    out = [(int(h), u if u.startswith("http") else f"{base}/{u.strip()}")
           for h, u in re.findall(r'RESOLUTION=\d+x(\d+)[^\n]*\n([^\n#]+)', pl)]
    if not out:  # single-variant playlist
        out = [(0, master)]
    for height, url in sorted(out, reverse=True):
        print(f"{height}p >{url}>{origin}/")


def demo() -> None:
    # no network: the parsing that silently yields "no results" if it drifts
    assert BASE.startswith("https://")
    eps = re.findall(r'data-num="([0-9.]+)"[^>]*data-ids?="([^"]+)"',
                     '<li data-num="7" data-ids="TOK7"></li><li data-num="8" data-ids="TOK8"></li>')
    assert eps == [("7", "TOK7"), ("8", "TOK8")], eps
    pl = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1920x1080\nindex-f1.m3u8\n'
    got = re.findall(r'RESOLUTION=\d+x(\d+)[^\n]*\n([^\n#]+)', pl)
    assert got == [("1080", "index-f1.m3u8")], got
    print("ani-cli-anikoto parsing ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
        sys.exit(0)

    if len(sys.argv) < 2:
        sys.exit("usage: ani-cli-anikoto.py <search|episodes|video> ...")

    cmd, args = sys.argv[1], sys.argv[2:]
    try:
        if cmd == "search" and len(args) == 1:
            cmd_search(args[0])
        elif cmd == "episodes" and len(args) == 2:
            cmd_episodes(args[0], args[1])
        elif cmd == "video" and len(args) == 3:
            cmd_video(args[0], args[1], args[2])
        elif cmd == "subs" and len(args) == 3:
            cmd_subs(args[0], args[1], args[2])
        else:
            sys.exit("bad arguments")
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
