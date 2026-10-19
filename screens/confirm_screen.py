from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static
from textual.binding import Binding
from rich.text import Text

from scanner import MediaItem


def _fmt_size(size_bytes: int) -> str:
    b = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _watch_label(item: MediaItem) -> str:
    """Short play history for the confirmation list.

    Shown per item so that something with watch history cannot be deleted
    without it being visible at the point of confirming.
    """
    stats = item.watch
    if stats is None or not stats.known:
        return "no watch data"
    if stats.parts_played == 0:
        return "never played"
    if stats.parts_total == 1:
        return f"played {stats.plays}x, last {stats.last_played_date}"
    return (
        f"{stats.parts_played}/{stats.parts_total} eps, {stats.plays} plays, "
        f"last {stats.last_played_date}"
    )


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #dialog {
        width: 110;
        max-width: 90%;
        height: auto;
        max-height: 30;
        border: round $error;
        padding: 1 2;
        background: $surface;
    }
    #title {
        text-align: center;
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }
    #items {
        height: auto;
        max-height: 12;
        overflow-y: auto;
    }
    .item-row {
        color: $text;
    }
    #total {
        margin-top: 1;
        text-style: bold;
        color: $warning;
    }
    #buttons {
        layout: horizontal;
        align: center middle;
        margin-top: 1;
        height: 3;
    }
    Button {
        margin: 0 1;
    }
    """

    ACTION_LABELS = {
        "nas_trash": "Move to NAS Trash",
        "local_trash": "Move to Trash",
        "permanent": "Delete Permanently",
    }

    def __init__(self, items: list[MediaItem], delete_mode: str) -> None:
        super().__init__()
        self._items = items
        self._delete_mode = delete_mode

    def compose(self) -> ComposeResult:
        total = sum(i.size_bytes for i in self._items)
        action_label = self.ACTION_LABELS.get(self._delete_mode, "Delete")

        with Static(id="dialog"):
            yield Label("Confirm Deletion", id="title")
            with Static(id="items"):
                for item in self._items:
                    row = Text()
                    row.append(f"  {item.display_title:<35}", style="default")
                    row.append(f"  {item.folder_label:<20}", style="dim")
                    row.append(f"  {_fmt_size(item.size_bytes):>9}", style="bold")
                    watch = _watch_label(item)
                    row.append(
                        f"  {watch}",
                        style="dim" if watch in ("never played", "no watch data") else "yellow",
                    )
                    yield Label(row, classes="item-row")
            if self._delete_mode == "nas_trash":
                freed_note = (
                    f"Total: {_fmt_size(total)} moved to the NAS trash "
                    "(no space freed until purged)"
                )
            else:
                freed_note = f"Total: {_fmt_size(total)} will be freed"
            yield Label(freed_note, id="total")
            # Deleting something people have actually watched is a different
            # decision from clearing out dead weight, so say it plainly here.
            played = [i for i in self._items if i.watch and i.watch.parts_played]
            if played:
                yield Label(
                    Text(
                        f"{len(played)} of these have been played"
                        + (f", most recently {max(i.watch.last_played_date for i in played)}"
                           if any(i.watch.last_played_ts for i in played) else "")
                        + ".",
                        style="bold yellow",
                    ),
                    id="watched-warning",
                )
            if self._delete_mode == "permanent":
                yield Label(
                    Text("This cannot be undone. Media is in no backup.", style="bold red"),
                    id="warning",
                )
            with Static(id="buttons"):
                yield Button(action_label, variant="error", id="confirm")
                yield Button("Cancel", variant="default", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)
