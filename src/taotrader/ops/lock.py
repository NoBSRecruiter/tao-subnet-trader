"""taotrader/ops/lock.py - single-instance lock per (mode, run_id) (WP12; DESIGN.md sections 4.7, 12.1).

`InstanceLock(path)` takes an exclusive, non-blocking OS lock on a lock file:
- Windows: `msvcrt.locking(fd, LK_NBLCK, 1)` on byte 0 (mandatory byte-range lock, released by the OS when the
  process dies, so a crash never leaves a stale lock);
- Linux/WSL: `fcntl.flock(fd, LOCK_EX | LOCK_NB)` (released when the descriptor closes or the process dies).

A second acquirer - another process, or another handle in the same process - gets `LockHeld` immediately (never
waits). The holder's pid, host, mode, run_id and start time are written to the sidecar `<lock>.info` (JSON) for
operators; it is informational only (the OS lock is the truth, and a stale .info after a crash is harmless).

Paths: `data/runs/<run_id>/<mode>.lock` (`lock_path(data_dir, run_id, mode)`). The lock is held for the whole life
of a paper, live or recovery process.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Final

__all__ = ["InstanceLock", "LockError", "LockHeld", "lock_path", "read_lock_info"]

INFO_SUFFIX: Final[str] = ".info"


class LockError(RuntimeError):
    """The lock file could not be opened or locked for a reason other than another holder."""


class LockHeld(LockError):
    """Another instance holds the lock."""

    def __init__(self, path: Path, info: dict[str, Any] | None) -> None:
        who = "" if not info else f" (pid {info.get('pid')} on {info.get('host')}, since {info.get('started_unix')})"
        super().__init__(f"another instance holds {path}{who}; only one instance per (mode, run) may run")
        self.path = path
        self.info = info


def lock_path(data_dir: str | Path, run_id: str, mode: str) -> Path:
    """data/runs/<run_id>/<mode>.lock"""
    return Path(data_dir) / "runs" / run_id / f"{mode}.lock"


def read_lock_info(path: str | Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(Path(str(path) + INFO_SUFFIX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _try_lock(fh: IO[bytes]) -> bool:
    """Exclusive non-blocking lock on the open file; False if another holder has it."""
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class InstanceLock:
    """Exclusive single-instance lock. Use as a context manager or call acquire()/release()."""

    def __init__(self, path: str | Path, *, mode: str = "", run_id: str = "") -> None:
        self.path = Path(path)
        self.mode = mode
        self.run_id = run_id
        self._fh: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> InstanceLock:
        if self._fh is not None:
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+b")  # noqa: SIM115 - held open for the lifetime of the lock
        except OSError as e:
            raise LockError(f"cannot open lock file {self.path}: {e}") from e
        try:
            if fh.seek(0, os.SEEK_END) == 0:
                fh.write(b"\0")                      # a byte to lock (msvcrt locks a byte range)
                fh.flush()
            ok = _try_lock(fh)
        except OSError as e:
            fh.close()
            raise LockError(f"cannot lock {self.path}: {e}") from e
        if not ok:
            fh.close()
            raise LockHeld(self.path, read_lock_info(self.path))
        self._fh = fh
        info = {"pid": os.getpid(), "host": socket.gethostname(), "mode": self.mode, "run_id": self.run_id,
                "started_unix": int(time.time())}
        with contextlib.suppress(OSError):
            Path(str(self.path) + INFO_SUFFIX).write_text(json.dumps(info, sort_keys=True), encoding="utf-8")
        return self

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        _unlock(fh)
        fh.close()
        with contextlib.suppress(OSError):
            Path(str(self.path) + INFO_SUFFIX).unlink()

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.release()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.release()
