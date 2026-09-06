#!/usr/bin/env python3
"""animehub backend for ~/scripts/ani-cli. Same 3-verb CLI shape as
ani-cli-allanime.py / ani-cli-anidb.py so sync_mode() can call any of them
interchangeably.

Added 2026-09-06 as a second working provider: AllAnime is dead for video
(its keygen CI has been frozen since 2026-08-03) and anidb.app went 503
"Under Maintenance" for days, leaving the pipeline with nothing. animehub is
anipy-api's own AllAnime replacement, so it needs no new dependency - just a
thin wrapper, same reasoning as the AllAnime helper.

Numbering is per-season starting at 1, matching AniList, so none of the
absolute-numbering translation the anidb helper needs applies here.

Streams are HLS served with `content-type: image/jpeg` and require a
Referer, so the referrer is emitted as a third field (ani-cli's download()
passes it to yt-dlp via --referer). Note yt-dlp's --download-sections cannot
seek these; plain full downloads work.

Usage:
    ani-cli-animehub.py search <query>                     -> id\\tname per line
    ani-cli-animehub.py episodes <id> <sub|dub>            -> episode number per line
    ani-cli-animehub.py video <id> <episode> <sub|dub>     -> "<res>p >url>referrer" per line
"""
import sys

from anipy_api.provider import list_providers
from anipy_api.provider.base import LanguageTypeEnum


def _provider():
    for p in list_providers():
        if p.NAME == "animehub":
            return p()
    raise RuntimeError("animehub provider not present in this anipy-api build")


def _lang(s: str) -> LanguageTypeEnum:
    return LanguageTypeEnum.DUB if s == "dub" else LanguageTypeEnum.SUB


def cmd_search(query: str) -> None:
    for r in _provider().get_search(query):
        print(f"{r.identifier}\t{r.name}")


def cmd_episodes(identifier: str, lang: str) -> None:
    for e in _provider().get_episodes(identifier, _lang(lang)):
        print(e)


def cmd_video(identifier: str, episode: str, lang: str) -> None:
    streams = list(_provider().get_video(identifier, episode, _lang(lang)))
    if not streams:
        raise RuntimeError(f"no streams for episode {episode}")
    for s in sorted(streams, key=lambda s: s.resolution or 0, reverse=True):
        print(f"{s.resolution}p >{s.url}>{s.referrer or ''}")


def demo() -> None:
    # no network: argv dispatch + lang mapping only
    assert _lang("dub") == LanguageTypeEnum.DUB
    assert _lang("sub") == LanguageTypeEnum.SUB
    assert _lang("anything-else") == LanguageTypeEnum.SUB
    print("ani-cli-animehub dispatch ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
        sys.exit(0)

    if len(sys.argv) < 2:
        sys.exit("usage: ani-cli-animehub.py <search|episodes|video> ...")

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
