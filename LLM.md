# MediaCleaner — Technical Reference for Developers and LLMs

This document describes the full architecture, data flow, design decisions, and non-obvious implementation details of MediaCleaner. It is intended for developers modifying the codebase or LLMs being asked to extend it.

---

## Project Overview

MediaCleaner is a single-user terminal UI application (TUI) that scans a local Jellyfin media library, fetches ratings from external APIs, and allows bulk deletion of selected titles. It has no database, no server, and no authentication. Everything runs locally.

**Stack:** Python 3.10, Textual (TUI), httpx (async HTTP), send2trash, python-dotenv.

---

## File Map

```
MediaCleaner/
├── main.py                    # Entry point: sets up logging, loads config, runs the Textual app
├── applog.py                  # Logging to mediacleaner.log, API-key masking, uncaught-exception hooks
├── config.py                  # Config loading, validation, dataclass
├── scanner.py                 # Filesystem scan → list[MediaItem]; recursive size + added-date calc
├── tmdb.py                    # RatingsClient: TMDB (search/IMDb ID) + OMDB (ratings)
├── jellyfin.py                # Watch data: read-only sqlite over SSH → WatchStats per title
├── deleter.py                 # Deletion: NAS trash over SSH, local trash, or permanent + audit log
├── app.py                     # Textual App subclass: screen orchestration
├── screens/
│   ├── folder_select.py       # Screen 1: choose which folders to scan
│   ├── main_screen.py         # Screen 2: main table, sorting, TMDB worker
│   └── confirm_screen.py      # Modal: deletion confirmation with size summary
├── widgets/
│   └── media_table.py         # Custom DataTable subclass with multi-select + sort
├── config.json                # Runtime config (gitignored)
├── cache.json                 # TMDB/OMDB response cache (auto-generated, gitignored)
├── watch_cache.json           # Last good watch-data read, used offline (auto-generated, gitignored)
├── deleted.log                # Append-only audit log of deletions (gitignored)
├── mediacleaner.log           # Everything the app does, rotated 5 MB x 5 (gitignored)
└── requirements.txt
```

---

## Data Flow

```
main.py
  └─ load_config()                         # validates config.json / env vars
       └─ MediaCleanerApp.run()
            └─ FolderSelectScreen          # user picks folders
                 └─ dismiss(selected_folders: list[str])
                      └─ MainScreen(config, selected_folders)
                           ├─ on_mount:
                           │    scan(config, selected_folders)  → (list[MediaItem], warnings)
                           │    MediaTable._populate()           → rows in table
                           │    _fetch_ratings() [Worker]        → fills ratings async
                           │         RatingsClient.fetch_ratings()
                           │              TMDB search → imdb_id
                           │              OMDB lookup → imdb_rating, rt_score
                           │         _refresh_item() → MediaTable.refresh_item()
                           │    _fetch_watch_data() [Worker]      → fills watch columns
                           │         jellyfin.fetch_watch_data()
                           │              ssh sqlite3 -readonly jellyfin.db
                           │              parse_watch_output() → {path: WatchStats}
                           │         _apply_watch() → MediaTable.refresh_all()
                           │         on failure: load_cache() → status "stale"
                           └─ on 'd':
                                ConfirmScreen (modal)
                                     └─ dismiss(True)
                                          └─ delete_items(paths, config) → list[DeleteResult]
                                               → deleted.log + mediacleaner.log
                                               → only rows with ok=True leave the table
```

---

## Module Details

### `config.py`

**`Config` dataclass fields:**
- `media_root: Path` — root of the Jellyfin library
- `tmdb_api_key: str` — TMDB v3 API key
- `omdb_api_key: str` — OMDB API key
- `type_map: dict[str, str]` — maps folder name → media type (`"movie"`, `"tv"`, `"anime"`)
- `delete_mode: str` — `"nas_trash"`, `"local_trash"` or `"permanent"` (the old boolean `trash_mode` key is still read when `delete_mode` is absent; `Config.trash_mode` is now a read-only property)
- `cache_ttl_days: int` — how long before a cache entry is considered stale
- `nas_ssh`, `local_mount_root`, `nas_media_root`, `nas_trash_dir: str` — NAS trash settings
- `watch_data: bool` — whether to read watch history from Jellyfin at all
- `jellyfin_ssh: str` — host holding `jellyfin.db`; falls back to `nas_ssh` when empty
- `jellyfin_db_path: str` — path to `jellyfin.db` on that host (not the in-container path)
- `jellyfin_container_root: str` — what `local_mount_root` is called inside the Jellyfin container, so stored paths can be rewritten to local ones
- `known_folders: list[str]` — subset of `type_map` keys that actually exist on disk

