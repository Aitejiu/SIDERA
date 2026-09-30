"""A small, provider-free run lock shared by local lifecycle tools."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

LOCK_DIRECTORY_NAME = ".sidera-maintenance.lock"
OWNER_FILE_NAME = "owner.json"


class MaintenanceLockBusy(RuntimeError):
    """Another local lifecycle or maintenance operation owns the run lock."""


def maintenance_lock_path(run_root: Path) -> Path:
    run_root = run_root.expanduser()
    if not run_root.is_absolute():
        raise ValueError("run_root_must_be_absolute")
    return run_root / LOCK_DIRECTORY_NAME


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_owner(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads((path / OWNER_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _remove_stale_lock(path: Path) -> bool:
    owner = _read_owner(path)
    if owner is None:
        return False
    raw_pid = owner.get("pid")
    if isinstance(raw_pid, bool) or not isinstance(raw_pid, (int, str)):
        return False
    try:
        pid = int(raw_pid)
    except ValueError:
        return False
    if _pid_is_alive(pid):
        return False
    try:
        (path / OWNER_FILE_NAME).unlink()
        path.rmdir()
    except OSError:
        return False
    return True


@contextmanager
def maintenance_lock(run_root: Path, *, owner: str) -> Iterator[None]:
    """Acquire a non-blocking, recoverable lock for one isolated run.

    The directory creation is the lock acquisition primitive, which is
    available on macOS and is also easy for shell lifecycle scripts to share.
    A live owner always wins; malformed or ambiguous lock state fails closed
    without touching any run data.
    """

    run_root = run_root.expanduser()
    if not run_root.is_absolute():
        raise ValueError("run_root_must_be_absolute")
    run_root.mkdir(parents=True, exist_ok=True, mode=stat.S_IRWXU)
    os.chmod(run_root, stat.S_IRWXU)
    path = maintenance_lock_path(run_root)
    try:
        path.mkdir(mode=stat.S_IRWXU)
    except FileExistsError:
        if not _remove_stale_lock(path):
            raise MaintenanceLockBusy("maintenance_busy")
        path.mkdir(mode=stat.S_IRWXU)
    os.chmod(path, stat.S_IRWXU)
    owner_path = path / OWNER_FILE_NAME
    owner_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "owner": owner,
                "acquired_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(owner_path, stat.S_IRUSR | stat.S_IWUSR)
    try:
        yield
    finally:
        current = _read_owner(path)
        if current is not None and current.get("pid") == os.getpid():
            try:
                owner_path.unlink()
                path.rmdir()
            except OSError:
                pass


__all__ = [
    "LOCK_DIRECTORY_NAME",
    "MaintenanceLockBusy",
    "maintenance_lock",
    "maintenance_lock_path",
]
