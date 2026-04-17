from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from send2trash import send2trash

LOG_PATH = Path(__file__).parent / "deleted.log"


def delete_items(paths: list[Path], trash_mode: bool) -> list[str]:
    """Delete or trash the given paths. Returns list of error messages."""
    errors: list[str] = []
    mode = "TRASH" if trash_mode else "DELETE"
    timestamp = datetime.now().isoformat(timespec="seconds")

    with LOG_PATH.open("a") as log:
        for path in paths:
            try:
                if trash_mode:
                    send2trash(str(path))
                else:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                log.write(f"{timestamp} {mode} {path}\n")
            except Exception as e:
                errors.append(f"{path.name}: {e}")

    return errors