**Loading priority:** `TMDB_API_KEY` / `OMDB_API_KEY` / `MEDIA_ROOT` env vars override `config.json` values. This allows keeping the API key in `.env` while the rest lives in `config.json`.

**First-run behavior:** If `config.json` does not exist, a template is written and the process exits with instructions. This prevents the app from running without valid config.

**Validation exits:** Empty `media_root`, missing API keys, invalid `type_map` values (`"movie"` / `"tv"` / `"anime"` are the only valid values), an unknown `delete_mode`, or `nas_trash` without its four settings all cause `sys.exit(1)` with a clear error message before the TUI launches. Errors are printed and logged.

**Watch data does not exit.** It is optional in the same way ratings are, so a missing
`jellyfin_ssh`, `jellyfin_db_path`, `jellyfin_container_root` or `local_mount_root` turns
`watch_data` off with a printed warning rather than stopping the tool.

**Secrets:** both API keys are registered with `applog.register_secret`, which masks them in every log line, tracebacks included.

**`known_folders`** is computed at load time by checking which `type_map` keys correspond to directories that actually exist under `media_root`. This is what the folder selector screen shows.

---

### `scanner.py`

**`MediaItem` dataclass:**

```python
@dataclass
class MediaItem:
    # Set by scanner
    title: str           # parsed from dirname, e.g. "Inception"
    year: int | None     # parsed from "(2010)" suffix; None if not present
    media_type: str      # "movie" | "tv" | "anime"
    path: Path           # absolute path to the title root directory
    size_bytes: int      # recursive sum of all file sizes under path
    added_ts: float      # oldest file creation time under path ("when it arrived")
    newest_ts: float     # newest file creation time under path (drives is_still_growing)
    folder_label: str    # top-level folder name, e.g. "Movies"

    # Set async by RatingsClient worker
    tmdb_id: int | None = None
    imdb_id: str | None = None      # e.g. "tt1375666"
    imdb_rating: float | None = None
    rt_score: int | None = None     # integer 0–100
    tmdb_title: str | None = None   # canonical TMDB title (useful to spot mismatches)
    tmdb_status: str = "pending"    # "pending" | "fetching" | "found" | "not_found" | "error"

    # Set in one go by the watch-data worker
    watch: WatchStats | None = None
    watch_status: str = "pending"   # "pending" | "ok" | "stale" | "unknown" | "off" | "error"

    selected: bool = False
```

`watch_status` distinguishes five outcomes that must not be conflated: `ok` (read from the NAS),
`stale` (from `watch_cache.json` because the NAS was unreachable), `unknown` (the read worked but
Jellyfin has no playable item under this path), `off` (`watch_data: false`) and `error` (no read
and no cache). Only `ok` and `stale` with `watch.parts_played == 0` mean "never played", which is
what `MediaItem.never_played` encodes.

**`_scan_tree(path, warnings)`** uses an explicit stack instead of `os.walk` to avoid deep recursion. It uses `os.scandir` for performance. Symlinks are not followed (`follow_symlinks=False`). It returns `(total_bytes, oldest_ts, newest_ts, error_count)` in a single pass, so the added-date columns cost no extra filesystem traversal. Any `OSError` (not just `PermissionError`) on a directory or file is logged, appended to `warnings`, and counted in `MediaItem.scan_errors`; nothing is skipped silently. An SMB timeout (`Errno 60`) used to crash the scan.

**Timestamps** come from `_entry_added_ts(st)`, which prefers `st_birthtime` (true creation time, present on macOS and on SMB mounts that report it) and falls back to `st_mtime`. Verified working over the `nas-share` SMB mount. Caveat worth remembering: files copied with timestamps preserved carry the original times, so the date is "when the file was created", not "when it was imported".

**`MediaItem.added_date`** formats `added_ts` as `YYYY-MM-DD`, and **`MediaItem.is_still_growing`** is true when `newest_ts` is more than 30 days after `added_ts`, which separates an actively-updating series from a one-off batch import.

