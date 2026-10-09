"""Static gates (DESIGN.md sections 4.2, 4.6, 10.6), run over the whole src tree so every WP's code is covered:

- import-linter contracts (.importlinter) pass;
- core imports only the stdlib; `import taotrader.core.protocols` works in a fresh interpreter;
- pure modules (core, protocol, features, risk, portfolio, strategies, engine.engine, engine.reducer) never import
  time/datetime/random/os/uuid/asyncio/httpx/websockets and never iterate a set display / set() / frozenset() call;
- no Decimal.normalize() anywhere;
- no bittensor import and no UnstakeAll / TransferStake / Batch identifiers outside live/ (the `execute(` rule is
  not enforced statically: sqlite3's Connection.execute is legitimate in data/; see the WP0 report);
- no 0.18 / 0.41 / 2952 / 14_400 / 720_000 literals outside protocol/regimes.py;
- no float literal, float() call or true division `/` in the money modules protocol/amm.py, core/portfolio.py and
  portfolio/planner.py (Decimal work goes through core.fixed.DEC/EXACT methods; integers use //);
- the section-5 core modules match DESIGN.md (drift guard).
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "taotrader"
PURE_PACKAGES = ("core", "protocol", "features", "risk", "portfolio", "strategies")
PURE_MODULES = ("engine/engine.py", "engine/reducer.py")
BANNED_IN_PURE = {"time", "datetime", "random", "os", "uuid", "asyncio", "httpx", "websockets"}
BANNED_LITERALS = {0.18, 0.41, 2952, 14_400, 720_000}
MONEY_MODULES = ("protocol/amm.py", "core/portfolio.py", "portfolio/planner.py")
LIVE_ONLY_NAMES = {"UnstakeAll", "TransferStake", "Batch"}


def _py_files(base: Path) -> Iterator[Path]:
    yield from sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(p: Path) -> str:
    return p.relative_to(SRC).as_posix()


def _tree(p: Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


def _pure_files() -> list[Path]:
    files = [p for pkg in PURE_PACKAGES for p in _py_files(SRC / pkg)]
    files += [SRC / m for m in PURE_MODULES if (SRC / m).exists()]
    return files


def _imported_roots(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


# ------------------------------------------------------------------------------------------------- imports
def test_import_linter_contracts_pass() -> None:
    proc = subprocess.run([sys.executable, "-c", "import sys; from importlinter.cli import lint_imports; "
                           "sys.exit(lint_imports(config_filename='.importlinter', no_cache=True))"],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 broken" in proc.stdout


def test_core_protocols_imports_in_a_fresh_interpreter() -> None:
    proc = subprocess.run([sys.executable, "-c", "import taotrader.core.protocols, sys; "
                           "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
                           "{'httpx','websockets','numpy','duckdb','keyring','xxhash','zstandard','bittensor'}); "
                           "print(bad); sys.exit(1 if bad else 0)"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_core_imports_only_the_stdlib() -> None:
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    for p in _py_files(SRC / "core"):
        bad = _imported_roots(_tree(p)) - allowed
        assert not bad, f"{_rel(p)} imports non-stdlib {sorted(bad)}"


def test_pure_modules_do_not_import_io_clock_or_randomness() -> None:
    for p in _pure_files():
        bad = _imported_roots(_tree(p)) & BANNED_IN_PURE
        assert not bad, f"{_rel(p)} is pure but imports {sorted(bad)}"


def test_bittensor_and_forbidden_intents_only_in_live() -> None:
    for p in _py_files(SRC):
        if _rel(p).startswith("live/"):
            continue
        tree = _tree(p)
        assert "bittensor" not in _imported_roots(tree), f"{_rel(p)} imports bittensor"
        for node in ast.walk(tree):
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            assert name not in LIVE_ONLY_NAMES, f"{_rel(p)}:{getattr(node, 'lineno', '?')} references {name}"


# ------------------------------------------------------------------------------------------------- AST lints
def test_no_decimal_normalize() -> None:
    for p in _py_files(SRC):
        for node in ast.walk(_tree(p)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "normalize":
                target = node.func.value
                is_decimal_cls = isinstance(target, ast.Name) and target.id == "Decimal"
                assert not (is_decimal_cls or len(node.args) == 0), f"{_rel(p)}:{node.lineno} calls .normalize()"


def test_no_unsorted_set_iteration_in_pure_modules() -> None:
    def is_set_expr(e: ast.expr) -> bool:
        if isinstance(e, (ast.Set, ast.SetComp)):
            return True
        return isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id in ("set", "frozenset")

    for p in _pure_files():
        for node in ast.walk(_tree(p)):
            iters: list[ast.expr] = []
            if isinstance(node, (ast.For, ast.AsyncFor)):
                iters.append(node.iter)
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                iters.extend(g.iter for g in node.generators)
            for it in iters:
                assert not is_set_expr(it), f"{_rel(p)}:{it.lineno} iterates an unsorted set; wrap it in sorted()"


def test_no_frozen_protocol_literals() -> None:
    for p in _py_files(SRC):
        if _rel(p) == "protocol/regimes.py":
            continue
        for node in ast.walk(_tree(p)):
            if isinstance(node, ast.Constant) and not isinstance(node.value, bool) and isinstance(node.value, (int, float)):
                assert node.value not in BANNED_LITERALS, (
                    f"{_rel(p)}:{node.lineno} literal {node.value!r}: read it live (section 2.0) or use protocol/regimes.py")


def test_money_modules_avoid_float_and_true_division() -> None:
    for rel in MONEY_MODULES:
        p = SRC / rel
        if not p.exists():
            continue
        for node in ast.walk(_tree(p)):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                pytest.fail(f"{rel}:{node.lineno} float literal on a money path")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
                pytest.fail(f"{rel}:{node.lineno} float() on a money path")
            if isinstance(node, (ast.BinOp, ast.AugAssign)) and isinstance(node.op, ast.Div):
                pytest.fail(f"{rel}:{node.lineno} true division on a money path (use // or DEC.divide)")


# ------------------------------------------------------------------------------------------------- drift guard
VERBATIM = ("units", "fixed", "state", "orders", "signals", "events", "views", "config", "protocols", "errors")


def _design_blocks() -> dict[str, str]:
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    sec = design[design.index("## 5. Core types and protocols"):design.index("### 5.12 Pure-function interfaces")]
    out: dict[str, str] = {}
    for block in re.findall(r"```python\n(.*?)```", sec, flags=re.S):
        m = re.match(r'"""taotrader/core/([a-z_]+)\.py', block)
        if m:
            out[m.group(1)] = block
    return out


@pytest.mark.parametrize("module", VERBATIM)
def test_section5_modules_are_verbatim(module: str) -> None:
    """Section 5 is committed verbatim: a change on either side needs an ADR (DESIGN.md section 5 preamble)."""
    blocks = _design_blocks()
    assert (SRC / "core" / f"{module}.py").read_text(encoding="utf-8") == blocks[module]


@pytest.mark.parametrize("module", ["codec", "portfolio"])
def test_section5_implemented_modules_keep_every_public_signature(module: str) -> None:
    """codec.py and portfolio.py carry WP0 implementations; every name and signature of the design block remains."""
    def sigs(tree: ast.Module) -> dict[str, str]:
        out: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                out[node.name] = (ast.dump(node.args) if isinstance(node, ast.FunctionDef)
                                  else "|".join(ast.dump(b) for b in node.bases))
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = "assign"
        return out

    want = sigs(ast.parse(_design_blocks()[module]))
    have = sigs(_tree(SRC / "core" / f"{module}.py"))
    for name, sig in want.items():
        assert have.get(name) == sig, f"core/{module}.py changed or lost {name}"
