"""engine.control: operator control files -> OperatorCommand inputs (DESIGN.md 3.11, 9.8 #14)."""
from __future__ import annotations

import json

import pytest

from taotrader.core.units import Block
from taotrader.engine.control import ControlWatcher, validate_command, write_command

B = Block(9_000_000)


def test_validate_command() -> None:
    assert validate_command(" halt ") == "halt"
    assert validate_command("flatten:007") == "flatten:7"
    for bad in ("HALT", "flatten:", "flatten:-1", "flatten:70000", "sell_all", ""):
        with pytest.raises(ValueError):
            validate_command(bad)


def test_write_command_is_atomic_and_canonical(tmp_path) -> None:
    p = write_command(tmp_path / "ctl", "exits_only", "drill", nonce="n-1")
    assert p.name == "cmd-n-1.json" and json.loads(p.read_text(encoding="utf-8")) == {
        "command": "exits_only", "nonce": "n-1", "reason": "drill"}
    assert not list((tmp_path / "ctl").glob(".*.tmp"))
    auto = write_command(tmp_path / "ctl", "halt")
    assert len(json.loads(auto.read_text(encoding="utf-8"))["nonce"]) == 32
    with pytest.raises(ValueError):
        write_command(tmp_path / "ctl", "halt", nonce="bad nonce!")


def test_poll_returns_new_commands_and_ack_archives_them(tmp_path) -> None:
    ctl = tmp_path / "ctl"
    write_command(ctl, "halt", "a", nonce="1")
    write_command(ctl, "flatten:9", "b", nonce="2")
    w = ControlWatcher(ctl)
    cmds = w.poll(B, halted=False, resume_count=0)
    assert [(c.command, c.nonce, c.block) for c in cmds] == [("halt", "1", B), ("flatten:9", "2", B)]
    assert cmds[0].idem() == "op:1"
    assert len(w.poll(B, halted=False, resume_count=0)) == 2          # still pending until acked (re-delivery)
    w.ack(cmds)
    assert sorted(p.name for p in (ctl / "done").iterdir()) == ["cmd-1.json", "cmd-2.json"]
    assert w.poll(B, halted=True, resume_count=0) == []


def test_already_journaled_commands_are_archived_not_repeated(tmp_path) -> None:
    ctl = tmp_path / "ctl"
    write_command(ctl, "resume", "x", nonce="old")
    w = ControlWatcher(ctl)
    assert w.poll(B, halted=False, resume_count=0, is_new=lambda idem: idem != "op:old") == []
    assert (ctl / "done" / "cmd-old.json").exists()


def test_malformed_files_are_rejected(tmp_path) -> None:
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    (ctl / "a.json").write_text("{not json", encoding="utf-8")
    (ctl / "b.json").write_text(json.dumps({"command": "sell_everything", "nonce": "x"}), encoding="utf-8")
    (ctl / "c.json").write_text(json.dumps({"command": "halt", "nonce": "y", "extra": 1}), encoding="utf-8")
    w = ControlWatcher(ctl)
    assert w.poll(B, halted=False, resume_count=0) == []
    assert sorted(p.name for p in (ctl / "rejected").iterdir()) == ["a.json", "b.json", "c.json"]
    assert w.errors == 3


def test_kill_file_halts_until_removed_even_across_a_resume(tmp_path) -> None:
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    (ctl / "KILL").write_text("stop", encoding="utf-8")
    w = ControlWatcher(ctl)
    (c,) = w.poll(B, halted=False, resume_count=0)
    assert c.command == "halt" and c.nonce.startswith("kill-") and c.nonce.endswith("-0")
    assert w.poll(B, halted=True, resume_count=0) == []                # already halted: nothing new
    write_command(ctl, "resume", "operator", nonce="r1")
    cmds = w.poll(B, halted=True, resume_count=0)
    assert [x.command for x in cmds] == ["resume", "halt"] and cmds[1].nonce.endswith("-1")
    journaled = {f"op:{x.nonce}" for x in cmds}
    w.ack(cmds)
    assert w.poll(B, halted=True, resume_count=1, is_new=lambda i: i not in journaled) == []
    (ctl / "KILL").unlink()
    assert w.poll(B, halted=False, resume_count=1) == []
