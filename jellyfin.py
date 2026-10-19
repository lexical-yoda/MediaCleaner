"""Watch data from Jellyfin's database, read over SSH.

MediaCleaner deliberately does not depend on the Jellyfin HTTP API, so that it
keeps working when Jellyfin is down. Watch data follows the same rule: it is
read straight out of `jellyfin.db` with `sqlite3 -readonly` over the SSH
connection that `nas_trash` already needs. Writes to that database are
off-limits; this module only ever reads.

What comes back is, per title shown in the table:

* how many playable items Jellyfin knows beneath it (1 for a movie, N episodes
  for a series),
* how many of those anyone has ever played,
* the total play count across every user,
* how many distinct users played anything in it,
* when it was last played.

That is enough to answer "has anyone ever opened this", which is the question
the reclaim lists in the vault are built on and the one the size and rating
columns cannot answer.

Three details that are easy to get wrong:

1. **`UserData` holds several rows per (item, user).** Jellyfin writes the same
   state once per data key — the item's own GUID, the TMDB id and the IMDb id —
   so summing `PlayCount` straight out of the table double or triple counts.
   Measured on the real library: sum-of-sums 10,441 against sum-of-maxes 5,149.
   The query takes `MAX` per (item, user) first.

2. **Three names for one directory.** A path is `/media/media/X` inside the
   Jellyfin container, `/mnt/pool/nas-share/media/X` on the NAS and
   `/Volumes/nas-share/media/X` on the Mac. Only the container form is in the
   database, so it is rewritten to the local form before matching.

3. **Unicode form.** macOS lists SMB names decomposed (NFD) while the NAS, and
   therefore Jellyfin, stores them composed (NFC), so `Amélie (2001)` does not
   match as raw text. Both sides are normalised to NFC before comparison, the
   same fix `deleter.py` applies to the delete path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("jellyfin")

CACHE_PATH = Path(__file__).parent / "watch_cache.json"

SSH_TIMEOUT = 120

# One statement, two kinds of line, so a single round trip answers everything.
#
#   L <path>                     a playable item that exists on disk
#   A <path> <user> <plays> <last played>   that item, played by that user
#
# `ud` collapses the duplicate data-key rows to one row per (item, user) before
# anything is counted. Virtual items (episodes Jellyfin knows from metadata but
# has no file for) are excluded: they have no bytes on disk, so counting them
# would make a complete series look part-missing.
WATCH_SQL = """
.mode tabs
.headers off
WITH ud AS (
  SELECT ItemId, UserId,
         MAX(PlayCount)      AS pc,
         MAX(Played)         AS pl,
         MAX(LastPlayedDate) AS lpd
  FROM UserData
  GROUP BY ItemId, UserId
),
leaf AS (
  SELECT Id, Path FROM BaseItems
  WHERE Path IS NOT NULL
    AND IsVirtualItem = 0
    AND Type IN ('MediaBrowser.Controller.Entities.Movies.Movie',
                 'MediaBrowser.Controller.Entities.TV.Episode')
)
SELECT 'L', l.Path, '', '', '' FROM leaf l
UNION ALL
SELECT 'A', l.Path, u.UserId, u.pc, COALESCE(u.lpd, '')
FROM ud u JOIN leaf l ON l.Id = u.ItemId
WHERE u.pc > 0 OR u.pl = 1 OR u.lpd IS NOT NULL;
"""


@dataclass
class WatchStats:
    """Play history for one row of the table, summed over every user."""

    parts_total: int = 0
    parts_played: int = 0
    plays: int = 0
    watchers: int = 0
    last_played_ts: float | None = None

    @property
    def known(self) -> bool:
        """True when Jellyfin has at least one playable item under this title."""
        return self.parts_total > 0

    @property
    def never_played(self) -> bool:
        return self.parts_total > 0 and self.parts_played == 0

    @property
    def played_fraction(self) -> float:
        if not self.parts_total:
            return 0.0
        return self.parts_played / self.parts_total

    @property
    def last_played_date(self) -> str:
        if self.last_played_ts is None:
            return "-"
        return datetime.fromtimestamp(self.last_played_ts).strftime("%Y-%m-%d")


@dataclass
class _Accum:
    parts_total: int = 0
    played_parts: set = field(default_factory=set)
    plays: int = 0
    users: set = field(default_factory=set)
    last: str = ""


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _parse_jellyfin_date(raw: str) -> float | None:
    """Turn a Jellyfin `LastPlayedDate` into a POSIX timestamp.

    The column holds strings like `2026-04-03 12:17:21.6829546`: UTC, with
    seven fractional digits, which `fromisoformat` refuses before Python 3.11.
    The fraction is truncated to six digits and the result is treated as UTC.
    """
    if not raw:
        return None
    text = raw.strip().replace("T", " ")
    if "." in text:
        head, _, frac = text.partition(".")
        frac = "".join(c for c in frac if c.isdigit())[:6]
        text = f"{head}.{frac}" if frac else head
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        log.debug("Could not parse LastPlayedDate %r", raw)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class WatchDataUnavailable(Exception):
    """Raised when the Jellyfin database could not be read."""


async def _run_query(config) -> str:
    """Run WATCH_SQL on the NAS and return its stdout."""
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
        config.jellyfin_ssh,
        "sqlite3", "-readonly", config.jellyfin_db_path,
    ]
    log.info("Watch data: reading %s via %s", config.jellyfin_db_path, config.jellyfin_ssh)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise WatchDataUnavailable(f"could not start ssh: {type(e).__name__}: {e}") from e

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(WATCH_SQL.encode()), timeout=SSH_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise WatchDataUnavailable(f"ssh timed out after {SSH_TIMEOUT}s")

    err = stderr.decode(errors="replace").strip()
    if proc.returncode != 0:
        raise WatchDataUnavailable(
            err or f"ssh exited {proc.returncode} with no error output"
        )
    if err:
        # sqlite3 warns on stderr but can still have produced usable output.
        log.warning("Watch data: sqlite3 stderr: %s", err)
    return stdout.decode("utf-8", errors="replace")


def _local_path(container_path: str, container_root: str, local_root: str) -> str | None:
    """Rewrite a Jellyfin container path to its path on this machine."""
    if not container_path.startswith(container_root):
        return None
    return local_root + container_path[len(container_root):]


def parse_watch_output(
    output: str, item_paths: list[str], container_root: str, local_root: str
) -> tuple[dict[str, WatchStats], int]:
    """Fold the query output onto the given title paths.

    Each line names a playable file; the row it belongs to is the nearest
    ancestor that is one of `item_paths`. Walking up from the file is what makes
    this independent of library layout — a movie file two levels under its
    category, an episode in a `Season 01` folder and an episode sitting loose in
    the series folder all land on the right row without the layout being
    described anywhere.

    Returns the stats by original item path, and the number of playable files
    that matched no row (titles Jellyfin knows and the scan did not, or the
    other way round).
    """
    # NFC form of each row's path, mapped back to the caller's own string so the
    # result can be looked up with the untouched path.
    index: dict[str, str] = {_nfc(p): p for p in item_paths}
    accum: dict[str, _Accum] = {}
    unmatched = 0
    # Walking the same directory chain repeatedly is the bulk of the work on a
    # 16,000-file library, so remember what each parent resolved to.
    resolved: dict[str, str | None] = {}

    def owner(file_path: str) -> str | None:
        """The row that this playable file belongs to, if any."""
        parent = str(Path(file_path).parent)
        if parent in resolved:
            return resolved[parent]
        chain: list[str] = []
        node = parent
        found: str | None = None
        while True:
            if node in resolved:
                found = resolved[node]
                break
            hit = index.get(_nfc(node))
            if hit is not None:
                found = hit
                break
            chain.append(node)
            up = str(Path(node).parent)
            if up == node:
                break
            node = up
        for step in chain:
            resolved[step] = found
        resolved[parent] = found
        return found

    for line in output.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        kind, container_path = fields[0], fields[1]
        local = _local_path(container_path, container_root, local_root)
        if local is None:
            unmatched += 1
            continue
        key = owner(local)
        if key is None:
            unmatched += 1
            continue
        acc = accum.setdefault(key, _Accum())
        if kind == "L":
            acc.parts_total += 1
            continue
        if kind != "A" or len(fields) < 5:
            continue
        user, plays_raw, last_raw = fields[2], fields[3], fields[4]
        acc.played_parts.add(local)
        acc.users.add(user)
        try:
            acc.plays += int(plays_raw or 0)
        except ValueError:
            pass
        if last_raw > acc.last:
            acc.last = last_raw

    stats: dict[str, WatchStats] = {}
    for key, acc in accum.items():
        stats[key] = WatchStats(
            parts_total=acc.parts_total,
            parts_played=len(acc.played_parts),
            plays=acc.plays,
            watchers=len(acc.users),
            last_played_ts=_parse_jellyfin_date(acc.last),
        )
    return stats, unmatched


async def fetch_watch_data(config, item_paths: list[str]) -> tuple[dict[str, WatchStats], int]:
    """Read watch data for the given title paths. Raises WatchDataUnavailable."""
    started = time.monotonic()
    output = await _run_query(config)
    stats, unmatched = parse_watch_output(
        output,
        item_paths,
        config.jellyfin_container_root.rstrip("/"),
        config.local_mount_root.rstrip("/"),
    )
    never = sum(1 for s in stats.values() if s.never_played)
    log.info(
        "Watch data: %d of %d titles matched, %d never played, %d playable files "
        "matched no row, in %.1fs",
        len(stats), len(item_paths), never, unmatched, time.monotonic() - started,
    )
    return stats, unmatched


def save_cache(stats: dict[str, WatchStats]) -> None:
    """Write the stats to disk so a later run without the NAS still has them."""
    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "titles": {
            path: {
                "parts_total": s.parts_total,
                "parts_played": s.parts_played,
                "plays": s.plays,
                "watchers": s.watchers,
                "last_played_ts": s.last_played_ts,
            }
            for path, s in stats.items()
        },
    }
    try:
        CACHE_PATH.write_text(json.dumps(payload))
    except OSError:
        log.warning("Could not write %s", CACHE_PATH, exc_info=True)


def load_cache() -> tuple[dict[str, WatchStats], str | None]:
    """Read the last saved stats. Returns ({}, None) when there are none."""
    if not CACHE_PATH.exists():
        return {}, None
    try:
        payload = json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        log.warning("Could not read %s", CACHE_PATH, exc_info=True)
        return {}, None
    stats = {
        path: WatchStats(
            parts_total=d.get("parts_total", 0),
            parts_played=d.get("parts_played", 0),
            plays=d.get("plays", 0),
            watchers=d.get("watchers", 0),
            last_played_ts=d.get("last_played_ts"),
        )
        for path, d in payload.get("titles", {}).items()
    }
    return stats, payload.get("fetched_at")
