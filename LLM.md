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
├── main.py                    # Entry point: loads config, runs the Textual app
├── config.py                  # Config loading, validation, dataclass
├── scanner.py                 # Filesystem scan → list[MediaItem]; recursive size calc
├── tmdb.py                    # RatingsClient: TMDB (search/IMDb ID) + OMDB (ratings)
├── deleter.py                 # Deletion logic: trash or permanent + audit log
├── app.py                     # Textual App subclass: screen orchestration
├── screens/
│   ├── folder_select.py       # Screen 1: choose which folders to scan
│   ├── main_screen.py         # Screen 2: main table, sorting, TMDB worker
│   └── confirm_screen.py      # Modal: deletion confirmation with size summary
├── widgets/
│   └── media_table.py         # Custom DataTable subclass with multi-select + sort
├── config.json                # Runtime config (gitignored)
├── cache.json                 # TMDB/OMDB response cache (auto-generated, gitignored)
├── deleted.log                # Append-only audit log (gitignored)
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
                           │    scan(config, selected_folders)  → list[MediaItem]
                           │    MediaTable._populate()           → rows in table
                           │    _fetch_ratings() [Worker]        → fills ratings async
                           │         RatingsClient.fetch_ratings()
                           │              TMDB search → imdb_id
                           │              OMDB lookup → imdb_rating, rt_score
                           │         _refresh_item() → MediaTable.refresh_item()
                           └─ on 'd':
                                ConfirmScreen (modal)
                                     └─ dismiss(True)
                                          └─ delete_items(paths, trash_mode)
                                               → deleted.log append
```

---

## Module Details

### `config.py`

**`Config` dataclass fields:**
- `media_root: Path` — root of the Jellyfin library
- `tmdb_api_key: str` — TMDB v3 API key
- `omdb_api_key: str` — OMDB API key
- `type_map: dict[str, str]` — maps folder name → media type (`"movie"`, `"tv"`, `"anime"`)
- `trash_mode: bool` — true = send to trash, false = permanent delete
- `cache_ttl_days: int` — how long before a cache entry is considered stale
- `known_folders: list[str]` — subset of `type_map` keys that actually exist on disk

**Loading priority:** `TMDB_API_KEY` / `OMDB_API_KEY` / `MEDIA_ROOT` env vars override `config.json` values. This allows keeping the API key in `.env` while the rest lives in `config.json`.

**First-run behavior:** If `config.json` does not exist, a template is written and the process exits with instructions. This prevents the app from running without valid config.

**Validation exits:** Empty `media_root`, missing API keys, or invalid `type_map` values (`"movie"` / `"tv"` / `"anime"` are the only valid values) all cause `sys.exit(1)` with a clear error message before the TUI launches.

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
    folder_label: str    # top-level folder name, e.g. "Movies"

    # Set async by RatingsClient worker
    tmdb_id: int | None = None
    imdb_id: str | None = None      # e.g. "tt1375666"
    imdb_rating: float | None = None
    rt_score: int | None = None     # integer 0–100
    tmdb_title: str | None = None   # canonical TMDB title (useful to spot mismatches)
    tmdb_status: str = "pending"    # "pending" | "fetching" | "found" | "not_found" | "error"

    selected: bool = False
```

**`_dir_size(path)`** uses an explicit stack instead of `os.walk` to avoid deep recursion. It uses `os.scandir` for performance. Symlinks are not followed (`follow_symlinks=False`).

**`_parse_name(dirname)`** applies `^(.+?)\s*\((\d{4})\)\s*$` to extract title and year from a directory name. If no year is present, year is `None` and the full dirname is used as the title. This handles `"Inception (2010)"` → `("Inception", 2010)` and `"Breaking Bad"` → `("Breaking Bad", None)`.

**`_scan_title_dirs(folder_path, media_type, folder_label, items)`** is the recursive scanner:

- For `media_type == "movie"`: if a subdirectory does **not** match the year pattern, it is treated as a subcategory (e.g., `Hollywood & Western Movies`) and the function recurses into it. This handles the actual library structure:
  ```
  Movies/
    Hollywood & Western Movies/
      Inception (2010)/
    Bollywood & Regional Movies/
      3 Idiots (2009)/
  ```
- For `tv` / `anime`: every subdirectory is unconditionally treated as a show title, regardless of whether it has a year in the name. The recursive subcategory logic is intentionally disabled for these types.

**Why not use the year pattern for TV?** TV show directories typically don't have years (e.g., `Breaking Bad/`), so a year-absence check would incorrectly treat every show as a subcategory folder.

**Size is always the full show/movie total.** For TV shows, `_dir_size` is called on the show root (e.g., `/TV Shows/Breaking Bad/`), so it sums across all Season subdirectories. This works identically whether episodes are in Season folders or directly in the show directory.

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

### `widgets/media_table.py`

**`MediaTable` extends `DataTable`.**

