from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from send2trash import send2trash

log = logging.getLogger("deleter")

LOG_PATH = Path(__file__).parent / "deleted.log"

MODE_NAS_TRASH = "nas_trash"
MODE_LOCAL_TRASH = "local_trash"
MODE_PERMANENT = "permanent"
DELETE_MODES = (MODE_NAS_TRASH, MODE_LOCAL_TRASH, MODE_PERMANENT)


@dataclass
class DeleteResult:
    """Outcome of one delete attempt.

    Callers must check `ok` before removing the item from any view. Previously
    the UI dropped rows unconditionally, so a failed delete looked like it had
    worked.
    """

    path: Path
    ok: bool
    mode: str
    detail: str = ""

    @property
    def error_line(self) -> str:
        return f"{self.path.name}: {self.detail}"


def _refuse_reason(path: Path, config) -> str | None:
    """Why this path must not be deleted, or None if it may be.

    Every row the scanner produces is at least <library folder>/<title> below
    media_root. Anything shallower is a library folder or the media root
    itself, and deleting it would take out a whole library in one go.
    """
    media_root = Path(os.path.abspath(config.media_root))
    candidate = Path(os.path.abspath(path))
    try:
        rel = candidate.relative_to(media_root)
    except ValueError:
        return f"refusing: not inside media_root {media_root}"
    if len(rel.parts) < 2:
        return f"refusing: {candidate} is a library folder or the media root, not a title"
    return None


def nas_relative_path(path: Path, local_root: str) -> str:
    """Path of `path` relative to the share root, as the NAS-side script expects.

    The SMB share `nas-share` is `/mnt/pool/nas-share` on the server, so
    `/Volumes/nas-share/media/TV Shows/X` becomes `media/TV Shows/X`, which the
    script joins onto nas_media_root. The last component is not resolved, so a
    symlink is never swapped for its target.
    """
    local = Path(os.path.abspath(path))
    local = local.parent.resolve() / local.name
    return local.relative_to(Path(local_root).resolve()).as_posix()


# Runs on the NAS under python3, fed over SSH stdin. Kept self-contained and
# stdlib-only. Prints one JSON line per item.
#
# Filenames are matched per path component by NFC form: macOS presents names on
# an SMB mount in decomposed (NFD) form, while the NAS stores them composed
# (NFC), so "Amélie (2001)" as seen on the Mac is a different byte string from
# the directory on the NAS and a plain `mv` cannot find it.
#
# Each item is renamed to <trash>/<stamp>/<its path relative to the share>, so
# two titles with the same folder name in different categories cannot collide,
# and the original location is visible in the trash for restoring. rename()
# never copies: if the trash is on another dataset it fails with EXDEV instead
# of silently turning into a full copy. After the rename the source must be
# gone and the destination present, otherwise the item is reported as failed.
_NAS_SCRIPT = r'''
import errno, json, os, sys, unicodedata

req = json.loads(PAYLOAD)
root = req["root"]
dest_root = req["dest"]

def nfc(s):
    return unicodedata.normalize("NFC", s)

def locate(rel):
    cur = root
    for part in rel.split("/"):
        cand = os.path.join(cur, part)
        if os.path.lexists(cand):
            cur = cand
            continue
        try:
            names = os.listdir(cur)
        except OSError as e:
            return None, "cannot list %s: %s" % (cur, e)
        want = nfc(part)
        matches = [n for n in names if nfc(n) == want]
        if not matches:
            return None, "not found on NAS: %s" % cand
        if len(matches) > 1:
            return None, "ambiguous on NAS (%d names match): %s" % (len(matches), cand)
        cur = os.path.join(cur, matches[0])
    return cur, ""

for i, rel in enumerate(req["items"]):
    res = {"i": i}
    try:
        src, err = locate(rel)
        if src is None:
            res.update(ok=False, detail=err)
        else:
            dst = os.path.join(dest_root, os.path.relpath(src, root))
            if os.path.lexists(dst):
                res.update(ok=False, detail="already exists in trash: %s" % dst)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.rename(src, dst)
                if os.path.lexists(src) or not os.path.lexists(dst):
                    res.update(ok=False, detail="rename returned but verification failed (src still present or dst missing)")
                else:
                    res.update(ok=True, src=src, dst=dst)
    except OSError as e:
        detail = "%s: %s" % (type(e).__name__, e)
        if e.errno == errno.EXDEV:
            detail += " (trash dir is on a different dataset; refusing to copy)"
        res.update(ok=False, detail=detail)
    except Exception as e:
        res.update(ok=False, detail="%s: %s" % (type(e).__name__, e))
    print(json.dumps(res), flush=True)
'''


