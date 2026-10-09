"""ops.lock: single-instance lock per (mode, run) - msvcrt on Windows, fcntl on Linux. A second instance is refused
immediately, across handles and across processes; a killed holder never leaves a stale lock."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from taotrader.ops.lock import InstanceLock, LockHeld, lock_path, read_lock_info

ROOT = Path(__file__).resolve().parents[2]


def _holder(path: Path) -> tuple[subprocess.Popen[str], int]:
    """A child process holding the lock; returns (Popen, the interpreter's own pid). On Windows the venv's
    python.exe is a launcher, so the interpreter that holds the lock is a grandchild with another pid."""
    code = textwrap.dedent(f"""
        import os, sys, time
        sys.path.insert(0, {str(ROOT / 'src')!r})
        from taotrader.ops.lock import InstanceLock
        lk = InstanceLock({str(path)!r}, mode="paper", run_id="r1").acquire()
        print("LOCKED", os.getpid(), flush=True)
        time.sleep(60)
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout is not None
    word, pid = p.stdout.readline().split()
    assert word == "LOCKED"
    return p, int(pid)


def test_lock_path_is_per_mode_and_run(tmp_path: Path) -> None:
    assert lock_path(tmp_path, "paper-main", "paper") == tmp_path / "runs" / "paper-main" / "paper.lock"
    assert lock_path(tmp_path, "paper-main", "live") != lock_path(tmp_path, "paper-main", "paper")


def test_second_handle_in_the_same_process_is_refused(tmp_path: Path) -> None:
    p = lock_path(tmp_path, "r1", "paper")
    a = InstanceLock(p, mode="paper", run_id="r1").acquire()
    info = read_lock_info(p)
    assert info is not None and info["mode"] == "paper" and info["run_id"] == "r1"
    with pytest.raises(LockHeld, match="another instance"):
        InstanceLock(p).acquire()
    a.release()
    with InstanceLock(p) as b:                                        # free again after release
        assert b.held
    assert not InstanceLock(p).held


def test_other_process_is_refused_and_a_killed_holder_releases(tmp_path: Path) -> None:
    p = lock_path(tmp_path, "r1", "paper")
    proc, pid = _holder(p)
    try:
        with pytest.raises(LockHeld) as ei:
            InstanceLock(p).acquire()
        assert ei.value.info is not None and ei.value.info["pid"] == pid
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)                              # Windows: TerminateProcess (a hard kill)
        proc.kill()
        proc.wait(30)
    deadline = time.monotonic() + 10
    while True:                                                       # the OS drops the lock with the process
        try:
            lk = InstanceLock(p).acquire()
            break
        except LockHeld:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    lk.release()


def test_different_runs_do_not_block_each_other(tmp_path: Path) -> None:
    with InstanceLock(lock_path(tmp_path, "r1", "paper")), InstanceLock(lock_path(tmp_path, "r2", "paper")), \
            InstanceLock(lock_path(tmp_path, "r1", "live_dry")):
        pass
