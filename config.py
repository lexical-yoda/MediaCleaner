from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
import os

from applog import register_secret
from deleter import DELETE_MODES

log = logging.getLogger("config")

_NETWORK_FS_TYPES = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "nfs3", "sshfs", "fuse.sshfs"}


def _mount_table() -> list[tuple[str, str]]:
    """Return [(mount_point, fstype)] on Linux or macOS.

    /proc/mounts does not exist on macOS, so the original implementation always
    raised and silently returned None there, which meant the network-filesystem
    check never fired on the machine this tool actually runs on. Fall back to
    parsing `mount` output, whose macOS form is:
        //user@host/share on /Volumes/share (smbfs, nodev, nosuid, ...)
    """
    try:
        with open("/proc/mounts") as f:
            out = []
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    out.append((parts[1], parts[2]))
            return out
    except OSError:
        pass
    try:
        proc = subprocess.run(["mount"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in proc.stdout.splitlines():
        if " on " not in line or "(" not in line:
            continue
        rest = line.split(" on ", 1)[1]
        mount_point, _, tail = rest.partition(" (")
        fstype = tail.split(",", 1)[0].rstrip(")").strip()
        out.append((mount_point.strip(), fstype))
    return out


def _detect_network_fs(path: Path) -> str | None:
    """Return the filesystem type if path lives on a network mount, else None."""
    try:
        resolved = str(path.resolve())
    except OSError:
        return None
    best_len = 0
    best_fstype: str | None = None
    for mount_point, fstype in _mount_table():
        if resolved.startswith(mount_point) and len(mount_point) >= best_len:
            best_len = len(mount_point)
            best_fstype = fstype
    return best_fstype if best_fstype in _NETWORK_FS_TYPES else None


load_dotenv()

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "media_root": "/mnt/nas-share/media",
    "tmdb_api_key": "YOUR_KEY_HERE",
    "omdb_api_key": "YOUR_OMDB_KEY_HERE",
    "type_map": {
        "Movies": "movie",
        "TV Shows": "tv",
        "Anime & Animation": "anime",
    },
    "delete_mode": "nas_trash",
    "cache_ttl_days": 7,
    "nas_ssh": "user@nas.example",
    "local_mount_root": "/Volumes/nas-share",
    "nas_media_root": "/mnt/pool/nas-share",
    "nas_trash_dir": "/mnt/pool/nas-share/.mediacleaner-trash",
    "watch_data": True,
    "jellyfin_ssh": "",
    "jellyfin_db_path": "/path/to/jellyfin/config/data/jellyfin.db",
    "jellyfin_container_root": "/media",
}


@dataclass
class Config:
    media_root: Path
    tmdb_api_key: str
    omdb_api_key: str
    type_map: dict[str, str]
    delete_mode: str
    cache_ttl_days: int
    nas_ssh: str = ""
    local_mount_root: str = ""
    nas_media_root: str = ""
    nas_trash_dir: str = ""
    watch_data: bool = False
    jellyfin_ssh: str = ""
    jellyfin_db_path: str = ""
    jellyfin_container_root: str = "/media"
    known_folders: list[str] = field(default_factory=list)

    @property
    def trash_mode(self) -> bool:
        """Back-compat for callers that ask whether deletion is recoverable."""
        return self.delete_mode in ("nas_trash", "local_trash")


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        log.warning("No config.json found; wrote a default one to %s and exited", CONFIG_PATH)
        print(f"Created default config at {CONFIG_PATH}")
        print("Please set your TMDB API key in config.json, then re-run.")
        sys.exit(0)

    with CONFIG_PATH.open() as f:
        raw = json.load(f)

    api_key = os.environ.get("TMDB_API_KEY") or raw.get("tmdb_api_key", "")
    omdb_api_key = os.environ.get("OMDB_API_KEY") or raw.get("omdb_api_key", "")
    register_secret(api_key)
    register_secret(omdb_api_key)
    media_root_str = os.environ.get("MEDIA_ROOT") or raw.get("media_root", "")
    type_map: dict[str, str] = raw.get("type_map", DEFAULT_CONFIG["type_map"])
    # delete_mode supersedes the old boolean trash_mode. Honour the old key when
    # the new one is absent so existing configs keep working.
    delete_mode: str = raw.get("delete_mode") or (
        "local_trash" if raw.get("trash_mode", True) else "permanent"
    )
    cache_ttl_days: int = raw.get("cache_ttl_days", 7)
    nas_ssh: str = raw.get("nas_ssh", "")
    local_mount_root: str = raw.get("local_mount_root", "")
    nas_media_root: str = raw.get("nas_media_root", "")
    nas_trash_dir: str = raw.get("nas_trash_dir", "")
    # Watch data is read over the same SSH connection nas_trash uses, so it
    # defaults to the same host when jellyfin_ssh is not given separately.
    watch_data: bool = bool(raw.get("watch_data", True))
    jellyfin_ssh: str = raw.get("jellyfin_ssh") or nas_ssh
    jellyfin_db_path: str = raw.get(
        "jellyfin_db_path", DEFAULT_CONFIG["jellyfin_db_path"]
    )
    jellyfin_container_root: str = raw.get("jellyfin_container_root", "/media")

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

    if delete_mode not in DELETE_MODES:
        errors.append(
            f"delete_mode '{delete_mode}' is not valid (use: {', '.join(DELETE_MODES)})"
        )
    elif delete_mode == "nas_trash":
        for key, value in (
            ("nas_ssh", nas_ssh),
            ("local_mount_root", local_mount_root),
            ("nas_media_root", nas_media_root),
            ("nas_trash_dir", nas_trash_dir),
        ):
            if not value:
                errors.append(f"delete_mode is 'nas_trash' but {key} is not set in config.json")

    valid_types = {"movie", "tv", "anime"}
    for folder, mtype in type_map.items():
        if mtype not in valid_types:
            errors.append(f"type_map['{folder}'] = '{mtype}' is not valid (use: movie, tv, anime)")

    if errors:
        for e in errors:
            log.error("Config error: %s", e)
            print(f"Config error: {e}")
        sys.exit(1)

    known_folders = [f for f in type_map if (media_root / f).is_dir()]
    for folder in type_map:
        if folder not in known_folders:
            log.warning("type_map folder %r is not a directory under %s; it will not be offered", folder, media_root)

    # A network mount cannot use the desktop trash: macOS reports "The volume
    # doesn't have a trash" for this SMB share even with the modern send2trash
    # backend. The previous behaviour here was to silently switch to permanent
    # deletion, which turned a recoverable action into an unrecoverable one
    # without the user choosing it. Prefer the NAS-side trash instead, and if
    # that is not configured, warn loudly rather than escalating.
    network_fs = _detect_network_fs(media_root)
    if network_fs and delete_mode == "local_trash":
        if nas_ssh and local_mount_root and nas_media_root and nas_trash_dir:
            msg = (
                f"Note: media_root is on a network filesystem ({network_fs}), which has no "
                "desktop trash. Using delete_mode 'nas_trash' instead."
            )
            log.warning(msg)
            print(msg)
            delete_mode = "nas_trash"
        else:
            msg = (
                f"Warning: media_root is on a network filesystem ({network_fs}), which has no "
                "desktop trash, so deletions will fail. Configure nas_ssh, local_mount_root, "
                "nas_media_root and nas_trash_dir and set delete_mode to 'nas_trash', or set "
                "delete_mode to 'permanent' to delete without recovery."
            )
            log.warning(msg)
            print(msg)
    elif not network_fs and delete_mode == "nas_trash":
        # nas_trash acts on the NAS copy of each path. If the share is not
        # mounted, media_root is just a local directory with the same name.
        log.warning(
            "media_root %s is not on a network filesystem, but delete_mode is nas_trash; "
            "is the share mounted?", media_root,
        )

    # Watch data is optional, exactly like ratings: a missing setting turns the
    # columns off and says so, rather than stopping the tool from running.
    if watch_data:
        missing = [
            key
            for key, value in (
                ("jellyfin_ssh (or nas_ssh)", jellyfin_ssh),
                ("jellyfin_db_path", jellyfin_db_path),
                ("jellyfin_container_root", jellyfin_container_root),
                ("local_mount_root", local_mount_root),
            )
            if not value
        ]
        if missing:
            msg = (
                "Watch data is on but " + ", ".join(missing) + " is not set in "
                "config.json; the Watched and Plays columns will be empty."
            )
            log.warning(msg)
            print(msg)
            watch_data = False

    log.info(
        "Config loaded: media_root=%s (fs=%s) delete_mode=%s nas_ssh=%s local_mount_root=%s "
        "nas_media_root=%s nas_trash_dir=%s known_folders=%s cache_ttl_days=%s "
        "watch_data=%s jellyfin_ssh=%s jellyfin_db_path=%s jellyfin_container_root=%s",
        media_root, network_fs or "local", delete_mode, nas_ssh, local_mount_root,
        nas_media_root, nas_trash_dir, known_folders, cache_ttl_days,
        watch_data, jellyfin_ssh, jellyfin_db_path, jellyfin_container_root,
    )

    return Config(
        media_root=media_root,
        tmdb_api_key=api_key,
        omdb_api_key=omdb_api_key,
        type_map=type_map,
        delete_mode=delete_mode,
        cache_ttl_days=cache_ttl_days,
        nas_ssh=nas_ssh,
        local_mount_root=local_mount_root,
        nas_media_root=nas_media_root,
        nas_trash_dir=nas_trash_dir,
        watch_data=watch_data,
        jellyfin_ssh=jellyfin_ssh,
        jellyfin_db_path=jellyfin_db_path,
        jellyfin_container_root=jellyfin_container_root,
        known_folders=known_folders,
    )
