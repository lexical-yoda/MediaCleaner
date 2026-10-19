from __future__ import annotations

import time

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
    ("watched", True),
    ("watched", False),
    ("plays", True),
    ("plays", False),
    ("added", True),
    ("added", False),
]

RECENT_DAYS = 30

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


def _watched_color(stats) -> str:
    """Colour the watched fraction by how close to untouched the title is."""
    if stats.parts_played == 0:
        return "bold red"
    if stats.played_fraction <= 0.05:
        return "red"
    if stats.played_fraction < 0.5:
        return "yellow"
    return "green"


def _fmt_watched(item: MediaItem) -> Text:
    """How much of this title anyone has ever played.

    A movie reads as a tick or a cross; a series reads as "played / total"
    episodes, counting an episode as played if any user played it. The
    distinction between "nobody watched this" and "Jellyfin does not know about
    this" is deliberate — only the first is evidence for deleting something.
    """
    if item.watch_status == "pending":
        return Text("…", style="dim")
    if item.watch_status == "off":
        return Text("", style="dim")
    if item.watch_status == "error":
        return Text("?", style="dim")
    stats = item.watch
    if stats is None or not stats.known:
        return Text("—", style="dim")
    style = _watched_color(stats)
    if item.watch_status == "stale":
        style = f"{style} italic"
    if stats.parts_total == 1:
        return Text("✓" if stats.parts_played else "✗", style=style, justify="right")
    return Text(f"{stats.parts_played}/{stats.parts_total}", style=style, justify="right")


def _fmt_plays(item: MediaItem) -> Text:
    """Total play count across every user, with the number of users behind it."""
    if item.watch_status == "pending":
        return Text("…", style="dim")
    if item.watch_status == "off":
        return Text("", style="dim")
    if item.watch_status == "error":
        return Text("?", style="dim")
    stats = item.watch
    if stats is None or not stats.known:
        return Text("—", style="dim")
    if stats.plays == 0:
        return Text("0", style="bold red", justify="right")
    label = f"{stats.plays}" if stats.watchers <= 1 else f"{stats.plays} ({stats.watchers}u)"
    return Text(label, style="default", justify="right")


def _fmt_added(item: MediaItem) -> Text:
    """Date the title first appeared, with a marker when it is still growing.

    Recent arrivals are highlighted because an unwatched title that only landed
    days ago is not a deletion candidate yet. A trailing "+" means content was
    added more than 30 days after the title first appeared, which distinguishes
    a series still receiving episodes from one that has not changed since it
    was imported.
    """
    if item.added_ts is None:
        return Text("-", style="dim")
    label = item.added_date
    if item.is_still_growing:
        label += "+"
    age_days = (time.time() - item.added_ts) / 86400
    style = "green" if age_days <= RECENT_DAYS else "default"
    return Text(label, style=style)


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
        self.add_columns("", "Title", "Folder", "IMDb", "RT", "Watched", "Plays", "Size", "Added")
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
        watched_text = _fmt_watched(item)
        plays_text = _fmt_plays(item)
        size_text = Text(_fmt_size(item.size_bytes), justify="right")
        added_text = _fmt_added(item)
        return (
            sel, title_text, folder_text, imdb_text, rt_text,
            watched_text, plays_text, size_text, added_text,
        )

    def refresh_all(self) -> None:
        """Redraw every row, keeping the cursor where the user left it.

        Cheaper than calling refresh_item in a loop, which costs a linear key
        lookup per row. Used when something fills a column for the whole table
        at once, such as the watch-data worker answering.
        """
        cursor = self.cursor_coordinate
        self._populate()
        try:
            self.cursor_coordinate = cursor
        except Exception:
            # The table can be shorter than it was; falling back to the top is
            # better than losing the refresh.
            pass

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
        elif field == "watched":
            # Ascending puts the never-played titles at the top, which is the
            # reason this sort exists. Titles Jellyfin knows nothing about sort
            # last either way, like a missing rating: no watch data is not the
            # same as no plays.
            self._items.sort(
                key=lambda x: (
                    x.watch is None or not x.watch.known,
                    x.played_fraction if ascending else -x.played_fraction,
                ),
            )
        elif field == "plays":
            self._items.sort(
                key=lambda x: (
                    x.watch is None or not x.watch.known,
                    x.plays if ascending else -x.plays,
                ),
            )
        elif field == "added":
            self._items.sort(
                key=lambda x: (x.added_ts is None, (x.added_ts or 0) if ascending else -(x.added_ts or 0)),
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
