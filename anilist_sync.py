import json
import re
import signal
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

# anipy_api 3.8.x ships a py3.12+ f-string (same-quote nesting) that SyntaxErrors on
# py<3.12. Re-apply the quote swap if a reinstall wiped it. Idempotent, no-op on py3.12+.
if sys.version_info < (3, 12):
    import importlib.util

    _spec = importlib.util.find_spec("anipy_api")
    if _spec and _spec.submodule_search_locations:
        _f = Path(_spec.submodule_search_locations[0]) / "provider/providers/allanime_provider.py"
        _src = _f.read_text()
        if '{keygen["epoch"]}' in _src:
            _f.write_text(_src.replace('{keygen["epoch"]}', "{keygen['epoch']}").replace(
                '{keygen["query_hash"]}', "{keygen['query_hash']}"))

from anipy_api.anilist import AniList, AniListAdapter, AniListAnime, AniListMyListStatusEnum
from anipy_api.anime import Anime
from anipy_api.download import Downloader
from anipy_api.provider import LanguageTypeEnum, list_providers

from anipy_cli.config import Config
from anipy_cli.util import get_prefered_providers

console = Console()


def signal_handler(signum, frame):
    console.print("\n[yellow]Interrupted. Progress saved.[/]")
    sys.exit(0)


def resolve_folder_name(anime: AniListAnime) -> str:
    if anime.alternative_titles:
        if anime.alternative_titles.english:
            name = anime.alternative_titles.english
        elif anime.alternative_titles.romaji:
            name = anime.alternative_titles.romaji
        else:
            name = anime.title.user_preferred
    else:
        name = anime.title.user_preferred

    return Downloader._get_valid_pathname(name)


def update_aliases(anilist_anime: AniListAnime, folder_name: str, alias_path: Path) -> None:
    """Point a show's other titles at the folder it ALREADY lives in, so the
    consolidator folds any english/romaji duplicate folder into that one.
    Root-cause fix: dupes stop accumulating instead of being cleaned up later.

    Safety rules:
    - canonical = the show's current folder (never forces romaji, so it never
      renames a folder you already have).
    - if ANY of the show's titles already appear in aliases.yaml (either side),
      the show is left entirely alone - your hand-curated entries win.
    - append-only, so comments and manual lines are preserved.
    """
    canonical = Downloader._get_valid_pathname(folder_name)

    titles = {canonical}
    alt = anilist_anime.alternative_titles
    if alt and alt.english:
        titles.add(Downloader._get_valid_pathname(alt.english))
    if alt and alt.romaji:
        titles.add(Downloader._get_valid_pathname(alt.romaji))
    titles.add(Downloader._get_valid_pathname(anilist_anime.title.user_preferred))

    import yaml
    existing = set()
    if alias_path.exists():
        try:
            data = yaml.safe_load(alias_path.read_text(encoding="utf-8")) or {}
            existing = {str(k) for k in data} | {str(v) for v in data.values()}
        except Exception:
            return  # unreadable alias file: do nothing rather than risk a bad append

    if titles & existing:
        return  # already known/curated -> hands off

    new = sorted(titles - {canonical})
    if not new:
        return

    def q(s: str) -> str:  # quote only when the name has YAML-special chars (e.g. "[Oshi no Ko]")
        return '"' + s.replace('"', '\\"') + '"' if re.search(r'[:#\[\]{}]', s) else s

    with alias_path.open("a", encoding="utf-8") as f:
        for k in new:
            f.write(f"{q(k)}: {q(canonical)}\n")


def contiguous_watermark(last_downloaded, new_eps: list, ok_eps: set):
    """Highest episode reachable from `last_downloaded` through UNBROKEN successes.

    `last_downloaded` is a high-water mark: everything at or below it is treated
    as done forever, and next run only looks past it. So it must never cross an
    episode that failed — otherwise that episode is skipped permanently and the
    gap is silent until you try to watch it. Stop at the first miss and let the
    next run retry from there.
    """
    hw = last_downloaded
    for ep in new_eps:
        if ep not in ok_eps:
            break
        hw = ep
    return hw