**Critical naming issue (already fixed):** `DataTable` has its own `refresh_row(row_index: int)` method. Our method for updating a row given a `MediaItem` is named `refresh_item(item: MediaItem)` to avoid overriding the parent. Setting `cursor_type` in the constructor (`super().__init__(cursor_type="row", ...)`) rather than in `on_mount` avoids triggering `watch_cursor_type` before the table has rows.

**Row identity:** Rows are keyed by `str(item.path)`. This is used both as the DataTable row key and as the index into `self._row_keys: list[str]`. The `_row_keys` list maintains the current display order and is rebuilt by `_populate()` on every sort.

**`refresh_item(item)`:** Finds the item's current row index via `_row_keys.index(key)` and calls `update_cell_at((row, col), value)` for each of the 6 columns. This is called from the async TMDB worker to update ratings in-place without re-populating the entire table.

**Sort cycles:** `SORT_CYCLES` is a list of `(field, ascending)` tuples. `cycle_sort()` increments the index mod len and re-sorts `self._items` in-place then calls `_populate()`. The sort key for ratings always puts `None` values at the end:
```python
key=lambda x: (x.imdb_rating is None, (x.imdb_rating or 0) if ascending else -(x.imdb_rating or 0))
```
The `(is_none, value)` tuple ensures `None` sorts last regardless of direction.

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

**`_fetch_ratings` worker:** `@work(exclusive=False, thread=False)` creates an asyncio coroutine worker (not a thread). Since it runs on the same event loop as the UI, calling UI methods directly from within it (like `_refresh_item`) is safe — no `call_from_thread` needed. `asyncio.gather(*tasks)` runs all per-item fetches concurrently, limited by the semaphores in `RatingsClient`.

**`_on_confirm`:** Gets the selected items from the table, calls `delete_items`, then removes the deleted paths from the table via `MediaTable.remove_items`. The local `self._items` reference is also updated to stay in sync. Errors from `delete_items` are shown as a Textual notification.

---

### `screens/confirm_screen.py`

**`ConfirmScreen`** is a `ModalScreen[bool]` — the generic parameter is the dismiss value type. `dismiss(True)` confirms, `dismiss(False)` cancels. The caller receives the value in its callback (`_on_confirm`).

The confirmation list is scrollable (`max-height: 12; overflow-y: auto`) to handle large selections. Total freed space is computed as `sum(i.size_bytes for i in items)` and displayed in human-readable form.

The action button label changes between `"Move to Trash"` and `"Delete Permanently"` based on `config.trash_mode`.

---

### `deleter.py`

Simple module with a single function `delete_items(paths, trash_mode) -> list[str]`.

- `trash_mode=True`: calls `send2trash.send2trash(str(path))`. This uses the OS trash mechanism (XDG Trash on Linux, Recycle Bin on Windows).
- `trash_mode=False`: calls `shutil.rmtree(path)` for directories, `path.unlink()` for files (though in practice all paths are directories).
- All deletions are appended to `deleted.log` regardless of mode: `{iso_timestamp} {TRASH|DELETE} {path}`
- Returns a list of error strings (one per failed path). An empty list means all succeeded.

---

### `app.py`

Minimal `App` subclass. `on_mount` pushes `FolderSelectScreen` with `_on_folders_selected` as the dismiss callback, which then pushes `MainScreen`. The app has no global CSS beyond a background color.

---

## Known Limitations and Extension Points

**Ratings accuracy for non-English titles:** TMDB's English search (`language=en-US`) may miss titles with non-Latin names. Passing the original title to TMDB can improve results for Bollywood and regional films.

**Anime ratings:** TMDB treats anime as TV shows. For better anime-specific ratings, AniList or MyAnimeList APIs could be integrated. They require different search logic and return different score formats.

**No interactive disambiguation:** If TMDB returns a wrong match (e.g., `"Dune"` matching the 1984 film instead of 2021), there's no UI to override it. A future improvement could add an `e` keybinding to open a search-override dialog for the highlighted item.

**Scan blocks the event loop:** `scan()` is called synchronously in `on_mount`. For very large libraries it may cause a brief freeze. This could be moved into a `@work(thread=True)` worker with a loading indicator if needed.

**Cache is a flat JSON file:** `cache.json` grows unboundedly over time (new entries are added, old ones are only overwritten when re-fetched). A periodic compaction could trim entries for titles no longer on disk.

**`type_map` is not exposed in folder selector:** The folder selector shows all known folders but doesn't let the user remap their media types. This is intentional — `type_map` is a config concern, not a runtime selection.

**`folder_label` vs subcategory:** For movies with subcategory folders (e.g., `Hollywood & Western Movies`), the `folder_label` stored on the `MediaItem` is the top-level folder (`Movies`), not the subcategory. The subcategory name is not preserved anywhere. This is intentional — the table shows `Movies` for all movies regardless of which subcategory they came from.
