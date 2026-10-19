from __future__ import annotations

import asyncio
import logging
from collections import Counter

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, Static
from textual.binding import Binding
from textual import work
from rich.text import Text

from config import Config
from scanner import MediaItem, scan
from jellyfin import WatchDataUnavailable, fetch_watch_data, load_cache, save_cache
from tmdb import RatingsClient
from deleter import delete_items
from widgets.media_table import MediaTable
from screens.confirm_screen import ConfirmScreen

log = logging.getLogger("ui")


def _fmt_size(size_bytes: int) -> str:
    b = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


class MainScreen(Screen):
    BINDINGS = [
        Binding("space", "toggle_select", "Select", show=True),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("d", "delete_selected", "Delete", show=True),
        Binding("ctrl+a", "select_all", "All", show=True),
        Binding("ctrl+d", "deselect_all", "None", show=True),
        Binding("b", "go_back", "Back", show=True),
        Binding("q", "quit_app", "Quit", show=True),
    ]

    CSS = """
    MainScreen {
        layout: vertical;
    }
    #status-bar {
        height: 1;
        background: $boost;
        padding: 0 2;
        color: $text-muted;
    }
    #sort-label {
        dock: right;
        padding: 0 2;
        color: $accent;
    }
    MediaTable {
        height: 1fr;
    }
    """

    def __init__(self, config: Config, selected_folders: list[str]) -> None:
        super().__init__()
        self._config = config
        self._selected_folders = selected_folders
        self._items: list[MediaItem] = []
        self._scan_warnings: list[str] = []
        # Set once the watch-data worker has answered, so the status bar can
        # say where the numbers came from and how old they are.
        self._watch_note: str = ""
        # The exact items shown in the confirmation dialog. What was confirmed
        # is what gets deleted, independent of anything that happens to the
        # table's selection in between.
        self._pending_delete: list[MediaItem] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("Scanning…", id="status-bar")
        yield MediaTable([], id="media-table")
        yield Footer()

    def on_mount(self) -> None:
        try:
            self._items, self._scan_warnings = scan(self._config, self._selected_folders)
        except Exception as e:
            log.exception("Scan crashed")
            self._items, self._scan_warnings = [], [f"scan crashed: {type(e).__name__}: {e}"]
            self.notify(
                f"Scan failed: {type(e).__name__}: {e}. See mediacleaner.log.",
                severity="error", timeout=30,
            )
        table = self.query_one(MediaTable)
        table._items = self._items
        table._populate()
        self._update_status()
        if self._scan_warnings:
            self.notify(
                f"{len(self._scan_warnings)} scan warning(s): something may be missing or "
                f"under-sized. First: {self._scan_warnings[0]}. See mediacleaner.log.",
                severity="warning", timeout=20,
            )
        self._fetch_ratings()
        self._fetch_watch_data()

    def _update_status(self) -> None:
        table = self.query_one(MediaTable)
        items = table.all_items
        total_size = sum(i.size_bytes for i in items)
        selected_count = sum(1 for i in items if i.selected)
        sort_label = table.current_sort_label

        status = self.query_one("#status-bar", Static)
        text = Text()
        text.append(f" {len(items)} items", style="bold")
        text.append("  ·  ")
        text.append(_fmt_size(total_size), style="cyan")
        if selected_count:
            text.append("  ·  ")
            text.append(f"{selected_count} selected", style="bold yellow")
        never = [i for i in items if i.never_played]
        if never:
            text.append("  ·  ")
            text.append(
                f"{len(never)} never played ({_fmt_size(sum(i.size_bytes for i in never))})",
                style="bold red",
            )
        if self._scan_warnings:
            text.append("  ·  ")
            text.append(f"{len(self._scan_warnings)} scan warnings (see log)", style="bold red")
        if self._watch_note:
            text.append("  ·  ")
            text.append(self._watch_note, style="yellow")
        text.append(f"  [sort: {sort_label}]", style="dim")
        status.update(text)

    # Ratings are optional. exit_on_error=False keeps a failure here from
    # taking the app down; it is logged and the table keeps working.
    @work(exclusive=False, thread=False, exit_on_error=False)
    async def _fetch_ratings(self) -> None:
        log.info("Ratings fetch started for %d titles", len(self._items))
        try:
            async with RatingsClient(
                self._config.tmdb_api_key, self._config.omdb_api_key, self._config.cache_ttl_days
            ) as client:
                tasks = [self._fetch_one(client, item) for item in self._items]
                await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("Ratings fetch cancelled")
            raise
        except Exception:
            log.exception("Ratings fetch crashed")
            return
        log.info("Ratings fetch finished: %s", dict(Counter(i.tmdb_status for i in self._items)))

    async def _fetch_one(self, client: RatingsClient, item: MediaItem) -> None:
        item.tmdb_status = "fetching"
        try:
            tmdb_title, imdb_id, imdb_rating, rt_score = await client.fetch_ratings(
                item.title, item.year, item.media_type
            )
            item.tmdb_title = tmdb_title
            item.imdb_id = imdb_id
            item.imdb_rating = imdb_rating
            item.rt_score = rt_score
            item.tmdb_status = "found" if imdb_id else "not_found"
            log.debug("Ratings %s: %s imdb=%s rt=%s", item.tmdb_status, item.display_title, imdb_rating, rt_score)
        except Exception:
            log.exception("Ratings lookup crashed for %s", item.path)
            item.tmdb_status = "error"
        try:
            self._refresh_item(item)
        except Exception:
            # The screen may be gone (user went back) while lookups finish.
            log.debug("Could not refresh row for %s", item.path, exc_info=True)

    # Watch data is optional in the same way ratings are: the table is fully
    # usable without it, so a failure here is logged and reported, never fatal.
    @work(exclusive=False, thread=False, exit_on_error=False)
    async def _fetch_watch_data(self) -> None:
        if not self._config.watch_data:
            self._apply_watch({}, "off")
            return
        if not self._items:
            return
        log.info("Watch data fetch started for %d titles", len(self._items))
        try:
            stats, unmatched = await fetch_watch_data(
                self._config, [str(i.path) for i in self._items]
            )
        except asyncio.CancelledError:
            log.info("Watch data fetch cancelled")
            raise
        except WatchDataUnavailable as e:
            log.warning("Watch data unavailable: %s", e)
            self._fall_back_to_cached_watch(str(e))
            return
        except Exception as e:
            log.exception("Watch data fetch crashed")
            self._fall_back_to_cached_watch(f"{type(e).__name__}: {e}")
            return

        self._apply_watch(stats, "ok")
        save_cache(stats)
        matched = sum(1 for i in self._items if i.watch is not None)
        never = sum(1 for i in self._items if i.never_played)
        log.info(
            "Watch data applied: %d/%d titles matched, %d never played, "
            "%d playable files matched no row",
            matched, len(self._items), never, unmatched,
        )
        if unmatched:
            # Jellyfin and the scan disagree about what is on disk. Worth
            # surfacing: it usually means a library needs a rescan.
            self.notify(
                f"{unmatched} playable file(s) in Jellyfin matched no row. "
                "Jellyfin's library may be out of date. See mediacleaner.log.",
                severity="warning", timeout=15,
            )
        if matched < len(self._items):
            log.info(
                "Titles with no Jellyfin item: %s",
                [str(i.path) for i in self._items if i.watch is None][:20],
            )

    def _fall_back_to_cached_watch(self, reason: str) -> None:
        """Show the last saved watch data, clearly marked as stale."""
        cached, fetched_at = load_cache()
        if cached:
            self._apply_watch(cached, "stale")
            self._watch_note = f"watch data from cache ({fetched_at})"
            log.info("Watch data: fell back to cache from %s", fetched_at)
            self.notify(
                f"Could not read Jellyfin watch data ({reason}). Showing cached "
                f"data from {fetched_at}.",
                severity="warning", timeout=20,
            )
        else:
            self._apply_watch({}, "error")
            self._watch_note = "watch data unavailable"
            self.notify(
                f"Could not read Jellyfin watch data: {reason}. "
                "The Watched and Plays columns are empty. See mediacleaner.log.",
                severity="warning", timeout=20,
            )

    def _apply_watch(self, stats: dict, status: str) -> None:
        """Attach the stats to every item and redraw, whatever the outcome.

        Every item gets a status even when it has no entry, so the columns stop
        showing the pending marker for titles Jellyfin does not know about.
        """
        for item in self._items:
            found = stats.get(str(item.path))
            item.watch = found
            item.watch_status = status if found is not None else (
                "unknown" if status in ("ok", "stale") else status
            )
        try:
            self.query_one(MediaTable).refresh_all()
            self._update_status()
        except Exception:
            # The screen may be gone (user went back) before the read finishes.
            log.debug("Could not refresh the table with watch data", exc_info=True)

    def _refresh_item(self, item: MediaItem) -> None:
        table = self.query_one(MediaTable)
        table.refresh_item(item)
        self._update_status()

    def action_toggle_select(self) -> None:
        table = self.query_one(MediaTable)
        row = table.cursor_row
        table.toggle_selected(row)
        if 0 <= row < len(table.all_items):
            item = table.all_items[row]
            log.info("%s: %s", "Selected" if item.selected else "Deselected", item.path)
        self._update_status()

    def action_cycle_sort(self) -> None:
        table = self.query_one(MediaTable)
        table.cycle_sort()
        log.debug("Sort changed to %s", table.current_sort_label)
        self._update_status()

    def action_select_all(self) -> None:
        table = self.query_one(MediaTable)
        table.select_all(True)
        log.info("Selected all %d items", len(table.all_items))
        self._update_status()

    def action_deselect_all(self) -> None:
        table = self.query_one(MediaTable)
        table.select_all(False)
        log.info("Deselected all items")
        self._update_status()

    def action_delete_selected(self) -> None:
        table = self.query_one(MediaTable)
        selected = table.get_selected_items()
        if not selected:
            self.notify("No items selected.", severity="warning")
            return
        self._pending_delete = list(selected)
        log.info(
            "Delete dialog opened for %d item(s), %.1f GB: %s",
            len(selected), sum(i.size_bytes for i in selected) / 1024**3,
            [str(i.path) for i in selected],
        )
        self.app.push_screen(
            ConfirmScreen(selected, self._config.delete_mode),
            self._on_confirm,
        )

    def _on_confirm(self, confirmed: bool) -> None:
        selected, self._pending_delete = self._pending_delete, []
        if not confirmed:
            log.info("Delete cancelled by user")
            return
        log.info("Delete confirmed by user for %d item(s)", len(selected))
        table = self.query_one(MediaTable)
        try:
            results = delete_items([item.path for item in selected], self._config)
        except Exception as e:
            log.exception("Delete crashed")
            self.notify(
                f"Delete crashed, nothing removed from the table: {type(e).__name__}: {e}. "
                "See mediacleaner.log.",
                severity="error", timeout=30,
            )
            return

        # Only drop rows whose delete actually succeeded. The previous version
        # removed every selected row before looking at the errors, so a failed
        # delete left the item on disk while disappearing from the table.
        succeeded = {str(r.path) for r in results if r.ok}
        failed = [r for r in results if not r.ok]
        table.remove_items(succeeded)
        self._items = table.all_items
        self._update_status()

        if failed and succeeded:
            self.notify(
                f"{len(succeeded)} removed, {len(failed)} failed. "
                f"First error: {failed[0].error_line}. See mediacleaner.log.",
                severity="warning", timeout=30,
            )
        elif failed:
            self.notify(
                f"Nothing was deleted. {failed[0].error_line}"
                + (f" (+{len(failed) - 1} more)" if len(failed) > 1 else "")
                + " See mediacleaner.log.",
                severity="error", timeout=30,
            )
        else:
            verb = {"nas_trash": "Moved to NAS trash", "local_trash": "Trashed"}.get(
                self._config.delete_mode, "Permanently deleted"
            )
            self.notify(f"{verb}: {len(succeeded)} item(s).", severity="information")

    def action_go_back(self) -> None:
        log.info("Back to folder selector")
        self.app.pop_screen()

    def action_quit_app(self) -> None:
        log.info("Quit requested")
        self.app.exit()
