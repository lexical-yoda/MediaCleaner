from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config import Config
from jellyfin import WatchStats

log = logging.getLogger("scanner")

YEAR_RE = re.compile(r"^(.+?)\s*\((\d{4})\)\s*$")

# OS clutter, never media. Everything else found at title level becomes a row.
_JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini"}


def _is_junk(name: str) -> bool:
    lower = name.lower()
    return lower in _JUNK_FILES or lower.startswith("._")


@dataclass
class MediaItem:
    title: str
    year: int | None
    media_type: str
    path: Path
    size_bytes: int
    folder_label: str
    added_ts: float | None = None
    newest_ts: float | None = None

    tmdb_id: int | None = None
    imdb_id: str | None = None
    imdb_rating: float | None = None
    rt_score: int | None = None
    tmdb_title: str | None = None
    tmdb_status: str = "pending"
    selected: bool = False
    scan_errors: int = 0

    # Filled in by the watch-data worker from Jellyfin's database. "pending"
    # until it answers, then "ok", "unknown" for a title Jellyfin has no
    # playable item for, "stale" when it came from the cache rather than the
    # NAS, "off" when watch data is disabled, and "error" when the read failed.
    watch: WatchStats | None = None
    watch_status: str = "pending"

    @property
    def display_title(self) -> str:
        if self.year:
            return f"{self.title} ({self.year})"
        return self.title

    @property
    def added_date(self) -> str:
        """Date this title first appeared on disk, as YYYY-MM-DD."""
        if self.added_ts is None:
            return "-"
        return datetime.fromtimestamp(self.added_ts).strftime("%Y-%m-%d")

    @property
    def is_still_growing(self) -> bool:
        """True when content was added well after the title first appeared.

        Distinguishes a series that is still receiving episodes from one that
        landed in a single batch and has not changed since.
        """
        if self.added_ts is None or self.newest_ts is None:
            return False
        return (self.newest_ts - self.added_ts) > 30 * 86400

    @property
    def never_played(self) -> bool:
        """True only when Jellyfin knows this title and nobody has opened it.

        A title Jellyfin has no playable item for is not "never played", it is
        unknown, and must not be counted as a deletion candidate on that basis.
        """
        return self.watch is not None and self.watch.never_played

    @property
    def plays(self) -> int:
        return self.watch.plays if self.watch else 0

    @property
    def played_fraction(self) -> float:
        return self.watch.played_fraction if self.watch else 0.0

    @property
    def size_human(self) -> str:
        b = self.size_bytes
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} PB"


def _entry_added_ts(st: os.stat_result) -> float:
    """Best available "arrived on disk" timestamp.

    st_birthtime is the true creation time and is present on macOS and on SMB
    mounts that report it. Elsewhere fall back to st_mtime. Note that a file
    copied with its timestamps preserved (rsync -t, cp -p) carries the original
    mtime, so this reflects when the file was created rather than when it was
    moved into the library.
    """
    return getattr(st, "st_birthtime", None) or st.st_mtime


def _scan_tree(path: Path, warnings: list[str]) -> tuple[int, float | None, float | None, int]:
    """Return (total bytes, oldest added ts, newest added ts, error count).

    Any directory or file that cannot be read is logged and counted rather than
    silently skipped, since it means the size shown for the title is too low.
    """
    total = 0
    oldest: float | None = None
    newest: float | None = None
    errors = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError as e:
            errors += 1
            msg = f"cannot list {current}: {type(e).__name__}: {e}"
            log.warning(msg)
            warnings.append(msg)
            continue
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    total += st.st_size
                    ts = _entry_added_ts(st)
                    if oldest is None or ts < oldest:
                        oldest = ts
                    if newest is None or ts > newest:
                        newest = ts
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_symlink():
                    log.debug("not following symlink inside title: %s", entry.path)
            except OSError as e:
                errors += 1
                msg = f"cannot stat {entry.path}: {type(e).__name__}: {e}"
                log.warning(msg)
                warnings.append(msg)
    return total, oldest, newest, errors


def _parse_name(dirname: str) -> tuple[str, int | None]:
    m = YEAR_RE.match(dirname)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return dirname.strip(), None


