"""protocol.regimes: the section 8.6 table, fee defaults by spec, touches_econ, and literal hygiene."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from taotrader.core.units import Block
from taotrader.protocol.regimes import (
    PENDING_REGIMES,
    REGIMES,
    SPECS,
    fee_rate_default,
    fee_rate_default_verified,
    regime,
    regime_at,
    touches_econ,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "taotrader" / "protocol"


def test_table_matches_section_8_6() -> None:
    expected = {
        "era_a": 4_920_351, "era_b_v3": 5_947_549, "halving": 7_103_976, "spec411": 8_283_784,
        "price_ema_rp": 8_466_531, "balancer": 8_486_594, "price_ema": 8_636_191, "gate_qmass": 8_713_794,
        "gate_rank32": 8_765_684, "spec445": 8_831_004, "curated": 8_938_466, "basket_trading": 9_117_749,
        "v2_only": 9_217_508,
    }
    assert {r.regime_id: int(r.first_block) for r in REGIMES} == expected
    assert [r.first_block for r in REGIMES] == sorted(r.first_block for r in REGIMES)
    assert regime("curated").last_block == 9_088_597
    assert set(PENDING_REGIMES) == {"taoflow", "chainbuy_unrecorded", "spec475"}
    assert not set(PENDING_REGIMES) & {r.regime_id for r in REGIMES}


@pytest.mark.parametrize(("block", "rid"), [
    (4_920_351, "era_a"), (5_947_548, "era_a"), (5_947_549, "era_b_v3"), (7_103_975, "era_b_v3"),
    (7_103_976, "halving"), (8_466_530, "spec411"), (8_466_531, "price_ema_rp"), (8_486_594, "balancer"),
    (8_765_683, "gate_qmass"), (8_765_684, "gate_rank32"), (8_938_466, "curated"), (9_088_597, "curated"),
    (9_088_598, "spec445"), (9_117_749, "basket_trading"), (9_240_388, "v2_only"),
])
def test_regime_at(block: int, rid: str) -> None:
    assert regime_at(Block(block)).regime_id == rid


def test_regime_lookup_errors() -> None:
    with pytest.raises(ValueError):
        regime_at(Block(4_920_350))
    with pytest.raises(KeyError):
        regime("spec475")                                   # pending until the lead's ADR


def test_fee_rate_defaults_by_spec() -> None:
    assert fee_rate_default(290) == fee_rate_default(292) == 196
    assert fee_rate_default(293) == fee_rate_default(348) == fee_rate_default(475) == 33
    assert fee_rate_default(250) == 33 and not fee_rate_default_verified(250)   # era A: unverified placeholder
    assert fee_rate_default_verified(290) and fee_rate_default_verified(475)


def test_touches_econ_defaults_false_until_adr() -> None:
    assert SPECS == ()
    assert not touches_econ(475) and not touches_econ(999)


def test_frozen_literals_only_in_regimes() -> None:
    """Section 10.6 lint, applied to WP2: no 0.18 / 0.41 / 2952 / 14_400 / 720_000 outside regimes.py, and no
    float literal anywhere in the protocol package's money modules."""
    banned = {0.18, 0.41, 2952, 14_400, 720_000}
    for p in sorted(SRC.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                if p.name != "regimes.py":
                    assert node.value not in banned, f"{p.name}:{node.lineno} {node.value!r}"
                assert not isinstance(node.value, float), f"{p.name}:{node.lineno} float literal {node.value!r}"
