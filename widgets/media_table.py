from __future__ import annotations

from textual.widgets import DataTable
from textual.reactive import reactive
from rich.text import Text

from scanner import MediaItem

SORT_CYCLES = [
    ("title", True),
    ("title", False),
    ("size", True),
    ("size", False),
    ("imdb", True),
    ("imdb", False),
    ("rt", True),
    ("rt", False),
]

FOLDER_COLORS = {
    "Movies": "default",
    "TV Shows": "cyan",
    "Anime & Animation": "magenta",
}


def _imdb_color(rating: float | None) -> str:
    if rating is None:
        return "dim"
    if rating >= 7.5:
        return "green"
    if rating >= 5.0:
        return "yellow"
    return "red"


def _rt_color(score: int | None) -> str:
    if score is None:
        return "dim"
    if score >= 70:
        return "green"
    if score >= 50:
        return "yellow"
    return "red"


def _fmt_imdb(rating: float | None, status: str) -> Text:
    if status == "fetching":
        return Text("…", style="dim")
    if status in ("not_found", "error"):
        return Text("N/A", style="dim")
    if rating is None:
        return Text("-", style="dim")
    return Text(f"{rating:.1f}", style=_imdb_color(rating))


def _fmt_rt(score: int | None, status: str) -> Text:
    if status == "fetching":
        return Text("…", style="dim")
    if status in ("not_found", "error"):
        return Text("N/A", style="dim")
    if score is None:
        return Text("-", style="dim")
    return Text(f"{score}%", style=_rt_color(score))


def _fmt_size(size_bytes: int) -> str:
    b = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


class MediaTable(DataTable):
    sort_index: reactive[int] = reactive(0, init=False)

    def __init__(self, items: list[MediaItem], **kwargs) -> None:
        super().__init__(cursor_type="row", **kwargs)
        self._items: list[MediaItem] = items
        self._row_keys: list[str] = []

    def on_mount(self) -> None:
        self.add_columns("", "Title", "Folder", "IMDb", "RT", "Size")
        self._populate()

    def _populate(self) -> None:
        self.clear()
        self._row_keys = []
        for item in self._items:
            key = str(item.path)
            self._row_keys.append(key)
            self.add_row(*self._row_cells(item), key=key)

    def _row_cells(self, item: MediaItem) -> tuple:
        sel = Text("◆", style="bold yellow") if item.selected else Text("·", style="dim")
        folder_color = FOLDER_COLORS.get(item.folder_label, "default")
        title_text = Text(item.display_title, style=folder_color)
        folder_text = Text(item.folder_label, style=folder_color)
        imdb_text = _fmt_imdb(item.imdb_rating, item.tmdb_status)
        rt_text = _fmt_rt(item.rt_score, item.tmdb_status)
        size_text = Text(_fmt_size(item.size_bytes), justify="right")
        return sel, title_text, folder_text, imdb_text, rt_text, size_text

    def refresh_item(self, item: MediaItem) -> None:
        key = str(item.path)
        if key not in self._row_keys:
            return
        idx = self._row_keys.index(key)
        for col_idx, cell in enumerate(self._row_cells(item)):
            self.update_cell_at((idx, col_idx), cell, update_width=False)

    def toggle_selected(self, row_index: int) -> None:
        if row_index < 0 or row_index >= len(self._items):
            return
        item = self._items[row_index]
        item.selected = not item.selected
        self.refresh_item(item)

    def select_all(self, value: bool = True) -> None:
        for item in self._items:
            item.selected = value
        self._populate()

    def cycle_sort(self) -> None:
        self.sort_index = (self.sort_index + 1) % len(SORT_CYCLES)
        field, ascending = SORT_CYCLES[self.sort_index]
        if field == "title":
            self._items.sort(key=lambda x: x.title.lower(), reverse=not ascending)
        elif field == "size":
            self._items.sort(key=lambda x: x.size_bytes, reverse=not ascending)
        elif field == "imdb":
            self._items.sort(
                key=lambda x: (x.imdb_rating is None, (x.imdb_rating or 0) if ascending else -(x.imdb_rating or 0)),
            )
        elif field == "rt":
            self._items.sort(
                key=lambda x: (x.rt_score is None, (x.rt_score or 0) if ascending else -(x.rt_score or 0)),
            )
        self._populate()

    def get_selected_items(self) -> list[MediaItem]:
        return [item for item in self._items if item.selected]

    def remove_items(self, paths: set[str]) -> None:
        self._items = [item for item in self._items if str(item.path) not in paths]
        self._populate()

    @property
    def all_items(self) -> list[MediaItem]:
        return self._items

    @property
    def current_sort_label(self) -> str:
        field, asc = SORT_CYCLES[self.sort_index]
        arrow = "↑" if asc else "↓"
        return f"{field}{arrow}"
