"""WP9 purity lint (DESIGN.md sections 4.2, 4.6, 10.6) on the strategies package, plus determinism and the Strategy
protocol shape of every implementation. tests/core/test_static_gates.py runs the same gates over the whole tree."""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from taotrader.core import codec
from taotrader.core.protocols import Strategy
from taotrader.core.units import StrategyId
from taotrader.strategies.baselines import BASELINES
from taotrader.strategies.carry import CarryStrategy
from taotrader.strategies.launch_lcw import LcwStrategy
from taotrader.strategies.momentum import MomentumStrategy

PKG = Path(__file__).resolve().parents[2] / "src" / "taotrader" / "strategies"
FILES = sorted(PKG.glob("*.py"))
BANNED = {"time", "datetime", "random", "os", "uuid", "asyncio", "httpx", "websockets", "numpy", "duckdb", "bittensor"}
ALLOWED_INTERNAL = {"core", "protocol", "features", "strategies"}
BANNED_LITERALS = {0.18, 0.41, 2952, 14_400, 720_000}
LIVE_ONLY = {"UnstakeAll", "TransferStake", "Batch"}


def _tree(p: Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def test_the_owned_modules_exist() -> None:
    assert {p.name for p in FILES} >= {"__init__.py", "base.py", "carry.py", "momentum.py", "launch_lcw.py", "baselines.py"}


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_imports_are_pure_and_inward(path: Path) -> None:
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                assert root not in BANNED and root in stdlib, f"{path.name} imports {a.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                root = (node.module or "").split(".")[0]
                assert root not in BANNED and root in stdlib, f"{path.name} imports from {node.module}"
            else:
                target = (node.module or "").split(".")[0] if node.level == 2 else "strategies"
                assert target in ALLOWED_INTERNAL, f"{path.name}: from {'.' * node.level}{node.module} import ..."


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_no_unsorted_set_iteration_normalize_frozen_literals_or_live_names(path: Path) -> None:
    def is_set(e: ast.expr) -> bool:
        return isinstance(e, (ast.Set, ast.SetComp)) or (
            isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id in ("set", "frozenset"))

    for node in ast.walk(_tree(path)):
        iters: list[ast.expr] = []
        if isinstance(node, (ast.For, ast.AsyncFor)):
            iters.append(node.iter)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            iters.extend(g.iter for g in node.generators)
        assert not any(is_set(it) for it in iters), f"{path.name}:{getattr(node, 'lineno', '?')} iterates a set"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "normalize", f"{path.name}:{node.lineno} calls .normalize()"
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            assert node.value not in BANNED_LITERALS, f"{path.name}:{node.lineno} literal {node.value!r}"
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
        assert name not in LIVE_ONLY, f"{path.name} references {name}"


def _all_strategies() -> list[Strategy]:
    out: list[Strategy] = [CarryStrategy(), MomentumStrategy({"b_s_per_z_day": 0.004}), LcwStrategy({"enabled": True})]
    out += [cls(None, strategy_id=StrategyId(f"baseline.{n}")) for n, cls in sorted(BASELINES.items())]
    return out


def test_every_strategy_declares_the_protocol_attributes() -> None:
    for s in _all_strategies():
        assert isinstance(s.id, str) and s.id
        assert isinstance(s.decide_every_blocks, int) and s.decide_every_blocks >= 1
        assert isinstance(s.wake_on, frozenset)
        assert isinstance(s.min_cadence_blocks, int) and s.min_cadence_blocks >= 1
        assert isinstance(s.valid_from_block, int)
        assert isinstance(s.declares_dilution, bool)
        assert callable(s.initial_memory) and callable(s.on_tick)


def test_identical_inputs_give_byte_identical_outputs(sx: SimpleNamespace) -> None:
    specs = [sx.Spec(10 + i, reg_at=8_000_000 + i, feat={"ret_1d": 0.001 * (i + 1), "ret_7d": 0.01 * (i + 1),
                                                          "flow_1d": 0.001 * (i + 1)}) for i in range(8)]
    m = sx.market(specs)
    for s in _all_strategies():
        a = s.on_tick(m.ctx(sid=str(s.id)), s.initial_memory())
        b = s.on_tick(m.ctx(sid=str(s.id)), s.initial_memory())
        assert codec.canonical_bytes(a.signals) == codec.canonical_bytes(b.signals), s.id
        assert codec.canonical_bytes(a.memory) == codec.canonical_bytes(b.memory), s.id
        assert all(sig.strategy == s.id for sig in a.signals)
        back = codec.decode_bytes(type(s.initial_memory()), codec.canonical_bytes(a.memory))
        assert back == a.memory, s.id
