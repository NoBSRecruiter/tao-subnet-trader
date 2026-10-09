# taotrader

> **Not financial advice.** This is research software. **Backtests and paper results do not predict future returns**, and paper fill rates are optimistic. Micro-cap subnet alpha can lose most of its value in hours: a pruned subnet pays out only a fraction of spot. You alone decide whether to put real funds at risk. The live adapter is gated, disabled by default, and run only by you.

taotrader is a research, backtest and paper-trading system for **Bittensor dTAO subnet-alpha** (micro-cap subnet tokens priced by on-chain AMM pools). It also has a gated live adapter for later. The build contract is [`docs/DESIGN.md`](docs/DESIGN.md), and the protocol mechanics come from [`docs/research/RESEARCH_BRIEF.md`](docs/research/RESEARCH_BRIEF.md).

- **Data:**
  - a Windows-native JSON-RPC chain reader (no bittensor dependency);
  - a resumable archive collector into a Parquet lake;
  - per-block refinement windows around prunes and events.
- **Research:**
  - a deterministic replay engine running N books per pass (sleeves, baselines, impact and dereg variants);
  - pre-registered studies (S0 benchmark onwards) with trial registration, deflated Sharpe and PBO;
  - HTML reports.
- **Paper:** the same engine on the live finalized feed, with a recorder, a hash-chained append-only journal, crash recovery, a heartbeat and alerts.
- **Live (gated, Linux/WSL, user-run):** a four-lock gate, preflight, Staking-proxy-only execution, reconciliation, and key alarms.

Strategies (DESIGN.md section 2):
- **carry** (core): hotkey-optimised yield, screened by structural sell load;
- **momentum** (experimental, shadow by default);
- mean reversion and launches as filters only;
- a risk overlay that owns prune, emission, owner and liquidity risk.

