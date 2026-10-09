# Runbook: live arming

**Covers:** the four locks; preflight failures and what they mean (including RealPaysFee, locks and `max_ops_balance_tao`); the UNARMED state and the `risk_exits_when_unarmed` decision; removing idle proxies after 72 h unarmed; and the single-position signature leak, with the ≥ 2-position recommendation for per-strategy coldkeys. References: DESIGN.md sections 9.2–9.8 and 12.2.

> **The live adapter is run by you, in your shell, on a Linux VPS or WSL2. Nothing in this repository's tooling, CI or agents ever runs `--submit`.** Not financial advice. Backtests and paper results do not predict live returns.

## The four locks (all required to submit)
| Lock | Requirement | Where |
|---|---|---|
| 1 config | `[live] enabled = true` | your private `config/live.toml` (copy of `config/live.example.toml`; never commit it) |
| 2 config confirmation | `mode = "submit"`; `network` set **explicitly** in your file (or via env/CLI); current spec_version in `accepted_specs`; `sleeves` lists only LIVE_ELIGIBLE sleeves; a valid `real_coldkey_ss58`; 2–3 distinct `delegate_wallets` | `config/live.toml` |
| 3 CLI | `taotrader live --live --submit` (both flags) | systemd `ExecStart` |
| 4 environment | `TAOTRADER_LIVE_ARMED=<expiry>:<hmac>` from `taotrader live arm`, valid ≤ 24 h, bound to the config hash. Mainnet also needs `TAOTRADER_LIVE_NETWORK_CONFIRM=finney` | `/etc/taotrader/live.env` (mode 600) |

Plan-only (live-dry) needs only lock 1 plus `--live`. It builds real intents, runs `client.plan()`, journals the result and never submits. The CLI evaluates locks 1 and 3 **before** `taotrader.live` is even imported. On Windows `taotrader live` always refuses.

## The ladder
1. Run on test.finney first: reads, quotes, `plan()`, and one tiny shielded order per intent type. Measure the shield miss rate and the nonce behaviour.
2. Run **live-dry for at least 7 consecutive days with 100% `plan()` acceptance.** The other paper gates (DESIGN.md section 9.1) must pass as well.
3. Arm:
   1. Once per host: `taotrader live arm --init-secret --config config/live.toml` creates the HMAC secret in the OS keyring.
   2. Each time: `taotrader live arm --config config/live.toml --ttl-hours 24`. It prints `export TAOTRADER_LIVE_ARMED='...'`; the token is never logged.
   3. Put the token in `/etc/taotrader/live.env`, add `--submit` to the unit's `ExecStart`, then `systemctl restart taotrader-live`.
4. Re-arming is a deliberate daily act. **Any config edit invalidates the token.**

## Preflight (every start and every 7,200 blocks)
| Failure | Meaning / fix |
|---|---|
| delegate == real coldkey | configuration error; the bot key must be a separate delegate |
| delegate free balance > `max_ops_balance_tao` (0.5 TAO) | delegates are a fee buffer only; move the excess back to the coldkey |
| proxy set: missing (delegate, **Staking**, delay 0), or **any other proxy type** (Any, NonTransfer, Transfer) for an ops delegate | refuse; remove the other proxy from the coldkey and add exactly Staking/0 |
| **RealPaysFee** on for a delegate | refuse; inner fees, and the alpha-fee trap, would land on the real coldkey. Turn it off from the coldkey (`Proxy.set_real_pays_fee`) |
| **locked alpha** > 0 on a held netuid | refuse; conviction locks make exits fail with StakeUnavailable. Release the lock or do not hold that subnet |
| bittensor ≠ 11.3.0 | install from `requirements-live.txt` (hashes) in a separate venv |
| spec not in `accepted_specs` | UNARMED (see below) |
| SafeMode, head lag ≥ 2, finality lag > 5 | wait (`safe-mode.md`, `provider-outage.md`) |
| fee float < `min_fee_float_tao`, real free balance < MIN_FREE_REAL | top up the delegates or the coldkey |
| `plan()` violations for buy, partial sell, full exit or same-subnet move; call indices / Staking allow-list ≠ V6 | do not submit; the runtime or SDK changed (`spec-change.md`) |
| reconciliation not clean (orphans, recon halt) | reconcile, then `taotrader clear-quarantine` (`key-compromise.md`, step 5) |

- A failure at start exits with code **3** (systemd does not restart on 3).
- A failure in the periodic preflight is a key alarm: FROZEN, plus an alert.

## UNARMED and `risk_exits_when_unarmed` (decide once, in `config/live.toml`)
- The process keeps running when the token expires or a SPEC_CHANGED leaves the spec outside `accepted_specs`. Reads, reconciliation, planning and journaling continue. LiveVenue refuses submissions with `OrderFailed(VENUE_REJECT, "unarmed")`.
- **`true`:** it may still submit **only EMERGENCY or URGENT full sells**, and only while V2, V3 and V6 pass on the current spec, with limits from runtime `sim_swap`. It never buys or moves. A crash restart under systemd keeps these exits: an authentic but expired token admits an UNARMED start.
- **`false`:** it submits nothing. While ladder exposure (prune_rank ≤ 15) is above 0, it alerts at T − 2 h before expiry and on every SPEC_CHANGED, listing each held position with its rank and t*. An expired token at start is refused.
- Re-arming (and accepting the spec) returns it to normal operation.

## Idle proxies (> 72 h unarmed)
A zero-delay Staking proxy stays active on chain after the token expires. The daily `taotrader-doctor.timer` runs `doctor --live`. When the last arm is older than 72 h, it alerts **"remove Staking proxies (`btcli proxy remove`)"** and lists the delegates. Remove them from the coldkey; re-adding later is one coldkey signature per delegate.

## Single-position signature leak
A coldkey with a single alpha position reveals trade direction even when shielded, because the signer and the timing stay visible. Per-strategy coldkeys (which limit the blast radius) should therefore hold **at least 2 positions**, or you accept the leak explicitly.
