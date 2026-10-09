"""taotrader/engine/control.py - operator control files -> journaled OperatorCommand inputs (WP7; DESIGN.md 3.11, 9.8 #14).

The control directory (default `data/control/`) is the operator's only write path into a running engine:
- `KILL`: while this file exists every book stays halted. The watcher journals `halt` (nonce
  "kill-<sha256 of the file>-<resume count>") whenever the books would otherwise not be halted, AFTER any command
  files of the same poll, so a `resume` while the file still exists is overridden in the same batch: removing the
  file is part of resuming.
- `*.json` command files, written atomically by `write_command` (the CLI's `halt`, `resume`, `exits-only`,
  `flatten`): {"command": "halt" | "resume" | "exits_only" | "flatten:<netuid>", "reason": "...", "nonce": "..."}.
  The nonce makes re-delivery idempotent: the OperatorCommand idempotency key is "op:<nonce>", and a file whose
  nonce is already journaled is archived without being journaled again. Within one poll the resumes are journaled
  first and the restrictive commands after them, so a halt (or exits_only / flatten) that arrives together with a
  resume always wins, whatever the file names.

`poll()` reads (never deletes) and returns OperatorCommands for the Runner to journal in the next tick batch
(run-level, Phase.INGEST, so every book folds them). After the batch commits, `ack()` moves the processed files to
`done/`. A malformed file is moved to `rejected/` and logged; nothing in this module ever raises into the tick loop
(I/O errors are logged and retried on the next poll).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from ..core.events import OperatorCommand
from ..core.units import Block

__all__ = ["COMMANDS", "DONE_DIR", "KILL_FILE", "REJECTED_DIR", "ControlWatcher", "validate_command", "write_command"]

log = logging.getLogger("taotrader.engine.control")

COMMANDS: Final[tuple[str, ...]] = ("halt", "resume", "exits_only")    # plus "flatten:<netuid>"
KILL_FILE: Final[str] = "KILL"
DONE_DIR: Final[str] = "done"
REJECTED_DIR: Final[str] = "rejected"
MAX_REASON: Final[int] = 200
_NONCE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def validate_command(command: str) -> str:
    """The canonical command text; ValueError for anything else."""
    c = command.strip()
    if c in COMMANDS:
        return c
    head, sep, tail = c.partition(":")
    if head == "flatten" and sep and tail.isdigit() and 0 <= int(tail) < 65_536:
        return f"flatten:{int(tail)}"
    raise ValueError(f"unknown operator command {command!r} (expected {', '.join(COMMANDS)} or flatten:<netuid>)")


def _validate_nonce(nonce: str) -> str:
    if not _NONCE_RE.fullmatch(nonce):
        raise ValueError(f"bad nonce {nonce!r}: 1-128 characters of [A-Za-z0-9_.:-]")
    return nonce


def write_command(directory: str | Path, command: str, reason: str = "", nonce: str | None = None) -> Path:
    """Atomically write a command file (CLI side). Returns its path. The nonce defaults to a random UUID."""
    cmd = validate_command(command)
    n = _validate_nonce(nonce if nonce is not None else uuid.uuid4().hex)
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"command": cmd, "reason": reason[:MAX_REASON], "nonce": n}, sort_keys=True)
    path = d / f"cmd-{n.replace(':', '_')}.json"
    tmp = d / f".{path.name}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


class ControlWatcher:
    """Reads the control directory once per tick (Runner) and turns it into OperatorCommand inputs."""

    def __init__(self, directory: str | Path, *, kill_file: str = KILL_FILE) -> None:
        self.dir = Path(directory)
        self.kill_file = kill_file
        self._pending: dict[str, Path] = {}          # nonce -> file, awaiting ack() after the commit
        self.errors = 0

    def poll(self, block: Block, *, halted: bool, resume_count: int,
             is_new: Callable[[str], bool] = lambda idem: True) -> list[OperatorCommand]:
        """Commands to journal at `block`. `is_new(idem)` tells whether "op:<nonce>" is not yet in the journal."""
        out: list[OperatorCommand] = []
        seen: set[str] = set()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.dir.glob("*.json")):
                parsed = self._read(path)
                if parsed is None:
                    continue
                cmd, reason, nonce = parsed
                if nonce in seen:
                    continue
                seen.add(nonce)
                if not is_new(f"op:{nonce}"):
                    self._move(path, DONE_DIR)             # journaled earlier (crash before the archive step)
                    continue
                out.append(OperatorCommand(block=block, command=cmd, reason=reason, nonce=nonce))
                self._pending[nonce] = path
            # Files are listed by name, i.e. by their (random) nonce, not by when they were written, so the order of
            # two commands that arrive within one poll is unknown. Fail closed: every resume of this poll is applied
            # BEFORE the restrictive commands (halt, exits_only, flatten), so a resume can never undo a halt that
            # arrived with it (section 9.8 #14). A deliberate resume after a halt needs a later poll (one block).
            out.sort(key=lambda c: 0 if c.command == "resume" else 1)
            # the kill file is checked AFTER the command files, so a resume in this same batch is overridden at once
            resumes = sum(1 for c in out if c.command == "resume")
            effective = halted
            for c in out:
                effective = True if c.command == "halt" else False if c.command == "resume" else effective
            kill = self.dir / self.kill_file
            if kill.is_file() and not effective:
                nonce = f"kill-{hashlib.sha256(kill.read_bytes()).hexdigest()[:16]}-{resume_count + resumes}"
                if nonce not in seen and is_new(f"op:{nonce}"):
                    out.append(OperatorCommand(block=block, command="halt", reason="kill file present", nonce=nonce))
        except OSError as e:
            self.errors += 1
            log.warning("control directory %s unreadable: %s", self.dir, e)
        return out

    def ack(self, commands: Sequence[OperatorCommand]) -> None:
        """Archive the files of commands that are now journaled."""
        for c in commands:
            path = self._pending.pop(c.nonce, None)
            if path is not None:
                self._move(path, DONE_DIR)

    def _read(self, path: Path) -> tuple[str, str, str] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) - {"command", "reason", "nonce"}:
                raise ValueError("expected an object with command, reason and nonce")
            cmd = validate_command(str(data["command"]))
            nonce = _validate_nonce(str(data["nonce"]))
            reason = str(data.get("reason", ""))[:MAX_REASON]
        except (OSError, ValueError, KeyError) as e:
            self.errors += 1
            log.warning("rejected control file %s: %s", path.name, e)
            self._move(path, REJECTED_DIR)
            return None
        return cmd, reason, nonce

    def _move(self, path: Path, sub: str) -> None:
        try:
            dest = self.dir / sub
            dest.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest / path.name)
        except OSError as e:                         # retried on the next poll (Windows: a reader may hold it open)
            self.errors += 1
            log.warning("could not archive control file %s: %s", path.name, e)
