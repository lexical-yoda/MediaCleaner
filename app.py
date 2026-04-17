from __future__ import annotations

from textual.app import App, ComposeResult

from config import Config, load_config
from screens.folder_select import FolderSelectScreen
from screens.main_screen import MainScreen


class MediaCleanerApp(App):
    CSS = """
    App {
        background: $background;
    }
    """

    TITLE = "MediaCleaner"

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config

    def on_mount(self) -> None:
        self.push_screen(FolderSelectScreen(self._config), self._on_folders_selected)

    def _on_folders_selected(self, selected_folders: list[str]) -> None:
        self.push_screen(MainScreen(self._config, selected_folders))
