# Runbook: RPC provider outage

**Covers:** endpoint rotation, the keyed fallback, and the stale-prune CAUTION behaviour. References: DESIGN.md sections 3.3, 3.11, 6.1 and 6.9–6.11.

## What the reader does by itself
- **Endpoints (`[rpc]`, in priority order):**
  - head (WebSocket): `entrypoint-finney` → `lite.chain` → `lite.sub.latent.to`;
  - archive (HTTP): keyed OnFinality (if set) → public OnFinality → `archive.chain.opentensor.ai`.
- **Rate limits:** one token bucket per endpoint, at most 3 req/s, burst 3, concurrency at most 3. Do not raise `rpc.rate_per_s` above 3 on public endpoints.
- **Transient errors:** 429 (honouring Retry-After), −32029, −32005, −32603 and transport errors back off exponentially (1 → 60 s, with jitter). The reader rotates to the next endpoint after 2 consecutive errors, and raises after at most 6 retries per call.
- **−32004** (historical work budget) opens that endpoint's breaker for 300 s. "State already discarded" is re-routed to the archive role.
- **Live feed:** if no finalized head arrives for 30 s or the WebSocket drops, the feed polls `chain_getFinalizedHead` over HTTP every 3 s and rotates endpoints. Gaps of up to 280 blocks are filled per block from the head node; larger gaps go to the archive. The next item carries `feed_gap_blocks`.
- **Provider disagreement:** every 200 snapshots, 3 random keys are compared across two providers. A mismatch quarantines the endpoint, journals `ModelDriftObserved(probe="provider")`, and sets CAUTION.

## Modes during an outage (automatic, section 3.11)
| Condition | Mode |
|---|---|
| no finalized head for > 36 s | CAUTION (`stall` alert) |
| > 120 s | EXITS_ONLY (live: discretionary submissions frozen) |
| finality lag > 30 blocks, or fewer than 2 healthy endpoints | CAUTION |
| no healthy head endpoint | FROZEN |
| prune inputs stale > 25 blocks | CAUTION; `get_subnet_to_prune` is queried on a second endpoint, and Tier A and target exits stay allowed using runtime-API state |

The dead-man ping stops while the feed is stalled, so your healthcheck service alerts you even if the process is alive.

## 1. Diagnose
- `scripts\run.cmd doctor --network` reports the finalized head and the number of healthy endpoints (read-only).
- `status.json` shows `lags.secs_since_block`, `lags.finality_lag_blocks`, `lags.healthy_endpoints`, `feed.stalled`, `feed.failed_blocks` and `feed.skipped_blocks`.
- The log `logs/taotrader-paper.jsonl` shows `feed` alerts (block unreadable, skipped) and breaker messages.

## 2. Keyed fallback (OnFinality)
1. Get an OnFinality key (free tier: 400k RU/day; the live workload uses about 2–4%).
2. Store the full RPC URL in the keyring, never in config or argv:
   `python -c "import keyring, getpass; keyring.set_password('taotrader', 'onfinality', getpass.getpass('URL: '))"`.
   Alternatively, put `TAOTRADER_ONFINALITY_URL=...` in `%USERPROFILE%\.taotrader\secrets.env`, restricted with `icacls`.
3. Restart the process. The keyed URL becomes the first archive endpoint. Its `apikey=` is redacted from every log line and error.

## 3. Prolonged outage
- **Paper:** let it run; it catches up. Long gaps coarsen events, but diffs span the gap, so nothing is hidden.
- **Live:** if the head is down for minutes while a held name is near the prune target, consider an operator `exits-only`. You can also exit manually from the coldkey (`prune-emergency.md`).
- A different head endpoint can be added with `--set rpc.head_endpoints=[...]` or in your config. The config hash changes, so the next start needs `--accept-drift`, and live needs a new arm token.
- **Collector:** `collect --catch-up` resumes from the fetch ledger. The `tao-collect` task retries daily and at logon.
