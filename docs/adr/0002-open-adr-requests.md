# ADR-0002: Open ADR requests from the WP0–WP12 build (consolidated for the lead)

**Status:** OPEN. This file lists the requests and gives a recommendation for each. **It does not decide anything**: the lead decides, then records each decision in a numbered ADR (or in DESIGN.md) and updates this list.
**Compiled by:** the integration pass, 2026-10-09.
**Sources:** `docs/build/WP_REPORTS.md` (WP0–WP9, review:WP2, review:WP6, review:WP7) and this run's reports (review:WP8, WP10, WP11, review:WP11, WP12).
**Excluded:** WP0's six requests, which ADR-0001 decided. WP4's draft ADR (`draft-wp4-spec-boundaries-and-era-a-fee.md`) is still unnumbered and undecided; its points appear below as D1–D3.

Each entry has an id, the requesting WP(s), the request, and a recommendation (**Rec**). Duplicate requests from several WPs are merged into one entry.

**Priority:**
- **P1** must be decided before any live `--submit`, or it changes money or risk behaviour now;
- **P2** changes research results or contracts that other WPs code against;
- **P3** documentation or a convention only.

---

## Summary

| Area | Ids | P1 items |
|---|---|---|
| A. Live safety and risk inputs | A1–A16 | A1, A2, A3, A4, A5, A6, A7 |
| B. Engine and journal contracts | B1–B13 | B6 |
| C. Data, lake and collector | C1–C10 | — |
| D. Protocol, calibration and regimes | D1–D12 | — |
| E. Strategies, backtest and studies | E1–E10 | — |
| F. Ops and CLI | F1–F5 | F1 (already applied, needs confirmation) |
| G. Unimplemented design items | G1–G4 | — |

The VERIFY items still open (on-chain checks, not ADRs) are listed at the end so they are not lost.

---

## A. Live safety and risk inputs

**A1. Health, or at least the finality lag, in TickContext** (WP7 ADR 2, WP8 ADR 1, review:WP8 still-open 1). **P1.**
- HealthObs is in neither TickContext nor RiskContext. WP7 floors the health rows of the mode table inside the Engine. WP8's planner takes an optional `finality_lag_blocks` keyword, but the Engine never passes it.
- As a result, the planner's §3.12 shield fallback is inactive: when finality lag is above 5 it should send URGENT/EMERGENCY exits unshielded with era 16. With the fallback inactive, every exit stays shielded, and LiveVenue/PaperVenue reject shielded orders as `shield_era_stale`. So no risk exit can land while finality is degraded.
- **Rec:** accept. Add `health: HealthObs` to TickContext in a §5 version bump. Have the Engine pass `ctx.health.finality_lag_blocks` to the planner, and move the WP7 health-floor rows into the overlay. Until then, either ship a stop-gap where the Engine passes `finality_lag_blocks` (the planner keyword is optional, so this is a type-compatible WP7 change), or record in §3.11 that the Engine owns the health floor.

**A2. Key alarms and validation results as journaled inputs to RiskContext** (WP8 ADR 5, WP11 ADR 1, WP11 unresolved). **P1.**
- A live key alarm is journaled today as `ReconAdjusted(evidence="key_alarm:…")`. The reducer folds it to `recon_halt` and LiveVenue freezes itself, but the overlay's mode never becomes FROZEN.
- A failed V1–V7 validation reaches the pipeline only as ModelDriftObserved, which gives CAUTION. So the §3.11 "EXITS_ONLY on validation failure" row cannot be expressed.
- **Rec:** accept. Add two journal kinds:
  - `KeyAlarmRaised(book|"", reasons)`, cleared by QuarantineCleared;
  - `ValidationResult(spec, check, ok)`.
- Have the reducer fold both into BookView or RiskContext fields, and have the overlay map them to FROZEN and EXITS_ONLY. Keep the ReconAdjusted encoding as the interim path, and make sure the new kind's upcaster reads it.

