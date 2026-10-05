"""Private, crash-safe JSON storage.

Files are written atomically (temp file + fsync + rename) with 0600
permissions, and reads refuse files that other users could have tampered with.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Optional


class StorageError(RuntimeError):
    pass


def _check_private(path: Path, expect_dir: bool) -> None:
    info = path.lstat()
    kind_ok = stat.S_ISDIR(info.st_mode) if expect_dir else stat.S_ISREG(info.st_mode)
    if not kind_ok or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise StorageError(f"{path} must be owned by you and not accessible to other users (chmod 700/600).")


def private_dir(path: Path) -> Path:
    # mkdir(parents=True) would create missing ancestors with default
    # permissions, so create each missing level as 0700 ourselves.
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    _check_private(path, expect_dir=True)
    return path


def read_json(path: Path) -> Optional[dict]:
    try:
        _check_private(path, expect_dir=False)
    except FileNotFoundError:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
