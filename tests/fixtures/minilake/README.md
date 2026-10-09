# WP10 lake fixtures (real chain data, public archives, read-only JSON-RPC)

| Directory | Builder | Content |
|---|---|---|
| `lake/` + `state.sqlite` + `MANIFEST.json` | `build_minilake.py` | The **mini-lake**: the WP4 collector's c60 schedule over the gate rank-32 window 8,765,684 -> 8,830,000 (60-block FULL snapshots, ~1,072, plus the daily membership points from 8,765,400), the point-in-time hotkey panel, calibration probes. Escrow on a daily grid (see `ESCROW_GRID`). |
| `prune116/` + `PRUNE_MANIFEST.json` | `build_prune_window.py` | The SN116 prune at P = 9,210,610: 60-block snapshots over [P - 3,600, P + 120] with runtime prune-target probes every 600 blocks, and the per-block REFINED window [P - 120, P + 25]. |
| `waves/` | `build_event_waves.py` | FULL snapshots at 8,463,543/8,463,544 (the purge) and 9,029,888/9,029,889 (the re-enable wave). |

All three are collected with the WP1 reader and the WP4 collector / WP3 lake, from the public archives only
(`https://bittensor-finney.api.onfinality.io/public`, `https://archive.chain.opentensor.ai:443`), at <= 3 req/s per
endpoint. Nothing is signed or submitted. Every builder is resumable: re-running it continues where it stopped.

```
.venv/Scripts/python.exe tests/fixtures/minilake/build_minilake.py --endpoint https://bittensor-finney.api.onfinality.io/public --end 8801999
.venv/Scripts/python.exe tests/fixtures/minilake/build_minilake.py --endpoint https://archive.chain.opentensor.ai:443 --start 8802000
.venv/Scripts/python.exe tests/fixtures/minilake/build_minilake.py --digest            # writes MANIFEST.json
.venv/Scripts/python.exe tests/fixtures/minilake/build_prune_window.py
.venv/Scripts/python.exe tests/fixtures/minilake/build_prune_window.py --digest        # writes PRUNE_MANIFEST.json
.venv/Scripts/python.exe tests/fixtures/minilake/build_event_waves.py
```

Cost note (measured 2026-10-09): the public archives enforce historical-work budgets (JSON-RPC -32004). One
`StakeInfoRuntimeApi_get_stake_info_for_coldkey(escrow)` call took ~160 s; a FULL snapshot with the tracked panel is
6-8 `state_queryStorageAt` calls of 2,500 keys. The mini-lake therefore reads escrow once a day, and the two
sub-ranges can run at the same time, one per endpoint (chunk buckets are absolute, 8,802,000 = 1,467 x 6,000).

Changing any fixture changes the golden digests (`tests/integration/golden_minilake_digests.json`); that needs an ADR.
