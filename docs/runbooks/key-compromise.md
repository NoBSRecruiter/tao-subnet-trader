# Runbook: key compromise and idle proxies

**Covers:** revoking the proxy from the coldkey signer, rotating delegates, reconciling, `QuarantineCleared`, and removing idle Staking proxies when live has not been armed for more than 72 h. References: DESIGN.md sections 3.11, 9.3, 9.7, 9.8 and 12.2.

**The one rule:** the bot key is a zero-delay **ProxyType::Staking** delegate. Only the coldkey (Ledger / Polkadot Vault, never on a bot host) can revoke it. The bot can never move funds out: it has no transfer intents and no `UnstakeAll`/`Batch`. A stolen delegate key, however, can force value-draining round trips through the stake calls.

## Triggers
- A `key_alarm` alert ("... revoke the Staking proxy from the coldkey ..."), raised by any of:
  - an unexplained stake delta or an unknown position;
  - a delegate nonce jump;
  - a change in `Proxy.Proxies` or announcements;
  - a scheduled coldkey swap;
  - foreign StakeMoved/Transferred/Swapped events, or `TransactionFeePaidWithAlpha`;
  - an inner included after a declared miss;
  - `RealPaysFee` turned on, or a lock on a held netuid;
  - 50 consecutive failed reconciliations.

  The live venue is then **FROZEN** and entries halt. With `allow_emergency_exits_when_frozen`, only EMERGENCY fill-or-kill sells are still possible.
- The daily `taotrader doctor --live` timer alerts **"remove Staking proxies (`btcli proxy remove`)"** when live has not been armed for > 72 h.
- You suspect the live host is compromised.

## 1. Contain (minutes)
1. **Revoke** each delegate's proxy **from the coldkey signer**: `btcli proxy remove --delegate <delegate_ss58> --proxy-type Staking --delay 0`, signed on the Ledger. This is the only step that actually stops a stolen key.
2. Stop the bot: `sudo systemctl stop taotrader-live`. A halt alone is not enough if the host itself is suspect.
3. If the host is suspect, treat every delegate key file and `live.env` on it as burned, and keep the journal for forensics (copy `data/runs/<run>/`).

## 2. Rotate delegates
1. Create 2–3 **new** delegate wallets on a clean host. Each gets only a small fee buffer, at most `max_ops_balance_tao` (0.5 TAO). Never reuse an old key.
2. From the coldkey, add each new delegate: `btcli proxy add --delegate <new> --proxy-type Staking --delay 0`. This is one coldkey signature per delegate, plus a 0.033 TAO deposit each.
3. Update `[live] delegate_wallets` in `config/live.toml`. This changes the config hash, so the old arm token is invalid.

## 3. Reconcile
1. Start plan-only on the clean host: `taotrader live --live --config config/live.toml --accept-drift`.
2. Preflight refuses to start if a delegate has any proxy type other than Staking, `RealPaysFee` is on, a delegate holds more than `max_ops_balance_tao`, or there are locks on held netuids.
3. Reconciliation compares chain share counts and balances with the journal. Every difference is journaled as `ReconAdjusted` (chain wins), and entries stay halted.
4. Compare the adjustments with what you know happened (taostats account history, `btcli stake list`). Unexplained losses are the incident's damage; record them.

## 4. Re-arm
Follow `live-arming.md`: `taotrader live arm`, set the token in `/etc/taotrader/live.env`, then `systemctl start taotrader-live`. Run live-dry for at least one day first if the host changed.

## 5. Clear the quarantine (operator decision)
Entries stay halted after `ReconAdjusted`, orphan facts or a key alarm until you journal `QuarantineCleared`:
1. Stop the process (the command needs every lock of the run).
2. `taotrader clear-quarantine --config config/live.toml --book <book> --reason "proxies rotated; recon matches chain at block N"`. Run it once per affected book.
3. Start again. The clear takes effect at recovery.

Notes:
- The proxy and announcement baseline is the set seen at the first reconciliation call. A change that persists alarms again after a clear, so fix the cause first.
- On Windows paper runs, the same command clears orphan quarantines: `scripts\run.cmd clear-quarantine --book paper-carry --reason "..."`.

## Idle proxies (no incident)
A Staking proxy stays active on chain after the arm token expires. If live will not be armed for days:
1. Run `taotrader doctor --live --config config/live.toml`. It lists the configured delegates and exits 1 when the last arm is older than 72 h.
2. Remove each proxy with `btcli proxy remove ...` from the coldkey.
3. Re-adding later costs one coldkey signature per delegate.
