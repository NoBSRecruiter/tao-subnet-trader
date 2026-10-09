# tao-subnet-trader: definitive design (v1.1)

**Date:** 2026-10-08. Finney is at spec 475 (live since 2026-10-07); spec 476 is proposed.
**Revision v1.1** applies the design-review findings: exact-amount live exits under per-call Policies, the UNARMED state, buy-only caps, crash recovery of SUBMITTING orders, the extended SdkPort, the N+2 miss rule and unwind time U = 15, `BookView`, the router split, the netting journal event, as-of calibration, and a set of smaller fixes. §5 was re-smoke-tested.
**Status:** the build contract for parallel implementation. If this document and code disagree, fix the code or amend this document through an ADR (`docs/adr/`). Never let them drift silently.
**Inputs:** `docs/research/RESEARCH_BRIEF.md` (source of truth for protocol mechanics), `verified_corpus.json`, five strategy designs, two architecture proposals (lean, robust) and three judge panels (quant-skeptic, protocol-mechanics, engineering). Brief references use §n.m. Facts first established here and not in the brief are marked **(VERIFY)** and are checked at build time (section 13).
**Disclaimer:** this is engineering design. Nothing in it is financial, investment or tax advice. Every number about returns is a prior or a test threshold, never a forecast. Outputs of the system describe what backtests and paper trading showed, nothing more.

---

## 1. Summary and honest expectations

### 1.1 What we are building
A Python 3.11 system for **micro-cap Bittensor dTAO subnet alpha**. It does five things:
- reads the chain natively on Windows with a small custom JSON-RPC reader;
- records a generation-keyed history in DuckDB/Parquet;
- backtests and paper-trades long-only strategies through **one event-driven engine code path**;
- puts every strategy under a single risk overlay;
- ships a **gated LIVE adapter**. The user runs it on Linux or WSL behind a Staking-only proxy. Nothing in our development or agent processes ever submits a live order.

### 1.2 Ground rules
1. **No strategy is presumed profitable.** Backtests and paper trading decide. "Hold TAO" is a legitimate, reportable result and is a benchmark in every table.
2. **Evaluate on total return, in TAO, net of everything:** 1+R = (P₁/P₀)·(I₁/I₀) − swap fees − own impact − tx fees − failed-order fees − dissolution losses. I is the hotkey share-price index (§3.6). Dilution is already in P; never subtract it twice.
3. **Assets are generations** `(netuid, NetworkRegisteredAt)`. No feature, position or P&L series ever crosses a re-registration.
4. **Era-correct prices.** Era A uses T/A, era B the v3 virtual reserves, era C Balancer weights (§2.10, §6.5).
5. **There is no shorting** (`ShortsEnabled = false`). The only levers are selection, timing and cash.
6. **Capital is configurable and unknown.** Every size is pool-relative and carries an exit-slippage budget.

### 1.3 What the evidence says, and what we do about it
| Evidence (brief) | Quality | Design consequence |
|---|---|---|
| Price-only micro-cap longs lost about −0.5%/day in the gate era (SMB −0.47 to −0.59, t ≈ −2 to −2.6; small tercile −0.57 to −0.67%/day) | [R], 66–109 days, T/A-era caveats | No passive micro-cap long. Every sleeve must beat equal-weight total return and a yield×size double sort out of sample |
| Nominator yield +0.3–0.6%/day on micro caps vs ~0.1%/day on large; total-return sign unknown | [P] yield; [R] price | First research deliverable is Study S0, the total-return answer to brief open question 3. Carry is the core hypothesis |
| 1-day continuation (IC +0.026, t 3.8) and 7-day momentum (WML7 +0.43–0.45, t 2.4–2.8) are positive gross; REV is negative (continuation) | [R] long-short, EW, zero lag, gross | Momentum is experimental and research-gated. Its 1-day flow features ship now as filters inside carry. Mean reversion is not a trading sleeve |
| Seeded launches: median −68 to −74% by day 90, 0 of 11 positive | [R] | Launches are a filter (Gatekeeper). The launch carry window (LCW) is paper-only behind flags |
| Prunes about weekly; holders recover 0.35–0.65× spot; the rule matched 52 of 52; 6 of the last 10 were MEV-shielded | [P] | The prune engine is the most important risk component and is built and replay-tested first |
| Emission is concentrated by the Hill gate (rank-32 bar); bottom tercile gets 0 TAO/day; 7,200 alpha/day is minted regardless | [P] | One shared emission/sell-load model, monitoring-only in the overlay until tested |
| EMA half-life ≈8 h; root emission switch-offs happen (54 subnets in one block) | [P] | Per-block risk tripwires; alpha decisions run hourly to daily |
| Swap fee 0.0504%/leg; tx fee ≈τ0.00103 buy and τ0.00083 sell; shielded orders land at N+2 | [P] | One shared cost and fill model. Gate on the conservative impact bound |

### 1.4 Verdicts
| Family | Verdict | One-line reason |
|---|---|---|
| (a) Flow & emission-yield, "carry" | **CORE** (MVP scope) | The only mechanism-grounded positive-carry hypothesis. Ships as yield carry, hotkey router, overlay guards and one sell-load score. Every other module stays behind flags until its ablation passes |
| (b) Momentum / rotation | **EXPERIMENTAL** (research-gated) | Best-supported regularity, but gross, long-short and 1-day. The sleeve reaches PAPER only after offline lagged long-leg tests pass. Its r_24h and nf_24h features ship now as carry filters |
| (c) Mean reversion | **FILTER-ONLY** | Rejected as a trader: no positive evidence, REV says continuation, tiny capacity, highest engineering burden. Survives as an offline event study, a candidate anti-chase entry filter, and microstructure calibration for the order planner |
| (d) New-subnet launches | **FILTER-ONLY** (Gatekeeper); LCW sub-sleeve **EXPERIMENTAL, paper-only** | The Gatekeeper (registry, recorder, mechanical flags) is shared infrastructure owned by the overlay. LCW can never be validated on the available history; it stays paper-only and is never promoted on fewer than 20 paper trades |
| Cross-cutting risk overlay and allocator | **CORE** | Prune, emission-off, owner, SafeMode, liquidity and execution controls are mandatory. Model-based haircuts stay monitoring-only until tested |

### 1.5 Expected edges (priors, not measurements)
- **Carry:** net −0.36 / +0.08 / +0.35 %/day (low/central/high) on deployed capital. Scenario weights 45% bear, 35% base, 20% bull give about +0.01%/day unconditionally. If T0–T6 and 30+ paper days pass, the post-haircut central estimate is +0.04 to +0.06%/day (90% interval about −0.20 to +0.30). The risk-light sub-component is hotkey choice: +0.054 to +0.09%/day versus the 18% default take.
- **Momentum:** central ≈ 0 after costs, with a small positive tail. The designer's own prior is about 1 in 3 that it clears its gates. Half its modelled gross is really carry.
- **Mean reversion (study only):** prior 15–25% that reversion exists at all at N+2 latency.
- **LCW:** base case 1–4 TAO/year; probability of a non-positive mean at least 40%.
- **Risk overlay:** generates no alpha. Avoided loss is about +0.05 to +0.25%/day of NAV for books exposed to the prune ladder, and about 0 for momentum. It must be confirmed by FT1 (out-of-sample prunes) and FT5 (overlay on vs off).
- **Capacity is project-wide, not per sleeve.** All sleeves draw on the same ~20–30 micro-cap pools, so the project can absorb about **100–250 TAO** before marginal net return reaches zero (micro-only mode: 40–120 TAO). Above that, hold TAO or move up to 3,000–8,000-TAO pools, where yields are lower.

### 1.6 Statistical power
- Book SD is about 3–4%/day and tracking error against the EW universe about 1.5–2%/day. A t = 2 on a 0.05–0.10%/day level edge needs roughly **1,400–5,600 days**. Level P&L tests can therefore only **kill on disasters**; they cannot confirm an edge.
- **Promotion rests on component tests that do have power**:
  - cross-sectional ICs with entry lag;
  - realised vs predicted hotkey yield;
  - realised vs predicted structural drift by tercile;
  - realised vs modelled cost and fill rate;
  - prune-guard recall and precision.
- The rank-32 gate has run about 66 days. Spec 475 changed dividend and emission code on 2026-10-07, so **every backtest is out-of-regime by construction**. Post-475 paper data is the only true out-of-sample, and the first post-475 forward period is reserved as the **common untouched holdout** for all sleeves.
- **Discipline:**
  - all thresholds in this document are pre-registered in `config/preregistration.toml`, whose hash is journaled;
  - the project-wide **P&L-tuned parameter budget is 11** (carry 3, momentum 4, MR study 4); everything else is frozen or calibrated against non-P&L targets;
  - every evaluation is counted in the trial registry, and reports print deflated Sharpe and CSCV probability of overfitting.

### 1.7 Build order and first deliverables
1. Reader, generation-keyed 60-block panel, P×I index, fill simulator, and emission/yield replicas re-verified on post-475 blocks (WP0–WP4).
2. Overlay prune engine, then **FT1**, the per-block replay of the last prunes (WP8, WP10).
3. **Study S0**: equal-weight total return of the eligible universe, plus the factor re-runs (SMB, WML7, REV, IC) on the generation-keyed, payout-inclusive P×I panel. This answers brief open question 3 and resets every prior used here.
4. Carry MVP: backtest, then SHADOW, then PAPER with component tests.
5. Offline studies: momentum lagged long-leg IC, launch FT1/FT2, mean-reversion event study. A sleeve is promoted only on a pre-registered pass.

---

## 2. Final strategy specifications

### 2.0 Shared conventions (bind every sleeve)
**Asset and data.** Generation key `(netuid, reg_at)`. Lookbacks are truncated at `NetworkRegisteredAt`. `ΔSubnetTaoFlow` is used only for blocks ≥ 8,466,531 and within one generation. Earlier flow uses the reserve-difference fallback `F = ΔSubnetTAO − Σ(SubnetTaoInEmission + SubnetExcessTao) − Δreservoir`, which is flagged unreliable before spec 411 (8,283,784) because chain buys were unrecorded. `SubnetTaoFlow` records user stake/unstake and registration-collateral buys. It excludes chain buys. Whether it includes `claim_root` sales is **(VERIFY)**.

**Shared models (one implementation, `taotrader.protocol`).**
- `emission_vector`, the exact `get_shares` replica: injection/chain-buy split and gate keep g/b.
- `ema_*`: a = SubnetMovingAlpha·b/(b+EMAPriceHalvingBlocks); a = 0 when the EMA is frozen.
- `prune.*`: ladder, t*, hazard, recovery.
- `yield_model.*`: closed form, A_earn and its deterministic growth.
- `sellload.sell_load`: the single structural sell-load model.

**Read live, never hard-code:** SubnetOwnerCut (a **global** StorageValue, default 11,796), SubnetAlphaOutEmission, runtime block emission (never `BlockEmission` storage), SubnetMovingAlpha, EMAPriceHalvingBlocks, EmissionGateBar/Rank/Exponent, TaoWeight, FeeRate, SwapBalancer (k_w = 1/w_base), NetworkImmunityPeriod, NetworkRateLimit, NetworkLockReductionInterval and SubnetLimit. No `0.18`, `0.41`, `2952`, `0.5 TAO`, `k_w = 2`, `14,400` or `720,000` literal is allowed outside tests (lint rule).

**Share index.** I = TotalHotkeyAlpha / shares, where shares = TotalHotkeyShares **V1 if present at that block, else V2** (SafeFloat m·10^e), per (hotkey, netuid), up to block 9,217,507. The decoder is validated against the closed form on SN70, SN92 and SN64.

**One cost and fill model** (`ExecCfg`, `protocol.amm`, `venues.sim`):
| Component | Value | Notes |
|---|---|---|
| Swap fee f | FeeRate/65535 per pool leg on the input side (33 → 0.050355%) | none on a same-subnet move; charged once on a cross-subnet move |
| Tx fee buy / sell | 0.001028 / 0.000837 TAO | Proxy + shield carrier, measured |
| Tx fee same-subnet move / rotation | ≈0.0010 / 0.00125 TAO | move_stake **(VERIFY)**; swap_stake_limit measured; move_stake_limit unmeasured |
| Carrier on a shield miss | 0.000098 TAO | charged only if the carrier was included (inner dropped); a never-included carrier costs 0. Sim models the measured 1.1% (included, not decrypted) and books the fee on the miss; live books it on the miss when the carrier is in N+2, else after era end via `CarrierFeeSettled` (§9.6) |
| Impact bound used for gating, sizing and headline reports | **TEMPORARY**: RT(V) ≈ 2f + 2V/(T+V) + 0.001865/V | buy on the pool, sell on the healed pool |
| Optimistic bound (reported) | **PERSISTENT**: RT ≈ 2f + 0.001865/V | own footprint persists |
| Fill timing | decision at finalized block b; fill at b + finality lag (≈3) + 2 | paper and refined windows fill a shielded order only at exactly that block (N+2), else MISSED; stride replays use the first available snapshot at or after it (§8.4) |
| Failures | 1.1% shield miss (sweep 0.5–3%); limit failures per the chain's strict semantics | a failed inner call pays the full fee |
| Marks | every position and every stop at executable value = one-shot `liq_value` (sim_sell), never spot | spot marks inflate P&L by ≈V/T |

Round trip at the TEMPORARY bound (T = 3,000 TAO): 0.25% of pool → ≈0.62%; 0.5% → ≈1.11%; 1% → ≈2.09%; 2% → ≈4.0%. The impact-optimal size for expected gross edge α_h over the hold is **V\* = T·(α_h − 2f)/4**, with net at optimum T·(α_h − 2f)²/8. Net is about half of gross at the optimum, and over-sizing costs more than under-sizing.

**Stages.** RESEARCH (offline) → SHADOW (live feed, signals journaled, zero budget) → PAPER (paper budget) → LIVE_ELIGIBLE (the user decides).
- Promotion needs the sleeve's pre-registered component tests plus the promotion rules in §8.9 and the paper gates in §9.1.
- Demotion is automatic through the kill rules in §3.11.

**Universe count.** Before a sleeve is built, the harness reports the eligible-universe size and composition at ≥10 historical dates and daily in paper. If a sleeve typically has fewer than 6 qualifying names, its capacity statement is restated.

### 2.1 (a) Carry: hotkey-optimised nominator-yield carry, screened by structural sell load. Verdict: **CORE**

**Thesis.** Hold staked alpha on the best near-zero-take dividend hotkey. Hold only where forecast yield plus structural price drift, net of prune hazard and the conservative round-trip cost, clears a hurdle. Price is a sum of flows: d ln p ≈ k_w·(CB + F)/y per day, with CB the chain-buy TAO/day and F the net user flow. Emission-recipient sell load is the part of F that is computable ex ante. Yield and sell push scale differently (1/A_earn vs 1/x), and their ratio, not the headline APY, decides. The honest prior is that the edge is small and possibly zero. The design is built so that components fail cheaply.

**Derived facts the design relies on** (re-derived and confirmed by the protocol judge):
- Chain buys exist only if E > rp·alpha_emission·spot·7200.
- At spot ≈ EMA with no burn, CB > 0 needs b ≥ 0.63θ (rp 0.13), 0.95θ (0.30), 1.16θ (0.40), 1.48θ (0.50) or 4.97θ (0.65). It is **impossible for rp ≳ 0.655**, which covers about the first 70 days. Young micro caps therefore get price-neutral injection and keep their full sell load.
- After a spot drop, CB rises: the cap tracks spot while E tracks the EMA.
- The reflexive loop (price → EMA → share → CB) is real but slow, k_eff ≈ 0.4–1.9%/day. It is a tilt at most, never a trade.

**MVP scope vs deferred modules.**
| Module | v1 status | Turns on only if |
|---|---|---|
| Yield carry on the router hotkey | ON | — |
| Hotkey router (owned by the overlay, §3.8) | ON | — |
| One structural sell-load score (`protocol.sellload`, fixed priors φ_o 0.8, φ_m 0.6, κ_b 0.02/day) | ON (score term) | T3 kill sets φ = 0 |
| Observed chain-buy push (trailing 300-block mean of SubnetExcessTao) | ON | — |
| 1-day flow gate and flow-crash exits (momentum features) | ON | — |
| Expected prune loss λ·(1−R) | ON | — |
| Forward chain-buy simulator (14,400-block emission projection) | OFF | T2b passes AND T9 ablation ≥ +0.03%/day |
| Gate-boundary tilt (band relative to live EmissionBarRank: N−10 … N+13) | OFF | same |
| Flow persistence/reversal score terms (a1, a2) | OFF (a1 = a2 = 0) | T3 coefficients significant AND T9 |
| Market intercept m_mkt | MOVED to the overlay's single regime throttle (monitoring) | FT-R1 |
| Emission re-enable event module | OFF | T7a |
| Drain-timing exits | ON in the planner for all NORMAL sells (≤0.025% per epoch, free) | — |

**Universe** (stricter than the overlay floor in §3.2; read live at the decision block):
| # | Rule |
|---|---|
| C-U1 | Non-root, in NetworksAdded, FirstEmissionBlockNumber set, SubtokenEnabled, NetworkRegistrationAllowed, SubnetEmissionEnabled (absent = true) |
| C-U2 | since_start = block − (FirstEmissionBlockNumber − 1) ≥ 45 d (324,000 blocks) |
| C-U3 | Pool 500 ≤ y ≤ 8,000 TAO (micro-only mode: y < 3,000). Planned V/y ≤ v_cap |
| C-U4 | Prune: non-immune requires prune_rank ≥ 8 AND ρ = EMA/EMA_bottom ≥ 2.0. Immune with expiry inside H_eval + 3 d requires projected rank ≥ 8 and ρ ≥ 2.0 at expiry (flat-spot EMA forecast). Never the current target |
| C-U5 | MinerBurned < 0.20 (research flag `allow_burn_bucket` admits up to 0.5 at half size) |
| C-U6 | Router hotkey exists with take ≤ 5%, membership ≥ 90% of the last 20 epochs, positive Δln I in ≥ 90% of the last 20 epochs, realised net yield ≥ 0.10%/day, and realised/closed-form ratio in [0.65, 1.35] |
| C-U7 | Owner not distributing: owner_sold over 7,200 blocks ≤ 0.5% of pool (TAO-equivalent); no SubnetOwner/OwnerHotkey change or autolock-off flip in 7 d; no overlay owner cooldown |
| C-U8 | Overlay mode NORMAL and EmissionView.model_ok |
| C-U9 | No carry risk exit on this generation in the last 3 d |
| C-U10 | Flow gate: flow_1d ≥ −0.5% of y AND flow_z_1d ≥ −1.0 (robust z vs trailing 30 d, same generation) |
| C-U11 | Escrow E/x ≤ 0.25 |

**Signals** (named storage → formula; refresh cadence):
| Signal | Formula | Inputs | Refresh |
|---|---|---|---|
| pool | y = SubnetTAO/1e9; x = SubnetAlphaIn/1e9; w_q = SwapBalancer.quote/1e18; w_b = 1 − w_q; k_w = 1/w_b; p = (w_b/w_q)·y/x; f = FeeRate/65535 | SubnetTAO, SubnetAlphaIn, Swap.SwapBalancer, Swap.FeeRate | every snapshot; re-quote with sim_swap before any order |
| yield_real | r_h(epoch) = ln(I_h(B)/I_h(B_prev)) at drain blocks B; Y_real = EWMA(half-life 20 epochs, K = 40)·(7200/Tempo); h = the book's router hotkey (`book_view.router`, §3.8), or `Feat.best_candidate` when none is assigned yet | TotalHotkeyAlpha, TotalHotkeyShares(V1/V2), LastEpochBlock | each EPOCH_DRAIN |
| yield_cf | AE·(1−c_o)·0.5·(1−rp)/A_earn·(1−take)·(1−ck_take); AE = 7200·SubnetAlphaOutEmission; c_o = SubnetOwnerCut/65535 if OwnerCutEnabled | + Delegates, ChildkeyTake, RootProp, AlphaDividendsPerSubnet keys | each epoch |
| yield_fcst | Y_f = Y_real · A₀/(A₀ + G·H/2) · (1 − yield_haircut). G = deterministic growth (`a_earn_growth_per_day`: compounding + escrow deposits while Σ EMA > 1) + κ_e·max(net inflow alpha/day, 0), κ_e = 0.7. Check: if trailing 14-d A_earn growth exceeds G, use trailing, clipped at 3%/day | a_earn, flows | each rebalance |
| struct | D = cb_push − sell_push, both from `protocol.sellload` (k_w·CB/y and k_w·S/y; CB = trailing-300-block mean SubnetExcessTao·7200) | SubnetAlphaOutEmission, MinerBurned, OwnerCut*, escrow E, SubnetExcessTao | every 300 blocks |
| hazard | λ = p_reg(H_eval)·exp(−κ_p·(ρ−1))/H_eval per day (κ_p = 4, logistic-calibrated in FT1; hazard, κ_p and R come from `CalibrationProvider.asof(block)` in backtests, §8.10); expected prune loss per day = λ·(1 − R) | PruneView, recovery_ratio | every snapshot for held, 60 blocks otherwise |
| score | μ = Y_f + D − λ(1−R); μ_lcb = μ − unc_z·σ_model, σ_model = rolling 30-day robust SD of (realised − predicted) 5-day return pooled by size tercile (prior 0.30%/day until 30 days of residuals exist) | above | every 300 blocks and on wake events |
| size | V\* = y·max(μ_lcb·H_eval − 2f − tx/V, 0)/4 (one fixed-point iteration); μ_net = μ_lcb − RT_temp(V)/H_eval | pool | same |
| flow | flow_1d = ΔSubnetTaoFlow(7,200)/SubnetTAO; flow_6h; flow_z_1d robust z (median/1.4826·MAD over 30 d) | SubnetTaoFlow | every snapshot |
| owner | owner_sold(Δ) = max(0, A₀·I₁/I₀ + accrual − A₁) on the owner coldkey position on the owner hotkey; accrual is the §3.6 formula (full owner cut with **no** (1 − AL) factor, plus validator-take credits) | AlphaV2/Alpha(owner_hotkey, owner_coldkey, netuid) **(VERIFY hashers)**; owner hotkey's HotkeyIdx (tracked, §5.12) | each FULL snapshot |

**Entry** (every 300 blocks; at most N_max = 12 names; long-only):
1. C-U1 to C-U11 pass, μ_net ≥ μ_in (0.10%/day), V ≥ V_min (1 TAO), and V/y ≤ v_cap.
2. Rank qualifiers by μ_net/max(σ_d, 3%) and keep the top N_max. Holdings need only μ_net ≥ μ_out (hysteresis).
3. Emit `Signal(TARGET, weight_ppm = V/sleeve_budget, max_size_rao = V, edge_ppm_day = μ_lcb, alpha_h_ppm = μ_lcb·H_eval, horizon = H_eval, declares_dilution = True)`. The hotkey is the router's. Limits and shielding belong to the planner (§3.12).

**Exits.** Sleeve-level exits set this sleeve's target to 0. Overlay forced exits (§3.3–3.6) apply on top and are authoritative.
| Exit | Rule | Urgency |
|---|---|---|
| C-X1 owner distribution | owner_sold over 1,800 blocks > 0.5% of pool, or SubnetOwner/OwnerHotkey change, or autolock off | HIGH |
| C-X2 flow crash | flow_6h < −2% of y AND robust z ≤ −3.5, or a LARGE_FLOW outflow ≥ 2% of y in one snapshot | HIGH |
| C-X3 hotkey loss | the router has no eligible hotkey for 2 consecutive epochs | NORMAL |
| C-X4 thesis stop | executable-value total return since entry ≤ −20% (dd_stop) | HIGH |
| C-X5 stricter prune | rank ≤ 3 or ρ ≤ 1.3 while the registration window is open or opens within 1,800 blocks | HIGH |
| C-S1 decay | μ_net < μ_out (−0.05%/day) at 2 consecutive evaluations, after min_hold 2 d | NORMAL |
| C-S2 rank | out of top N_max + 3 for 2 evaluations and μ_net < μ_in | NORMAL |
| C-S3 max hold | 21 d forces re-underwriting through the entry path (no churn if still top) | NORMAL |

**Event triggers:** decide every 300 blocks. Wake on EPOCH_DRAIN (held), EMISSION_TOGGLED, DEREGISTERED, LARGE_FLOW (held), OWNER_POSITION_CHANGED, OWNER_CHANGED, AUTOLOCK_TOGGLED, TAKE_CHANGED, DIVIDEND_MEMBERSHIP and PRUNE_TARGET_CHANGED. Spec and param changes are handled by the overlay mode. `valid_from_block` = 8,765,684 (rank-32 gate). `min_cadence_blocks` = 60.

**Sizing.** V = min(V\*, v_cap·y, y·s_max/(1−s_max), w_max·capital) × m_burn.
- v_cap = 1.0%, s_max = 1.0%, w_max = 12%, N_max = 12, V_min = 1 TAO.
- Re-size each rebalance; trade only if |target − held| ≥ max(V_min, 25% of target).
- Profit-max sizes land at 0.15–0.7% of the pool. Capacity is ≈60–250 TAO for this sleeve alone, and is shared with the other sleeves (§1.5).

**Parameters** (T = P&L-tuned; F = frozen; C = calibrated against a non-P&L target):
| Name | Default | Range | Kind | Rationale |
|---|---|---|---|---|
| H_eval | 5 d | 2–14 | T | amortisation horizon; break-even holds 1.6–2.8 d at 0.25–0.5% of pool |
| mu_in | 0.10 %/day | 0.03–0.40 | T | net-of-cost entry hurdle |
| unc_z | 0.5 | 0–1.0 | T | winner's-curse shrinkage |
| mu_out | −0.05 %/day | −0.20–0.05 | F | hysteresis |
| phi_o / phi_m | 0.8 / 0.6 | 0.3–1.0 / 0.2–1.0 | C (T3 direct measurement) | fraction of liquid owner/miner emission sold |
| kappa_b | 0.02/day | 0.01–0.04 | C | basket claim release |
| kappa_e | 0.7 | 0.3–1.0 | F | share of net inflow landing in earning hotkeys |
| yield_haircut | 0.10 | 0–0.30 | F | realised-to-forward uncertainty |
| kappa_p | 4 | 2–8 | C (FT1 logistic) | P_bottom = exp(−κ_p(ρ−1)) |
| min_age_start | 45 d | 14–90 | F | EMA warm-up and launch decay |
| T_min / T_max | 500 / 8,000 TAO | 300–1,500 / 3,000–20,000 | F | depth band |
| v_cap / s_max / w_max | 1.0% / 1.0% / 12% | 0.5–2 / 0.5–2.5 / 5–25 | F | pool-relative caps |
| N_max / V_min | 12 / 1 TAO | 4–20 / 0.5–3 | F | diversification and fixed-fee floor |
| rank_in / rho_in | 8 / 2.0 | 5–15 / 1.5–3.5 | F | entry distance from the ladder |
| rank_exit / rho_exit | 3 / 1.3 | 2–5 / 1.15–1.6 | F | sleeve prune exit |
| burn_max | 0.20 | 0–0.5 | F | MinerBurned cap |
| owner_exit_frac | 0.5% of y per 1,800 blocks | 0.2–1.5 | F | owner dumps cost −40–50% within hours |
| flow_exit | 6 h ≤ −2% of y AND z ≤ −3.5 | −1 to −5% / −2.5 to −5 | F | rarely triggered |
| dd_stop | 20% (executable value) | 10–35 | F | tail cap |
| min/max hold | 2 d / 21 d | 1–5 / 10–45 | F | churn control |
| rebalance_blocks | 300 | 60–1,200 | F | inputs move at EMA speed |

**Cost model:** the shared model, gated at the TEMPORARY bound. Fees are read live. The tx fees are amortised in V\*. The 0.0504% swap fee is charged per leg.

**Falsification tests** (pre-registered; walk-forward; generation-keyed; P×I in simple TAO returns; payouts included; regime splits at setCode+1):
| Test | Procedure | PASS | KILL / action |
|---|---|---|---|
| **T0 baseline (first)** | Study S0: EW total return of C-U-eligible names (router hotkey, overlay guards, TEMPORARY costs, 5–14 d holds) over the gate era and post-June era | reported, no threshold | every module must beat it out of sample |
| T1 data identity | regress 1-d Δln p on k_w·CB/y and k_w·ΔSubnetTaoFlow/y with subnet and day FE, on 60-block data with \|flow\| > 0.2% of y | both coefficients in [0.7, 1.3], R² ≥ 0.7 | fix data or model before anything else |
| T2a emission parity | E_model vs 7200·(SubnetTaoInEmission + SubnetExcessTao + Δreservoir) on all snapshots ≥ 8,765,684 **and again on post-475 blocks** | `protocol.emission.parity_ok`: median abs rel error < 1% AND ≥ 90% within 5%, over enabled subnets with E > 1 TAO/day, on trailing 300-block observed emission (the same function defines `EmissionView.model_ok`) | HALT carry (model_ok false) |
| T3 sell load (re-specified) | (i) direct: φ_o from owner-position diffs vs accrual; φ_m from miner-coldkey flows (Taostats stake-events once the key exists, else top-5 miner coldkey positions per epoch). (ii) cross-check: k_w·ΔF/y regressed on owner, miner and basket components of k_w·S/y, with controls ln y, age since start_call, rp, EMA rank, trailing 1d/7d returns, ladder distance and day FE; errors clustered by subnet and day | (i) φ estimates in [0.3, 1.0]. (ii) owner and miner betas in [−1.5, −0.3], combined t ≥ 2, same sign in both halves; cross-sectional SD of k_w·S/y ≥ 0.3%/day | φ = 0 (pure yield carry) if the 90% CI of the combined beta includes 0 |
| T4a selection | walk-forward (fit 45 d, trade 14, slide 7): top vs bottom μ quintile, 3- and 5-day forward total return, 5-day block bootstrap ×10,000 | spread ≥ +0.25%/day with t ≥ 2, quintiles monotone (Spearman ≥ 0.8), IC ≥ 0.04 with t ≥ 2, **and** beats the yield×size double-sort benchmark by ≥ +0.05%/day with t ≥ 1.5 | KILL the score if spread ≤ 0 or t < 1. If it does not beat the double sort, ship the double sort instead |
| T4b level (kill-only) | top-quintile net at rule sizes, TEMPORARY costs | — | hold TAO if the 90% upper bound < 0 |
| T5 cost robustness | fees ×2, slippage ×1.5, tx ×2, +0.2% adverse N+2 drift, 1% failures with fees, holds cut to min_hold | net ≥ 0 on the top quintile at v_cap 0.5% | KILL if negative at costs ×1.5 |
| T6 hotkey router | paired excess Δln I of the ex-ante router choice vs the median take ≤ 18% member over 5 d | ≥ +0.03%/day with t ≥ 2; switching losses < 0.02%/day | static best-take hotkey if < +0.01%/day |
| T9 ablation | nested: yield-only → + sell load → + flow gate → + each deferred module | each adds ≥ +0.03%/day OOS (t ≥ 1.5), same sign in ≥ 2 of 3 sub-periods | drop the module |
| T12 paper components (≥ 30 days, replaces the P&L-sign gate) | (a) realised vs predicted yield; (b) realised vs predicted structural drift by tercile; (c) realised vs modelled cost; (d) guard recall | (a) bias within ±0.05%/day and ≥ 80% of cycles with positive Δln I; (b) bias within ±0.15%/day; (c) mean error ≤ 10 bp, p90 ≤ 30 bp; (d) no unguarded prune or disable loss | HALT on rolling 30-day net < −3% of deployed, drawdown > 10%, or model_ok false > 24 h |
| T7a / T11 | enable-event CAR / drain microstructure | as in the original design | modules stay OFF |

Statement carried in every carry report: **the level edge cannot be confirmed in under about 1–2 years of data; only components and disasters are testable now.**

### 2.2 (b) Momentum / rotation. Verdict: **EXPERIMENTAL (research-gated)**

**Thesis.** Price is reserves. Net flow persistence is return persistence. The measured effect is roughly a 1-day continuation with a weak tail: alpha_days(H) = min(H, 1) + 0.25·max(H − 1, 0). Costs are the same order as the edge, so the sleeve is cost-gated and mostly sits in TAO.

**Required fixes (applied):**
1. The cost gate uses the **TEMPORARY** round trip. The original `candidate()` sold into the post-buy pool, which was the optimistic bound and over-admitted trades by 0.2–0.6%.
2. Shared share-index decoder.
3. dp/struct terms come from `protocol.sellload` with live parameters.
4. Regime boundaries come from the shared setCode+1 table.
5. NetworkRateLimit is read live; prune_possible is applied.
6. Launch-age eligibility comes from the overlay/Gatekeeper.
7. Rotation is disabled until move_stake_limit fees and outcomes are verified (FT-M5c) and the planner's MOVE_STAKE_LIMIT path is enabled.

**MVP (the only thing that can reach PAPER).**
- **Universe:**
  - overlay floor;
  - 600 ≤ T ≤ 10,000 TAO (micro class < 3,000);
  - age_reg ≥ 30 d AND (age_reg ≥ 90 d OR EMA rank ≤ 40);
  - MinerBurned ≤ 0.5;
  - quote weight in [0.45, 0.55];
  - router hotkey take ≤ 2% and ChildkeyTake = 0;
  - ≥ 7 d same-generation history with no SubnetTaoFlow reset;
  - non-immune prune_rank ≥ 7.
- **Signals:**
  - pbar_t = median(p_t, p_{t−60}, p_{t−120}) (era-correct p);
  - r_24h = ln(pbar_t/pbar_{t−7200}) and r_7d (50,400 blocks);
  - nf_24h = ΔSubnetTaoFlow(7,200)/SubnetTAO;
  - S_mvp = z(z(r_24h) + z(r_7d) + z(nf_24h)), with z the rank-gauss transform over the eligible set;
  - universe drift m_u = clip(0.5·mean 14-day EW daily Δln P×I, −0.5%, +0.3%);
  - EH = H_e·(m_u + y_n) + b_S·S·alpha_days(H_e), with y_n = min(closed form, trailing router Δln I);
  - net_edge = EH − RT_temp(V) − 2·lat.
- **Entry** (every 300 blocks): top K + K_buf by S; S ≥ S_in; r_24h > 0; net_edge ≥ 0.25%; V ≥ 1.5 TAO; at most 2 new entries per cycle; trailing-24h one-way turnover ≤ 30% of sleeve NAV.
- **Sizing:** V = min(T·max(0, EH − φ)/(4λ), ρ·T, T·s_exit/(1−s_exit), w_max·sleeve NAV), with φ = 2f + 2·lat, λ = 1.5, ρ = 0.30% (micro) / 0.50% (near-micro), s_exit = 1%, w_max = 30%. Adaptive min hold = clip(RT/daily_edge, 1, 4) d.
- **Exits:**
  - overlay forced exits;
  - trailing stop on executable value, stop = clip(2.5·σ_d, 10%, 25%) from peak (HIGH);
  - flow reversal: nf_1h ≤ −2.5% or nf_4h ≤ −5% of pool (HIGH);
  - decay: S < S_out on 2 cycles or rank > K + K_buf;
  - time: 3 d micro / 5 d near-micro, re-underwritten at most twice;
  - universe fail on 2 cycles.
- **Triggers:** decide every 300 blocks; wake on LARGE_FLOW (held), EMISSION_TOGGLED and DEREGISTERED. valid_from 8,466,531. min_cadence 60.

**Deferred hypotheses** (code behind flags, each with its own test):
| Feature | Test |
|---|---|
| P1 impulse path | F14 shows edge surviving to t+3 blocks |
| ema_gap and struct_level/struct_lag | F4 t ≥ 2 after controls |
| atomic rotation | F5c |
| breadth gate and micro weekly-reversal guard | F8 layer; the reversal evidence is from the dead Taoflow regime |
| blow-off trim | F9 pump symmetry |

**Parameters:**
| Name | Default | Range | Kind |
|---|---|---|---|
| S_in / S_out | 1.0 / −0.25 | 0.5–1.75 / −0.75–0.25 | T (S_in), F |
| rho micro/near | 0.30% / 0.50% | 0.15–0.75 / 0.25–1.2 | T |
| H_e | 2 d | 1–4 | T |
| k_stop | 2.5 | 1.5–4 | T |
| b_S | 0.0010 per z per day | 0.0005–0.0020 | C (walk-forward ridge shrunk 50% to the prior) |
| alpha_tail | 0.25 | 0–1 | C (F3) |
| K / K_buf | 5 / 2 | 3–8 / 0–4 | F |
| lam / s_exit / w_max | 1.5 / 1% / 30% | 1–3 / 0.5–2 / 20–40 | F |
| net_margin | 0.25% | 0.1–0.8 | F |
| lat | 0.05% per side (stress 0.3%) | 0–0.3 | F |
| limit_eps | 1% | 0.5–2.5 | F (planner β overrides when larger) |
| turnover_cap | 0.30 of NAV/day | 0.15–0.6 | F |
| T_min / T_max / micro_cut | 600 / 10,000 / 3,000 | — | F |
| take_max | 2% | 0–5 | F |

**Cost model:** the shared model at the TEMPORARY bound, buy 0.001028 and sell 0.000837 TAO. The rotation cost (fee once on the origin leg, ≈0.00125 TAO) applies only after FT-M5c.

**Falsification tests** (all offline before any code beyond the MVP sleeve; promotion to SHADOW needs F1, F3, F14 and F15):
| Test | PASS | KILL |
|---|---|---|
| F0 prior re-derivation | WML7, REV and 1d IC re-run on the generation-keyed, payout-inclusive P×I panel (S0). The b_S prior is reset from it | — |
| F1 existence (cross-section, N ≈ 100, entry lag L ∈ {0, 300, 1200}) | IC(300) ≥ 0.015 with 90% bootstrap lower bound > 0 post-Taoflow; ≥ 0.01 in the gate era; IC(300)/IC(0) ≥ 0.5 | IC(300) < 0.01 or its CI includes 0 |
| F2 tradable universe (N ≈ 30) | point ≥ 0.01 and lower bound ≥ −0.01 (power is low; this can only reject) | point ≤ 0 |
| F3 horizon decay | IC(day 1) > 0; days 2–3 cumulative excess not below −0.1%/day | day-1 long-leg excess below the modelled RT at every pool size |
| F5 cost realism | local sim vs sim_swap ≤ 2 bp on 200 triples; paper slippage error mean ≤ 10 bp, p90 ≤ 30 bp; failures ≤ 5% | — |
| F6 net walk-forward (kill-only) | — | mean net < 0 at 10 TAO with 1.0× costs |
| F7 cost stress | net ≥ 0 at 10 TAO with slippage ×1.5, tx ×2, lat 0.3% | label fragile, do not promote |
| F8 ablation ladder | each layer beats its bootstrap s.e. | remove the layer |
| F9 regime split | no net < 0 in 2 of 3 post-June sub-eras; leave-top-3-out > 0; no subnet > 35% of gross | otherwise KILL |
| F10 placebos | beats 95th percentile of 1,000 permutations; lagged signal ≈ 0; beats random entry by ≥ 0.05%/day | — |
| F11 flow validity | nf vs Σ(StakeAdded − StakeRemoved) within 10% on ≥ 30 subnet-days | use the reserve-difference flow |
| F12 capacity | capacity (largest AUM with net ≥ 0.03%/day) ≥ 20 TAO | not worth live capital |
| F14 latency (300 impulses, per-block windows) | return from t+60 blocks to +1 d exceeds one-way cost | P1 stays dead if the move completes by t+3 |
| F15 attribution | price-selection component > 0 after costs vs EW buy-and-hold of U at the same exposure | the sleeve is disguised carry: do not promote |
| F16 paper gate | ≥ 60 days forward, realised RT within 30% of modelled, fill failures ≤ 5%, no unreconciled position | — |

### 2.3 (c) Mean reversion. Verdict: **FILTER-ONLY** (rejected as a trading sleeve)

**Why it was rejected:**
1. There is no positive evidence. The closest evidence points the other way: 1-day REV −0.53 to −0.57 (t ≈ −3) means continuation, and dips cluster in dying, diluted subnets.
2. The AMM has no restoring force. Mechanical refill is ≤ 5–10% of an 8% shock in 12 h and zero below the gate. The rest must be behavioural recovery inside a 12 h window, after a 2-minute stabilisation wait plus N+2 latency.
3. Economics are tiny: ≈0.05–0.25 TAO per event, 10–50 TAO useful capital, and porous stops with a 0.35–0.65× prune tail.
4. It carries the highest engineering burden: per-block System.Events decoding, heuristic flow labels and fragmented calibration cells.
5. Its own power floor (N ≥ 40 tradable shocks) may never be reached in the gate era.

**What survives, and why each piece is kept:**
1. **Offline event study E-MR** (`backtest/studies.py`, WP10). It answers cheaply whether reversion exists. Method:
   - shocks are detected **without outcome selection**, from per-block `ΔSubnetTaoFlow` change-sets (state_queryStorage over the 128 SubnetTaoFlow keys after 8,466,531; the flow changes only on user trades);
   - a shock is a single-block φ = ΔF/T ≤ −3.92% (log price move ≤ −8%), with quiet-before (≤ 1 prior ≥ 4% drop in 300 blocks; 24 h ≥ −25%; 7 d ≥ −45%);
   - pool windows are then fetched with state_queryStorageAt at t−1 … t+H;
   - the price identity splits user flow from chain buys (k_w·ln of the TAO-reserve change after removing only tao_in);
   - the post-shock protocol bid is modelled as max(0, E(EMA) − 7200·rp·p_post), because CB rises after a drop;
   - H is set from the live half-life ln2/a_n.

   Pre-registered thresholds:
   - power floor: N ≥ 80 shocks, ≥ 40 after filters, else "insufficient power";
   - F1: 90% lower bound of recovery ρ_H ≥ ρ* + 0.05 (≈0.30 at D = 8%) at ≥ 2 of {1, 6, 12 h} (Bonferroni);
   - F4: matched-placebo excess at 12 h ≥ +1.0% with p < 0.05;
   - F7: net at a 12-block delay ≥ 0 and ≥ 50% of net at 2 blocks;
   - F9: pump symmetry;
   - F15: identity residual < 0.01 on ≥ 98% of non-migration blocks.

   A trading sleeve is re-proposed (ADR) only if ρ_lcb ≥ ρ* + 0.05 with N ≥ 40 and the edge survives a ≥ 12-block delay.
2. **Spike guard (candidate overlay entry filter, default MONITOR):** no new entry for any sleeve while `fast_ema_gap ≥ +6%` (local 600-block-half-life EMA of spot, computed in the feature engine; SubnetFastMovingPrice only as a cross-check). It is activated only if F9 shows pumps revert AND an overlay-on/off ablation (FT5 style) improves net or drawdown.
3. **Microstructure calibration:** the |ln p_t − ln p_{t−h}| distribution (h = decision-to-fill latency, §3.12) and post-shock refill rates feed the planner's β buffers and the "tranche only with chain-buy refill" rule (§3.12).

### 2.4 (d) New-subnet launches. Verdict: **FILTER-ONLY (Gatekeeper)**; LCW sub-sleeve **EXPERIMENTAL, paper-only**

**Gatekeeper** (`features/gatekeeper.py`, overlay-owned, always on, no capital).
- **Generation registry** detected from storage diffs:
  - LastRateLimitedBlock(0x02) advancing marks the registration block Q;
  - a NetworksAdded / NetworkRegisteredAt change of the victim netuid marks the same-block removal;
  - NetworkRegisteredAt reappearing 17–25 blocks later marks NetworkAdded.
- **Seed assertions at Added:** SubnetTAO = lock; SubnetAlphaIn = lock/median price; quote weight 0.5 (else SEED_ANOMALY); netuid = victim (lowest free, skipping DissolveCleanupQueue; assertion only).
- **Recorder:** start_call is detected by FirstEmissionBlockNumber None → Some, which switches the recorder to dense mode (every block for 600 blocks, every 10 blocks up to 7,200).
- **States:** QUEUED, WAIT_START, WATCH (≤ 14 d after start), MATURE.
- **Mechanical flags kept** (vetoes are applied by the overlay policy table in §3.4; each flag must survive the FT7 leave-one-flag-out test):

| Flag | Rule | Use |
|---|---|---|
| UNSTARTED | FirstEmissionBlockNumber None | veto all |
| SEED_ANOMALY | seed assertions failed | veto all |
| EMA_WARMING | since_start < 72,000 (10 d) | veto all except LCW |
| BURNING | MinerBurned > 0.60 | veto all |
| EMISSION_OFF | SubnetEmissionEnabled false | veto all except LCW-paper |
| YOUNG_IMMUNE | age_reg < NetworkImmunityPeriod | informational (immunity calendar, hazard) |
| REG_CLOCK_HOT | P(reg within 24 h) > 0.25 | tightens prune-guard thresholds |

- **Dropped** until the consolidated sell-load model passes T3: `DILUTED`, `svs_struct`. `GATE_STARVED` (b/θ < 0.4) is monitoring-only.
- **Corrections applied:** chain_buy ≈ 0 for rp ≳ 0.655 at spot ≈ EMA (computed only via the shared split function). The registration clock is the overlay's hazard API, not a separate implementation.

**LCW: launch carry window** (`strategies/launch_lcw.py`; flag `lcw.enabled = false`; may enter PAPER only after offline FT1 **and** FT2 pass; never promoted on fewer than 20 paper trades).
- **Universe:**
  - WATCH generation with 720 ≤ since_start ≤ 72,000 and age_reg ≤ 100 d;
  - 600 ≤ T ≤ 6,000;
  - spot/spot0 in [0.8, 1.6];
  - MinerBurned (2-epoch mean) ≤ 0.35;
  - ≥ 8 miner UIDs from ≥ 5 coldkeys, top-1 coldkey share ≤ 0.5, ≥ 2 permit coldkeys (from `SubnetState.metagraph: MetagraphLite`, §5.3, read via `SubnetInfoRuntimeApi_get_selective_metagraph` or storage **(VERIFY)** only when `lcw.enabled`);
  - owner: autolock on, OR owner_sold_7d ≤ 50% of accrual and owner TAO out over 24 h ≤ 0.5% of T;
  - router hotkey with ≥ 2 epochs of positive realised Δln I and marginal net yield ≥ 0.6%/day;
  - φ_ewma ≥ +0.4% of T per day and φ_6h ≥ 0;
  - μ_hat ≥ 0.8%/day and G − 2f ≥ 3%;
  - ≤ 2 launch positions; LCW budget ≤ 5% of NAV (0 until FT1/FT2 pass).
- **Yield forecast:**
  - ŷ(τ) = AE·(1−c_o)·0.5·(1 − rp_τ)·(1−take)·(1−ck)/(A₀ + G_mech·τ + κ_e·max(φ_ewma, 0)·T·τ/p + a_me), averaged over τ = 1 … 10 d;
  - G_mech = deterministic A_earn growth (≈2,950 alpha/day while Σ EMA > 1);
  - if total miner incentive is 0 in an epoch, validators also receive the miner half (an upside case, flagged).
- **Score:** μ_hat = 2·λ·φ_ewma + ŷ_avg − headwind − RT/h, with λ = 0.5 and h = 10 d. Headwind is 2·p·ΔS_alpha/T from the shared sell-load model.
- **Exits:**
  - X1 emission disabled after enabled (URGENT, overlay);
  - X2 owner dump: ≥ 3× accrual or ≥ 1% of T in 24 h, lock or owner change (HIGH);
  - X3 MinerBurned > 0.60 for 2 epochs or < 4 miners (try a same-subnet move first if the hotkey failed);
  - X4 stop −8% from cost / trailing 12% on executable value (HIGH);
  - X5 φ_6h ≤ −1% of T per day, or φ_ewma < 0 for 2 epochs after day 5;
  - X6 realised yield < 0.25%/day while μ_hat < 0;
  - X7 hold ≥ 14 d, or age_reg ≥ NetworkImmunityPeriod − 144,000 blocks (20 d before expiry; read live).
- **Limits** are post-fill marginal prices from the simulator (§3.12), never spot × 1.02 or spot × 0.985.
- **Sizing:** min(T·(G − 2f)/4, V_max at s = 1.5%, 1% of T, 0.5 × estimated daily organic buying, 2.5% NAV). Two tranches 360 blocks apart.
- **Removed from the build:** L2 start_call snipe live shadow, Mode I ignition, and the right-tail scorecard. FT5 remains an offline replay **only** if the dense recorder makes it free. FT8 is not built: 2 winners make it untestable.

**Launch tests:**
| Test | PASS | KILL |
|---|---|---|
| FT0 integrity | Queued→Added lag 17–25 on the last 10; seed reconstruction to 1e-6/1e-4; EMA forecast vs recursion 1e-4; emission pipeline vs chain 1e-6 TAO/block; zero generation splices; era-B fills vs sim_swap ≤ 0.01% | fix before anything |
| FT1 total-return restatement | d14 median P×I return ≥ +3% at 1.5× costs (0.5% of T), ≥ 55% positive, d30 median ≥ −5% | d14 median < +1% or carry < 40% of it |
| FT2 early carry exists | median realised best-hotkey net yield d1–14 ≥ 0.8%/day; ≥ 60% of launches ≥ 0.5%/day; realised/ex-ante within 1.5× | median < 0.5%/day |
| FT3 flow persistence (only post-8,466,531 launches; earlier ones use stake events or reserve differences) | corr ≥ 0.2, 90% lower bound > 0 | λ = 0 (then LCW almost surely dies) |
| FT4 rule back-test | median ≥ +1.0% at 1.5× costs, hit ≥ 55%, worst decile ≥ −15%, positive in ≥ 2 of 3 strata, beats entry-timing placebo by ≥ 1 pp (p < 0.10); fewer than 8 qualifying trades = NOT TESTABLE | — |
| FT6 SVS validity | Spearman IC ≥ 0.15 | SVS stays descriptive |
| FT7 Gatekeeper utility | each flag lowers drawdown ≥ 20% or raises mean ≥ +0.05%/day (t ≥ 2) in ≥ 2 of 3 regimes in leave-one-flag-out | drop the flag |
| FT10 economics | median expected profit ≥ 0.05 TAO/trade and ≥ 20× fixed fees | paper only |
| Kill switches K1–K6 | as designed: K1 ≥ 6 trades with median < 0 and cumulative < 0; K2 any loss > 25%; K3 owner-dump exit slippage > 5%; K4 freeze-list change; K5 realised yield < 50% of ex-ante for 3 trades; K6 2 shield misses or head lag > 2 | disable LCW |

### 2.5 Baselines and placebos (run as books in every backtest pass)
- `cash`: hold TAO.
- `ew_price`: equal-weight overlay-eligible universe, daily rebalance, price only.
- `ew_total`: the same with the P×I router-hotkey index.
- `yield_x_size`: double sort on closed-form net yield tercile × pool-size tercile, holding the high-yield/large-pool cell. This is the benchmark carry must beat.
- `prune_blind`: ew_total with the overlay's prune rules disabled. It prices the prune engine.
- `random_entry`: placebo with turnover, holding time and sizing matched to the sleeve under test. It is seeded from (seed, block_hash).

Each baseline also runs under both impact bounds and three dereg payout variants (formula, 0.35×, 0.65× spot).

---

## 3. Risk overlay and allocator (OVERLAY-A1). Verdict: **CORE**

The overlay produces no alpha. It is the final authority on the physical book. Defaults are the `RiskCfg` values in §5.

### 3.1 Authority and ownership
1. **The overlay alone owns** every forced exit on the whole physical position. It also owns:
   - prune policy, emission-off policy, burn caps, and the launch-age / young-subnet policy;
   - owner events;
   - the YieldRouter (one hotkey per coldkey and subnet);
   - the single market-regime throttle, kill switches and modes.
2. **Sleeves may only be stricter.** A sleeve may add entry filters and may set its own sleeve share to 0 (EXIT signal, at most HIGH urgency). It can never raise a target past an overlay cap.
3. **Monotone review.** `RiskOverlay.review` may lower targets, add `ForcedExit`s, veto entries or raise the mode. `TargetBook.reduced` raises if anything increases.
4. **One model, one place.** Gate haircuts (m_gate), total-return-drift haircuts (m_trd, TRD_BAN) and the regime throttle are computed from the shared emission and sell-load modules and journaled as `RiskAction(action="MONITOR")`. They become active only after FT4 / FT-R1 pass, and then only for sleeves with `declares_dilution = False`.

### 3.2 Universe floor (applies to every sleeve; values read live)
| Section | Rule (enter unless noted) |
|---|---|
| A eligibility (hold) | netuid ≠ 0; in NetworksAdded; generation key equals the position's key (otherwise DISSOLVED); FirstEmissionBlockNumber set (except LCW); NetworkRegistrationAllowed (false freezes the EMA and breaks the prune model) |
| A eligibility (enter) | SubtokenEnabled (buys need it; sells do not) |
| B liquidity | SubnetTAO ≥ 200 TAO; SwapBalancer quote ∈ [0.30, 0.70] (outside that band, quotes come from runtime sim_swap only); FeeRate ≤ 330; escrow E/x ≤ 0.50 |
| C emission | SubnetEmissionEnabled = true (LCW-paper exception); see the ban rules in §3.4 |
| D prune | §3.3 entry rules |
| E launch age | since_start ≥ 100,800 blocks (14 d) AND age_reg ≥ 216,000 blocks (30 d), except LCW; Gatekeeper vetoes in §2.4 |
| F burn | MinerBurned ≤ 0.50 |
| G validator | at least one `RouterCandidate` passes the book-independent filters (§3.8); the book's router (WP8) must also return a hotkey after its per-book Q_MAX check, otherwise yield is zero and there is no entry |
| H cooldowns (per book) | abnormal-fill 7,200 blocks; owner event 7,200 blocks; ≥ 3 failed orders on the netuid within 600 blocks (per-block outcomes only; stride-replay fills are excluded, §3.12) → 7,200 blocks; stale data |
| I aggregates (per book) | §3.5 bucket caps |

**Ownership.** Sections A–G depend only on chain state and are computed once per snapshot by WP5 (`features/universe.py`). Sections H and I depend on the book's own orders, fills and holdings, so WP8 evaluates them per book from `TickContext.book_view` (§5.10): H in `risk/overlay.py`, I in `risk/liquidity.py`. Every snapshot the feature engine publishes `FeatureFrame.universe_eligible` (sections A–G), and reports add per-book and per-sleeve counts.

### 3.3 Prune engine
**Ladder and target.** C(t) = {n ∈ NetworksAdded, n ≠ 0, t ≥ NetworkRegisteredAt[n] + NetworkImmunityPeriod}.
- target = argmin over C of (SubnetMovingPrice, NetworkRegisteredAt). This matched 52 of 52 prunes.
- prune_rank = 1-based position in that order.
- The runtime `get_subnet_to_prune()` is cross-checked every 25 blocks in paper/live. On a mismatch the runtime value wins, a data alarm is raised, and entries halt until explained.
- `prune_possible = n_nonroot + len(DissolveCleanupQueue) ≥ SubnetLimit`. If false, hazard = 0.

**EMA kinematics.** a_k = SubnetMovingAlpha·b/(b + EMAPriceHalvingBlocks), with b = block − (FirstEmissionBlockNumber − 1); a = 0 if the EMA is frozen.
- Stressed time-to-target: **t\*_k = ln((E_k − s)/(E_1 − s)) / −ln(1 − a_k)** with s = (1 − D_STRESS)·spot_k (default spot·0.5).
- Other subnets are projected at current spot, and subnets whose immunity ends inside the horizon are admitted at their expiry block. The 10-block grid search in `prune.py` handles a moving bottom.
- The closed form above is used when E_1 is flat. t\* = None (never crosses) if s ≥ E_1.

**Unwind time** U = L_exec·(1 + retries) = 5·3 = **15 blocks** for a single-shot exit, measured from the finalized decision block b.
- L_exec = finality_lag + shield latency = 3 + 2 = 5 blocks per attempt. Attempt 1 is submitted at the best head (≈ b + finality_lag) and lands at N+2. Its outcome (fill, failure or miss) is final once N+2 is finalized (§9.6), and the retry is submitted at that tick on another free delegate, so each attempt costs one L_exec.
- `RiskCfg.unwind_exec_blocks` must equal `ExecCfg.finality_lag_blocks + ExecCfg.latency_blocks`; config validation enforces it. If the measured finality lag (§13 Q8) differs, U and the table below are regenerated.

**Tier A (EMERGENCY, deterministic).** Fires if prune_possible AND block + U + M_A ≥ W_open (window open or opening within the margin) AND (k == target OR t\*_k ≤ U + M_A). It also fires when a held immune subnet's expiry falls inside U + M_A and it would be the target.
- Execution: one single-shot `remove_stake_limit(full, allow_partial=True)` (live: the exact alpha read at the submit head, §9.4), limit = spot·(1 − s_emerg)^(1/w_quote), with **s_emerg = clamp(0.5·(1 − R_k), 0.05, 0.25)**. Re-quote at each terminal outcome (fill, failure or miss), at most once per L_exec blocks, until flat; each retry rotates to a free delegate. Never tranche: our own tranches lower our EMA.

Corrected coverage (a = 0.000286; EMA gap to the bottom that Tier A still clears; regenerated for U = 15):
| M_A (blocks) | U + M_A | gap covered at 50% spot crash | gap covered at spot → 0 |
|---|---|---|---|
| 150 | 165 | 2.4% | 4.8% |
| **300 (default)** | 315 | **4.5%** | **9.4%** |
| 600 | 615 | 8.8% | 19.2% |
| 1,200 | 1,215 | 17.2% | 41.6% |

Tier A is the last-ditch layer. Most protection comes from the entry rules, the backstop and (phase 2) Tier B. M_A is calibrated by FT1b on per-block replays of the last 10 prunes, range 60–1,200.

**Backstop (URGENT, model-free).** prune_possible AND non-immune prune_rank ≤ K_BOTTOM (3) AND r(now) = cost/L ≤ R_BACKSTOP (1.2). r ≤ 1.2 ⇔ Δ ≥ 46,080 blocks since the last registration. Exit single-shot at S_URGENT = 3% average slippage.

**Never hold the target.** k == target with prune_possible gives an EMERGENCY exit regardless of window state.

**Registration hazard** (`protocol.prune.HazardModel`; indexed by the cost ratio r = c/L so it survives I_eff and halving changes).
- Empirical CDF, n = 32 registrations (Feb–Oct 2026):

| r | 1.75 (window opens) | 1.462 | 1.253 | 1.132 | 1.045 | 0.958 | 0.872 | 0.767 | 0.266 |
|---|---|---|---|---|---|---|---|---|---|
| F | 0.0625 (P_OPEN) | 0.09 | 0.19 | 0.31 | 0.50 | 0.72 | 0.84 | 0.94 | 1.0 |

- Smoothing: F = (32·F_emp + N0·F_prior)/(32 + N0), N0 = 4, F_prior = 1 − exp(−(Δ − 14,400)/43,200). Interpolation is piecewise-linear in r.
- Tail floor: once r ≤ 1.045 the per-block hazard is ≥ ln2/7,200 (≥ 50%/day).
- Hot market: if the last 2 registrations paid r ≥ 1.4, P_OPEN = 0.5.
- p_reg(t₀, t₁) = 1 − S(t₁)/S(t₀). It is 0 inside NetworkRateLimit and when not prune_possible.
- Reference P(reg within 24 h): 8.3% at opening, 7.3% at Δ 31k, 15.7% at 43k, 39.6% at 50k, 51% at 55k, 55% at 65k.
- **Invalidation path:** if spec-475 "PoW registration" (or any later change) means registrants no longer wait for the lock-cost decline, set `hazard_valid = False`. Fall back to the window rule plus a constant hazard ln2/7,200 per block after window opening, and refit only after 8 new registrations.
- The table is refit weekly and after every registration. It is a calibration, not a P&L parameter.
- **As-of rule.** The table above is fit on Feb–Oct 2026 and is used as-is only in paper and live. Backtests get the hazard, κ_p, R and the Tier B jump parameters from the `CalibrationProvider` (§5.12), which fits each of them only on events with block < decision block (§8.10).

**Tier B (URGENT, phase 2; `tier_b_enabled = False` until FT1 and FT2 pass).** prune_possible AND block + H_B (7,200) ≥ W_open AND P_prune_24h(k)·(1 − R_k) ≥ PI_B (0.75% of position).
- P_prune comes from a Monte Carlo with N_PATHS = 2,000. Per path, every 60 blocks:
  - ln s_j += σ_j·√(60/7200)·(√ρ·Z_c + √(1−ρ)·Z_j) + J_j;
  - σ_j = max(6%/day, realised 3-day);
  - ρ = 0.3;
  - jumps ln(1 − 0.5) with probability 0.03/day·60/7200;
  - EMA update per the recursion;
  - candidates admitted at expiry;
  - a registration is drawn with p_reg(step); the target is removed and the window closes for NetworkRateLimit.
- **Seeded:** `numpy.random.Generator(PCG64(int.from_bytes(blake2b(f"{run_seed}|{block_hash}".encode(), digest_size=8).digest(), "little")))`. P_prune is logged with every decision.
- Jump parameters must reproduce, within ±5 pp, that 7/52 victims were outside the bottom 3 two days before the prune, 12/52 had been rank 1 for < 24 h, and 2/52 were outside the bottom 7 within 1 day. In backtests they are refit as-of (only prunes before the decision block; the prior above until 10 prunes exist).

**Recovery ratio** (dissolution payout per alpha / spot).
- T_after = T·(x/(x+E))^(w_b/w_q) (baskets are sold first).
- denom = (S − E) + P + (x + E if reg_at > TaoInRefundDeploymentBlock (8,334,450) else 0).
- S = TotalAlphaStaked (fallback AlphaOut − ProtocolAlpha before spec 448); P = SubnetProtocolAlpha.
- R = clamp((T_after/denom)/spot, 0, 1). SN92 golden ≈ 0.368 (0.41 before the basket sale).
- Fallback R_DEFAULT = 0.35. If FT10 fails, R = min(formula, 0.35). In backtests the FT10 decision is made as-of, on dissolutions before the decision block only (`Calibration.r_default`).

**Entry rules (floor).**
- Non-immune: prune_rank ≥ 6 AND EMA ≥ 1.5 × target EMA, and, once the MC is enabled, P_prune_7d·(1 − R) ≤ 1%.
- Immune with expiry within 7 d: projected rank at expiry ≥ 6.
- No increase while the ladder bucket (prune_rank ≤ 15) is ≥ 15% of NAV_liq.
- No entry inside the Tier A or backstop zone.
- REG_CLOCK_HOT raises the floor to rank ≥ 8 and EMA ≥ 1.7×.

**SafeMode end.** At `SafeMode.EnteredUntil + 1`: reconcile, re-run Tier A and the backstop on post-SafeMode state, and submit those exits first. A registrant can prune in that same block.

**Stale prune inputs** (> 25 blocks old): mode CAUTION. Query `get_subnet_to_prune` on a second endpoint. Tier A and target exits remain allowed using runtime-API state.

### 3.4 Emission, burn and launch-age policy (single source for all sleeves)
| Event / state | Action |
|---|---|
| `EMISSION_TOGGLED` → false while held | URGENT single-shot exit at S_URGENT (no tranching: a disabled subnet has no chain-buy refill). Entry ban until max(disable + 100,800 blocks, re-enable + 360 blocks). Prune-hazard flag set (leading indicator: 10 June switch-offs were pruned 1–15 weeks later). FT3 may downgrade this to "freeze entries + cap ×0.25" if holding wins |
| ≥ 3 disables in one block (wave) | entries halted for 7,200 blocks book-wide; ladder and MC refreshed |
| `EMISSION_TOGGLED` → true | no chasing; the ban rule above applies; sleeves are notified (wake event) |
| MinerBurned ≥ 0.90 for 2 consecutive epochs while held | NORMAL exit (zero-emission bucket, root-purge candidate) |
| Launch-age floor | since_start ≥ 14 d AND age_reg ≥ 30 d for every sleeve except LCW. Stricter sleeve floors: carry since_start ≥ 45 d; momentum age_reg ≥ 30 d and (≥ 90 d or EMA rank ≤ 40) |
| LCW-only positions | NORMAL exit if not emission-enabled within 21 d of start_call; hard stop at age_reg ≥ NetworkImmunityPeriod − 144,000 |
| `NetworkRegistrationAllowed` false | EMA frozen (a = 0); entries vetoed; holdings re-checked by the prune engine |

### 3.5 Liquidity, size and aggregate caps
- **Per subnet:**
  - T_st = min(T_now, min T over 3 d)·(1 − D_T), D_T = 0.20;
  - **V_cap = min(T_st·s/(1−s)·m_esc·[m_gate·m_trd·m_owner if active], NU_MAX·NAV_liq)**, s = S_EXIT_ENTRY = 1.5% (V_cap = 1.52% of T_st);
  - m_esc = clamp(1 − E/x, 0.5, 1) is active;
  - m_gate (1 / 0.5 / 0.25 at keep ≥ 0.5 / ≥ 0.1 / below) and m_trd (0.5 if TRD < −0.25%/day) are MONITOR-only;
  - m_owner = 0.5 when `Feat.owner_liquid_frac` ≥ 10% (`RiskCfg.owner_liquid_max_ppm`) is MONITOR-only (`owner_haircut_active = False`). It becomes active only after an FT5-style overlay-on/off ablation shows it improves net or drawdown (brief risk #7: size down where the owner cut is liquid);
  - for |w_q − 0.5| > 0.01, solve shortfall(V) = s by bisection on `quote_sell`.
- **Impact-optimal:** when a sleeve supplies α_h, the target is ≤ V\* = T·(α_h − 2f)/4 (λ-shrunk by the sleeve).
- **Hold budget:** ES_n = 1 − liq_value/(alpha·spot). If ES > S_EXIT_HOLD_MAX (2.5%), trim to V_cap at T_now. Σ ES·V ≤ 2% of NAV_liq; trim the largest ES first.
- **Portfolio limits:**
  - gross alpha ≤ G_MAX_EFF·NAV_liq, with G_MAX = 80% (≥ 20% TAO cash);
  - per subnet ≤ 15%;
  - ladder bucket ≤ 15%;
  - owner-coldkey cluster ≤ 20%;
  - young (since_start < 30 d) ≤ 10%;
  - LCW ≤ 5%;
  - **N_eff = min(12, floor(G_MAX_EFF·NAV_liq/(4·V_MIN)))**, so each position can be trimmed in quarters.
- **Minimum order and fees:** V_MIN = 0.5 TAO (buy-fee drag 0.21%). Sleeves set stricter minimums (carry 1, momentum 1.5). Chain floors: gross ≥ 0.002 TAO + fee; post-fee ≥ 0.002; partial-sell output ≥ 0.002.

### 3.6 Owner guard
Owner position A_t = value of AlphaV2/Alpha(owner_hotkey, owner_coldkey, netuid) at each FULL snapshot **(VERIFY item names and hashers)**. The owner hotkey is always tracked (§5.12 `track_hotkeys`), so I(owner_hotkey) is available.
- sold = max(0, A₀·I₁/I₀ + accrual − A₁), with
  - accrual = c_o·Σ alpha_out_emission over the interval (0 when OwnerCutEnabled is false) + Σ over the drains in the interval of last_dividend·t/(1 − t), where t = take_u16/65535 of the owner hotkey and the term applies only at drains where it earns;
  - **no (1 − AL) factor.** An auto-locked owner cut is a conviction lock on stake that stays in the owner's position, so the expected growth includes the full cut whatever AL is. (1 − AL) appears only inside `protocol.sellload`, where it means the liquid fraction that can be sold;
  - the take-credit term assumes the take is credited as new shares to the owner hotkey's owning coldkey and that this coldkey is the subnet owner coldkey **(VERIFY the take-credit recipient)**; if it is not, the term is 0.
- Moves to other coldkeys count as sales (conservative).
- Triggers:
  - sold over 7,200 blocks ≥ 2% of SubnetTAO (TAO-equivalent);
  - SubnetOwner or SubnetOwnerHotkey change;
  - autolock true → false.
- Each trigger sets a 7,200-block entry cooldown and halves V_cap for 1 d.
- **Standing exposure features** (brief risk #7), computed every FULL snapshot by WP5:
  - `Feat.owner_liquid_frac` = owner_alpha·(1 − autolock)/x (autolock as 0/1, x = SubnetAlphaIn);
  - `Feat.top_holder_frac` = (owner_alpha + Σ TotalHotkeyAlpha of the top-5 tracked hotkeys)/(AlphaOut − ProtocolAlpha);
  - both are reported per held name; `owner_liquid_frac` feeds the MONITOR haircut m_owner in §3.5.
- Lock perpetual→decaying events need System.Events. Detection is assigned to WP4 `events_decoder.py` and listed in the §13 backlog; until it ships, no rule depends on it.
- Carry's stricter owner exit is in §2.1.

### 3.7 Gatekeeper flags
Consumed exactly as in the §2.4 table. Every sleeve truncates lookbacks at NetworkRegisteredAt. For EMA_WARMING names, EMA-derived ranks and shares are replaced by the flat-spot EMA forecast.

### 3.8 YieldRouter: validator selection (one hotkey per coldkey and subnet)
The router is split by what it depends on. **WP5** (`features/yield_router.py`) publishes book-independent candidate data once per snapshot as `Feat.router_candidates` (§5.8). **WP8** (`risk/router.py`) makes the per-book choice from those candidates, the book's own holdings and the router memory `RouterState` in `TickContext.book_view` (§5.10). It sets `TargetPosition.hotkey`.
- **Candidates (WP5):** tracked hotkeys of the subnet. Tracking (§5.12 `track_hotkeys`) covers:
  - top 5 by TotalHotkeyAlpha among keys of AlphaDividendsPerSubnet(n, ·), re-listed daily via `state_getKeysPaged`;
  - every take-0 earner;
  - every hotkey held or chosen by any book, and the owner hotkey;
  - tracking is sticky: a tracked (hotkey, generation) stays tracked until the generation ends.
- **Book-independent filters (WP5; result in `RouterCandidate.eligible`):**
  - member in ≥ 90% of the last 20 epochs and in the last 2;
  - take = Delegates/65535 ≤ TAKE_MAX (5%);
  - no take increase in the last 216,000 blocks, from Delegates diffs; unknown before tracking start → allowed but flagged;
  - ChildkeyTake ≤ TAKE_MAX;
  - rank by TotalHotkeyAlpha among dividend recipients ≤ 0.8·MaxAllowedValidators[n] (`SubnetState.max_allowed_validators`; permit risk);
  - realised/closed-form net yield ratio ∈ [0.65, 1.35].
- **Score (WP5):** EWMA_{half-life 20 epochs}(Δln I per epoch)·(7,200/Tempo). Candidates are published best first; ties go to lower take, then larger TotalHotkeyAlpha, then lexicographic hotkey. `Feat.best_candidate` is the first eligible one.
- **Per-book filter (WP8):** our alpha after the planned trade / TotalHotkeyAlpha(h, n) ≤ Q_MAX (5%), because bond-EMA lag dilutes large own shares. Books of different sizes can therefore choose different hotkeys on the same subnet.
- **Switch (WP8)** via same-subnet `MOVE_STAKE` (whole position on the origin hotkey: sim moves all shares, live passes the exact alpha read at the submit head, §9.4; no swap fee, ≈0.001 TAO) if any of:
  - the current hotkey fails filters for 2 epochs;
  - take jumps > 5 pp;
  - the best beats the current by > max(0.02%/day, 10% relative) for 2 epochs AND the 30-day expected gain > 3× the move fee.
  The epoch counters live in `RouterState` (`fail_epochs`, `beat_epochs`), which the Engine journals in `DecisionTrace.memories` under the pseudo-id `risk.router`; the reducer folds it back into `book_view.router`.
- **Spec-475 Null consensus** (0 of 128 subnets as of block 9,240,814) is monitored per subnet through `SubnetState.consensus_mode` (storage item name **VERIFY** from spec-475 metadata, §6.5). A change is a per-subnet PARAM_CHANGED (§4.3) that forces re-validation of the closed form (T6) for that subnet and marks its candidates `ratio_ok = False` until T6 passes again.

### 3.9 Dust and remainder rules
- Full exits are exempt from the partial-sell minimum.
- A partial sell must output ≥ 0.002 TAO.
- If a sell would leave a remainder below REMAINDER_MIN = max(0.05 TAO-equivalent, V_MIN), it becomes a full exit (`full_position=True`).
- Never leave a nominator position below NominatorMinRequiredStake (read live; 0.02 TAO). The chain force-sells it with no price limit.
- Never use `UnstakeAll` / `UnstakeAllAlpha` / `'all'` for buys. Live never uses `'all'` or u64::MAX at all, because `bt.Policy` treats them as unbounded spend (§9.4).
- **Live full exits and moves** pass the exact alpha that `SdkPort.post_state` reads at the submit head. An epoch drain between that read and inclusion leaves a remainder equal to the drain's yield on the position. Fills are built from share deltas (§9.6), so the ledger keeps the remainder as an open position and no reconciliation mismatch arises. A remainder below NominatorMinRequiredStake is force-sold by the chain, and reconciliation books that as a fill. A larger remainder still has target 0, so the planner exits it as a new full exit (attempt + 1, same urgency).

### 3.10 Allocator (`portfolio/allocator.py`)
1. **Budgets by evidence stage, never by backtest P&L:**
   - B_s = G_MAX_EFF·NAV_liq·budget_ppm_s·m_stage·m_burnin·m_dd;
   - m_stage: RESEARCH and SHADOW = 0 in paper/live books (backtest books use 1); PAPER = 1; LIVE_ELIGIBLE = 1 only if listed in `[live].sleeves`;
   - PAPER sleeves get equal fixed budgets, B_MAX = 40% each, LCW ≤ 5%;
   - inverse-vol budgets only after ≥ 60 paper days AND FT8, then shrunk 50% toward fixed.
2. **Post-spec burn-in:** m_burnin = 0.5 for 100,800 blocks after a spec change, but only when one of these holds:
   - `protocol.regimes.touches_econ(spec)` is True. The flag is set per spec in `protocol/regimes.py` by the lead through an ADR, after reading the release diff for emission, dividend or registration code (spec 475 is True);
   - within 100,800 blocks after the spec change, the post-spec V4 emission parity, T6 yield parity or FT1 hazard-validity check moves outside tolerance. These are journaled as `ModelDriftObserved(probe="emission_parity" | "yield_parity" | "hazard_validity")`.

   Otherwise there is no burn-in. The parity checks (T2a, T6, FT1 hazard validity) are re-run after every spec change regardless. Because specs land about every 3 days, a blanket rule would halve budgets permanently.
3. **Sleeve targets:** value_{s,k} = min(B_s·weight_ppm, max_size_rao, V\*(α_h) if α_h > 0).
   - EXIT signals zero the sleeve share.
   - AVOID forbids sleeve increases.
   - Signals expire after horizon_blocks.
4. **Aggregate per subnet (sum-then-cap):** total_k = Σ_s value_{s,k}; cap_k is the §3.5 V_cap, computed before the allocator runs by the `CapsFn` in `risk/liquidity.py` (§5.10) and passed in as `caps`.
   - When over the cap: existing sleeve holdings keep priority (pro rata to current shares), then higher stage, then pro rata.
5. **Portfolio constraints:** N_eff (drop the lowest Σ edge_ppm_day·value), bucket caps, gross G_MAX_EFF.
6. **Netting:** sleeves trade with each other at the decision spot inside the virtual ledger, and only the net hits the pool.
   - The allocator returns each internal trade in `TargetBook.transfers` (§5.5). The Engine journals one `SleeveTransfer` per (key, from, to) in EMIT (§5.6).
   - `reduce` moves `SleeveHolding.shares` from one sleeve to the other and moves `sleeve_cash` the opposite way at the decision spot. It posts no ledger entries, because the transfer is virtual and the physical position and cash are unchanged. Invariant 3 (sleeve shares sum to `Position.shares`; sleeve cash sums to `cash`) therefore holds by construction.
   - Reports always also compute **un-netted stand-alone sleeve P&L** from the journal (fills plus transfers), and kill switches use the un-netted figure (`SleeveStats`, §5.10).
7. **Market-regime throttle (the ONE throttle; MONITOR by default):** every 7,200 blocks compute α₀ = EWMA(half-life 7 d) of the cross-sectional median residual daily P×I return of the eligible universe (return − modelled cb_push + sell_push). The candidate multiplier is m_regime = clip(1 + α₀/0.5%, 0, 1). It becomes active (multiplying G_MAX_EFF) only if FT-R1 passes: OOS net improves ≥ +0.03%/day, or max drawdown falls ≥ 25%, without turning off > 40% of days.
8. **Output:** a TargetBook with the book's router hotkey per subnet (from `book_view.router` after the router step, §4.4), ppm attribution and the netting transfers.

### 3.11 Modes, kill switches and drawdown governors
| Trigger (journaled input or derived) | Mode / action |
|---|---|
| spec_version change | CAUTION in the same block; run validation suite V1–V7 (§9.5). Pass → NORMAL (live also needs the user to re-arm). Fail → EXITS_ONLY via runtime-API state |
| transaction_version change | live FROZEN; paper CAUTION |
| SafeMode.EnteredUntil ≥ block | FROZEN (no staking calls are whitelisted); precompute post-SafeMode exits |
| secs_since_block > 36 / > 120 | CAUTION / EXITS_ONLY |
| finality lag > 30 blocks; healthy endpoints < 2; prune inputs stale > 25 blocks | CAUTION |
| no healthy head endpoint | FROZEN |
| EmissionView.model_ok false (`protocol.emission.parity_ok`); sim_swap drift > 5 bp | CAUTION |
| orphan facts > 0 (unknown-order events) | entries halted until QuarantineCleared |
| live: arm token expired, or spec_version ∉ `accepted_specs` | LiveVenue UNARMED: submits nothing, except EMERGENCY/URGENT full sells when `risk_exits_when_unarmed` is set and V2, V3, V6 pass (§9.3). Paper and backtest are unaffected |
| key alarm (unexplained stake delta, delegate nonce jump, proxy set or announcement change, coldkey-swap scheduled, foreign stake events, TransactionFeePaidWithAlpha) | FROZEN; alert "revoke the Staking proxy from the coldkey". Exception (`allow_emergency_exits_when_frozen`): Tier A/target exits with fill-or-kill limits at s ≤ 5% |
| fee float (live): < `fee_float_alert_rao` / < `fee_float_caution_rao` / < `fee_float_exits_rao` (0.25 / 0.15 / 0.05 TAO) | alert / CAUTION / EXITS_ONLY |
| failures: ≥ 3 on one netuid in 600 blocks / ≥ 5 book-wide (per-block outcomes only; stride-replay fills excluded, §3.12) | 7,200-block subnet cooldown / 300-block CAUTION |
| DD30 ≥ DD_SOFT / DD_HARD (NAV_liq) | G_MAX_EFF ×0.5 / ×0.25 more; unwind the excess over ≤ 3 d in NORMAL urgency, largest ES first |
| daily NAV_liq loss ≥ DAILY_LOSS | CAUTION for 7,200 blocks |
| operator command (control file → OperatorCommand) | halt / resume / exits_only / flatten:<netuid> |

- **Calibration:** DD_SOFT (default 15%), DD_HARD (25%) and DAILY_LOSS (8%) are placeholders. Before a book enters PAPER they are replaced by block-bootstrap values (7-day blocks) on its backtest NAV_liq series, chosen so that **false CAUTION occurs on ≤ 5% of days**.
- **Inputs.** DD30 and the daily loss come from `book_view.nav_liq_daily`; the sleeve rules below read `book_view.sleeve_stats` (§5.10). Both are maintained by the reducer from journaled `DecisionTrace.nav_liq` / `sleeve_nav`, fills and `SleeveTransfer`s, so replays reproduce them.
- **Sleeve kill (daily, on un-netted P&L):**
  - REDUCED (budget ×0.5): sleeve DD ≥ 8%, OR 20-trade cost ratio (realised/modelled) > 1.5, OR turnover > 2× backtest;
  - SUSPENDED (budget 0, positions unwound over ≤ 3 d): sleeve DD ≥ 15%, OR 45-day mean below the 5th percentile of its backtest bootstrap, OR cost ratio > 2.5, OR a pre-registered falsification breach;
  - re-promotion: SUSPENDED → REDUCED after ≥ 30 paper days with mean > 0 and DD < 5%; REDUCED → ACTIVE after 30 d with DD < 4%;
  - FT8 calibrates false-kill ≤ 5% and true-kill ≥ 80% within 45 d.

### 3.12 Limit-price planner (`portfolio/planner.py`)
- **Inputs.** Besides the `RiskDecision`, the planner reads only `TickContext` (§5.10). Book history comes from `ctx.book_view`: open and recent orders with their attempt numbers, chase episodes, free and locked delegates, recent own fills and per-netuid failure counts.
- **Priority:** EMERGENCY > URGENT > HIGH > NORMAL sells > NORMAL buys. Within a tier, order by expected-loss rate P·(1−R)·V/blocks_to_deadline, then key.
- **Concurrency:**
  - one in-flight order per netuid;
  - one in-flight shielded carrier per delegate (carrier nonce n, inner n+1);
  - N_DELEGATES = 3 in live and in sim/paper (`ExecCfg.n_delegates` must equal the live count, so backtests model the same unwind time U as live);
  - after a shield miss, the delegate is locked until its era end + 2 blocks (`book_view.delegate_locked_until`); a retry goes to another free delegate.
- **Buy** (ADD_STAKE_LIMIT, allow_partial = False):
  - Δ = target − current executable value, rounded to the no-trade band max(V_MIN, 20%·target);
  - require benefit α_h·Δ ≥ Δ·(Δ/T + f) + buy fee;
  - **tao_in = min(Δ, cash − MIN_FREE_REAL)**, with MIN_FREE_REAL = `RiskCfg.min_free_real_rao`;
  - **limit = ceil(marginal_after_buy(pool, tao_in)·(1 + β_entry))**, asserted > spot. Then assert tao_in ≤ max_buy_to_limit(pool, limit), which holds by construction because the limit lies above the post-fill marginal price of tao_in. There is no circularity: tao_in is fixed first and the limit is derived from it;
  - β_entry = clamp(q95 of |ln p_t − ln p_{t−h}|, 0.1%, 2%) over the last 1,800 blocks (or the last 30 stride points), excluding blocks with own fills. **h is the venue's effective decision-to-fill latency:** h = finality_lag + latency_blocks (5) when snapshots are per-block (paper, live, refined windows), and h = stride in stride replays. The FeatureEngine computes it (§5.8), with own-fill blocks supplied by the Runner (§5.10). LiveVenue may only tighten the limit, so β must already cover the full horizon;
  - pre-trade: `sim_swap_tao_for_alpha` at the decision block. Abort if alpha == 0 or |local/chain − 1| > 1e-5.
- **Normal sell** (REMOVE_STAKE_LIMIT, allow_partial = False):
  - apply the dust rule;
  - **limit = floor(marginal_after_sell(pool, alpha)·(1 − β_exit))**, asserted < spot;
  - β_exit = clamp(q99 of the same |ln p_t − ln p_{t−h}| sample, 0.25%, 5%).
  - Full exits use `full_position = True`. Sim sells the whole holding; live sends RemoveStakeLimit with the exact alpha read at the submit head, never `'all'` (§3.9, §9.4). On SlippageTooHigh, the fallback is allow_partial with the same limit.
- **Urgent/emergency sell:**
  - allow_partial = True;
  - limit = floor(spot·(1 − s)^(1/w_quote)), s = S_URGENT (3%) or s_emerg;
  - re-quote at each terminal outcome (fill remainder, failure or miss), at most once per L_exec blocks, widening s ×1.5 up to s_emerg; each retry rotates to a free delegate. One order per netuid stays in flight at any time;
  - risk exits are never tranched; NORMAL exits tranche only where chain-buy refill exists. CPMM path independence makes refill-free tranching strictly worse;
  - for chain-buy-refill tranching: V_tr = T·1%/(1−1%), spacing clamp(ceil(V_tr/SubnetExcessTao), 10, 600) blocks.
- **Hotkey switch:** MOVE_STAKE of the whole position on the origin hotkey (`full_position = True`; live passes the exact alpha, §9.4), no limit, NORMAL priority, never in the same tick as another order on that netuid. The destination is `TargetPosition.hotkey` from the book's router.
- **Chase rules:**
  - entries re-quote at most K_CHASE = 2 times while cumulative drift from the decision spot < CHI_MAX = 1.5%, then abandon. The episode state (re-quotes so far, decision spot) is `book_view.chase`;
  - a re-decision after a terminal failure gets attempt + 1 and a new order id.
- **Drain timing:** a NORMAL sell is deferred if 0 < next_drain − (b + fill_latency) ≤ 30 blocks. Unstaking in block B keeps B's credit.
- **Shield fallback:** if finality lag > 5 blocks (the shield era is stale) and urgency ≥ URGENT, submit unshielded with era 16 and the same limit. MEV risk is accepted as smaller than prune risk.
- **Validity (TTL):**
  - shielded intents: `valid_until` = the planned inclusion block b + finality_lag + latency. At submit the venue fixes the only legal inclusion block, `VenueAck.expected_fill_block` = submit head + 2. A shielded fill at any other block is MISSED in paper and in refined replays; only stride replays may fill at the next available snapshot (§8.4);
  - unshielded era-16 intents: `valid_until` = b + finality_lag + 16 (the era end; `SubmitStarted.era_end` is authoritative);
  - the nonce lock uses the separate `SubmitStarted.era_end` (finalized anchor + 8, or + 16 unshielded, plus 2 blocks of margin; §9.6).
- **Limit-failure accounting:** PriceLimitExceeded and SlippageTooHigh rates are reported separately for per-block fills and for stride fills. Stride fills (`exact_block = False`) do not count toward the 3-per-600-block cooldown or the book-wide failure burst.
- **Long-only:** the planner refuses any order that would create a negative position. ShortsEnabled is monitored only.

---

## 4. Architecture

### 4.1 Choice: lean core plus robust money-safety grafts
**From the lean proposal (kept):**
- State is a `ChainSnapshot`. Chain events are a pure diff of consecutive snapshots: no event bus and no System.Events decoder in v1.
- Features are computed once per snapshot and shared by all books. They hold only book-independent data; anything that depends on one book's holdings, orders or memory (the per-book router choice, cooldowns, aggregates, chase and failure counters) comes from that book's `BookView` (§5.10).
- Four substitution seams: DataSource, Strategy, RiskOverlay, ExecutionVenue.
- N books in one pass (sleeves × baselines × impact and dereg variants).
- Strategies are stateless scorers emitting signals.
- Parquet for market data with in-memory DuckDB views; stdlib SQLite for run state.
- Small dependency set; one asyncio process. bittensor is imported by exactly one package (`taotrader.live`).

**Grafted from the robust proposal (where money is at stake):**
1. **Deterministic replay through the same engine code path.**
   - `Engine.decide` is pure; `reduce(state, event)` is the only state transition.
   - Recovery re-runs `decide` on journaled inputs and asserts identical outputs (`ReplayDivergence` otherwise).
   - Backtest, paper, live-dry and live differ only in the adapters handed to the Runner.
2. **Idempotent orders.**
   - `order_id = blake2b(run_id|book|block|netuid|reg_at|hotkey|kind|attempt)`; UNIQUE journal idempotency keys.
   - Write-ahead bracket: INTENDED → SubmitStarted → venue call → VenueAck/OrderFailed.
   - The `UNKNOWN` state is resolved from chain truth and never blind-resent.
3. **Crash recovery.** An input and every decision derived from it commit in ONE SQLite transaction (WAL, synchronous=FULL). Side effects happen strictly after the commit. Recovery = verify the hash chain, then fold the journal, re-drive the outbox, resolve unknowns and reconcile.
4. **Append-only ledger.** The journal is hash-chained, with UPDATE/DELETE blocked by triggers. A double-entry ledger (per-unit postings sum to zero) is kept independently of the typed portfolio, and the two are cross-checked after every commit.
5. **Exact money arithmetic.** Integer rao everywhere. Fractional powers use `decimal` with a fixed context (identical on Windows and Linux). Floats are confined to features and cross into decisions only via `to_ppm`.

**Rejected from both:** microservices, a message bus, an ORM, a plugin framework, ML stacks, a metadata-driven SCALE codec on the hot path, an intra-block simulator.

### 4.2 Package layout (`src/taotrader/`; WP = owning work package, section 11)
```
tao-subnet-trader/
  pyproject.toml  uv.lock  .importlinter  mypy.ini  ruff.toml                                 WP0
  config/default.toml  config/preregistration.toml                                           WP0
  config/books.backtest.toml                                                                 WP10
  config/books.paper.toml                                                                    WP12
  config/live.example.toml  requirements-live.txt                                            WP11
  scripts/run.cmd  scripts/register-tasks.ps1  scripts/taotrader-live.service               WP12
  tools/capture_golden.py      one-off golden fixture capture (plain httpx JSON-RPC)        WP0
  docs/DESIGN.md  docs/adr/                                                                  lead
  docs/runbooks/{crash-recovery,spec-change,prune-emergency,key-compromise,provider-outage,safe-mode,live-arming}.md   WP12
  src/taotrader/
    __init__.py  __main__.py                     version; main() -> cli.main                 WP0
    core/                                        PURE kernel: stdlib only                     WP0
      units.py fixed.py state.py orders.py signals.py events.py portfolio.py views.py
      config.py protocols.py codec.py errors.py
    protocol/                                    PURE chain-rule replicas (golden-tested)     WP2
      amm.py        era-correct swap math, limits, V_max, V*, round-trip bounds
      emission.py   block emission curve, root prop, get_shares replica, injection/chain-buy split
      ema.py        EMA alpha/step/forecast, t*
      prune.py      ladder, target, prune_possible, registration cost, hazard model + as-of fit, recovery ratio
      yield_model.py closed-form yield, A_earn, deterministic A_earn growth
      sellload.py   the ONE structural sell-load model
      calibration.py Calibration bundle + CalibrationProvider protocol (as-of calibrated inputs)
      regimes.py    the ONE regime table (setCode+1), fee defaults by spec, per-spec touches_econ
      fees.py       tx-fee table, min stake, dust thresholds
      derive.py     derive_events(prev, cur), tracked-hotkey selection
    chain/                                       I/O: custom JSON-RPC reader (Windows-native)  WP1
      hashing.py    twox128 / twox64concat / blake2_128concat / key builders
      scale.py      decoders (u*, i*, I96F32, U96F32, U64F64, Perquintill, SafeFloat, Option, compact, AccountId)
      items.py      spec-aware storage item registry (one row = key + decoder + default + plan)
      rpc.py        HTTP + WS JSON-RPC, token bucket, backoff, endpoint pool, circuit breakers
      runtime_api.py state_call encoders/decoders
      metadata.py   per-spec ValueQuery defaults (offline extraction; cached JSON)
      reader.py     ChainReader: snapshot(), hotkey panel, owner positions, escrow, cross-checks
      head.py       LiveChainFeed DataSource: finalized-head follower, gap fill, stall detector
    data/
      schema.py     DuckDB/SQLite DDL + Parquet column specs                               WP3
      lake.py       Parquet chunk writer/reader, manifest, DuckDB views                       WP3
      store.py      SnapshotStore (lake + hot staging), lookahead guard                        WP3
      replay.py     ParquetReplay DataSource (era-correct pools, warm-up)                      WP3
      recorder.py   live recorder: hot staging (fsync per block) -> hourly Parquet compaction  WP3
      journal.py    SqliteJournal (append-only, hash-chained, atomic batches) + projections    WP3
      collector.py  resumable archive collector (60/300-block, hotkey panel, calibration probes) WP4
      refine.py     per-block windows (prune bisect, event windows), spec-boundary search      WP4
      calibration.py LakeCalibrationProvider (as-of fits from registration/generation tables) + FrozenCalibration (paper/live)  WP4
      events_decoder.py  OPTIONAL System.Events decoder (scalecodec extra)                     WP4
      taostats.py   optional REST enrichment / cross-checks (never in the decision path)      WP4
    features/                                                                                 WP5
      engine.py     FeatureEngine: rolling generation-keyed buffers -> FeatureFrame
      micro.py      median-of-3 price, local fast EMA, beta buffers (latency horizon h, own-fill exclusion), flow z-scores
      yield_router.py  YieldRouter candidate panel: book-independent scores, filters, best_candidate
      gatekeeper.py launch registry, states, mechanical flags
      universe.py   overlay floor sections A-G (book-independent) + published counts
    venues/                                                                                   WP6
      sim.py        SimVenue: the ONE shared execution simulator (latency, impact bounds, chain rules, failures)
      paper.py      PaperVenue: SimVenue on the live feed + sim_swap drift probe
    engine/                                                                                   WP7
      engine.py     Engine.decide (pure pipeline)
      reducer.py    EngineState, reduce(), ledger postings, invariant checks
      runner.py     Runner: tick, commit, drain, outbox, recover (the only side-effect loop)
      recovery.py   replay verification, checkpoints, outbox re-drive, UNKNOWN resolution
      control.py    control-file watcher -> OperatorCommand inputs
    risk/                                                                                     WP8
      overlay.py (incl. section H cooldowns)  modes.py  prune_guard.py  hazard_mc.py  emission_guard.py
      liquidity.py (CapsFn, section I aggregates)  owner_guard.py  regime_throttle.py
      router.py     per-book YieldRouter choice: Q_MAX vs own shares, hysteresis, MOVE_STAKE decision
    portfolio/                                                                                WP8
      allocator.py  planner.py
    strategies/                                                                               WP9
      base.py  carry.py  momentum.py  launch_lcw.py  baselines.py
    backtest/                                                                                 WP10
      books.py  runner.py  metrics.py  stats.py  studies.py  bias.py
    reports/                                                                                  WP10
      html.py  tables.py
    live/                                        Linux/WSL only; the ONLY package importing bittensor  WP11
      gate.py  preflight.py  sdk_port.py  venue.py  reconcile.py  nonce.py
    ops/
      config_load.py  secrets.py                 TOML -> core.config; keyring/env secrets     WP0
      logging.py  alerts.py  health.py  lock.py                                               WP12
    cli.py                                                                                    WP12
  tests/
    core/ (WP0)  protocol/ (WP2)  chain/ (WP1)  data/ (WP3: schema, lake, store, replay, recorder, journal; WP4: collector, refine, calibration)
    features/ (WP5)  venues/ (WP6)  engine/ (WP7)  risk/ + portfolio/ (WP8)  strategies/ (WP9)
    backtest/ + integration/ (WP10)  live/ (WP11)  ops/ (WP0: config_load, secrets; WP12: the rest)
    fixtures/golden/*.json   raw storage bytes and runtime-API results captured at fixed blocks   WP0
    fixtures/cassettes/*.jsonl  recorded JSON-RPC sessions for offline reader tests            WP1
```
**Import contracts** (import-linter, CI):
- `core` imports only the stdlib.
- `protocol` imports only `core`.
- `features`, `risk`, `portfolio`, `strategies` and `engine` import only `core`, `protocol` and each other as listed in their WP dependencies.
- `chain`, `data`, `venues`, `backtest`, `reports`, `ops`, `cli` may import inward.
- `ops.config_load` and `ops.secrets` (WP0) are leaf modules (they import only `core` and the stdlib/keyring). Every shell package (`chain`, `data`, `venues`, `backtest`, `live`, `cli`) may import them; no other `ops` module may be imported outside `ops` and `cli`.
- `live` is imported **only** by `cli` behind the gate.
- Pure packages (`core`, `protocol`, `features`, `risk`, `portfolio`, `strategies`, `engine.engine`, `engine.reducer`) may not import `time`, `datetime`, `random`, `os`, `uuid`, `asyncio`, `httpx`, `websockets`, or iterate an unsorted `set` (AST lint).
- No module outside `live/` references `bittensor`, `UnstakeAll`, `TransferStake` or `Batch`, and no SDK `execute(` call (sqlite3 `Connection.execute` is allowed; ADR-0001 #5).

### 4.3 Event model
**Chain events** (`core.events.ChainEvent`): produced only by `protocol.derive.derive_events(prev, cur)`, sorted by (kind, key).
| Kind | Detection rule (prev → cur) |
|---|---|
| REGISTERED / DEREGISTERED | generation key present only in cur / only in prev, where presence means NetworksAdded == true (§5.3 `ChainSnapshot.subnets`). DEREGISTERED therefore fires in the removal block P, even though NetworkRegisteredAt and the pool storage are cleared only during cleanup 17–25 blocks later. A prune followed by re-registration of the same netuid yields DEREGISTERED(old) + REGISTERED(new), never a price jump |
| START_CALLED | FirstEmissionBlockNumber None → Some |
| EMISSION_TOGGLED / REG_ALLOWED_TOGGLED / AUTOLOCK_TOGGLED | flag differs (flag = new value) |
| EPOCH_DRAIN | LastEpochBlock differs |
| LARGE_FLOW | both SubnetTaoFlow values valid and same generation, and \|Δ\| ≥ 2% of SubnetTAO |
| OWNER_POSITION_CHANGED | owner sold estimate (§3.6) ≥ 0.25% of pool alpha |
| OWNER_CHANGED | SubnetOwner or SubnetOwnerHotkey differs |
| TAKE_CHANGED / DIVIDEND_MEMBERSHIP | a tracked hotkey's Delegates take or `earns` differs |
| REGISTRATION_SEEN | LastRateLimitedBlock(0x02) increased |
| REG_WINDOW_OPENED | prev.block < last_reg + NetworkRateLimit ≤ cur.block |
| IMMUNITY_EXPIRED | reg_at + NetworkImmunityPeriod ∈ (prev.block, cur.block] |
| PRUNE_TARGET_CHANGED / GATE_BAR_UPDATED | ladder()[0] / EmissionGateBar differs |
| SPEC_CHANGED | spec_version or transaction_version differs |
| PARAM_CHANGED | a freeze-list value differs: SubnetMovingAlpha, EmissionBarRank, EmissionGateExponent, TaoWeight, SubnetLimit, NetworkImmunityPeriod, NetworkRateLimit, NetworkLockReductionInterval, NetworkMinLockCost, SubnetOwnerCut, ShortsEnabled; per subnet FeeRate, EMAPriceHalvingBlocks, Tempo, consensus mode (`SubnetState.consensus_mode`, Null consensus) |
| SAFE_MODE | SafeMode active state flips |

Events on FULL-plan-only fields fire at FULL snapshots (every 60 blocks live). Hot-path fields fire per finalized block.

**Journal events** (`core.events`; closed vocabulary; `KIND` and `VERSION`; upcasters for old versions):
| Kind | Emitted by | Idempotency key | Effect in `reduce` |
|---|---|---|---|
| snapshot_observed | Runner (input) | snap:{hash} | clock, health, last block |
| chain_event | Engine (derived) | — | prev-state bookkeeping, bans, cooldowns; a DEREGISTERED held key marks the position DISSOLVING |
| operator_command / capital_changed / config_applied / model_drift_observed | Runner (inputs) | op:/capital:/— | halt flags, cash, config hash, drift alarms, post-spec burn-in triggers |
| yield_accrued | Engine (ACCOUNT) | — | ledger pos += Δ, income:yield −= Δ |
| dereg_settled | Engine (ACCOUNT; backtest, paper, live-dry) or Runner (live reconcile only, `model="observed"`) | dereg:{book}:{netuid}:{reg_at} | DISSOLVING position closed; cash += payout. At most one per held generation |
| decision_trace | Engine (DECIDE) | — | strategy memories and the `risk.router` memory, standing signals, recent forced exits, NAV_liq and sleeve NAV samples |
| mode_changed | Engine | — | mode |
| sleeve_transfer | Engine (EMIT; allocator netting) | xfer:{book}:{block}:{netuid}:{reg_at}:{from}:{to} | sleeve shares from → to, sleeve cash to → from; no ledger postings |
| order_intended / order_cancelled | Engine (EMIT) | intent:/cancel: | outbox record INTENDED / CANCELLED |
| submit_started | Runner outbox (before I/O; carries delegate, reserved nonce, era_end) | submit: | SUBMITTING |
| venue_ack / submit_unknown | Runner (after I/O; `submit_unknown` also from recovery for every SUBMITTING order, and on a live nonce mismatch) | ack:/— | SUBMITTED / UNKNOWN |
| fill_reported / order_failed | Runner (venue drain or resolve) | fill:/fail: | FILLED/FAILED/EXPIRED, ledger, portfolio |
| carrier_fee_settled | Runner (live venue drain or resolve, after the era end of a miss whose carrier was absent from N+2) | carrier:{order_id}:{attempt} | fee_float −= fee, fees:tx += fee (fee 0 if the carrier was never included); allowed on an EXPIRED order |
| recon_adjusted / quarantine_cleared | Runner (live reconcile) | — | chain truth wins; halt/unhalt entries |

### 4.4 Event flow (identical in backtest, paper, live-dry, live)
```
DataSource.stream(after=last_journaled_block) -> SourceItem(snapshot, health)       [one item per processed block]
Runner.tick(item):
  store.clock = snap.block
  ev   = derive_events(prev, snap)                                                  # pure
  own  = union of fill blocks of every book in the last 1,800 blocks              # from book states (journal-derived)
  frame = features.update(snap, ev, own_fill_blocks=own)                            # pure, ONCE per snapshot, shared
  batch = [SnapshotObserved(..., health)] + [ChainEventObserved(e) for e in ev]
  for book in books:                                                                # N books share the pass
      view = book.venue.mark_to(snap)                                               # sim/paper: + own footprint; live: identity
      batch += book.engine.decide(book.state, snap, prev, ev, frame, view, health)  # PURE (phases below)
  journal.append_batch(batch)                     # === THE COMMIT POINT (atomic + fsync) ===
  fold batch into every book.state; book.venue.observe(e) for each e               # memory follows the journal
  for book in books:
      while (e := await book.venue.advance(book.venue.mark_to(snap))) is not None:  # fills due NOW, one at a time
          commit([e]); fold; observe                                                # each fill is its own batch
      await flush_outbox(book, snap)
        # INTENDED   -> (delegate, nonce, era_end) = venue.reserve() -> commit SubmitStarted -> venue.submit
        #               -> commit VenueAck | OrderFailed | SubmitUnknown (raise or live nonce mismatch)
        # SUBMITTING -> commit SubmitUnknown(detail="recovered_submitting")   (only after a crash; FSM: -> UNKNOWN)
        # UNKNOWN    -> venue.resolve() -> commit its events (never re-send blindly)
  prev = snap
```
**Engine.decide phases** (pure; `Phase` gives the LogicalTime ordering):
1. **INGEST:** apply inputs (operator commands, health).
2. **ACCOUNT:**
   - `YieldAccrued` per held position (value change of our shares from the index change since the last tick);
   - `DeregSettled` for DEREGISTERED held keys, using the last good snapshot (refined removal−1 when present) and the book's dereg model. **Backtest, paper and live-dry only.** In `RunMode.LIVE` the Engine emits nothing here: the reducer has already marked the position DISSOLVING from the DEREGISTERED chain event, and only reconciliation emits the observed `DeregSettled(model="observed")` (§9.7). Its idempotency key `dereg:{book}:{netuid}:{reg_at}` makes a second settlement impossible.
3. **DECIDE** (fixed order; every step reads the book's `TickContext`, including `book_view`, §5.10):
   1. mode update (§3.11) → `ModeChanged`;
   2. strategies that are due, in fixed order. Due means: `block // decide_every > last_call // decide_every` (aligned to absolute blocks, so stride 60 and stride 1 agree) OR a wake event; AND `block ≥ valid_from_block`; AND `frame.warm`; AND the cadence contract holds. Each returns `StrategyOutput(signals, memory)`;
   3. standing signals expire after their horizon;
   4. **router** (`risk/router.py`, `RouterFn`) → new `RouterState`; the Engine replaces `ctx.book_view.router` with it (`dataclasses.replace`) for every later step;
   5. **caps** (`risk/liquidity.py`, `CapsFn`) → per-subnet V_cap;
   6. **allocator**(signals, ctx, caps) → TargetBook (hotkeys from the router, netting transfers);
   7. `overlay.review` → RiskDecision (forced exits are evaluated **every** snapshot regardless of strategy cadence);
   8. **planner**(decision, ctx, inflight, ...) → OrderIntents, if the mode allows (FROZEN allows only the configured emergency exception);
   9. cancellations of INTENDED orders made invalid by the new decision.
4. **EMIT:** `DecisionTrace` (signals, actions, mode, strategy memories plus the `risk.router` memory, features digest, calibration digest, NAV_liq and sleeve NAV samples) + one `SleeveTransfer` per netting transfer + `OrderIntended`*.

**Cadence:**
- Backtest stride 60 (every snapshot FULL), with optional per-block refinement windows.
- Paper/live: every finalized block is a tick (HEAD plan, ~1,600 keys). A FULL plan runs at block % 60 == 0.
- Risk tripwires run every tick. Carry and momentum decide every 300 blocks. LCW decides at epochs. The router step runs every tick but re-evaluates a subnet only at its EPOCH_DRAIN (candidate scores change only there) or when the book's holdings change.
- **Data-cadence contract:** the Runner refuses a strategy whose `min_cadence_blocks` < `DataSource.cadence_blocks`. Reports state that prune-exit timing inside a 60-block snapshot is approximated; FT1b uses per-block windows.

**What differs by mode:**
| Mode | DataSource | ExecutionVenue | Journal backend | Health input |
|---|---|---|---|---|
| backtest | ParquetReplay (lake, era-correct, 30-day warm-up) | SimVenue (one per book; impact/dereg variants) | SQLite `:memory:` or file, synchronous=OFF | `HealthObs.nominal()` |
| paper | LiveChainFeed (finalized heads) + recorder | PaperVenue | SQLite file, WAL, synchronous=FULL | measured |
| live_dry | LiveChainFeed | LiveVenue(plan-only) | SQLite file | measured |
| live | LiveChainFeed | LiveVenue(submit), gated (§9) | SQLite on local ext4 | measured |

### 4.5 Crash windows and recovery
| Crash point | Journal holds | Recovery |
|---|---|---|
| before append_batch / inside the transaction | nothing (rolled back) | the source re-delivers the block; decide recomputes identical outputs |
| after commit, before fold | full batch | `recover()` re-folds; the outbox is re-driven |
| after SubmitStarted | order SUBMITTING | `recover()` first journals `SubmitUnknown(detail="recovered_submitting")` (SUBMITTING → UNKNOWN), then calls `venue.resolve`. The FSM never allows SUBMITTING → FILLED/EXPIRED, so a resolved fill or miss lands on UNKNOWN and is never an orphan. Sim/paper: resolve re-derives the deterministic ack (PLACED + `VenueAck`, UNKNOWN → SUBMITTED), so fills match a crash-free run. Live: resolve uses the reserved delegate nonce, the carrier at N+2 and the inner extrinsic by hash (§9.6) |
| after venue.submit, before VenueAck | SUBMITTING | same; live finds the carrier by delegate nonce and inner hash |
| after VenueAck | SUBMITTED | `venue.observe` rebuilds pending fills |
| mid venue drain | some fills committed | re-run the drain at the last snapshot before streaming resumes |

`recover()`:
1. `journal.verify_chain()`; the head hash is checked against `journal.anchor`.
2. Load the latest checkpoint (optional) and verify its state hash.
3. Re-ingest the lake window needed to warm the FeatureEngine. It must reproduce the journaled `features_digest`.
4. For each later batch: load the snapshot by digest from the store, re-run `decide` in VERIFY mode and compare canonical outputs (`ReplayDivergence` on mismatch), then fold.
5. Journal `SubmitUnknown(detail="recovered_submitting")` for every order still in SUBMITTING (one batch), then re-drive the outbox, resolve every UNKNOWN order, and reconcile (live).
6. Resume the stream strictly after the last journaled block.

Resuming with changed code or config requires `--accept-drift`. That writes a `ConfigApplied` event and disables VERIFY for earlier batches. `reduce` is total on valid journals: facts about unknown or closed orders increment `orphans` (which halts entries) and never raise. The one expected fact on a closed order, `CarrierFeeSettled` on an EXPIRED order, is not an orphan. Strict ordering puts the snapshot batch for block b before fills due at b, so yield for b is accrued before a fill changes the share count.

### 4.6 Determinism rules
- No wall clock, randomness, environment or I/O in pure packages. Health observations are journaled inputs.
- Stochastic pieces (MC, bootstrap, placebo, shield-miss injection) are seeded from `(run seed, block_hash, order_id)`.
- Money paths are integer or `Decimal(DEC)`. `Decimal.normalize()` is banned (lint). Codec Decimals are exact text.
- Iteration order is always sorted (keys, hotkeys, strategies by id).
- The features digest, the calibration digest, strategy memories and the router memory are journaled each decision.
- Cross-OS: money paths and journal bytes must match bit-for-bit on Windows and Linux (CI). Float features are checked to tolerance. A cross-OS replay runs in tolerant mode for decisions and strict mode for money.

### 4.7 Process model
- **One asyncio process per mode instance.** Tasks:
  - feed: finalized-head follower → snapshot reads → recorder hot staging (fsync before the journal may reference the snapshot);
  - engine loop: the sole journal writer; commits run on one dedicated worker thread so fsync never blocks the feed;
  - venue I/O tasks: they only enqueue events;
  - reconciler (live);
  - heartbeat/alerts;
  - control-file watcher.
- The feed queue is bounded at 64. On overflow, intermediate blocks are skipped and the next item carries `feed_gap_blocks` > 0. Skipped blocks coarsen events but cannot hide them, because diffs span the gap.
- A single-instance lock file per (mode, run_id) (msvcrt on Windows, fcntl on Linux).
- Backtests and grids run in separate processes (`ProcessPoolExecutor`, ≤ 8 workers).

---

## 5. Core types and protocols (real code; WP0 commits these verbatim)

These modules were compiled and smoke-tested with Python 3.11 when this document was written: imports, the FSM, monotone `TargetBook.reduced`, idempotency keys, the `to_ppm` bridge and `ChainSnapshot.get` generation matching. WP0 commits them and adds the missing implementations: `codec.encode/decode/canonical_bytes` and `portfolio.check_invariants`, plus the remaining ledger helpers (`yield_txn`, `dereg_txn`, `fail_txn`, `capital_txn`, `carrier_fee_txn`). `SleeveTransfer` has no ledger helper because it posts nothing. The review revision of this section (BookView, RouterState, RouterCandidate, MetagraphLite, SleeveXfer/SleeveTransfer, CarrierFeeSettled, `ExecutionVenue.reserve`, the as-of `Calibration` API) was compiled and smoke-tested the same way with Python 3.11: imports, the FSM including the illegal SUBMITTING → FILLED/EXPIRED, the new idempotency keys, BookView and RouterState construction, and the config cross-field values (U = 15).

Every other WP codes only against these types and the function signatures in §5.12. Any change to this section after WP0 lands needs an ADR and a version bump of the affected journal `KIND`.

**Units:**
- on-chain amounts are `int` in rao (`Rao` for TAO, `AlphaRao` per generation);
- prices are `PriceRao` (rao of TAO per whole alpha, the chain's `limit_price` unit);
- fractions are `Ppm`; rates are `PpmPerDay`;
- shares and decoded fixed-point values are exact `Decimal`;
- time is `Block`.

### 5.1 `taotrader/core/units.py`: units, identities, logical time, mode and stage enums
```python
"""taotrader/core/units.py - units, identities, logical time, mode enums.

Stdlib only. No I/O, no clock, no randomness. Every on-chain amount is an integer
in its smallest unit; the NewType names carry the unit so mypy --strict catches mix-ups.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Final, NewType

NetUid = NewType("NetUid", int)          # u16; 0 = root
Block = NewType("Block", int)            # block number
BlockHash = NewType("BlockHash", str)    # "0x" + 64 lowercase hex
Rao = NewType("Rao", int)                # TAO amount in rao (1 TAO = 1e9 rao)
AlphaRao = NewType("AlphaRao", int)      # alpha amount of ONE subnet generation, 1e-9 alpha units
PriceRao = NewType("PriceRao", int)      # rao of TAO per 1 whole alpha (= TAO/alpha * 1e9): the chain's limit_price unit
Ppm = NewType("Ppm", int)                # parts per million (fractions, weights, slippage budgets)
PpmPerDay = NewType("PpmPerDay", int)    # rate: ppm per 7,200 blocks (0.10 %/day = 1_000)
Hotkey = NewType("Hotkey", str)          # "0x" + 64 hex public key; ss58 only inside taotrader.live
Coldkey = NewType("Coldkey", str)        # same encoding as Hotkey
StrategyId = NewType("StrategyId", str)  # "carry", "momentum", "lcw", "baseline.ew_total", ...
BookId = NewType("BookId", str)          # one simulated/paper/live portfolio inside a run
OrderId = NewType("OrderId", str)        # deterministic blake2b-96 hex, never random

RAO_PER_TAO: Final[int] = 10**9
PPM: Final[int] = 1_000_000
BLOCKS_PER_DAY: Final[int] = 7_200
BLOCK_SECONDS: Final[int] = 12
FEE_DEN: Final[int] = 65_535             # FeeRate, Delegates take, ChildkeyTake, SubnetOwnerCut are /65535
PERQUINTILL: Final[int] = 10**18         # Swap.SwapBalancer.quote raw scale
HALF_E18: Final[int] = 5 * 10**17
U64_MAX: Final[int] = 2**64 - 1          # chain "whole position" sentinel (spec >= 469); NEVER sent live (Policy: unbounded)
DEFAULT_TAKE_U16: Final[int] = 11_796    # 18%: take of any hotkey that never set Delegates
MIN_STAKE_RAO: Final[int] = 2_000_000    # DefaultMinStake (0.002 TAO)


@dataclass(frozen=True, slots=True, order=True)
class SubnetKey:
    """Asset identity = one GENERATION of a netuid. netuids are reused about weekly.

    Never key anything (features, positions, history, P&L) by netuid alone.
    """
    netuid: NetUid
    reg_at: Block          # SubtensorModule.NetworkRegisteredAt[netuid] at observation time


@dataclass(frozen=True, slots=True, order=True)
class PositionKey:
    subnet: SubnetKey
    hotkey: Hotkey


class Phase(IntEnum):
    """Intra-block ordering, identical in every mode.

    The snapshot batch (INGEST..EMIT) commits first; fills due at this block (VENUE)
    commit after it; outbox submissions (OUTBOX) last.
    """
    INGEST = 0
    ACCOUNT = 1
    DECIDE = 2
    EMIT = 3
    VENUE = 4
    OUTBOX = 5


@dataclass(frozen=True, slots=True, order=True)
class LogicalTime:
    """Chain time only. No wall clock ever enters a decision."""
    block: Block
    phase: Phase = Phase.INGEST
    sub: int = 0


class Mode(IntEnum):
    """Overlay operating mode (risk/modes.py). Higher = more restrictive."""
    NORMAL = 0       # everything allowed
    CAUTION = 1      # no increases; exits and trims allowed
    EXITS_ONLY = 2   # only forced risk exits (via runtime-API state if local models are unvalidated)
    FROZEN = 3       # nothing is submitted (SafeMode, key alarm, no healthy head endpoint)


class Stage(IntEnum):
    """Evidence stage of a sleeve. Budgets are funded by stage, never by backtest P&L."""
    RESEARCH = 0       # backtest/offline only
    SHADOW = 1         # runs live on paper feed, signals journaled, zero budget
    PAPER = 2          # paper budget
    LIVE_ELIGIBLE = 3  # passed every gate; the USER decides whether to list it in [live].sleeves


class RunMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE_DRY = "live_dry"   # real reads, real plan(), never submits
    LIVE = "live"           # gated; user-run on Linux/WSL only
```

### 5.2 `taotrader/core/fixed.py`: exact arithmetic helpers
```python
"""taotrader/core/fixed.py - exact arithmetic for the money path.

Decimal with fixed contexts is bit-identical on Windows and Linux (libm pow is not),
so journals replay identically across OSes. Floats are allowed only in feature math
and must cross into the decision path through to_ppm().
"""
from __future__ import annotations

import math
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal
from typing import Final

from .units import PPM, Ppm

DEC: Final[Context] = Context(prec=60, rounding=ROUND_FLOOR)   # AMM fractional powers, spot, limits
EXACT: Final[Context] = Context(prec=160)                      # fixed-point decoding: 2**-64 terminates in 64 digits

ONE: Final[Decimal] = Decimal(1)


def floor_int(d: Decimal) -> int:
    return int(d.to_integral_value(rounding=ROUND_FLOOR))


def mul_ppm(x: int, p: int) -> int:
    """x * p / 1e6, floored. x is any integer amount, p a Ppm."""
    return x * p // PPM


def frac_ppm(num: int, den: int) -> Ppm:
    return Ppm(0 if den == 0 else num * PPM // den)


def to_ppm(f: float) -> Ppm:
    """The ONLY float -> int bridge on the decision path (used at the Signal boundary).

    Round half-even on the exact decimal repr; raises on NaN/inf so a bad feature fails loudly.
    """
    if not math.isfinite(f):
        raise ValueError(f"non-finite value {f!r}")
    return Ppm(int((Decimal(repr(f)) * PPM).to_integral_value(rounding=ROUND_HALF_EVEN)))
```

### 5.3 `taotrader/core/state.py`: decoded chain state
```python
"""taotrader/core/state.py - decoded chain state. Immutable; built only by chain.reader / data.replay."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import IntEnum, IntFlag

from .fixed import DEC, floor_int
from .units import (
    PERQUINTILL, RAO_PER_TAO, AlphaRao, Block, BlockHash, Coldkey, Hotkey, NetUid, PriceRao, Rao, SubnetKey,
)


class PoolKind(IntEnum):
    CP_REAL = 1        # Era A (blocks < per-subnet v3 init): constant product on (SubnetTAO, SubnetAlphaIn), w = 0.5
    CP_V3_VIRTUAL = 2  # Era B (5,947,549 .. 8,486,593): constant product on virtual reserves (L*sqrtP, L/sqrtP)
    BALANCER = 3       # Era C (>= 8,486,594): weighted pool, weights from Swap.SwapBalancer


class Quality(IntFlag):
    OK = 0
    TA_PRICE = 1               # price from T/A in era B (biased in v3-era micro caps; mask in factor work)
    SEED_FALLBACK = 2          # Balancer seeding fell back to q = 0.5 at 8,486,594: check for a price jump
    EARLY_TINY_POOL = 4        # first weeks of dTAO, pools < 10 TAO
    CHAIN_STALL_GAP = 8        # 2025-05-20 freeze window
    DEFAULT_FILLED = 16        # an absent ValueQuery key was filled from a spec whose defaults are not validated
    REFINED = 32               # snapshot fetched by the per-block refinement pass (e.g. removal-1)
    NOT_STARTED = 64           # FirstEmissionBlockNumber is None
    CARRIED = 128              # non-hot-path fields carried forward from the last FULL read (live HEAD plan)
    NO_YIELD_IDX = 256         # no tracked earning hotkey: nominator yield unknown
    BALANCER_MIGRATION = 512   # within 60 blocks of 8,486,594


class ReadPlan(IntEnum):
    HEAD = 1   # per-block hot-path keys (pool, EMA, flags, flow, epoch); rest carried (Quality.CARRIED)
    FULL = 2   # every subnet item + globals; every 60 blocks live, every backtest snapshot


@dataclass(frozen=True, slots=True)
class PoolState:
    kind: PoolKind
    tao: Rao                 # real SubnetTAO (sizing caps, depth, dissolution pot)
    alpha: AlphaRao          # real SubnetAlphaIn
    px_tao: int              # pricing reserve y used by swap math (== tao except CP_V3_VIRTUAL)
    px_alpha: int            # pricing reserve x
    w_quote_e18: int         # Perquintill raw TAO weight (Swap.SwapBalancer.quote); 5*10**17 for CP kinds
    fee_rate: int            # Swap.FeeRate (/65535); absent -> spec default (33; 196 for specs 290-292)

    @property
    def w_base_e18(self) -> int:
        return PERQUINTILL - self.w_quote_e18

    def spot(self) -> Decimal:
        """TAO per alpha = (w_base / w_quote) * y / x. Never hard-code 0.5."""
        return DEC.divide(DEC.multiply(Decimal(self.w_base_e18), Decimal(self.px_tao)),
                          DEC.multiply(Decimal(self.w_quote_e18), Decimal(self.px_alpha)))

    def spot_rao(self) -> PriceRao:
        return PriceRao(floor_int(DEC.multiply(self.spot(), Decimal(RAO_PER_TAO))))

    def shifted(self, d_tao: int, d_alpha: int) -> PoolState:
        """Pool after a reserve change (own-impact overlay, post-trade pool). Weights unchanged (no injection)."""
        return replace(self, tao=Rao(self.tao + d_tao), alpha=AlphaRao(self.alpha + d_alpha),
                       px_tao=self.px_tao + d_tao, px_alpha=self.px_alpha + d_alpha)


@dataclass(frozen=True, slots=True)
class HotkeyIdx:
    """Hotkey share pool on one subnet generation. Position value = shares * index; yield raises the index only."""
    hotkey: Hotkey
    total_alpha: AlphaRao            # TotalHotkeyAlpha(h, n)
    total_shares: Decimal            # TotalHotkeyShares V1 (U64F64) if present at this block, else V2 SafeFloat m*10^e; exact
    take_u16: int = 11_796           # Delegates[h]; absent -> 18%
    childkey_take_u16: int = 0       # ChildkeyTake(h, n)
    earns: bool = False              # h is a key of AlphaDividendsPerSubnet(n, .) at this block
    last_dividend: AlphaRao = AlphaRao(0)   # AlphaDividendsPerSubnet(n, h): post-take nominator alpha of last epoch

    def index(self) -> Decimal:
        if self.total_shares == 0:
            return Decimal(1)
        return DEC.divide(Decimal(self.total_alpha), self.total_shares)

    def value_of(self, shares: Decimal) -> AlphaRao:
        if self.total_shares == 0:
            return AlphaRao(floor_int(shares))
        return AlphaRao(floor_int(DEC.divide(DEC.multiply(shares, Decimal(self.total_alpha)), self.total_shares)))

    def shares_for(self, alpha: int) -> Decimal:
        if self.total_alpha == 0 or self.total_shares == 0:
            return Decimal(alpha)
        return DEC.divide(DEC.multiply(Decimal(alpha), self.total_shares), Decimal(self.total_alpha))


@dataclass(frozen=True, slots=True)
class MetagraphLite:
    """Miner-quality summary for LCW gates (risk #22). Read only when lcw.enabled (get_selective_metagraph or
    storage; VERIFY item names); None otherwise."""
    n_miners: int                    # miner UIDs with incentive > 0
    n_miner_coldkeys: int            # distinct coldkeys owning those UIDs
    top1_coldkey_share_ppm: int      # largest coldkey's share of total miner incentive
    n_permit_coldkeys: int           # distinct coldkeys holding a validator permit


@dataclass(frozen=True, slots=True)
class SubnetState:
    key: SubnetKey
    pool: PoolState
    alpha_out: AlphaRao                    # SubnetAlphaOut (includes protocol + burned alpha)
    protocol_alpha: AlphaRao               # SubnetProtocolAlpha
    moving_price: Decimal                  # SubnetMovingPrice (I96F32 exact): emission share AND prune rank
    root_prop: Decimal                     # RootProp U96F32 (computed for blocks < 7,135,420)
    miner_burned: Decimal                  # MinerBurned U96F32 (0 before 8,466,597)
    emission_enabled: bool                 # SubnetEmissionEnabled (absent -> True)
    subtoken_enabled: bool                 # SubtokenEnabled (start_call done); buys need it, sells do not
    reg_allowed: bool                      # NetworkRegistrationAllowed (False freezes the EMA, leaves emit set)
    first_emission_block: Block | None     # FirstEmissionBlockNumber; None = start_call never made
    tempo: int
    last_epoch_block: Block
    ema_halving_blocks: int                # EMAPriceHalvingBlocks (201,600)
    tao_in_emission: Rao                   # SubnetTaoInEmission (per-block value)
    excess_tao: Rao                        # SubnetExcessTao (per-block chain buy)
    alpha_out_emission: AlphaRao           # SubnetAlphaOutEmission (per block)
    alpha_in_emission: AlphaRao            # SubnetAlphaInEmission (per block)
    reservoir_tao: Rao = Rao(0)            # Swap.BalancerTaoReservoir
    reservoir_alpha: AlphaRao = AlphaRao(0)
    tao_flow_cum: int | None = None        # SubnetTaoFlow i64 running total (valid >= 8,466,531, within one generation)
    volume_cum: int | None = None          # SubnetVolume u128
    fast_moving_price: Decimal | None = None   # SubnetFastMovingPrice U64F64 (basket era; cross-check only)
    owner_coldkey: Coldkey | None = None   # SubnetOwner
    owner_hotkey: Hotkey | None = None     # SubnetOwnerHotkey (item name VERIFY at build)
    owner_cut_enabled: bool | None = None  # OwnerCutEnabled[n] (default True; VERIFY)
    owner_cut_autolock: bool | None = None # OwnerCutAutoLockEnabled[n] (default False; VERIFY)
    total_alpha_staked: AlphaRao | None = None   # TotalAlphaStaked (spec >= 448); fallback alpha_out - protocol_alpha
    escrow_alpha: AlphaRao | None = None   # basket escrow alpha E on this subnet (>= 8,765,684); forward-filled
    owner_alpha: AlphaRao | None = None    # owner coldkey's alpha on the owner hotkey (position value)
    max_allowed_validators: int | None = None   # MaxAllowedValidators[n] (router permit filter, section 3.8)
    consensus_mode: int | None = None      # per-subnet consensus mode (spec 475 Null consensus; item name VERIFY)
    metagraph: MetagraphLite | None = None # LCW miner-quality inputs; None unless lcw.enabled
    hotkeys: tuple[HotkeyIdx, ...] = ()    # TRACKED hotkeys only (incl. the owner hotkey), sorted by hotkey
    quality: Quality = Quality.OK

    def hotkey(self, hk: Hotkey) -> HotkeyIdx | None:
        for h in self.hotkeys:
            if h.hotkey == hk:
                return h
        return None


@dataclass(frozen=True, slots=True)
class ChainGlobals:
    spec_version: int
    tx_version: int
    total_issuance: Rao
    block_emission: Rao                     # runtime get_block_emission / curve(TotalIssuance); NEVER BlockEmission storage
    moving_alpha: Decimal                   # SubnetMovingAlpha (I96F32; live 0.0003)
    gate_bar: Decimal                       # EmissionGateBar theta (U64F64; live 0.0082624)
    gate_rank: int                          # EmissionBarRank (absent -> 32)
    gate_exponent: int                      # EmissionGateExponent (absent -> 3)
    tao_weight: Decimal                     # TaoWeight raw / u64::MAX (live 0.18)
    root_tao: Rao                           # SubnetTAO[0]
    owner_cut_u16: int                      # SubnetOwnerCut: a GLOBAL StorageValue (absent -> 11,796)
    subnet_limit: int                       # SubnetLimit (128)
    immunity_period: int                    # NetworkImmunityPeriod (864,000)
    network_rate_limit: int                 # NetworkRateLimit (14,400)
    last_reg_block: Block                   # LastRateLimitedBlock(RateLimitKey::NetworkLastRegistered, suffix 0x02)
    last_lock_cost: Rao                     # NetworkLastLockCost
    min_lock_cost: Rao                      # NetworkMinLockCost (1 TAO)
    lock_reduction_interval: int            # NetworkLockReductionInterval (115,200)
    tao_in_refund_block: Block              # TaoInRefundDeploymentBlock (8,334,450)
    nominator_min_stake: Rao                # NominatorMinRequiredStake factor * DefaultMinStake / 1e6
    cleanup_queue_len: int                  # len(DissolveCleanupQueue)
    n_nonroot_networks: int                 # count(NetworksAdded) - 1
    safe_mode_until: Block | None           # SafeMode.EnteredUntil
    shorts_enabled: bool = False            # monitored only (long-only is enforced by the planner)
    runtime_prune_target: NetUid | None = None   # SubnetInfoRuntimeApi_get_subnet_to_prune (cross-check)


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    block: Block
    block_hash: BlockHash
    timestamp_ms: int                       # Timestamp.Now
    plan: ReadPlan
    glob: ChainGlobals
    subnets: tuple[SubnetState, ...]        # sorted by netuid; exactly one generation per netuid; root excluded.
                                            # Contains EXACTLY the non-root netuids with NetworksAdded == true: a netuid
                                            # that is removed and in cleanup, or queued and not yet added, is excluded
                                            # even while NetworkRegisteredAt / pool storage still exist (reader asserts).
    digest: str = ""                        # blake2b-128 of canonical bytes; computed once by the builder, stored in the lake
    _idx: dict[int, SubnetState] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_idx", {int(s.key.netuid): s for s in self.subnets})

    def by_netuid(self, n: int) -> SubnetState | None:
        return self._idx.get(n)

    def get(self, key: SubnetKey) -> SubnetState | None:
        """None if the netuid is gone OR now holds a different generation (our asset was dissolved)."""
        s = self._idx.get(int(key.netuid))
        return s if s is not None and s.key == key else None
```

### 5.4 `taotrader/core/orders.py`: order intents, fills, FSM, deterministic ids
```python
"""taotrader/core/orders.py - order intents, fills, the order FSM, deterministic ids."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import IntEnum, StrEnum

from .units import PPM, AlphaRao, Block, BookId, Hotkey, OrderId, Ppm, PriceRao, Rao, StrategyId, SubnetKey

Attribution = tuple[tuple[StrategyId, Ppm], ...]   # parts-per-million split across originating sleeves; sums to PPM


class OrderKind(StrEnum):
    ADD_STAKE_LIMIT = "add_stake_limit"                  # call 88: buy alpha with TAO
    REMOVE_STAKE_LIMIT = "remove_stake_limit"            # call 89: sell alpha (partial, or the whole position when full_position)
    REMOVE_STAKE_FULL_LIMIT = "remove_stake_full_limit"  # call 103: whole position, Option limit (sim only; live maps it to 89)
    MOVE_STAKE = "move_stake"                            # call 85, same netuid: hotkey switch, no swap fee
    MOVE_STAKE_LIMIT = "move_stake_limit"                # call 149, cross-subnet rotation: DISABLED until FT-M5c passes


class Urgency(IntEnum):
    """Planner priority. Execution order: EMERGENCY > URGENT > HIGH > NORMAL > LOW."""
    LOW = 0
    NORMAL = 1       # rebalances, soft exits, trims
    HIGH = 2         # time-critical entries, sleeve thesis stops
    URGENT = 3       # emission-off, prune backstop / Tier B, owner exits
    EMERGENCY = 4    # prune Tier A, held subnet is the prune target


class OrderState(StrEnum):
    INTENDED = "INTENDED"      # journaled, nothing sent (the outbox)
    SUBMITTING = "SUBMITTING"  # SubmitStarted journaled BEFORE any I/O
    SUBMITTED = "SUBMITTED"    # venue acked; in flight
    UNKNOWN = "UNKNOWN"        # I/O raised or crashed mid-submit: resolve from chain truth, NEVER blind-resend
    FILLED = "FILLED"          # terminal (full or partial-final)
    FAILED = "FAILED"          # terminal (chain error or venue reject; tx fee may have been paid)
    EXPIRED = "EXPIRED"        # terminal (shield miss / era expiry; provably not executed)
    CANCELLED = "CANCELLED"    # terminal (never submitted: mode change, pool gone)


TERMINAL: frozenset[OrderState] = frozenset(
    {OrderState.FILLED, OrderState.FAILED, OrderState.EXPIRED, OrderState.CANCELLED})

# SUBMITTING never goes straight to FILLED or EXPIRED. A crash leaves an order in SUBMITTING, and the Runner
# journals SubmitUnknown(detail="recovered_submitting") (SUBMITTING -> UNKNOWN) BEFORE any venue.resolve(), so a
# resolved fill or miss always applies to UNKNOWN (section 4.5). Reducer test: SUBMITTING + crash + resolve(LANDED)
# ends FILLED with no orphan.
_NEXT: dict[OrderState, frozenset[OrderState]] = {
    OrderState.INTENDED: frozenset({OrderState.SUBMITTING, OrderState.CANCELLED}),
    OrderState.SUBMITTING: frozenset({OrderState.SUBMITTED, OrderState.FAILED, OrderState.UNKNOWN}),
    OrderState.UNKNOWN: frozenset({OrderState.SUBMITTED, OrderState.FILLED, OrderState.FAILED, OrderState.EXPIRED}),
    OrderState.SUBMITTED: frozenset({OrderState.FILLED, OrderState.FAILED, OrderState.EXPIRED}),
}


class FailReason(StrEnum):
    PRICE_LIMIT_EXCEEDED = "PriceLimitExceeded"     # limit already crossed at execution (fee paid)
    SLIPPAGE_TOO_HIGH = "SlippageTooHigh"           # fill-or-kill amount above max_amount_to_limit (fee paid)
    AMOUNT_TOO_LOW = "AmountTooLow"
    INSUFFICIENT_LIQUIDITY = "InsufficientLiquidity"
    RESERVES_TOO_LOW = "ReservesTooLow"
    SWAP_INPUT_TOO_LARGE = "SwapInputTooLarge"
    SUBTOKEN_DISABLED = "SubtokenDisabled"
    SUBNET_NOT_EXISTS = "SubnetNotExists"           # generation dissolved before the order landed
    NOT_ENOUGH_STAKE = "NotEnoughStakeToWithdraw"
    STAKE_UNAVAILABLE = "StakeUnavailable"          # locks / collateral
    PROXY_ERROR = "ProxyExecutedErr"                # Proxy.ProxyExecuted{result: Err} under ExtrinsicSuccess
    CALL_FILTERED = "CallFiltered"
    SHIELD_MISSED = "ShieldMissed"                  # inner not executed in N+2 (final once N+2 is finalized). Carrier in
                                                    # N+2: tx_fee = carrier fee. Carrier absent: tx_fee 0, and live books
                                                    # any later inclusion's fee with CarrierFeeSettled
    ERA_EXPIRED = "EraExpired"
    NOT_PLACED = "NotPlaced"                        # provably never reached the chain (resolve())
    SAFE_MODE = "SafeMode"
    VENUE_REJECT = "VenueReject"                    # pre-submit check (caps, crossed limit, plan() violation)
    OTHER = "Other"


def make_order_id(run_id: str, book: BookId, block: Block, key: SubnetKey, hotkey: Hotkey,
                  kind: OrderKind, attempt: int) -> OrderId:
    """Deterministic: a replay reproduces the same id, so a duplicate submission is structurally impossible."""
    raw = f"{run_id}|{book}|{block}|{key.netuid}|{key.reg_at}|{hotkey}|{kind.value}|{attempt}".encode()
    return OrderId(hashlib.blake2b(raw, digest_size=12).hexdigest())


@dataclass(frozen=True, slots=True)
class OrderIntent:
    order_id: OrderId
    attempt: int                     # 0 for the first decision; re-decisions after a terminal failure increment it
    book: BookId
    created_block: Block
    kind: OrderKind
    key: SubnetKey                   # origin generation
    hotkey: Hotkey                   # origin hotkey (staking target for buys)
    tao_in: Rao                      # ADD_STAKE_LIMIT: gross TAO incl. swap fee; else 0
    alpha_in: AlphaRao               # REMOVE_*/MOVE_*: alpha to sell/move; ignored when full_position
    full_position: bool              # whole position on (key, hotkey); exempt from the partial-sell minimum. Sim: all
                                     # shares; live: the exact alpha read at the submit head, NEVER 'all'/u64::MAX
    limit_price: PriceRao            # buy: post-fill MARGINAL spot <= limit and limit > spot; sell: >= limit and limit < spot;
                                     # MOVE_STAKE_LIMIT: min dest alpha per origin alpha * 1e9; MOVE_STAKE: 0
    allow_partial: bool
    shielded: bool                   # submit_shielded (era 8). False only for risk exits when finality lag > 5 blocks
    valid_until: Block               # shielded: planned inclusion block (created + finality_lag + latency); the venue's
                                     # VenueAck.expected_fill_block (submit head + 2) is the ONLY legal inclusion block.
                                     # unshielded (era 16): created + finality_lag + 16. Nonce locks use SubmitStarted.era_end
    expected_out: int                # model output at decision (alpha rao for buys, rao for sells)
    urgency: Urgency
    attribution: Attribution
    reason: str                      # machine-readable rule/reason code
    dest_key: SubnetKey | None = None
    dest_hotkey: Hotkey | None = None

    def __post_init__(self) -> None:
        if sum(p for _, p in self.attribution) != PPM:
            raise ValueError("attribution must sum to 1e6 ppm")
        if self.kind is OrderKind.ADD_STAKE_LIMIT:
            if self.tao_in <= 0 or self.alpha_in != 0 or self.full_position:
                raise ValueError("ADD_STAKE_LIMIT needs tao_in > 0, alpha_in == 0, full_position False")
        elif self.kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
            if self.tao_in != 0 or (self.alpha_in <= 0 and not self.full_position):
                raise ValueError("REMOVE needs tao_in == 0 and alpha_in > 0 or full_position")
        else:
            if self.dest_hotkey is None or (self.kind is OrderKind.MOVE_STAKE_LIMIT and self.dest_key is None):
                raise ValueError("MOVE orders need a destination")


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str                     # f"{order_id}:{attempt}:{leg}" - idempotency key of the accounting effect
    order_id: OrderId
    attempt: int
    book: BookId
    block: Block                     # inclusion block
    kind: OrderKind
    key: SubnetKey
    hotkey: Hotkey
    tao: Rao                         # BUY: gross TAO debited incl. swap fee; SELL: TAO credited; MOVE: 0
    alpha: AlphaRao                  # BUY: alpha received; SELL: alpha sold (pool absorbs all); MOVE: alpha moved
    shares: Decimal                  # shares credited (BUY) / debited (SELL, MOVE origin); exact
    swap_fee: int                    # input-side units: rao (BUY) or alpha rao (SELL); 0 for same-subnet MOVE
    author_fee_tao: Rao              # SELL: TAO paid out of the pool to the block author for the fee alpha
    tx_fee: Rao                      # carrier + inner extrinsic fee paid by the fee payer
    d_pool_tao: int                  # reserve change caused by this fill (own-impact overlay input)
    d_pool_alpha: int
    spot_before: PriceRao
    shortfall_ppm: Ppm               # 1 - executed / spot, incl. swap fee
    complete: bool                   # False: allow_partial stopped at the limit
    exact_block: bool = True         # False: evaluated on a later stride snapshot (stride replays only); excluded from
                                     # failure-burst counters and reported separately (section 3.12)
    dest_key: SubnetKey | None = None
    dest_hotkey: Hotkey | None = None
    dest_shares: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderRecord:
    intent: OrderIntent
    state: OrderState = OrderState.INTENDED
    fill_ids: tuple[str, ...] = ()

    def to(self, new: OrderState) -> OrderRecord:
        if new not in _NEXT.get(self.state, frozenset()):
            raise IllegalTransition(f"{self.intent.order_id}: {self.state.value} -> {new.value}")
        return replace(self, state=new)


class IllegalTransition(Exception):
    pass


class Resolution(StrEnum):
    NOT_PLACED = "NOT_PLACED"              # provably never on chain (era expired, nonce unconsumed): terminal fail, may re-decide
    PLACED = "PLACED"                      # in flight, awaiting finalized N+2 (sim: re-derived VenueAck attached)
    LANDED = "LANDED"                      # outcome final (fill, inner failure, or shield miss at N+2); facts attached
    UNRESOLVABLE_YET = "UNRESOLVABLE_YET"  # ask again next block; NOTHING is re-sent meanwhile


@dataclass(frozen=True, slots=True)
class VenueCaps:
    kind: str                        # "sim" | "paper" | "live_dry" | "live"
    shield_latency_blocks: int       # 2 (N+2)
    era_blocks: int                  # 8 for shielded carriers
    supports_rotation: bool          # MOVE_STAKE_LIMIT allowed (False in v1)
    max_inflight_per_netuid: int     # 1
    n_delegates: int                 # funded Staking-proxy delegates (3); sim/paper model the same count (ExecCfg.n_delegates)
```

### 5.5 `taotrader/core/signals.py`: the declarative decision layer
```python
"""taotrader/core/signals.py - the declarative decision layer: strategies emit Signals, never orders.

Floats stop here: every field is an integer (ppm / rao / blocks). Strategies convert with core.fixed.to_ppm.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from .orders import Attribution, Urgency
from .units import Block, Hotkey, Mode, Ppm, PpmPerDay, PriceRao, Rao, StrategyId, SubnetKey


class SignalKind(StrEnum):
    TARGET = "target"   # "I want to hold this, sized at weight_ppm of my sleeve (subject to caps)"
    EXIT = "exit"       # "my sleeve's share must go to 0" (sleeve-level exit; never touches other sleeves)
    AVOID = "avoid"     # "do not open new exposure in this name for my sleeve"


@dataclass(frozen=True, slots=True)
class Signal:
    strategy: StrategyId
    key: SubnetKey
    asof: Block
    kind: SignalKind
    weight_ppm: Ppm = Ppm(0)                 # TARGET: share of the strategy's sleeve budget
    edge_ppm_day: PpmPerDay = PpmPerDay(0)   # expected TAO return per day held, net of take & dilution, BEFORE trading costs
    alpha_h_ppm: Ppm = Ppm(0)                # expected gross return over horizon_blocks; >0 lets the allocator apply V*
    max_size_rao: Rao | None = None          # sleeve's own size cap (e.g. carry V*); applied before overlay caps
    horizon_blocks: int = 0                  # opinion time-to-live; 0 = until the strategy's next evaluation
    urgency: Urgency = Urgency.NORMAL        # EXIT may be HIGH (sleeve thesis stop); risk urgencies belong to the overlay
    hotkey_pref: Hotkey | None = None        # advisory only; YieldRouter picks the one hotkey per subnet
    declares_dilution: bool = False          # True: sleeve already netted structural sell load; overlay must not re-haircut
    reasons: tuple[str, ...] = ()            # machine-readable reason codes


@dataclass(frozen=True, slots=True)
class StrategyOutput:
    signals: tuple[Signal, ...]
    memory: object                           # the strategy's own frozen Memory dataclass (codec-encodable)


@dataclass(frozen=True, slots=True)
class ForcedExit:
    key: SubnetKey
    urgency: Urgency                         # EMERGENCY / URGENT (risk) or NORMAL (liquidity trim, launch stop)
    rule: str                                # "prune_A" | "prune_B" | "prune_backstop" | "prune_target" | "emission_off" |
                                             # "owner" | "burn" | "liquidity_trim" | "dissolved" | "operator" | ...
    exit_slip_ppm: Ppm                       # average-slippage budget for the marginal limit
    trim_to_rao: Rao | None = None           # None = full exit; else target executable value after the trim


@dataclass(frozen=True, slots=True)
class TargetPosition:
    key: SubnetKey
    hotkey: Hotkey                           # the book's router choice (risk/router.py; one hotkey per coldkey and subnet)
    value_rao: Rao                           # target executable (sim_sell) value in TAO after allocation and caps
    urgency: Urgency
    attribution: Attribution
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SleeveXfer:
    """One netting transfer planned by the allocator (section 3.10 step 6); the Engine journals it as SleeveTransfer."""
    key: SubnetKey
    from_strategy: StrategyId
    to_strategy: StrategyId
    shares: Decimal                          # share-pool shares moved from -> to (exact)
    tao: Rao                                 # virtual TAO paid to -> from at the decision spot
    price: PriceRao                          # decision spot on the book's view


@dataclass(frozen=True, slots=True)
class TargetBook:
    asof: Block
    items: tuple[TargetPosition, ...]        # sorted by key
    forced: tuple[ForcedExit, ...] = ()
    halt_entries: bool = False
    transfers: tuple[SleeveXfer, ...] = ()   # netting between sleeves; sorted by (key, from, to); never touches the pool

    def get(self, key: SubnetKey) -> TargetPosition | None:
        for t in self.items:
            if t.key == key:
                return t
        return None

    def reduced(self, key: SubnetKey, value_rao: Rao, reason: str, urgency: Urgency | None = None) -> TargetBook:
        """Monotone by construction: a risk overlay may lower a target, never raise it."""
        out: list[TargetPosition] = []
        for t in self.items:
            if t.key == key:
                if value_rao > t.value_rao:
                    raise ValueError("risk overlay tried to increase a target")
                t = replace(t, value_rao=value_rao, reasons=t.reasons + (reason,),
                            urgency=urgency if urgency is not None else t.urgency)
            out.append(t)
        return replace(self, items=tuple(out))


@dataclass(frozen=True, slots=True)
class RiskAction:
    rule: str                 # e.g. "prune.entry_rank", "liquidity.vcap", "mode.caution", "owner.cooldown"
    key: SubnetKey | None
    action: str               # "CLAMP" | "FORCE_EXIT" | "VETO_ENTRY" | "HALT_ENTRIES" | "MODE" | "MONITOR"
    detail: str               # canonical "k=v;k=v" text (journaled, used by reports)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    targets: TargetBook
    actions: tuple[RiskAction, ...]
    mode: Mode
```

### 5.6 `taotrader/core/events.py`: chain events and the journal vocabulary
```python
"""taotrader/core/events.py - (1) chain events derived by diffing snapshots, (2) the closed, versioned journal vocabulary.

Chain events are a pure function of two consecutive snapshots (protocol.derive.derive_events), so they are
identical in backtest (60-block stride), paper and live (per finalized block). Journal events are the ONLY way
engine state changes (engine.reducer.reduce).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar, TypeVar

from .orders import FailReason, Fill, OrderIntent
from .signals import RiskAction, Signal
from .state import ReadPlan
from .units import (
    AlphaRao, Block, BlockHash, BookId, Hotkey, Mode, NetUid, OrderId, PositionKey, Ppm, PriceRao, Rao, StrategyId,
    SubnetKey,
)


# ----------------------------------------------------------------------------------------------- chain events
class ChainEventKind(StrEnum):
    REGISTERED = "registered"                    # a new generation (netuid, reg_at) appeared
    DEREGISTERED = "deregistered"                # a generation vanished (prune/dissolve); reuse => DEREGISTERED + REGISTERED
    START_CALLED = "start_called"                # FirstEmissionBlockNumber None -> Some
    EMISSION_TOGGLED = "emission_toggled"        # SubnetEmissionEnabled flip (flag = new value)
    REG_ALLOWED_TOGGLED = "reg_allowed_toggled"  # NetworkRegistrationAllowed flip
    EPOCH_DRAIN = "epoch_drain"                  # LastEpochBlock changed
    LARGE_FLOW = "large_flow"                    # |dSubnetTaoFlow| >= large_flow_frac * SubnetTAO between snapshots
    OWNER_POSITION_CHANGED = "owner_position"    # owner coldkey alpha changed beyond accrual tolerance (amount signed)
    OWNER_CHANGED = "owner_changed"              # SubnetOwner / SubnetOwnerHotkey diff
    AUTOLOCK_TOGGLED = "autolock_toggled"        # OwnerCutAutoLockEnabled flip
    TAKE_CHANGED = "take_changed"                # Delegates[h] diff for a tracked hotkey (old/new u16 text)
    DIVIDEND_MEMBERSHIP = "dividend_membership"  # tracked hotkey entered (flag True) / left AlphaDividendsPerSubnet
    REGISTRATION_SEEN = "registration_seen"      # LastRateLimitedBlock(0x02) advanced
    REG_WINDOW_OPENED = "reg_window_opened"      # block crossed last_reg_block + NetworkRateLimit
    IMMUNITY_EXPIRED = "immunity_expired"        # generation entered the prune candidate set
    PRUNE_TARGET_CHANGED = "prune_target_changed"
    GATE_BAR_UPDATED = "gate_bar_updated"
    SPEC_CHANGED = "spec_changed"                # spec_version or transaction_version changed
    PARAM_CHANGED = "param_changed"              # a freeze-list global or per-subnet value changed (name/old/new; key if per subnet)
    SAFE_MODE = "safe_mode"                      # SafeMode.EnteredUntil set or cleared (flag = active)


@dataclass(frozen=True, slots=True)
class ChainEvent:
    kind: ChainEventKind
    block: Block
    key: SubnetKey | None = None
    hotkey: Hotkey | None = None
    flag: bool | None = None          # toggles / membership / safe-mode active
    amount: int | None = None         # LARGE_FLOW: signed rao; OWNER_POSITION_CHANGED: signed alpha rao
    frac_ppm: Ppm | None = None       # LARGE_FLOW: amount / SubnetTAO
    name: str | None = None           # PARAM_CHANGED: storage item name
    old: str | None = None            # canonical text of the old value
    new: str | None = None


@dataclass(frozen=True, slots=True)
class HealthObs:
    """Wall-clock-derived observations are INPUTS: journaled with each snapshot so replays are exact.
    Backtests use HealthObs.nominal()."""
    finality_lag_blocks: int
    secs_since_block: int
    healthy_endpoints: int
    head_lag_blocks: int              # submit node vs best known head
    feed_gap_blocks: int              # blocks skipped since the previous snapshot beyond the cadence

    @staticmethod
    def nominal() -> HealthObs:
        return HealthObs(finality_lag_blocks=3, secs_since_block=12, healthy_endpoints=2, head_lag_blocks=0,
                         feed_gap_blocks=0)                      # finality lag == ExecCfg.finality_lag_blocks


# ----------------------------------------------------------------------------------------------- journal events
class JournalEvent:
    __slots__ = ()
    KIND: ClassVar[str] = ""
    VERSION: ClassVar[int] = 1

    def idem(self) -> str | None:
        """Journal-level UNIQUE idempotency key; None = not deduplicated."""
        return None


REGISTRY: dict[str, type[JournalEvent]] = {}
E = TypeVar("E", bound=type[JournalEvent])


def journal_event(kind: str, version: int = 1) -> Callable[[E], E]:
    def deco(cls: E) -> E:
        if kind in REGISTRY:
            raise ValueError(f"duplicate journal kind {kind}")
        cls.KIND = kind
        cls.VERSION = version
        REGISTRY[kind] = cls
        return cls
    return deco


# --- inputs from the world (payloads of snapshots live in the lake; the digest pins their exact content)
@journal_event("snapshot_observed")
@dataclass(frozen=True, slots=True)
class SnapshotObserved(JournalEvent):
    block: Block
    block_hash: BlockHash
    digest: str
    plan: ReadPlan
    ts_ms: int
    health: HealthObs

    def idem(self) -> str:
        return f"snap:{self.block_hash}"


@journal_event("operator_command")
@dataclass(frozen=True, slots=True)
class OperatorCommand(JournalEvent):
    block: Block
    command: str                      # "halt" | "resume" | "exits_only" | "flatten:<netuid>"
    reason: str
    nonce: str                        # from the control file; makes re-delivery idempotent

    def idem(self) -> str:
        return f"op:{self.nonce}"


@journal_event("capital_changed")
@dataclass(frozen=True, slots=True)
class CapitalChanged(JournalEvent):
    book: BookId
    block: Block
    cash_delta: int
    fee_float_delta: int
    memo: str

    def idem(self) -> str:
        return f"capital:{self.book}:{self.memo}"


@journal_event("config_applied")
@dataclass(frozen=True, slots=True)
class ConfigApplied(JournalEvent):
    block: Block
    config_hash: str
    code_hash: str
    prereg_hash: str                  # hash of config/preregistration.toml in force


@journal_event("model_drift_observed")
@dataclass(frozen=True, slots=True)
class ModelDriftObserved(JournalEvent):
    block: Block
    probe: str                        # "sim_swap_buy" | "sim_swap_sell" | "price_all" | "prune_target" | "provider"
                                      # | "emission_parity" | "yield_parity" | "hazard_validity" (these three may start
                                      # a post-spec burn-in, section 3.10 step 2)
    netuid: NetUid | None
    err_ppm: int


# --- derived chain facts (deterministic; no idem)
@journal_event("chain_event")
@dataclass(frozen=True, slots=True)
class ChainEventObserved(JournalEvent):
    event: ChainEvent


# --- accounting facts emitted by the engine
@journal_event("yield_accrued")
@dataclass(frozen=True, slots=True)
class YieldAccrued(JournalEvent):
    book: BookId
    key: SubnetKey
    hotkey: Hotkey
    block: Block
    index_before: Decimal
    index_after: Decimal
    delta_alpha: int                  # value change of OUR shares; may be -1 rao from share-pool rounding


@journal_event("dereg_settled")
@dataclass(frozen=True, slots=True)
class DeregSettled(JournalEvent):
    book: BookId
    key: SubnetKey
    hotkey: Hotkey
    block: Block
    alpha_value: AlphaRao             # our alpha at the last good snapshot
    payout_tao: Rao                   # modelled (backtest/paper/live-dry, Engine ACCOUNT) or OBSERVED free-TAO credit
                                      # (live: reconciliation only; the Engine never emits it in RunMode.LIVE)
    model: str                        # "formula" | "fixed:0.35" | "observed"

    def idem(self) -> str:
        return f"dereg:{self.book}:{self.key.netuid}:{self.key.reg_at}"


@journal_event("decision_trace")
@dataclass(frozen=True, slots=True)
class DecisionTrace(JournalEvent):
    book: BookId
    block: Block
    strategies_run: tuple[StrategyId, ...]
    signals: tuple[Signal, ...]
    actions: tuple[RiskAction, ...]
    mode: Mode
    memories: tuple[tuple[StrategyId, bytes], ...]   # canonical bytes of each strategy's new Memory, plus the
                                                     # pseudo-id "risk.router" -> RouterState (section 5.10)
    features_digest: str
    n_intents: int
    calib_digest: str = ""                           # digest of the Calibration in force (section 5.12)
    nav_liq: Rao = Rao(0)                            # book NAV_liq at this tick (reducer samples one per 7,200 blocks)
    sleeve_nav: tuple[tuple[StrategyId, Rao], ...] = ()   # un-netted stand-alone value per sleeve (kill switches)


@journal_event("mode_changed")
@dataclass(frozen=True, slots=True)
class ModeChanged(JournalEvent):
    book: BookId
    block: Block
    mode: Mode
    reason: str


@journal_event("sleeve_transfer")
@dataclass(frozen=True, slots=True)
class SleeveTransfer(JournalEvent):
    """Netting (section 3.10 step 6): one sleeve sells to another at the decision spot. Virtual: reduce moves
    SleeveHolding shares from -> to and sleeve_cash to -> from, and posts NO ledger entries."""
    book: BookId
    block: Block
    key: SubnetKey
    from_strategy: StrategyId
    to_strategy: StrategyId
    shares: Decimal
    tao: Rao
    price: PriceRao

    def idem(self) -> str:
        return (f"xfer:{self.book}:{self.block}:{self.key.netuid}:{self.key.reg_at}:"
                f"{self.from_strategy}:{self.to_strategy}")


@journal_event("order_intended")
@dataclass(frozen=True, slots=True)
class OrderIntended(JournalEvent):
    intent: OrderIntent

    def idem(self) -> str:
        return f"intent:{self.intent.order_id}"


@journal_event("order_cancelled")
@dataclass(frozen=True, slots=True)
class OrderCancelled(JournalEvent):
    book: BookId
    order_id: OrderId
    attempt: int
    block: Block
    reason: str

    def idem(self) -> str:
        return f"cancel:{self.order_id}:{self.attempt}"


# --- execution-side facts (the write-ahead bracket)
@journal_event("submit_started")
@dataclass(frozen=True, slots=True)
class SubmitStarted(JournalEvent):
    book: BookId
    order_id: OrderId
    attempt: int
    delegate: str                     # fee-paying delegate id ("sim0".."sim2" for simulators)
    nonce: int | None                 # live: carrier nonce n reserved by venue.reserve() (pool-aware
                                      # system_accountNextIndex read immediately before this record); inner = n + 1
    era_end: Block | None = None      # last block of the carrier's mortal era (finalized anchor + 8, or + 16 unshielded,
                                      # + 2 margin): the delegate's nonce lock and the carrier-fee settlement point

    def idem(self) -> str:
        return f"submit:{self.order_id}:{self.attempt}"


@journal_event("venue_ack")
@dataclass(frozen=True, slots=True)
class VenueAck(JournalEvent):
    book: BookId
    order_id: OrderId
    attempt: int
    submit_block: Block               # best head at submit
    expected_fill_block: Block        # submit_block + shield latency (+ sim latency)
    carrier_hash: str                 # live: carrier extrinsic hash; sim: ""
    inner_hash: str                   # live: inner (proxied) extrinsic hash; sim: ""

    def idem(self) -> str:
        return f"ack:{self.order_id}:{self.attempt}"


@journal_event("submit_unknown")
@dataclass(frozen=True, slots=True)
class SubmitUnknown(JournalEvent):
    book: BookId
    order_id: OrderId
    attempt: int
    detail: str                       # exception text | "recovered_submitting" | "nonce_mismatch:<used nonce>"


@journal_event("fill_reported")
@dataclass(frozen=True, slots=True)
class FillReported(JournalEvent):
    fill: Fill

    def idem(self) -> str:
        return f"fill:{self.fill.fill_id}"


@journal_event("order_failed")
@dataclass(frozen=True, slots=True)
class OrderFailed(JournalEvent):
    book: BookId
    order_id: OrderId
    attempt: int
    block: Block
    reason: FailReason
    tx_fee: Rao                       # a failed inner call still pays; SHIELD_MISSED: carrier fee if the carrier is in
                                      # N+2, else 0 (live then settles it with CarrierFeeSettled)
    expired: bool = False             # True -> EXPIRED terminal state, else FAILED
    exact_block: bool = True          # False: evaluated on a later stride snapshot (stride replays only)
    detail: str = ""                  # decoded chain error name, e.g. a Proxy.ProxyExecuted Err (section 9.6)

    def idem(self) -> str:
        return f"fail:{self.order_id}:{self.attempt}"


@journal_event("carrier_fee_settled")
@dataclass(frozen=True, slots=True)
class CarrierFeeSettled(JournalEvent):
    """Live only. After a shield miss with the carrier ABSENT from N+2, the carrier fee is settled from the
    delegate's nonce once era_end has passed:
    nonce n -> never included (fee 0); n + 1 -> carrier included, inner dropped (fee = observed delegate balance
    change); >= n + 2 -> the inner was included after all: LiveVenue reads its dispatch result and reconciliation
    raises a key alarm. Valid on an EXPIRED order; posts fee_float -fee, fees:tx +fee."""
    book: BookId
    order_id: OrderId
    attempt: int
    block: Block
    fee_rao: Rao
    outcome: str                      # "never_included" | "carrier_only" | "inner_included"

    def idem(self) -> str:
        return f"carrier:{self.order_id}:{self.attempt}"


@journal_event("recon_adjusted")
@dataclass(frozen=True, slots=True)
class ReconAdjusted(JournalEvent):
    """Live only: chain truth wins. Entries halt until QuarantineCleared."""
    book: BookId
    block: Block
    cash_delta: int
    fee_float_delta: int
    share_deltas: tuple[tuple[PositionKey, Decimal], ...]
    evidence: str


@journal_event("quarantine_cleared")
@dataclass(frozen=True, slots=True)
class QuarantineCleared(JournalEvent):
    book: BookId
    block: Block
    reason: str
```

### 5.7 `taotrader/core/portfolio.py`: portfolio and double-entry ledger
```python
"""taotrader/core/portfolio.py - typed portfolio state + an independent double-entry ledger.

Both are updated from the same journal events by engine.reducer and cross-checked by check_invariants
after EVERY commit. A breach halts entries and alerts; it never crash-loops.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .orders import Fill, OrderKind
from .units import Block, Hotkey, PositionKey, Rao, StrategyId, SubnetKey

TAO_UNIT = "TAO"


def alpha_unit(key: SubnetKey) -> str:
    return f"A:{key.netuid}:{key.reg_at}"


def pos_account(key: SubnetKey, hotkey: Hotkey) -> str:
    return f"pos:{key.netuid}:{key.reg_at}:{hotkey}"


def market_account(key: SubnetKey) -> str:
    return f"market:{key.netuid}:{key.reg_at}"


# Fixed accounts: "cash", "fee_float", "fees:swap", "fees:tx", "income:yield", "loss:dereg", "equity:capital".


@dataclass(frozen=True, slots=True)
class Position:
    """Physical holding. Exactly one hotkey per (coldkey, subnet generation) - YieldRouter rule."""
    key: SubnetKey
    hotkey: Hotkey
    shares: Decimal          # share-pool shares; yield raises the hotkey index, never this number
    cost_tao: Rao            # remaining cost basis incl. swap + tx fees (attribution, tax lots)
    opened_block: Block

    @property
    def pkey(self) -> PositionKey:
        return PositionKey(self.key, self.hotkey)


@dataclass(frozen=True, slots=True)
class SleeveHolding:
    """Virtual ownership of a physical position by one sleeve. Sum over sleeves == Position.shares."""
    strategy: StrategyId
    key: SubnetKey
    shares: Decimal
    cost_tao: Rao


@dataclass(frozen=True, slots=True)
class Portfolio:
    cash: Rao                                       # free TAO on the dedicated coldkey (the planner keeps
                                                    # RiskCfg.min_free_real_rao of it untouched: MIN_FREE_REAL)
    fee_float: Rao                                  # TAO on the fee-paying delegate(s): alpha-fee-trap buffer
    positions: tuple[Position, ...] = ()            # sorted by key
    sleeves: tuple[SleeveHolding, ...] = ()         # sorted by (strategy, key)
    sleeve_cash: tuple[tuple[StrategyId, Rao], ...] = ()   # virtual cash per sleeve; sums to cash

    def position(self, key: SubnetKey) -> Position | None:
        for p in self.positions:
            if p.key == key:
                return p
        return None


@dataclass(frozen=True, slots=True)
class Posting:
    account: str
    unit: str                 # "TAO" or alpha_unit(key)
    amount: int               # signed rao / alpha rao


@dataclass(frozen=True, slots=True)
class LedgerTxn:
    txn_id: str               # "fill:<fill_id>", "fail:<order>:<attempt>", "carrier:<order>:<attempt>",
                              # "yield:<book>:<key>:<block>", "dereg:..."
    block: Block
    postings: tuple[Posting, ...]

    def validate(self) -> None:
        sums: dict[str, int] = {}
        for p in self.postings:
            sums[p.unit] = sums.get(p.unit, 0) + p.amount
        bad = {u: s for u, s in sums.items() if s != 0}
        if bad:
            raise ValueError(f"unbalanced ledger txn {self.txn_id}: {bad}")


def fill_txn(f: Fill) -> LedgerTxn:
    """Postings for a fill. Every unit sums to zero; fees are explicit accounts."""
    mkt, au, pa = market_account(f.key), alpha_unit(f.key), pos_account(f.key, f.hotkey)
    p: list[Posting]
    if f.kind is OrderKind.ADD_STAKE_LIMIT:
        p = [Posting("cash", TAO_UNIT, -f.tao), Posting(mkt, TAO_UNIT, f.tao - f.swap_fee),
             Posting("fees:swap", TAO_UNIT, f.swap_fee), Posting(pa, au, f.alpha), Posting(mkt, au, -f.alpha)]
    elif f.kind in (OrderKind.REMOVE_STAKE_LIMIT, OrderKind.REMOVE_STAKE_FULL_LIMIT):
        p = [Posting(mkt, TAO_UNIT, -(f.tao + f.author_fee_tao)), Posting("cash", TAO_UNIT, f.tao),
             Posting("fees:swap", TAO_UNIT, f.author_fee_tao), Posting(pa, au, -f.alpha), Posting(mkt, au, f.alpha)]
    elif f.kind is OrderKind.MOVE_STAKE:
        assert f.dest_hotkey is not None
        p = [Posting(pa, au, -f.alpha), Posting(pos_account(f.key, f.dest_hotkey), au, f.alpha)]
    else:
        raise ValueError("MOVE_STAKE_LIMIT is disabled in v1")
    if f.tx_fee:
        p += [Posting("fee_float", TAO_UNIT, -f.tx_fee), Posting("fees:tx", TAO_UNIT, f.tx_fee)]
    return LedgerTxn(f"fill:{f.fill_id}", f.block, tuple(p))


def check_invariants(portfolio: Portfolio, ledger_balances: dict[tuple[str, str], int],
                     position_alpha: dict[PositionKey, int]) -> list[str]:
    """Returns violations (empty = OK). Implemented by WP0. Checks:
    1. cash == ledger("cash","TAO") and fee_float == ledger("fee_float","TAO"); both >= 0.
    2. For each Position: |value_of(shares) - ledger(pos_account, alpha_unit)| <= 2 rao (position_alpha supplies value_of).
    3. Sum of SleeveHolding.shares per key == Position.shares (exact Decimal); sum of sleeve_cash == cash.
       SleeveTransfer moves shares and sleeve cash between sleeves without ledger postings, so it preserves both sums.
    4. Each unit sums to zero across all accounts.
    5. No Position with shares <= 0; at most one Position per SubnetKey.
    """
    raise NotImplementedError
```

### 5.8 `taotrader/core/views.py`: shared per-snapshot features
```python
"""taotrader/core/views.py - shared per-snapshot features. Computed ONCE per snapshot by features.engine and
shared by every book and strategy. Floats are allowed here (feature math); decisions quantize via to_ppm.

None means "not enough same-generation history" - consumers must treat None as ineligible, never as zero.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .units import Block, Hotkey, Ppm, SubnetKey


@dataclass(frozen=True, slots=True)
class RouterCandidate:
    """Book-independent YieldRouter inputs for one tracked hotkey (WP5, section 3.8). The per-book choice
    (Q_MAX against own shares, hysteresis, MOVE_STAKE) is WP8 risk/router.py."""
    hotkey: Hotkey
    score_ppm_day: int                   # EWMA_{hl 20 epochs}(d ln I per epoch) * 7200/Tempo, net of take (to_ppm)
    take_u16: int                        # Delegates[h]
    childkey_take_u16: int               # ChildkeyTake(h, n)
    member_frac_ppm: Ppm                 # share of the last 20 epochs with h in AlphaDividendsPerSubnet(n, .)
    member_last2: bool                   # recipient in each of the last 2 epochs
    permit_rank: int | None              # rank by TotalHotkeyAlpha among dividend recipients (1 = largest)
    ratio_ok: bool                       # realised / closed-form net yield in [0.65, 1.35] (False while T6 is re-validated)
    take_increase_recent: bool           # take raised within 216,000 blocks (Delegates diffs); unknown -> False, flagged
    eligible: bool                       # every book-independent filter of section 3.8 passes


@dataclass(frozen=True, slots=True)
class Feat:
    key: SubnetKey
    # --- price, depth, returns (log, from the median-of-3 60-block price; generation-truncated)
    spot: float                          # TAO/alpha (era-correct)
    pool_tao: float                      # SubnetTAO in TAO
    k_w: float                           # 1 / w_base (2.0 at 0.5/0.5)
    ret_1h: float | None
    ret_1d: float | None
    ret_7d: float | None
    sigma_d: float | None                # 14-day realised SD of daily ln P
    fast_ema_gap: float                  # ln(min(spot,1) / local 600-block-half-life EMA of spot)
    ema_gap: float                       # ln(min(spot,1) / SubnetMovingPrice)
    # --- flows (dSubnetTaoFlow / SubnetTAO; None before 8,466,531 or across a generation change)
    flow_1h: float | None
    flow_1d: float | None
    flow_7d: float | None
    flow_z_1d: float | None              # robust z vs trailing 30 d of this generation
    # --- emission and structure (shared protocol replicas; read-live parameters)
    emis_tao_day: float                  # modelled E_i
    chain_buy_day: float                 # modelled chain buy (TAO/day); 0 when E <= rp*alpha_em*spot*7200
    obs_emis_tao_day: float              # 7200*(SubnetTaoInEmission + SubnetExcessTao + reservoir delta)
    gate_keep: float                     # g/b = 1/(1+(theta/b)^h)
    burn_adj_rank: int | None            # 1 = largest burn-adjusted share among eligible
    ema_rank_desc: int | None            # 1 = largest SubnetMovingPrice
    rp: float                            # root proportion
    sell_push_day: float                 # k_w * S / y from protocol.sellload (fraction of price per day, >= 0)
    cb_push_day: float                   # k_w * CB / y
    escrow_frac: float | None            # E / x
    # --- yield (book-independent YieldRouter inputs; the per-book choice is risk/router.py, section 3.8)
    a_earn_alpha: float                  # sum TotalHotkeyAlpha over dividend recipients (tracked approximation flagged)
    yield_cf_gross_day: float            # AE*(1-c_o)*0.5*(1-rp)/A_earn
    router_candidates: tuple[RouterCandidate, ...]   # tracked hotkeys, best first (eligible, score, take, stake, hotkey)
    best_candidate: Hotkey | None        # first eligible candidate; NOT a book's choice (books differ by Q_MAX, hysteresis)
    yield_net_day: float | None          # realised EWMA d ln I of best_candidate, net of take
    a_earn_growth_day: float             # deterministic A_earn growth (escrow + compounding) as a fraction/day
    # --- lifecycle and prune
    age_reg_blocks: int                  # block - NetworkRegisteredAt
    since_start_blocks: int | None       # block - (FirstEmissionBlockNumber - 1)
    immune: bool
    immune_until: Block
    prune_rank: int | None               # 1 = current target among non-immune; None if immune
    rho: float | None                    # SubnetMovingPrice / bottom non-immune EMA
    t_star_stress_blocks: float | None   # time-to-target with spot -> D_STRESS * spot; inf -> None
    launch_flags: frozenset[str]         # Gatekeeper mechanical flags (section 3.7)
    # --- execution microstructure
    beta_entry_ppm: Ppm                  # q95 |ln p_t - ln p_{t-h}| over 1,800 blocks (30 stride points), own-fill blocks
                                         # excluded; h = finality_lag + latency (per-block data) or stride (section 3.12)
    beta_exit_ppm: Ppm                   # q99 of the same sample, for sells
    # --- owner and holder concentration (section 3.6; brief risk #7)
    owner_sold_6h_frac: float | None     # owner net alpha sold over 1,800 blocks / pool alpha
    owner_liquid_frac: float | None      # owner_alpha * (1 - autolock) / SubnetAlphaIn
    top_holder_frac: float | None        # (owner_alpha + top-5 tracked TotalHotkeyAlpha) / (AlphaOut - ProtocolAlpha)


@dataclass(frozen=True, slots=True)
class PruneView:
    prune_possible: bool                 # n_nonroot + cleanup_queue_len >= SubnetLimit
    target: SubnetKey | None             # local rule; == runtime target or a data alarm is raised
    runtime_agrees: bool
    ladder: tuple[SubnetKey, ...]        # non-immune, ascending (moving_price, reg_at)
    bottom_ema: float
    blocks_since_reg: int
    window_open: bool                    # blocks_since_reg >= NetworkRateLimit
    blocks_to_window: int                # 0 if open
    cost_ratio: float                    # r = registration cost / NetworkLastLockCost
    p_reg_ppm: tuple[tuple[int, Ppm], ...]   # (horizon_blocks, P(a registration lands within horizon))
    hazard_valid: bool                   # False after a registration-economics change (spec-475 PoW scope etc.)
    immunity_calendar: tuple[tuple[Block, SubnetKey], ...]   # upcoming expiries, ascending


@dataclass(frozen=True, slots=True)
class EmissionView:
    theta: float
    gate_rank: int
    sum_ema: float                       # sum SubnetMovingPrice over eligible; root sell flag = sum_ema > 1
    root_flag: bool
    parity_err_max_tao_day: float        # max |E_model - E_obs|
    model_ok: bool                       # protocol.emission.parity_ok: median |rel err| < 1% AND >= 90% within 5%, over
                                         # enabled subnets with E > 1 TAO/day, trailing 300-block observed emission (= T2a)


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    block: Block
    warm: bool                           # >= 30 days of history ingested; no strategy runs before
    feats: Mapping[SubnetKey, Feat]
    prune: PruneView
    emission: EmissionView
    regime_id: str                       # protocol.regimes label (reports and valid_from guards; strategies may not branch on it)
    universe_eligible: int               # overlay-floor sections A-G count (book-independent; published every snapshot)
    beta_horizon_blocks: int             # h used for beta at this snapshot (journaled via digest; reports split by it)
    digest: str                          # canonical digest, journaled in DecisionTrace
```

### 5.9 `taotrader/core/config.py`: frozen configuration (defaults = sections 2-3)
```python
"""taotrader/core/config.py - frozen config dataclasses. TOML is parsed and validated by ops.config_load (WP0);
the core only sees these. Units are in the field names. Defaults == section 3 of DESIGN.md.
Cross-field rule checked by config_load: RiskCfg.unwind_exec_blocks == ExecCfg.finality_lag_blocks + latency_blocks.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .units import BookId, Ppm, Rao, RunMode, Stage, StrategyId


@dataclass(frozen=True, slots=True)
class ExecCfg:
    """Shared cost and fill model (venues.sim, strategies' cost gates, planner)."""
    latency_blocks: int = 2                    # shielded inclusion N+2
    finality_lag_blocks: int = 3               # engine acts on finalized heads; added to fill timing in sim
    shield_miss_ppm: int = 11_000              # 1.1% non-decrypt (swept 0.5%-3%)
    buy_tx_fee_rao: int = 1_028_000            # Proxy(add_stake_limit) 933,081 + carrier ~94,560
    sell_tx_fee_rao: int = 837_000             # Proxy(remove_stake_limit) 742,666 + carrier
    move_tx_fee_rao: int = 1_000_000           # same-subnet move_stake (UNMEASURED; verify on test.finney)
    rotate_tx_fee_rao: int = 1_250_000         # Proxy(swap_stake_limit) measured; move_stake_limit UNMEASURED
    carrier_fee_rao: int = 98_000              # charged on a miss only if the carrier was included; sim books it on every
                                               # injected miss (the measured 1.1% were included-but-undecrypted carriers)
    impact_half_life_blocks: int | None = 0    # 0 = healed/temporary impact (HEADLINE, gating); None = permanent (optimistic)
    fail_inject_ppm: int = 0                   # extra random inner-call failures (stress)
    n_delegates: int = 3                       # delegates modelled in sim/paper; must equal the live delegate count


@dataclass(frozen=True, slots=True)
class RiskCfg:
    # --- prune engine
    unwind_exec_blocks: int = 5                # L_exec per attempt = finality lag 3 + N+2 latency 2 (outcome final once
                                               # N+2 is finalized); U = L_exec * (1 + retries) = 15
    unwind_retries: int = 2
    margin_a_blocks: int = 300                 # M_A (FT1-calibrated; range 60-1,200)
    d_stress_ppm: Ppm = Ppm(500_000)           # Tier A stressed spot = (1 - 0.5) * spot
    k_bottom: int = 3                          # backstop rank
    r_backstop_ppm: Ppm = Ppm(1_200_000)       # backstop when cost/L <= 1.2
    tier_b_enabled: bool = False               # phase 2: after FT1 + FT2 pass
    h_b_blocks: int = 7_200
    pi_b_ppm: Ppm = Ppm(7_500)                 # Tier B: P_prune_24h * (1 - R) >= 0.75% of position
    mc_paths: int = 2_000
    entry_min_rank: int = 6                    # non-immune entry floor (prune_rank >= 6)
    entry_min_rho_ppm: Ppm = Ppm(1_500_000)    # EMA >= 1.5 x target EMA
    pi_entry_7d_ppm: Ppm = Ppm(10_000)         # once MC is enabled
    r_default_ppm: Ppm = Ppm(350_000)
    # --- emission, burn, launch age (single source for all sleeves)
    emission_ban_blocks: int = 100_800         # 14 d after a disable
    reenable_wait_blocks: int = 360
    burn_entry_max_ppm: Ppm = Ppm(500_000)
    burn_exit_ppm: Ppm = Ppm(900_000)          # NORMAL exit when MinerBurned >= 0.9 for 2 epochs
    min_since_start_blocks: int = 100_800      # 14 d (EMA 99.7% warm)
    min_age_reg_blocks: int = 216_000          # 30 d (launch phase belongs to LCW only)
    launch_stop_before_immunity_end: int = 144_000   # LCW hard stop at NetworkImmunityPeriod - this (read live)
    # --- liquidity and size
    s_exit_entry_ppm: Ppm = Ppm(15_000)        # V_cap = T_st * s / (1 - s)
    s_exit_hold_max_ppm: Ppm = Ppm(25_000)
    d_t_ppm: Ppm = Ppm(200_000)                # pool stress haircut
    s_urgent_ppm: Ppm = Ppm(30_000)
    nu_max_ppm: Ppm = Ppm(150_000)             # per-subnet share of NAV_liq
    g_max_ppm: Ppm = Ppm(800_000)              # gross alpha <= 80% of NAV_liq
    n_max: int = 12
    v_min_rao: Rao = Rao(500_000_000)          # 0.5 TAO min order
    remainder_min_rao: Rao = Rao(500_000_000)  # max(0.05 TAO, V_MIN)
    band_ppm: Ppm = Ppm(200_000)               # no-trade band
    ladder_bucket_ppm: Ppm = Ppm(150_000)      # sum over prune_rank <= 15
    owner_cluster_ppm: Ppm = Ppm(200_000)
    young_bucket_ppm: Ppm = Ppm(100_000)       # since_start < 30 d
    exit_budget_ppm: Ppm = Ppm(20_000)         # sum ES*V <= 2% NAV_liq
    escrow_max_ppm: Ppm = Ppm(500_000)         # E/x
    t_min_pool_rao: Rao = Rao(200 * 10**9)
    fee_rate_max: int = 330
    gate_haircuts_active: bool = False         # m_gate / m_trd monitoring-only until FT4 passes
    min_free_real_rao: Rao = Rao(50_000_000)   # MIN_FREE_REAL: 0.05 TAO never spent on buys; must be >= existential deposit
    # --- owner
    owner_cooldown_blocks: int = 7_200
    owner_unstake_frac_ppm: Ppm = Ppm(20_000)  # owner coldkey unstake > 2% of SubnetTAO
    owner_liquid_max_ppm: Ppm = Ppm(100_000)   # m_owner = 0.5 on V_cap when owner_liquid_frac >= 10%
    owner_haircut_active: bool = False         # m_owner monitoring-only until an FT5-style ablation passes
    # --- validator router
    take_max_ppm: Ppm = Ppm(50_000)
    q_max_ppm: Ppm = Ppm(50_000)               # our stake <= 5% of TotalHotkeyAlpha(h, n)
    permit_rank_frac_ppm: Ppm = Ppm(800_000)
    k_epochs: int = 20
    switch_min_gain_ppm_day: int = 200         # 0.02 %/day
    # --- planner
    beta_entry_floor_ppm: Ppm = Ppm(1_000)
    beta_entry_cap_ppm: Ppm = Ppm(20_000)
    beta_exit_floor_ppm: Ppm = Ppm(2_500)
    beta_exit_cap_ppm: Ppm = Ppm(50_000)
    k_chase: int = 2
    chi_max_ppm: Ppm = Ppm(15_000)
    finality_shield_pause_blocks: int = 5
    # --- modes and kill switches (DD/daily thresholds are re-derived by bootstrap: <= 5% false CAUTION days)
    stall_warn_s: int = 36
    stall_halt_s: int = 120
    finality_caution_blocks: int = 30
    stale_prune_blocks: int = 25
    dd_soft_ppm: Ppm = Ppm(150_000)
    dd_hard_ppm: Ppm = Ppm(250_000)
    daily_loss_ppm: Ppm = Ppm(80_000)
    fail_burst_netuid: int = 3                 # per 600 blocks -> 7,200-block subnet cooldown (per-block outcomes only)
    fail_burst_global: int = 5                 # per 600 blocks -> 300-block CAUTION (per-block outcomes only)
    fee_float_alert_rao: Rao = Rao(250_000_000)     # live fee float < 0.25 TAO -> alert
    fee_float_caution_rao: Rao = Rao(150_000_000)   # < 0.15 TAO -> CAUTION
    fee_float_exits_rao: Rao = Rao(50_000_000)      # < 0.05 TAO -> EXITS_ONLY (alpha-fee trap)
    spec_burn_in_blocks: int = 100_800         # halve budgets 14 d after a spec with regimes.touches_econ, or after a
                                               # post-spec parity breach (section 3.10 step 2)
    allow_emergency_exits_when_frozen: bool = True   # Tier A exits with tight fill-or-kill limits even on key alarm
    regime_throttle_active: bool = False       # the ONE market-regime throttle: monitoring-only until FT-R1 passes


@dataclass(frozen=True, slots=True)
class SleeveCfg:
    strategy: StrategyId
    stage: Stage
    budget_ppm: Ppm                            # share of G_MAX_EFF * NAV_liq; funded by stage (section 3.10)
    params: Mapping[str, object] = field(default_factory=dict)   # validated into the strategy's own Params


@dataclass(frozen=True, slots=True)
class BookCfg:
    book: BookId
    capital_rao: Rao                           # REQUIRED explicit value; capital is unknown and configurable
    fee_float_rao: Rao
    sleeves: tuple[SleeveCfg, ...]
    risk: RiskCfg = RiskCfg()
    exec: ExecCfg = ExecCfg()
    dereg_model: str = "formula"               # "formula" | "fixed:350000" | "fixed:650000" (ppm of spot)


@dataclass(frozen=True, slots=True)
class RpcCfg:
    head_endpoints: tuple[str, ...]            # wss://, in priority order
    archive_endpoints: tuple[str, ...]         # https:// JSON-RPC
    rate_per_s: float = 3.0
    burst: int = 3
    max_concurrency: int = 3
    keys_per_call: int = 2_000
    timeout_s: float = 30.0


@dataclass(frozen=True, slots=True)
class LiveCfg:
    enabled: bool = False                      # lock 1 of 4
    mode: str = "plan_only"                    # "plan_only" | "submit"  (lock 2: config confirmation)
    network: str = "test"                      # "test" | "finney"
    real_coldkey_ss58: str = ""
    delegate_wallets: tuple[str, ...] = ()     # Staking-proxy delegates (2-3)
    sleeves: tuple[StrategyId, ...] = ()       # only LIVE_ELIGIBLE sleeves the user lists here
    allowed_netuids: tuple[int, ...] = ()      # empty = overlay universe; buys only. Sell/move policies use
                                               # allowed_netuids + held netuids, so an edit never strands a position
    # Caps below bound BUYS ONLY (section 9.6). Sells are bounded only by the held position and same-subnet moves by
    # the position on the origin hotkey; a risk exit (urgency >= URGENT) is never VENUE_REJECTed for a cap.
    max_order_tao: float = 1.0                 # per buy; also the buy Policy's max_spend_tao
    max_daily_turnover_tao: float = 5.0        # buy TAO per 7,200 blocks (sells and moves not counted)
    max_position_tao: float = 5.0              # a buy may not take a position's executable value above this
    max_fee_tao: float = 0.005
    min_fee_float_tao: float = 0.15            # preflight floor (== RiskCfg.fee_float_caution_rao)
    max_ops_balance_tao: float = 0.5           # preflight: each delegate's free balance <= this (a fee buffer only)
    accepted_specs: tuple[int, ...] = ()       # submit refuses on any other spec_version (UNARMED, section 9.3)
    risk_exits_when_unarmed: bool = False      # user sets it explicitly in live.example.toml: while UNARMED (arm token
                                               # expired or spec not accepted), allow EMERGENCY/URGENT full sells only,
                                               # and only if V2, V3, V6 pass on the current spec (section 9.3)


@dataclass(frozen=True, slots=True)
class RunCfg:
    run_id: str
    mode: RunMode
    books: tuple[BookCfg, ...]
    rpc: RpcCfg
    live: LiveCfg = LiveCfg()
    data_dir: str = "data"
    cadence_blocks: int = 60                   # backtest stride / paper FULL-plan cadence
    seed: int = 0                              # stochastic pieces are seeded from (seed, block_hash)
```

### 5.10 `taotrader/core/protocols.py`: the seams
```python
"""taotrader/core/protocols.py - the seams. Everything a WP implements in parallel is typed here.

Rule: the engine/strategies/risk/portfolio packages are pure (no I/O, no clock, no randomness, no env);
chain/data/venues/live/ops are the imperative shell. import-linter enforces the direction.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from .config import BookCfg, RiskCfg, SleeveCfg
from .events import ChainEvent, ChainEventKind, HealthObs, JournalEvent
from .orders import Fill, OrderIntent, OrderRecord, Resolution, Urgency, VenueCaps
from .portfolio import Portfolio
from .signals import RiskDecision, StrategyOutput, TargetBook
from .state import ChainSnapshot, ReadPlan
from .units import (
    AlphaRao, Block, BlockHash, BookId, Hotkey, LogicalTime, Mode, NetUid, Ppm, PpmPerDay, PriceRao, Rao, Stage,
    StrategyId, SubnetKey,
)
from .views import FeatureFrame


# ------------------------------------------------------------------ data
class SnapshotStore(Protocol):
    """Immutable decoded snapshots (lake + live hot buffer). Never returns data after `clock`."""
    clock: Block                                         # set by the Runner before each tick

    def at(self, block: Block) -> ChainSnapshot: ...     # exact block or KeyError
    def at_or_before(self, block: Block) -> ChainSnapshot: ...
    def window(self, until: Block, span_blocks: int) -> Sequence[ChainSnapshot]: ...   # LookaheadError if until > clock


@dataclass(frozen=True, slots=True)
class SourceItem:
    snapshot: ChainSnapshot
    health: HealthObs


class DataSource(Protocol):
    """ParquetReplay (backtest), LiveChainFeed (paper/live, finalized heads), JournalSource (recovery)."""
    cadence_blocks: int                                  # data-resolution contract (Strategy.min_cadence_blocks)
    store: SnapshotStore

    def stream(self, after: Block | None) -> AsyncIterator[SourceItem]: ...   # strictly after the last journaled block
    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SwapSim:
    """SimSwapResult (48 bytes, spec >= 391). All-zero == failure."""
    tao_amount: int
    alpha_amount: int
    tao_fee: int
    alpha_fee: int
    tao_slippage: int
    alpha_slippage: int


class ChainReader(Protocol):
    """WP1. Windows-native JSON-RPC reader; every read is pinned to a block hash."""
    async def block_hash(self, block: Block) -> BlockHash: ...
    async def finalized_head(self) -> tuple[Block, BlockHash]: ...
    async def snapshot(self, block: Block, block_hash: BlockHash, plan: ReadPlan,
                       prev: ChainSnapshot | None, tracked: Sequence[tuple[SubnetKey, Hotkey]]) -> ChainSnapshot: ...
    async def dividend_keys(self, netuid: NetUid, block_hash: BlockHash) -> tuple[Hotkey, ...]: ...
    async def sim_swap_buy(self, netuid: NetUid, tao_rao: int, block_hash: BlockHash) -> SwapSim: ...
    async def sim_swap_sell(self, netuid: NetUid, alpha_rao: int, block_hash: BlockHash) -> SwapSim: ...
    async def prices_all(self, block_hash: BlockHash) -> dict[int, int]: ...       # netuid -> rao/alpha
    async def subnet_to_prune(self, block_hash: BlockHash) -> NetUid | None: ...
    async def registration_cost(self, block_hash: BlockHash) -> Rao: ...
    async def escrow_by_subnet(self, block_hash: BlockHash) -> dict[int, AlphaRao]: ...
    async def spec_version(self, block_hash: BlockHash) -> tuple[int, int]: ...    # (spec, tx_version)


@dataclass(frozen=True, slots=True)
class JournalRecord:
    seq: int
    batch: int                     # seq of the first record of the atomic batch
    time: LogicalTime
    book: BookId                   # "" for run-level records (snapshot, chain events, operator, config)
    kind: str
    version: int                   # the event class's VERSION (upcasters key on it)
    payload: bytes                 # canonical JSON (core.codec)
    idem: str | None
    prev_hash: bytes
    hash: bytes                    # blake2b-256(prev_hash || block|phase|sub|book|kind|version|payload) (= section 7.2)


class Journal(Protocol):
    """WP3. Append-only, hash-chained, atomic batches (SQLite WAL, synchronous=FULL in paper/live)."""
    def append_batch(self, items: Sequence[tuple[LogicalTime, BookId, JournalEvent]]) -> list[JournalRecord]: ...
    def read(self, from_seq: int = 1) -> Iterator[JournalRecord]: ...
    def has_idem(self, key: str) -> bool: ...
    def head(self) -> tuple[int, bytes]: ...
    def verify_chain(self) -> int: ...       # returns records verified; raises on a broken chain


# ------------------------------------------------------------------ features
class FeatureEngine(Protocol):
    """WP5. Deterministic function of the snapshot sequence and of own_fill_blocks; rebuilt on recovery by
    re-ingesting the lake window. Book-INDEPENDENT only: per-book choices live in BookView / WP8.
    Constructed with a CalibrationProvider (protocol/calibration.py) for the hazard and kappa_p used in PruneView."""
    warm: bool

    def update(self, raw: ChainSnapshot, events: Sequence[ChainEvent],
               own_fill_blocks: frozenset[Block] = frozenset()) -> FeatureFrame: ...
        # own_fill_blocks: union over all books of blocks with own fills (Runner-supplied, journal-derived);
        # those blocks are excluded from the beta samples (section 3.12)
    def state_digest(self) -> str: ...


# ------------------------------------------------------------------ decisions
@dataclass(frozen=True, slots=True)
class SleeveStats:
    """Un-netted stand-alone statistics of one sleeve (section 3.11 kill rules). Maintained by engine.reducer
    from DecisionTrace.sleeve_nav, fills and SleeveTransfer; references come from SleeveCfg.params."""
    strategy: StrategyId
    state: str                                   # "ACTIVE" | "REDUCED" | "SUSPENDED" (sleeve kill state)
    dd_ppm: Ppm                                  # un-netted drawdown from peak
    cost_ratio_20_ppm: Ppm                       # realised / modelled cost over the last 20 trades (1e6 = parity)
    turnover_ratio_ppm: Ppm                      # trailing 30-d turnover / backtest reference
    mean_45d_ppm_day: PpmPerDay | None           # trailing 45-d mean daily un-netted return; None before 45 d
    mean_45d_p5_ppm_day: PpmPerDay | None        # 5th percentile of its backtest bootstrap (preregistered reference)
    days_in_state: int                           # re-promotion clock


@dataclass(frozen=True, slots=True)
class RouterState:
    """Per-book YieldRouter memory (WP8 risk/router.py, section 3.8). Journaled in DecisionTrace.memories under
    the pseudo-id "risk.router" and folded back by the reducer."""
    choice: tuple[tuple[SubnetKey, Hotkey], ...] = ()              # current hotkey per subnet, sorted by key
    fail_epochs: tuple[tuple[SubnetKey, int], ...] = ()            # consecutive epochs the current hotkey failed filters
    beat_epochs: tuple[tuple[SubnetKey, Hotkey, int], ...] = ()    # consecutive epochs this challenger beat the current

    def hotkey(self, key: SubnetKey) -> Hotkey | None:
        for k, h in self.choice:
            if k == key:
                return h
        return None


@dataclass(frozen=True, slots=True)
class BookView:
    """Book-specific execution and risk history: a frozen, bounded projection of engine.reducer's EngineState
    (WP7). Everything the strategies, router, caps, allocator, overlay and planner need beyond the market."""
    orders: tuple[OrderRecord, ...]                       # open records + terminal records of the last 7,200 blocks
    recent_fills: tuple[Fill, ...]                        # last 1,800 blocks (cost ratios, own-fill bookkeeping)
    chase: tuple[tuple[SubnetKey, int, PriceRao], ...]    # open entry episodes: (key, re-quotes so far, decision spot)
    delegates_free: tuple[str, ...]                       # no carrier in flight and no nonce lock
    delegate_locked_until: tuple[tuple[str, Block], ...]  # (delegate, era_end + 2) after a miss or while in flight
    fail_counts_600: tuple[tuple[NetUid, int], ...]       # terminal failures per netuid, last 600 blocks, exact_block only
    fail_count_600_book: int
    cooldowns: tuple[tuple[SubnetKey, str, Block], ...]   # (key, rule, until): section 3.2 H cooldowns, 3.4 entry bans
    entries_halted_until: Block | None                    # wave halt, fail-burst CAUTION, ...
    recent_forced_exits: tuple[tuple[Block, SubnetKey, str, Urgency], ...]   # last 21,600 blocks (carry C-U9)
    nav_liq_daily: tuple[tuple[Block, Rao], ...]          # one NAV_liq sample per 7,200-block day, >= 45 d (DD30, daily loss)
    sleeve_stats: tuple[SleeveStats, ...]
    router: RouterState
    dissolving: tuple[SubnetKey, ...] = ()                # held generations removed on chain, awaiting DeregSettled


@dataclass(frozen=True, slots=True)
class TickContext:
    block: Block
    raw: ChainSnapshot                 # the market as it is: features and signals come from here
    view: ChainSnapshot                # raw + this book's own footprint (sim/paper); == raw live. Sizing/quotes/marks use it
    prev: ChainSnapshot | None
    events: tuple[ChainEvent, ...]     # derived this tick
    frame: FeatureFrame                # book-independent
    portfolio: Portfolio
    nav_liq: Rao                       # cash + sum of one-shot sim_sell value of every position on `view`
    sleeve: SleeveCfg                  # the calling strategy's sleeve config and budget
    sleeve_value: Rao                  # current executable value of this sleeve's holdings
    mode: Mode
    store: SnapshotStore               # bounded history; cannot see beyond `block`
    book_view: BookView                # this book's orders, fills, delegates, cooldowns, NAV history, router memory


class Strategy(Protocol):
    """WP9. Pure: no clock, I/O, randomness or unsorted-set iteration. State lives in the returned Memory."""
    id: StrategyId
    decide_every_blocks: int
    wake_on: frozenset[ChainEventKind]
    min_cadence_blocks: int            # coarsest data cadence it can be honestly evaluated on
    valid_from_block: Block            # regime guard (e.g. 8,765,684 for gate-dependent logic)
    declares_dilution: bool            # True if its scores already net structural sell load

    def initial_memory(self) -> object: ...
    def on_tick(self, ctx: TickContext, memory: object) -> StrategyOutput: ...


@dataclass(frozen=True, slots=True)
class RiskContext:
    tick: TickContext                  # sleeve field is the book-level pseudo-sleeve
    cfg: RiskCfg
    book: BookCfg
    stages: tuple[tuple[StrategyId, Stage], ...]
    halted_by_operator: bool
    orphans: int                       # unknown-order facts in quarantine (>0 halts entries)
    burn_in_until: Block | None        # post-spec burn-in end (reducer: SPEC_CHANGED with regimes.touches_econ, or a
                                       # post-spec ModelDriftObserved parity breach; section 3.10 step 2)


@runtime_checkable
class RiskOverlay(Protocol):
    """WP8. Final authority. Monotone: may lower targets, add forced exits, veto entries, raise the mode.
    Constructed with a CalibrationProvider (kappa_p, R, Tier B jumps as-of the decision block)."""
    def review(self, proposal: TargetBook, ctx: RiskContext) -> RiskDecision: ...


# ------------------------------------------------------------------ execution
class ExecutionVenue(Protocol):
    """WP6 (sim, paper) and WP11 (live). Venues hold NO hidden state: everything needed after a restart is
    rebuilt from journaled events via observe(). submit() must be idempotent on (order_id, attempt)."""
    caps: VenueCaps

    def mark_to(self, raw: ChainSnapshot) -> ChainSnapshot: ...      # sim/paper: + own-impact overlay; live: identity
    async def reserve(self, intent: OrderIntent, now: ChainSnapshot) -> tuple[str, int | None, Block | None]: ...
        # (delegate, carrier nonce, era_end) for SubmitStarted; sends nothing. Live: a free delegate, its pool-aware
        # system_accountNextIndex read immediately before SubmitStarted is journaled, finalized anchor + 8 (16) + 2.
        # Sim/paper: ("sim<i>", None, deterministic era_end)
    async def submit(self, intent: OrderIntent, now: ChainSnapshot) -> JournalEvent: ...
        # returns VenueAck, OrderFailed(VENUE_REJECT ...) or SubmitUnknown (live: the SDK used a nonce other than the
        # reserved one); raising => runner journals SubmitUnknown
    async def advance(self, view: ChainSnapshot) -> JournalEvent | None: ...
        # next due FillReported / OrderFailed / CarrierFeeSettled (ONE per call; the runner commits it, re-marks,
        # and calls again)
    async def resolve(self, intent: OrderIntent, now: ChainSnapshot) -> tuple[Resolution, tuple[JournalEvent, ...]]: ...
    def observe(self, ev: JournalEvent) -> None: ...                 # called for EVERY journaled event, incl. recovery


# ------------------------------------------------------------------ pure pipeline functions (signatures)
# DECIDE order (section 4.4): strategies -> router -> caps -> allocator -> overlay.review -> planner.
# After the router, the Engine passes ctx' = replace(ctx, book_view=replace(ctx.book_view, router=new_state)).

RouterFn = Callable[[TickContext, RiskCfg], RouterState]
"""WP8 risk/router.py: per-book hotkey choice from frame.feats[k].router_candidates, the book's positions and
ctx.book_view.router (Q_MAX against own shares, 2-epoch hysteresis, switch triggers). Pure."""

CapsFn = Callable[[TickContext, RiskCfg], dict[SubnetKey, Rao]]
"""WP8 risk/liquidity.py: per-subnet V_cap of section 3.5 (T_st, s, m_esc, active haircuts, NU_MAX). Pure.
Runs before the allocator; its result is the allocator's `caps`."""


class Allocator(Protocol):
    """WP8 portfolio/allocator.py: sleeves -> per-subnet aggregate targets (sum-then-cap, netting transfers,
    hotkeys from ctx.book_view.router)."""
    def __call__(self, signals: Sequence[tuple[SleeveCfg, StrategyOutput]], ctx: TickContext,
                 caps: dict[SubnetKey, Rao]) -> TargetBook: ...


class Planner(Protocol):
    """WP8 portfolio/planner.py: TargetBook -> ordered OrderIntents (limits, dust, priority, one in flight per netuid).
    Attempts, chase state, delegates, own fills and failure counts come from ctx.book_view; `inflight` is the set of
    keys with a non-terminal order in ctx.book_view.orders."""
    def __call__(self, decision: RiskDecision, ctx: TickContext, inflight: frozenset[SubnetKey],
                 run_id: str, book: BookId) -> tuple[OrderIntent, ...]: ...


class ShareValue(Protocol):
    def __call__(self, key: SubnetKey, hotkey: Hotkey, shares: Decimal) -> AlphaRao: ...
```

### 5.11 `taotrader/core/codec.py and core/errors.py`: canonical codec contract, errors
```python
"""taotrader/core/codec.py - canonical, type-hint-driven JSON codec and digests (WP0 implements fully).

Rules (property-tested):
- int stays int (arbitrary size); bool stays bool; None -> null.
- Decimal -> exact text via format(d, "f") with trailing zeros stripped. NEVER Decimal.normalize(): it rounds
  to the 28-digit context precision and silently corrupts share counts.
- Enums by value; tuples/lists -> arrays; frozenset -> sorted array; Mapping -> object with sorted keys.
- dataclasses -> objects with sorted field names (fields with metadata {"codec": False} or leading "_" skipped).
- bytes -> "0x"-hex. float allowed only in non-money fields and must be finite (allow_nan=False).
- decode(cls, data) rebuilds by type hints (NewType-aware); unknown KIND or a newer VERSION raises.
"""
from __future__ import annotations

import hashlib
from typing import Any, TypeVar

T = TypeVar("T")


def encode(obj: Any) -> Any:
    raise NotImplementedError


def decode(cls: type[T], data: Any) -> T:
    raise NotImplementedError


def canonical_bytes(obj: Any) -> bytes:
    """json.dumps(encode(obj), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()"""
    raise NotImplementedError


def digest(obj: Any, size: int = 16) -> str:
    return hashlib.blake2b(canonical_bytes(obj), digest_size=size).hexdigest()
```
```python
"""taotrader/core/errors.py"""
from __future__ import annotations


class LookaheadError(Exception):
    """A strategy/feature asked the SnapshotStore for data after the engine clock."""


class ReplayDivergence(Exception):
    """Recovery re-ran decide() and got different outputs than the journal (code/config drift or nondeterminism)."""


class DecodeError(Exception):
    """Storage bytes could not be decoded; the whole snapshot is rejected (all-or-nothing)."""


class GateError(Exception):
    """Live gating refused (missing lock, bad proxy type, spec not accepted, ...)."""


class DataContractError(Exception):
    """Strategy needs finer data than the DataSource provides, or runs before its valid_from_block."""
```

### 5.12 Pure-function interfaces owned by WP2 (`taotrader/protocol/*`)
Signatures and formulas are binding; bodies are WP2's. `models.py` below is a single listing of the public API of `emission.py`, `ema.py`, `prune.py`, `yield_model.py`, `sellload.py`, `calibration.py`, `regimes.py` and `derive.py`; WP2 splits it into those modules (same names, same signatures).

`taotrader/protocol/amm.py`
```python
"""taotrader/protocol/amm.py - era-correct swap math (WP2). Integer rao in/out; Decimal (core.fixed.DEC) inside.

Exact formulas (brief 2.3-2.7):
  BUY : fee = floor(tao_in*f/65535); dy = tao_in - fee; alpha_out = x*(1-(y/(y+dy))**(w_q/w_b))  [w=0.5: x*dy//(y+dy)]
  SELL: fee_a = floor(a*f/65535); dx = a - fee_a; tao_out = y*(1-(x/(x+dx))**(w_b/w_q));
        then fee_a is sold fee-free into the same pool: tao_fee = y1*(1-(x1/(x1+fee_a))**(w_b/w_q)); pool absorbs all of a.
  Guards: MinimumReserve 1,000,000 rao; input <= 1000*reserve; gross >= 0.002 TAO + fee and dy >= 0.002 TAO;
          partial sells need tao_out >= 0.002 TAO; full exits exempt.
  Limits: buy max net dy = y*((p'/p)**w_b - 1); sell max net dx = x*((p/p')**w_q - 1); gross = net*65535/(65535-f).
          Strict: buy needs spot < limit, sell needs spot > limit, else PriceLimitExceeded.
  Era B: PoolKind.CP_V3_VIRTUAL uses px_* = (L*sqrtP, L/sqrtP) at w = 0.5 (exact absent tick crossings).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..core.orders import FailReason
from ..core.state import PoolState
from ..core.units import AlphaRao, PriceRao, Rao


class SwapError(Exception):
    def __init__(self, reason: FailReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SwapQuote:
    amount_in: int                # gross input (rao for buys, alpha rao for sells)
    fee: int                      # input-side swap fee
    amount_out: int               # alpha out (buy) / TAO out (sell)
    author_fee_tao: int           # sells: TAO paid to the author for the fee alpha
    d_tao: int                    # real AND pricing reserve change (own-impact overlay input)
    d_alpha: int
    spot_before: PriceRao
    marginal_after: PriceRao      # pool spot after the fill (what the chain's limit bounds)
    shortfall_ppm: int            # 1 - executed / spot, incl. fee


class ImpactBound(StrEnum):
    TEMPORARY = "temporary"       # other flow heals our footprint: impact paid on BOTH legs (headline, gating)
    PERSISTENT = "persistent"     # our footprint persists: an immediate round trip costs only fees (optimistic bound)


def quote_buy(p: PoolState, tao_in: Rao) -> SwapQuote: ...
def quote_sell(p: PoolState, alpha_in: AlphaRao, *, partial_remaining: bool = False) -> SwapQuote: ...
def max_buy_to_limit(p: PoolState, limit: PriceRao) -> Rao: ...          # raises SwapError(PRICE_LIMIT_EXCEEDED) if limit <= spot
def max_sell_to_limit(p: PoolState, limit: PriceRao) -> AlphaRao: ...    # raises if limit >= spot
def marginal_after_buy(p: PoolState, tao_in: Rao) -> PriceRao: ...      # spot*(1 + dy/y)**(1/w_b)
def marginal_after_sell(p: PoolState, alpha_in: AlphaRao) -> PriceRao: ...   # spot*(1 + dx/x)**(-1/w_q)
def v_max(pool_tao: Rao, slip_ppm: int) -> Rao: ...                     # T*s/(1-s): exit-slippage budget
def liq_value(p: PoolState, alpha: AlphaRao) -> Rao: ...                # one-shot sell value; 0 if it would fail
def round_trip_cost_ppm(p: PoolState, size: Rao, bound: ImpactBound, tx_fees_rao: int) -> int:
    """TEMPORARY: buy on p, then sell the alpha on the ORIGINAL p (healed) -> ~2f + 2V/(T+V) + tx/V.
    PERSISTENT: sell into the post-buy pool -> ~2f + tx/V. Momentum's original gate used PERSISTENT by mistake."""
    ...
def v_star(p: PoolState, alpha_h_ppm: int, shrink_ppm: int = 1_000_000) -> Rao:
    """Impact-optimal size maximising alpha_h*V - 2f*V - 2V^2/T: V* = T*(alpha_h - 2f)/4, times shrink (lambda^-1)."""
    ...
```

`taotrader/protocol/models.py`
```python
"""taotrader/protocol/{emission,ema,prune,yield_model,sellload,regimes,fees,derive}.py - public API (WP2).

All functions are pure, read parameters from ChainGlobals/SubnetState (never hard-coded constants), and are
golden-tested against the brief's verified vectors (section 10).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..core.events import ChainEvent
from ..core.state import ChainGlobals, ChainSnapshot, SubnetState
from ..core.units import AlphaRao, Block, NetUid, Ppm, Rao, SubnetKey


# ---------------------------------------------------------------- emission.py
def block_emission_for_issuance(issuance_rao: int) -> Rao:
    """floor(1e9 * 2**(-floor(log2(1/(1 - I/21e15))))); 0 at/above the cap. Used for TAO and per-subnet alpha."""
    ...


def root_prop(glob: ChainGlobals, alpha_issuance: AlphaRao) -> Decimal:
    """rp = (SubnetTAO[0]*TaoWeight)/(SubnetTAO[0]*TaoWeight + alpha_issuance). Stored RootProp wins when present."""
    ...


@dataclass(frozen=True, slots=True)
class EmissionShare:
    key: SubnetKey
    b: Decimal                    # burn-adjusted EMA share (renormalised)
    keep: Decimal                 # g/b = 1/(1+(theta/b)^h)
    final: Decimal                # gated share among ENABLED subnets
    tao_per_block: Rao            # E_i
    tao_in_per_block: Rao         # min(E_i, rp*alpha_emission*spot)
    chain_buy_per_block: Rao      # E_i - tao_in (the only price-moving protocol bid)


def emission_vector(snap: ChainSnapshot, ema_override: dict[SubnetKey, Decimal] | None = None,
                    enabled_override: dict[SubnetKey, bool] | None = None,
                    refresh_theta: bool = False) -> dict[SubnetKey, EmissionShare]:
    """Exact replica of get_shares (rank mode): eligible = FirstEmissionBlockNumber set & SubtokenEnabled &
    NetworkRegistrationAllowed & non-root; s = EMA/sum; b = s*(1-min(MB,1)) renormalised (fallback s);
    theta = EmissionGateBar (or the Nth-largest positive b when refresh_theta or theta == 0; N = gate_rank);
    g = b/(1+(theta/b)^h); gate runs BEFORE disabled subnets are zeroed; renormalise over enabled;
    E_i = block_emission * final_i. Then the injection split per subnet."""
    ...


def parity_ok(rel_errors: Sequence[Decimal]) -> bool:
    """THE emission-parity gate (EmissionView.model_ok AND carry T2a). rel_errors = |E_model - E_obs|/E_obs over
    enabled subnets with E > 1 TAO/day, E_obs = trailing-300-block 7200*(SubnetTaoInEmission + SubnetExcessTao
    + reservoir delta). True iff median < 1% AND >= 90% of them are within 5%. Empty input -> False."""
    ...


# ---------------------------------------------------------------- ema.py
def ema_alpha(glob: ChainGlobals, s: SubnetState, block: Block) -> Decimal:
    """a = SubnetMovingAlpha * b/(b + EMAPriceHalvingBlocks), b = block - (FirstEmissionBlockNumber - 1).
    a = 0 if not emit-eligible (no start_call, SubtokenEnabled False, or NetworkRegistrationAllowed False: EMA frozen)."""
    ...


def ema_forecast(e0: Decimal, spot: Decimal, b0: int, dn: int, moving_alpha: Decimal, halving: int) -> Decimal:
    """Flat-spot closed form: S - (S - E0)*exp(-alpha*(dn - H*ln((H+b0+dn)/(H+b0)))), S = min(spot, 1)."""
    ...


def t_star_blocks(ema_k: Decimal, ema_bottom: Decimal, spot_k: Decimal, a_k: Decimal) -> int | None:
    """Blocks until EMA_k <= bottom EMA if k's spot holds at spot_k: ln((E_k - s)/(E_1 - s)) / -ln(1-a).
    0 if already at/below; None if spot_k >= E_1 (never crosses) or a == 0."""
    ...


# ---------------------------------------------------------------- prune.py
def ladder(snap: ChainSnapshot) -> tuple[SubnetKey, ...]:
    """Non-immune (block >= reg_at + NetworkImmunityPeriod), non-root, ascending (moving_price, reg_at). [0] = target."""
    ...


def prune_possible(glob: ChainGlobals) -> bool:
    """n_nonroot_networks + cleanup_queue_len >= SubnetLimit; otherwise a registration does not prune."""
    ...


def registration_cost(glob: ChainGlobals, block: Block) -> Rao:
    """max(NetworkMinLockCost, 2L - (L / I_eff)*(block - last_reg_block)), I_eff = NetworkLockReductionInterval*block_emission/1e9."""
    ...


def cost_ratio(glob: ChainGlobals, block: Block) -> Decimal:
    """r = registration_cost / NetworkLastLockCost (1.75 at window opening, 1.0 after ~8 days)."""
    ...


@dataclass(frozen=True, slots=True)
class HazardModel:
    cdf_by_r: tuple[tuple[Decimal, Decimal], ...]   # (r, F) points, r descending; see section 3.3
    p_open: Decimal                                  # mass at window opening (0.0625; 0.5 in a hot market)
    n0: int                                          # prior pseudo-count (4)
    lambda_floor_per_block: Decimal                  # ln2/7200 once r <= 1.045
    valid: bool                                      # False -> fall back to window rule + constant floor hazard


def p_registration(glob: ChainGlobals, model: HazardModel, block: Block, horizon_blocks: int) -> Decimal:
    """P(a registration lands in (block, block+horizon] | none since last_reg_block). 0 inside the rate limit
    or when not prune_possible."""
    ...


@dataclass(frozen=True, slots=True)
class RegistrationRow:
    """One registration (lake `registration` table)."""
    queued_block: Block
    victim_netuid: NetUid | None
    cost_ratio: Decimal                 # r = cost / NetworkLastLockCost at the registration block
    blocks_since_prev: int


def fit_hazard(regs: Sequence[RegistrationRow], asof: Block, prior: HazardModel) -> HazardModel:
    """As-of refit of the section 3.3 CDF: uses ONLY rows with queued_block < asof (asserted), smoothed toward
    `prior` with n0 pseudo-counts; returns `prior` unchanged with fewer than 8 rows. Hot-market and validity rules
    as in section 3.3. Deterministic."""
    ...


def recovery_ratio(s: SubnetState, glob: ChainGlobals, r_default: Decimal) -> Decimal:
    """Dissolution payout per alpha / spot. Baskets sold first: T_after = T*(x/(x+E))**(w_b/w_q);
    denom = (S - E) + P + ((x + E) if reg_at > TaoInRefundDeploymentBlock else 0), S = TotalAlphaStaked
    (fallback alpha_out - protocol_alpha); clamp to [0,1]. SN92 golden: ~0.368."""
    ...


# ---------------------------------------------------------------- yield_model.py
def a_earn(s: SubnetState) -> AlphaRao:
    """Sum of TotalHotkeyAlpha over tracked hotkeys with earns=True (flag NO_YIELD_IDX if coverage < 95%)."""
    ...


def closed_form_yield_gross(s: SubnetState, glob: ChainGlobals) -> Decimal:
    """Per alpha per day: 7200*alpha_out_emission*(1 - c_o)*0.5*(1 - rp)/A_earn, c_o = SubnetOwnerCut/65535
    (if OwnerCutEnabled). = 2952*(1-rp)/A_earn today. Golden: SN70 0.557%, SN92 0.448%, SN64 0.0918%."""
    ...


def a_earn_growth_per_day(s: SubnetState, glob: ChainGlobals, root_flag: bool) -> Decimal:
    """Deterministic A_earn growth in alpha/day: nominator compounding 7200*ae*(1-c_o)*0.5*(1-rp)
    + escrow deposits 7200*ae*(1-c_o)*0.5*rp while root_flag (sum EMA > 1). Net user flow is added by callers."""
    ...


# ---------------------------------------------------------------- sellload.py  (the ONE structural model)
@dataclass(frozen=True, slots=True)
class SellLoadParams:
    phi_owner_ppm: Ppm = Ppm(800_000)     # fraction of liquid owner cut sold (prior; measured from owner positions in T3)
    phi_miner_ppm: Ppm = Ppm(600_000)     # fraction of miner emission sold (prior)
    kappa_basket_ppm_day: Ppm = Ppm(20_000)   # basket claim release c ~ 2%/day of escrow stock


@dataclass(frozen=True, slots=True)
class SellLoad:
    owner_tao_day: Decimal
    miner_tao_day: Decimal
    basket_tao_day: Decimal               # post-v441: kappa*E*p; pre-v441: root dividends sold every block
    total_tao_day: Decimal
    sell_push_day: Decimal                # k_w * total / y
    cb_push_day: Decimal                  # k_w * chain_buy_day / y
    coverage: Decimal                     # chain_buy_day / total (0 if total == 0)


def sell_load(s: SubnetState, glob: ChainGlobals, share: EmissionShare, params: SellLoadParams,
              block: Block, root_flag: bool) -> SellLoad:
    """S = p*[phi_o*c_o*(1-AL)*AE + phi_m*0.5*(1-c_o)*(1-min(MB,1))*AE + basket], AE = 7200*SubnetAlphaOutEmission.
    basket = kappa_b*E (>= 8,765,684) else rp*0.5*(1-c_o)*AE*root_flag (root dividends sold per block pre-v441).
    (1-AL) here is the LIQUID fraction of the owner cut; owner-sale DETECTION (section 3.6) never applies it."""
    ...


# ---------------------------------------------------------------- calibration.py
@dataclass(frozen=True, slots=True)
class Calibration:
    """Every calibrated decision input, fit only on events with block < asof (section 8.10)."""
    asof: Block
    hazard: HazardModel                 # fit_hazard on registrations before asof
    kappa_p: Decimal                    # FT1 logistic on prunes before asof (prior 4)
    r_default: Decimal                  # FT10 outcome on dissolutions before asof: 0.35, or min(formula, 0.35) flag
    r_cap_formula: bool                 # True -> R = min(formula, r_default)
    tier_b_jump_p_day: Decimal          # Tier B jump probability per day (prior 0.03)
    tier_b_jump_size: Decimal           # ln(1 - 0.5) prior
    phi: SellLoadParams                 # T3-measured phi on data before asof (priors until measured)
    digest: str                         # journaled in DecisionTrace.calib_digest


class CalibrationProvider(Protocol):
    """Injected into the FeatureEngine and RiskOverlay constructors. Backtests: data.calibration.LakeCalibrationProvider
    (as-of fits from the lake registration and generation tables, cached per refit point). Paper/live: the same
    provider on recorded history, or FrozenCalibration (the preregistered section 3.3 values; allowed ONLY in
    paper and live)."""
    def asof(self, block: Block) -> Calibration: ...


# ---------------------------------------------------------------- regimes.py
@dataclass(frozen=True, slots=True)
class Regime:
    regime_id: str
    first_block: Block          # setCode block + 1
    last_block: Block | None
    note: str


REGIMES: tuple[Regime, ...] = ()        # filled by WP2 from section 8.6; ONE shared table


@dataclass(frozen=True, slots=True)
class SpecInfo:
    spec_version: int
    touches_econ: bool          # release changes emission, dividend or registration code (lead-maintained via ADR)
    note: str


SPECS: tuple[SpecInfo, ...] = ()        # one row per known spec; unknown spec -> touches_econ False until its ADR lands


def regime_at(block: Block) -> Regime: ...
def touches_econ(spec_version: int) -> bool: ...       # post-spec burn-in trigger (section 3.10 step 2)
def fee_rate_default(spec_version: int) -> int: ...
    # 196 for specs 290-292; 33 for later specs; era-A specs (< 290): the value WP4 measures (VERIFY; section 8.2),
    # with era-A results flagged Quality.DEFAULT_FILLED until it is verified


# ---------------------------------------------------------------- derive.py
def derive_events(prev: ChainSnapshot | None, cur: ChainSnapshot, large_flow_frac_ppm: Ppm = Ppm(20_000)
                  ) -> tuple[ChainEvent, ...]:
    """The ONLY chain-event source in v1 (section 4.2). Pure; sorted by (kind, key); idempotent on identical snapshots."""
    ...


def track_hotkeys(snap: ChainSnapshot, dividend_keys: dict[int, Sequence[str]],
                  held: Sequence[tuple[SubnetKey, str]], prev_tracked: Sequence[tuple[SubnetKey, str]] = (),
                  top_n: int = 5) -> tuple[tuple[SubnetKey, str], ...]:
    """Tracked set per subnet: every (key, hotkey) held or chosen by any book + top-N earning by TotalHotkeyAlpha
    + every take-0 earner + (owner_hotkey, netuid) for every subnet with owner_hotkey set. Sticky: every pair in
    prev_tracked whose generation is still in snap stays tracked until the generation ends. dividend_keys must be
    listed point-in-time at snap.block_hash (historical collection included)."""
    ...
```

### 5.13 Notes for implementers
1. **Positions hold shares, not alpha.** Value is `HotkeyIdx.value_of(shares)`. Nominator yield needs no special code: it shows up as a rising index and is journaled as `YieldAccrued`. Buys compute shares at the index of the fill snapshot, which matches the chain rule that stake added in block B does not earn B's drain.
2. **`TickContext` carries `raw` and `view`.** Features and signals read `raw`, the market. Sizing, quotes, limits and marks read `view`, the market plus our own footprint. A strategy therefore never mistakes its own impact for momentum.
3. **`Signal` is the float firewall.** Strategies call `to_ppm` exactly once per numeric field.
4. **Memories.** Each strategy defines a frozen `Memory` dataclass of primitives (for example per-key weak-cycle counters, entry blocks, peak executable values). It is codec-encodable and journaled in `DecisionTrace.memories`. A strategy must not keep state anywhere else.
5. **Venues hold no hidden state.** `SimVenue.observe` rebuilds its queue and footprint from `VenueAck` and `FillReported`. `LiveVenue.observe` rebuilds in-flight carriers from `SubmitStarted` and `VenueAck`, together with the `live_submissions` sidecar (§7.3).
6. **`ChainSnapshot.subnets` excludes root.** `ChainGlobals.root_tao` carries SubnetTAO[0].
7. **Account ids in core are 32-byte hex.** ss58 conversion happens only in `taotrader.live`.
8. **Book-dependent inputs come only from `TickContext.book_view`.** The reducer (WP7) maintains `EngineState` and projects a frozen `BookView` for each tick. WP5 features never see book state, and WP8/WP9 never keep book history of their own. A new book-dependent input means a new `BookView` field (ADR), not a cache inside a rule.
9. **Calibrated inputs are as-of.** Anything fitted on history (hazard, κ_p, R, Tier B jumps, φ) reaches decisions only through `CalibrationProvider.asof(block)`; its digest is journaled with every decision.

---

## 6. Chain reader (`taotrader.chain`, WP1): Windows-native, no bittensor

### 6.1 Endpoints and roles (config `[rpc]`; order = priority)
| Role | Endpoints | Use |
|---|---|---|
| head (WS) | own lite node if any → `wss://entrypoint-finney.opentensor.ai:443` → `wss://lite.chain.opentensor.ai:443` → `wss://lite.sub.latent.to:443` | finalized/new-head subscriptions, per-block reads (lite nodes keep ~300 blocks of state) |
| archive (HTTP) | keyed OnFinality (when the user adds a key: free 400k RU/day, ≤ 40 RU/s, 1 RU per call) → `https://bittensor-finney.api.onfinality.io/public` (measured 3.4 req/s sustained; trips at ~8 concurrent or > 25 req/s; batch arrays rejected) → `archive.chain.opentensor.ai` (−32004 historical budget; fallback only) | history, gap fill > 280 blocks, "State already discarded" re-routes |
| test | `wss://test.finney.opentensor.ai:443` | live-adapter smoke tests |

Every read is pinned to a block hash, never to "latest". A multi-call snapshot is therefore consistent, and a different provider returns the same bytes.

### 6.2 JSON-RPC methods used
| Method | Purpose |
|---|---|
| `chain_getBlockHash(n)`, `chain_getHeader(h)`, `chain_getFinalizedHead()`, `chain_getBlock(h)` | block identity; live reconciliation finds carrier and inner extrinsics |
| `chain_subscribeFinalizedHeads` / `chain_subscribeNewHeads` | engine input / venue timing and lag telemetry |
| `state_getRuntimeVersion(h)`, `state_subscribeRuntimeVersion` | spec_version, transaction_version |
| `state_queryStorageAt([keys], h)` | THE batch unit: one call with up to `keys_per_call` (default 2,000; measured OK at 2,332–2,732) |
| `state_queryStorage([keys], from_h, to_h)` | change-sets (per-block refinement, flow change-sets for the MR study) |
| `state_getKeysPaged(prefix, 1000, start_key, h)` | AlphaDividendsPerSubnet(n, ·) membership; NetworksAdded fallback |
| `state_getStorage(key, h)` | single reads (DissolveCleanupQueue, NextKey) |
| `state_call(method, hex_args, h)` | runtime APIs (§6.6) |
| `state_getMetadata(h)` | offline only: per-spec ValueQuery defaults (`chain/metadata.py`, needs the `collector` extra) |
| `system_health` | peer and sync sanity |

### 6.3 Storage-key construction (`chain/hashing.py`)
```python
import hashlib, xxhash

def twox128(b: bytes) -> bytes:                  # check: twox128(b"System").hex() == "26aa394eea5630e07c48ae0c9558cef7"
    return xxhash.xxh64_intdigest(b, seed=0).to_bytes(8, "little") + xxhash.xxh64_intdigest(b, seed=1).to_bytes(8, "little")

def twox64_concat(b: bytes) -> bytes:            # Swap pallet maps keyed by netuid
    return xxhash.xxh64_intdigest(b, seed=0).to_bytes(8, "little") + b

def blake2_128_concat(b: bytes) -> bytes:        # hotkey / coldkey keys
    return hashlib.blake2b(b, digest_size=16).digest() + b

def identity(b: bytes) -> bytes:                 # netuid u16 keys of SubtensorModule
    return b

def le16(n: int) -> bytes: return n.to_bytes(2, "little")
def prefix(pallet: str, item: str) -> bytes: return twox128(pallet.encode()) + twox128(item.encode())   # cached

# Map key = prefix ++ hasher1(k1) [++ hasher2(k2) [++ hasher3(k3)]]; value keys = prefix only.
k_subnet_tao   = lambda n: prefix("SubtensorModule", "SubnetTAO") + identity(le16(n))
k_balancer     = lambda n: prefix("Swap", "SwapBalancer") + twox64_concat(le16(n))
k_hk_alpha     = lambda hk32, n: prefix("SubtensorModule", "TotalHotkeyAlpha") + blake2_128_concat(hk32) + identity(le16(n))
k_alpha_divs   = lambda n, hk32: prefix("SubtensorModule", "AlphaDividendsPerSubnet") + identity(le16(n)) + blake2_128_concat(hk32)
k_last_reg     = prefix("SubtensorModule", "LastRateLimitedBlock") + b"\x02"   # RateLimitKey::NetworkLastRegistered (enum variant 2)
```
Hasher layouts not given in the brief are marked VERIFY in §6.5. `chain/items.py` asserts each layout against `state_getMetadata` of the live spec at startup (`taotrader verify-metadata`), and refuses to run on a mismatch.

### 6.4 Decoders (`chain/scale.py`; little-endian; exact)
```python
from decimal import Decimal
from taotrader.core.fixed import EXACT
_u = lambda b, n, s=False: int.from_bytes(b[:n], "little", signed=s)
d_bool   = lambda b: b[0] != 0
d_u8, d_u16, d_u32, d_u64 = (lambda b: _u(b, 1)), (lambda b: _u(b, 2)), (lambda b: _u(b, 4)), (lambda b: _u(b, 8))
d_u128   = lambda b: _u(b, 16)
d_i64    = lambda b: _u(b, 8, True)                                     # SubnetTaoFlow
d_i96f32 = lambda b: EXACT.divide(Decimal(_u(b, 16, True)), Decimal(2**32))   # SubnetMovingPrice, SubnetMovingAlpha
d_u96f32 = lambda b: EXACT.divide(Decimal(_u(b, 16)), Decimal(2**32))         # RootProp, MinerBurned
d_u64f64 = lambda b: EXACT.divide(Decimal(_u(b, 16)), Decimal(2**64))         # EmissionGateBar, AlphaSqrtPrice, V1 shares, FastMovingPrice
d_perquintill_raw = lambda b: _u(b, 8)                                  # SwapBalancer.quote (first field), keep the 1e18-scaled int
d_tao_weight = lambda b: EXACT.divide(Decimal(_u(b, 8)), Decimal(2**64 - 1))
d_safefloat  = lambda b: Decimal(f"{_u(b, 16)}E{_u(b[16:], 8, True)}")  # {mantissa u128, exponent i64} = m*10^e; string ctor is exact
d_account    = lambda b: "0x" + b[:32].hex()
def d_option(inner, width: int):                                        # OptionQuery stores raw T; ValueQuery<Option<T>> stores 0x00 / 0x01++T
    def f(b: bytes | None):
        if b is None or b == b"\x00": return None
        if len(b) == width: return inner(b)
        if len(b) == width + 1 and b[0] == 1: return inner(b[1:])
        raise DecodeError(f"option width {width}: {b.hex()}")
    return f
def d_blocknum(b):                                                      # u32 or u64 depending on the item; decode by length
    return _u(b, len(b))
def d_compact(b) -> tuple[int, int]:                                    # (value, bytes consumed)
    m = b[0] & 3
    if m == 0: return b[0] >> 2, 1
    if m == 1: return _u(b, 2) >> 2, 2
    if m == 2: return _u(b, 4) >> 2, 4
    n = (b[0] >> 2) + 4; return _u(b[1:], n), n + 1
def d_vec_len(b) -> int: return d_compact(b)[0]                        # DissolveCleanupQueue length
```
**Rules:**
- An absent key takes the default of **that block's runtime**: `metadata.defaults[spec][item]`, cached JSON. If the spec's defaults are not validated, the snapshot gets `Quality.DEFAULT_FILLED`.
- An undecodable value fails the whole snapshot (`DecodeError`). The snapshot is never partial: the block goes to `fetch_ledger` as INVALID with the raw bytes kept.
- Golden fixtures cover every decoder:
  - SubnetMovingAlpha raw 1,288,490 → 0.0003;
  - EmissionGateBar → 0.0082624;
  - TaoWeight → 0.18;
  - SafeFloat and Option round trips.

### 6.5 Storage item registry (`chain/items.py`; one row = key + decoder + default + plan)
Per subnet (n = 1 … `max_netuid`, default SubnetLimit + 16 = 144; the reader extends the range automatically if a key is present at the upper bound):
| Field | Pallet.Item | Key hasher | Decoder | Absent → | Exists from | Plan |
|---|---|---|---|---|---|---|
| added | SubtensorModule.NetworksAdded | Identity u16 | bool | False | — | HEAD |
| reg_at | NetworkRegisteredAt | Identity | u64 | 0 | — | HEAD |
| tao | SubnetTAO | Identity | u64 | 0 | — | HEAD |
| alpha_in | SubnetAlphaIn | Identity | u64 | 0 | — | HEAD |
| alpha_out | SubnetAlphaOut | Identity | u64 | 0 | — | FULL |
| protocol_alpha | SubnetProtocolAlpha | Identity | u64 | 0 | — | FULL |
| moving_price | SubnetMovingPrice | Identity | I96F32 | 0 | — | HEAD |
| fast_moving_price | SubnetFastMovingPrice | Identity | U64F64 (OptionQuery) | None | basket era | FULL |
| root_prop | RootProp | Identity | U96F32 | computed (§5.12 root_prop) | 7,135,420 | FULL |
| miner_burned | MinerBurned | Identity | U96F32 | 0 | 8,466,597 | FULL |
| emission_enabled | SubnetEmissionEnabled | Identity | bool | True | spec 411 (8,283,784) | HEAD |
| subtoken_enabled | SubtokenEnabled | Identity | bool | metadata default | — | HEAD |
| reg_allowed | NetworkRegistrationAllowed | Identity | bool | metadata default | — | HEAD |
| first_emission_block | FirstEmissionBlockNumber | Identity | Option<u64> (decode by length) | None | — | HEAD |
| tempo | Tempo | Identity | u16 | 360 | — | FULL |
| last_epoch_block | LastEpochBlock | Identity | u64 | 0 | — | HEAD |
| ema_halving_blocks | EMAPriceHalvingBlocks | Identity | u64 | 201,600 | — | FULL |
| tao_in_emission | SubnetTaoInEmission | Identity | u64 | 0 | — | FULL (HEAD when held) |
| excess_tao | SubnetExcessTao | Identity | u64 | 0 | 8,283,784 | FULL (HEAD when held) |
| alpha_out_emission / alpha_in_emission | SubnetAlphaOutEmission / SubnetAlphaInEmission | Identity | u64 | 0 | — | FULL |
| tao_flow_cum | SubnetTaoFlow | Identity | i64 | None | ~2025-11-04 (valid ≥ 8,466,531) | HEAD |
| volume_cum | SubnetVolume | Identity | u128 | None | — | FULL |
| owner_coldkey | SubnetOwner | Identity | AccountId32 | None | — | FULL |
| owner_hotkey | SubnetOwnerHotkey **(VERIFY name)** | Identity | AccountId32 | None | — | FULL |
| owner_cut_enabled | OwnerCutEnabled **(VERIFY map vs value)** | Identity | bool | True | — | FULL |
| owner_cut_autolock | OwnerCutAutoLockEnabled **(VERIFY)** | Identity | bool | False | — | FULL |
| total_alpha_staked | TotalAlphaStaked | Identity | u64 | fallback AlphaOut − ProtocolAlpha | spec 448 | FULL |
| max_allowed_validators | MaxAllowedValidators | Identity | u16 | metadata default | — | FULL |
| consensus_mode | per-subnet consensus-mode item **(VERIFY name, key and enum from spec-475 metadata)** | Identity | u8 enum index | metadata default | spec 475 | FULL |
| metagraph (LCW only) | `get_selective_metagraph` or Incentive/ValidatorPermit/Keys storage **(VERIFY)** → `MetagraphLite` | — | scalecodec / storage | None | — | FULL, only when `lcw.enabled` |
| w_quote_e18 | Swap.SwapBalancer | Twox64Concat u16 | first 8 bytes u64 | 5·10^17 | era C (8,486,594) | HEAD |
| fee_rate | Swap.FeeRate | Twox64Concat | u16 | `fee_rate_default(spec)` (33; 196 for specs 290–292; era A measured by WP4, **VERIFY**) | — | HEAD |
| reservoir_tao / reservoir_alpha | Swap.BalancerTaoReservoir / BalancerAlphaReservoir | Twox64Concat | u64 | 0 | era C | FULL |
| v3 sqrt price | Swap.AlphaSqrtPrice | Twox64Concat | U64F64 | — | era B only (≤ 8,486,593) | FULL |
| v3 liquidity | Swap.CurrentLiquidity | Twox64Concat | u64 | — | era B only | FULL |

Globals (plain value keys unless noted):

`SubtensorModule`:
- SubnetMovingAlpha (I96F32);
- EmissionGateBar (U64F64);
- EmissionBarRank (VERIFY int width; absent → 32);
- EmissionGateExponent (absent → 3);
- EmissionBarQuantile (logged only);
- TaoWeight (u64/u64::MAX);
- SubnetTAO[0] (Identity key 0);
- SubnetOwnerCut (u16 **global**; absent → 11,796);
- SubnetLimit, NetworkImmunityPeriod, NetworkRateLimit, NetworkLastLockCost, NetworkMinLockCost, NetworkLockReductionInterval;
- `LastRateLimitedBlock ++ 0x02`;
- TaoInRefundDeploymentBlock, NominatorMinRequiredStake;
- TotalIssuance (VERIFY pallet);
- DissolveCleanupQueue (Vec; length only);
- ShortsEnabled (VERIFY).

Other pallets:
- `SafeMode.EnteredUntil` (Option<BlockNumber>, decode by length);
- `Timestamp.Now` (u64 ms);
- `System.Number`.

Spec and transaction version come from `state_getRuntimeVersion`.

Hotkey panel (per tracked (hotkey, netuid); FULL plan, plus at each EPOCH_DRAIN of held or candidate subnets):
| Field | Item | Key | Decoder |
|---|---|---|---|
| total_alpha | TotalHotkeyAlpha | Blake2_128Concat(hk) ++ Identity(u16) | u64 |
| shares (V1) | TotalHotkeyShares | same | U64F64 (used if present at that block) |
| shares (V2) | TotalHotkeySharesV2 | same | SafeFloat (≥ 8,036,577) |
| earns / last_dividend | AlphaDividendsPerSubnet | Identity(u16) ++ Blake2_128Concat(hk) | u64 (present ⇒ earns) |
| take_u16 | Delegates | Blake2_128Concat(hk) **(VERIFY)** | u16 (absent → 11,796) |
| childkey_take_u16 | ChildkeyTake | (hk, netuid) **(VERIFY)** | u16 (absent → 0) |

Owner position: `AlphaV2(hotkey, coldkey, netuid)` → SafeFloat shares (≥ 8,036,577), plus legacy `Alpha` (U64F64) until 9,217,507. In the overlap, read both; legacy wins. Hashers **(VERIFY)**: expected Blake2_128Concat, Blake2_128Concat, Identity. Value = shares × I(owner_hotkey).

### 6.6 Runtime APIs (`chain/runtime_api.py`; args are SCALE LE, hex-encoded `"0x…"`)
| state_call method | Args | Returns / decode | Use |
|---|---|---|---|
| `SwapRuntimeApi_current_alpha_price` | u16 | u64 rao/alpha | spot cross-check |
| `SwapRuntimeApi_current_alpha_price_all` | (none) | compact len ++ [u16 netuid, u64 price]… (129 entries incl. root; VERIFY layout) | every 100 blocks: price parity ≤ 1e-6 relative |
| `SwapRuntimeApi_sim_swap_tao_for_alpha` | u16 ++ u64 rao | 6×u64 {tao_amount, alpha_amount, tao_fee, alpha_fee, tao_slippage, alpha_slippage} (48 B, spec ≥ 391); 4×u64 (specs 302–377) | pre-trade quotes; AMM parity. **All-zero = failure** |
| `SwapRuntimeApi_sim_swap_alpha_for_tao` | u16 ++ u64 alpha rao | same | same |
| `SubnetInfoRuntimeApi_get_subnet_to_prune` | (none) | Option<u16> | prune cross-check every 25 blocks; stale-input fallback |
| `SubnetInfoRuntimeApi_get_next_epoch_start_block` | u16 | Option<u64> (VERIFY) | drain timing cross-check |
| `SubnetInfoRuntimeApi_get_block_emission` | (none) | u64 (VERIFY Option) | block_emission (fallback: curve on TotalIssuance) |
| `SubnetRegistrationRuntimeApi_get_network_registration_cost` | (none) | u64 rao | cost cross-check (golden 962.89 TAO at 9,240,878) |
| `BetaBasketRuntimeApi_get_all_validator_baskets` | (none) | complex struct, decoded with scalecodec + V15 runtime-API metadata (collector extra; **VERIFY on Windows**) | escrow E per subnet, every 360 blocks |
| `StakeInfoRuntimeApi_get_stake_info_for_coldkey` | AccountId32 (escrow `"modl"+"subtensr"+"beta/esc"` padded to 32) | Vec<StakeInfo> (scalecodec) | escrow fallback |
| `SubnetInfoRuntimeApi_get_selective_metagraph` | u16 ++ Vec<u16> field indexes | complex (scalecodec) | LCW miner quality only |

If scalecodec is unavailable or fails on a spec, escrow_alpha is forward-filled from its last value with `Quality.CARRIED`, and sleeves that need it (carry C-U11, overlay m_esc) treat stale > 7,200 blocks as "unknown" (ineligible for new entries).

### 6.7 Read plans and key counts
| Plan | Content | Keys | Calls |
|---|---|---|---|
| HEAD (every finalized block, paper/live) | hot-path per-subnet fields (11 × 144) + globals (~25) | ~1,600 | 1 `state_queryStorageAt` |
| FULL (block % 60 == 0 live; every backtest snapshot) | all per-subnet fields (~30 × 144) + globals | ~4,300 | 2–3 calls of ≤ 2,000 keys |
| HOTKEY (FULL cadence for tracked pairs; at epochs) | 5 fields × ~6 hotkeys × relevant subnets + owner positions | ≤ 2,000 | 1 call |
| MEMBERSHIP (daily) | `state_getKeysPaged` of AlphaDividendsPerSubnet(n, ·) per started subnet | — | ~130 calls/day |
| CROSSCHECK | prune target / 25 blocks; price_all / 100 blocks; sim_swap at paper fills | — | ~400 calls/day |
| ESCROW | baskets API / 360 blocks | — | 20 calls/day |

### 6.8 Snapshot assembly (`chain/reader.py`; all-or-nothing)
1. `(block, hash)` comes from the finalized subscription or `chain_getBlockHash`. `state_getRuntimeVersion(hash)` gives spec and tx version.
2. Build keys for the plan from the registry, filtered by `since`/`until` spec and block.
3. Run `state_queryStorageAt` in chunks, concurrently up to `max_concurrency` (3), under the shared token bucket.
4. Decode every key, filling absent keys from the spec defaults.
5. For HEAD plans, carry FULL-only fields from the previous FULL snapshot and set `Quality.CARRIED`.
6. Choose the pool by era:
   - ≥ 8,486,594: `BALANCER(SubnetTAO, SubnetAlphaIn, quote)`;
   - era B with AlphaSqrtPrice present: `CP_V3_VIRTUAL(px_tao = L·√P, px_alpha = L/√P)`;
   - otherwise `CP_REAL` with `TA_PRICE` set after block 6,205,195.
7. Mark `SEED_FALLBACK` within 60 blocks of 8,486,594 if quote == 0.5 exactly and the price jumped > 1%.
8. Validate:
   - `subnets` contains exactly the non-root netuids whose NetworksAdded value is true at this block hash. A netuid that is removed and in cleanup, or queued and not yet added, is excluded even if NetworkRegisteredAt or its pool keys are still present (assertion; a violation is a `DecodeError`);
   - reserves > 0;
   - quote ∈ [0.01, 0.99];
   - one generation per netuid;
   - spot vs `current_alpha_price_all` ≤ 1e-6 relative (every 100 blocks live; every 50th snapshot in the collector, which aborts on > 1e-4).
9. Build the `ChainSnapshot` and store `digest = blake2b-128(canonical_bytes)`.

### 6.9 Live feed (`chain/head.py`, `LiveChainFeed`)
- **Input:** `chain_subscribeFinalizedHeads` (no reorg handling needed). A parallel `chain_subscribeNewHeads` provides best head and lag telemetry (HealthObs) and the live venue's N+2 timing.
- **Fallback:** if no finalized head arrives for 30 s or the WebSocket drops, poll `chain_getFinalizedHead` over HTTP every 3 s and rotate endpoints.
- **Gap fill:** gaps ≤ 280 blocks are filled from the lite node at full resolution. Larger gaps go to the archive at stride 10, then per-block. The first item after a gap carries `feed_gap_blocks`.
- **Stall detector:** no finalized head for 3 × 12 s sets `secs_since_block` (mode logic in §3.11).
- **Windows:** use the default Proactor loop and `signal.signal(SIGINT/SIGBREAK)` with a shutdown event. No `add_signal_handler`.

### 6.10 Rate limiting, backoff, circuit breakers (`chain/rpc.py`)
- **One token bucket per endpoint per process:** rate 3 req/s, burst 3, concurrency ≤ 3 (public OnFinality trips at ~8). Keyed providers are configured up to their plan.
- **Transient errors:** HTTP 429 (honour `Retry-After` / `retry_after_seconds`), −32029, −32005, −32603 and transport errors. Backoff is exponential, 1 → 60 s, with jitter ×[0.5, 1.5]. After 2 consecutive transient errors, rotate endpoint. Max 6 retries per call, then raise.
- **−32004 "Historical work rate limit exceeded":** open the breaker on that endpoint for 300 s. Half-open probe with one call.
- **"State already discarded":** re-route that read to the archive role.
- **Non-retryable:** decode errors and invalid params are never retried.
- **Provider disagreement:** every 200 snapshots, 3 random keys are compared across two providers at the same hash. A mismatch quarantines the endpoint and journals `ModelDriftObserved(probe="provider")`.

### 6.11 Archive vs head and the RU budget
| Workload | Calls | Notes |
|---|---|---|
| Live HEAD + FULL + crosschecks + membership + escrow | ≈ 8–10k RU/day (+7.2k if the optional System.Events decoder is on) | ≈ 2–4% of the free OnFinality key; ≈ 0.12 req/s on public endpoints |
| Era-C backfill, 60-block (8,486,594 → head, ≈ 12.6k snapshots × 3) | ≈ 38k | ≈ 2–3 h public |
| Full dTAO history, 300-block (from 4,920,351, ≈ 14.4k × 3) | ≈ 43k | for S0/F1 outside the gate era |
| Hotkey panel per epoch from 8,466,531 (≈ 2,150 epochs; covers the post-June and gate eras of S0/S1) + daily point-in-time membership (`state_getKeysPaged` at each historical hash) | ≈ 16k | tracked sets are point-in-time and sticky (§5.12) |
| Optional: 300-block hotkey panel from 4,920,351 to 8,466,531 (≈ 11.8k points) + daily point-in-time membership | ≈ 60k | needed only if S1 covers the Maymin window with P×I; otherwise those results are labelled price-only (§8.11) |
| Per-block prune windows (last 10 prunes × 3,600 blocks, ≈ 80 keys) | ≈ 400 range calls | FT1b |
| MR event study flow change-sets (gate era, 1,000-block chunks) + candidate windows | ≈ 500–5,000 | measure range-call limits first |
| Launch dense windows (52 generations) | ≈ 4k | |
| **Total one-off** | **≈ 105–155k RU** (≈ 165–215k with the optional pre-June panel) | < 1 day on a free key; ~12–20 h public |

### 6.12 Spec handling
- `SPEC_CHANGED` sets mode CAUTION and starts `verify` V1–V7 (§9.5).
- Item `since`/`until` bounds come from `state_getMetadata` diffs per spec, extracted offline, cached in `chain/spec_defaults/<spec>.json` and committed.
- An unknown spec with an unregistered item fails closed. The reader still runs (raw bytes kept), but snapshots are flagged and live goes FROZEN until the user accepts the spec.

---

## 7. Storage schema

**Principle.** Market data is immutable and append-only: Parquet, zstd, read through **in-memory DuckDB views**, so there are no file locks on Windows and any number of readers. Run state is small, transactional and single-writer, so it lives in **SQLite WAL**. The journal must be ACID with concurrent readers while paper trades; DuckDB allows only one read-write process, and that is why the journal is not in DuckDB.

The generation key `(netuid, reg_at)` is on every per-subnet row. Joins never use netuid alone.

### 7.1 Market-data lake (`data/lake/<table>/era=<A|B|C>/part-<first>-<last>.parquet`)
Chunks are written by the process that collected them: in-memory DuckDB `COPY … TO tmp (FORMAT PARQUET, COMPRESSION ZSTD)`, then an atomic rename, then a `manifest` row. Chunks are sorted by (block, netuid) and never edited; a decoder fix rebuilds them from `raw_rpc`.
```sql
-- global state, one row per snapshot block
CREATE TABLE snap_global (
  block UBIGINT PRIMARY KEY, block_hash VARCHAR, ts_ms UBIGINT, plan UTINYINT,           -- 1 HEAD, 2 FULL
  spec_version USMALLINT, tx_version USMALLINT, total_issuance UBIGINT, block_emission UBIGINT,
  moving_alpha_raw HUGEINT, gate_bar_raw HUGEINT, gate_rank USMALLINT, gate_exponent UTINYINT,
  tao_weight_raw UBIGINT, root_tao UBIGINT, owner_cut_u16 USMALLINT, subnet_limit USMALLINT,
  immunity_period UBIGINT, network_rate_limit UBIGINT, last_reg_block UBIGINT, last_lock_cost UBIGINT,
  min_lock_cost UBIGINT, lock_reduction_interval UBIGINT, tao_in_refund_block UBIGINT, nominator_min_stake UBIGINT,
  cleanup_queue_len USMALLINT, n_nonroot_networks USMALLINT, safe_mode_until UBIGINT, shorts_enabled BOOLEAN,
  runtime_prune_target USMALLINT, digest VARCHAR, quality_or UINTEGER, decoder_version USMALLINT);

-- per subnet generation per snapshot. PK (block, netuid). Generation = (netuid, reg_at).
CREATE TABLE snap_subnet (
  block UBIGINT, netuid USMALLINT, reg_at UBIGINT,
  pool_kind UTINYINT, tao UBIGINT, alpha_in UBIGINT, px_tao HUGEINT, px_alpha HUGEINT,   -- era-correct effective pool
  w_quote_e18 UBIGINT, fee_rate USMALLINT, reservoir_tao UBIGINT, reservoir_alpha UBIGINT,
  alpha_out UBIGINT, protocol_alpha UBIGINT,
  moving_price_raw HUGEINT, fast_moving_raw HUGEINT, root_prop_raw HUGEINT, miner_burned_raw HUGEINT,
  emission_enabled BOOLEAN, subtoken_enabled BOOLEAN, reg_allowed BOOLEAN, first_emission_block UBIGINT,
  tempo USMALLINT, last_epoch_block UBIGINT, ema_halving_blocks UINTEGER,
  tao_in_emission UBIGINT, excess_tao UBIGINT, alpha_out_emission UBIGINT, alpha_in_emission UBIGINT,
  tao_flow_cum BIGINT, volume_cum HUGEINT, owner_coldkey VARCHAR, owner_hotkey VARCHAR,
  owner_cut_enabled BOOLEAN, owner_cut_autolock BOOLEAN, total_alpha_staked UBIGINT, max_allowed_validators USMALLINT,
  consensus_mode UTINYINT,                                                                  -- NULL before spec 475
  mg_n_miners USMALLINT, mg_n_miner_coldkeys USMALLINT, mg_top1_coldkey_ppm UINTEGER, mg_n_permit_coldkeys USMALLINT,  -- LCW only
  escrow_alpha UBIGINT, escrow_block UBIGINT,                                               -- forward-fill source block
  owner_alpha UBIGINT, quality UINTEGER, decoder_version USMALLINT);

-- tracked hotkey share pools. PK (block, netuid, hotkey)
CREATE TABLE snap_hotkey (
  block UBIGINT, netuid USMALLINT, reg_at UBIGINT, hotkey VARCHAR,                          -- 0x + 64 hex
  total_alpha UBIGINT, shares_src UTINYINT,                                                 -- 1 = V1 U64F64, 2 = V2 SafeFloat
  shares_mantissa HUGEINT, shares_exp SMALLINT,                                             -- exact: shares = m * 10^e (V1 converted exactly)
  take_u16 USMALLINT, childkey_take_u16 USMALLINT, earns BOOLEAN, last_dividend UBIGINT);

-- membership listing (daily state_getKeysPaged) PK (block, netuid, hotkey)
CREATE TABLE dividend_keys (block UBIGINT, netuid USMALLINT, reg_at UBIGINT, hotkey VARCHAR);

-- generation lifecycle (built by data.collector + data.refine)
CREATE TABLE generation (
  netuid USMALLINT, reg_at UBIGINT,                                                         -- PK
  queued_block UBIGINT, added_block UBIGINT, start_call_block UBIGINT, first_seen UBIGINT, last_seen UBIGINT,
  end_block UBIGINT, end_kind VARCHAR,                                                      -- 'pruned' | 'dissolved' | 'open'
  end_refined BOOLEAN, lock_amount UBIGINT, seed_price_rao UBIGINT, seed_anomaly BOOLEAN,
  pre_end_tao UBIGINT, pre_end_alpha_in UBIGINT, pre_end_alpha_out UBIGINT, pre_end_protocol UBIGINT,
  pre_end_escrow UBIGINT, pre_end_total_staked UBIGINT, observed_payout_ratio DOUBLE);       -- FT10 calibration

-- derived chain events (lake copy of protocol.derive output at collection cadence)
CREATE TABLE chain_event (block UBIGINT, kind VARCHAR, netuid USMALLINT, reg_at UBIGINT, hotkey VARCHAR,
  flag BOOLEAN, amount HUGEINT, frac_ppm INTEGER, name VARCHAR, old VARCHAR, new VARCHAR);

CREATE TABLE registration (queued_block UBIGINT PRIMARY KEY, victim_netuid USMALLINT, victim_reg_at UBIGINT,
  new_reg_at UBIGINT, cost_ratio DOUBLE, lock_amount UBIGINT, blocks_since_prev UBIGINT, shielded BOOLEAN);
CREATE TABLE spec_boundary (spec_version USMALLINT PRIMARY KEY, setcode_block UBIGINT, first_logic_block UBIGINT);
CREATE TABLE calib (block UBIGINT, probe VARCHAR, netuid USMALLINT, model DOUBLE, chain DOUBLE, rel_err DOUBLE);
CREATE TABLE raw_rpc (block UBIGINT, call VARCHAR, request_sha VARCHAR, response_zstd BLOB);  -- optional layer 0 for re-decoding
-- optional enrichment (Taostats), joined by BLOCK to a generation, never by netuid alone
CREATE TABLE ext_trades (block UBIGINT, netuid USMALLINT, reg_at UBIGINT, side VARCHAR, coldkey VARCHAR,
  tao_rao UBIGINT, alpha_rao UBIGINT, extrinsic_id VARCHAR, seq USMALLINT);
CREATE TABLE ext_crosscheck (day DATE, netuid USMALLINT, reg_at UBIGINT, chain_price_rao UBIGINT, ts_price_rao UBIGINT, rel_diff DOUBLE);
```
**DuckDB views** (`data/lake.py`):
- `CREATE VIEW v_subnet AS SELECT * FROM read_parquet('data/lake/snap_subnet/**/*.parquet', hive_partitioning=1)`, and likewise for the other tables.
- Generation lookup: `SELECT reg_at FROM generation WHERE netuid=? AND first_seen<=? AND coalesce(end_block, 1e18)>?`.
- Replay query: `SELECT … FROM v_subnet WHERE block BETWEEN ? AND ? ORDER BY block, netuid`, streamed with `fetchmany` into `ChainSnapshot`s (`data/replay.py`). `snap_hotkey` is joined by (block, netuid). Escrow is forward-filled from past values only.
- **Size estimate (VERIFY after M1):** era C at 60-block cadence ≈ 12.6k snapshots × 144 subnets ≈ 1.8M subnet rows, tens of MB zstd. The full 300-block history is similar. The live per-block recorder is ≈ 30 MB/day before compaction. Everything stays well inside 400 GB.

**Live hot staging** (`data/recorder.py`): every processed snapshot is appended to `data/hot/<yyyymmdd>.jsonl.zst` with fsync **before** the journal commit that references its digest. It is compacted to Parquet hourly; the hot file is deleted only after the manifest row commits. `SnapshotStore` reads hot staging and lake transparently, by block or by digest.

### 7.2 Journal (SQLite, `data/runs/<run_id>/journal.sqlite`)
```sql
PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;        -- backtests: ':memory:' or synchronous=OFF
CREATE TABLE journal (
  seq INTEGER PRIMARY KEY, batch INTEGER NOT NULL,         -- batch = seq of the first record of the atomic batch
  block INTEGER NOT NULL, phase INTEGER NOT NULL, sub INTEGER NOT NULL, book TEXT NOT NULL,
  kind TEXT NOT NULL, version INTEGER NOT NULL, payload BLOB NOT NULL,   -- canonical JSON (core.codec)
  idem TEXT UNIQUE,                                        -- idempotency key (NULL = not deduplicated)
  prev_hash BLOB NOT NULL, hash BLOB NOT NULL);            -- blake2b-256(prev || block|phase|sub|book|kind|version|payload)
CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TABLE anchor (id INTEGER PRIMARY KEY CHECK (id = 1), head_seq INTEGER, head_hash BLOB);  -- updated in the same txn
```
`append_batch`:
1. `BEGIN IMMEDIATE`.
2. Assert non-decreasing (block, phase).
3. Extend the hash chain and insert all rows.
4. Update the anchor and `COMMIT`.
5. Any failure (a duplicate idem included) rolls back the whole batch.

Dropped triggers are still caught by `verify_chain()`. The heartbeat carries the head hash, so whole-file rollback is detectable off-host.

### 7.3 Run-state projections (`data/runs/<run_id>/state.sqlite`; rebuildable from the journal)
```sql
CREATE TABLE run_meta (run_id TEXT PRIMARY KEY, mode TEXT, code_hash TEXT, config_hash TEXT, prereg_hash TEXT,
  data_manifest_hash TEXT, parent_run TEXT, started_wall_ts INTEGER);
CREATE TABLE orders_proj (book TEXT, order_id TEXT, attempt INTEGER, state TEXT, kind TEXT, netuid INTEGER, reg_at INTEGER,
  hotkey TEXT, tao_in INTEGER, alpha_in INTEGER, limit_price INTEGER, urgency INTEGER, created_block INTEGER,
  terminal_block INTEGER, reason TEXT, PRIMARY KEY (book, order_id, attempt));
CREATE TABLE live_submissions (order_id TEXT, attempt INTEGER, delegate TEXT, nonce INTEGER, era_start INTEGER,
  era_end INTEGER, submit_head INTEGER, expected_fill_block INTEGER, used_nonce INTEGER,
  delegate_free_before INTEGER, carrier_hash TEXT, inner_hash TEXT, carrier_fee_settled INTEGER, state TEXT,
  PRIMARY KEY (order_id, attempt));                        -- written BEFORE any send; updated from the submit result
CREATE TABLE checkpoint (book TEXT, seq INTEGER, state_hash TEXT, blob_zstd BLOB, features_digest TEXT,
  PRIMARY KEY (book, seq));
CREATE TABLE fetch_ledger (block INTEGER PRIMARY KEY, status TEXT, attempts INTEGER, provider TEXT, last_error TEXT);
CREATE TABLE manifest (path TEXT PRIMARY KEY, tbl TEXT, first_block INTEGER, last_block INTEGER, rows INTEGER,
  sha256 TEXT, schema_version INTEGER, decoder_version INTEGER, created_wall_ts INTEGER);
CREATE TABLE endpoint_health (endpoint TEXT PRIMARY KEY, ewma_ms REAL, errors INTEGER, breaker TEXT, since INTEGER);
CREATE TABLE trial (trial_id TEXT PRIMARY KEY, cfg_hash TEXT, strategy TEXT, data_range TEXT, purpose TEXT, wall_ts INTEGER);
```
`fetch_ledger`, `manifest` and `endpoint_health` live in `data/state.sqlite`, shared by the collector and recorder; the other tables are per run. `trial` is the multiple-testing registry: every backtest evaluation writes a row, and reports print the count.

### 7.4 Run outputs (Parquet, `data/runs/<run_id>/out/`; exported at end of run or nightly)
- `nav (book, block, cash, fee_float, positions_spot, positions_liq, nav_spot, nav_liq)`.
- `fills (… Fill columns …, attribution)`.
- `ledger_entries (txn_id, block, account, unit, amount)`.
- `positions_daily`, `signals`, `risk_actions`.
- `roundtrips (entry_fill, exit_fill, hold_blocks, pnl_price, pnl_yield, swap_fees, tx_fees, shortfall, dereg_loss)`.
- `metrics (run_id, book, scope, regime, metric, value, ci_lo, ci_hi, n_eff)`.
- `attribution (book, strategy, component, value)`.
- `universe_counts (block, sleeve, eligible)`.

---

## 8. Backtest engine (`taotrader.backtest` + `venues.sim`, WP10/WP6)

A backtest is the production `Runner` with `ParquetReplay` + `SimVenue` + an in-memory journal. There is no separate backtest loop.
- A run is identified by (code hash, config hash, preregistration hash, data manifest hash, seed) and is bit-reproducible: identical journal hash chains.
- One pass drives N books: each sleeve alone, the blend, and the baselines (§2.5), crossed with impact bounds {TEMPORARY, half-life 14,400, PERSISTENT} and dereg models {formula, 0.35×, 0.65×}.

### 8.1 Replay
- `ParquetReplay(start, end, stride=60)` streams FULL snapshots in block order. A 30-day warm-up before `start` is ingested with `warm = False`, so features fill but nothing trades.
- Strategies are refused if `min_cadence_blocks < stride` or `block < valid_from_block` (`DataContractError`).
- **Refinement windows** (`data/refine.py`) add per-block snapshots where timing matters:
  - the 3,600 blocks before each of the last 10 prunes (FT1b);
  - ±4 h around the 2026-06-22 purge (8,463,544) and the 47-subnet re-enable (9,029,889);
  - ±150 blocks around momentum impulses (F14).
- Replay merges refined blocks into the stream, so fills and tripwires inside those windows are block-exact.

### 8.2 Era-correct AMM (`protocol/amm.py`; the simulator never branches on era, the loader builds the right `PoolState`)
| Era | Blocks | Pool | Price | Fee |
|---|---|---|---|---|
| A | 4,920,351 → per-subnet v3 init | CP_REAL on (SubnetTAO, SubnetAlphaIn), w = 0.5 | T/A | **unknown (VERIFY)**: Swap.FeeRate did not exist before v3, and the brief gives only 196 (specs 290–292) and 33 after. WP4 measures it at ≈ 5 era-A blocks from StakeAdded tao/alpha against T/A, or from the era-A runtime constants; `fee_rate_default(spec)` returns the measured value. Until then 33 is used and era-A results carry `Quality.DEFAULT_FILLED` |
| B | 5,947,549 → 8,486,593 (lazy per subnet; a reused netuid is uninitialised until its first swap) | CP_V3_VIRTUAL: px_tao = L·√P, px_alpha = L/√P, with √P = Swap.AlphaSqrtPrice (U64F64), L = Swap.CurrentLiquidity; w = 0.5. Buy: α_out = px_alpha·d/(px_tao + d); sell: τ_out = px_tao·d/(px_alpha + d). Exact when no tick is crossed (≤ 0.00003% vs sim_swap) | P = √P². T/A is used only before 6,205,195 or for uninitialised pools (`TA_PRICE`) | `Swap.FeeRate` per subnet if present, else 196 for specs 290–292, 33 after |
| C | ≥ 8,486,594 | BALANCER with w_quote from SwapBalancer (never 0.5 hard-coded); buy α_out = x·(1 − (y/(y+dy))^(w_q/w_b)), sell §5.12 | (w_b/w_q)·y/x | FeeRate (absent → 33) |
Era C pools seeded at q = 0.5 because of a seeding failure are flagged `SEED_FALLBACK`. Real reserves (tao, alpha) are always kept for caps, depth and the dissolution pot, even in era B.

### 8.3 Own-impact fills (the footprint overlay)
- `SimVenue` keeps, per generation, the cumulative reserve displacement of this book's fills: (d_tao, d_alpha, stamp_block). It is rebuilt from journaled `FillReported` on recovery.
- `mark_to(raw)` returns the snapshot with each touched pool shifted by `decay·(d_tao, d_alpha)`, where decay = 0.5^((block − stamp)/half_life).
- **TEMPORARY** (`impact_half_life_blocks = 0`, the **headline and gating bound**): the displacement vanishes immediately, so the exit trades against the historical pool and pays impact on both legs.
- **PERSISTENT** (`None`): the displacement stays, so a round trip costs only fees. This is the optimistic bound.
- **Half-life 14,400** is reported as a middle case.
- Prototype measurement: a 10-TAO position in a 1,000-TAO pool loses 0.208 TAO round trip under TEMPORARY vs 0.012 under PERSISTENT, a 17× difference. The truth is not calibratable before real fills exist, so every report shows both and gates use TEMPORARY.
- The footprint is additive on reserves. It ignores other traders reacting and emission-share feedback, which is first-order valid only while positions stay ≤ 1–2% of the pool; the caps enforce that.

### 8.4 Latency model (N+2)
- An order decided on snapshot b is acked with `expected_fill_block = b + finality_lag (3) + latency (2)`.
- **Paper and refined windows:** a shielded order fills only at exactly `expected_fill_block`, because a shielded inner can execute only in block N+2. If the deterministic miss injection fires, it is MISSED at N+2 (`OrderFailed(SHIELD_MISSED, expired=True, tx_fee=carrier fee)`). A fill at N+3 … N+8 is never legal.
- **Stride replays only:** the fill uses the first available snapshot at or after that block (usually the next 60-block snapshot). This is conservative for continuation signals; the fill is flagged `exact_block = False`, and the report shows the effective-latency distribution. β uses h = stride in these replays (§3.12).
- Unshielded era-16 risk-exit fallbacks fill at the first block in (submit, era_end].
- Stress variants: latency +30 and +120 blocks; adverse drift +0.2% at fill.

### 8.5 Chain rules and failures (exactly as the chain would revert)
- **Minimums and liquidity:** gross ≥ 0.002 TAO + fee and post-fee ≥ 0.002 (`AmountTooLow`); MinimumReserve 1,000,000 rao (`ReservesTooLow`); input ≤ 1,000 × reserve; output ≤ reserve.
- **Limits:**
  - strict crossing: buy needs spot < limit, sell needs spot > limit, else `PriceLimitExceeded`;
  - amount > max_amount_to_limit with allow_partial = False → `SlippageTooHigh`;
  - allow_partial fills to the limit and refunds the rest.
- **Partial sells** need output ≥ 0.002 TAO. A nominator remainder below the dust limit is force-sold at no limit and accounted as a fill.
- **State checks:** buys need SubtokenEnabled; orders on a dissolved generation fail with `SubnetNotExists` (`pool_gone`).
- **Fees:**
  - the swap fee comes from the snapshot's own FeeRate;
  - tx fees from `ExecCfg`;
  - **a failed inner call still pays the full tx fee**;
  - a shield miss pays the carrier fee on the miss itself (the measured 1.1% were carriers that were included but not decrypted, so sim always charges it; only live has never-included carriers, settled by `CarrierFeeSettled`).
- **Failure injection:** an order is MISSED if `blake2b(seed|order_id|attempt) mod 1e6 < shield_miss_ppm` (1.1%; swept 0.5–3%). This is deterministic, so replays and sweeps are stable. The miss is declared at N+2 and the delegate is locked until its era end + 2, exactly as live (§9.6). Optional `fail_inject_ppm` adds random inner-call failures with fee paid.

### 8.6 Regime table (`protocol/regimes.py`: the ONE table; new logic starts at setCode block + 1)
| regime_id | First block | Notes |
|---|---|---|
| era_a | 4,920,351 | dTAO launch; first weeks `EARLY_TINY_POOL` |
| era_b_v3 | 5,947,549 (per-subnet lazy) | fee 196 for specs 290–292; T/A diverges after 6,205,195; sim_swap from 6,262,253 |
| taoflow | spec 334/338 setCode + 1 (2025-11-04/05; block found by `refine.spec_boundaries`) | flow-EMA shares |
| chainbuy_unrecorded | v3.3.1-362 (2025-12-12) → 8,283,783 | ΣSubnetTaoInEmission undercounts |
| halving | 7,103,976 | 0.5 TAO/block |
| spec411 | 8,283,784 | SubnetExcessTao and SubnetEmissionEnabled exist |
| price_ema_rp | 8,466,531 (setCode 8,466,530, spec 421) | price-EMA × rp, no gate; SubnetTaoFlow valid as a running total; purge at 8,463,544 just before |
| balancer | 8,486,594 | era C |
| price_ema | 8,636,191 (spec 432) | rp removed |
| gate_qmass | 8,713,794 (spec 440) | Hill gate, q-mass mode |
| gate_rank32 | 8,765,684 (spec 441) | rank-32 gate; Root Reborn; escrow baskets |
| spec445 | 8,831,004 | miner-burn scaling restored (spec 444 never ran) |
| curated | 8,938,466 → 9,088,597 | curated root weights; removed at 9,088,598 |
| basket_trading | 9,117,749 | BasketTradingEnabled |
| v2_only | 9,217,508 | legacy Alpha drained |
| spec475 | setCode(475) + 1 (2026-10-07; found by binary search on `state_getRuntimeVersion`) | Null consensus, "precise emissions", PoW registration: **re-verify all replicas** |

Event markers (not regimes): 8,463,544 (54 disables), 9,029,889 (47 re-enables), 2026-09-02 disable wave, and the freeze gap 5,611,658 → next block (`CHAIN_STALL_GAP`).

Strategies cannot read the regime id (no hindsight). It drives `valid_from_block` guards and report slicing only.

### 8.7 Yield accrual (share-price index)
- A position is `shares` in a specific hotkey pool; value = shares × TotalHotkeyAlpha/shares_total (V1 if present, else V2).
- Each tick, `YieldAccrued(delta = value_t − value_{t−1})` is emitted per held position from the index change only. This is the epoch-drain credit (rounding may give −1 rao), and it doubles as the per-epoch income record for tax lots.
- Buys get shares at the index of the fill snapshot, so a buyer never earns a drain that happened before the fill.
- Hotkey switches are modelled `MOVE_STAKE` fills: no swap fee, one tx fee, shares re-issued at the destination index.
- If no tracked earning hotkey exists, the book cannot enter (`NO_YIELD_IDX`). The closed form is only a cross-check (T6 / T12a).

### 8.8 Dissolution payouts
- A `DEREGISTERED` held key gives `DeregSettled` from the **last good snapshot**: the refined removal−1 snapshot when the collector produced one, else the last stride snapshot, flagged as stale by up to 59 blocks.
- `formula`: payout = alpha_value × R × spot (§3.3 recovery ratio), credited as free TAO.
- Variants `fixed:0.35` and `fixed:0.65` run as separate books.
- **FT10 calibration:** replay the dissolutions of SN116 (9,210,610), SN82 (9,155,237), SN108 (9,111,229) and SN35 (9,046,671). Observed payout ratio = coldkey free-balance deltas over the on_idle blocks / alpha × spot at P−1. The pass threshold is |predicted − observed| ≤ 0.05; otherwise R = min(formula, 0.35).
- Survivorship: the universe at t contains every generation alive at t, including those pruned later.

### 8.9 Metrics, inference and promotion
- **Primary series:** NAV_liq in TAO (cash + one-shot `liq_value` of each position on the book's view), resampled to UTC days. NAV_spot is shown only beside it.
- **Exact decomposition per position and interval:** price = shares·I₀·Δp; yield = shares·ΔI·p₁; plus swap fees, tx fees, execution shortfall vs decision spot, dereg loss (payout vs last spot) and failed-order fees. An identity test asserts the components sum to ΔNAV_liq within 1 rao per tick.
- **Statistics:**
  - mean daily net, Newey–West t (lag 5), 95% stationary block bootstrap (mean block 7 d), effective n;
  - max drawdown, turnover, fee drag, hit rate, average hold, time in cash;
  - per-subnet contribution and leave-top-3-subnets-out;
  - regime table;
  - capacity sweep at capital ∈ {1, 3, 10, 30, 100, 300} TAO, reporting the capital at which net edge halves;
  - sensitivity grid: impact × dereg × fees×2 × latency×2.
- **Multiple testing:** every evaluation writes a `trial` row. Reports print the trial count, deflated Sharpe and the CSCV probability of backtest overfitting.
- **Promotion and kill.** P&L-level tests only kill; promotion needs the sleeve's component tests (§2) plus:
  1. a frozen pre-registered configuration;
  2. walk-forward only (fit earlier, report later, embargo ≥ 1 d);
  3. same sign in both halves of the evaluation window and leave-one-subnet-out;
  4. non-negative at fees ×2 and latency ×2 (kill if negative at ×1.5);
  5. capacity consistent with the configured capital.

  The default outcome of a weak sample is "not promoted".

### 8.10 Bias controls (each is a test or a report row)
- **Lookahead:** `SnapshotStore.window` raises `LookaheadError` past the clock. A canary strategy that peeks must raise. The future-truncation test: runs on data[:k] and data[:k+m] agree on all decisions before k − latency.
- **Survivorship and netuid splicing:** everything is keyed by (netuid, reg_at). Dissolved positions are paid out, never dropped. Taostats data is never a source of truth, because it splices generations and its rank includes immune subnets.
- **Prices and costs:** era-correct pools. `TA_PRICE` rows are masked in factor studies. Costs come from each snapshot's own fee rate.
- **Total return** only: yield comes through the index.
- **Impact** is reported as a bracket, gated at the conservative bound; sizing is pool-relative.
- **Regimes:** valid_from guards; strategies are blind to regime labels.
- **Placebos and canaries:** random-entry placebo, shuffled-signal placebo (must lose about the costs), delayed-signal monotone degradation, and an oracle canary (must show an implausible edge, proving the harness can see edges).
- **Forward-filled series** (escrow) use past values only and carry their source block.
- **As-of calibration:** every calibrated decision input (hazard CDF, P_OPEN, κ_p, R / the FT10 choice, Tier B jump parameters, φ) is fit only on events with block < decision block, through `CalibrationProvider.asof(block)` (§5.12). The frozen §3.3 table is allowed only in paper and live. The calibration digest is journaled in every `DecisionTrace`. **Bias test:** perturbing or deleting registrations, prunes and dissolutions after block t leaves every decision before t byte-identical.
- **Own-fill exclusion in β:** blocks with any book's own fills are excluded from the β samples (`FeatureEngine.update(own_fill_blocks=…)`), so a book never widens its own limits from its own impact.

### 8.11 Offline studies (`backtest/studies.py`)
| Study | Purpose | Output |
|---|---|---|
| S0 | EW total return (P×I, router hotkey, overlay guards, TEMPORARY costs) of eligible universes per sleeve; brief open question 3 | the benchmark every module must beat |
| S1 | factor re-run: SMB, WML7, WML30, REV, 1d/7d IC on the generation-keyed, payout-inclusive P×I panel, with entry lags {0, 300, 1,200} blocks, long leg only and long-short, net of TEMPORARY costs at realistic sizes | resets the momentum and carry priors (F0) |
| S2 | universe counts per sleeve at ≥ 10 dates (and daily) | capacity statements |
| S3 | carry T1, T2a, T3, T4, T5, T6 | carry gates |
| S4 | momentum F1–F3, F9–F12, F14, F15 | momentum gates |
| S5 | mean-reversion event study E-MR (§2.3) | re-proposal decision |
| S6 | launch FT0–FT4, FT6, FT10 | LCW gate |
| S7 | overlay FT1–FT6, FT9, FT10 (§10.4) | overlay calibration |

**Panel coverage rule (S0, S1).** P×I needs the hotkey panel, which WP4 collects per epoch from 8,466,531, so S0 and S1 are total-return results from that block on. Before 8,466,531 they are total-return only if the optional 300-block pre-June panel was collected (§6.11). Otherwise those windows are labelled **"price-only + closed-form yield proxy"** in every table and are ineligible for promotion or kill decisions.

### 8.12 Performance budget
- `decide` takes about 1 ms per book-tick at 128 subnets without AMM work. Decimal quotes cost tens of µs each.
- The gate era (≈ 7.9k snapshots) × 24 books runs in minutes on one core.
- `taotrader grid` fans out over ≤ 8 worker processes (≈ 1.5 GB each, reading the lake by DuckDB range scans).

---

## 9. Paper trader and LIVE adapter

### 9.1 Paper trader (`taotrader paper`, native Windows)
- It is the same `Runner` with `LiveChainFeed` (finalized heads; HEAD plan per block, FULL every 60), `PaperVenue` and a SQLite journal (WAL, synchronous=FULL).
- **PaperVenue = SimVenue on the live feed:**
  - fills use the actual recorded state at `expected_fill_block = submit best-head + 2`, because the recorder stores every finalized block;
  - a shielded fill is legal only at that block; misses are declared there with the carrier fee (§8.4);
  - fees come from `ExecCfg`, refreshed weekly from `estimate_shielded_carrier_fee` and plan() fee quotes logged by live-dry when available;
  - both impact bounds run as separate paper books.
- **Model drift probe:** at every simulated fill, call `sim_swap_*` at the fill block hash on the un-overlaid pool and compare with the local AMM. If the error is > 5 bp, journal `ModelDriftObserved`, set mode CAUTION and alert. This catches weight drift, superellipse pools (PR #3211) and fee changes. All-zero sim results count as failures.
- **Honest limit:** paper cannot know whether a shielded carrier would have been included. It uses the configured miss rate, and the real rate comes only from live telemetry. Paper fill rates are therefore optimistic.
- **Replay equality (the cheapest "backtest = live" check):** a nightly job replays the day's recorded snapshots through ParquetReplay with the same config and must reproduce the paper run's journal decisions byte-for-byte. Fills may differ only where the latency model differs. Any decision difference is a bug.
- **Budgets:** only PAPER-stage sleeves get capital (§3.10). SHADOW sleeves run in the same process with zero budget and journal their signals.
- **Paper gates for LIVE_ELIGIBLE** (all required; the user still decides):
  1. the sleeve's component tests (§2) on ≥ 30 post-475 days (carry), ≥ 60 days (momentum) or ≥ 20 trades (LCW);
  2. realised vs modelled cost: mean ≤ 10 bp, p90 ≤ 30 bp;
  3. fill failures ≤ 5%;
  4. no unreconciled position and no invariant breach;
  5. FT11 kill-switch drills passed;
  6. ≥ 7 consecutive days of live-dry with 100% `plan()` acceptance;
  7. net P&L not below the kill thresholds (P&L never promotes).

### 9.2 Live adapter: process and runtime
- **Where it runs:** WSL2 (systemd user unit) or a small Linux VPS (recommended for 24/7, because WSL2 sleeps with Windows). The live adapter is run **by the user, in the user's shell**. Nothing in this repository's tooling, CI or agents ever runs `--submit`.
- **Install:** a separate venv with `pip install --require-hashes -r requirements-live.txt`:
  - `bittensor==11.3.0 --hash=sha256:4651d9125cd29ecfda1eed0ef758fe9e29563dd81ecd9d42a9f1c9ec6603cbaa`;
  - verify PyPI Trusted Publishing provenance (RaoFoundation/subtensor, workflow `watch-mainnet-release.yml`, commit d1718c99…);
  - never install `bittensor-cli` or `bittensor-wallet` separately.
- **Files:** journal and state on local ext4 (never `/mnt/c`). The lake is mirrored from Windows by rsync (immutable chunks); as a fallback, run the collector at 300-block cadence for 30 days plus 60-block for 2 days.
- **Isolation:** `taotrader.live` is the only package that imports bittensor, and `cli.py` imports it lazily only after the gate passes. `live.sdk_port.SdkPort` (§9.4) hides the SDK's sync/async shape (brief open question 1), so a `FakeSdkPort` drives every live test without the SDK. LiveVenue also receives the WP1 `ChainReader` (block hashes, finalized head) by injection.

### 9.3 Gating (four locks plus preflight; any failure → `GateError`, exit non-zero)
| Lock | Requirement |
|---|---|
| 1 config | `[live] enabled = true` |
| 2 config confirmation | `[live] mode = "submit"`; `network` set explicitly; current spec_version ∈ `accepted_specs`; `sleeves` lists only LIVE_ELIGIBLE sleeves |
| 3 CLI | `taotrader live --live --submit` (both flags) |
| 4 environment | `TAOTRADER_LIVE_ARMED` = `"<expiry_unix>:<hmac>"`, hmac = HMAC-SHA256(local secret in the OS keyring of the live host, config_hash ‖ expiry), created by `taotrader live arm` (user-run), expiry ≤ 24 h, compared with `hmac.compare_digest`. Any config edit invalidates it. Mainnet also needs `TAOTRADER_LIVE_NETWORK_CONFIRM=finney` |
| plan-only (`live_dry`) | locks 1 + `--live` only. Builds real intents, runs `client.plan()`, journals the result, never submits |

**Arming lapses at runtime (UNARMED).** Runtime upgrades arrive about every 3 days and prunes about weekly, and 23% of prune victims had been rank 1 for under 24 h. The process therefore keeps running when the arm token expires or a SPEC_CHANGED leaves spec_version ∉ `accepted_specs`. It enters **UNARMED**: reads, reconciliation, planning and journaling continue, and LiveVenue refuses every submission with `OrderFailed(VENUE_REJECT, detail="unarmed")`, with one exception:
- **`LiveCfg.risk_exits_when_unarmed = true`** (the user sets it explicitly in `config/live.example.toml`; the live-arming runbook explains it): LiveVenue may submit **only EMERGENCY or URGENT full sells**, and only while V2 (sim_swap parity), V3 (prune-target parity) and V6 (call indices and the Staking allow-list) pass on the current spec. Limits come from runtime `sim_swap` at the head, never from the local AMM alone. It never submits a buy or a move. All other locks still apply and a config edit still invalidates the token. At start, an authentic but expired token for the current config hash, or a spec_version ∉ `accepted_specs`, admits an UNARMED start only when this flag is true (so a crash restart under systemd keeps the risk exits); otherwise the gate refuses.
- **`risk_exits_when_unarmed = false`:** while ladder exposure (positions with prune_rank ≤ 15) is above 0, alert at T − 2 h before arm expiry and on every SPEC_CHANGED. The alert lists each held position with its prune_rank and t\* and says that no exit can be submitted until the user re-arms.
- Re-arming (`taotrader live arm`, and `accepted_specs` updated after V1–V7 pass) returns the venue to normal operation.

**Preflight** (every start, and every 7,200 blocks):
1. The ops delegate key ≠ the real coldkey. Each delegate's free balance ≤ `max_ops_balance_tao` (0.5 TAO, a fee buffer only).
2. `Proxy.Proxies(real)` contains (delegate, **Staking (index 8)**, delay 0) for each configured delegate, and **no** other proxy type for any ops delegate. Any, NonTransfer or Transfer → refuse.
3. **Fee payer:** refuse if `RealPaysFee(real, delegate)` is true for any delegate (storage item name **VERIFY** from the `Proxy.set_real_pays_fee` dispatch). Otherwise inner fees, and the alpha-fee trap, would move onto the real coldkey.
4. **Locks:** refuse if `stake_availability(real, n).locked > 0` for any held netuid (read name **VERIFY**). Conviction locks on the trading coldkey make exits fail with StakeUnavailable (brief risk #16).
5. `bt.__version__ == "11.3.0"`; spec accepted (else UNARMED); SafeMode clear; head lag < 2 blocks; finality lag ≤ 5; fee float ≥ `min_fee_float_tao`; real free balance ≥ `RiskCfg.min_free_real_rao` (MIN_FREE_REAL, ≥ the existential deposit).
6. A tiny `plan()` of each intent shape returns no violations: a buy under the buy Policy, and a partial sell, a full exit and a same-subnet move built with exact alpha amounts under the sell/move Policy (§9.4). The call indices and the Staking allow-list match V6.
7. Reconciliation of chain vs journal is clean (no orphans).

A preflight failure at start is a `GateError`. At a periodic run it is a key alarm (FROZEN, §3.11, with its emergency-exit exception).

### 9.4 SDK 11.3.0 surface (`live/sdk_port.py`; signatures from brief §5.3–5.7, unexecuted, **VERIFY** in the WP11 contract test)
```python
@dataclass(frozen=True, slots=True)
class LiveCall:
    """An OrderIntent resolved by LiveVenue to exact amounts at the submit head. Never 'all' or u64::MAX."""
    kind: OrderKind                       # REMOVE_STAKE_FULL_LIMIT is mapped to REMOVE_STAKE_LIMIT before this point
    hotkey_ss58: str                      # origin hotkey
    netuid: int
    amount: int                           # buy: rao; sell/move: exact alpha rao (full exits: PostState.alpha_value)
    limit_price_rao: int                  # 0 for MOVE_STAKE
    allow_partial: bool
    dest_hotkey_ss58: str | None
    max_spend_tao: float | None           # buy: LiveCfg.max_order_tao; sell/move: None
    allowed_netuids: tuple[int, ...] | None   # buy: LiveCfg.allowed_netuids (None if empty); sell/move: that + held netuids


@dataclass(frozen=True, slots=True)
class PostState:
    """Chain state at one block (client.at(block)); fills are built from share deltas, never value deltas."""
    block: int
    real_free_rao: int                    # free TAO of the real coldkey
    shares: Decimal                       # real coldkey's shares on (hotkey, netuid): AlphaV2 SafeFloat (legacy Alpha wins)
    hk_total_alpha: int                   # TotalHotkeyAlpha(hotkey, netuid)
    hk_total_shares: Decimal              # TotalHotkeyShares V1 if present, else V2
    alpha_value: int                      # chain-computed stake value (sizes exact full exits; fill cross-check only)
    delegate_free_rao: int
    delegate_nonce: int                   # System.Account(delegate).nonce at this block


class SdkPort(Protocol):
    # --- reads
    async def proxies(self, real_ss58: str) -> list[tuple[str, str, int]]: ...        # client.balances.proxies(coldkey_ss58=real)
    async def proxy_announcements(self, real_ss58: str) -> list[tuple[str, str, int]]: ...   # Proxy.Announcements: (delegate, call_hash, height)
    async def coldkey_swap_scheduled(self, real_ss58: str) -> bool: ...                # scheduled coldkey swap for the real account
    async def real_pays_fee(self, real_ss58: str, delegate_ss58: str) -> bool: ...     # RealPaysFee(real, delegate) (name VERIFY)
    async def locked_alpha(self, real_ss58: str, netuid: int) -> int: ...              # stake_availability(real, n).locked (VERIFY)
    async def next_index(self, delegate_ss58: str) -> int: ...                        # pool-aware system_accountNextIndex
    async def quote(self, call: LiveCall) -> tuple[int, int]: ...                      # client.prices.quote_stake / quote_unstake (all-zero = failure)
    async def plan(self, call: LiveCall, delegate: str) -> tuple[list[str], int]: ...  # client.plan(..., policy=<per-call>): (violations, fee_rao)
    async def post_state(self, block_hash: str, real_ss58: str, hotkey_ss58: str, netuid: int,
                         delegate_ss58: str) -> PostState: ...
    async def block_extrinsics(self, block_hash: str) -> list[tuple[int, str, str | None, int | None]]: ...
        # (index, ext_hash, signer_ss58, nonce) for every extrinsic of the block (SDK substrate interface)
    async def extrinsic_events(self, block_hash: str, index: int) -> list[tuple[str, str, dict[str, object]]]: ...
        # decoded System.Events of ApplyExtrinsic(index): (pallet, event, fields), e.g. ExtrinsicFailed,
        # Proxy.ProxyExecuted{result}, Utility.ItemFailed, TransactionPayment.TransactionFeePaid
    async def account_events(self, block_hash: str, coldkey_ss58: str) -> list[tuple[str, str, dict[str, object]]]: ...
        # every event of the block that names the coldkey (StakeMoved/Transferred/Swapped, TransactionFeePaidWithAlpha, ...)
    # --- writes (LiveVenue in submit mode only: all four locks, or the UNARMED risk-exit exception of section 9.3)
    async def submit_shielded(self, call: LiveCall, delegate: str) -> tuple[str, str, int, int]: ...
        # client.submit_shielded(bt_intent, ops_wallet, policy=<per-call Policy>, proxy_for=REAL, proxy_type="Staking",
        #                        period=8, wait_for_inclusion=False)
        # -> (carrier_hash, inner_hash, submit_head_block, carrier_nonce); the SDK picks the nonces (carrier n, inner n+1)
    async def submit_plain(self, call: LiveCall, delegate: str) -> tuple[str, int, int]: ...
        # client.execute(..., period=16): risk-exit fallback ONLY -> (ext_hash, submit_head_block, nonce)
```
`RealSdk` implements every method with the SDK's client and its substrate interface on the live host; `FakeSdkPort` implements them for tests. LiveVenue gets block hashes for a block range from the injected WP1 `ChainReader`.

**Intent mapping** (amounts are always exact; no `'all'` and no u64::MAX anywhere in live):
| OrderKind | SDK intent |
|---|---|
| ADD_STAKE_LIMIT | `bt.AddStakeLimit(hotkey_ss58, netuid, amount_tao, limit_price_rao, allow_partial)` under the buy Policy |
| REMOVE_STAKE_LIMIT | `bt.RemoveStakeLimit(hotkey_ss58, netuid, amount_alpha, limit_price_rao, allow_partial)` under the sell Policy. For `full_position`, amount_alpha = `PostState.alpha_value` read at the submit head; a later drain's remainder follows §3.9 |
| REMOVE_STAKE_FULL_LIMIT | not sent live. A raw call needs `allow_raw_calls=True`, which LiveVenue never sets; it is mapped to RemoveStakeLimit with the exact amount |
| MOVE_STAKE (same subnet) | `bt.MoveStake(origin_hotkey, netuid, dest_hotkey, netuid, amount_alpha)` with the exact alpha at the submit head, under the move Policy (plain move_stake, no swap fee) |
| MOVE_STAKE_LIMIT | disabled in v1 |

**Policy** (brief §5.7: a per-call policy **replaces** the client policy, and `'all'` or TransferStake amounts count as unbounded spend, which `max_spend_tao` blocks):
- **Buys:** `bt.Policy(max_fee_tao, max_spend_tao=max_order_tao, allowed_netuids, allow_raw_calls=False)`.
- **Sells and same-subnet moves:** a per-call `bt.Policy(max_fee_tao, max_spend_tao=None, allowed_netuids ∪ held netuids, allow_raw_calls=False)`. A sell spends alpha, not TAO, and a full exit must never be blocked by a buy-size cap.
- Because per-call policies replace the client policy, LiveVenue enforces its own caps (§9.6 step 2) and never relies on a client-level policy.
- Full exits and moves pass the exact live alpha read by `SdkPort.post_state` at the submit head, never the string `'all'`. A remainder created by a drain between read and inclusion is handled by the dust rule (§3.9).
- A §10.5 Linux contract test asserts that `Policy.check()` accepts, with no violations, the exact RemoveStakeLimit (partial and full) and MoveStake objects LiveVenue builds under these policies.

**Never imported or constructed:** `UnstakeAll`, `UnstakeAllAlpha`, `TransferStake`, `Batch`, default `AddStake` (its 5% bound fails above 2.47% of the reserve), or `'all'` for any amount (unbounded under Policy; for buys its 500k-rao headroom is also below the ≈850k-rao fee).

### 9.5 Spec validation suite (`taotrader verify --spec`; run on SPEC_CHANGED, at live start, and in CI nightly)
| ID | Check | Tolerance |
|---|---|---|
| V1 | decode parity: local snapshot vs `current_alpha_price_all`, and against `get_all_dynamic_info` fields **excluding** `emission` and `pending_root_emission` (hard-coded 0 on chain) | price ≤ 1e-6 relative |
| V2 | AMM parity vs `sim_swap_*` on 3 subnets × 2 sizes × buy/sell | ≤ 1e-6 relative on outputs and fees |
| V3 | local prune target == `get_subnet_to_prune` | exact |
| V4 | emission replica vs SubnetTaoInEmission + SubnetExcessTao + Δreservoir | ≤ 1e-4 TAO/block per subnet; Σ = block emission |
| V5 | fee parity: plan() / query_info vs `ExecCfg` | within 20% |
| V6 | call indices 88/89/103/85/149/90 and the ProxyType::Staking (8) allow-list unchanged (metadata) | exact |
| V7 | freeze-list governance constants diff | any change → PARAM_CHANGED → re-run the affected tests (T2a, T6, FT1 hazard validity) |

Pass → paper resumes automatically; live resumes only after the user adds the spec to `accepted_specs` and re-arms. Fail → EXITS_ONLY using runtime-API state (prune target, prices, sim_swap-derived fill-or-kill limits). A transaction_version change → live FROZEN.

Until the user re-arms, live is **UNARMED** (§9.3). With `risk_exits_when_unarmed = true` it may still submit EMERGENCY/URGENT full sells while V2, V3 and V6 pass on the new spec, with limits from runtime sim_swap at the head. With it false it submits nothing and alerts on the SPEC_CHANGED while ladder exposure is above 0, listing held positions with their t\*.

### 9.6 Live order path (`live/venue.py`, `LiveVenue`; `mark_to` = identity because the chain contains our footprint)
Nonces below: the carrier is signed at delegate nonce **n** and the inner at **n + 1**; blocks: the submit head is **N** and the only legal inclusion block is **N + 2** (brief §5.9).
1. **Reserve, then journal.** `reserve()` picks a free delegate and reads its pool-aware `next_index` immediately before the Runner journals `SubmitStarted(delegate, nonce = n, era_end)`. era_end = finalized head + period (8; 16 unshielded) + 2 blocks of margin for the SDK's own anchor read. The `live_submissions` sidecar row (delegate, n, era start/end, delegate free balance before) is written **before** the send.
2. **Pre-submit checks against the head node used for NextKey:**
   - not lagging; SafeMode clear; SubtokenEnabled for buys; armed, or within the UNARMED risk-exit exception (§9.3);
   - resolve the `LiveCall`: for full exits and moves, the exact alpha from `post_state` at the submit head (§9.4);
   - fresh `quote`, where all-zero → `OrderFailed(VENUE_REJECT)`;
   - recompute the limit from fresh spot but **never looser** than `intent.limit_price`;
   - size ≤ max_*_to_limit;
   - **caps bound buys only.** `max_order_tao`, `max_daily_turnover_tao` (buy TAO only, per 7,200 blocks) and `max_position_tao` (post-buy executable value) apply to ADD_STAKE_LIMIT, as does the buy Policy's `max_spend_tao`. Sells are bounded only by the held position, and same-subnet moves only by the position on the origin hotkey. A risk exit (`ForcedExit` urgency ≥ URGENT) is **never** `VENUE_REJECT`ed for a cap; a 5-TAO Tier A exit with `max_order_tao = 1` is submitted;
   - `allowed_netuids` (buys; sells and moves use allowed ∪ held), kill file;
   - `plan()` shows no violations.
3. `submit_shielded` (period 8) → (carrier_hash, inner_hash, submit_head N, carrier_nonce).
   - carrier_nonce == the journaled n → `VenueAck(carrier_hash, inner_hash, submit_block = N, expected_fill_block = N + 2)`;
   - carrier_nonce ≠ n → `SubmitUnknown(detail="nonce_mismatch:<used>")`; the sidecar records `used_nonce`, and `resolve()` works from it;
   - an exception → the Runner journals `SubmitUnknown` (never `OrderFailed`).
4. **`advance(view)`**, once the finalized head ≥ N + 2:
   - fetch block N + 2's hash (WP1 `ChainReader`) and its `block_extrinsics`; find the carrier by hash. The inner is at carrier index + 1;
   - **carrier absent from finalized N + 2 → miss, final at once.** A carrier outside N + 2 can never be decrypted (brief §5.9). Journal `OrderFailed(SHIELD_MISSED, expired=True, tx_fee=0)`. The delegate stays locked until era_end + 2, and the order may be re-decided immediately on another free delegate;
   - carrier present: read `extrinsic_events(N + 2, inner index)`. `ExtrinsicFailed` → `OrderFailed(reason, tx_fee paid)`. **`Proxy.ProxyExecuted{result: Err(e)}` under `ExtrinsicSuccess`** → `OrderFailed(reason, detail = decoded error name, tx_fee paid)`, where reason is the FailReason that matches the decoded name (so the planner's SlippageTooHigh fallback still works) and `PROXY_ERROR` otherwise. Inner absent although the carrier is present → `OrderFailed(SHIELD_MISSED, expired=True, tx_fee = carrier fee)` (undecryptable inner);
   - success → `FillReported` from **share deltas**, never from the raw value delta and never from `StakeAdded`. `post_state` at N + 1 and N + 2 gives the coldkey's share count on (hotkey, netuid) and the hotkey index. Fill.shares = the change in share count (buys credited, sells and moves debited); Fill.alpha = that change × the post index I = hk_total_alpha/hk_total_shares at N + 2; Fill.tao = the free-TAO change of the real coldkey; tx fees from the two extrinsics' fee events. An epoch drain in N + 2 runs before extrinsics, so a value delta would include yield that `YieldAccrued` already books. If several own orders land in N + 2, the free-TAO change is apportioned by each inner's StakeAdded/StakeRemoved TAO amount, and the sum is checked against the change (a residual goes to `ReconAdjusted`);
   - **carrier-fee settlement after a miss with the carrier absent from N + 2**, once the finalized head > era_end: read the delegate nonce at era_end + 1. **n** → never included, `CarrierFeeSettled(fee 0, "never_included")`. **n + 1** → carrier included and inner dropped, fee = delegate free balance before − after (the delegate is locked, so nothing else moved it), `"carrier_only"`. **≥ n + 2** → the inner was included after all: scan blocks N + 1 … era_end with `block_extrinsics` for (delegate, nonce n + 1), read its dispatch result, journal `CarrierFeeSettled("inner_included")` and run reconciliation. An inner that executed contradicts the declared miss and raises a key alarm, and so does a nonce advance with no matching extrinsic (foreign use of the delegate key).
5. **`resolve()`** (after a crash, or after `SubmitUnknown`) applies the same rules from the sidecar (delegate, reserved or used nonce n, submit head if known, era_end):
   - delegate nonce at the finalized head still n and the finalized head > era_end + 8 → provably never sent: `NOT_PLACED` → `OrderFailed(NOT_PLACED, tx_fee 0)`; a later tick may re-decide under a new id (attempt + 1);
   - nonce ≥ n + 1 → find the carrier by (signer = delegate, nonce = n) with `block_extrinsics` over the era, take its block as N + 2, and apply step 4 (fill, failure or miss, then carrier-fee settlement);
   - otherwise `UNRESOLVABLE_YET`. Nothing is ever re-sent while unresolved.
6. **Delegate rotation:**
   - 2–3 funded Staking-proxy delegates, one in-flight carrier each (carrier n, inner n + 1);
   - after a miss, the stale carrier may hold nonce n until its era expires, so retries rotate to a free delegate. This is untested on chain; measure on test.finney first (brief open question 2);
   - each delegate needs its own proxy deposit (0.033 TAO) and fee buffer.

### 9.7 Reconciliation (`live/reconcile.py`)
- **Cadence:** every 25 blocks and after every fill.
- **Expected values:** each (real coldkey, hotkey, netuid) share count and value, and the real/delegate free balances, are compared with the ledger's expected values (own matched fills + epoch accrual at LastEpochBlock). Reads use `SdkPort.post_state`.
- **Tolerance:** 2 rao, or 1e-6 relative, per position.
- **On mismatch:** `ReconAdjusted` (chain wins) and entries halt until `QuarantineCleared`, which needs an operator command.
- **Key alarms** (→ FROZEN, §3.11), each with its `SdkPort` source (§9.4):
  - unexplained negative delta; delta > 3× expected epoch yield; unknown position (`post_state`);
  - delegate nonce ≠ expected (`post_state.delegate_nonce` against the journaled nonces);
  - `Proxy.Proxies(real)` changed (`proxies`) or announcements changed (`proxy_announcements`);
  - coldkey swap scheduled (`coldkey_swap_scheduled`);
  - foreign StakeMoved/Transferred/Swapped on our coldkey, and `TransactionFeePaidWithAlpha` (`account_events` on every finalized block; the events are matched against our own journaled inner hashes);
  - `RealPaysFee` turned on, or a lock appears on a held netuid (`real_pays_fee`, `locked_alpha`; every 7,200 blocks with preflight).
- **Dissolution:** the Engine never settles a held dissolution in live; the reducer marks it DISSOLVING. The observed free-TAO payout after the held generation's `NetworkRemoved` is journaled by reconciliation as `DeregSettled(model="observed")`, whose idempotency key `dereg:{book}:{netuid}:{reg_at}` allows exactly one settlement. It feeds FT10.
- **After any force-sell (dust rule):** re-read stake.

### 9.8 Safety invariants (enforced by code and tests; violation → halt and alert)
1. Nothing in the development, CI or agent process ever calls `submit_shielded`, `submit_plain` or `execute`. Only `taotrader live --live --submit` under all four locks can (or, UNARMED with `risk_exits_when_unarmed`, only EMERGENCY/URGENT full sells, §9.3), and it is user-run.
2. Only `taotrader.live` imports bittensor (import-linter plus an AST check in CI). Windows paper processes cannot import it at all.
3. The bot key is a zero-delay **ProxyType::Staking** delegate. The coldkey never touches any host that runs this code. Preflight refuses any other proxy type for the delegate.
4. No intent class capable of moving funds out (`TransferStake`, `Batch`, `UnstakeAll*`) is referenced anywhere in the code base.
5. Every buy has an explicit limit strictly above spot and size ≤ `max_buy_to_limit`. Every sell has a limit strictly below spot. Live limits are never looser than the planner's.
6. Every order has a deterministic id. Journaled `OrderIntended` and `SubmitStarted` precede any I/O. A crash never causes a blind re-send. UNKNOWN orders are resolved from chain truth.
7. One in-flight order per netuid; one in-flight carrier per delegate.
8. Balances and positions come from chain post-state share counts and the hotkey index, never from events, `StakeAdded` or raw value deltas. Chain truth wins through `ReconAdjusted`.
9. The journal is append-only and hash-chained, and every commit is atomic. Invariants (ledger balance, portfolio = ledger, sleeves = physical) are checked after every commit.
10. Spec, transaction-version or governance-constant changes block new risk until validated (V1–V7), and live resumes only after the user re-arms. Meanwhile live is UNARMED: it submits nothing, or, if the user set `risk_exits_when_unarmed`, only EMERGENCY/URGENT full sells while V2, V3 and V6 pass. With the flag off it alerts at T − 2 h before arm expiry and on every SPEC_CHANGED while ladder exposure is above 0 (§9.3).
11. The fee float stays ≥ 0.05 TAO (`fee_float_exits_rao`), or the mode is EXITS_ONLY (alpha-fee trap). `RealPaysFee` is off for every delegate (preflight).
12. Caps are enforced in two independent places: the planner and LiveVenue's own `max_order_tao` / turnover / position / `allowed_netuids` / buy `bt.Policy`. **These caps and `Policy.max_spend_tao` bound buys only**, and daily turnover counts buy TAO only. Sells are bounded only by the held position, same-subnet moves by the position on the origin hotkey, and a risk exit (urgency ≥ URGENT) is never `VENUE_REJECT`ed for a cap.
13. Long-only: no order may create a negative position. Positions are fully exited or kept ≥ the dust floor.
14. A kill file (`data/control/KILL`) or `taotrader halt` stops all new submissions within one block. `exits_only` and `flatten:<netuid>` are explicit operator commands.
15. A SafeMode or a stall ≥ 120 s freezes discretionary submissions. Risk exits are attempted only when the chain accepts staking calls.

---

## 10. Testing plan

`uv run pytest -m "not network"` runs on every change. `-m network` runs nightly against the archive with recorded expectations. CI runs on Windows (primary) and Linux (adds the live contract tests and cross-OS determinism).

### 10.1 Unit tests with verified vectors (golden; WP0 captures raw storage and runtime results at fixed blocks into `tests/fixtures/golden/`)
| Vector | Expected | Module |
|---|---|---|
| twox128(b"System") | `26aa394eea5630e07c48ae0c9558cef7`; System.Account and Timestamp.Now full prefixes match | chain.hashing |
| SubnetMovingAlpha raw 1,288,490 / 2^32 | 0.0003 (exact decode); EmissionGateBar 0.0082624; TaoWeight 0.18 | chain.scale |
| **SN92 10-TAO buy** at block ≈ 9,240,388 (captured pool) | tao_amount net 9,994,964,523 rao; fee 5,035,477 rao; alpha_out 7,289.425629 alpha (≤ 1e-7 relative; integer floors within a few rao) | protocol.amm |
| **SN1 1-TAO quote** (captured pool, spot 6,562,800 rao/alpha) | net 999,496,453 rao; fee 503,547 rao; alpha_out 152,290,647,774 rao (152.29 alpha) at block 9,240,388 (ADR-0001 #1) | protocol.amm |
| SN92 100-TAO buy (587.2-TAO pool) | 63,351.67 alpha out; 10,820.70 alpha impact (14.6%) | protocol.amm |
| Era-B virtual reserves at block 7,000,020 (SN1, SN19, SN64) | equal to sim_swap (≤ 3e-7 relative) | protocol.amm |
| **Slippage table** (shortfall Δ′/(T+Δ′), Δ′ = Δ(1 − 0.000504)) | T = 300: 0.33% / 3.23% / 25.0% for Δ = 1/10/100; T = 500: 0.20/1.96/16.7; T = 1,000: 0.10/0.99/9.09; T = 2,000: 0.05/0.50/4.76; T = 3,000: 0.03/0.33/3.23; T = 6,700: 0.015/0.15/1.47. Post-trade spot after a 100-TAO buy: +78% (T = 300), +21% (T = 1,000) | protocol.amm |
| V_max = T·s/(1−s) | s = 1% → 1.0101% of T; 2% → 2.0408%; 5% → 5.263%; 10% → 11.11% | protocol.amm |
| v11 default 5% bound | max buy = y·(√1.05 − 1) = 2.47% of the reserve (SN70 ≈ 241 TAO → ≈ 5.9 TAO) | protocol.amm |
| **EMA half-life table** (a = 0.0003·b/(b + 201,600), half-life = ln2/a) | mature (b ≈ 4.3M) 7.7–8.4 h; SN92 9.4 h (fixture b); b = 30 d → 14.9 h; 7 d → 1.6 d; 1 d → 9.3 d | protocol.ema |
| EMA warm-up 1 − exp(−0.0003·(b − 201,600·ln(1 + b/201,600))) | ≈ 3.7–4% at 1 d; ≈ 28% at 3 d; ≈ 80% at 7 d; 95.6% at 10 d; 99.7% at 14 d; closed form = per-block recursion within 1e-4 | protocol.ema |
| **Prune rule 52/52** | for each row of the brief §4.4 log, `ladder(snapshot at P−1)[0]` == the logged netuid (network test on archive snapshots; SN73 at 5,145,525 excluded as a dissolve) | protocol.prune |
| Live prune target | SN92 at the fixture block; SN58 at 8,500,160 | protocol.prune |
| Registration cost | 962.89 TAO at 9,240,878 (L = 653.02, last 9,210,610, I_eff 57,600); 1,003.01 for the 8,618,670 registration (L = 842.35, Δ = 46,614) | protocol.prune |
| Cost ratio → CDF | r = 2 − Δ/57,600: Δ 31k → 1.462; 43k → 1.253; 50k → 1.132; 55k → 1.045; 65k → 0.872 | protocol.prune |
| Recovery ratio | SN92 ≈ 0.368 (0.41 before the basket sale); SN47 (legacy) ≈ 0.36 | protocol.prune |
| t\* worst case | ladder ranks 2–4 at 1.255× / 1.508× / 1.53× of the bottom: 2.8 h / 4.8 h / 4.9–5.0 h; closed form = brute-force EMA stepping | protocol.ema |
| Closed-form yield | SN70 0.557%/day (rp 0.655, A_earn 182,839); SN92 0.448% (0.479, 343,198); SN64 0.0918% (0.133, 2,789,195) | protocol.yield_model |
| SN70 index | hotkey 56a9: I 1.329667 → 1.631360 over 30 d; flat 9,240,222–9,240,581, then +0.0279% at 9,240,582 (= LastEpochBlock) | protocol.yield_model |
| Injection split | SN51: rp 0.139, alpha_in 0.139/block, tao_in 0.01397, chain buy 0.0515 TAO/block; network Σ(tao_in + excess) = 0.5 TAO/block | protocol.emission |
| Emission replica | per-subnet E vs SubnetTaoInEmission + SubnetExcessTao ≤ 1e-6 TAO/block on 20 fixture blocks ≥ 8,765,684; SN51 ≈ 472–473 TAO/day | protocol.emission |
| Block emission curve | 0.5 TAO at issuance 11.6M; 1.0 below 10.5M; first 0.5-TAO block 7,103,976 | protocol.emission |
| Fees | floor(10e9·33/65535) = 5,035,477; buy tx 1,028,000 rao; sell 837,000 | protocol.fees |

Further unit tests:
- **events.derive:** netuid reuse gives DEREGISTERED + REGISTERED; idempotent on identical snapshots; LARGE_FLOW threshold; FULL-only fields fire only on FULL plans; **at the removal block P, DEREGISTERED fires at P even though NetworkRegisteredAt is still present** (fixture: NetworksAdded false, NetworkRegisteredAt and pool keys still set); a per-subnet consensus-mode change gives PARAM_CHANGED.
- **chain.reader:** a snapshot never includes a netuid with NetworksAdded false (removed-in-cleanup and queued-not-added fixtures).
- **protocol:** `parity_ok` boundary cases (median 0.99%/1.01%, 89%/90% within 5%, empty input); `fit_hazard` ignores rows at or after `asof`; `track_hotkeys` keeps sticky pairs and always includes the owner hotkey.
- **core:**
  - codec exact Decimal round trip (including the `normalize()` trap);
  - FSM legal and illegal transitions, including SUBMITTING → FILLED and SUBMITTING → EXPIRED being illegal;
  - `TargetBook.reduced` monotone;
  - `OrderIntent` validation;
  - ledger transactions balance per unit;
  - `make_order_id` stable.

### 10.2 Property tests (hypothesis)
- **AMM:**
  - a round trip on an unchanged pool is never profitable;
  - split vs single sell output within fee rounding (path independence);
  - weighted invariant x^w_b·y^w_q non-decreasing for buys;
  - `marginal_after_buy(max_buy_to_limit(p, L)) ≤ L` and sells symmetric, for balanced and skewed weights (q ∈ [0.3, 0.7]);
  - every guard raises the chain error name;
  - Decimal results equal on repeated runs (and across OS in CI).
- **Planner:**
  - never emits an amount below the minimums, above `max_*_to_limit`, or leaving dust;
  - limits are on the correct side of spot;
  - one order per netuid;
  - priority order respected.
- **Allocator:**
  - Σ targets ≤ G_MAX_EFF·NAV_liq;
  - per-subnet aggregate ≤ V_cap;
  - sleeve shares sum to the physical position;
  - EXIT and AVOID honoured.
- **Reducer** (`RuleBasedStateMachine` over random journals): `fold(journal) == checkpoint + tail`; invariants hold after every event; orphans never raise. Specific cases:
  - SUBMITTING + crash + `SubmitUnknown("recovered_submitting")` + resolve(LANDED) → FILLED with no orphan; the same with a miss → EXPIRED;
  - random `SleeveTransfer`s keep invariant 3 (sleeve shares sum to `Position.shares`, sleeve cash sums to `cash`) and post nothing to the ledger;
  - `CarrierFeeSettled` on an EXPIRED order is accepted (not an orphan) and balances fee_float against fees:tx;
  - a duplicate `DeregSettled` for the same generation is rejected by its idempotency key;
  - `BookView` projections (fail counts, cooldowns, chase, delegates, NAV samples, `SleeveStats`, router memory) are identical after a checkpoint + tail fold and a full fold; stride fills (`exact_block = False`) never increment the failure counters.
- **Codec:** `decode(encode(x)) == x` for every journal event type, including `SleeveTransfer`, `CarrierFeeSettled`, the new `DecisionTrace` fields and `OrderFailed.detail`.

### 10.3 Integration tests (`tests/integration`, WP10)
1. **Golden replay of a known period:**
   - blocks 8,765,684 → 8,830,000 (gate rank-32, ≈ 1,070 snapshots, from a committed mini-lake) with all baseline books and the carry MVP;
   - expected run digest per book, updated only with an ADR;
   - checks the decomposition identity and that the EW-price baseline is negative over the window (the brief's sign).
2. **Prune replay:** per-block window before the SN116 prune (9,210,610; rank 2 → 1 in < 1 h, 0.3% gap). Assert:
   - the local target matches the runtime target at sampled blocks;
   - a synthetic holding at 1% of T exits via Tier A or the backstop before P under default M_A, or the failure is reported as the known fast-prune case;
   - DeregSettled payout vs FT10 observed within tolerance.
3. **Event waves:**
   - 47 `EMISSION_TOGGLED(true)` at 9,029,889;
   - 54 disables at 8,463,544, which leave the overlay EXITS-ready, with URGENT exits for held names and the wave halt.
4. **Crash matrix:** crash at every fault point (pre-commit, in-transaction, post-commit, after SubmitStarted, after venue.submit, mid-drain) on the golden replay, restart with fresh objects, and assert an identical final money-state digest with no duplicate intent or fill. pytest samples about 150 points; the full matrix runs nightly.
5. **Determinism:**
   - two runs give byte-identical journal hash chains;
   - identical under PYTHONHASHSEED ∈ {0, 1, 777};
   - a restart on a complete journal re-verifies and writes nothing;
   - a Linux replay of a Windows journal is money-identical.
6. **No lookahead:** future truncation and the canary strategy; **as-of calibration**: perturbing or deleting registrations, prunes and dissolutions after block t leaves every decision (and `calib_digest`) before t byte-identical.
7. **Paper ↔ offline equality:** the replay of a recorded paper day reproduces its decisions.
8. **Cadence invariance:** slow strategies at stride 60 vs 300 agree within sampling error.
9. **Chaos (reader):** a fake JSON-RPC server injecting −32029, −32004, 429 with Retry-After, truncated bodies, WS drops, stale finalized heads and disk-full on journal commit. Assert bounded retries, endpoint rotation, no partial snapshot, resume from the manifest, gap flagging, halt on disk-full (no corruption).

### 10.4 Overlay falsification and calibration tests (S7; pre-registered)
| Test | PASS | Otherwise |
|---|---|---|
| FT1a recall (prunes 27–52 out-of-sample; synthetic victim at 0.5/1/1.5% of T) | Tier A/B/backstop fired ≥ U + M_A before P in ≥ 25/26, **reported with a 95% CI** | < 23/26 → redesign |
| FT1b timing (per-block windows, last 10 prunes) | rebuilt EMA error < 0.1%; M_A chosen as the smallest value with full recall on these 10 | — |
| FT1c precision | Σ avoided loss (1 − R − c_exit)·V / Σ false-alarm cost ≥ 3 at 1% sizing; false-alarm cost reported per sleeve | < 1.5 → drop the backstop or Tier B per ablation |
| FT2 hazard | walk-forward Brier skill ≥ +10% vs constant hazard; MC reproduces 7/52 and 12/52 within ±5 pp | constant hazard + window rule; Tier B stays off |
| FT3 emission-off | exiting at event + feasible latency beats holding by median ≥ 2% and in ≥ 60% of events at 7 and 30 d (P×I, payouts included) | downgrade to freeze entries + cap ×0.25 |
| FT4 gate/TRD | weekly IC of TRD vs next-7d P×I ≥ 0.05, NW t ≥ 2, monotone terciles, robust to λ_sell ∈ [0.3, 0.7] | haircuts stay MONITOR |
| FT5 overlay on/off per family | improves net, or cuts max DD ≥ 30% at ≤ 10% relative return cost, in ≥ 2 of 3 regimes; component ablation | delete components that lose in all regimes |
| FT6 cap realism | ≥ 95% of normal exits within S_EXIT_HOLD_MAX and ≥ 80% of urgent ones within budget, on replayed next-block flows | raise D_T, lower S_EXIT_ENTRY |
| FT7 planner (paper; per-block fills only, stride fills reported separately) | fills ≥ 97%; PriceLimitExceeded ≤ 2%; SlippageTooHigh ≤ 2%; shortfall error ≤ 0.1% + 25% of predicted; risk exits complete within U + M_A in ≥ 99% of drills | refit β quantiles (horizon h), K_CHASE |
| FT8 kill switches | false SUSPEND ≤ 5% on 1,000 bootstrapped paths of the sleeve's own backtest; true kill ≥ 80% within 45 d on cost-shifted paths; netting savings ≥ 0 | recalibrate; disable netting |
| FT9 router | chosen hotkey ≥ median eligible in ≥ 60% of subnet-months, switches net-positive | static rule (take 0, largest stake) |
| FT10 recovery | \|R_pred − R_obs\| ≤ 0.05 on SN116, SN82, SN108, SN35 | R = min(formula, 0.35) |
| FT11 drills (paper; blocking for live) | spec change, SafeMode, 2-min stall, finality lag 10, 30% RPC errors, stale ladder, unexplained stake delta, nonce jump: correct mode within 1 block and correct queued exits on recovery | blocks LIVE_ELIGIBLE |
| FT-R1 regime throttle | OOS net +0.03%/day or max DD −25%, with < 40% of days off | stays MONITOR |

### 10.5 Live adapter tests (no network, no bittensor; WP11)
- `FakeSdkPort` drives `LiveVenue` through:
  - crossed limit at submit;
  - plan() violations;
  - carrier absent from finalized N+2 → `OrderFailed(SHIELD_MISSED, tx_fee 0)` at once → retry on another free delegate → delegate locked until era end + 2;
  - carrier-fee settlement after era end for delegate nonce n (fee 0), n + 1 (fee = balance change) and ≥ n + 2 (inner found → key alarm; nothing found → key alarm);
  - carrier present but inner absent → miss with the carrier fee;
  - inner failure with fee paid (`ExtrinsicFailed`, via `extrinsic_events`);
  - `ProxyExecuted{Err}` under success → `OrderFailed` with the matching FailReason (SlippageTooHigh) or `PROXY_ERROR`, and `detail` = the decoded name;
  - partial fill;
  - a fill in a block that also has an epoch drain: Fill.shares and Fill.alpha come from share deltas and exclude the drain's yield;
  - two own orders landing in the same block (free-TAO apportioning);
  - reconcile from share deltas;
  - caps exceeded on a buy;
  - **a 5-TAO Tier A full exit with `max_order_tao = 1` and the daily turnover used up is submitted** (caps bound buys only);
  - full exit and hotkey move send the exact `PostState.alpha_value`, never `'all'`; a drain remainder is re-exited;
  - kill file;
  - fee float low;
  - nonce mismatch between the reserved and the SDK-used nonce → `SubmitUnknown("nonce_mismatch")` → resolved by the used nonce;
  - crash between SubmitStarted and ack with resolution by nonce;
  - UNARMED: arm token expired or spec not accepted → no buys or moves ever; with `risk_exits_when_unarmed` an EMERGENCY full sell is submitted only while V2/V3/V6 pass, and a HIGH or NORMAL sell is refused; with it off nothing is submitted and the T − 2 h and SPEC_CHANGED alerts fire while ladder exposure > 0;
  - key-alarm sources: `proxy_announcements` change, `coldkey_swap_scheduled`, a foreign StakeMoved and a `TransactionFeePaidWithAlpha` in `account_events`.
- Gate tests: every combination of the four locks except all-true refuses; any config edit invalidates the arm token; an expired token refuses, except that an authentic expired token starts UNARMED when `risk_exits_when_unarmed` is true; a Staking-only proxy set passes preflight while an extra Any, NonTransfer or Transfer proxy for the delegate fails; `RealPaysFee` on for any delegate fails; a lock on a held netuid fails; a delegate balance above `max_ops_balance_tao` fails.
- Linux CI contract test introspects bittensor 11.3.0 for `AddStakeLimit`, `RemoveStakeLimit`, `MoveStake`, `Policy`, `Client.plan`, `submit_shielded` and `prices.quote_stake`. It fails if the pin or the signatures change. It also asserts that **`Policy.check()` accepts, with no violations, the exact `RemoveStakeLimit` (partial and full-exit amounts) and `MoveStake` objects LiveVenue builds under the per-call sell/move Policy (`max_spend_tao=None`)**, and that the same objects with `'all'` are rejected under the buy Policy (documenting why live never sends `'all'`).
- Manual, user-run on test.finney: reads, quotes, plan(), one tiny shielded order per intent type, the shield inclusion and miss rate, and nonce-lockout behaviour.

### 10.6 Static gates
- `mypy --strict` on all packages.
- `ruff`.
- import-linter contracts (§4.2).
- AST lints:
  - no float or `/` on money-typed names in `protocol/amm.py`, `core/portfolio.py`, `portfolio/planner.py`;
  - no `Decimal.normalize`;
  - no unsorted set iteration in pure packages;
  - no `0.18` / `0.41` / `2952` / `14_400` / `720_000` literals outside tests and `protocol/regimes.py` comments.
- `mutmut` on `protocol/amm.py`, `core/portfolio.py`, `engine/reducer.py`, `portfolio/planner.py`; a surviving mutant fails review.
- Every dependency change needs a lockfile diff review.

---

## 11. Work packages for parallel implementation

**Rules:**
- Every file has exactly one owner. A WP codes only against §5 types and signatures, plus the implementations of the WPs it lists as dependencies.
- Cross-WP interface changes need an ADR approved by the lead.
- Each WP ships with tests, passes `mypy --strict`, ruff and import-linter, and runs on native Windows (WP11 excepted).
- Network tests are marked `@pytest.mark.network`.
- Waves:

| Wave | WPs | Can start when |
|---|---|---|
| 0 | WP0 (core types, skeleton, `ops/config_load.py`, `ops/secrets.py`) | now (lead) |
| 1 | WP1 chain reader, WP2 protocol replicas, WP3 storage + journal | WP0 merged |
| 2 | WP4 collector, WP5 features, WP6 venues, WP7 engine | their wave-1 dependencies merged |
| 3 | WP8 risk overlay + router + portfolio, WP9 strategies | WP2 + WP5 merged (`BookView` is §5 code, so WP8 and WP9 test against hand-built views and do not wait for WP7) |
| 4 | WP10 backtest/studies/reports, WP11 live adapter | WP1/3/4/5/6/7/8/9 (WP10); WP0/1/3/5/7/8/9 (WP11) |
| 5 | WP12 ops (logging, alerts, health, lock) + CLI + runbooks | all |

Config loading and secrets are wave-0 code because WP4 (keys), WP10 (books config) and WP11 (live config, arm-token secret) need them earlier; nobody writes a private loader.

### WP0: core types, package skeleton, pyproject (lead; wave 0)
- **Owns:**
  - `pyproject.toml`, `uv.lock`, `.importlinter`, `mypy.ini`, `ruff.toml`, `config/default.toml`, `config/preregistration.toml`;
  - `src/taotrader/__init__.py`, `__main__.py`, `src/taotrader/core/*` (all twelve modules), and every empty subpackage `__init__.py`;
  - `src/taotrader/ops/config_load.py`, `src/taotrader/ops/secrets.py`, `tests/ops/test_config_load.py`, `tests/ops/test_secrets.py`;
  - `tests/conftest.py`, `tests/core/*`, `tests/fixtures/golden/*`, `tools/capture_golden.py`.
- **Depends on:** nothing.
- **Details:**
  - Commit §5 verbatim. Implement `codec` (rules in §5.11), `check_invariants`, and the ledger helpers (`yield_txn`, `dereg_txn`, `fail_txn`, `capital_txn`, `carrier_fee_txn`).
  - `ops/config_load.py`: TOML → `core.config` dataclasses with strict validation (unknown keys rejected, units in names), precedence defaults < file < `TAOTRADER_*` env < CLI, the cross-field rule `unwind_exec_blocks == finality_lag_blocks + latency_blocks`, and `config_hash`.
  - `ops/secrets.py`: keyring / Windows Credential Manager, env fallback; values are never logged or put in argv. Both modules import only `core`, the stdlib and keyring, so every shell WP can use them (§4.2).
  - `pyproject.toml`: `requires-python = ">=3.11,<3.12"`, build backend uv_build, script `taotrader = "taotrader.cli:main"`.
  - Runtime deps pinned exactly. Initial pins, confirmed by `uv lock` at WP0 time: `websockets==15.0.1`, `httpx==0.28.1`, `xxhash==3.5.0`, `duckdb==1.2.2`, `numpy==2.2.6`, `zstandard==0.23.0`, `keyring==25.6.0`.
  - Extras: `collector = ["scalecodec==1.2.11"]`; `dev = [pytest, hypothesis, mypy, ruff, import-linter, mutmut]`, all `==`-pinned in uv.lock.
  - The `live` extra is **not** in pyproject. It lives in `requirements-live.txt` (WP11) so that Windows never resolves bittensor.
  - `uv.lock` is committed. CI uses `uv sync --frozen`.
  - `tools/capture_golden.py` uses plain httpx JSON-RPC (not the WP1 reader) to capture raw storage values and runtime-API results into JSON with block number and hash:
    - SN92 pool and dividend state at ≈ 9,240,388;
    - SN1 pool for the 1-TAO vector;
    - SN70/SN92/SN64 yield inputs;
    - SN51 emission state;
    - era-B SN1/SN19/SN64 at 7,000,020;
    - globals at 9,240,878;
    - 20 blocks ≥ 8,765,684 for emission parity.
  - `config/preregistration.toml` freezes every default and threshold in §2–§3 and §10.4. Its hash is journaled by `ConfigApplied`.
- **Acceptance:**
  - `uv sync` succeeds on Windows 11, Python 3.11.
  - `python -c "import taotrader.core.protocols"` works; mypy --strict is clean on core.
  - Codec property tests (exact Decimal, every journal event round trips).
  - Ledger transactions balance; FSM tests (including the illegal SUBMITTING → FILLED/EXPIRED); `check_invariants` detects each of its five violation classes.
  - Config round trip and rejection tests; the cross-field rule rejects a mismatched `unwind_exec_blocks`; secrets never appear in logs.
  - Import-linter contracts are active and pass on the skeleton.
  - The golden fixtures exist with a README listing block/hash provenance.

### WP1: chain reader (wave 1)
- **Owns:** `src/taotrader/chain/{hashing,scale,items,rpc,runtime_api,metadata,reader,head}.py`, `src/taotrader/chain/spec_defaults/*.json`, `tests/chain/*`, `tests/fixtures/cassettes/*`.
- **Depends on:** WP0 (`core.state`, `core.events.HealthObs`, `core.protocols.ChainReader/DataSource/SourceItem/SnapshotStore`, `core.errors`).
- **Details:**
  - Implement §6 exactly. The reader stores **raw chain values only**. It never computes derived quantities (rp before 7,135,420, emission and so on are computed in protocol/features).
  - It builds the era-correct `PoolState` (§6.8 step 6) and the Quality flags.
  - `LiveChainFeed(store, on_snapshot)` receives its SnapshotStore and the recorder callback by injection, so there is no import of `data/`.
  - `metadata.py`: an offline command (collector extra) extracts per-spec ValueQuery defaults and storage hasher layouts to JSON. At runtime only that JSON is read.
  - `verify-metadata` asserts every registry row's hasher and decoder width against the live spec. VERIFY items: SubnetOwnerHotkey, OwnerCut* map/value form, Delegates/ChildkeyTake/AlphaV2 hashers, EmissionBarRank width, SafeMode.EnteredUntil width, ShortsEnabled, TotalIssuance pallet, the per-subnet consensus-mode item (spec 475), and the metagraph items used for `MetagraphLite` (read only when `lcw.enabled`).
  - Snapshot membership is NetworksAdded: the reader includes exactly the non-root netuids whose NetworksAdded is true and asserts it (§6.8 step 8).
  - RU counters per endpoint are exposed for health and reports.
- **Acceptance:**
  - Hashing vectors pass.
  - Decoder golden tests pass, including exact SafeFloat, I96F32, U64F64 and Option by width.
  - Cassette replay of a FULL snapshot equals the golden decoded snapshot digest.
  - A removed-in-cleanup netuid (NetworksAdded false, NetworkRegisteredAt still set) and a queued-not-added netuid are excluded from `subnets`.
  - Network tests:
    - price parity ≤ 1e-6 vs `current_alpha_price_all` at 3 blocks;
    - `get_subnet_to_prune` parity;
    - era-B pool at 7,000,020 reproduces `sim_swap` ≤ 3e-7;
    - 6-block historical pull at ≥ 1.5 snapshots/s with a 3 req/s bucket.
  - Chaos tests: 429/−32029/−32004/WS drop/truncated body give bounded retries, rotation and no partial snapshot.
  - Gap fill ≤ 280 blocks from lite; stall detector.

### WP2: protocol replicas (wave 1)
- **Owns:** `src/taotrader/protocol/{amm,emission,ema,prune,yield_model,sellload,calibration,regimes,fees,derive}.py`, `tests/protocol/*`.
- **Depends on:** WP0.
- **Details:**
  - Implement the §5.12 signatures with the formulas in their docstrings, using Decimal (`core.fixed.DEC`) on the money path and the exact integer path at w = 0.5.
  - `regimes.REGIMES` holds the §8.6 table, and `regimes.SPECS` the per-spec `touches_econ` flags. Unknown-block entries (taoflow, spec475), the era-A fee and every `touches_econ` value are **not** filled by WP2 or WP4 directly: WP4 outputs the `spec_boundary` table (and the era-A fee measurement) plus an ADR, and the lead applies the ADR to `regimes.py`.
  - `emission.parity_ok` is the single emission-parity gate (EmissionView.model_ok and T2a).
  - `prune.fit_hazard` and `calibration.Calibration`/`CalibrationProvider` implement the as-of rule (§8.10); WP2 owns the fit functions, WP4 the lake-backed provider.
  - `sellload` implements both the pre- and post-v441 basket terms.
  - `derive_events` implements the §4.3 table, emitting events for FULL-only fields only when both snapshots carry them. `track_hotkeys` adds owner hotkeys and keeps tracked pairs sticky.
  - `HazardModel` holds the §3.3 CDF and invalidation flag.
- **Acceptance:**
  - All pure §10.1 vectors (AMM, slippage table, V_max, EMA half-life/warm-up, t\*, registration cost, CDF points, closed-form yield, injection split, recovery SN92/SN47 from golden fixtures, block emission curve).
  - §10.2 AMM properties.
  - `emission_vector` parity on the 20 golden blocks ≤ 1e-6 TAO/block.
  - `derive_events` netuid-reuse, removal-block and idempotence tests.
  - `parity_ok`, `fit_hazard` (as-of) and `track_hotkeys` (owner, sticky) unit tests (§10.1).
  - No literal protocol constants outside `regimes.py` (lint).

### WP3: storage and journal (wave 1)
- **Owns:** `src/taotrader/data/{schema,lake,store,replay,recorder,journal}.py`, `tests/data/{test_schema,test_lake,test_store,test_replay,test_recorder,test_journal}.py`.
- **Depends on:** WP0.
- **Details:**
  - §7 DDL.
  - `SqliteJournal` implements `core.protocols.Journal` with BEGIN IMMEDIATE, hash chain, triggers, anchor, `verify_chain`, and a projection rebuild (`orders_proj`) from the journal.
  - `lake.py` writes chunks atomically (temp + rename + manifest in one SQLite transaction) and exposes in-memory DuckDB views.
  - `store.py` implements `SnapshotStore` over the lake plus hot staging, with lookup by block and by digest and a `LookaheadError` guard.
  - `replay.py` implements `ParquetReplay(DataSource)`: stride, warm-up, merging of refinement rows, nominal health.
  - `recorder.py`: hot JSONL.zst with fsync per snapshot and hourly compaction to Parquet.
- **Acceptance:**
  - Atomic batch rollback on a duplicate idem; non-monotone blocks rejected; UPDATE/DELETE blocked; tampering detected with triggers dropped.
  - Snapshot → Parquet → snapshot round trip is digest-identical, including Decimal exactness of I96F32/SafeFloat.
  - Concurrent DuckDB readers while the recorder appends, on Windows.
  - A crash between the hot fsync and the journal commit leaves a consistent store.
  - `ParquetReplay` yields strictly increasing blocks and honours `after`.

### WP4: collector, refinement, optional decoders (wave 2)
- **Owns:** `src/taotrader/data/{collector,refine,calibration,events_decoder,taostats}.py`, `tests/data/{test_collector,test_refine,test_calibration,test_events_decoder,test_taostats}.py`.
- **Depends on:** WP0 (`ops.config_load`, `ops.secrets` for the Taostats and OnFinality keys), WP1 (ChainReader), WP2 (`fit_hazard`, `Calibration`), WP3 (lake, schema, store).
- **Details:**
  - Resumable schedules: 60-block from 8,486,594; 300-block from 4,920,351; refinement windows.
  - Coarse-to-fine ordering (600 → 300 → 60) so partial runs are useful.
  - `fetch_ledger` drives resume.
  - Calibration probes write `calib` and abort on drift: price every 50th snapshot, AMM vs `sim_swap` every 200th, prune target.
  - **Hotkey panel** at every epoch block **from 8,466,531** (post-June and gate eras, so S0/S1 have P×I there), plus, if S1 is to cover the Maymin window, at 300-block cadence from 4,920,351 (optional; §6.11). Tracked sets are selected **point-in-time** with `state_getKeysPaged` at each historical block hash (never today's membership), and tracking is **sticky**: once a (hotkey, generation) is tracked it stays tracked until the generation ends, so a backtest book never loses its index mid-position. Owner hotkeys are always tracked. If the pre-June panel is not collected, S0/S1 outside it are labelled price-only (§8.11).
  - Generation and registration tables are built from NetworksAdded/NetworkRegisteredAt/LastRateLimitedBlock diffs, with bisection to the exact removal block (the NetworksAdded flip) and capture of the removal−1 snapshot (`REFINED`).
  - `refine.spec_boundaries()` binary-searches `state_getRuntimeVersion` to find the setCode blocks for 334/338, 362 and 475, and verifies 421, 432, 440 and 441 against §8.6. **WP4 outputs the `spec_boundary` table plus an ADR; the lead (owner of `protocol/regimes.py` edits) applies it.** WP4 never edits `regimes.py`.
  - **Era-A fee:** measure it at ≈ 5 era-A blocks from StakeAdded tao/alpha against T/A, or from the era-A runtime constants, and report it in the same ADR (§8.2).
  - `calibration.py`: `LakeCalibrationProvider` (as-of fits of hazard, κ_p, R, Tier B jumps and φ from the registration and generation tables, using only events before the requested block; cached per refit point) and `FrozenCalibration` (the preregistered §3.3 values; paper and live only).
  - The owner-position and escrow series are collected.
  - `events_decoder.py` (optional, scalecodec): decodes System.Events per spec for owner-lock and claim enrichment. **Owner lock perpetual→decaying detection** is assigned here (§13 backlog); no v1 rule depends on it.
  - `taostats.py`: raw `Authorization` header, credit-aware limiter (5/min on the free tier), deep history walked by block ranges, `ext_*` tables only.
- **Acceptance:**
  - Kill-and-resume produces no duplicate chunks and an identical manifest.
  - Spec-boundary search reproduces 8,466,530/531 for spec 421.
  - The 52-prune generation table matches brief §4.4: blocks, netuids and Δreg.
  - The era-C 60-block backfill completes end-to-end (network, a few hours) with calibration error ≤ 1e-6.
  - The hotkey panel has no gap for any (hotkey, generation) between its first tracked block and the generation's end.
  - `LakeCalibrationProvider.asof(t)` is unchanged when registration rows after t are perturbed.
  - Taostats client tests use mocks; the key is read through `ops.secrets` (keyring/env) and never logged.

### WP5: features, YieldRouter, Gatekeeper, universe (wave 2)
- **Owns:** `src/taotrader/features/{engine,micro,yield_router,gatekeeper,universe}.py`, `tests/features/*`.
- **Depends on:** WP0, WP2.
- **Details:**
  - `FeatureEngine.update` fills every `Feat` field (§5.8):
    - rolling generation-keyed buffers truncated at reg_at;
    - median-of-3 60-block price;
    - local 600-block-half-life EMA of spot;
    - flow validity (None before 8,466,531 or across a generation);
    - robust z-scores;
    - emission and chain-buy via `protocol.emission`;
    - `sell_push_day` / `cb_push_day` via `protocol.sellload` with frozen priors;
    - closed-form and realised yields;
    - deterministic A_earn growth;
    - prune rank, ρ and t\* (stress D_STRESS), with the hazard and κ_p from the injected `CalibrationProvider.asof(block)`;
    - β buffers: q95/q99 of |ln p_t − ln p_{t−h}| over 1,800 blocks (30 stride points), h = finality_lag + latency_blocks when the previous snapshot is 1 block earlier, else h = the stride; blocks in `own_fill_blocks` are excluded (§3.12);
    - owner-sold estimates (§3.6 accrual), `owner_liquid_frac` and `top_holder_frac`.
  - Features are **book-independent only**. No feature reads a book's orders, fills, holdings or memory.
  - `warm` becomes true after 30 d of history. `state_digest` covers the buffers.
  - `yield_router.py` publishes `Feat.router_candidates` and `best_candidate` (§3.8 book-independent filters and score), including `TAKE_CHANGED` from Delegates diffs. The per-book choice is WP8's.
  - `gatekeeper.py` implements §2.4 states and flags.
  - `universe.py` implements §3.2 sections A–G and publishes counts; sections H and I are WP8's.
- **Acceptance:**
  - Replaying a mini-lake twice gives identical frame digests.
  - Flow-validity rules hold.
  - The candidate panel ranks a take-0 earner first on the SN70 fixture and flags a take increase, a permit-rank breach and a ratio breach.
  - β with h = 5 on per-block data and h = 60 on stride data reproduces hand-computed quantiles; own-fill blocks are excluded.
  - The Gatekeeper reproduces Queued→Added lags of 17–25 and netuid = victim on the **fixture** sequences (offline). The same check on the last 10 real registrations is a network test owned by WP10, which has WP1 and WP4 in scope.
  - t\* matches brute-force stepping.
  - Universe counts are published.

### WP6: execution venues (wave 2)
- **Owns:** `src/taotrader/venues/{sim,paper}.py`, `tests/venues/*`.
- **Depends on:** WP0, WP2 (amm, fees).
- **Details:**
  - `SimVenue` implements `ExecutionVenue` per §8.3–§8.5:
    - footprint overlay with `impact_half_life_blocks`;
    - latency `finality_lag + latency`;
    - TTL;
    - strict chain-rule failures with fees;
    - deterministic shield-miss injection;
    - MOVE_STAKE fills;
    - `observe()` rebuilds the queue and footprint from journal events;
    - one event per `advance()`;
    - idempotent `submit` on (order_id, attempt).
  - `reserve()` returns ("sim<i>", None, deterministic era_end) over `ExecCfg.n_delegates` simulated delegates, so delegate locks after a miss and retry rotation behave as live.
  - Shielded fills are legal only at `expected_fill_block` in paper and refined windows; stride replays fill at the next snapshot with `exact_block = False` (§8.4). Injected misses are declared at N+2 with the carrier fee.
  - `resolve()` re-derives the deterministic ack for an UNKNOWN order (PLACED + VenueAck), so a crash after SubmitStarted reproduces the crash-free fills.
  - `PaperVenue` extends it with the actual recorded fill-block state and the `sim_swap` drift probe through an injected `ChainReader`.
- **Acceptance:**
  - The 10-TAO / 1,000-TAO round trip reproduces the TEMPORARY vs PERSISTENT bracket (≈ 0.208 vs 0.012 TAO).
  - Every failure reason fires on a constructed case and pays the right fee.
  - Rebuilding from observe() gives identical future fills.
  - Miss injection is deterministic across runs.
  - The paper drift probe emits `ModelDriftObserved` above 5 bp (fake reader).

### WP7: engine, reducer, runner, recovery (wave 2)
- **Owns:** `src/taotrader/engine/{engine,reducer,runner,recovery,control}.py`, `tests/engine/*`.
- **Depends on:** WP0, WP2 (`derive_events`), WP3 (`SqliteJournal`, `SnapshotStore`); fakes for strategy, overlay, allocator, planner and venue in tests.
- **Details:**
  - `Engine.decide` implements the phases in §4.4: ACCOUNT emits YieldAccrued, and DeregSettled in backtest/paper/live-dry only (never in `RunMode.LIVE`); DECIDE handles cadence alignment, wake events, valid_from, the warm and data contracts, and standing-signal TTL; then it invokes router → caps → allocator → overlay → planner through the §5.10 protocols, replacing `ctx.book_view.router` after the router step. EMIT journals `DecisionTrace` (with `risk.router` memory, `calib_digest`, `nav_liq`, `sleeve_nav`), one `SleeveTransfer` per `TargetBook.transfers` entry, and `OrderIntended`*.
  - `reduce` maintains EngineState: portfolio, ledger balances, order records, strategy and router memories, standing signals, mode, bans/cooldowns, orphans, health, DISSOLVING positions, and everything `BookView` projects (orders window, recent fills, chase episodes, delegate locks, per-netuid failure counts on exact-block outcomes only, recent forced exits, daily NAV_liq samples, `SleeveStats`). `SleeveTransfer` moves sleeve shares and sleeve cash without ledger postings; `CarrierFeeSettled` is accepted on EXPIRED orders. It checks invariants after each batch and projects a frozen `BookView` for each tick.
  - `Runner`: tick (including the `own_fill_blocks` union passed to `FeatureEngine.update`), commit, drain, outbox (`reserve()` before `SubmitStarted`), recover (`SubmitUnknown("recovered_submitting")` for every SUBMITTING order before any resolve), checkpoints, the `--accept-drift` path, and operator control-file ingestion (`control.py`) as journaled `OperatorCommand`.
- **Acceptance:**
  - Crash matrix with fakes: identical final money digest at every fault point, no duplicate intents or fills; SUBMITTING + crash + resolve(LANDED) ends FILLED with no orphan.
  - Two runs give identical hash chains.
  - A restart re-verifies and writes nothing.
  - Deliberate nondeterminism raises `ReplayDivergence`.
  - An orphan fact quarantines without raising.
  - Cadence alignment is identical at stride 1 and 60.
  - Phase ordering: yield accrues before same-block fills.
  - `BookView` from checkpoint + tail equals `BookView` from a full fold; netting transfers keep invariant 3; a live DEREGISTERED marks DISSOLVING and a second `DeregSettled` is rejected.

### WP8: risk overlay, allocator, planner (wave 3)
- **Owns:** `src/taotrader/risk/{overlay,modes,prune_guard,hazard_mc,emission_guard,liquidity,owner_guard,regime_throttle,router}.py`, `src/taotrader/portfolio/{allocator,planner}.py`, `tests/risk/*`, `tests/portfolio/*`.
- **Depends on:** WP0, WP2, WP5. `BookView` is §5.10 code (WP0); tests use hand-built `BookView`s, and WP7's reducer fills them at integration.
- **Details:**
  - `StandardOverlay.review` implements §3.1–§3.11 in fixed rule order: modes → universe floor (A–G from the frame; **H cooldowns** from `book_view`) → prune → emission/burn/age → owner (incl. MONITOR m_owner) → liquidity and caps → **I aggregates**. Every rule emits a `RiskAction`. Mode and sleeve-kill rules read `book_view.nav_liq_daily` and `sleeve_stats`; it is constructed with the `CalibrationProvider` (κ_p, R, Tier B jumps as-of).
  - `router.py` (`RouterFn`) implements the per-book §3.8 choice: Q_MAX against own shares after the planned trade, 2-epoch hysteresis and switch triggers kept in `RouterState`, and the MOVE_STAKE decision through `TargetPosition.hotkey`.
  - `liquidity.py` holds the `CapsFn` (§3.5 V_cap, run before the allocator) and the section-I aggregates.
  - `hazard_mc.py` holds the seeded MC behind `tier_b_enabled`.
  - `regime_throttle.py` computes the monitored m_regime.
  - `allocator.py` implements §3.10: stage budgets, burn-in, sum-then-cap with the given caps, netting returned as `TargetBook.transfers`, hotkeys from the router.
  - `planner.py` implements §3.12: limits (tao_in first, then the limit), β, priority, dust, drain timing, chase rules from `book_view.chase`, delegate availability, unshielded fallback, one order per netuid, validity rules, deterministic ids via `make_order_id`.
- **Acceptance:**
  - Unit tests for each §3 rule.
  - The router switches per the hysteresis rules on synthetic sequences, and two books of different sizes on the same subnet choose different hotkeys when Q_MAX binds for one of them.
  - The Tier A coverage table (U = 15) is reproduced by the t\* code.
  - Backstop triggers at Δ ≥ 46,080.
  - The mode table is driven by HealthObs/ChainEvent fixtures.
  - §10.2 allocator and planner properties.
  - MC determinism under a fixed seed and block hash.
  - FT11 drill hooks (inject health and events) produce the right mode within one tick.

### WP9: strategies and baselines (wave 3)
- **Owns:** `src/taotrader/strategies/{base,carry,momentum,launch_lcw,baselines}.py`, `tests/strategies/*`.
- **Depends on:** WP0, WP2, WP5.
- **Details:**
  - Implement §2.1 (carry MVP; deferred modules present but behind flags defaulting OFF), §2.2 (momentum MVP; P1/gap/struct/rotation/breadth/reversal behind flags) and §2.4 (LCW behind `lcw.enabled = False`).
  - Baselines per §2.5; the random-entry placebo is seeded from (seed, block_hash).
  - Each strategy declares `id`, `decide_every_blocks`, `wake_on`, `min_cadence_blocks`, `valid_from_block` and `declares_dilution`. It has a frozen `Params` dataclass validated from `SleeveCfg.params` and a frozen `Memory` dataclass.
  - Floats appear only in feature math; `to_ppm` is applied at the Signal boundary.
  - Book-dependent inputs come only from `ctx.book_view`: carry's yield hotkey is `book_view.router.hotkey(key)` (else `Feat.best_candidate`), and C-U9 reads `book_view.recent_forced_exits`.
- **Acceptance:**
  - Purity lint passes.
  - Hand-computed carry μ, V\* and μ_net on a fixture frame.
  - The momentum gate rejects a trade that the PERSISTENT bound would admit.
  - LCW rejects on each failed gate, with reason codes.
  - Memory round trips through the codec.
  - Baselines produce the expected weights.

### WP10: backtest harness, studies, reports, integration tests (wave 4)
- **Owns:** `src/taotrader/backtest/{books,runner,metrics,stats,studies,bias}.py`, `src/taotrader/reports/{html,tables}.py`, `config/books.backtest.toml`, `tests/backtest/*`, `tests/integration/*`, `tests/fixtures/minilake/*`.
- **Depends on:** WP0 (`ops.config_load`), WP1, WP3, WP4, WP5, WP6, WP7, WP8, WP9.
- **Details:**
  - `books.py` expands `config/books.backtest.toml` (loaded through `ops.config_load`) into sleeve × baseline × impact × dereg books, and wires the `LakeCalibrationProvider` into every FeatureEngine and overlay.
  - Owns the network acceptance test moved from WP5: the Gatekeeper reproduces Queued→Added lags of 17–25 and netuid = victim on the last 10 real registrations.
  - Reports split limit-failure rates by `exact_block`, and label pre-8,466,531 S0/S1 windows price-only unless the optional panel exists (§8.11).
  - `runner.py` runs single passes and the process-pool grid, with the trial registry.
  - `metrics.py` and `stats.py` implement §8.9: NAV_liq, decomposition identity, NW t, stationary block bootstrap, deflated Sharpe, CSCV, capacity sweep.
  - `studies.py` implements S0–S7 (§8.11) with pre-registered thresholds and pass/fail output.
  - `bias.py` holds the canaries and placebos.
  - Reports are static HTML (inline SVG, no JS) plus CSV, carrying the no-advice statement, trial count, regime table, both impact bounds, universe counts and the price/yield/fee/dereg attribution as the first chart.
  - The mini-lake is ≈ 1,070 snapshots from 8,765,684, committed with the digest.
- **Acceptance:** the §10.3 integration suite is green: golden replay digest, prune replay, event waves, crash matrix with real SimVenue, determinism, no-lookahead (including the as-of calibration perturbation test), paper↔offline equality harness, cadence invariance. The Gatekeeper network test passes. The S0 report runs end-to-end on the backfilled lake.

### WP11: live adapter (wave 4; Linux/WSL only)
- **Owns:** `src/taotrader/live/{gate,preflight,sdk_port,venue,reconcile,nonce}.py`, `requirements-live.txt`, `config/live.example.toml`, `tests/live/*`.
- **Depends on:** WP0 (incl. `ops.config_load` and `ops.secrets` for the live config and the arm-token HMAC secret), WP1 (`LiveChainFeed`, `ChainReader`), WP3 (journal, `state.sqlite` `live_submissions`), WP5 (FeatureEngine), WP7 (Runner, reducer), WP8 (router, overlay, planner intents), WP9 (strategies).
- **Details:**
  - §9.2–§9.8: four-lock gate with HMAC arm token, the UNARMED state and its risk-exit exception, preflight (incl. RealPaysFee, locks, `max_ops_balance_tao`, MIN_FREE_REAL), `SdkPort` (§9.4) with `RealSdk` (the only `import bittensor`) and `FakeSdkPort`, `LiveVenue` (reserve, submit, advance, resolve, observe; buy-only caps; exact amounts; per-call policies; N+2 miss rule; carrier-fee settlement; share-delta fills), reconciliation with ReconAdjusted and the key alarms, delegate rotation and nonce locks, live-dry plan-only mode.
  - `requirements-live.txt` pins bittensor 11.3.0 with its hash.
  - `config/live.example.toml` has `enabled=false`, `mode="plan_only"` and an explicit, commented `risk_exits_when_unarmed = false` that the user must decide on, and never holds secrets.
- **Acceptance:**
  - §10.5 suite green on Linux CI.
  - Static check: no bittensor import outside `live/`; no forbidden intents referenced anywhere.
  - A dry run against test.finney is documented as a user-run procedure (not executed by CI or agents).

### WP12: ops, CLI, scripts, runbooks (wave 5)
- **Owns:** `src/taotrader/ops/{logging,alerts,health,lock}.py`, `src/taotrader/cli.py`, `scripts/*`, `config/books.paper.toml`, `docs/runbooks/*.md`, `tests/ops/*` (except WP0's `test_config_load.py` and `test_secrets.py`).
- **Depends on:** all.
- **Details:**
  - `config_load` and `secrets` are WP0's; WP12 only uses them.
  - `logging`: JSON lines with a redaction filter.
  - `alerts`: webhook / Windows toast / log sinks, dedupe with cooldown, dead-man ping.
  - `health`: `status.json` heartbeat with block, lags, mode, open orders, NAV and journal head hash.
  - `lock`: single-instance lock (msvcrt/fcntl).
  - `cli` commands:
    - data: `collect`, `refine`, `verify`, `verify-metadata`, `verify-journal`, `verify-lake`;
    - research: `backtest`, `grid`, `study`, `report`;
    - running: `paper`, `replay`;
    - operator: `halt`, `resume`, `exits-only`, `flatten`;
    - live: `live arm`, `live --live [--submit]` (lazy import of `taotrader.live` after the gate);
    - `doctor` (on the live host, `doctor --live` also checks the last arm time and raises the idle-proxy alert of §12.2).
  - `scripts/register-tasks.ps1` registers the Task Scheduler entries. `scripts/taotrader-live.service` is the systemd unit for the user, and `scripts/taotrader-doctor.timer` runs `doctor --live` daily.
- **Acceptance:**
  - CLI wiring tests (every command parses, loads config through WP0's `config_load`, and fails closed on bad input).
  - Secrets never appear in logs, including through the redaction filter (test).
  - `doctor --live` raises the idle-proxy alert when the last arm is older than 72 h (fake clock).
  - A second paper instance is refused by the lock.
  - A 1-hour paper smoke run against public endpoints (network) shows heartbeat, recorder and journal growth with zero invariant breaches.
  - Runbooks reviewed by the lead.

---

## 12. Ops runbook outline (full runbooks in `docs/runbooks/`, WP12)

### 12.1 Windows 11: collector, backtests, paper (native)
- **Setup:**
  - `uv sync --frozen` (Python 3.11);
  - `uv sync --extra collector` on machines that extract metadata or decode baskets;
  - the project lives at `%USERPROFILE%\Trading\tao-subnet-trader`; `data\`, `logs\` and `reports\output\` are git-ignored.
- **Host settings the user applies by hand (the bot never changes system settings):**
  - keep `data\` outside OneDrive;
  - optionally exclude `data\` from Defender real-time scanning for SQLite WAL and Parquet throughput;
  - disable sleep on AC for the paper machine (`powercfg /change standby-timeout-ac 0`);
  - set Windows Update active hours.
  Reboots are survivable: recovery is journal replay plus gap fill.
- **Scheduled tasks** (`scripts/register-tasks.ps1`, run by the user):
  - `tao-collect` daily 04:00 and at logon (`taotrader collect --catch-up`);
  - `tao-paper` at logon, restart on failure every minute (`taotrader paper --books config\books.paper.toml`);
  - nightly `verify-journal`, `verify-lake`, paper↔offline replay equality, and `VACUUM INTO` backups of journals and state to a second disk;
  - weekly `robocopy` of immutable lake chunks plus `verify-lake`;
  - weekly report (data quality, model drift, prune watch, paper components).
- **Single instance** per (mode, run): a lock file. Windows specifics: default Proactor loop, SIGBREAK handling, no `add_signal_handler`.

### 12.2 WSL2 or VPS: live (user-run)
- **Preferred host:** a small Linux VPS for 24/7. WSL2 sleeps with Windows.
- **Setup:**
  - separate venv: `pip install --require-hashes -r requirements-live.txt`;
  - check bittensor 11.3.0 provenance before the first install;
  - chrony for time sync (shield era window);
  - journal and state on ext4;
  - lake mirror by rsync from Windows.
- **Service:** `systemd` unit `taotrader-live.service` with `Restart=on-failure`, `RestartSec=5`, `ProtectSystem=strict`, `ReadWritePaths` limited to data and logs, and `EnvironmentFile=/etc/taotrader/live.env` (mode 600).
- **Keys:**
  - the coldkey stays on a Ledger or Polkadot Vault;
  - the user signs the one-time Staking-proxy addition (`btcli proxy add --delegate ops --proxy-type Staking --delay 0`) for each of 2–3 delegates;
  - only delegate key files live on the host (chmod 600), each with a small TAO fee buffer (≤ `max_ops_balance_tao`);
  - per-strategy coldkeys holding only that strategy's capital limit the blast radius;
  - **idle proxies** (brief risk #8): a zero-delay Staking proxy stays active on chain after the arm token expires, and a stolen delegate key can still force value-draining round trips. If live has not been armed for more than 72 h, `taotrader doctor --live` (daily systemd timer) alerts **"remove Staking proxies (`btcli proxy remove`)"** and lists the configured delegates. Re-adding them later is a one-time coldkey signature per delegate;
  - **single-position signature** (brief risk #10): a coldkey that holds a single alpha position reveals trade direction even when shielded, because the signer and timing stay visible. Per-strategy coldkeys should hold at least 2 positions, or the user accepts the leak explicitly.
- **Ladder:** `live-dry` for ≥ 7 days with 100% `plan()` acceptance, then the user arms with `taotrader live arm`, sets `TAOTRADER_LIVE_ARMED`, and runs `taotrader live --live --submit`. The arm token expires in ≤ 24 h, so re-arming is a deliberate daily act.
- **Lapsed arming:** when the token expires or a new spec is not yet accepted, live is UNARMED (§9.3). The user decides once, in `live.example.toml`, whether `risk_exits_when_unarmed` lets it still submit EMERGENCY/URGENT full sells while V2, V3 and V6 pass. With it off, the bot alerts at T − 2 h before expiry and on every spec change while ladder exposure is above 0, listing held positions and their t\*.
- **First steps:** test.finney smoke tests (reads, quotes, plan(), one tiny shielded order per intent type), measuring the shield miss rate and nonce behaviour.

### 12.3 Taostats key and MCP
- The **Taostats MCP** is for Claude-side research only. It is never a bot dependency and never on the decision path.
- The **Taostats REST key** (when it arrives) goes into keyring or Windows Credential Manager as `taotrader/taostats`, or the env var `TAOSTATS_API_KEY` in `%USERPROFILE%\.taotrader\secrets.env` with an `icacls` ACL restricted to the user.
- Uses:
  - `taotrader collect --enrich` (trades and stake-events for T3 miner-coldkey flows and F11 flow validity);
  - `taotrader verify --taostats` (price cross-check at block % 300 == 0).
- Rules: raw `Authorization: <key>` header; free tier 5 credits/min; per-endpoint credit costs are unpublished, so start conservatively. Pool history splices generations: join by block only.

### 12.4 Secrets and keyed providers
- Never in the repo or argv. Logs pass through a redaction filter (keys, ss58 of the real coldkey optional).
- **Secrets inventory:**
  - Taostats key;
  - OnFinality key (an `https://…/rpc?apikey=…` URL stored in keyring; recommended for the one-off backfill and the live head);
  - alert webhook URL;
  - (live host only) delegate wallet password file (`BT_WALLET_PASSWORD_FILE`) and the arm-token HMAC secret in the OS keyring.
- The arm token itself is set per session.

### 12.5 Alerts and monitoring
- **Sinks:** webhook (Discord, Telegram or generic), Windows toast, log. Dedupe with cooldown. A dead-man ping every minute to a healthcheck URL.
- **Alerts:**
  - mode changes;
  - invariant breach or orphan;
  - model drift or provider disagreement;
  - spec, tx-version or freeze-list change;
  - SafeMode or stall;
  - a held name entering prune rank ≤ 3, being disabled or being dissolved;
  - the registration window opening while ladder exposure > 0;
  - fee float low;
  - any live order outcome;
  - arm-token expiry at T − 2 h, and every SPEC_CHANGED while UNARMED, when ladder exposure (prune_rank ≤ 15) is above 0 and `risk_exits_when_unarmed` is off, listing held positions with their t\*;
  - live not armed for > 72 h: "remove Staking proxies (`btcli proxy remove`)", listing the delegates;
  - a daily summary of NAV_liq, components, universe counts and P&L attribution.

### 12.6 Runbooks (one page each)
| Runbook | Covers |
|---|---|
| crash-recovery | `taotrader replay --recover`, ReplayDivergence handling, `--accept-drift` |
| spec-change | V1–V7, updating `accepted_specs` and fee tables, the `touches_econ` ADR, regime-table ADR, re-arming, what UNARMED does meanwhile |
| prune-emergency | manual flatten, verifying Tier A outcomes, post-dissolution payout reconciliation |
| key-compromise | revoke the proxy from the coldkey signer, rotate delegates, reconcile, QuarantineCleared; remove idle Staking proxies when live has not been armed for > 72 h (`btcli proxy remove`, delegates listed by `doctor --live`) |
| provider-outage | endpoint rotation, keyed fallback, stale-prune CAUTION behaviour |
| safe-mode | what is frozen, the post-SafeMode exit queue |
| live-arming | the four locks, preflight failures and their meaning (incl. RealPaysFee, locks, `max_ops_balance_tao`); the UNARMED state and the `risk_exits_when_unarmed` decision; idle-proxy removal after 72 h unarmed; the single-position signature leak and the ≥ 2-position recommendation for per-strategy coldkeys |

---

## 13. Open questions to resolve at build time
| # | Question | Owner | Resolution path |
|---|---|---|---|
| 1 | Exact names, map-vs-value form and hashers of `SubnetOwnerHotkey`, `OwnerCutEnabled`, `OwnerCutAutoLockEnabled`, `Delegates`, `ChildkeyTake`, `AlphaV2`/`Alpha`, `EmissionBarRank` width, `SafeMode.EnteredUntil` width, `ShortsEnabled`, `TotalIssuance` pallet, `DissolveCleanupQueue` element type | WP1 | `verify-metadata` against spec-475 metadata; registry rows updated |
| 2 | Spec-475 setCode block; what "precise emissions", Null consensus and PoW registration change in emission, dividend and registration code | WP4 + lead | boundary search; re-run T2a, T6, FT1/FT2 on post-475 blocks; registration-economics check for `HazardModel.valid` |
| 3 | Does `SubnetTaoFlow` include `claim_root` sales, and will a runtime reset or migrate it (brief Q14)? | WP4 | compare ΔSubnetTaoFlow with Δreserves minus emission on claim-burst blocks (9.15M–9.19M) |
| 4 | SDK 11.3.0 client shape (blocking vs async), exact intent signatures, that `Policy.check()` accepts exact-amount RemoveStakeLimit/MoveStake under `max_spend_tao=None`, and that `submit_shielded` returns or exposes the carrier nonce (brief Q1). Raw calls are not used (`allow_raw_calls=False`) | WP11 | Linux contract test plus test.finney |
| 5 | Shield never-included rate and nonce-lockout behaviour after a miss (brief Q2) | user + WP11 | test.finney logs, then tiny mainnet probes (user approval) |
| 6 | Fees of Proxy(move_stake) same-subnet and Proxy(move_stake_limit) | WP11 | plan() fee quotes on test.finney; update `ExecCfg` |
| 7 | `BetaBasketRuntimeApi` / `StakeInfoRuntimeApi` decoding with scalecodec on Windows; escrow history from 8,765,684 | WP1/WP4 | prototype decode at 3 blocks; else forward-fill plus staleness rule |
| 8 | Typical finality lag on finney (sets U and the shield-fallback threshold) | WP1 | measure for 7 days |
| 9 | OnFinality public range-call limits (`state_queryStorage`) and whether `state_call` is weighted more | WP4 | measured probes before sizing the MR study and FT1b windows |
| 10 | Dissolution payout precision incl. pending emission and basket chunking (brief Q10) | WP10 | FT10 replay of SN116, SN82, SN108, SN35 |
| 11 | Taostats credit cost per endpoint and tier gating (brief Q7) | user + WP4 | first key: probe with a credit counter |
| 12 | Miner-coldkey flow measurement for T3 without an event indexer | WP4/WP10 | Taostats stake-events once the key arrives; interim: top-5 miner coldkey positions per epoch |
| 13 | Metagraph storage names (Incentive, ValidatorPermit, Keys) for LCW miner quality | WP1 | metadata check; otherwise `get_selective_metagraph` via scalecodec |
| 14 | Balancer weight drift from 0.5 and superellipse pools (PR #3211) live or not (brief Q16–17) | WP6 | paper sim_swap drift probe; V2 suite |
| 15 | Emission-enable criteria and cadence (brief Q6); whether weekly root reviews continue | lead | monitor SubnetEmissionEnabledSet history; affects LCW only |
| 16 | Σ EMA trending toward 1 (root sell flag off stops escrow inflow and changes A_earn growth and sell load) | WP5 | monitored in `EmissionView.root_flag`; models already switch on it |
| 17 | User decisions: capital range for defaults and the capacity grid; VPS vs WSL2 for 24/7 paper/live; keyed OnFinality plan for the backfill; Taostats plan; `risk_exits_when_unarmed`; whether to collect the optional pre-June hotkey panel | user | config only; nothing else in the design depends on the answers |
| 18 | Tax-lot reporting format (YieldAccrued per epoch is the income record) | user | export only; not advice |
| 19 | Storage/read names for `RealPaysFee(real, delegate)` (from the `Proxy.set_real_pays_fee` dispatch), `stake_availability(real, n).locked`, the scheduled-coldkey-swap item, and `Proxy.Announcements` layout | WP11 | spec-475 metadata plus the SDK on the live host; preflight and key alarms fail closed until confirmed |
| 20 | Per-subnet consensus-mode item (Null consensus) name, key and enum | WP1 | spec-475 metadata; until confirmed `consensus_mode` stays None and PARAM_CHANGED cannot fire on it |
| 21 | Era-A swap fee (Swap.FeeRate absent before v3) | WP4 + lead | ≈ 5 era-A blocks of StakeAdded tao/alpha vs T/A, or era-A runtime constants; ADR into `fee_rate_default` |
| 22 | Recipient of validator-take credits: are they new shares for the hotkey's owning coldkey, and is that the subnet owner coldkey for owner hotkeys (owner-sale accrual, §3.6)? | WP2 + WP4 | `distribute_dividends_and_incentives` source plus an owner-position diff across one drain |
| 23 | **Backlog:** owner lock perpetual→decaying event detection (brief §4.8, risk #7) | WP4 (`events_decoder.py`) | v2 enrichment; no v1 rule depends on it |