## Quickstart (Windows 11, native)
Requirements: Python 3.11 via [uv](https://docs.astral.sh/uv/). Keep the repository, and `data\` in particular, **outside OneDrive**.

```powershell
git clone https://github.com/NoBSRecruiter/tao-subnet-trader.git
cd tao-subnet-trader
uv sync --frozen --all-extras          # project venv from uv.lock (collector + dev extras included)
scripts\run.cmd doctor                 # environment check (python, data dir, secrets present/absent, runs)

# 1. data: era-C snapshots (60-block) into data\lake; resumable, read-only JSON-RPC at <= 3 req/s per endpoint
scripts\run.cmd collect --catch-up
scripts\run.cmd verify-lake --lake data\lake

# 2. one backtest pass of every configured book (config\books.backtest.toml), summary + HTML report
scripts\run.cmd backtest --report --out reports\output\backtest

# 3. the S0 benchmark study (pre-registered; writes index.html, CSVs and trials.sqlite)
scripts\run.cmd study s0 --out reports\s0

# 4. paper trading on the live finalized feed (config\books.paper.toml); Ctrl+C stops cleanly
scripts\run.cmd paper --config config\books.paper.toml
```

- `scripts\run.cmd` is a thin wrapper for `.venv\Scripts\python.exe -m taotrader`. `uv run taotrader ...` works too.
- The first full era-C backfill takes hours on public endpoints. A keyed OnFinality URL makes it faster (see Secrets).
- Paper strategies start deciding once the feature engine is warm: 30 days of journaled paper history by default (`[paper] feature_warm_blocks`). The heartbeat, recorder and journal grow from the first block.
- To run unattended, **you** register the scheduled tasks with `scripts\register-tasks.ps1 -BackupRoot D:\taotrader-backup` (try `-WhatIf` first). It creates:
  - `tao-collect`: daily, and at logon;
  - `tao-paper`: at logon, restarted on crash;
  - `tao-nightly`: verify-journal, verify-lake, paper<->offline replay equality, VACUUM INTO backups;
  - `tao-weekly`: lake mirror and report;
  - `tao-doctor`.
- The bot never changes system settings. Optional host settings you may apply yourself:
  - exclude `data\` from Defender real-time scanning;
  - `powercfg /change standby-timeout-ac 0` on the paper machine;
  - set Windows Update active hours.

Tests: `.venv\Scripts\python.exe -m pytest -q` (network tests are deselected; add `-m network` to run them).

## Command reference
Every command takes `--config TOML` (repeatable; applied after `config\default.toml`), `--set path=value`, `--log-dir` (default `logs`, JSON lines with secret redaction), `--log-level` and `--quiet`.
- Configuration precedence: default.toml < files < `TAOTRADER_CFG_*` environment < `--set`.
- Every command validates its input strictly and **fails closed** before any work starts.
- Exit codes: 0 ok · 1 a check found problems · 2 usage/config/missing secret · 3 live gate refused · 4 another instance holds the lock · 5 replay divergence.

| Group | Command | What it does |
|---|---|---|
| data | `collect [--catch-up] [--schedule c60\|h300] [--start/--end] [--status]` | resumable archive collector into the lake (committed chunks are skipped) |
| | `collect --enrich --netuids 1,2 --from-block A --to-block B` | Taostats trades into `ext_trades` (needs the Taostats key; never on the decision path) |
| | `refine <lifecycle\|windows\|spec-boundaries\|verify-prune-log\|range-probe> ...` | per-block refinement passes (`taotrader.data.refine`) |
| | `verify --spec [--block N] [--accept-freeze]` | spec validation suite V1–V4, V7 (V5/V6 run in live preflight) |
| | `verify --taostats --netuids ... --from-block A --to-block B` | Taostats price cross-check at block % 300 == 0 (diagnostic) |
| | `verify-metadata [--block N]` | live runtime metadata vs the committed storage registry (collector extra) |
| | `verify-journal [--run R \| --journal F \| --all] [--deep]` | hash chain of run journals (plus the heartbeat head) |
| | `verify-lake [--lake D] [--deep]` | lake chunk files vs the manifest |
| research | `backtest [--books a,b] [--start/--end] [--journal F] [--report]` | one replay pass of the configured books; `summary.json`; trial registration |
| | `grid --capacity BOOK [--capitals 1,3,10] \| --sensitivity BOOK` | capacity / sensitivity grids (≤ 8 processes) |
| | `study s0\|all [--out D]` | pre-registered offline studies |
| | `report [--with-studies none\|s0\|all]` / `report --run RUN` | HTML research report / paper-or-live run summary |
| running | `paper [--max-minutes M] [--max-ticks N] [--accept-drift]` | paper trading (single instance per run; heartbeat `data\runs\<run>\status.json`) |
| | `replay [--run R] [--use-checkpoints]` / `replay --recover [--accept-drift]` | re-verify a journal on a copy / crash recovery in place |
| operator | `halt [--kill]`, `resume [--clear-kill]`, `exits-only`, `flatten <netuid>` | control files picked up by the running process within one block |
| | `clear-quarantine --book B --reason "..."` | journal `QuarantineCleared` for a **stopped** run, after you reconciled |
| live | `live arm [--init-secret] [--ttl-hours ≤24]` | print a `TAOTRADER_LIVE_ARMED` token for the current config (never logged) |
| | `live --live [--submit]` | live-dry (plan-only) / submit under all four locks; Linux/WSL only |
| health | `doctor [--network] [--live]` | environment, heartbeat and run checks; `--live` raises the idle-proxy alert after 72 h unarmed |

Runbooks, one page each, are in [`docs/runbooks/`](docs/runbooks/): crash recovery, spec change, prune emergency, key compromise, provider outage, SafeMode, and live arming.

## Safety model
1. **Paper is the default.** Backtest, paper and live-dry never sign anything. `config\default.toml` has `[live] enabled = false`.
2. **Four locks** must all pass before anything is submitted (DESIGN.md section 9.3):
   1. `[live] enabled = true`;
   2. `mode = "submit"`, with the network set explicitly, the spec accepted, and LIVE_ELIGIBLE sleeves only;
   3. the CLI flags `--live --submit`;
   4. a ≤ 24 h HMAC arm token in `TAOTRADER_LIVE_ARMED` (any config edit invalidates it), plus `TAOTRADER_LIVE_NETWORK_CONFIRM=finney` on mainnet.

   The CLI checks locks 1 and 3 **before** importing `taotrader.live`. Preflight then checks proxies, `RealPaysFee`, locks, balances, spec, health and `plan()`.
3. **Staking proxy only.** The bot key is a zero-delay `ProxyType::Staking` delegate holding at most 0.5 TAO of fee buffer. The coldkey stays on a hardware signer and never touches a bot host.
   - No code path references transfers, `UnstakeAll` or `Batch`.
   - Buys have explicit limits and caps.
   - Long-only.
   - Exact amounts only, never `'all'`.
4. **User-run live on Linux/WSL.** bittensor is imported only inside `taotrader.live`, which cannot run on Windows. You install it in a separate venv with hash-pinned requirements, and you start the systemd unit (`scripts\taotrader-live.service`, plan-only as shipped). Nothing in this repository's tooling, CI or agents ever runs `--submit`.
5. **Crash safety:**
   - an atomic, hash-chained, append-only journal;
   - idempotent order ids, with a write-ahead bracket before any I/O;
   - unknown outcomes resolved from chain truth, never re-sent blindly;
   - chain truth wins in reconciliation, and entries halt until you clear the quarantine.
6. **Kill switches:** `halt` / the `data\control\KILL` file, `exits-only` and `flatten`. A single-instance lock applies per (mode, run). Alerts are deduplicated, and a dead-man ping stops when the feed stalls.

## Secrets, the Taostats key and the MCP
Secrets never go in the repository, in config files or in argv (a value seen on the command line is refused). The log files and stderr pass through a redaction filter.

Lookup order:
1. the OS keyring, service `taotrader` (Windows Credential Manager target `<name>@taotrader`);
2. the environment variable;
3. `%USERPROFILE%\.taotrader\secrets.env`.

| Name | Env variable | Use |
|---|---|---|
| `taostats` | `TAOSTATS_API_KEY` | Taostats REST key: `collect --enrich`, `verify --taostats` |
| `onfinality` | `TAOTRADER_ONFINALITY_URL` | keyed OnFinality RPC URL (`https://.../rpc?apikey=...`): backfill and the live head |
| `alert_webhook` | `TAOTRADER_ALERT_WEBHOOK` | Discord / Telegram / generic webhook for alerts |
| `healthcheck_url` | `TAOTRADER_HEALTHCHECK_URL` | dead-man ping (e.g. healthchecks.io), every 60 s while healthy |
| `live_arm_hmac` | (keyring only) | live arm-token HMAC key on the live host (`taotrader live arm --init-secret`) |

Store a value without echoing it:
```powershell
.venv\Scripts\python.exe -c "import keyring, getpass; keyring.set_password('taotrader', 'taostats', getpass.getpass('key: '))"
```

If you use `secrets.env` instead, restrict it to your user:

`icacls %USERPROFILE%\.taotrader\secrets.env /inheritance:r /grant:r "%USERNAME%:R"`

**Taostats:**
- The REST key is optional and only enriches or cross-checks data. It is sent as a raw `Authorization: <key>` header.
- The free tier is 5 credits/min; per-endpoint costs are unpublished, so start with small ranges.
- Pool history splices subnet generations, so join it by block only.
- `scripts\run.cmd doctor` shows whether each secret is set; values are never shown.

**Taostats MCP:** the Taostats MCP server is for Claude-side research only (exploring subnets, sanity-checking numbers). It is **never** a bot dependency and never on the decision path. Configure it in your Claude client's MCP settings with your own key. The bot does not read it.

## Layout
- `src/taotrader/`:
  - `core` (pure types);
  - `protocol` (chain-rule replicas);
  - `chain` (reader);
  - `data` (lake, journal, recorder, collector);
  - `features`, `risk`, `portfolio`, `strategies`;
  - `engine` (pure decide, reducer, runner);
  - `venues` (sim, paper);
  - `backtest`, `reports`;
  - `live` (gated);
  - `ops` (config, secrets, logging, alerts, health, lock);
  - `cli.py`.
- `config/`: `default.toml`, `books.backtest.toml`, `books.paper.toml`, `live.example.toml` (copy it to a private `config/live.toml`; never commit it), `preregistration.toml`.
- `scripts/`:
  - `run.cmd`, `supervise.ps1`, `nightly.ps1`, `weekly.ps1`, `backup_sqlite.py`, `register-tasks.ps1` (Windows);
  - `taotrader-live.service`, `taotrader-doctor.service`, `taotrader-doctor.timer` (Linux).
- `docs/`: `DESIGN.md`, `adr/`, `runbooks/`, `research/`.
