"""taotrader/core/errors.py"""
from __future__ import annotations


class LookaheadError(Exception):
    """A strategy/feature asked the SnapshotStore for data after the engine clock."""


class ReplayDivergence(Exception):
    """Recovery re-ran decide() and got different outputs than the journal (code/config drift or nondeterminism)."""


class DecodeError(Exception):
    """Storage bytes could not be decoded; the whole snapshot is rejected (all-or-nothing)."""


class GateError(Exception):
    """Live gating refused (missing lock, bad proxy type, spec not accepted, ...)."""


class DataContractError(Exception):
    """Strategy needs finer data than the DataSource provides, or runs before its valid_from_block."""