def _nas_trash(paths: list[Path], config) -> list[DeleteResult]:
    """Move items into a trash directory on the NAS, over SSH.

    The trash directory must live on the same ZFS dataset as the media so the
    move is a rename rather than a copy: instant, and it frees no space until
    the trash is purged, which is what makes it recoverable. macOS cannot trash
    on this SMB mount at all ("The volume doesn't have a trash"), so this is
    the only recoverable mode available.
    """
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = f"{config.nas_trash_dir.rstrip('/')}/{stamp}"

    results: dict[int, DeleteResult] = {}
    rels: list[str] = []
    index_of: list[int] = []
    for i, p in enumerate(paths):
        try:
            rel = nas_relative_path(p, config.local_mount_root)
        except (ValueError, OSError) as e:
            results[i] = DeleteResult(p, False, MODE_NAS_TRASH, f"path is outside local_mount_root ({e})")
            continue
        rels.append(rel)
        index_of.append(i)

    if rels:
        payload = json.dumps({"root": config.nas_media_root.rstrip("/"), "dest": dest, "items": rels})
        script = f"PAYLOAD = {payload!r}\n{_NAS_SCRIPT}"
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", config.nas_ssh, "python3", "-"]
        log.info("NAS trash: moving %d item(s) to %s via %s", len(rels), dest, config.nas_ssh)
        for rel in rels:
            log.debug("NAS trash request: %s", rel)
        reported: dict[int, dict] = {}
        try:
            proc = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=300)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.error("NAS trash: ssh failed: %s: %s", type(e).__name__, e)
            failure = f"ssh failed: {type(e).__name__}: {e}"
        else:
            log.info("NAS trash: ssh exit code %s", proc.returncode)
            if proc.stderr.strip():
                log.warning("NAS trash: ssh stderr: %s", proc.stderr.strip())
            for line in proc.stdout.splitlines():
                try:
                    rec = json.loads(line)
                    reported[int(rec["i"])] = rec
                except (ValueError, KeyError, TypeError):
                    log.warning("NAS trash: unexpected output line: %r", line)
            failure = proc.stderr.strip() or f"no result reported (ssh exit code {proc.returncode})"

        for j, i in enumerate(index_of):
            rec = reported.get(j)
            if rec is None:
                results[i] = DeleteResult(paths[i], False, MODE_NAS_TRASH, failure)
            elif rec.get("ok"):
                results[i] = DeleteResult(paths[i], True, MODE_NAS_TRASH, rec.get("dst", dest))
            else:
                results[i] = DeleteResult(paths[i], False, MODE_NAS_TRASH, rec.get("detail", "failed"))

    return [results[i] for i in range(len(paths))]


def _local_trash(paths: list[Path], config) -> list[DeleteResult]:
    """macOS / desktop trash via send2trash. Fails on SMB volumes."""
    results = []
    for path in paths:
        try:
            send2trash(str(path))
            results.append(DeleteResult(path, True, MODE_LOCAL_TRASH))
        except Exception as e:
            log.exception("local trash failed for %s", path)
            results.append(DeleteResult(path, False, MODE_LOCAL_TRASH, f"{type(e).__name__}: {e}"))
    return results


def _permanent(paths: list[Path], config) -> list[DeleteResult]:
    """Unrecoverable delete. Media is in no backup, so there is no undo."""
    results = []
    for path in paths:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            if os.path.lexists(path):
                results.append(DeleteResult(path, False, MODE_PERMANENT, "delete returned but path still exists"))
            else:
                results.append(DeleteResult(path, True, MODE_PERMANENT))
        except Exception as e:
            log.exception("permanent delete failed for %s", path)
            results.append(DeleteResult(path, False, MODE_PERMANENT, f"{type(e).__name__}: {e}"))
    return results


_BACKENDS = {
    MODE_NAS_TRASH: _nas_trash,
    MODE_LOCAL_TRASH: _local_trash,
    MODE_PERMANENT: _permanent,
}


def delete_items(paths: list[Path], config) -> list[DeleteResult]:
    """Delete the given paths using config.delete_mode.

    Returns one result per path, in order. Every attempt is written to
    deleted.log (the audit trail) and to mediacleaner.log, successes and
    failures alike. A backend that raises is caught here and reported as a
    failure for every path it was given, so the caller always gets results.
    """
    if not paths:
        return []

    mode = getattr(config, "delete_mode", MODE_PERMANENT)
    log.info("Delete requested: %d item(s), mode=%s", len(paths), mode)

    results: list[DeleteResult | None] = [None] * len(paths)
    allowed: list[int] = []
    for i, p in enumerate(paths):
        reason = _refuse_reason(p, config)
        if reason:
            results[i] = DeleteResult(p, False, mode, reason)
        else:
            allowed.append(i)

    backend = _BACKENDS.get(mode)
    if backend is None:
        for i in allowed:
            results[i] = DeleteResult(paths[i], False, mode, f"unknown delete_mode '{mode}'")
    elif allowed:
        try:
            backend_results = backend([paths[i] for i in allowed], config)
        except Exception as e:
            log.exception("delete backend %s crashed", mode)
            backend_results = [
                DeleteResult(paths[i], False, mode, f"internal error: {type(e).__name__}: {e}") for i in allowed
            ]
        for i, r in zip(allowed, backend_results):
            results[i] = r

    final = [r for r in results if r is not None]

    timestamp = datetime.now().isoformat(timespec="seconds")
    try:
        with LOG_PATH.open("a") as audit:
            for r in final:
                verb = r.mode.upper()
                if r.ok:
                    extra = f" -> {r.detail}" if r.detail else ""
                    audit.write(f"{timestamp} {verb} OK {r.path}{extra}\n")
                else:
                    audit.write(f"{timestamp} {verb} FAIL {r.path} :: {r.detail}\n")
    except OSError:
        log.exception("could not write audit log %s", LOG_PATH)

    for r in final:
        if r.ok:
            log.info("Delete OK (%s): %s%s", r.mode, r.path, f" -> {r.detail}" if r.detail else "")
        else:
            log.error("Delete FAILED (%s): %s :: %s", r.mode, r.path, r.detail)
    log.info(
        "Delete finished: %d ok, %d failed", sum(r.ok for r in final), sum(not r.ok for r in final)
    )
    return final
