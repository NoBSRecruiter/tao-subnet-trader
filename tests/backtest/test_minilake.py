"""The committed mini-lake fixture (DESIGN.md 10.3 item 1, 11 WP10): content pinned by MANIFEST.json, complete panel.

tests/fixtures/minilake/build_minilake.py collected it with the WP4 collector (c60 schedule, 8,765,684 -> 8,830,000,
hotkey panel from the membership point 8,765,400) from the public archive; `--digest` wrote MANIFEST.json.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from taotrader.data.collector import CollectorCfg, panel_gaps
from taotrader.data.lake import Lake

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "minilake"


def test_manifest_pins_the_committed_lake(open_lake: Lake) -> None:
    man = json.loads((ROOT / "MANIFEST.json").read_text(encoding="utf-8"))
    assert man["start"] == 8_765_684 and man["end"] == 8_830_000 and man["panel_from"] == 8_765_400
    assert open_lake.manifest_hash() == man["manifest_hash"]
    refs = open_lake.snapshot_refs()
    assert len(refs) == man["snapshots"] >= 1_070
    assert min(int(r.block) for r in refs) == man["first_block"] == 8_765_400
    assert max(int(r.block) for r in refs) == man["last_block"] >= 8_829_940
    for info in open_lake.manifest():
        p = ROOT / "lake" / info.path                            # manifest paths are relative to the lake root
        assert hashlib.sha256(p.read_bytes()).hexdigest() == info.sha256 == man["chunks"][info.path]


def test_every_60_block_cell_of_the_window_is_present(open_lake: Lake) -> None:
    blocks = {int(r.block) for r in open_lake.snapshot_refs()}
    want = set(range(8_765_700, 8_830_001, 60))
    assert want <= blocks, sorted(want - blocks)[:10]


def test_hotkey_panel_has_no_gaps(open_lake: Lake) -> None:
    assert panel_gaps(open_lake, CollectorCfg(panel_from=8_765_400), limit=20) == []


def test_prune_window_and_wave_fixtures_are_pinned() -> None:
    man = json.loads((ROOT / "PRUNE_MANIFEST.json").read_text(encoding="utf-8"))
    lake = Lake(ROOT / "prune116" / "lake", ROOT / "prune116" / "state.sqlite")
    try:
        assert lake.manifest_hash() == man["manifest_hash"]
        refs = lake.snapshot_refs()
        refined = sorted(int(r.block) for r in refs if r.series == "refine")
        assert len(refs) == man["snapshots"] and len(refined) == man["refined"]
        assert refined == list(range(man["window"][0], man["window"][1] + 1))      # every block of the window
    finally:
        lake.close()
    waves = Lake(ROOT / "waves" / "lake", ROOT / "waves" / "state.sqlite")
    try:
        assert sorted(int(r.block) for r in waves.snapshot_refs()) == [8_463_543, 8_463_544, 9_029_888, 9_029_889]
    finally:
        waves.close()
