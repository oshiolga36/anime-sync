#!/usr/bin/env python3
"""Turn summary.json into the body of a Telegram build notification.

Prints nothing if there is no summary (e.g. the build failed before the sync
ran) — the notifier still sends its header, so a missing file degrades to the
old terse message instead of breaking the notification.
"""
import json
import sys
from pathlib import Path

MAX_SHOWS = 15          # keep the message readable, and under Telegram's 4096 limit
MAX_EPS = 6


def fmt_eps(eps: list) -> str:
    eps = sorted(eps)
    shown = ", ".join(f"E{e:02d}" for e in eps[:MAX_EPS])
    return shown + (f" +{len(eps) - MAX_EPS} more" if len(eps) > MAX_EPS else "")


def build(summary: dict) -> str:
    stats = summary.get("stats", {})
    shows = summary.get("shows", [])

    lines = []
    for s in shows[:MAX_SHOWS]:
        if s.get("status") == "downloaded" and s.get("episodes"):
            lines.append(f"↓ {s['title']}: {fmt_eps(s['episodes'])}")
        if s.get("status") == "failed":
            lines.append(f"! {s['title']}: {s.get('detail', 'failed')}")
        elif s.get("errors"):
            lines.append(f"! {s['title']}: {len(s['errors'])} episode(s) failed, will retry")

    if len(shows) > MAX_SHOWS:
        lines.append(f"…and {len(shows) - MAX_SHOWS} more")

    if not lines:
        lines.append("Nothing new — everything up to date.")

    tail = f"{stats.get('downloaded', 0)} downloaded, {stats.get('skipped', 0)} up to date"
    if stats.get("failed"):
        tail += f", {stats['failed']} failed"
    return "\n".join(lines) + "\n" + tail


def changed(summary: dict) -> bool:
    """True if this run actually did something worth interrupting someone for."""
    stats = summary.get("stats", {})
    return bool(stats.get("downloaded") or stats.get("failed"))


def demo() -> None:
    out = build({
        "stats": {"downloaded": 3, "skipped": 55, "failed": 1},
        "shows": [
            {"title": "Frieren", "status": "downloaded", "episodes": [14, 15], "errors": []},
            {"title": "Dandadan", "status": "downloaded", "episodes": [7], "errors": [8]},
            {"title": "Bocchi", "status": "failed", "detail": "no provider match"},
        ],
    })
    assert "Frieren: E14, E15" in out, out
    assert "Dandadan: E07" in out, out
    assert "! Dandadan: 1 episode(s) failed, will retry" in out, out
    assert "! Bocchi: no provider match" in out, out
    assert "3 downloaded, 55 up to date, 1 failed" in out, out
    assert "Nothing new" in build({"stats": {}, "shows": []})
    assert fmt_eps([1, 2, 3, 4, 5, 6, 7, 8]) == "E01, E02, E03, E04, E05, E06 +2 more"

    assert changed({"stats": {"downloaded": 1, "failed": 0}})
    assert changed({"stats": {"downloaded": 0, "failed": 2}})       # breakage is news
    assert not changed({"stats": {"downloaded": 0, "skipped": 57}})  # idle run
    assert not changed({})
    print("summary_text ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
        sys.exit(0)

    f = Path(__file__).parent / "summary.json"
    summary = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    if "--changed" in sys.argv:
        # exit 0 = worth notifying about, 1 = idle run. No summary = idle.
        sys.exit(0 if summary and changed(summary) else 1)

    if summary:
        print(build(summary))
