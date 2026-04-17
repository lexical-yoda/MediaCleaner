from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
import os

_NETWORK_FS_TYPES = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "nfs3", "sshfs", "fuse.sshfs"}


def _detect_network_fs(path: Path) -> str | None:
    """Return the filesystem type if path lives on a network mount, else None."""
    try:
        resolved = str(path.resolve())
        best_match_len = 0
        best_fstype: str | None = None
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount_point, fstype = parts[1], parts[2]
                if resolved.startswith(mount_point) and len(mount_point) >= best_match_len:
                    best_match_len = len(mount_point)
                    best_fstype = fstype
        return best_fstype if best_fstype in _NETWORK_FS_TYPES else None
    except OSError:
        return None

load_dotenv()

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "media_root": "/mnt/hodorShare/media",
    "tmdb_api_key": "YOUR_KEY_HERE",
    "omdb_api_key": "YOUR_OMDB_KEY_HERE",
    "type_map": {
        "Movies": "movie",
        "TV Shows": "tv",
        "Anime & Animation": "anime",
    },
    "trash_mode": True,
    "cache_ttl_days": 7,
}


@dataclass
class Config:
    media_root: Path
    tmdb_api_key: str
    omdb_api_key: str
    type_map: dict[str, str]
    trash_mode: bool
    cache_ttl_days: int
    known_folders: list[str] = field(default_factory=list)


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        print(f"Created default config at {CONFIG_PATH}")
        print("Please set your TMDB API key in config.json, then re-run.")
        sys.exit(0)

    with CONFIG_PATH.open() as f:
        raw = json.load(f)

    api_key = os.environ.get("TMDB_API_KEY") or raw.get("tmdb_api_key", "")
    omdb_api_key = os.environ.get("OMDB_API_KEY") or raw.get("omdb_api_key", "")
    media_root_str = os.environ.get("MEDIA_ROOT") or raw.get("media_root", "")
    type_map: dict[str, str] = raw.get("type_map", DEFAULT_CONFIG["type_map"])
    trash_mode: bool = raw.get("trash_mode", True)
    cache_ttl_days: int = raw.get("cache_ttl_days", 7)

    errors: list[str] = []

    if not api_key or api_key == "YOUR_KEY_HERE":
        errors.append("tmdb_api_key is not set in config.json (or TMDB_API_KEY env var)")

    if not omdb_api_key or omdb_api_key == "YOUR_OMDB_KEY_HERE":
        errors.append("omdb_api_key is not set in config.json (or OMDB_API_KEY env var) — get a free key at omdbapi.com")

    if not media_root_str:
        errors.append("media_root is not set in config.json (or MEDIA_ROOT env var)")
        media_root = Path(".")
    else:
        media_root = Path(media_root_str)
        if not media_root.exists():
            errors.append(f"media_root does not exist: {media_root}")

    valid_types = {"movie", "tv", "anime"}
    for folder, mtype in type_map.items():
        if mtype not in valid_types:
            errors.append(f"type_map['{folder}'] = '{mtype}' is not valid (use: movie, tv, anime)")

    if errors:
        for e in errors:
            print(f"Config error: {e}")
        sys.exit(1)

    known_folders = [f for f in type_map if (media_root / f).is_dir()]

    network_fs = _detect_network_fs(media_root)
    if network_fs and trash_mode:
        print(
            f"Warning: media_root is on a network filesystem ({network_fs}). "
            "Trash mode disabled — files will be permanently deleted. "
            "Set trash_mode: false in config.json to suppress this warning."
        )
        trash_mode = False

    return Config(
        media_root=media_root,
        tmdb_api_key=api_key,
        omdb_api_key=omdb_api_key,
        type_map=type_map,
        trash_mode=trash_mode,
        cache_ttl_days=cache_ttl_days,
        known_folders=known_folders,
    )
