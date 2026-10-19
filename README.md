# MediaCleaner

A terminal UI application for managing a Jellyfin media library. Browse your movies, TV shows, and anime by IMDb and Rotten Tomatoes ratings alongside their disk usage, then bulk-delete what you no longer want.

## Features

- Scans your local Jellyfin media directories directly (no Jellyfin API needed)
- Fetches IMDb ratings and Rotten Tomatoes scores for every title
- Displays total disk size per title (recursive, includes all seasons/episodes)
- Shows when each title was added to disk, so recent arrivals are not mistaken for dead weight
- Shows how much of each title anyone has ever played, and the total play count, read
  straight out of Jellyfin's database
- Sortable by title, size, IMDb rating, RT score, watched fraction, play count, or added date
- Multi-select items and delete them all at once
- Three deletion modes, including a recoverable trash on the NAS itself
- Audit log of every deletion attempt written to `deleted.log`, successes and failures alike
- Everything the tool does, and every failure, logged to `mediacleaner.log`
- Results cached locally for 7 days — repeat runs are instant

## Requirements

- Python 3.10+ (currently run from the miniconda base environment, Python 3.14, which has every
  dependency installed)
- A free [TMDB API key](https://www.themoviedb.org/settings/api)
- A free [OMDB API key](https://www.omdbapi.com/apikey.aspx)

## Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Configure**

Edit `config.json`:

```json
{
  "media_root": "/mnt/nas-share/media",
  "tmdb_api_key": "your_tmdb_key_here",
  "omdb_api_key": "your_omdb_key_here",
  "type_map": {
    "Movies": "movie",
    "TV Shows": "tv",
    "Anime & Animation": "anime"
  },
  "delete_mode": "nas_trash",
  "cache_ttl_days": 7,
  "nas_ssh": "user@nas.example",
  "local_mount_root": "/Volumes/nas-share",
  "nas_media_root": "/mnt/pool/nas-share",
  "nas_trash_dir": "/mnt/pool/nas-share/.mediacleaner-trash",
  "watch_data": true,
  "jellyfin_ssh": "user@nas.example",
  "jellyfin_db_path": "/path/to/jellyfin/config/data/jellyfin.db",
  "jellyfin_container_root": "/media"
}
```

- `media_root` — path to the root of your Jellyfin media library
- `type_map` — maps subfolder names to their media type; add or rename entries to match your folder layout
- `delete_mode` — one of `nas_trash`, `local_trash` or `permanent`; see below
- `cache_ttl_days` — how many days before ratings are re-fetched
- `nas_ssh`, `local_mount_root`, `nas_media_root`, `nas_trash_dir` — only needed for `nas_trash`
- `watch_data` — set to `false` to turn the Watched and Plays columns off entirely
- `jellyfin_ssh`, `jellyfin_db_path`, `jellyfin_container_root` — only needed for watch data; see
  below. `jellyfin_ssh` defaults to `nas_ssh` when it is left empty

The old boolean `trash_mode` still works if `delete_mode` is absent: `true` maps to `local_trash`
and `false` to `permanent`.

## Watch data

Size and ratings say how much a title costs and how good it is. Neither answers the question
that actually decides a deletion: **has anyone ever watched it?** That comes from Jellyfin.

MediaCleaner reads it straight out of `jellyfin.db` with `sqlite3 -readonly`, over the same SSH
connection `nas_trash` already uses. It does **not** use the Jellyfin HTTP API, so there is no API
key to manage and the tool still works when Jellyfin itself is down. Nothing is ever written to
that database.

The read is one query, takes well under a second on a 16,000-file library, and runs in the
background after the table appears — exactly like the ratings lookup. The table is fully usable
before it lands.

- `jellyfin_ssh` — where the database lives. Defaults to `nas_ssh`.
- `jellyfin_db_path` — path to `jellyfin.db` **on that host**, not inside the container.
- `jellyfin_container_root` — what `local_mount_root` is called inside the Jellyfin container.
  Jellyfin stores container paths, so this is what lets a row in the database be matched to a
  folder on your machine. With the values above, Jellyfin's
  `/media/media/TV Shows/X` becomes `/Volumes/nas-share/media/TV Shows/X`.

If the host is unreachable the last successful read is shown instead, in italics, with its date in
the status bar. If there is no cached read either, the two columns are blank and everything else
still works.

### The Watched and Plays columns

`Watched` is how many of the playable items under a title anyone has ever played.

- A **movie** shows `✓` or `✗`.
- A **series** shows `played / total` episodes, for example `4/327`. An episode counts as played
  if *any* user played it.
- Colour runs red (nothing), red (5% or less), yellow (under half), green (more).
- `—` means Jellyfin has no playable item under that folder at all. That is **not** the same as
  "never played", and is never counted as one: it usually means Jellyfin's library needs a rescan.
  If any such files are found, the tool says so.

`Plays` is the total play count across every user, with the number of distinct users in brackets
when there is more than one, for example `20 (2u)`.

The status bar carries the headline: how many titles have never been played and what they come to
on disk. The confirmation dialog repeats the play history per item and warns when something you
are about to delete has been watched.

**A never-played title is not necessarily unwanted.** It may be queued, or kept deliberately, or
have arrived last week — which is what the `Added` column is for. Watch data answers "has anyone
opened this", and nothing more.

**Counting note.** Jellyfin writes `UserData` once per data key (the item's own GUID, its TMDB id
and its IMDb id), so the same play is stored two or three times. The query takes the maximum per
user before summing. Summing the rows directly roughly doubles every number.

## Deletion modes

**`nas_trash` (recommended when the library is on a network share).** Moves items over SSH into a
timestamped directory under `nas_trash_dir` on the NAS itself. Because that directory is on the same
ZFS dataset as the media, the move is a rename: instant, regardless of how large the title is, and
fully recoverable. Nothing is copied over the network. The NAS needs `python3` (TrueNAS SCALE ships
it); the move runs as a short Python script over SSH.

Each item keeps its path inside the trash, so the trash shows where everything came from and two
titles with the same folder name in different categories cannot collide:

```
/mnt/pool/nas-share/.mediacleaner-trash/2026-09-30_101500/media/Movies/Hollywood & Western Movies/Zodiac (2007)
```

An item is only reported as deleted after the NAS confirms the source path is gone and the trash path
exists. Anything else is a failure: the row stays in the table and the reason goes to both logs.

Note that **no space is freed until the trash is purged**, which is the point. Purge with:

```bash
ssh user@nas.example 'rm -rf /mnt/pool/nas-share/.mediacleaner-trash/<timestamp>'
```

Restore an item by moving it back to the same path without the trash prefix:

```bash
ssh user@nas.example 'mv "/mnt/pool/nas-share/.mediacleaner-trash/<timestamp>/media/Movies/Hollywood & Western Movies/Zodiac (2007)" "/mnt/pool/nas-share/media/Movies/Hollywood & Western Movies/"'
```

`local_mount_root` and `nas_media_root` describe the same directory from each side, so the tool can
translate paths. With the values above, `/Volumes/nas-share/media/TV Shows/X` becomes
`/mnt/pool/nas-share/media/TV Shows/X`. `nas_trash_dir` **must be on the same dataset as the media**;
if it is not, the rename fails with `EXDEV` and the tool refuses rather than copying.

Names with accents are matched regardless of Unicode form. macOS shows names on the SMB mount in
decomposed form (`e` + combining accent) while the NAS stores them composed (`é`), so a plain path
lookup for a title like `Amélie (2001)` fails on the NAS. The NAS-side script compares each path
component in composed form instead.

In every mode the tool refuses to delete the media root or a library folder such as `Movies`
itself; only titles under a library folder can be deleted.

**`local_trash`.** The desktop trash via `send2trash`. This **does not work on an SMB mount**: macOS
reports `OSError: The volume doesn't have a trash`, because no `.Trashes` directory exists on the
share and macOS will not create one. It is kept for the case where the library is on local disk. On
macOS it needs `pyobjc-framework-Cocoa`, without which send2trash silently falls back to a
deprecated Carbon backend that fails with the less helpful `OSError: Directory not found`.

**`permanent`.** `shutil.rmtree`, with no recovery. Worth remembering that Jellyfin media is
excluded from the backup jobs, so permanent here really does mean permanent.

If `delete_mode` is `local_trash` and the library turns out to be on a network mount, the tool
switches to `nas_trash` when that is configured and says so. It will **not** silently escalate to
permanent deletion, which is what an earlier version did.

**3. Run**

```bash
python main.py
```

## Usage

### Folder Selector (startup screen)

On launch, a list of your configured media folders is shown. Use `space` to toggle which folders to include, then `enter` to start scanning.

### Main Table

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate |
| `space` | Toggle selection on current item |
| `ctrl+a` | Select all |
| `ctrl+d` | Deselect all |
| `s` | Cycle sort (title↑ → title↓ → size↑ → size↓ → IMDb↑ → IMDb↓ → RT↑ → RT↓ → watched↑ → watched↓ → plays↑ → plays↓ → added↑ → added↓) |
| `d` | Delete selected items (opens confirmation) |
| `b` | Go back to folder selector |
| `q` | Quit |

Ratings load progressively in the background after the table appears. Items without a match show `N/A`.
Watch data lands in one go, usually within a second, and fills the `Watched` and `Plays` columns for
every row at once. `watched↑` is the sort that puts the never-played titles at the top.

Rows are only removed from the table when their delete actually succeeded. A partial failure reports
how many of each, and the details go to `deleted.log` and `mediacleaner.log`.

If anything goes wrong during the scan (a folder that cannot be listed, a file that cannot be read,
a symlink that is skipped), the status bar shows a red scan-warning count and every warning is in
`mediacleaner.log`.

One quirk of SMB: a deleted directory can still appear in a local listing for a few seconds after a
successful move, because macOS caches directory attributes. It clears on its own.

### The Added column

`Added` is the date the title first appeared on disk, taken as the oldest creation time of any file
beneath it. Creation time comes from `st_birthtime` where the filesystem reports it (macOS, and SMB
mounts that pass it through) and falls back to `st_mtime` otherwise.

- **Highlighted green** when the title arrived in the last 30 days. An unwatched title that only
  landed days ago is not a deletion candidate yet, so this is the column to check before deleting
  anything.
- **A trailing `+`** means content was added more than 30 days after the title first appeared. That
  distinguishes a series still receiving episodes from one that landed in a single batch and has not
  changed since.

Note that copying files with their timestamps preserved (`rsync -t`, `cp -p`) carries the original
timestamps across, so for that content the date reflects when the file was created rather than when
it was moved into the library.

### Deletion Confirmation

Shows a summary of selected items and total space that will be freed. Confirm to proceed or press `Escape` / Cancel to abort.

## Directory Structure Support

The scanner handles:

- **Movies with subcategory folders**: `Movies/Hollywood & Western Movies/Film (2020)/`
- **Movies flat**: `Movies/Film (2020)/`
- **Movies without a year, or with extra text after it**: `Dangal/`, `La La Land (2016) [1080p]/`
- **TV shows with seasons**: `TV Shows/Show Name/Season 01/`
- **TV shows without seasons**: `TV Shows/Show Name/episode.mkv`
- **Anime** (same as TV shows, either layout)
- **Loose video files** sitting directly in a library or category folder, each shown as its own row

A folder directly under a movie library is a category when it contains a `Title (YYYY)` folder, or
contains only folders. Anything else there is a title. Below a category, every folder is a title.
Only OS clutter files (`.DS_Store`, `._*`, `Thumbs.db`, `desktop.ini`) are left out of the table;
every other file on disk belongs to exactly one row.

## Files

| File | Purpose |
|------|---------|
| `config.json` | Your configuration (not committed) |
| `cache.json` | Cached TMDB/OMDB responses (auto-generated) |
| `watch_cache.json` | Last successful read of Jellyfin's watch data, used when the NAS is unreachable (auto-generated) |
| `deleted.log` | Timestamped audit log of all deletions |
| `mediacleaner.log` | Everything the tool does: startup, config, scan results and warnings, selections, deletes, rating lookup failures, crashes. Rotates at 5 MB, keeping 5 old files. API keys are masked |