def _is_category_dir(path: Path) -> bool:
    """True for a movie grouping folder such as "Hollywood & Western Movies".

    The earlier rule treated any folder without a "(YYYY)" suffix as a
    category, so a movie folder named "Dangal" or "La La Land (2016) [1080p]"
    was descended into, its video file ignored, and the movie never appeared in
    the table. Now a folder is a category when it holds a "Title (YYYY)"
    subfolder, which movie folders do not, or when it holds only subfolders.
    A folder with files of its own and no dated subfolders, or an empty one, is
    a title. The first test comes first so that a stray video dropped into a
    category does not turn the whole category into one deletable row.
    """
    has_subdir = False
    has_file = False
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    if YEAR_RE.match(entry.name):
                        return True
                    has_subdir = True
                elif entry.is_file(follow_symlinks=False) and not _is_junk(entry.name):
                    has_file = True
    except OSError as e:
        log.warning("cannot list %s to classify it (%s: %s); treating it as a title", path, type(e).__name__, e)
        return False
    return has_subdir and not has_file


def _scan_title_dirs(
    folder_path: Path,
    media_type: str,
    folder_label: str,
    items: list[MediaItem],
    warnings: list[str],
    depth: int = 0,
) -> None:
    try:
        with os.scandir(folder_path) as it:
            entries = sorted(it, key=lambda e: e.name.lower())
    except OSError as e:
        msg = f"cannot list {folder_path}: {type(e).__name__}: {e}"
        log.error(msg)
        warnings.append(msg)
        return

    for entry in entries:
        path = Path(entry.path)
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError as e:
            msg = f"cannot read {path}: {type(e).__name__}: {e}"
            log.warning(msg)
            warnings.append(msg)
            continue

        if entry.is_symlink():
            msg = f"skipped symlink (not shown, not deletable from here): {path}"
            log.warning(msg)
            warnings.append(msg)
            continue

        if is_file:
            if _is_junk(entry.name):
                log.debug("skipped OS clutter file: %s", path)
                continue
            # A video sitting loose in a library or category folder is still
            # media taking up space, so it gets its own row.
            title, year = _parse_name(path.stem)
            try:
                st = entry.stat(follow_symlinks=False)
                size, added_ts, errors = st.st_size, _entry_added_ts(st), 0
            except OSError as e:
                msg = f"cannot stat {path}: {type(e).__name__}: {e}"
                log.warning(msg)
                warnings.append(msg)
                size, added_ts, errors = 0, None, 1
            items.append(MediaItem(
                title=title, year=year, media_type=media_type, path=path,
                size_bytes=size, folder_label=folder_label,
                added_ts=added_ts, newest_ts=added_ts, scan_errors=errors,
            ))
            log.debug("loose file listed as its own title: %s", path)
            continue

        if not is_dir:
            msg = f"skipped entry that is neither file nor folder: {path}"
            log.warning(msg)
            warnings.append(msg)
            continue

        title, year = _parse_name(entry.name)
        # Categories only exist one level down from a movie library root.
        # Inside a category, every folder is a title.
        if media_type == "movie" and depth == 0 and year is None and _is_category_dir(path):
            log.info("Movie category folder: %s", path)
            _scan_title_dirs(path, media_type, folder_label, items, warnings, depth + 1)
            continue

        size, added_ts, newest_ts, errors = _scan_tree(path, warnings)
        items.append(MediaItem(
            title=title,
            year=year,
            media_type=media_type,
            path=path,
            size_bytes=size,
            folder_label=folder_label,
            added_ts=added_ts,
            newest_ts=newest_ts,
            scan_errors=errors,
        ))


def scan(config: Config, selected_folders: list[str]) -> tuple[list[MediaItem], list[str]]:
    """Return every title under the selected folders, plus any scan warnings.

    Every warning is also in mediacleaner.log. A non-empty warning list means
    something on disk may be missing from the table or under-sized in it.
    """
    items: list[MediaItem] = []
    warnings: list[str] = []
    for folder_label in selected_folders:
        folder_path = config.media_root / folder_label
        media_type = config.type_map.get(folder_label, "movie")
        if not folder_path.is_dir():
            msg = f"selected folder is not a directory (share unmounted?): {folder_path}"
            log.error(msg)
            warnings.append(msg)
            continue
        before = len(items)
        _scan_title_dirs(folder_path, media_type, folder_label, items, warnings)
        added = items[before:]
        log.info(
            "Scanned %s: %d titles, %.1f GB",
            folder_label, len(added), sum(i.size_bytes for i in added) / 1024**3,
        )
    log.info("Scan complete: %d titles, %d warnings", len(items), len(warnings))
    return items, warnings
