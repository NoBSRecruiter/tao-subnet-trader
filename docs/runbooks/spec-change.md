# Runbook: runtime spec change

**Covers:** the checks V1–V7, updating `accepted_specs` and the fee tables, the `touches_econ` ADR, the regime-table ADR, re-arming, and what UNARMED does in the meantime. References: DESIGN.md sections 3.11, 6.12, 8.6, 9.3 and 9.5.

## What happens automatically
- A `SPEC_CHANGED` chain event (spec_version or transaction_version differs) sets every book to **CAUTION** in the same block: no increases, but exits and trims continue.
- A transaction_version change sets **live to FROZEN** and paper to CAUTION.
- **Paper** returns to NORMAL once validation passes.
- **Live** stays **UNARMED** until you accept the spec and re-arm.
  - With `risk_exits_when_unarmed = true`, it may still submit EMERGENCY/URGENT **full sells**, and only while V2, V3 and V6 pass on the new spec. Their limits come from runtime `sim_swap`.
  - With the flag false it submits nothing. It alerts on the SPEC_CHANGED while ladder exposure (positions with prune_rank ≤ 15) is above 0, listing each held position with its rank and t*.
- An unknown spec with an unregistered storage item fails closed: snapshots are flagged and live is FROZEN.

## 1. Run the validation suite (read-only)
`scripts\run.cmd verify --spec` runs at the finalized head by default; add `--block N` to pin a block.
| Check | Meaning | Tolerance |
|---|---|---|
| V1 | price decode parity (local vs `current_alpha_price_all`) | ≤ 1e-6 relative |
| V2 | AMM vs `sim_swap_*`: 3 deepest subnets × 1 and 10 TAO × buy/sell, outputs and fees | ≤ 1e-6 (1 ppm) |
| V3 | local prune target == `get_subnet_to_prune` | exact |
| V4 | emission replica vs observed per-block emission | ≤ 1e-4 TAO/block per subnet |
| V5 | fee parity, plan()/query_info vs `ExecCfg` | within 20%; **live host only** (preflight) |
| V6 | call indices 88/89/103/85/149/90 and the Staking (8) allow-list | exact; **live host only** (preflight, RiskExitChecks) |
| V7 | freeze-list governance constants vs the recorded baseline | any change is reported |

- On Windows, V5 and V6 report SKIPPED. On the live host they run in `taotrader live` preflight at every start; an unaccepted spec means UNARMED.
- Exit 1 means at least one FAIL. Add `--out file.json` to keep the result.

## 2. If something FAILED
- **V2 or V4 fail:** the swap or emission math changed. Do not accept the spec.
  1. Open an ADR.
  2. Update `protocol/regimes.py`: a new row starting at setCode block + 1, with per-spec `touches_econ`. Also update the fee defaults by spec in `protocol/fees.py` / `regimes.py`.
  3. Re-run the golden tests and the affected studies (T2a, T6, FT1 hazard validity).
  4. Until then the books stay in CAUTION. If validation fails, the overlay moves them to EXITS_ONLY using runtime-API state (prune target, prices, sim_swap fill-or-kill limits).
- **V3 fails:** prune-target logic changed. The runtime value wins. Entries halt until the cause is explained.
- **V7 changed:** a freeze-list constant changed (PARAM_CHANGED). Re-run the affected tests.
  - After reviewing the change, record the new baseline with `verify --spec --accept-freeze`.
  - If `NetworkImmunityPeriod`, `NetworkRateLimit` or the lock-cost parameters changed, the prune hazard model may be invalid (`hazard_valid = False`; DESIGN.md section 3.3 invalidation path). That needs an ADR.

## 3. Accept the spec (live only; the user decides)
1. V1–V4 and V7 pass here, and V5/V6 pass in a live-dry start on the new spec.
2. Add the spec_version to `[live] accepted_specs` in your private `config/live.toml`.
3. This config edit changes the config hash, so the old arm token is invalid. Re-arm: `taotrader live arm`, set `TAOTRADER_LIVE_ARMED` in `/etc/taotrader/live.env`, then `systemctl restart taotrader-live` (`live-arming.md`).
4. Restarting with changed config needs `--accept-drift`; add it once on the service's command line, or run `taotrader live --live --accept-drift` once by hand.

## 4. Fee tables
If V5 shows fees outside 20% of `ExecCfg`, update the fee table (`protocol/fees.py`, by spec) through an ADR and re-run the backtest sensitivity grid (`taotrader grid --sensitivity <book>`). Paper fills use `ExecCfg`, so their realised-vs-modelled cost check (paper gate 2) depends on it.