**A3. FailReason and terminal block in the BookView order rows** (WP8 ADR 3, review:WP8 still-open 2). **P1.**
- Without FailReason the planner cannot tell VENUE_REJECT, NOT_PLACED or an early expiry from a chain failure. These outcomes therefore count toward the L_exec re-quote throttle and the ×1.5 limit widening, and "SlippageTooHigh → retry with allow_partial" is approximated as "any FAILED sell".
- **Rec:** accept. Add `fail_reason: FailReason | None` and `terminal_block: Block | None` to the BookView order row; the reducer already has both. Make only chain outcomes count toward the throttle and the widening. Also add `decision_spot` to the row (see B7).

**A4. Live key-alarm consumers: CarrierFeeSettled "inner_included" overload** (WP11 ADR 3). **P1** (alarm semantics).
- The outcome "inner_included" is also used when the delegate nonce is n+2 or higher with no own extrinsic at n+1, i.e. foreign use of the key.
- **Rec:** accept for now; the behaviour is fail-safe because it raises a key alarm. In the next `CarrierFeeSettled` VERSION, add the outcome `foreign_nonce` so reports can tell the two cases apart.

**A5. Live-dry (plan-only) results journaled as `OrderFailed(VENUE_REJECT, fee 0)`** (WP11 ADR 2). **P1** (the LIVE_ELIGIBLE gate reads it).
- Details are `plan_only:accepted:fee=<rao>` or `plan_only:<reason>`. The 100% plan() acceptance gate is computed by parsing these strings.
- **Rec:** accept and record in §9.3. Parsing a detail string is fragile for a promotion gate, so later add a dedicated `PlanChecked(order_id, accepted, fee_rao, reason)` event.

**A6. QuarantineCleared operator path** (WP12 ADR 2 / deviation 1). **P1** (without it, a recon halt has no exit).
- Nothing produced QuarantineCleared. WP12 added `taotrader clear-quarantine --book B --reason R`. The command takes every instance lock, checks the heartbeat head, and appends one non-tick batch at (head, Phase.OUTBOX).
- **Rec:** accept the offline command as the v1 path. Also add `clear_quarantine` to the WP7 control-file vocabulary, so a running live process can take it without a restart; restrictive-wins ordering still applies within a poll.

**A7. Reconcile-hook exceptions crash-loop the Runner** (WP7 open item 5). **P1** (live availability).
- An exception from the live reconcile hook poisons the Runner, and the crash loop lasts as long as the cause does. resolve() instead alerts and retries.
- **Rec:** treat a reconcile-hook exception like resolve(): alert, skip reconciliation this tick, and retry. Escalate to a key alarm after N consecutive failures; WP11 already raises one after 50 failed reconciliation calls. Fix this in WP7's runner and record it in §9.7.

**A8. `VenueBusy` / NoFreeDelegate in core.errors** (WP6 ADR 1). P2.
- reserve() raises `venues.sim.NoFreeDelegate`, a RuntimeError, and the engine may not import venues. WP7 therefore catches any reserve() exception.
- **Rec:** accept. Add `class VenueBusy(TaotraderError)` to core.errors and have SimVenue, PaperVenue and LiveVenue raise it. The Engine then catches only that type, so other reserve() bugs are no longer swallowed.

**A9. Delegate-lock boundary** (WP6 ADR 4). P3.
- **Rec:** accept the rule in force: a delegate is locked through `era_end + 2` inclusive and free from `era_end + 3`. WP6, WP7 (reducer `LOCK_MARGIN_BLOCKS = 2`) and the planner already agree. Record it in §3.12.

**A10. TTL / `valid_until` derivation** (WP6 ADR 5). P3.
- **Rec:** accept and record in §3.12:
  - shielded `valid_until = b + finality_lag + latency`;
  - unshielded `valid_until = b + finality_lag + 16`;
  - both from the same ExecCfg the venue uses.
- This is already implemented in `portfolio/planner.py::_valid_until`.

**A11. Unshielded era-16 anchor: §3.12 and §9.6 disagree** (review:WP6 open 2). P2.
- §3.12 says `valid_until = b + lag + 16`, "the era end". §9.6 says `era_end = finalized anchor + 16 + 2`. Paper does not enforce unshielded era staleness at all.
- **Rec:** adopt §9.6: an era born at the finalized anchor, valid for 16 blocks, plus the SDK's +2 margin only for lock accounting. Add the matching staleness check to PaperVenue: reject when the landing block is past anchor + 15.

