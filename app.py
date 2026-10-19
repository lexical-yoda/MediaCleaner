from __future__ import annotations

import logging

from textual.app import App, ComposeResult

from config import Config, load_config
from screens.folder_select import FolderSelectScreen
from screens.main_screen import MainScreen

log = logging.getLogger("app")


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
        log.info("Folders selected for scan: %s", selected_folders)
        self.push_screen(MainScreen(self._config, selected_folders))

    def _handle_exception(self, error: Exception) -> None:
        # Textual renders a fatal error to the terminal and exits, and the
        # traceback is gone once the terminal is cleared. Keep a copy.
        log.critical("Unhandled exception, app exiting", exc_info=error)
        super()._handle_exception(error)
