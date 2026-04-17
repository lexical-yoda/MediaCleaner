from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, Static
from textual.binding import Binding
from textual import work
from rich.text import Text

from config import Config
from scanner import MediaItem, scan
from tmdb import RatingsClient
from deleter import delete_items
from widgets.media_table import MediaTable
from screens.confirm_screen import ConfirmScreen


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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("Scanning…", id="status-bar")
        yield MediaTable([], id="media-table")
        yield Footer()

    def on_mount(self) -> None:
        self._items = scan(self._config, self._selected_folders)
        table = self.query_one(MediaTable)
        table._items = self._items
        table._populate()
        self._update_status()
        self._fetch_ratings()

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
        text.append(f"  [sort: {sort_label}]", style="dim")
        status.update(text)

    @work(exclusive=False, thread=False)
    async def _fetch_ratings(self) -> None:
        async with RatingsClient(
            self._config.tmdb_api_key, self._config.omdb_api_key, self._config.cache_ttl_days
        ) as client:
            tasks = [self._fetch_one(client, item) for item in self._items]
            await asyncio.gather(*tasks)

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
        except Exception:
            item.tmdb_status = "error"
        self._refresh_item(item)

    def _refresh_item(self, item: MediaItem) -> None:
        table = self.query_one(MediaTable)
        table.refresh_item(item)
        self._update_status()

    def action_toggle_select(self) -> None:
        table = self.query_one(MediaTable)
        row = table.cursor_row
        table.toggle_selected(row)
        self._update_status()

    def action_cycle_sort(self) -> None:
        table = self.query_one(MediaTable)
        table.cycle_sort()
        self._update_status()

    def action_select_all(self) -> None:
        table = self.query_one(MediaTable)
        table.select_all(True)
        self._update_status()

    def action_deselect_all(self) -> None:
        table = self.query_one(MediaTable)
        table.select_all(False)
        self._update_status()

    def action_delete_selected(self) -> None:
        table = self.query_one(MediaTable)
        selected = table.get_selected_items()
        if not selected:
            self.notify("No items selected.", severity="warning")
            return
        self.app.push_screen(
            ConfirmScreen(selected, self._config.trash_mode),
            self._on_confirm,
        )

    def _on_confirm(self, confirmed: bool) -> None:
        if not confirmed:
            return
        table = self.query_one(MediaTable)
        selected = table.get_selected_items()
        paths = [item.path for item in selected]
        errors = delete_items(paths, self._config.trash_mode)
        removed_paths = {str(item.path) for item in selected}
        table.remove_items(removed_paths)
        self._items = table.all_items
        self._update_status()
        if errors:
            self.notify(f"Errors: {'; '.join(errors)}", severity="error", timeout=8)
        else:
            action = "Trashed" if self._config.trash_mode else "Deleted"
            self.notify(f"{action} {len(paths)} item(s).", severity="information")

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_quit_app(self) -> None:
        self.app.exit()