**A12. Q19 storage names resolved** (WP11 ADR 4). P3.
- RealPaysFee is `Proxy.RealPaysFeeConsentV1`. The scheduled coldkey swap is `SubtensorModule.ColdkeySwapAnnouncements` plus `ColdkeySwapDisputes`; `ColdkeySwapScheduled` is the pre-475 name.
- **Rec:** accept. Update §9.3 item 3, §9.4 and §13 Q19. Keep reading both key orders until the user-run test.finney procedure confirms one.

**A13. Headless arm-token secret** (WP12 ADR 3). P2.
- `live_arm_hmac` is keyring-only, and a systemd service may have no readable keyring.
- **Rec:** keep the keyring as the default. Add an explicit opt-in file source for this one secret on Linux only: the path comes from config, the file must be mode 0600 and owned by the service user, and the source refuses to work otherwise. Alternatively, document systemd `LoadCredential=`. Do not reuse the general `secrets.env` fallback for it.

**A14. Stale re-drive after a long outage** (WP7 open item 7). P3.
- **Rec:** close this as covered. LiveVenue's stale-intent guard rejects an intent once a fresh finalized head is past `valid_until`, and its head/TTL checks add a second layer (WP11 deviations). Record in §4.5 that the venue guard is the protection.

**A15. Live buy turnover counts FAILED buys** (review:WP8 still-open 6). P3.
- **Rec:** accept. The rule is conservative and only limits buys. Document it in §9.8 #12.

**A16. RealSdk shape assumptions** (review:WP11 residual 1–4, 7, 8). P2. These are mostly VERIFY items, but two of them are policy:
- (a) stake events with an empty extrinsic hash, such as an `on_idle` dissolution StakeRemoved, are classed as foreign and raise a false key alarm;
- (b) arming tracks `spec_version` but not `transaction_version`.
- **Rec:**
  - (a) Whitelist StakeRemoved with no extrinsic hash on a netuid that is DISSOLVING in our state; it is the dissolution payout.
  - (b) Add `transaction_version` to the arm token's hashed inputs (§9.8 #10).

---

## B. Engine and journal contracts

**B1. `Engine.decide` signature** (WP7 ADR 1). P3.
- WP7 added two keyword-only arguments, `inputs` and `store`, and the method returns `list[(LogicalTime, BookId, JournalEvent)]`.
- **Rec:** accept and record it in §4.4.

**B2. DecisionTrace action conventions** (WP7 ADR 3, WP8 journal contract).
- The conventions are `engine.forced_exit`, `engine.order_spot`, `cooldown_until=<block>` and `halt_until=<block>`, plus WP8's rule names.
- **Rec:** accept. Record them in §5.6 and §5.10 as the reducer's fold contract, because they are load-bearing. Longer term, give cooldowns and halts typed fields instead of parsed detail strings.

**B3. Ledger account `adj:recon`** (WP7 ADR 4). P3.
- **Rec:** accept and add it to the fixed account list in §5.7.

**B4. Journal ordering rules** (WP3 ADR 3, WP7 ADR 5).
- The rules are:
  - blocks are globally non-decreasing;
  - (block, phase) is non-decreasing per book;
  - run-level records use book "";
  - an event that names a book is journaled under that book;
  - recovery facts go at the last journaled block, Phase.OUTBOX.
- **Rec:** accept and record them in §7.2 and §4.5.

**B5. Journal hash pre-image excludes batch, seq and idem** (WP3 ADR 4). P2.
- **Rec:** accept for the next journal VERSION: add `batch` to the pre-image. A batch split cannot be detected today. The heartbeat/expected_head check limits the exposure, so this is not urgent.

**B6. Contract changes from review:WP7** (record). **P1** (already in force).
- The changes are:
  - `engine.order_spot` is emitted for every swap intent;
  - `OrderEntry.decision_spot` exists, so old checkpoints fall back to a full replay;
  - one DeregSettled covers all hotkeys of a generation;
  - restrictive control commands win over a resume within one poll;
  - a restart with a pending control file writes one recovery batch.
- The review:WP8 fixes are also in force:
  - the planner applies forced exits before the §3.1 aggregates;
  - the throttle ignores CANCELLED orders;
  - the SafeMode FROZEN exception is removed;
  - LIVE/LIVE_DRY without a LiveCfg fails closed.
