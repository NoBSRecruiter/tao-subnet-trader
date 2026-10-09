"""Drift guard: every public name of DESIGN.md section 5.12 exists in taotrader.protocol with the binding signature
(function arguments and return annotation; dataclass fields and annotations; Protocol methods; module constants)."""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "src" / "taotrader" / "protocol"


def _sigs(tree: ast.Module) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            out[node.name] = ("def", ast.dump(node.args), ast.dump(node.returns) if node.returns else None)
        elif isinstance(node, ast.ClassDef):
            fields = [(n.target.id, ast.dump(n.annotation)) for n in node.body
                      if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
            methods = {n.name: ast.dump(n.args) for n in node.body if isinstance(n, ast.FunctionDef)}
            out[node.name] = ("class", fields, methods, [ast.dump(b) for b in node.bases])
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = ("var", ast.dump(node.annotation))
    return out


def test_section_5_12_names_and_signatures_are_implemented() -> None:
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    sec = design[design.index("### 5.12 Pure-function interfaces"):design.index("### 5.13 Notes for implementers")]
    want: dict[str, Any] = {}
    for block in re.findall(r"```python\n(.*?)```", sec, flags=re.S):
        want.update(_sigs(ast.parse(block)))
    assert len(want) >= 45
    have: dict[str, Any] = {}
    for p in sorted(PROTOCOL.glob("*.py")):
        for name, sig in _sigs(ast.parse(p.read_text(encoding="utf-8"))).items():
            have.setdefault(name, sig)
    missing = sorted(set(want) - set(have))
    assert missing == []
    changed = sorted(n for n in want if have[n] != want[n])
    assert changed == [], f"section 5.12 signatures changed (needs an ADR): {changed}"
