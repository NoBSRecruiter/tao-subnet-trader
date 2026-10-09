# Runbook: chain SafeMode

**Covers:** what is frozen, and the post-SafeMode exit queue. References: DESIGN.md sections 3.3 ("SafeMode end"), 3.11 and 9.8 #15.

## What SafeMode means
- While `SafeMode.EnteredUntil ≥ block`, the runtime whitelists no staking calls. Nobody can stake, unstake or move, so neither the bot nor you can exit.
- Registrations, and therefore prunes, can resume in the **first block after** SafeMode ends. A held prune target is most exposed right then.

## What the bot does (automatic)
- The `SAFE_MODE` chain event sets every book to **FROZEN**: no new submissions.
  - Live: LiveVenue refuses every submission while SafeMode is set.
  - Paper: no simulated fills are possible, because the chain rules reject staking calls.
- Reads, journaling, reconciliation and decisions continue. Intents that cannot be submitted wait in INTENDED, or are cancelled when the next decision invalidates them.
- **Post-SafeMode exit queue:** while frozen, the overlay keeps evaluating Tier A, the backstop and "never hold the target" on every snapshot. At `EnteredUntil + 1` it:
  1. reconciles (live);
  2. re-runs Tier A and the backstop on the **post-SafeMode** state;
  3. submits those exits **first**, in priority order EMERGENCY > URGENT > HIGH, then by expected-loss rate.
- Alerts: a `mode` change (to FROZEN and back), and `prune_watch` for held names at rank ≤ 3.

## What you do
1. **Nothing urgent while SafeMode is on.** No call can succeed.
   - Do not `halt` or create the KILL file: a halt keeps the books FROZEN after SafeMode ends and blocks the queued exits.
   - `exits-only` is harmless.
2. Check exposure: open `status.json` (positions per book) and look for `prune_watch` alerts.
   - Live: if `risk_exits_when_unarmed = false` and the arm token will expire during SafeMode, **re-arm before it ends** (`live-arming.md`). Otherwise the post-SafeMode exits cannot be submitted.
3. Watch the end block (`EnteredUntil`). Within a few blocks after it, the journal should show the queued exits (`OrderIntended` with EMERGENCY/URGENT urgency), then `FillReported` or `OrderFailed`.
4. If an exit fails repeatedly (e.g. SLIPPAGE in a crowded post-SafeMode block), the planner re-quotes once per L_exec. You may also `flatten <netuid>` (`prune-emergency.md`).
5. After a long SafeMode, run `taotrader verify --spec` if the SafeMode came with a runtime upgrade (`spec-change.md`).

## Drill (FT11; paper)
The FT11 kill-switch drills include SafeMode. The correct mode must be reached within 1 block, and the correct exits must be queued on recovery. Passing them is a paper gate for LIVE_ELIGIBLE (DESIGN.md section 9.1, gate 5).
