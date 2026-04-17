# MediaCleaner

A terminal UI application for managing a Jellyfin media library. Browse your movies, TV shows, and anime by IMDb and Rotten Tomatoes ratings alongside their disk usage, then bulk-delete what you no longer want.

## Features

- Scans your local Jellyfin media directories directly (no Jellyfin API needed)
- Fetches IMDb ratings and Rotten Tomatoes scores for every title
- Displays total disk size per title (recursive, includes all seasons/episodes)
- Sortable by title, size, IMDb rating, or RT score
- Multi-select items and delete them all at once
- Sends to system trash by default (recoverable); permanent delete is opt-in
- Audit log of every deletion written to `deleted.log`
- Results cached locally for 7 days — repeat runs are instant

## Requirements

- Python 3.10+
- Conda environment `py310` (or any Python 3.10+ environment)
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
  "media_root": "/mnt/hodorShare/media",
  "tmdb_api_key": "your_tmdb_key_here",
  "omdb_api_key": "your_omdb_key_here",
  "type_map": {
    "Movies": "movie",
    "TV Shows": "tv",
    "Anime & Animation": "anime"
  },
  "trash_mode": true,
  "cache_ttl_days": 7
}
```

- `media_root` — path to the root of your Jellyfin media library
- `type_map` — maps subfolder names to their media type; add or rename entries to match your folder layout
- `trash_mode` — `true` sends to system trash (recoverable), `false` deletes permanently
- `cache_ttl_days` — how many days before ratings are re-fetched

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
| `s` | Cycle sort (title↑ → title↓ → size↑ → size↓ → IMDb↑ → IMDb↓ → RT↑ → RT↓) |
| `d` | Delete selected items (opens confirmation) |
| `b` | Go back to folder selector |
| `q` | Quit |

Ratings load progressively in the background after the table appears. Items without a match show `N/A`.

### Deletion Confirmation

Shows a summary of selected items and total space that will be freed. Confirm to proceed or press `Escape` / Cancel to abort.

## Directory Structure Support

The scanner handles:

- **Movies with subcategory folders**: `Movies/Hollywood & Western Movies/Film (2020)/`
- **Movies flat**: `Movies/Film (2020)/`
- **TV shows with seasons**: `TV Shows/Show Name/Season 01/`
- **TV shows without seasons**: `TV Shows/Show Name/episode.mkv`
- **Anime** (same as TV shows, either layout)

## Files

| File | Purpose |
|------|---------|
| `config.json` | Your configuration (not committed) |
| `cache.json` | Cached TMDB/OMDB responses (auto-generated) |
| `deleted.log` | Timestamped audit log of all deletions |