**`_parse_name(dirname)`** applies `^(.+?)\s*\((\d{4})\)\s*$` to extract title and year from a directory name. If no year is present, year is `None` and the full dirname is used as the title. This handles `"Inception (2010)"` → `("Inception", 2010)` and `"Breaking Bad"` → `("Breaking Bad", None)`.

**`_scan_title_dirs(folder_path, media_type, folder_label, items, warnings, depth)`** is the scanner. Every non-junk entry it sees becomes exactly one row; only OS clutter (`.DS_Store`, `._*`, `Thumbs.db`, `desktop.ini`) is skipped, and symlinks are skipped with a warning.

- **Loose files** at title level become their own row (title parsed from the file stem).
- For `media_type == "movie"` at `depth == 0` only: a yearless folder is a category (recursed into once) when `_is_category_dir` says so: it contains a `Title (YYYY)` subfolder, or it contains only subfolders. A folder with files of its own and no dated subfolders, or an empty folder, is a title. The old rule ("no year means category") hid every yearless movie such as `Dangal/` or `La La Land (2016) [1080p]/` — 10 movies, 26.8 GB on the real library. The dated-subfolder test comes first so a stray video dropped into a category cannot turn the whole category into one deletable row. Inside a category every folder is a title (no deeper recursion). This handles the actual library structure:
  ```
  Movies/
    Hollywood & Western Movies/
      Inception (2010)/
    Bollywood & Regional Movies/
      3 Idiots (2009)/
  ```
- For `tv` / `anime`: every subdirectory is unconditionally treated as a show title, regardless of whether it has a year in the name. The recursive subcategory logic is intentionally disabled for these types.

**Why not use the year pattern for TV?** TV show directories typically don't have years (e.g., `Breaking Bad/`), so a year-absence check would incorrectly treat every show as a subcategory folder.

**Size is always the full show/movie total.** For TV shows, `_scan_tree` is called on the show root (e.g., `/TV Shows/Breaking Bad/`), so it sums across all Season subdirectories. This works identically whether episodes are in Season folders or directly in the show directory.

**`scan(config, folders)`** returns `(items, warnings)`. Warnings are shown as a count in the status bar and a notification; each one is also in `mediacleaner.log`.

---

### `tmdb.py`

**Architecture:** `RatingsClient` is an async context manager wrapping a single `httpx.AsyncClient`. It is used inside a `@work` async worker in `MainScreen`. The cache is loaded once on construction and saved to disk on `__aexit__`.

**Two-API pipeline per title:**

1. **TMDB search** → get TMDB ID
2. **TMDB detail / external_ids** → get IMDb ID
3. **OMDB** with IMDb ID → get `imdbRating` + Rotten Tomatoes from `Ratings[]`

This two-step approach is used because:
- TMDB has the best search engine for international/anime titles
- OMDB provides IMDb rating + RT score in a single request when queried by IMDb ID
- TMDB's own `vote_average` was intentionally not used (user requirement)

**Rate limiting:**
- TMDB: `Semaphore(8)` + 120ms sleep per request. TMDB's official limit is 40 req/10s; semaphore(8) keeps well below that across concurrent coroutines.
- OMDB: `Semaphore(5)` + 200ms sleep per request. OMDB free tier is 1,000/day with no explicit rate but conservative concurrency avoids 503s.
- Both semaphores are module-level globals initialized lazily on first use (not in `__init__`) to ensure they are created on the correct running event loop.
- On TMDB 429: reads `Retry-After` header, sleeps for that duration + 1s, then retries once.
- Every failed TMDB/OMDB request, and every OMDB `Response: False` (including `Request limit reached!`), is logged as a warning. `httpx`'s own request logging is held at WARNING because it prints the full URL, API key included.

**Cache key format:** `v2:{media_type}:{title_lower}:{year}` — the `v2:` prefix was added when the schema changed (old cache entries used `v1` keys implicitly through the old `search:` prefix). This means old cached entries are simply ignored rather than causing key conflicts.

**Cache entry format:**
```json
{
  "tmdb_title": "Inception",
  "imdb_id": "tt1375666",
  "imdb_rating": 8.8,
  "rt_score": 87,
  "fetched_at": "2026-04-17T16:30:00.123456"
}
```
`None` values are stored explicitly. A `None` IMDb ID means TMDB found no match; this is cached to avoid re-querying on every run.

