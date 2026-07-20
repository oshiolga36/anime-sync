#!/usr/bin/env python3
"""Turn summary.json into the body of a Telegram build notification.

Prints nothing if there is no summary (e.g. the build failed before the sync
ran) — the notifier still sends its header, so a missing file degrades to the
old terse message instead of breaking the notification.
"""
import json
import os
import sys
from pathlib import Path

# Signature of the last reported failure set. Lives in the persisted state dir,
# not the workspace, so it survives the workspace being wiped.
SIG_FILE = Path(os.environ.get("STATE", str(Path(__file__).parent))) / "last_failures.json"

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
    if summary.get("anipy"):
        tail += f" · anipy {summary['anipy']}"
    return "\n".join(lines) + "\n" + tail


def state(summary: dict) -> str:
    """Classify the run: new / mixed / errors / idle / unknown.

    'downloaded > 0' and 'failed > 0' are independent — a run can do both, and
    reporting only "something changed" produced messages claiming new episodes
    when in fact everything had failed.
    """
    if not summary:
        return "unknown"
    stats = summary.get("stats", {})
    got, bad = stats.get("downloaded", 0), stats.get("failed", 0)
    if got and bad:
        return "mixed"
    if got:
        return "new"
    if bad:
        return "errors"
    return "idle"


def signature(summary: dict) -> list:
    """Stable fingerprint of *what* is failing, ignoring run-to-run noise.

    Includes the reason, so a show whose failure mode changes (provider gone ->
    crypto error) still re-alerts rather than hiding behind the old signature.
    """
    sig = []
    for s in summary.get("shows", []):
        if s.get("status") == "failed":
            sig.append(f"{s['title']}|{s.get('detail', 'failed')}")
        elif s.get("errors"):
            sig.append(f"{s['title']}|{len(s['errors'])} episodes")
    return sorted(sig)


def decide(current: list, previous: list) -> str:
    """notify (new/changed breakage) / suppress (same as last time) /
    recovered (was broken, now clean) / quiet (clean, was clean)."""
    if current:
        return "suppress" if current == previous else "notify"
    return "recovered" if previous else "quiet"


def titles(sig: list) -> list:
    return [s.split("|", 1)[0] for s in sig]


def demo() -> None:
    out = build({
        "stats": {"downloaded": 3, "skipped": 55, "failed": 1},
        "anipy": "3.8.18",
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
    assert "3 downloaded, 55 up to date, 1 failed · anipy 3.8.18" in out, out
    # older summaries have no anipy field — must not print "anipy None"
    assert "anipy" not in build({"stats": {"downloaded": 1}, "shows": []}), "leaked anipy"
    assert "Nothing new" in build({"stats": {}, "shows": []})
    assert fmt_eps([1, 2, 3, 4, 5, 6, 7, 8]) == "E01, E02, E03, E04, E05, E06 +2 more"

    assert state({"stats": {"downloaded": 1, "failed": 0}}) == "new"
    assert state({"stats": {"downloaded": 2, "failed": 3}}) == "mixed"
    # the regression: all-failures must NOT read as "new episodes are in"
    assert state({"stats": {"downloaded": 0, "failed": 6}}) == "errors"
    assert state({"stats": {"downloaded": 0, "skipped": 57}}) == "idle"
    assert state({}) == "unknown"

    broken = {"shows": [
        {"title": "Rakugo", "status": "downloaded", "episodes": [], "errors": [1, 2, 3]},
        {"title": "Bocchi", "status": "failed", "detail": "no provider match"},
    ]}
    sig = signature(broken)
    assert sig == ["Bocchi|no provider match", "Rakugo|3 episodes"], sig

    # the whole point: identical breakage twice in a row must not re-alert
    assert decide(sig, []) == "notify"
    assert decide(sig, sig) == "suppress"
    # ...but a changed failure mode must break through the suppression
    worse = signature({"shows": [{"title": "Rakugo", "status": "failed", "detail": "gone"}]})
    assert decide(worse, sig) == "notify"
    # clearing up announces itself once, then goes quiet
    assert decide([], sig) == "recovered"
    assert decide([], []) == "quiet"
    assert titles(sig) == ["Bocchi", "Rakugo"]
    print("summary_text ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
        sys.exit(0)

    f = Path(__file__).parent / "summary.json"
    summary = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    if "--state" in sys.argv:
        print(state(summary))
        sys.exit(0)

    if "--check-alert" in sys.argv:
        # A build that died before writing a summary tells us nothing about what
        # is failing — say notify and leave the stored signature untouched, so a
        # crash can neither suppress a real alert nor fake a recovery.
        if not summary:
            print("notify")
            sys.exit(0)

        previous = []
        if SIG_FILE.exists():
            try:
                previous = json.loads(SIG_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

        current = signature(summary)
        verdict = decide(current, previous)

        SIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        SIG_FILE.write_text(json.dumps(current), encoding="utf-8")

        print(verdict)
        if verdict == "recovered":
            print(", ".join(titles(previous)))
        sys.exit(0)

    if summary:
        print(build(summary))
