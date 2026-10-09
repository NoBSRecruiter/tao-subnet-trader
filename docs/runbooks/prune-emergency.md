# Runbook: prune emergency

**Covers:** manual flatten, verifying Tier A outcomes, and post-dissolution payout reconciliation. References: DESIGN.md sections 3.3, 3.11, 8.8, 9.6 and 9.7.

## Background (why minutes matter)
- A subnet is pruned when a registration happens while it is the prune target. 23% of prune victims had been rank 1 for under 24 h.
- A held position on a dissolved subnet is paid out at the recovery ratio R (typically about 0.35–0.57 of spot), **not** at spot.
- The overlay acts automatically:
  - **Tier A** (EMERGENCY, single-shot full exit) when the registration window is open or opening within U + M_A blocks and the subnet is the target or would become it;
  - the **backstop** (URGENT) at prune_rank ≤ 3 with r ≤ 1.2;
  - **never hold the target**.
- Manual action is for when you disagree with the model, or when the bot cannot act (UNARMED with `risk_exits_when_unarmed = false`, FROZEN, or stopped).

## 1. See the situation
- `prune_watch` alerts arrive when a held name reaches prune rank ≤ 3, has emission disabled, or is dissolved. `REG_WINDOW_OPENED` with ladder exposure > 0 is also alerted.
- `data/runs/<run>/status.json` shows each book's mode, positions and open orders.
- `taotrader report --run <run>` writes `summary.json`/`.html` with fills, failures by reason, model drift and mode changes.

## 2. Manual flatten (all books, one netuid)
1. `scripts\run.cmd flatten <netuid> --reason "prune risk"` (Linux: `taotrader flatten ... --config config/live.toml`).
   - The command writes a control file. The running process journals `OperatorCommand(flatten:<netuid>)` within one block, and the planner sells that netuid's positions in every book.
   - The command is idempotent (nonce); a re-delivered file is archived, not repeated.
2. Optional: `scripts\run.cmd exits-only --reason "..."` stops every new entry and keeps risk exits.
3. A full stop: `scripts\run.cmd halt --kill`. This creates `data/control/KILL`, and every book stays halted while it exists.
   - A halt sets the books to FROZEN. Only the configured EMERGENCY exception (`allow_emergency_exits_when_frozen`, with fill-or-kill limits) can still exit, and the KILL file stops even that in live.
   - Prefer `exits-only` while a prune is coming.
4. To resume: `scripts\run.cmd resume --clear-kill`. Resume is refused in effect while the KILL file exists.

**Live and UNARMED:** if the arm token expired or the spec is not accepted, nothing can be submitted unless `risk_exits_when_unarmed = true`. Re-arm first (`live-arming.md`). As a last resort, sell from the coldkey yourself (e.g. `btcli stake remove` signed on the Ledger). Then expect the next reconciliation to journal `ReconAdjusted` and halt entries (step 5).

## 3. Verify the Tier A outcome
For each exit, the journal carries:
1. `OrderIntended` (urgency EMERGENCY, rule `exit.prune_A`);
2. `SubmitStarted` (live);
3. then exactly one of:
   - `FillReported` (shares sold, TAO received, fees);
   - `OrderFailed` (reason, e.g. SHIELD_MISSED or SLIPPAGE), which is followed by a re-quote on another delegate, at most once per L_exec blocks, until flat.

Check that the position is gone in `status.json` (`positions`), or that a remainder at or above the dust floor is being re-exited (attempt + 1). In live, reconciliation compares the chain share count every 25 blocks; a mismatch journals `ReconAdjusted`.

## 4. After a dissolution (payout reconciliation)
- **Paper and backtest:** the Engine journals `DeregSettled` from the last good snapshot with the book's dereg model (formula R, capped by FT10).
- **Live:** the Engine never settles. The reducer marks the position **DISSOLVING** at the `DEREGISTERED` chain event. Reconciliation baselines the coldkey's free TAO and journals the first unexplained increase as `DeregSettled(model="observed")`, exactly once (idempotency key `dereg:{book}:{netuid}:{reg_at}`).
  - If no payout is seen within 7,200 blocks, it settles at 0, and the next cash reconciliation books a late credit as `ReconAdjusted`.
  - After a restart the observed-payout baseline is re-taken. A payout that arrived while the process was down is booked through the same reconciliation path.
- Compare the observed payout with the modelled one (alpha × spot × R at P−1): this is FT10 evidence. Record the subnet, block and ratio for the lead.
- If `ReconAdjusted` halted entries, check the amounts against the chain (taostats or `btcli wallet balance`). Then clear the quarantine (`key-compromise.md`, step 5).