**Movie search strategy (`_movie_imdb_id`):**
1. Search TMDB with `primary_release_year` if year is available
2. If no results, retry without the year (handles minor year mismatches in filenames)
3. `_pick_movie_result` iterates the top 5 results, skips entries with `vote_count < 5`, and checks that `abs(release_year - folder_year) <= 1` (allows ±1 year for release vs folder naming discrepancy)
4. Falls back to `results[0]` if no vote-filtered match is found

**TV/anime search strategy (`_tv_imdb_id`):**
1. Search `/search/tv` with title
2. Take `results[0]` (TMDB's relevance ranking is reliable for exact show names)
3. Fetch `/tv/{tmdb_id}/external_ids` to get the IMDb ID (not included in search results for TV; movies include it in the detail endpoint `/movie/{id}`)

**OMDB ratings extraction (`_get_omdb_ratings`):**
- `imdbRating` field → float (e.g., `"8.8"` → `8.8`); `"N/A"` is treated as `None`
- Rotten Tomatoes score: iterates `Ratings[]` array looking for `Source == "Rotten Tomatoes"`, then strips `%` and parses as int. This field is not always present (depends on OMDB data coverage).
- The `tomatoes=true` query parameter is passed to request the extended Rotten Tomatoes data from OMDB.

---

### `jellyfin.py`

Watch history, read out of Jellyfin's own database. Deliberately **not** the Jellyfin HTTP API:
the tool's premise is that it works when Jellyfin is down, and SSH to the NAS is already a
dependency of `nas_trash`. The database is opened `-readonly` and never written.

**Entry point:** `await fetch_watch_data(config, item_paths) -> (dict[str, WatchStats], unmatched)`.
Raises `WatchDataUnavailable` on any SSH or sqlite failure; the caller falls back to
`load_cache()`.

**`WatchStats`** per title: `parts_total` (playable items Jellyfin knows beneath it, 1 for a
movie, N episodes for a series), `parts_played` (of those, played by at least one user), `plays`
(total play count over every user), `watchers` (distinct users), `last_played_ts`. The derived
`never_played` is true only when `parts_total > 0 and parts_played == 0`, so a title Jellyfin
knows nothing about is never reported as unwatched.

**`WATCH_SQL`** is one statement returning two line kinds, so a single round trip answers
everything: `L <path>` for each playable item on disk, and `A <path> <user> <plays> <last played>`
for each (item, user) with actual activity. Measured on the real library: 42,817 lines, 6.3 MB,
0.8 s end to end.

Three things it has to get right, each of which silently corrupts the numbers otherwise:

1. **`UserData` holds one row per data key, not per item.** Jellyfin writes the same state under
   the item's GUID, its TMDB id and its IMDb id, so most (item, user) pairs have 2–3 identical
   rows. The `ud` CTE takes `MAX(PlayCount)`, `MAX(Played)`, `MAX(LastPlayedDate)` per
   (ItemId, UserId) *before* anything is summed. Verified on the real library: summing raw rows
   gives 10,441 movie plays against a correct 5,149. There is also a `PLACEHOLDER`-typed item
   holding orphaned user data (3,468 rows, one pair with 1,734 of them); the join to `leaf`
   excludes it.
2. **`IsVirtualItem = 0`.** Jellyfin lists episodes it knows from metadata but has no file for
   (2,415 of 17,346). Counting them would make a complete series read as part-missing.
3. **Unicode form.** Jellyfin stores NFC (what the NAS has), macOS lists SMB names as NFD. Both
   sides are normalised to NFC in `parse_watch_output`, the same fix `deleter.py` needed.

**`parse_watch_output(output, item_paths, container_root, local_root)`** does the fold. Paths in
the database are container paths (`/media/media/X`), rewritten to local (`/Volumes/nas-share/media/X`)
by prefix swap. Each playable file is then attributed to **the nearest ancestor directory that is
one of `item_paths`**. Walking up from the file is what makes this independent of library layout:
a movie file two levels under its category, an episode under `Season 01` and an episode loose in
the series folder all land on the right row with nothing about the layout written down. Resolved
parents are memoised, which matters at 16,000 files.

It returns the count of playable files that matched no row. On the real library that is **0**; a
non-zero value means Jellyfin and the scan disagree about what is on disk, which is surfaced as a
notification because it usually means a library needs a rescan.

**Validated against the real database on 2026-10-19:** 1,540 titles, all matched, 0 unmatched
files, 203 never played. Reproduces the vault runbook's numbers exactly — Gotham 0/99, Family Guy
12/457, Gintama 2/366, Hirayasumi 1/20 — and matches `Amélie (2001)` through an NFD lookup.

**Caching.** `save_cache` / `load_cache` keep the last good read in `watch_cache.json`, keyed by
local path. It is a fallback for an unreachable NAS, not a TTL cache: a reachable NAS is always
re-read, because the data is cheap and changes daily.

**`playback_reporting.db` was considered and not used.** It holds per-session rows with watch
duration, but it is bounded by its retention window and its `PlayDuration` column has junk values
on some rows. `UserData` is the lifetime record, which is what "has anyone ever played this" needs.

---

### `widgets/media_table.py`

**`MediaTable` extends `DataTable`.**

**Critical naming issue (already fixed):** `DataTable` has its own `refresh_row(row_index: int)` method. Our method for updating a row given a `MediaItem` is named `refresh_item(item: MediaItem)` to avoid overriding the parent. Setting `cursor_type` in the constructor (`super().__init__(cursor_type="row", ...)`) rather than in `on_mount` avoids triggering `watch_cursor_type` before the table has rows.

**Row identity:** Rows are keyed by `str(item.path)`. This is used both as the DataTable row key and as the index into `self._row_keys: list[str]`. The `_row_keys` list maintains the current display order and is rebuilt by `_populate()` on every sort.

**`refresh_item(item)`:** Finds the item's current row index via `_row_keys.index(key)` and calls `update_cell_at((row, col), value)` for each of the 6 columns. This is called from the async TMDB worker to update ratings in-place without re-populating the entire table.

**Watch columns.** `Watched` and `Plays` sit between `RT` and `Size`. `_fmt_watched` renders a
movie as `✓`/`✗` and a series as `played/total`, coloured red (nothing) → red (≤5%) → yellow
(<50%) → green, and italic when the data came from the cache. `—` means Jellyfin knows no playable
item under that path, and is deliberately distinct from "never played": only the latter is
evidence for deleting something. `_fmt_plays` shows the total with the distinct user count in
brackets when more than one user played it.

**`refresh_all()`** repopulates the whole table while preserving `cursor_coordinate`. The watch
worker fills every row at once, and calling `refresh_item` in a loop would cost a linear
`_row_keys.index` per row — quadratic at 1,540 rows.

**Sort cycles:** `SORT_CYCLES` is a list of `(field, ascending)` tuples. `cycle_sort()` increments the index mod len and re-sorts `self._items` in-place then calls `_populate()`. The sort key for ratings always puts `None` values at the end:
```python
key=lambda x: (x.imdb_rating is None, (x.imdb_rating or 0) if ascending else -(x.imdb_rating or 0))
```
The `(is_none, value)` tuple ensures `None` sorts last regardless of direction. `watched` and
`plays` use the same shape, keyed on `watch is None or not watch.known`, so titles Jellyfin has no
data for sort last in both directions rather than masquerading as zero-play. `watched↑` is the
sort that puts the never-played titles on top, which is the reason it exists.

**Color coding:**
- Folder column: Movies = default, TV Shows = cyan, Anime & Animation = magenta
- IMDb rating: ≥7.5 green, ≥5.0 yellow, <5.0 red
- RT score: ≥70 green, ≥50 yellow, <50 red
- Pending/fetching: dim `…`; not found / error: dim `N/A`

---

### `screens/folder_select.py`

**`FolderSelectScreen`** is a `Screen` (not modal) that wraps a `ListView`. It shows only folders from `config.known_folders` (i.e., folders that actually exist on disk).

**Key handling:** `space` and `enter` bindings are declared with `priority=True`. This is necessary because `ListView` is a focusable widget that would otherwise consume both keys before the screen's BINDINGS handler fires. `priority=True` inverts the handler order so the screen catches them first.

**Toggle state** is held in `self._selected: set[str]`. On toggle, `_refresh_list()` rebuilds the `ListView` contents while preserving the highlighted index (`lv.index`).

**Dismissal:** `dismiss(selected_folders: list[str])` passes the chosen folder names back to `MediaCleanerApp._on_folders_selected`, which then pushes `MainScreen`.

---

### `screens/main_screen.py`

**`MainScreen`** is the primary working screen.

**`on_mount` sequence:**
1. `scan()` runs synchronously — filesystem scan including recursive size calculation. For large libraries (1000+ items) this can take a few seconds. It blocks the event loop briefly but is acceptable since it only runs once per session and no UI exists to block yet.
2. Injects the items into the already-mounted (but empty) `MediaTable` by directly setting `table._items` and calling `table._populate()`.
3. Kicks off `_fetch_ratings()` as a `@work` async worker.

Scan exceptions are caught and logged; the screen still opens (empty) with an error notification rather than crashing the app.

**`_fetch_ratings` worker:** `@work(exclusive=False, thread=False, exit_on_error=False)` creates an asyncio coroutine worker (not a thread). Since it runs on the same event loop as the UI, calling UI methods directly from within it (like `_refresh_item`) is safe — no `call_from_thread` needed. `asyncio.gather(*tasks)` runs all per-item fetches concurrently, limited by the semaphores in `RatingsClient`. Ratings are optional: `exit_on_error=False` plus a logging `try/except` means a ratings failure can never close the app, and the finishing status counts are logged.

**`_fetch_watch_data` worker:** same contract as the ratings worker — `exit_on_error=False`, every
failure logged, never fatal. It is an async worker rather than a thread one because the SSH call
goes through `asyncio.create_subprocess_exec`, which keeps it on the UI event loop and avoids
`call_from_thread`. On `WatchDataUnavailable` it calls `_fall_back_to_cached_watch`, which shows
`watch_cache.json` marked `stale` with its date in the status bar, or leaves the columns empty
when there is no cache either. `_apply_watch` stamps a status on **every** item, including those
with no entry, so no row is left showing the pending marker.

**The status bar** gains a never-played count and the disk they come to, which is the headline
number for a reclaim pass.

**Every user action is logged** (`ui` logger): folder choice, each select/deselect with its path, select all/none, sort, delete dialog opened (with paths and size), confirmed or cancelled, back, quit.

**`_on_confirm`:** Deletes exactly the items that were shown in the confirmation dialog (captured in `self._pending_delete` when the dialog opened), not a fresh read of the table selection. It calls `delete_items`, then removes **only the paths whose result has `ok=True`** via `MediaTable.remove_items`. The local `self._items` reference is also updated to stay in sync. Failures are shown as a notification pointing at `mediacleaner.log`; if `delete_items` itself raises, nothing is removed from the table.

---

### `screens/confirm_screen.py`

**`ConfirmScreen`** is a `ModalScreen[bool]` — the generic parameter is the dismiss value type. `dismiss(True)` confirms, `dismiss(False)` cancels. The caller receives the value in its callback (`_on_confirm`).

The confirmation list is scrollable (`max-height: 12; overflow-y: auto`) to handle large selections. Total freed space is computed as `sum(i.size_bytes for i in items)` and displayed in human-readable form.

Each row carries its play history (`4/327 eps, 12 plays, last 2026-03-21`, or `never played`, or
`no watch data`), and a bold line below the total says how many of the selected items have been
played and when, so watched content cannot be deleted without that being visible at the moment of
confirming. The dialog is 110 columns wide to fit it.

The action button label follows `delete_mode`: `"Move to NAS Trash"`, `"Move to Trash"` or `"Delete Permanently"`. In `nas_trash` mode the total line says no space is freed until the trash is purged; in `permanent` mode a red warning says the media is in no backup.

---

### `deleter.py`

Entry point `delete_items(paths, config) -> list[DeleteResult]`, one result per path, in order. `DeleteResult(path, ok, mode, detail)`; callers must check `ok`.

- **Guard (`_refuse_reason`)**, all modes: a path must be at least `<library folder>/<title>` below `media_root`. The media root and library folders themselves are refused.
- **`nas_trash`**: one SSH call runs `_NAS_SCRIPT` under `python3` on the NAS. The payload (JSON, ASCII-escaped) carries `nas_media_root`, the stamped trash dir, and each item's path relative to the share (`nas_relative_path`). Per item the script: finds the source by walking path components, matching by NFC form (macOS shows SMB names as NFD, the NAS stores NFC; verified that a raw NFD lookup fails on this NAS); renames it to `<trash>/<stamp>/<same relative path>`; then verifies source gone and destination present. It prints one JSON line per item keyed by index, so output parsing does not depend on path text. `rename` fails with `EXDEV` rather than copying if the trash is on another dataset. SSH failure, a missing result, or unparseable output all count as failure.
- **`local_trash`**: `send2trash`. Fails on SMB volumes.
- **`permanent`**: `shutil.rmtree` / `unlink`, then checks the path is actually gone.
- A backend that raises is caught; every path it was given is reported as failed.
- Every attempt goes to `deleted.log` (`{ts} {MODE} OK {path} -> {trash path}` or `{ts} {MODE} FAIL {path} :: {reason}`) and to `mediacleaner.log`, along with the SSH exit code and stderr.

---

### `applog.py`

`setup_logging()` (called first in `main.py`) attaches a rotating file handler (`mediacleaner.log`, 5 MB x 5) to the root logger at DEBUG, installs `sys.excepthook` and `threading.excepthook` to log uncaught exceptions, and holds `httpx`/`httpcore`/`asyncio` at WARNING. The formatter masks every value passed to `register_secret` (the API keys) in the fully formatted record, tracebacks included. `MediaCleanerApp._handle_exception` logs any exception Textual catches before it exits the app.

---

### `app.py`

Minimal `App` subclass. `on_mount` pushes `FolderSelectScreen` with `_on_folders_selected` as the dismiss callback, which then pushes `MainScreen`. The app has no global CSS beyond a background color.

---

## Known Limitations and Extension Points

**Jellyfin's clean titles are not used for ratings yet.** The watch-data query already pulls every
item Jellyfin knows, and `BaseItems` carries `Name`, `ProductionYear`, `OriginalTitle` and
`BaseItemProviders` (so TMDB and IMDb ids are there for the asking). Ratings still resolve from
folder names such as `mindhunter (tv series 2017-2019) [67744]`, which is why `cache.json` had 982
movie ratings against only 8 TV ones. Taking the title — or better, the provider id — from the
same query would skip the TMDB search step entirely for most titles, which also sidesteps the
TMDB DNS block on the home network. This is the single highest-value follow-up.

**Watch data is a snapshot, not live.** It is read once on mount. A playback that starts while the
table is open is not reflected until the next run.

**Jellyfin must agree with the filesystem.** A title Jellyfin has not scanned shows `—`, not
"never played". The unmatched-file count in the other direction is surfaced as a notification.
Both mean Jellyfin's library is out of date, not that the tool is wrong.

**Schema coupling.** The query names `BaseItems`, `UserData` and the 12.x
`MediaBrowser.Controller.Entities.*` type strings. A Jellyfin major upgrade can rename these; the
failure mode is a clean `WatchDataUnavailable` and empty columns, not bad numbers.

**Ratings accuracy for non-English titles:** TMDB's English search (`language=en-US`) may miss titles with non-Latin names. Passing the original title to TMDB can improve results for Bollywood and regional films.

**Anime ratings:** TMDB treats anime as TV shows. For better anime-specific ratings, AniList or MyAnimeList APIs could be integrated. They require different search logic and return different score formats.

**No interactive disambiguation:** If TMDB returns a wrong match (e.g., `"Dune"` matching the 1984 film instead of 2021), there's no UI to override it. A future improvement could add an `e` keybinding to open a search-override dialog for the highlighted item.

**Scan blocks the event loop:** `scan()` is called synchronously in `on_mount`. For very large libraries it may cause a brief freeze. This could be moved into a `@work(thread=True)` worker with a loading indicator if needed.

**Names stored decomposed (NFD) on the NAS cannot be opened from the Mac.** macOS lists them, but smbfs composes the name when opening it, so the server reports it missing. Such a title still gets a row and can still be deleted (the NAS-side script matches by NFC), but its size shows 0 B and a scan warning is logged. Verified with a test folder on 2026-09-30; the real library had no such names at that time. Names created over SMB from the Mac are stored NFC.

**Cache is a flat JSON file:** `cache.json` grows unboundedly over time (new entries are added, old ones are only overwritten when re-fetched). A periodic compaction could trim entries for titles no longer on disk.

**`type_map` is not exposed in folder selector:** The folder selector shows all known folders but doesn't let the user remap their media types. This is intentional — `type_map` is a config concern, not a runtime selection.

**`folder_label` vs subcategory:** For movies with subcategory folders (e.g., `Hollywood & Western Movies`), the `folder_label` stored on the `MediaItem` is the top-level folder (`Movies`), not the subcategory. The subcategory name is not preserved anywhere. This is intentional — the table shows `Movies` for all movies regardless of which subcategory they came from.
