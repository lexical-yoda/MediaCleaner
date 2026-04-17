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


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #dialog {
        width: 60;
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

    def __init__(self, items: list[MediaItem], trash_mode: bool) -> None:
        super().__init__()
        self._items = items
        self._trash_mode = trash_mode

    def compose(self) -> ComposeResult:
        total = sum(i.size_bytes for i in self._items)
        action_label = "Move to Trash" if self._trash_mode else "Delete Permanently"

        with Static(id="dialog"):
            yield Label("Confirm Deletion", id="title")
            with Static(id="items"):
                for item in self._items:
                    row = Text()
                    row.append(f"  {item.display_title:<35}", style="default")
                    row.append(f"  {item.folder_label:<20}", style="dim")
                    row.append(f"  {_fmt_size(item.size_bytes):>9}", style="bold")
                    yield Label(row, classes="item-row")
            yield Label(f"Total: {_fmt_size(total)} will be freed", id="total")
            with Static(id="buttons"):
                yield Button(action_label, variant="error", id="confirm")
                yield Button("Cancel", variant="default", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)