def load_watchlist(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        console.print(f"  [yellow]![/] Warning: {path} is corrupt or empty, starting fresh")
        return {}


def save_watchlist(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def pick_language(languages: set) -> LanguageTypeEnum:
    if LanguageTypeEnum.SUB in languages:
        return LanguageTypeEnum.SUB
    return LanguageTypeEnum.DUB


def ensure_mapping(
    anilist_obj: Optional[AniList],
    anilist_anime: Optional[AniListAnime],
    watchlist: dict,
    watchlist_path: Path,
    key: Optional[str] = None,
) -> Optional[tuple]:
    if not key and anilist_anime:
        key = str(anilist_anime.id)

    if not key:
        return None

    entry = watchlist.get(key)

    if entry:
        provider_cls = next(
            (p for p in list_providers() if p.NAME == entry["provider"]),
            None,
        )
        if provider_cls is None:
            console.print(f"  [red]![/] Provider '{entry['provider']}' not found, skipping")
            return None
        config = Config()
        url_override = config.provider_urls.get(entry["provider"], None)
        provider = provider_cls(url_override)
        lang_set = {LanguageTypeEnum(l) for l in entry["languages"]}
        lang = pick_language(lang_set)

        folder_name = entry.get("folder_name")
        if not folder_name:
            if anilist_anime:
                folder_name = resolve_folder_name(anilist_anime)
            else:
                folder_name = f"Anime_{key}"

        folder_name = Downloader._get_valid_pathname(folder_name)
        entry["folder_name"] = folder_name

        anime = Anime(provider, folder_name, entry["identifier"], lang_set)
        return anime, lang, folder_name

    if anilist_obj is None or anilist_anime is None:
        return None

    folder_name = resolve_folder_name(anilist_anime)
    config = Config()
    result = None
    for p in get_prefered_providers("anilist"):
        adapter = AniListAdapter(anilist_obj, p)
        result = adapter.from_anilist(
            anilist_anime,
            config.tracker_mapping_min_similarity,
            config.tracker_mapping_use_filters,
            config.tracker_mapping_use_alternatives,
        )
        if result is not None:
            break

    if result is None:
        console.print(f"  [red]![/] Could not match '[bold]{anilist_anime.title.user_preferred}[/]' to a provider, skipping")
        return None

    lang = pick_language(result.languages)
    watchlist[key] = {
        "folder_name": folder_name,
        "provider": result.provider.NAME,
        "identifier": result.identifier,
        "languages": [l.value for l in result.languages],
        "last_downloaded": -1,
    }
    save_watchlist(watchlist_path, watchlist)
    return result, lang, folder_name


def sync_all(anilist_obj: Optional[AniList], watchlist_path: Path) -> None:
    config = Config()
    watchlist = load_watchlist(watchlist_path)

    items_to_sync = []
    if anilist_obj:
        console.print("[blue]>>[/] Fetching AniList watching list...")
        try:
            watching = anilist_obj.get_anime_list(AniListMyListStatusEnum.WATCHING)
            console.print(f"[blue]>>[/] Found [bold]{len(watching)}[/] show(s) in WATCHING list.\n")
            for a in watching:
                items_to_sync.append((str(a.id), a.title.user_preferred, a))
        except Exception as e:
            console.print(f"[red]![/] Error fetching AniList: {e}. Falling back to local watchlist.")
            anilist_obj = None

    if not anilist_obj:
        console.print("[yellow]>>[/] Using local watchlist for synchronization...")
        for key, entry in watchlist.items():
            if not entry.get("skip"):
                title = entry.get("folder_name", f"ID: {key}")
                items_to_sync.append((key, title, None))

    state = {"ep_task_id": None}

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        overall_task = progress.add_task("Overall", total=len(items_to_sync))
        ep_task_id = progress.add_task("", total=100, visible=False)

        def progress_callback(pct):
            if state["ep_task_id"] is not None:
                progress.update(state["ep_task_id"], completed=pct)

        downloader = Downloader(
            progress_callback=progress_callback,
            info_callback=lambda msg, *_: None,
            soft_error_callback=lambda msg, *_: progress.console.print(f"  [red]![/] {msg}"),
        )

        stats = {"downloaded": 0, "skipped": 0, "failed": 0, "shows_with_new": 0}
        # Per-show outcome, written to summary.json for the Jenkins notifier.
        # Structured rather than scraped from the console: rich's output is for
        # humans and reformatting it would silently break the notifications.
        report = []

        for key, title, anilist_anime in items_to_sync:
            progress.update(overall_task, description=f"Syncing: {title}")
            progress.console.print(f"\n[dim]──[/] [bold]{title}[/]")

            if watchlist.get(key, {}).get("skip"):
                progress.console.print(f"  [yellow]–[/] Skipped (skip flag set)")
                stats["skipped"] += 1
                progress.advance(overall_task)
                continue

            try:
                mapping = ensure_mapping(anilist_obj, anilist_anime, watchlist, watchlist_path, key=key)
            except Exception as e:
                progress.console.print(f"  [red]![/] Could not map to provider: {e}")
                mapping = None
            if mapping is None:
                stats["failed"] += 1
                report.append({"title": title, "status": "failed", "detail": "no provider match"})
                progress.advance(overall_task)
                continue

            anime, lang, folder_name = mapping
            last_downloaded = watchlist[key]["last_downloaded"]

            if anilist_anime is not None:
                try:
                    update_aliases(anilist_anime, folder_name, config.download_folder_path / "aliases.yaml")
                except Exception as e:
                    progress.console.print(f"  [yellow]![/] alias update skipped: {e}")

            try:
                episodes = anime.get_episodes(lang)
            except Exception as e:
                progress.console.print(f"  [red]![/] Could not fetch episodes: {e}")
                stats["failed"] += 1
                report.append({"title": title, "status": "failed", "detail": f"episode list: {e}"})
                progress.advance(overall_task)
                continue

            if last_downloaded == -1:
                new_eps = episodes
            else:
                try:
                    idx = episodes.index(last_downloaded)
                    new_eps = episodes[idx + 1:]
                except ValueError:
                    new_eps = [ep for ep in episodes if ep > last_downloaded]

            if not new_eps:
                progress.console.print(f"  [green]✓[/] Up to date")
                stats["skipped"] += 1
                progress.advance(overall_task)
                continue

            progress.console.print(f"  [cyan]↓[/] {len(new_eps)} new episode(s) to download")

            valid_folder = Downloader._get_valid_pathname(folder_name)
            eps_ok = 0
            got, lost = [], []
            ok_eps = set()

            def advance_watermark():
                watchlist[key]["last_downloaded"] = contiguous_watermark(
                    last_downloaded, new_eps, ok_eps
                )
                save_watchlist(watchlist_path, watchlist)

            for ep in new_eps:
                try:
                    stream = anime.get_video(ep, lang)
                    if stream is None:
                        # no stream is a failure, not a silent skip: it must block the
                        # watermark so the next run retries this episode
                        progress.console.print(f"  [red]![/] No stream for episode {ep}")
                        stats["failed"] += 1
                        lost.append(ep)
                        continue

                    filename = config.download_name_format.format(
                        show_name=valid_folder,
                        episode_number=str(stream.episode).zfill(2),
                        quality=stream.resolution,
                        provider=anime.provider.NAME,
                        type=str(stream.language),
                    )
                    filename = Downloader._get_valid_pathname(filename)
                    download_path = config.download_folder_path / valid_folder / filename

                    progress.update(ep_task_id, description=f"  Ep {ep:02d}", completed=0, visible=True)
                    state["ep_task_id"] = ep_task_id

                    downloader.download(
                        stream,
                        download_path,
                        container=config.remux_to,
                        ffmpeg=config.ffmpeg_hls,
                    )

                    progress.update(ep_task_id, visible=False)
                    state["ep_task_id"] = None

                    ok_eps.add(ep)
                    advance_watermark()
                    eps_ok += 1
                    got.append(ep)
                    stats["downloaded"] += 1
                except Exception as e:
                    progress.update(ep_task_id, visible=False)
                    state["ep_task_id"] = None
                    progress.console.print(f"  [red]![/] Error downloading episode {ep}: {e}")
                    stats["failed"] += 1
                    lost.append(ep)
                    continue

            if eps_ok:
                progress.console.print(f"  [green]✓[/] Downloaded {eps_ok} episode(s)")
                stats["shows_with_new"] += 1

            if got or lost:
                report.append({"title": title, "status": "downloaded", "episodes": got, "errors": lost})

            progress.advance(overall_task)

    # Written even when nothing happened, so a stale summary from a previous run
    # can never be reported as if it were this one's.
    # anipy-api is upgraded on every run, so it is the usual suspect when a sync
    # that worked yesterday stops working. Recording it turns "did anipy change?"
    # into a fact in the notification instead of an investigation.
    try:
        from importlib.metadata import version as _pkg_version

        anipy_version = _pkg_version("anipy-api")
    except Exception:
        anipy_version = "?"

    summary_path = Path(__file__).parent / "summary.json"
    summary_path.write_text(
        json.dumps({"stats": stats, "shows": report, "anipy": anipy_version}, indent=2),
        encoding="utf-8",
    )

    console.rule("[bold blue]Sync Complete[/]")
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    table.add_column(justify="right")
    table.add_row("[green]✓[/]", "Shows processed", str(len(items_to_sync)))
    table.add_row("[cyan]↓[/]", "Episodes downloaded", str(stats["downloaded"]))
    table.add_row("[green]–[/]", "Up to date", str(stats["skipped"]))
    if stats["failed"]:
        table.add_row("[red]![/]", "Errors", str(stats["failed"]))
    else:
        table.add_row("[green]✓[/]", "Errors", "0")
    console.print(table)


def main() -> None:
    signal.signal(signal.SIGINT, signal_handler)

    console.rule("[bold blue]anilist-sync[/]")

    config = Config()
    token = config.anilist_token
    watchlist_path = Path(__file__).parent / "watchlist.json"

    if not token:
        console.print("[red]Error:[/] anilist_token is not set in your config.yaml")
        sys.exit(1)

    anilist_obj = None
    try:
        anilist_obj = AniList.from_implicit_grant(token)
        console.print("[green]✓[/] Connected to AniList\n")
    except Exception as e:
        console.print(f"[yellow]Warning:[/] Failed to authenticate with AniList: {e}")
        console.print("[yellow]Running in fallback mode using local watchlist.[/]\n")

    sync_all(anilist_obj, watchlist_path)


def demo() -> None:
    cw = contiguous_watermark
    # all succeed -> advance to the end
    assert cw(3, [4, 5, 6], {4, 5, 6}) == 6
    # the regression this guards: 5 fails, 6 succeeds -> must NOT jump to 6,
    # or episode 5 is never retried
    assert cw(4, [5, 6], {6}) == 4
    # partial run: 5 and 6 land, 7 fails
    assert cw(4, [5, 6, 7], {5, 6}) == 6
    # nothing landed -> unchanged
    assert cw(4, [5, 6], set()) == 4
    # first-ever sync starts at -1
    assert cw(-1, [1, 2, 3], {1, 2, 3}) == 3
    assert cw(-1, [1, 2, 3], {2, 3}) == -1
    print("watermark ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        demo()
    else:
        main()