- **Rec:** record all of these in §3.1, §3.11, §5.6, §5.10 and §9.7.

**B7. Decision spot for the overlay's abnormal-fill check** (WP7 open item 3). P2.
- `risk/overlay.py::_modelled_shortfall_ppm` evaluates `expected_out` at the fill's `spot_before`, not at the decision spot. This is the same drift contamination WP7 fixed in the sleeve kill switch.
- **Rec:** accept. Expose `decision_spot` on the BookView order row (with A3), and have the overlay use it.

**B8. ModelDriftObserved in the `advance()` contract; no `book` field** (WP6 ADR 2–3). P3.
- **Rec:** accept the §5.10 amendment: advance() may return ModelDriftObserved. Do not add `book`, since drift is pool-level and suppressing it across books is correct.

**B9. Model-drift CAUTION floor duration** (WP7 open item 8). P2.
- Any non-burn-in ModelDriftObserved floors every book at CAUTION for 7,200 blocks. The design gives no duration.
- **Rec:** keep 7,200 blocks, but key the floor by probe. A `sim_swap` drift probe should floor only books that hold, or are entering, that netuid. Record it in §3.11.

**B10. Degenerate pools can crash-loop a tick** (WP7 open item 6). P2.
- `PoolState.spot()` raises DivisionByZero when `px_alpha == 0`. It is reached from `_dereg_payout` and from the `order_spot` action.
- **Rec:** no ADR is needed. Fix it in WP7 the way WP6's review did: a price of 0 on a degenerate pool, so the payout is 0 and the action carries spot 0. Track this as a WP7 bug.

**B11. Strategy memory written on every wake** (WP9 ADR 3). P2.
- Carry is woken on most stride ticks and journals 5–20 KB of memory each time.
- **Rec:** accept "skip the memory write when the canonical bytes are unchanged". The Engine already knows the last journaled memory. This changes journal content, so the WP10 golden digests must be regenerated under this ADR.

**B12. `burn_in_until` visible to the allocator** (WP8 ADR 2, review:WP8 still-open 3). P2.
- The overlay halves every target during burn-in. For cap-bound targets this differs from the design's budget halving.
- **Rec:** accept. Expose `burn_in_until` in BookView, and move the m_burnin multiplier into the allocator's budget step.

**B13. `alpha_h_ppm` on TargetPosition; flow-adjusted NAV samples** (WP8 ADR 4 and 6). P2.
- **Rec:** accept both.
  - Add the `alpha_h_ppm` field and drop the `"alpha_h_ppm=<n>"` reason-string channel.
  - Record `nav_liq_daily` net of CapitalChanged flows, so deposits and withdrawals do not trip, or mask, DD30 and the daily-loss rule.

---

## C. Data, lake and collector

**C1. `[rpc].keys_per_call` = 2,500** (WP1 ADR 1). P3.
- **Rec:** accept. Set `config/default.toml` and the RpcCfg default to 2,500; it is inside the measured-OK range of 2,332–2,732. This changes `config_hash`, so do it before any paper run is started.

**C2. NOT_STARTED semantics before spec 257 / 273** (WP1 ADR 2). P3.
- **Rec:** accept the comment amendment "None and NOT_STARTED unset = runtime without start_call". WP5 already keys on the flag.

