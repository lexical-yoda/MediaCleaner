from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, Label, ListView, ListItem, Static
from textual.binding import Binding
from rich.text import Text

from config import Config


class FolderSelectScreen(Screen):
    BINDINGS = [
        Binding("space", "toggle_item", "Toggle", show=True, priority=True),
        Binding("enter", "confirm", "Scan", show=True, priority=True),
        Binding("q", "quit_app", "Quit", show=True),
    ]

    CSS = """
    FolderSelectScreen {
        align: center middle;
    }
    #panel {
        width: 50;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
        color: $accent;
    }
    #hint {
        text-align: center;
        color: $text-muted;
        margin-top: 1;
    }
    ListView {
        height: auto;
        border: none;
        background: transparent;
    }
    ListItem {
        background: transparent;
        padding: 0 1;
    }
    ListItem:hover {
        background: $boost;
    }
    ListItem.--highlight {
        background: $boost;
    }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config
        self._selected: set[str] = set(config.known_folders)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Static(id="panel"):
            yield Label("MediaCleaner — Select folders to scan", id="title")
            items = []
            for folder in self._config.known_folders:
                checked = folder in self._selected
                label = Text()
                label.append("[x] " if checked else "[ ] ", style="bold yellow" if checked else "dim")
                label.append(folder)
                items.append(ListItem(Label(label), id=f"folder_{folder.replace(' ', '_').replace('&', 'and')}"))
            yield ListView(*items)
            yield Label("space: toggle  enter: scan  q: quit", id="hint")
        yield Footer()

    def action_toggle_item(self) -> None:
        lv = self.query_one(ListView)
        highlighted = lv.highlighted_child
        if highlighted is None:
            return
        folder = self._get_folder_from_item(highlighted)
        if folder is None:
            return
        if folder in self._selected:
            self._selected.discard(folder)
        else:
            self._selected.add(folder)
        self._refresh_list()

    def _get_folder_from_item(self, item: ListItem) -> str | None:
        for folder in self._config.known_folders:
            safe = f"folder_{folder.replace(' ', '_').replace('&', 'and')}"
            if item.id == safe:
                return folder
        return None

    def _refresh_list(self) -> None:
        lv = self.query_one(ListView)
        highlighted_idx = lv.index
        lv.clear()
        for folder in self._config.known_folders:
            checked = folder in self._selected
            label = Text()
            label.append("[x] " if checked else "[ ] ", style="bold yellow" if checked else "dim")
            label.append(folder)
            safe = f"folder_{folder.replace(' ', '_').replace('&', 'and')}"
            lv.append(ListItem(Label(label), id=safe))
        if highlighted_idx is not None:
            lv.index = highlighted_idx

    def action_confirm(self) -> None:
        selected = [f for f in self._config.known_folders if f in self._selected]
        if not selected:
            self.notify("Select at least one folder.", severity="warning")
            return
        self.dismiss(selected)

    def action_quit_app(self) -> None:
        self.app.exit()
