# ADR-0003: Daily escrow grid for the era-C backfill

**Status:** accepted (lead), 2026-10-10

## Context
The first era-C backfill ran at about 50 snapshots/hour, which would take about 10 days. Profiling one snapshot at block 8,950,200 on a keyed OnFinality archive gave:

| Read | Time |
|---|---|
| snapshot with 621 tracked pairs (11 × `state_queryStorageAt`) | ≈ 6 s |
| generations + dividend keys | ≈ 10 s, membership points only |
| escrow (`StakeInfoRuntimeApi_get_stake_info_for_coldkey` for the basket escrow account) | **≈ 231 s** |

With the default 360-block escrow grid, about 2,100 escrow reads cover era C, roughly 135 hours. Every other read takes about 4 hours in total.

## Decision
1. The era-C backfill runs with `--escrow-grid 7200`: one escrow read per day, about 107 reads. The code default stays at 360 (DESIGN §6.7), so tests and the mini-lake fixture are unchanged.
2. `EscrowCache.fill` runs up to `ESCROW_CONCURRENCY = 4` escrow reads in parallel.
3. The partial lake collected with the 360 grid (812 snapshots) was moved to `data_grid360_partial/`, unused. `collector_meta` pins `escrow_grid`, so mixing grids is refused (ConfigMismatch, fail closed).

## Consequences
- Escrow E is a slow stock. It is released at about 2%/day, with bursts of 4-5%/day. Daily sampling is sufficient for:
  - the carry universe filter C-U11 (E/x ≤ 0.25);
  - the sell-load basket term κ_b·E;
  - the dissolution haircut.
- A claim burst can make E up to about 1 day stale; the impact on these uses is a few percent of E at most.
- Paper and live read escrow on their own cadence and are unaffected.
- **Follow-up (optional):** compute E from storage instead of the runtime API. That means `StakingHotkeys(escrow)`, then `state_getKeysPaged` on AlphaV2 and legacy Alpha by (hotkey, escrow) prefix, valued via `HotkeyIdx.value_of`. It should take about 10-20 s per read instead of about 231 s. It must match the runtime API exactly at the golden block (9,240,388) and several historical blocks before it replaces it.
- **Follow-up:** the collector should back off and retry, rather than exit, when a single membership chunk times out. A watchdog should restart it after a crash.