**C3. Duplicated era-boundary constants** (WP1 ADR 3; also data/schema.py `era_of` and the engine's era constants). P3.
- **Rec:** expose named constants (`DTAO_LAUNCH`, `ERA_B`, `BALANCER`, …) in protocol.regimes §5.12 and import them where the import contracts allow. Where they do not (data and engine), keep the local copies and the equality tests.

**C4. Lake column `exact_json`; HUGEINT stored as DECIMAL(38,0)** (WP3 ADR 1–2). P3.
- **Rec:** accept both and record them in §7.1.

**C5. Faster codec: per-class field-list cache** (WP3 ADR 5). P3.
- **Rec:** accept as a byte-identical core.codec optimisation guarded by the existing property tests. WP3 can then drop its fast path. Low priority.

**C6. `ParquetReplay(lake, start, end, stride=60, …)`** (WP3 ADR 6). P3.
- **Rec:** accept the signature and update §8.1.

**C7. `collector_meta` table** (WP4 ADR 6). P3.
- **Rec:** accept and list it in §7.3.

**C8. Panel size: take-0 earners** (WP4 ADR 5). P2.
- About 13.7 pairs per subnet against the design's ~6; this triples the backfill cost.
- **Rec:** keep tracking every take-0 earner, because the router needs them. If backfill time binds, cap them by a minimum stake (e.g. ≥ 1,000 alpha), not by a top-N.

**C9. Escrow read grid** (WP10 ADR 2; WP4 deviation). P2.
- One StakeInfo(escrow) call costs about 160 s of public-archive work, so the 360-block §6.7 grid is infeasible on public endpoints.
- **Rec:** allow a daily (7,200-block) grid for research lakes, pinned in `collector_meta`, and keep the `escrow_block` column. Keep 360 for keyed-endpoint backfills and live. Decoding BetaBasketRuntimeApi is not worth it while StakeInfo works.

**C10. Unbounded live-lake ref index** (WP3 unresolved, WP12 unresolved). P2.
- A year-long paper run reaches about 2.6M refs, roughly 0.5 GB in memory.
- **Rec:** decide a roll-up policy. For example, the live series past 30 days is thinned to the 60-block grid at daily compaction, and the per-block rows move to a cold series that is not indexed in memory. This needs a WP3/WP12 follow-up.

---

## D. Protocol, calibration and regimes

**D1. Regime rows from the spec-boundary search** (WP4 ADR 1; WP4 draft §1). P2.
- **Rec:** accept:
  - `chainbuy_unrecorded` = 7,091,127 to 8,283,783;
  - `spec475` = 9,233,782.
- For `taoflow`, use 6,811,691 (spec 334) as the fail-closed choice until the release diff settles 334 against 338.
- Set `SPECS` `touches_econ` for 475 to True; §5.12 / §3.11 already state it. Leave the other specs False until their diffs are read (§13 Q2).
- Renumber the draft as ADR-0003 when it is decided.

**D2. Era-A staking fee** (WP4 ADR 2; draft §2). P2.
- **Rec:** option (b). Keep 33/65535 as a conservative proxy and mark it verified with the draft as evidence. No backtest window trades in era A (carry and the books start at or after 8,765,684). Adopt (a), `ERA_A_FEE_RATE = 0` plus a flat-fee model, only if era-A parity studies are run.

**D3. `LastRateLimitedBlock(0x02)` written at the add block in specs 438–450** (WP4 ADR 3, WP5 ADR 1). P3.
- **Rec:** accept and reword §2.4, §4.3 and §7.1. Q is the victim's removal block, which both WP4 and WP5 already use. REGISTRATION_SEEN fires at the add block in that spec range.

**D4. Recovery-ratio staker base** (WP2 ADR 1; review:WP2 6). P2.
- **Rec:** keep the fail-closed `max(TotalAlphaStaked, AlphaOut − ProtocolAlpha)` until the FT10 replay (§13 Q10) decides. Reword §3.3 now so that it does not claim both definitions.

**D5. Hazard reference values with the tail floor** (WP2 ADR 2; review:WP2 fix). P3.
- **Rec:** annotate the §3.3 reference row with the floored values. The review corrected these to +0.8 / 1.9 / 3.0 pp at Δ 50k / 55k / 65k. Keep the floor.

**D6. Rounding of `marginal_after_buy` (up) and `marginal_after_sell` (down)** (WP2 ADR 3). P3.
- **Rec:** accept and add it to the §5.12 docstrings.

**D7. `fit_hazard` invalidation** (WP2 ADR 4; review:WP2 risk 1). P2.
- **Rec:** accept. Add `invalidated_at: Block | None` to HazardModel so that `fit_hazard` drops rows before it itself (fail closed). The lead should also decide `hazard_invalid_from = 9_233_782` (spec-475 PoW registration) for LakeCalibrationProvider; WP4 notes this.

**D8. Lookahead-free backtest hazard prior; κ_p / Tier-B estimators** (WP4 ADR 4; review:WP2 risk 2). P2.
- **Rec:** accept WP4's choices and record them in §3.3 and §5.12:
  - the parametric Δ-prior from stored globals before asof;
  - the conditional-logit κ_p, shrunk with n0 = 4 and clipped to [2, 8];
  - the moment estimator for the Tier-B jump p.
- Tier B stays disabled until FT1/FT2 pass, at which point the MC calibration should replace the moment estimator.

**D9. `a_earn_growth_per_day` before v441** (review:WP2 risk 3). P3.
- **Rec:** record the caller rule "root_flag=False before 8,765,684" in §5.12. Optionally add a block argument later.

**D10. permit_rank is exact only within the top 5 tracked recipients** (WP5 ADR 2). P2. This is a fail-open risk in the router.
- **Rec:** at each membership point, record the TotalHotkeyAlpha of every AlphaDividendsPerSubnet key (the collector already lists them all), and publish a recipient count in SubnetState. Until then, treat permit_rank beyond 5 as unknown and fail the permit check closed when MaxAllowedValidators < tracked recipients + margin.

**D11. kappa_p in PruneView; `take_known` flag** (WP5 ADR 3–4). P3.
- **Rec:**
  - Drop "kappa_p" from the WP5 entry; consumers read it from the CalibrationProvider (see E1).
  - Add the optional `RouterCandidate.take_known`, so that an unknown take increase is distinguishable from False.

**D12. Brief wave counts** (WP10 ADR 5). P3.
- On chain, 57 subnets were disabled at 8,463,544 and 49 were re-enabled at 9,029,889 (45 started); the brief says 54 / 47.
- **Rec:** correct §10.3 #3 to the chain counts. Define the count as "raw EmissionEnabled flips", and give the started-subnet count separately.

---

## E. Strategies, backtest and studies

**E1. Calibration seam for strategies** (WP9 ADR 1). P3.
- **Rec:** bless constructor injection, `build_strategy(..., calibration=provider)`, in §5.10. Do not add the provider to TickContext. The as-of discipline stays with the provider, which checks `asof <= block`.

**E2. Sleeve capital figure** (WP9 ADR 2). P3.
- **Rec:** accept the convention `G_MAX × NAV_liq × budget_ppm` and document it in §2 and §5.10. A dedicated sleeve-budget field is unnecessary while the allocator applies the stage, burn-in and DD multipliers.

**E3. `wake_on` qualifiers** (WP9 ADR 3).
- **Rec:** covered by B11. An optional Strategy wake filter is not needed if unchanged memory is not rewritten.

**E4. Optional Feat fields** (WP9 ADR 4): `positive_frac_ppm`, `flow_z_6h`, `spot0`. P3.
- **Rec:** defer to v1.2. The store-derived approximations are documented and deterministic.

**E5. LCW `phi_ewma` half-life** (WP9 ADR 5). P2, because it is a prereg change.
- **Rec:** accept 7,200 blocks and add it to `[lcw]` in `preregistration.toml` before any LCW paper run. This changes `prereg_hash`.

**E6. Strategy cadence declarations** (WP10 ADR 3). P2.
- Every strategy declares `min_cadence_blocks = 60`, so a stride-300 run cannot use them unwrapped.
- **Rec:** set `min_cadence_blocks` to each strategy's real data need. The daily-rebalance baselines can declare 300 or more; carry and momentum keep 60. Do not compare against `decide_every_blocks`, because it measures something else.

**E7. Router history requirement** (WP10 ADR 4). P3.
- **Rec:** document it in §8.1 / §3.8. Universe G needs about 18 of the last 20 epochs per hotkey, i.e. about 7,200 blocks of pre-history at a stride of 360 or less. A 600-stride lake cannot trade carry. Do not add a warm-up exception.

**E8. NAV_liq includes the fee float** (WP10 ADR 1; WP7 open item 1). P2.
- **Rec:** accept WP10's research definition, cash + fee float + liq_value, so tx, failure and carrier fees appear in NAV, DD and kill P&L. Change §8.9 and §5.10, and later align the Engine's `DecisionTrace.nav_liq` and the DD governors.
- Also decide WP7 open item 2, the backtest fee-float top-up: with 1 TAO of fee float, about 1,000 orders drive it negative and halt entries. **Rec:** add an automatic top-up from cash, journaled as a SleeveTransfer-like capital move, when the float falls below `min_fee_float`.

**E9. §8.4 adverse-drift stress variant** (review:WP6 open 1, WP10 ADR 6). P2.
- **Rec:** accept. Add `ExecCfg.adverse_drift_ppm: int = 0` (a §5 config change), implement it in SimVenue, and add it to the WP10 sensitivity grid.

**E10. FT10 observed payout ratios** (WP10 ADR 7). P2.
- **Rec:** assign this to WP4. Collect coldkey free-balance deltas over the on_idle dissolution blocks into `generation.observed_payout_ratio`. Until then S6/S7 FT10 stay NOT_EVALUABLE and the R cap stays on, which is fail closed.

---

## F. Ops and CLI

**F1. `.gitignore` the private live config** (WP11, WP12 ADR 1). **P1** (secret-adjacent).
- **Applied by the integration pass** as a minimal safety fix: `/config/live.toml` was added to `.gitignore`.
- **Rec:** the lead confirms it, or reverts it and chooses another path.

**F2. Paper lake and hot paths are relative to the working directory, not `data_dir`** (WP12 ADR 4). P3.
- **Rec:** resolve relative `[paper].lake` / `hot` paths against `data_dir`. The default `data_dir = "data"` keeps today's layout.

**F3. Shutdown compaction without `final=True`** (WP12 deviation 3). P3.
- **Rec:** accept. Hot staging keeps every block until the hourly compaction, which avoids ChunkConflict on restarts within the same hour. Record it in §7.

**F4. CLI exit codes and extra scripts** (WP12 deviations 2 and 6).
- The exit codes are 0 / 1 / 2 / 3 / 4 / 5 / 130. The extra scripts are `supervise.ps1`, `nightly.ps1`, `weekly.ps1`, `backup_sqlite.py` and `taotrader-doctor.service`.
- **Rec:** accept and record them in §4.2 / §12.

**F5. `[live].network` explicitness rule** (WP11 deviation).
- `network` counts as set explicitly only outside `config/default.toml`, or through `TAOTRADER_CFG_LIVE__NETWORK` or a `live.network=` CLI override.
- **Rec:** accept and record it in §9.3 lock 2.

---

## G. Unimplemented design items (decide: required for v1, or deferred)

These are not interface requests. They are DESIGN behaviour the builders documented as not implemented, so the lead must decide whether v1 needs them.

**G1. Pacing of "unwind over ≤ 3 days"** for the DD governor and SUSPENDED sleeves (WP8, review:WP8 5). Today targets drop at once, and execution goes through the band, drain timing and tranching. **Rec:** required before live with more than about 1 TAO per name. Paper can run without it.

**G2. Pre-trade `sim_swap` check** (WP8). **Rec:** required for live. LiveVenue's plan() and runtime quote cover most of it, so confirm whether that satisfies §3.12, and record the decision.

**G3. Inverse-vol budgets** (WP8). They need FT8 first. **Rec:** defer.

**G4. Planner MOVE of a whole position when the router hotkey changes, even when the new target is below REMAINDER_MIN** (review:WP8 4). This costs one extra move fee and one order round. **Rec:** a WP8 fix (exit instead of moving when the target is below REMAINDER_MIN). No ADR is needed.

---

## Open VERIFY items (not ADRs; listed so they are not lost)

- **On test.finney** (user-run):
  - the move tx fee and the AmountTooLow boundary;
  - whether a same-subnet move needs SubtokenEnabled;
  - SafeMode fee behaviour (§13 Q6, WP6);
  - the RealSdk event, extrinsic and proxy shapes and `alpha_price`'s return shape (review:WP11 1–3);
  - the RealPaysFee key order.
- **Measurements:**
  - the 7-day finality-lag measurement (§13 Q8; only 10 minutes so far, WP1);
  - Taostats credits (§13 Q11).
- **Era-A fee:** the variant for specs 265–277 on removes of 1 TAO or more, and the single 25,000-rao add at spec 261 (WP4).
- **Not done:**
  - §13 Q3 (SubnetTaoFlow vs claim_root), Q12 (miner-coldkey flows) and Q22 (take-credit recipient; the fail-closed `TAKE_CREDIT_TO_OWNER = True` is in force);
  - mutmut on amm.py, portfolio.py, reducer.py and planner.py (Linux CI);
  - the Linux contract test for bittensor 11.3.0;
  - the hashed `requirements-live.txt` closure;
  - the cross-OS replay leg.
