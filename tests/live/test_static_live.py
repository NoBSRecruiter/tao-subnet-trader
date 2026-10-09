"""Static gates for the live adapter (DESIGN.md 4.2, 9.8 #2 and #4, 10.6; WP11 acceptance):

- bittensor is imported nowhere outside src/taotrader/live/ (src, tools and tests other than the Linux contract test),
  and inside live/ only by sdk_port.load_bittensor();
- no forbidden intent (UnstakeAll, UnstakeAllAlpha, TransferStake, Batch) is referenced ANYWHERE in the code base, as an
  identifier, attribute, import or string constant (getattr tricks included);
- live/ names no bittensor intent other than AddStakeLimit, RemoveStakeLimit and MoveStake;
- live/ never passes 'all', never sets allow_raw_calls=True, never touches remove_stake_full_limit as a call, and uses
  U64_MAX only to refuse amounts (inside a comparison);
- SdkPort writes (submit_shielded / submit_plain) are called only from live/venue.py.
"""
from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "taotrader"
LIVE = SRC / "live"
FORBIDDEN = {"UnstakeAll", "UnstakeAllAlpha", "TransferStake", "Batch"}
OTHER_INTENTS = {"AddStake", "RemoveStake", "SwapStake", "MoveSwapStake", "AddProxy", "RemoveProxy", "RemoveProxies",
                 "CreatePureProxy", "ExecuteProxyAnnounced", "ClaimRootWithHotkey", "SwapBasket", "RemoveStakeFullLimit"}
ALLOWED_INTENTS = {"AddStakeLimit", "RemoveStakeLimit", "MoveStake"}
CONTRACT_TEST = ROOT / "tests" / "live" / "test_sdk_contract.py"
SELF = Path(__file__).resolve()


def py_files(*bases: Path) -> Iterator[Path]:
    for base in bases:
        yield from sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def tree(p: Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def docstring_nodes(t: ast.Module) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(t):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def _is_bt_import(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] == "bittensor" for a in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.level == 0 and (node.module or "").split(".")[0] == "bittensor"
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
        fn = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        return fn in ("import_module", "__import__") and str(node.args[0].value).startswith("bittensor")
    return False


def imports_bittensor(t: ast.Module) -> list[int]:
    return [getattr(node, "lineno", 0) for node in ast.walk(t) if _is_bt_import(node)]


def names_in(t: ast.Module) -> Iterator[tuple[str, int]]:
    skip = docstring_nodes(t)
    for node in ast.walk(t):
        if isinstance(node, ast.Name):
            yield node.id, node.lineno
        elif isinstance(node, ast.Attribute):
            yield node.attr, node.lineno
        elif isinstance(node, ast.alias):
            yield node.name.split(".")[-1], getattr(node, "lineno", 0)
            if node.asname:
                yield node.asname, getattr(node, "lineno", 0)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node.lineno
        elif isinstance(node, ast.keyword) and node.arg:
            yield node.arg, getattr(node, "lineno", 0)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.value, node.lineno


def test_bittensor_is_imported_nowhere_outside_live() -> None:
    for p in py_files(SRC, ROOT / "tools", ROOT / "tests"):
        if p.resolve() in (CONTRACT_TEST, SELF):
            continue
        lines = imports_bittensor(tree(p))
        if p.is_relative_to(LIVE):
            if p.name == "sdk_port.py":
                assert len(lines) == 1, f"live/sdk_port.py must import bittensor exactly once (load_bittensor): {lines}"
            else:
                assert not lines, f"{p.relative_to(ROOT)} imports bittensor (only sdk_port.load_bittensor may)"
        else:
            assert not lines, f"{p.relative_to(ROOT)}:{lines} imports bittensor outside taotrader.live"


def test_the_only_bittensor_import_is_inside_load_bittensor() -> None:
    t = tree(LIVE / "sdk_port.py")
    fn = next(n for n in ast.walk(t) if isinstance(n, ast.FunctionDef) and n.name == "load_bittensor")
    assert imports_bittensor(ast.Module(body=fn.body, type_ignores=[]))
    top: list[ast.stmt] = [n for n in t.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not imports_bittensor(ast.Module(body=top, type_ignores=[])), "bittensor must be imported lazily"


def test_forbidden_intents_are_referenced_nowhere() -> None:
    for p in py_files(SRC, ROOT / "tools"):
        for name, line in names_in(tree(p)):
            assert name not in FORBIDDEN, f"{p.relative_to(ROOT)}:{line} references the forbidden intent {name}"


def test_live_names_only_the_three_allowed_intents() -> None:
    for p in py_files(LIVE):
        for name, line in names_in(tree(p)):
            assert name not in OTHER_INTENTS, f"{p.relative_to(ROOT)}:{line} names the intent {name}"
    used = {n for n, _ in names_in(tree(LIVE / "sdk_port.py"))}
    assert used >= ALLOWED_INTENTS


def test_live_never_sends_all_raw_calls_or_call_103() -> None:
    for p in py_files(LIVE):
        t = tree(p)
        skip = docstring_nodes(t)
        for node in ast.walk(t):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                assert node.value.strip().lower() != "all", f"{p.relative_to(ROOT)}:{node.lineno} uses 'all'"
            if isinstance(node, ast.keyword) and node.arg == "allow_raw_calls":
                assert isinstance(node.value, ast.Constant) and node.value.value is False, \
                    f"{p.relative_to(ROOT)}:{node.lineno} allow_raw_calls must be False"
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("remove_stake_full_limit", "unstake_all", "unstake_all_alpha", "transfer_stake"), \
                    f"{p.relative_to(ROOT)}:{node.lineno} touches {node.attr}"
            if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
                assert node.value < 2**64 - 1, f"{p.relative_to(ROOT)}:{node.lineno} u64::MAX literal"


def test_u64_max_is_only_used_to_refuse_amounts() -> None:
    for p in py_files(LIVE):
        t = tree(p)
        compares = {id(n) for c in ast.walk(t) if isinstance(c, ast.Compare) for n in ast.walk(c)}
        for node in ast.walk(t):
            if isinstance(node, ast.Name) and node.id == "U64_MAX":
                assert id(node) in compares, f"{p.relative_to(ROOT)}:{node.lineno} uses U64_MAX outside a comparison"


def test_sdk_writes_are_called_only_from_the_live_venue() -> None:
    for p in py_files(SRC):
        for node in ast.walk(tree(p)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("submit_shielded", "submit_plain")):
                assert p.relative_to(SRC).as_posix() in ("live/venue.py", "live/sdk_port.py"), \
                    f"{p.relative_to(ROOT)}:{node.lineno} calls {node.func.attr}"
