#!/usr/bin/env python3
"""AllAnime backend for ~/scripts/ani-cli. Thin CLI wrapper around anipy_api's
AllAnimeProvider (the same class /home/tolga/scripts/anime-sync/anilist_sync.py
uses) so the ani-cli-style shell script gets identical scraping mechanics
without reimplementing its AES-GCM signed-request crypto in shell.

Usage:
    ani-cli-allanime.py search <query>            -> id\\tname per line
    ani-cli-allanime.py episodes <id> <sub|dub>    -> episode number per line
    ani-cli-allanime.py video <id> <episode> <sub|dub> -> "<res>p >url" per line
"""
import sys

if sys.version_info < (3, 12):
    import importlib.util
    from pathlib import Path

    _spec = importlib.util.find_spec("anipy_api")
    if _spec and _spec.submodule_search_locations:
        _f = Path(_spec.submodule_search_locations[0]) / "provider/providers/allanime_provider.py"
        _src = _f.read_text()
        if '{keygen["epoch"]}' in _src:
            _f.write_text(
                _src.replace('{keygen["epoch"]}', "{keygen['epoch']}").replace(
                    '{keygen["query_hash"]}', "{keygen['query_hash']}"
                )
            )


# ported from anilist_sync.py::_patch_allanime_query_hash - upstream's keygen
# CI sometimes publishes query_hash: null, which breaks get_video. Recompute
# it from the live site and self-disable once upstream fixes it.
def _patch_query_hash():
    import functools
    import hashlib
    import re

    import requests

    import anipy_api.provider.providers.allanime_provider as _aa

    CDN = "https://cdn.mkissa.net/all/mk/_app/immutable"
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

    def scrape_query_hash():
        s, h = requests.Session(), {"User-Agent": UA}
        html = s.get("https://mkissa.to/", headers=h, timeout=15).text
        app = CDN + re.search(r"/entry/app\.[A-Za-z0-9_.-]+\.js", html).group()
        app_js = s.get(app, headers=h, timeout=15).text
        for chunk in re.findall(r'"\.\./(chunks/[A-Za-z0-9_.-]+\.js)"', app_js)[:5]:
            js = s.get(f"{CDN}/{chunk}", headers=h, timeout=15).text
            if "VaildTranslationTypeEnumType" not in js and "x-aa-boot" not in js:
                continue
            tmpl = next(
                (t for t in re.findall(r"(\nquery\([^`]*)`", js) if "sourceUrls" in t and "episode(" in t),
                None,
            )
            if tmpl is None:
                return None

            def resolve(t, depth=0):
                if depth > 12:
                    return t
                for name in re.findall(r"\$\{([^}]+)\}", t):
                    if name.endswith("()"):
                        m = re.search(
                            re.escape(name[:-2]) + r"\s*=\s*\w+\s*=>\s*\w+\s*\?\s*`[^`]*`\s*:\s*`([^`]*)`", js
                        )
                    else:
                        m = re.search(re.escape(name) + r"\s*=\s*`([^`]*)`", js)
                    t = t.replace("${" + name + "}", resolve(m.group(1), depth + 1) if m else "")
                return t

            query = resolve(tmpl)
            return None if "${" in query else hashlib.sha256(query.encode()).hexdigest()
        return None

    _orig = _aa.fetch_keygen

    @functools.lru_cache()
    def fetch_keygen(session):
        keygen = dict(_orig(session))
        if not keygen.get("query_hash"):
            keygen["query_hash"] = scrape_query_hash()
        return keygen

    _aa.fetch_keygen = fetch_keygen


_patch_query_hash()

from anipy_api.provider.base import LanguageTypeEnum  # noqa: E402
from anipy_api.provider.providers.allanime_provider import AllAnimeProvider  # noqa: E402


def _lang(s: str) -> LanguageTypeEnum:
    return LanguageTypeEnum.DUB if s == "dub" else LanguageTypeEnum.SUB


def cmd_search(query: str) -> None:
    for r in AllAnimeProvider().get_search(query):
        print(f"{r.identifier}\t{r.name}")


def cmd_episodes(identifier: str, lang: str) -> None:
    for e in AllAnimeProvider().get_episodes(identifier, _lang(lang)):
        print(e)


def cmd_video(identifier: str, episode: str, lang: str) -> None:
    # 3rd field is the Referer header the source needs (mp4upload etc. 403
    # without it) - anipy_api tracks this per-stream, so surface it rather
    # than let downloads fail silently on hosts that check it.
    for s in AllAnimeProvider().get_video(identifier, episode, _lang(lang)):
        print(f"{s.resolution}p >{s.url}>{s.referrer or ''}")


def demo() -> None:
    # no network: just proves argv dispatch and lang mapping don't crash/typo
    assert _lang("dub") == LanguageTypeEnum.DUB
    assert _lang("sub") == LanguageTypeEnum.SUB
    assert _lang("anything-else") == LanguageTypeEnum.SUB
    print("ani-cli-allanime dispatch ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
        sys.exit(0)

    if len(sys.argv) < 2:
        sys.exit("usage: ani-cli-allanime.py <search|episodes|video> ...")

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
