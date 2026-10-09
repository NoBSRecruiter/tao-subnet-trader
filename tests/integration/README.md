# Section 10.3 integration suite (WP10)

| Item | Test module | Data |
|---|---|---|
| 1 golden replay (+ S0 end-to-end) | `test_golden_replay.py` | mini-lake, 8,765,400 -> 8,830,000, every baseline + carry |
| 2 prune replay | `test_prune_replay.py` | `fixtures/minilake/prune116` (per-block window before P) |
| 3 event waves | `test_event_waves.py` | `fixtures/minilake/waves` |
| 4 crash matrix (real SimVenue) | `test_crash_matrix.py` | mini-lake, first 2,400 blocks |
| 5 determinism | `test_determinism.py` | mini-lake, first 2,400 blocks |
| 6 no lookahead (incl. as-of calibration) | `test_no_lookahead.py` | mini-lake, first 3,000 blocks |
| 7 paper <-> offline equality | `test_paper_offline.py` | `prune116` (60-block + per-block blocks) |
| 8 cadence invariance (60 vs 300) | `test_cadence.py` | mini-lake (the golden replay is the stride-60 side) |
| 9 chaos | `test_chaos.py` | cassette 9,240,388, mini-lake, `waves` |
| WP5 -> WP10 network test: Gatekeeper on the last 10 registrations | `test_gatekeeper_network.py` | public archive (`-m network`) |

Commands (from the repo root):

```
.venv/Scripts/python.exe -m pytest tests/integration -q                       # the suite (network test deselected)
.venv/Scripts/python.exe -m pytest tests/integration/test_gatekeeper_network.py -m network -q
TAOTRADER_FULL_MATRIX=1 .venv/Scripts/python.exe -m pytest tests/integration/test_crash_matrix.py -q   # nightly
TAOTRADER_REGEN_GOLDEN=1 .venv/Scripts/python.exe -m pytest tests/integration/test_golden_replay.py -q # with an ADR only
```

S0 report on the mini-lake (static HTML + CSV; trial registry in the output directory):

```
.venv/Scripts/python.exe -m taotrader.backtest.studies s0 --lake tests/fixtures/minilake/lake \
    --set backtest.start_block=8765400 --set backtest.warmup_blocks=0 --set backtest.feature_warm_blocks=7200 \
    --books base-cash,base-ew-price,base-ew-total,base-yield-size,base-prune-blind,base-random,carry --out reports/s0-minilake
```

S0 (or every study, `all`) on the full lake, after the WP4 era-C backfill
(`python -m taotrader.data.collector backfill --schedule c60 --lake data/lake`):

```
.venv/Scripts/python.exe -m taotrader.backtest.studies s0 --lake data/lake --out reports/s0
.venv/Scripts/python.exe -m taotrader.backtest.studies all --lake data/lake --out reports/all
```

The full-lake run uses `config/books.backtest.toml` as committed: window 8,765,684 -> the lake's last block, a 30-day
warm-up ingested with `warm = false`, all 21 books. `--start/--end/--books` and `--set path=value` narrow it.
