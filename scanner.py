from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from config import Config

YEAR_RE = re.compile(r"^(.+?)\s*\((\d{4})\)\s*$")


@dataclass
class MediaItem:
    title: str
    year: int | None
    media_type: str
    path: Path
    size_bytes: int
    folder_label: str

    tmdb_id: int | None = None
    imdb_id: str | None = None
    imdb_rating: float | None = None
    rt_score: int | None = None
    tmdb_title: str | None = None
    tmdb_status: str = "pending"
    selected: bool = False

    @property
    def display_title(self) -> str:
        if self.year:
            return f"{self.title} ({self.year})"
        return self.title

    @property
    def size_human(self) -> str:
        b = self.size_bytes
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} PB"


def _dir_size(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except PermissionError:
            pass
    return total


def _parse_name(dirname: str) -> tuple[str, int | None]:
    m = YEAR_RE.match(dirname)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return dirname.strip(), None


def _scan_title_dirs(
    folder_path: Path,
    media_type: str,
    folder_label: str,
    items: list[MediaItem],
) -> None:
    try:
        entries = sorted(os.scandir(folder_path), key=lambda e: e.name.lower())
    except PermissionError:
        return

    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        title, year = _parse_name(entry.name)
        if media_type == "movie" and year is None:
            # No year match — treat as a subcategory folder, recurse one level
            _scan_title_dirs(Path(entry.path), media_type, folder_label, items)
        else:
            size = _dir_size(Path(entry.path))
            items.append(MediaItem(
                title=title,
                year=year,
                media_type=media_type,
                path=Path(entry.path),
                size_bytes=size,
                folder_label=folder_label,
            ))


def scan(config: Config, selected_folders: list[str]) -> list[MediaItem]:
    items: list[MediaItem] = []
    for folder_label in selected_folders:
        folder_path = config.media_root / folder_label
        media_type = config.type_map.get(folder_label, "movie")
        if not folder_path.is_dir():
            continue
        _scan_title_dirs(folder_path, media_type, folder_label, items)
    return items
