# Bittensor dTAO micro-cap subnet-alpha trading: engineering research brief

**As of:** 2026-10-08. Finney mainnet is at specVersion 475, head about block 9,240,300–9,240,900. Repo `main` is at spec 476, which is proposed but not live.

**Scope:** what a developer needs to build a Python system that trades and rebalances small and micro-cap subnet alpha tokens.

**Evidence standard:** facts come only from the verified corpus. Where a finding was corrected, the corrected value is used. Items marked **(UNVERIFIED)** are single-source or could not be checked. A "common misconception" is a claim the corpus refutes.

**Disclaimer:** this is engineering research, not financial, investment or tax advice.

---

## 1. Executive summary

1. **The platform has moved, and so has the code.** `github.com/opentensor/subtensor` now 301-redirects to `RaoFoundation/subtensor`. That repo is a monorepo holding the chain, the Python SDK under `sdk/python`, btcli and the docs. `opentensor/bittensor` (now `RaoFoundation/bittensor`) was archived on 2026-07-10. `docs.learnbittensor.org` redirects to `bittensor.com/docs`. The Python package is **`bittensor==11.3.0`** (released 2026-10-07, Python 3.10–3.14). It uses an intent-based API that is incompatible with v10. Runtime releases arrive about every 3 days (about 30 between 2026-06-29 and 10-08). **Treat on-chain storage as authoritative.** Several docs constants are stale.

2. **Each subnet is one protocol-owned weighted (Balancer) pool.** The reserves are `SubnetTAO` (y) and `SubnetAlphaIn` (x). Weights are in `Swap.SwapBalancer`, and every live pool is at about 0.5/0.5, so the pools behave as constant product today. User liquidity is permanently disabled. The swap fee is `FeeRate/65535` (33, about 0.0504%, on all 128 subnets). It is taken from the input and paid to the block author. Staking means buying alpha and unstaking means selling it. There is **no unbonding, no stake cooldown and no per-block staking rate limit**.

3. **TAO emission is 0.5 TAO/block (3,600/day) since the first halving** at block 7,103,975 (2025-12-15). Taoflow no longer decides how this is split across subnets. The split follows each subnet's **price EMA (`SubnetMovingPrice`)**, scaled by (1 − `MinerBurned`). The result then passes through a **Hill-function emission gate** centred on the 32nd-largest share (h = 3). The top 8 subnets receive about 53% of emission, the top 32 about 97%, and the bottom third by pool size a median of 0 TAO/day. The EMA's on-chain smoothing is 0.0003 per block, which gives a **half-life of about 8 hours** on mature subnets. So emission share and prune rank react to price within hours.

4. **Most emission reaches the pool as a protocol market bid.** Each subnet's TAO emission is injected as `tao_in` plus `alpha_in` at spot, which does not move the price. Injection is capped by the root proportion, and the excess is used for fee-free "chain buys" of alpha that accrue to `SubnetProtocolAlpha`. Live split: about 1,131 TAO/day of injection and about 2,468 TAO/day of chain buys.

5. **Every started subnet mints 1 alpha per block (7,200/day) whatever its TAO share.** The split is 18% owner, 41% miners and 41% validators/root, paid at tempo drains (usually every 360 blocks). Tail subnets below the gate therefore face **dilution and sell flow with almost no protocol bid**. Owner-cut auto-lock defaults to off and is enabled on only 13 subnets, so owner alpha is liquid on about 115.

6. **Staked alpha also earns yield.** Nominator dividends compound into a hotkey share-price index I = `TotalHotkeyAlpha/TotalHotkeyShares`, credited at each epoch drain. Measured net-of-take yields on 2026-10-08 were about **0.3–0.6%/day on micro caps** versus about 0.08–0.11%/day on large caps. Total return in TAO = (P₁/P₀)·(I₁/I₀) − costs. Leaving out the yield can flip the sign of a size factor.

7. **Deregistration is the main micro-cap risk.** The cap is 128 subnets and all slots are full. Each new registration (at most one per 14,400 blocks network-wide; current lock about 963–969 TAO) **dissolves, in the same block, the non-immune subnet with the lowest `SubnetMovingPrice`**. Immunity is 864,000 blocks, about 120 days. There have been 52 prunes since 2025-10-19, about one a week. Holders are paid pro rata from the pool's TAO after validator basket holdings are sold, at roughly **0.35–0.65× spot**. The mempool gives no warning: 6 of the last 10 pruning registrations were MEV-shielded. The current target is **SN92**.

8. **Root has discretionary switches.** It can set `SubnetEmissionEnabled = false`: 54 subnets were switched off in one block on 2026-06-22, and 6 are off today (29, 35, 36, 82, 108, 116). New subnets start with emission off. It can force SafeMode, which blocks all staking, or dissolve a subnet. The emission rule itself changed about 6 times in 11 months, with little notice.

9. **Production execution path:** a Staking proxy (ProxyType index 8) signs `AddStakeLimit` / `RemoveStakeLimit`, submitted through **`client.submit_shielded()`**. Plain `execute()` never MEV-shields. The limit price bounds the pool's **marginal price after the fill**. v11 intents default to a 5% fill-or-kill bound, which caps a buy at about 2.47% of the TAO reserve. Shielded orders land at block N+2 (about 24 s), and 98.9% of included carriers decrypt and execute. All-in fee is about τ0.00103 per buy and about τ0.00083 per sell, plus 0.05% swap fee per leg.

10. **Market structure: micro caps are small, mostly new or dying, and have recently underperformed.** A practical micro-cap cutoff is pool TAO below about 3,000 (bottom quartile, under about $0.8M at TAO ≈ $268). The bottom decile is below about 1,000–1,150 TAO. Slippage against spot is Δ/(T+Δ). Since the June 2026 return to price-based emission, the price-only small-minus-big premium has **reversed** to about −0.5%/day (t ≈ −2). 1-day continuation and 7-day momentum stay positive, gross of costs. Seeded new launches fall a median 68–74% by day 90 (0 of 11 positive). Capacity is tiny.

---

## 2. AMM and swap math

### 2.1 Pool state and spot price
Source: `pallets/swap/src/pallet/balancer.rs`; the formulas were checked against on-chain `sim_swap`.

| Symbol | Storage | Encoding |
|---|---|---|
| x (alpha reserve) | `SubtensorModule.SubnetAlphaIn[netuid]` | u64 rao, Identity hasher on u16 netuid |
| y (TAO reserve) | `SubtensorModule.SubnetTAO[netuid]` | u64 rao |
| w₂ (quote = TAO weight) | `Swap.SwapBalancer[netuid].quote` | Perquintill: first 8 bytes u64 / 1e18; Twox64Concat key |
| w₁ (base = alpha weight) | 1 − w₂ | — |
| f (fee rate) | `Swap.FeeRate[netuid]` | u16, default 33; absent key means default |

- **Spot price** p = (w₁/w₂)·(y/x) TAO per alpha. Invariant L = x^w₁·y^w₂.
- Weights are bounded at `MIN_WEIGHT` = 0.01. The code comment says [0.1, 0.9]; it is stale, so trust the constant. Live quote weights on all 128 pools are 0.49999974–0.5, so p ≈ y/x today. **Do not hard-code 0.5.** Weights are re-solved on every protocol injection (§2.9).
- Runtime: `SwapRuntimeApi_current_alpha_price(netuid)` returns floor(p·1e9) as a u64 (rao per alpha).

### 2.2 Fees
- **Swap fee:** `fee = floor(amount_in · FeeRate / 65535)`, taken from the **input**. FeeRate is 33 (≈0.0504%) on every subnet. Root can set it with `set_fee_rate`, up to `SwapMaxFeeRate` = 10,000 (15.26%).
- **Destination:** 100% to the block author. A TAO fee is transferred directly. An alpha fee is first swapped fee-free into the same pool, then the TAO goes to the author. If there is no author, the fee is burned.
- **No swap fee:** moves within one subnet between hotkeys (no swap), root (netuid 0) staking (1:1), and protocol chain buys (`drop_fees = true`).
- **Cross-subnet move/swap:** two pool legs, but the fee is charged once.
- The **extrinsic transaction fee** is separate, paid in TAO and recycled (§5.12).

### 2.3 Exact swap outputs
Source: `swap_step.rs`, `impls.rs`, `stake_utils.rs`.

**BUY (stake TAO → alpha):**
```
fee  = floor(tao_in * f / 65535)
dy   = tao_in - fee
alpha_out = x * (1 - (y / (y + dy)) ** (w2 / w1))      # w=0.5: x*dy/(y+dy)
new reserves: y' = y + dy ; x' = x - alpha_out          # fee leaves the pool to the author
```
**SELL (unstake alpha → TAO):**
```
fee_a = floor(alpha_in * f / 65535)
dx    = alpha_in - fee_a
tao_out = y * (1 - (x / (x + dx)) ** (w1 / w2))        # w=0.5: y*dx/(x+dx)
x1, y1  = x + dx, y - tao_out
# chain then sells fee_a fee-free into the same pool and pays the TAO to the author:
tao_fee = y1 * (1 - (x1 / (x1 + fee_a)) ** (w1 / w2))
final reserves: x = x1 + fee_a ; y = y1 - tao_fee        # the pool absorbs the full alpha_in
```

### 2.4 Guards and validation that cause failures

| Check | Rule | Error |
|---|---|---|
| Output reserve before the swap | must be ≥ `MinimumReserve` = 1,000,000 rao | `ReservesTooLow` |
| Net input size | ≤ 1,000 × input-side reserve | `SwapInputTooLarge` (swap pallet) |
| Subtensor pre-check | buys: amount ≤ 1000·SubnetTAO; sells: ≤ 1000·SubnetAlphaIn | `InsufficientLiquidity` |
| Output | paid_out ≤ reserve | `InsufficientLiquidity` |
| Minimum stake | gross ≥ `DefaultMinStake` (2,000,000 rao = 0.002 TAO) + fee, **and** post-fee amount_paid_in ≥ 0.002 TAO | `AmountTooLow` |
| Partial unstake | if a position remains, the simulated TAO out **of the amount being sold** must be ≥ 0.002 TAO. Full exits are exempt. | `AmountTooLow` |
| Nominator dust | after any remove-type call, a remaining nominator position (coldkey ≠ hotkey owner) worth less than `NominatorMinRequiredStake` (raw factor 10,000,000 per-million × DefaultMinStake = **0.02 TAO**, compared in alpha at spot) is **force-sold** at min_price, with no price limit, and its locks are force-reduced | — |
| Buying | requires `SubtokenEnabled` (start_call done) | `SubtokenDisabled` |
| Selling | does **not** require SubtokenEnabled; a cross-subnet move or swap requires it on **both** subnets | — |

Practical rule: exit positions fully, or keep the remainder at least 0.02 TAO-equivalent.

### 2.5 Price impact closed forms (w = 0.5)
- Buy Δ TAO into reserve T: shortfall against spot = Δ′/(T+Δ′), where Δ′ = Δ(1−0.000504). Average price = p·(1+Δ′/T). **Post-trade spot = p·(1+Δ′/T)²**.
- Sell Δx alpha: average = p/(1+Δx/x). Post-trade spot = p/(1+Δx/x)².
- Selling a position worth V TAO at spot realises V·T/(T+V). **Maximum position for exit slippage s: V_max = T·s/(1−s)**, i.e. 1% → 1.01% of T, 2% → 2.04%, 5% → 5.26%, 10% → 11.1%.

| Pool TAO | 1 TAO | 10 TAO | 100 TAO | Spot move after 100-TAO buy |
|---|---|---|---|---|
| 300 | 0.33% | 3.23% | 25.0% | +78% |
| 500 | 0.20% | 1.96% | 16.7% | |
| 1,000 | 0.10% | 0.99% | 9.09% | +21% |
| 2,000 | 0.05% | 0.50% | 4.76% | |
| 3,000 | 0.03% | 0.33% | 3.23% | |
| 6,700 (median) | 0.015% | 0.15% | 1.47% | |

Add 0.05% fee per side. Live check: 100 TAO into SN92 (587 TAO pool) gave 63,351.67 alpha with 10,820.70 alpha of impact (14.6%). 100 TAO into SN51 lost 1.08 alpha.

### 2.6 Limit-price sizing and semantics
- `limit_price` is in **rao per alpha (price × 1e9)**. It caps the pool's **marginal spot price after the fill**, not the average fill price.
- Largest input before the marginal price reaches limit p′:
  - buy: Δy = y·((p′/p)^w₁ − 1)
  - sell: Δx = x·((p/p′)^w₂ − 1)
- When the limit binds, fee = δ·f/(65535 − f).
- `allow_partial=False` makes the order fill-or-kill: if amount > max_amount it fails with `SlippageTooHigh`. `allow_partial=True` fills up to the limit and refunds the unswapped TAO to the coldkey, or leaves the unsold alpha staked.
- **A limit already crossed at execution fails outright** with `Swap::PriceLimitExceeded`, even with `allow_partial=True`. The check is strict: a buy needs spot < limit, a sell needs spot > limit. A fillable amount below 0.002 TAO fails with `AmountTooLow`.
- For move/swap limits, limit = origin_price/dest_price × 1e9, the minimum destination-alpha per origin-alpha. The chain's `get_max_amount_move` assumes a single constant-product step. Its own TODO says it is "not 100% correct for… highly asymmetric balancers".
- On the root netuid, an add limit ≥ 1e9 fills fully, as does a remove limit ≤ 1e9.
- The v11 default 5% bound on spot allows at most Δy = y·(√1.05 − 1) ≈ **2.47% of the TAO reserve** per buy. On SN70 (about 241 TAO) that is about 5.9 TAO. Larger default `bt.AddStake` orders always fail with `SlippageTooHigh`.

### 2.7 Python-ready simulator
Integer rao in and out. Floating-point exponentiation matches the chain to about 1e-7 relative; integer floors can differ by a few rao. Always cross-check with §2.8.

```python
from dataclasses import dataclass, replace

RAO = 10**9
FEE_DEN = 65_535
MIN_RESERVE = 1_000_000          # Swap MinimumReserve (rao)
MAX_IN_MULT = 1_000              # MAX_SWAP_INPUT_RESERVE_MULTIPLIER
MIN_STAKE = 2_000_000            # DefaultMinStake (rao)

class SwapError(Exception): pass

@dataclass(frozen=True)
class Pool:
    tao: int          # SubnetTAO[n]      (y, rao)
    alpha: int        # SubnetAlphaIn[n]  (x, rao)
    w_quote: float    # SwapBalancer[n].quote / 1e18  (TAO weight, ~0.5)
    fee_rate: int = 33

    @property
    def w_base(self) -> float: return 1.0 - self.w_quote
    def spot(self) -> float:                  # TAO per alpha
        return (self.w_base / self.w_quote) * self.tao / self.alpha
    def spot_rao(self) -> int: return int(self.spot() * RAO)

def simulate_buy(p: Pool, tao_in: int):
    """TAO -> alpha (add_stake). Returns (alpha_out, tao_fee, new_pool)."""
    fee = tao_in * p.fee_rate // FEE_DEN
    dy = tao_in - fee
    if tao_in < MIN_STAKE + fee or dy < MIN_STAKE: raise SwapError("AmountTooLow")
    if tao_in > MAX_IN_MULT * p.tao: raise SwapError("InsufficientLiquidity")
    if p.alpha < MIN_RESERVE: raise SwapError("ReservesTooLow")
    if dy > MAX_IN_MULT * p.tao: raise SwapError("SwapInputTooLarge")
    x, y = p.alpha, p.tao
    alpha_out = int(x * (1.0 - (y / (y + dy)) ** (p.w_quote / p.w_base)))
    if alpha_out <= 0 or alpha_out > x: raise SwapError("InsufficientLiquidity")
    return alpha_out, fee, replace(p, tao=y + dy, alpha=x - alpha_out)

def simulate_sell(p: Pool, alpha_in: int, partial_remaining: bool = False):
    """alpha -> TAO (remove_stake). Returns (tao_out, author_tao_fee, new_pool)."""
    fee_a = alpha_in * p.fee_rate // FEE_DEN
    dx = alpha_in - fee_a
    if alpha_in > MAX_IN_MULT * p.alpha: raise SwapError("InsufficientLiquidity")
    if p.tao < MIN_RESERVE: raise SwapError("ReservesTooLow")
    if dx > MAX_IN_MULT * p.alpha: raise SwapError("SwapInputTooLarge")
    x, y = p.alpha, p.tao
    e = p.w_base / p.w_quote
    tao_out = int(y * (1.0 - (x / (x + dx)) ** e))
    if tao_out <= 0 or tao_out > y: raise SwapError("InsufficientLiquidity")
    if partial_remaining and tao_out < MIN_STAKE: raise SwapError("AmountTooLow")
    x1, y1 = x + dx, y - tao_out
    tao_fee = int(y1 * (1.0 - (x1 / (x1 + fee_a)) ** e))   # fee alpha sold fee-free
    return tao_out, tao_fee, replace(p, tao=y1 - tao_fee, alpha=x1 + fee_a)

def max_buy_to_limit(p: Pool, limit_price: float) -> int:
    """Largest gross TAO input before marginal price reaches limit (allow_partial cap)."""
    s = p.spot()
    if limit_price <= s: raise SwapError("PriceLimitExceeded")   # strict
    net = p.tao * ((limit_price / s) ** p.w_base - 1.0)
    return int(net * FEE_DEN / (FEE_DEN - p.fee_rate))

def max_sell_to_limit(p: Pool, limit_price: float) -> int:
    s = p.spot()
    if limit_price >= s: raise SwapError("PriceLimitExceeded")
    net = p.alpha * ((s / limit_price) ** p.w_quote - 1.0)
    return int(net * FEE_DEN / (FEE_DEN - p.fee_rate))
    # note: the post-swap fee-alpha sale pushes the final price slightly below the limit
```

### 2.8 Cross-checking against the chain
Call the runtime API with JSON-RPC `state_call(method, hex_args, block_hash)`. Arguments are SCALE little-endian.

| Method | Args | Returns |
|---|---|---|
| `SwapRuntimeApi_current_alpha_price` | netuid u16 LE (e.g. `0x0100` for SN1) | u64 rao/alpha |
| `SwapRuntimeApi_current_alpha_price_all` | `0x` | Vec<{netuid u16, price u64}> (129 entries incl. root) |
| `SwapRuntimeApi_sim_swap_tao_for_alpha` | u16 netuid ++ u64 tao rao | 6×u64 = 48 bytes |
| `SwapRuntimeApi_sim_swap_alpha_for_tao` | u16 netuid ++ u64 alpha rao | 6×u64 |

- **SimSwapResult** = `{tao_amount, alpha_amount, tao_fee, alpha_fee, tao_slippage, alpha_slippage}`.
  - Buy: `tao_amount` = net TAO in after the fee, `alpha_amount` = alpha out.
  - Sell: `alpha_amount` = net alpha in, `tao_amount` = TAO out.
  - `*_fee` is on the input side. `*_slippage` is price impact on the output side; for a buy, `alpha_slippage` = (gross incl. fee / spot) − alpha_out, so it includes the fee effect.
- **On any swap error the runtime returns all zeros**, not an error. Treat `alpha_amount == 0` or `tao_amount == 0` as failure.
- The sim excludes the extrinsic fee. It skips the reserve-overuse check. It reflects the post-state of the queried block, before the next block's injection or chain buy.
- SDK v11: `await client.prices.quote_stake(netuid, amount_tao)` and `quote_unstake(netuid, amount_alpha)` return `SwapQuote(tao, alpha, tao_fee, alpha_fee, tao_slippage, alpha_slippage)`. Both accept `block=`.
- Verified vectors:
  - SN92 at block ~9,240,388, 10 TAO buy → `[9.994964523 net, 7289.425629 alpha, fee 0.005035477]`.
  - SN1, 1 TAO → tao_amount 999,496,453, tao_fee 503,547 rao, alpha 152.29, spot 6,562,800 rao/alpha.

### 2.9 Protocol injection changes weights, not price
Per block, `adjust_protocol_liquidity` → `update_weights_for_added_liquidity`:
- q₁ = w_base·y·(x+dx), q₂ = w_quote·x·(y+dy), new w_quote = q₂/(q₁+q₂). Spot is unchanged.
- Amounts that would push a weight outside [0.01, 0.99] are parked in `Swap.BalancerTaoReservoir` / `BalancerAlphaReservoir` and released later.
- **Re-read `SwapBalancer` and the reserves every block before simulating.**

### 2.10 Historical fill models for backtests
- **Era A (dTAO launch at block 4,920,351 until v3 init per subnet):** price = T/A, constant product on (SubnetTAO, SubnetAlphaIn).
- **Era B (swap v3, from block 5,947,549 / 2025-07-08 / spec 290; lazy init per subnet):**
  - Price P = `Swap.AlphaSqrtPrice`² (U64F64 as u128 LE / 2^64).
  - Fills use constant product on **virtual reserves**: x_v = L·√P (TAO), y_v = L/√P (alpha), L = `Swap.CurrentLiquidity`.
    - Buy: α_out = y_v·d/(x_v+d), √P′ = √P + d/L.
    - Sell: τ_out = x_v·d/(y_v+d), 1/√P′ = 1/√P + d/L.
  - This is exact (≤0.00003% error vs `sim_swap`) when no tick is crossed. Constant product on T/A was off by −15.75% to +10.6%.
  - Fee rate: 196/65535 (0.299%) for specs 290–292 (until block ≤6.1M), then 33. It was owner/root-settable (e.g. SN104 at 222, then 333).
  - **T/A diverges from P after block 6,205,195** (spec 301, 2025-08-12), because protocol swap fees were compounded into L without being credited to SubnetTAO. The gap reached 16% on SN103 at block 7.5M. Before 6,205,195, T/A is within about 0.5%.
  - `sim_swap` exists from block 6,262,253 (spec 302). The result was 32 bytes (4 fields) for specs 302–377, and 48 bytes from spec 391.
  - From spec 391 (block 7,782,857) swap fees go to the block author.
- **Era C (Balancer, spec 423):** setCode at block 8,486,593; migration at 8,486,594. Pools were seeded at the v3 price; any pool whose seeding failed fell back to q = 0.5, so check for jumps. All v3 storage was deleted; it is readable only at block hashes ≤8,486,593.
- User concentrated liquidity was economically negligible: ≤0.17% of in-range L where checked (blocks 6.5M and 7.0M). LP was disabled by `disable_lp` (v3.2.17-350, block 7,034,932, 2025-12-05).

---

## 3. Emissions and yield

### 3.1 Block emission and halvings
- `get_block_emission_for_issuance(I) = floor(1e9 · 2^(−floor(log2(1/(1 − I/21e15)))))` rao; 0 at or above the cap. Thresholds: 10.5M, 15.75M, 18.375M…
- **First halving:** issuance crossed 10.5M inside block 7,103,975 (2025-12-15 13:31:24 UTC). Block 7,103,976 is the first 0.5-TAO block.
- Live TotalIssuance ≈ 11,597,6xx TAO, so emission is 0.5 TAO/block (3,600/day). The next halving at 15.75M is about 4.15M TAO away, about 1,150 days, so roughly 2029 or later (derived).
- Recycling (registration locks; TAO transaction fees since v445) reduces issuance and delays halvings. "Burned" TAO stays in issuance. Sources use these terms loosely (§6.8).
- **Do not read `SubtensorModule.BlockEmission`.** It is deprecated and still stores 1e9 (1 TAO).
- Each subnet's alpha follows the same curve on `alpha_issuance = SubnetAlphaIn + SubnetAlphaOut + protocol alpha reservoir`. The largest are SN4 at about 6.41M, SN64 6.39M, SN12 6.38M and SN28 6.27M, so the first alpha halvings (at 10.5M) are likely around early 2028.

### 3.2 Cross-subnet TAO split (current; reproduces live per-subnet emission to about 2e-7 TAO)
`coinbase/subnet_emissions.rs::get_shares`:
1. **Eligible subnets** (`get_subnets_to_emit_to`): non-root, `FirstEmissionBlockNumber` set, `SubtokenEnabled`, `NetworkRegistrationAllowed`.
2. s_i = `SubnetMovingPrice_i` / Σ SubnetMovingPrice.
3. b_i = s_i·(1 − min(`MinerBurned_i`, 1)), renormalised. If every weight is 0, fall back to s.
4. **Gate:** g_i = b_i / (1 + (θ/b_i)^h), then final_i = g_i/Σg. If every g is 0, fall back to the ungated shares.
   - h = `EmissionGateExponent` (unset, so 3; root-settable 1–8).
   - θ = `EmissionGateBar` (U64F64 / 2^64; live **0.0082624**). In **rank mode** θ is the Nth-largest positive b, with `EmissionBarRank` unset, so N = 32. θ is recomputed when `block % 360 == 0`, or whenever it is 0. `EmissionBarQuantile` is stored as 0.75 but is ignored in rank mode (code default 0.61).
   - The gate runs **before** disabled subnets are zeroed, so disabled subnets still take ranks in θ.
5. Subnets with `SubnetEmissionEnabled = false` get 0, and the remainder is renormalised: subnet_tao_i = 0.5 TAO · final_i / Σ_enabled final.

How much of its share a subnet keeps: at s = θ, 50%; at θ/2, 11%; at θ/3, 3.6%; at θ/5, 0.8%.

Live (block ~9,240,382):
- 125 eligible subnets, 99 with a positive burn-adjusted share.
- 26 subnets have `MinerBurned` ≥ 1 and receive 0 (e.g. SN95, which is EMA rank 8).
- Concentration: top 8 = 53.1%, top 16 = 76.3%, top 32 = 97.2%, top 48 = 99.7%.
- Only 35 subnets receive ≥10 TAO/day and 50 receive ≥1 TAO/day.
- Top recipients (TAO/day): SN51 472, SN64 329, SN4 243, SN120 214, SN107 180. SN90 receives 96 (rank 14).
- Official v440 snapshot: below-bar subnets fell from 38.4% to 12.5% of emission, and the effective number of subnets from about 50 to about 22. Near the bar the response is strong: "a subnet at rank 36 that grows its demand 10% gains roughly 26% more emission".

### 3.3 Price EMA (`SubnetMovingPrice`)
`stake_utils.rs::update_moving_price` runs every block in `block_step`, **after `run_coinbase`**, and only for emit-eligible subnets:
```
b     = now - (FirstEmissionBlockNumber - 1)            # blocks since start_call, not since registration
a     = SubnetMovingAlpha * b / (b + EMAPriceHalvingBlocks[netuid])
EMA'  = a * min(spot, 1.0) + (1 - a) * EMA
```
- `SubnetMovingAlpha` on chain is I96F32 raw 1,288,490/2^32 = **0.0003**, 100× the code default 0.000003. `EMAPriceHalvingBlocks` is 201,600 on every subnet.
- Half-life ≈ ln2/a:

| Subnet age (b) | Half-life |
|---|---|
| Mature, genesis-era | about 7.7–8.4 h |
| SN92 | 9.4 h |
| 30 days after start_call | 14.9 h |
| 7 days after start_call | 1.6 days |
| 1 day after start_call | 9.3 days |

- Warm-up after start_call: EMA/spot ≈ 1 − exp(−0.0003·(b − 201,600·ln(1 + b/201,600))), i.e. about 4% after 1 day, 28% after 3 days, 80% after 7 days.
- Storage: I96F32 (raw/2^32). The internal arithmetic type changed U96F32 → U64F64 in v441, affecting precision only. Default 0, and removed on dissolve, so new and reused netuids start at **0**. A subnet that never calls start_call keeps EMA exactly 0 and is the first prune candidate once its immunity ends.
- Spot is capped at 1.0 TAO.
- The EMA input is the post-injection spot, before that block's user extrinsics, so the target for block N is fixed before any transaction in block N. Emission in block n uses EMA_{n−1}.
- Setting `SubnetEmissionEnabled = false` does **not** freeze the EMA. `NetworkRegistrationAllowed = false` does.
- `SubnetFastMovingPrice` (U64F64 / 2^64, OptionQuery, absent until the first update) has a 600-block half-life and is unclamped. It is used only for basket-trade price bands, not for emission.
- **Common misconception:** the claim that EMA reaction slowed from about 10 h to about 48 h around Jan 2026 (SubnetEdge, paywalled). There is no code or parameter change. Per-block checks either side of 2026-01-15 match 0.0003·b/(b+201,600) within about 1%.

### 3.4 Per-subnet injection and chain buys
`run_coinbase.rs::get_subnet_terms`, for TAO emission E_i:
```
rp_i     = (SubnetTAO[0]*TaoWeight) / (SubnetTAO[0]*TaoWeight + alpha_issuance_i)
alpha_in = E_i / spot_i ;  cap = rp_i * alpha_emission_i
if alpha_in > cap: alpha_in = cap; tao_in = cap*spot_i   else: tao_in = E_i
excess   = E_i - tao_in   # swapped TAO->alpha, drop_fees=true, no price limit; alpha -> SubnetProtocolAlpha
```
- `TaoWeight` = raw/u64::MAX = **0.18** on chain. The code default is about 5.27%. Live `SubnetTAO[0]` (root) is about 5.454M TAO.
- `RootProp[netuid]` (U96F32) is stored from block 7,135,420 onward; compute it yourself for earlier blocks.
- rp depends on a subnet's **age (alpha issuance), not its market cap**. Young micro caps have rp of about 0.3–0.88; old subnets about 0.13–0.16.
- Live SN51: rp 0.139, alpha_in 0.139/block, tao_in 0.01397, chain buy 0.0515 TAO/block.
- Network totals: Σ(`SubnetTaoInEmission` + `SubnetExcessTao`) = 0.5 TAO/block = 3,600/day. Of that, about **1,131 TAO/day is injection and about 2,468 TAO/day is chain buys**.
- Chain-buy alpha is added to **both** `SubnetAlphaOut` and `SubnetProtocolAlpha`, and is not staked to any hotkey.
- `SubnetTaoInEmission` counts only the TAO that became price-active; amounts parked in reservoirs show up later.

### 3.5 alpha_out and its split
- alpha_out = `get_block_emission_for_issuance(alpha_issuance)`, currently **1.0 alpha/block** for every started subnet in the emit set, **including emission-disabled subnets**. It is 0 for subnets without start_call (SN59, 82, 86).
- Disabling emission zeroes only tao_in, alpha_in and chain buys. alpha_out, the owner cut and the pending miner and validator emission continue.
- The split (`emit_to_subnets`):

| Recipient | Share | Notes |
|---|---|---|
| Owner | 18% (`SubnetOwnerCut` 11,796/65535) | Only if `OwnerCutEnabled` (default true). Paid to the owner hotkey. **`OwnerCutAutoLockEnabled` defaults to false**; on only 13 subnets (17, 31, 34, 51, 53, 64, 73, 87, 90, 96, 106, 109, 122). |
| Miners | 0.5 × 0.82 = 41% | Miner incentive sent to owner hotkeys is burned or recycled, and that fraction becomes `MinerBurned` (§3.2). |
| Root | rp × 0.41 | Accrues to `PendingRootAlphaDivs` only if Σ SubnetMovingPrice over eligible subnets > 1. Live 1.185, trending down from 1.163–1.333 since v441. Otherwise recycled. |
| Alpha validators/nominators | 0.41 × (1 − rp) | |

- Pending amounts drain at the epoch. The epoch fires when `LastEpochBlock + tempo` is reached, at an owner-triggered `PendingEpochAt`, or when `BlocksSinceLastStep > tempo`.
- Tempos: 124 subnets at 360; one each at 99, 1440, 1800 and 7200.
- `MaxEpochsPerBlock` = 2, or 1 when a Null-consensus epoch is due; extra epochs are deferred one block. A skipped epoch keeps its pending emission, so the next payout is doubled.
- If total miner incentive in an epoch is 0, validators also receive the miner half.
- Example (SN92): 7,200 alpha/day × 0.0013482 ≈ 9.7 TAO/day of potential sell flow, about 1.65% of its 587-TAO reserve, while its TAO emission is about 0.

### 3.6 Dividend distribution and nominator yield (exact mechanics, verified on chain)
1. Each UID's dividend goes through `get_parent_child_dividends_distribution`. It is split among parents by contribution α_p·prop + 0.18·τ_p·prop. For a parent under a different coldkey, `ChildkeyTake` (0–18%, default 0) and CKBurn (now 0) are deducted.
2. `calculate_dividend_distribution`: alpha_divs = d·α/(α + 0.18·τ). root_divs = the rest, only if the hotkey has a root UID; otherwise that part is unassigned. The totals are then normalised onto `PendingValidatorEmission` and `PendingRootAlphaDivs`.
3. `distribute_dividends_and_incentives`: take = `Delegates[h]`/65535 (PerU16; **default 11,796 = 18% for any hotkey that never set a take**; minimum 0). The take is credited as new shares to the owner at the current price. The remainder goes through `increase_stake_for_hotkey_on_subnet` → `SharePool.update_value_for_all`, which raises `TotalHotkeyAlpha` while shares stay fixed. Every coldkey's value floor(V·S/D) rises pro rata, so it compounds automatically.
4. `AlphaDividendsPerSubnet(n, h)` is set to the post-take nominator alpha of the last epoch. **Stake earns if and only if its hotkey is a key of `AlphaDividendsPerSubnet(n, ·)`.** Childkey parents earn whether or not they are registered or hold a permit. A UID with no permit earns 0 itself.
   - Permits go to the top `MaxAllowedValidators` by stake weight (128 initial; `init_new_network` sets 64). `StakeThreshold` = 1,000 TAO-equivalent. The activity cutoff ≈ 13,889·tempo/1000 ≈ 5,000 blocks.
   - Under classic Yuma (`Yuma3On` default off) a bond EMA (0.1) lags new stake into a permit holder.
   - **Null consensus** (added in spec 475; 0 of 128 subnets use it at block 9,240,814) pays half the epoch budget to every registered UID with positive stake, plus parents.
5. **Timing:** there is no stake-age gate. Stake included at or before block B−1 earns the full drain at block B. Stake added in block B itself does not (B's drain runs before extrinsics). Unstaking in block B keeps the credit. Verified on SN70: the share price was flat from 9,240,222 to 9,240,581, then rose 0.0279% at 9,240,582 (= `LastEpochBlock`).

**Closed-form yield** (gross, per alpha per day):
`≈ 7200 · alpha_emission · 0.82 · 0.5 · (1 − rp) / A_earn = 2952·(1 − rp)/A_earn`
- Net = gross × (1 − take) × (1 − childkey_take).
- A_earn = Σ `TotalHotkeyAlpha` over the dividend recipients. It is **not** `SubnetAlphaOut`, which also includes protocol alpha, burned alpha and non-earning stake. On SN92, AlphaOut is 630,980 against A_earn of 343,198.

| Subnet | rp | A_earn | Predicted gross %/day | Observed (take-0 hotkey) |
|---|---|---|---|---|
| SN70 | 0.655 | 182,839 | 0.557 | 0.557 |
| SN92 | 0.479 | 343,198 | 0.448 | 0.448 |
| SN64 | 0.133 | 2,789,195 | 0.0918 | 0.092 |

- Other gross %/day: SN36 0.63, SN99 0.44, SN16 0.32, SN47 0.24, SN1 0.16, SN3 0.118, SN8 0.115, SN9 0.10, SN51 0.094, SN4 0.089.
- Realised index examples: SN70 hotkey 56a9: I went 1.329667 → 1.631360 over 30 days (+22.7%; daily yield fell from 0.86% to 0.57%). The same subnet with a 6%-take hotkey: +21.2%.
- **Total return identity (TAO terms):** 1+R = (P₁/P₀)·(I₁/I₀) − swap fees − slippage − transaction fees. Dilution from alpha_out is already in P; do not subtract it again.
- **Choosing a validator:** among the `AlphaDividendsPerSubnet(n,·)` keys, prefer take 0 and the highest trailing realised Δln I, measured ex-ante. Check `ChildkeyTake` and that the parent link is stable (child changes have a 7,200-block cooldown). Moving between hotkeys on the same subnet (`move_stake`) costs no swap fee, but it does pay the transaction fee.
- Take changes: increase at most once per 216,000 blocks (about 30 days), with no step-size limit (0 → 18% in one transaction). Decreases are allowed any time but restart the 30-day clock. Both are `Pays::Yes`.
- **Do not use** `DelegateInfo.return_per_1000` / `total_daily_return` (they aggregate across subnets and divide by root stake) or `StakeInfo.emission` (hotkey-level).

### 3.7 Root Reborn: validator baskets (v441, block 8,765,683, 2026-08-03)
- Root dividends are **no longer sold every block**. Before v441, about 936 TAO/day of alpha was sold. Now each epoch the validator's root alpha dividend minus take is queued (`enqueue_basket_deposit`, `PendingBasketDeposits`) and credited in place, with no swap, to the escrow position (validator_hotkey, escrow, origin_netuid).
  - The escrow account is "modl"+"subtensr"+"beta/esc", padded to 32 bytes.
  - Positions are ordinary nominator stake, so they also earn the validator's alpha dividends.
- Live: 189 funds, NAV 28,425 τ realisable. Lifetime 55,681 τ deposited and 26,255 τ redeemed.
- **Escrow share of micro-cap pools.** Median escrow-to-pool-alpha ratio (E/x) is 5.3% for the bottom-third subnets (a one-shot sale would cut price about 9.7%), against 0.65% / 1.1% for the middle and top thirds.

  | Subnet | E (alpha) | E/x | One-shot price drop | Note |
  |---|---|---|---|---|
  | SN92 | 49,063 | 11.2% | 19.1% | 55 funds; largest 11,689 α |
  | SN103 | 70,972 | 22.5% | 33.4% | |
  | SN70 | 57,665 | 26.4% | 37.4% | |
  | SN76 | 31,907 | 34.8% | 45.0% | |
  | SN58 | 50,427 | 43.0% | 51.1% | |
  | SN90 | 57,483 | 30.0% | 40.9% | |
  | SN16 | 53,615 | 15.1% | 24.5% | |
  | SN99 | 64,099 | 16.2% | 25.9% | |
  | SN47 | 25,516 | 4.4% | 8.3% | old, low RootProp |
  | SN84 | 21,718 | 3.5% | 6.7% | old, low RootProp |

- **Release channels:**
  - **(a) Staker claims** (`claim_root` / `claim_root_with_hotkey`; no auto-claim). A claim sells the claimant's pro-rata slice of every holding through the AMM, fee-free, with **no price limit**, and restakes the TAO on root. Claims below `RootClaimableThreshold` (500,000 rao) are skipped. Observed release is about 2% of holdings per day, with bursts of 4–5% per day across all holdings simultaneously (e.g. blocks 9,150,000–9,190,000).
  - **(b) `swap_basket`.** `BasketTradingEnabled` has been true since block 9,117,749. Each leg must fill within 2% of the **strictest** of slow EMA, fast EMA and spot. Turnover is capped at 10% of NAV per day. Holdings are capped at 10% of the destination pool's alpha reserve. On-chain `BasketConcentrationCap` is 7,865/65,535 ≈ 12.0% of NAV (code default 1/16) and `BasketMinTradeTao` is 0.005 TAO (code default 0.5). Only 13 funds have ever traded, and none bought into the bottom-10 micro caps. A basket cannot be dumped on a micro cap.
  - **(c) Dust consolidation:** negligible.
  - **(d) Dissolution:** baskets are sold **first** (§4.5).
- Release model: dE/dt = inflow − c·E − swaps, with c ≈ 0.02/day (±30%). Steady state E* ≈ inflow/c (SN92: about 64k alpha).
- **Common misconception:** "Curated Beta (v450) routes root dividends into micro caps." Curated weight vectors ran only from spec 450 (block 8,938,465) to spec 464 (block 9,088,597). Vectors weighted large caps, and the feature has been removed since.

### 3.8 Price-pressure accounting for one subnet (per day)

| Flow | Sign | Size and driver |
|---|---|---|
| Chain buys (`SubnetExcessTao`) | Bid | ∝ emission share; ~0 below the gate or when disabled |
| tao_in + alpha_in injection | Neutral at spot | Deepens the pool; capped at rp·alpha_emission |
| Owner cut (18% of 7,200) | Potential sell | Liquid unless auto-lock is on (13 subnets) |
| Miner emission (41%) | Potential sell | Reduced by MinerBurned |
| Validator/nominator alpha (41%·(1−rp)) | Compounds | Sells only on unstake |
| Root dividends (41%·rp) | Accrue in baskets | Released by claims at about 2%/day of the stock |
| Basket swaps | Bounded | Within 2% bands and 10%-NAV/day |
| Reflexive loop | ± | Price → EMA (8 h) → emission share → chain buys → price; strongest near rank 25–40 |

### 3.9 Emission regime history (align backtests to block+1 after each setCode block)

| Blocks | Dates | Cross-subnet rule and notes |
|---|---|---|
| 4,920,351 → ~Nov 2025 | 2025-02-13 → | Price-EMA shares (Era A/B prices) |
| v3.2.10-334 / 338 | 2025-11-04/05 → 2026-06-23 | Taoflow: user-flow EMA (smoothing factor 29,597,889,189,277/i64::MAX ≈ 3.209e-6/block, half-life 216,000 blocks ≈ 30 days). z = max(S − L, 0), L = max(TaoFlowCutoff, min(minS, 0)), shares z^p/Σz^p with p = 1. From May 2026 (spec 411): net flow = user_ema − norm·protocol_ema. |
| 2025-12-12 (v3.3.1-362) | | Alpha injection cap; chain buys begin |
| 7,103,975 | 2025-12-15 | Halving |
| 8,463,544 | 2026-06-22 15:28Z | 54 subnets emission-disabled in one block (+~10 within a day) |
| 8,466,530 setCode (spec 421); 8,466,531 first | 2026-06-23 01:25Z | price_EMA × root_proportion × (1−MinerBurned), normalised, **no gate**. 64/128 subnets disabled at 8,550,000. Taoflow EMA frozen at 8,466,530. |
| 8,636,190 (spec 432, "v431") | 2026-07-16 19:30Z | price_EMA × (1−MinerBurned); root_proportion removed |
| 8,713,793 (spec 440) | 2026-07-27 14:16Z | Hill gate added in **q-mass mode** (q = 0.61; θ ≈ 1.38% at first, ≈1.339% at rank 18) |
| 8,765,683 (spec 441) | 2026-08-03 19:18Z | Gate switched to **rank-32 mode**; Root Reborn |
| 8,772,666 (443) → 8,831,003 (445) | 2026-08-04 → 08-12 | Spec 444 (miner-burn removal) never ran on mainnet; 445 has miner-burn scaling restored |
| 8,938,465 (450) → 9,088,597 (464) | 2026-08-27 → 09-17 | Curated root weights live, then removed |
| 9,029,889 | 2026-09-09 | 47 subnets re-enabled in one block |
| Spec 475 | 2026-10-07 | Null consensus, "precise emissions", PoW registration; re-verify emission series after this block |

---

## 4. Subnet lifecycle

### 4.1 Registration
- **Cost** (`root.rs::get_network_lock_cost`):
  `lock = max(NetworkMinLockCost, 2·L − (L / I_eff)·(now − last_lock_block))`
  - I_eff = `NetworkLockReductionInterval` (115,200) × block_emission/1e9 = **57,600 blocks**.
  - On chain: `NetworkMinLockCost` = 1 TAO; `NetworkLastLockCost` L = 653.02 TAO; last lock at block 9,210,610.
  - The cost is 1.75·L when the rate limit expires, returns to L after 8 days, and reaches the 1-TAO floor after about 16 days.
  - Live values: 969.38 TAO (block ~9,240,300) → 965.70 (~9,240,630) → 962.89 TAO (block 9,240,878, `subnetInfo_getLockCost` / `SubnetRegistrationRuntimeApi_get_network_registration_cost`).
- **Rate limit:** `NetworkRateLimit` = 14,400 blocks (about 2 days), **network-wide**, keyed on `LastRateLimitedBlock(RateLimitKey::NetworkLastRegistered)` (storage key suffix 0x02). The docs catalog says "per coldkey"; the code says otherwise. It was 28,800 until about Dec 2025. The current window has been open since block 9,225,010.

### 4.2 New-subnet state (`set_new_network_state`)
- `SubnetTAO` = full lock. `SubnetAlphaIn` = lock / median subnet alpha price. `SubnetAlphaOut` = 0. No owner alpha (hotfix v3.3.15-402, 2026-05-08). EMA = 0.
- **`SubnetEmissionEnabled = false`** (since v3.4.7-422). Only root can enable it. SN36 had been disabled for about 49 days at the time of checking.
- `StartCallDelay` = 0. The owner calls `start_call` at will, which sets `FirstEmissionBlockNumber = now + 1` and `SubtokenEnabled`. Before that there is no buying, no emission and no EMA updates.
  - Observed delays: from 610 blocks (SN116) to more than 76 days. SN86, SN59 and SN82 have never started.
- Owner cut auto-lock is off by default.

### 4.3 Pruning rule (picked the actual victim in 52 of 52 events)
```python
def prune_target(subnets, now, immunity=864_000):        # NetworkImmunityPeriod on chain
    c = [s for s in subnets if s.added and s.netuid != 0 and now >= s.registered_at + immunity]
    return min(c, key=lambda s: (s.moving_price, s.registered_at)) if c else None
```
- Pruning runs only inside `do_register_network`, when non-root subnets + `DissolveCleanupQueue.len()` ≥ `SubnetLimit` (128). If every subnet is immune: `SubnetLimitReached`. If cleanup is already pending, the registration waits instead.
- **Same-block removal:** `do_dissolve_network` removes the subnet from `NetworksAdded`, emits `NetworkRemoved`, subtracts `SubnetTAO` from `TotalStake` and queues cleanup. The new registration is queued (`NetworkRegistrationQueued`), and `NetworkAdded` follows **17–25 blocks later**. After removal, `remove_stake` and `move_stake` fail with `SubnetNotExists`.
- Direct query: `SubnetInfoRuntimeApi_get_subnet_to_prune()` returned `Some(92)`.
- Immunity runs from `NetworkRegisteredAt`, not from start_call.
- **Pruning a registration can be MEV-shielded** (`pallet_shield` has no call filter). The mempool shows only a ~1.4 KB `submit_encrypted`. 6 of the last 10 prunes were shielded.
- Root can also call `root_dissolve_network` (call index 120).

### 4.4 Lead time and hazard (52 prunes, rebuilt from archive state)
- **How long the pruned subnet had been rank 1 beforehand:** <1h: 1; 1–4h: 1; 4–8h: 1; 8–12h: 1; 12–24h: 8; 1–2d: 5; 2–4d: 5; 4–7d: 18; 7–14d: 11; ≥14d: 1. So **23% had been rank 1 for less than 24 h**.
- Fastest climbs: SN67 went from rank 49 to 1 within a day; SN90 from rank 7 to 1 within 8 h; SN116 from rank 2 to 1 in under 1 h.
- EMA gap to rank 2 at block P−1: median 7.2%; 14 of 51 cases were under 2.5%.
- **Blocks since the previous registration** (Feb–Oct 2026, n = 32): minimum 14,401 (twice). Cumulative share: 31k → 9%, 43k → 19%, 50k → 31%, 55k → 50%, 60k → 72%, 65k → 84%, 71k → 94%. Maximum 99,900. Registrants usually wait until cost falls back to about the previous lock (about 8 days).
- **Time for subnet k to become the target** (bottom EMA E₁ flat, k's spot drops to s): t* = ln((E_k − s)/(E₁ − s))/a_k. The worst case (s ≈ 0) is half-life × log₂(E_k/E₁).
- Current non-immune ladder:
  - Rank 1: SN92, EMA 0.0013565 (the target).
  - Rank 2: SN47 at 1.255× the bottom (2.8 h worst case).
  - Rank 3: SN72 at 1.508× (4.8 h).
  - Ranks 4–15: 1.53–1.70× (4.9–6.3 h).

Compact prune log. Lock cost is at block P−1 in TAO; "Δreg" is blocks since the previous registration; "rank1" is how long the victim had been rank 1.
```
block,date,netuid,lock,Δreg,gap_to_rank2,rank1,submission
6693448,2025-10-19,100,650.63,85220,EMA0 tie,>=14d,plain
6783158,2025-10-31,49,287.93,89710,3.6%,7-14d,plain
6841399,2025-11-09,105,284.72,58241,29.9%,7-14d,plain
6914378,2025-11-19,86,208.70,72979,9.3%,4-7d,plain
6962737,2025-11-25,94,242.18,48359,0.9%,4-7d,plain
7013758,2025-12-03,92,269.84,51021,3.0%,4-7d,plain
7063126,2025-12-09,90,308.41,49368,0.2%,1-4h,plain
7105263,2025-12-15,108,165.60,42137,1.1%,1-2d,plain
7119664,2025-12-17,113,248.38,14401,4.3%,2-4d,plain
7151800,2025-12-22,80,219.61,32136,14.6%,4-7d,plain
7173591,2025-12-25,31,273.05,21791,7.2%,1-2d,plain
7208725,2025-12-30,87,213.00,35134,40.0%,4-7d,plain
7236936,2026-01-03,67,217.36,28211,5.7%,4-8h,plain
7257480,2026-01-05,109,279.66,20544,6.6%,4-7d,plain
7284230,2026-01-09,38,299.57,26750,12.7%,2-4d,plain
7312241,2026-01-13,114,307.78,28011,4.7%,1-2d,plain
7340355,2026-01-17,47,315.11,28114,5.9%,12-24h,plain
7366897,2026-01-21,15,339.82,26542,7.1%,2-4d,plain
7415113,2026-01-27,99,395.17,48216,11.7%,4-7d,plain
7457580,2026-02-02,107,498.99,42467,4.1%,4-7d,plain
7525773,2026-02-12,126,407.23,68193,16.6%,7-14d,plain
7574784,2026-02-19,76,467.95,49011,48.7%,4-7d,plain
7633645,2026-02-27,91,457.71,58861,58.0%,7-14d,plain
7692872,2026-03-07,96,444.78,59227,0.9%,4-7d,plain
7735450,2026-03-13,97,560.77,42578,5.4%,12-24h,plain
7787562,2026-03-20,70,614.20,52112,32.8%,7-14d,plain
7840965,2026-03-28,102,658.96,53403,35.6%,4-7d,plain
7894898,2026-04-04,36,700.91,53933,8.1%,1-2d,plain
7966145,2026-04-14,78,534.85,71247,40.2%,7-14d,plain
8026517,2026-04-22,82,509.11,60372,45.5%,7-14d,plain
8057320,2026-04-27,57,745.95,30803,4.0%,4-7d,plain
8085297,2026-05-01,84,1129.58,27977,1.8%,12-24h,plain
8123781,2026-05-06,26,1504.47,38484,0.8%,2-4d,plain
8138182,2026-05-08,69,2632.78,14401,0.9%,12-24h,plain
8238082,2026-05-22,122,699.37,99900,14.6%,12-24h,plain
8294730,2026-05-30,116,710.90,56648,51.5%,4-7d,plain
8352006,2026-06-07,92,714.90,57276,30.1%,4-7d,plain
8409860,2026-06-15,40,711.74,57854,0.1%,7-14d,plain
8460646,2026-06-22,16,795.94,50786,0.6%,12-24h,plain
8511017,2026-06-29,58,895.83,50371,0.3%,8-12h,plain[emission-off]
8572056,2026-07-07,99,842.35,61039,49.6%,4-7d,plain[emission-off]
8618670,2026-07-14,90,1003.01,46614,15.6%,4-7d,plain[emission-off]
8693261,2026-07-24,86,707.14,74591,14.5%,7-14d,SHIELDED[emission-off]
8762355,2026-08-03,103,566.32,69094,1.8%,4-7d,SHIELDED
8825550,2026-08-12,70,511.55,63195,12.1%,4-7d,SHIELDED[emission-off]
8884341,2026-08-20,36,501.16,58791,42.5%,7-14d,SHIELDED
8938751,2026-08-27,59,529.07,54410,17.1%,7-14d,plain
9003827,2026-09-05,76,460.59,65076,56.6%,2-4d,plain(proxy)
9046671,2026-09-11,35,578.58,42844,0.2%,1-2d,plain
9111229,2026-09-20,108,508.69,64558,38.6%,12-24h,SHIELDED
9155237,2026-09-26,82,628.72,44008,2.2%,12-24h,plain(proxy)
9210610,2026-10-04,116,653.03,55373,0.3%,<1h,SHIELDED
```
- Deregistration returned with PR #2066 (merged 2025-09-25); `NetworkRegistrationStartBlock` = 6,573,966.
- SN73 at block 5,145,525 was a dissolve, not a prune.
- Victim profile: median lifespan 317 days (128–835). Pre-prune pools ranged from 4 to 7,315 TAO. Pre-prune AlphaIn/AlphaOut was 0.45–1.03, median about 0.75.

### 4.5 What holders receive on dissolution
Code: `dissolution.rs`, `remove_stake.rs::destroy_alpha_in_out_stakes*`, `claim_root.rs`. Phases run in `on_idle` across blocks:
0. Reservoirs are folded into the reserves.
1. **`SubnetBasketHoldingsToRoot`:** every validator basket holding on the subnet is sold through the AMM (fee-free, chunked, no price limit). This drains `SubnetTAO` first.
2. **GetTotalAlphaValue → SettleStakes:** pot = remaining `SubnetTAO`. Each (hotkey, coldkey) receives pot·alpha_value_i/total_alpha_value, with largest-remainder rounding. It is credited as **free TAO to the coldkey**, not restaked.
   - total_alpha_value = Σ staker share-pool values + protocol term.
   - Protocol term = `SubnetAlphaIn + SubnetProtocolAlpha` if registered **after** `TaoInRefundDeploymentBlock` (8,334,450, 2026-06-04). Otherwise (legacy) `SubnetProtocolAlpha` only.
3. Alpha, hotkey totals, conviction locks and decaying locks are cleared, so **accumulated conviction is lost**.
4. The protocol's share is recycled. A legacy owner refund of max(0, lock − owner emission in TAO) applies only if the subnet was registered before `NetworkRegistrationStartBlock` (6,573,966).

**Approximate recovery per alpha relative to spot** (corrected; `SubnetAlphaOut` already includes protocol alpha, so do not add it twice):
- New subnets: ≈ SubnetTAO/(AlphaOut + AlphaIn), i.e. about AlphaIn/(AlphaIn+AlphaOut) × spot. SN92: 587.2/1,067,584 ≈ 0.00055 against spot 0.00135, so **≈0.41× spot before basket sales and ≈0.35–0.37× after** (the basket takes about 10.1% of the pot).
- Legacy subnets: ≈ SubnetTAO/AlphaOut. SN47: 983.9/1,576,160 ≈ 0.00062 against spot 0.0017, so **≈0.36×**.
- Basket haircut E/(x+E): SN92 10.1%, SN103 18.4%, SN70 20.9%, SN76 25.8%, SN58 30.1%.
- A broad 0.45–1.0× range was seen historically before prunes; current candidates sit lower.
- **Conclusion: exit before the prune.** Holding through dissolution is equivalent to every holder dumping at once, after the baskets.

### 4.6 Emission switch-off and other root actions
- `sudo_set_subnet_emission_enabled` (AdminUtils call 94, `ensure_root`) emits `SubnetEmissionEnabledSet{netuid, enabled}`. An absent key means the default, true.
- History:
  - 2026-06-22: 54 subnets switched off in block 8,463,544, plus about 10 more within a day. Announced by Const on Discord (criteria: no active miner distribution or no code); weekly Monday reviews (secondary).
  - Re-enables trickled through July–Aug.
  - 2026-09-02: second wave, including large SN8, SN9 and SN34.
  - 2026-09-09: 47 re-enabled in block 9,029,889.
- **Leading indicator:** 10 subnets switched off in late June were pruned 1–15 weeks later (58, 99, 90, 86, 70, 36, 76, 108, 82, 116).
- Disabled subnets lose TAO injection and chain buys but keep minting alpha. Their EMA keeps updating, and they still count in the gate ranking.
- **SafeMode** (pallet index 20): no permissionless entry. Root `ForceEnter` lasts 7,200 blocks and `ForceExtend` adds 3,600. The whitelist contains **no staking calls**, so no exits are possible while it is active. It happened once: 2024-07-02 to 07-12.

### 4.7 Netuid reuse
- Netuids are reused about weekly; the new subnet takes the lowest free id after cleanup.
- Key each asset by **(netuid, `NetworkRegisteredAt`)**. `RegisteredSubnetCounter` is filled only for the 21 most recent generations (netuid 116 = 2), so older generations read 0.
- `TokenSymbol` is reassigned. `NetworkRegisteredAt` is removed during cleanup.
- Recent registrations: 9,210,632 (116), 9,155,260 (82), 9,111,253 (108), 9,046,691 (35), 9,003,844 (76), 8,938,771 (59), 8,884,359 (36), 8,825,571 (70), 8,762,380 (103), 8,693,284 (86). Gaps of 6.0–9.6 days.
- Taostats pool history is keyed only by netuid, so it **splices generations together**. CoinGecko keeps stale slugs (e.g. `agent-arena-by-masa` still shows sn59; `soundsright` sn105 shows as "Beam" with market cap 0).

### 4.8 Ownership changes by conviction
- `change_subnet_owner_if_needed` runs at each epoch. If the subnet is at least ONE_YEAR (7200·365+1800 = 2,629,800 blocks) old **and** a single hotkey's conviction exceeds 18% of (AlphaOut − ProtocolAlpha − AlphaBurned), ownership and the 18% cut move to that hotkey (gate set in v447).
- Locks decay as locked(t) = m·e^(−Δt/UnlockRate). `UnlockRate` = 934,866 (default), about 21% unlocked after 30 days and 50% after about 90 days. `MaturityRate` on chain is 311,622.
- Switching a lock from perpetual to decaying emits a public event, an early exit signal.
- Coldkeys reject transfers of locked alpha by default (`SetRejectLockedAlpha`).

### 4.9 Upcoming immunity expiries (from block 9,240,878)
Each subnet below takes the target slot from this block if its EMA is still the lowest.

| Subnet | EMA | Leaves immunity at block | Date (approx.) |
|---|---|---|---|
| SN16 | 0.000950 | 9,324,646 | 2026-10-20 |
| SN99 | 0.000995 | 9,436,056 | 2026-11-04 |
| SN86 | 0 (never started) | 9,557,284 | 2026-11-21 |
| SN103 | 0.000934 | 9,626,380 | 2026-12-01 |
| SN70 | 0.001138 | 9,689,571 | 2026-12-10 |
| SN59 | 0 | 9,802,771 | 2026-12-25 |
| SN82 | 0 | 10,019,260 | 2027-01-24 |

The next registration is most likely between blocks about 9.253M and 9.276M (2026-10-10 to 10-13).

---

## 5. Execution API reference

### 5.1 Versions and install
- **Pin `bittensor==11.3.0`** (PyPI 2026-10-07; Python >=3.10,<3.15). It bundles btcli and the wallet, so do not install `bittensor-cli` or `bittensor-wallet`.
  - It is published via PyPI Trusted Publishing from `RaoFoundation/subtensor`, workflow `.github/workflows/watch-mainnet-release.yml`, environment `mainnet`, commit `d1718c99c34cf96abbf2bf09c0e9e48b945c76f5`.
  ```
  bittensor==11.3.0 --hash=sha256:4651d9125cd29ecfda1eed0ef758fe9e29563dd81ecd9d42a9f1c9ec6603cbaa   # wheel
  # sdist sha256: 8ce05029a712866048c6cf3e3d5df1592ac896175f5b4227f63044b7e7b7edb3
  pip install --require-hashes -r requirements.txt
  ```
  - It was one day old at the time of writing. Verify provenance, run it against testnet, and gate production behind smoke tests.
- Release history: 11.0.0 (2026-07-17), 11.0.1 (07-23), 11.0.2 (08-03), 11.1.0 (08-14), 11.2.0 only as rc34, 11.3.0 (10-07). The last v10 is 10.5.0 (2026-06-25); whether it works with spec 475 is untested.
- **Windows is not supported natively: use WSL.** This matters because the host is Windows 11.
- Known malicious packages: `bittensor==6.12.2` (2024), `bitensor`, `bittenso`, `bittenso-cli`, `qbittensor` 9.9.4/9.9.5 (2025-08-06), and `bittensor-burn-message` (flagged 2026-06-11, UNVERIFIED).

### 5.2 Client and endpoints
```python
import bittensor as bt
async with bt.Subtensor("finney") as client: ...      # documented async form; also `client = await bt.Subtensor(net)`
sub = bt.Subtensor()                                   # documented blocking form (finney default, lazy connect)
bt.Client(network="finney", *, policy=None, fallback_endpoints=None, archive_endpoints=None,
          retry_forever=False, substrate=None)        # pass [] to pin a single endpoint
```
(UNVERIFIED) Sources disagree on whether `bt.Subtensor()` is blocking or async-only and whether `bt.SyncClient` exists. Check `sdk/python/bittensor/__init__.py` before writing the adapter.

| Network | Endpoint |
|---|---|
| finney | `wss://entrypoint-finney.opentensor.ai:443` (also HTTP JSON-RPC) |
| fallback | `wss://lite.chain.opentensor.ai:443`, `wss://lite.sub.latent.to:443` |
| archive | `wss://archive.chain.opentensor.ai:443`, `wss://archive.sub.latent.to:443` |
| keyed or public third party | `https://bittensor-finney.api.onfinality.io/public` |
| test / devnet / local | `wss://test.finney.opentensor.ai:443` / `wss://dev.chain.opentensor.ai:443` / `$BT_CHAIN_ENDPOINT` or `ws://127.0.0.1:9944` |

- Lite nodes keep about 300 blocks of state; reads 1,000 blocks back fail with "State already discarded".
- Public endpoints are documented at about 1 req/s per IP. `entrypoint-finney` returns `{"error":"Too many requests…","policy":"http_60s","retry_after_seconds":60}` under bursts.
- For production, run your own **lite node**: about 128 GB, `--sync warp --database paritydb`, image `ghcr.io/raofoundation/subtensor`, `docker compose up -d mainnet-lite`, WS and HTTP on port 9944.
- Constants: `RAO_PER_TAO` = 1e9, `BLOCKTIME` = 12 s, `DEFAULT_ERA_PERIOD` = 128, `MEV_SHIELD_ERA_PERIOD` = 8.

### 5.3 Reads (v11; every typed read accepts `block=`)
```python
await client.prices.alpha_price(netuid)            # {'netuid','tao_per_alpha','price_rao'}
await client.prices.alpha_prices()                 # {netuid: tao_per_alpha}  (current_alpha_price_all)
await client.prices.quote_stake(netuid, amount_tao)      # SwapQuote(tao, alpha, tao_fee, alpha_fee, tao_slippage, alpha_slippage)
await client.prices.quote_unstake(netuid, amount_alpha)
await client.staking.get(coldkey_ss58, hotkey_ss58, netuid)   # Balance
await client.staking.stake_for_coldkey(coldkey_ss58)          # list[StakePosition]; stake_for_coldkeys([...])
await client.staking.stake_value_for_coldkey(coldkey_ss58)    # spot-marked valuation
await client.staking.stake_availability(coldkey_ss58, netuid) # total / locked / available
await client.subnets.all() / info(netuid) / subnet(netuid)    # SubnetInfo: tempo, burn, neuron_count — NOT pool data
await client.subnets.metagraph(netuid, block=None, commitments=True)   # iterate: [n.total_stake for n in mg]
await client.subnets.subnet_hyperparameters(netuid); client.subnets.subnet_registration_cost()
await client.subnets.subnet_tao_flows()            # STALE: frozen SubnetEmaTaoFlow since block 8,466,530
await client.epochs.epoch_status(netuid); next_epoch_start_block; blocks_until_next_epoch
await client.delegation.delegates() / delegate(hotkey) / delegate_take(hotkey)
await client.balances.proxies(coldkey_ss58=...)
# generic
await client.runtime(bt.runtime_api.SubnetInfoRuntimeApi.get_all_dynamic_info, [], block=N)
await client.runtime(bt.runtime_api.SwapRuntimeApi.sim_swap_tao_for_alpha, {"netuid": n, "tao": rao})
await client.query(bt.storage.SubtensorModule.SubnetTAO, [netuid])
await client.query_batch(bt.storage.SubtensorModule.SubnetTAO, [[n] for n in netuids]); client.query_map(...)
snap = await client.at(block)    # read-only snapshot pinned to one block; pruned state retried on archive pool
await client.spec_version()
```
Useful runtime APIs:
- `SubnetInfoRuntimeApi`: `get_dynamic_info(netuid)`, `get_all_dynamic_info()`, `get_subnet_to_prune()` (returns `Option<NetUid>`), `get_block_emission()`, `get_next_epoch_start_block(netuid)`, `get_metagraph`, `get_selective_metagraph`, `get_subnet_hyperparams_v3`, `get_subnet_state`, `get_mechagraph`.
- `StakeInfoRuntimeApi`: `get_stake_info_for_coldkey(s)`, `get_stake_availability_for_coldkeys`, `get_stake_fee`.
- `BetaBasketRuntimeApi`: `get_all_validator_baskets`, `get_validator_basket(hotkey)`, `get_validator_basket_nav`, `get_root_basket_owed(coldkey)`.
- `SubnetRegistrationRuntimeApi_get_network_registration_cost()`.

### 5.4 DynamicInfo and key storage
`get_all_dynamic_info` / `DynamicInfo` fields:
- netuid, owner_hotkey, owner_coldkey, subnet_name, token_symbol, tempo, last_step, blocks_since_last_step
- **emission (always 0)**, alpha_in (= SubnetAlphaIn), alpha_out, tao_in (= SubnetTAO)
- alpha_out_emission, alpha_in_emission, tao_in_emission
- pending_alpha_emission, **pending_root_emission (always 0)**
- subnet_volume (u128), network_registered_at, subnet_identity (V3), moving_price (I96F32)
- **There is no price field.** Compute it from weights or call `current_alpha_price`. The v10 SDK's constant-product slippage helpers are valid only at w = 0.5.

Storage key = `twox128(pallet) ++ twox128(item) ++ hasher(key)`. Check value: twox128("System") = `26aa394eea5630e07c48ae0c9558cef7`.

| Item (pallet) | Hasher | Decode |
|---|---|---|
| SubnetTAO, SubnetAlphaIn, SubnetAlphaOut, SubnetProtocolAlpha, SubnetTaoInEmission, SubnetExcessTao, SubnetAlphaIn/OutEmission (SubtensorModule) | Identity u16 LE | u64 rao |
| SubnetMovingPrice | Identity | I96F32 (i128 / 2^32) |
| SubnetFastMovingPrice | Identity | U64F64 / 2^64, OptionQuery |
| MinerBurned, RootProp | Identity | U96F32 / 2^32 |
| SubnetEmissionEnabled, SubtokenEnabled, NetworkRegistrationAllowed | Identity | bool (absent → default) |
| NetworkRegisteredAt, FirstEmissionBlockNumber, Tempo, EMAPriceHalvingBlocks, LastEpochBlock | Identity | u64 / Option<u64> |
| SubnetTaoFlow / SubnetEmaTaoFlow | Identity | i64 / (u64 block, I64F64): stale for emission |
| TaoWeight | value | u64 / u64::MAX |
| EmissionGateBar | value | U64F64 / 2^64 |
| SubnetMovingAlpha | value | I96F32 |
| EmissionBarRank, EmissionGateExponent, EmissionBarQuantile, SubnetLimit, NetworkImmunityPeriod, NetworkRateLimit, NetworkMinLockCost, NetworkLastLockCost, TotalIssuance, TaoInRefundDeploymentBlock, NominatorMinRequiredStake | value | — |
| TotalHotkeyAlpha (hotkey, netuid) | Blake2_128Concat + Identity | u64 |
| TotalHotkeyShares (V1, U64F64; removed from metadata at v473) / TotalHotkeySharesV2 (SafeFloat {mantissa u128, exponent i64} = m·10^e; from block 8,036,577) | same | — |
| AlphaDividendsPerSubnet (netuid, hotkey) | Identity + Blake2_128Concat | u64 post-take nominator alpha |
| Delegates, ChildkeyTake, ParentKeys/ChildKeys | — | — |
| Swap.FeeRate, Swap.SwapBalancer, Swap.BalancerTaoReservoir/AlphaReservoir | Twox64Concat: xxh64(le16, seed 0) as 8 LE bytes ++ le16 | u16 / Perquintill |
| MevShield.NextKey (1,184 B), NextKeyExpiresAt, PendingKey; LimitOrders.LimitOrdersEnabled; SafeMode.EnteredUntil; Proxy.Announcements; Timestamp.Now (ms) | — | — |

Pallet indices: SubtensorModule 7, SafeMode 20, Swap 28, MevShield 30, LimitOrders 32.

### 5.5 Writes: v11 intents
Source: `sdk/python/bittensor/intents/staking.py`, where `DEFAULT_RATE_TOLERANCE` = 0.05, valid range [0, 1).
```python
bt.AddStake(hotkey_ss58, netuid, amount_tao|'all', slippage_protection=True, rate_tolerance=0.05)
    # -> add_stake_limit(limit=int(spot_rao*(1+tol)), allow_partial=False); plain add_stake if protection off
bt.AddStakeLimit(hotkey_ss58, netuid, amount_tao, limit_price_rao: int, allow_partial=False)
bt.RemoveStake(hotkey_ss58, netuid, amount_alpha|'all', slippage_protection=True, rate_tolerance=0.05, claim=False)
bt.RemoveStakeLimit(hotkey_ss58, netuid, amount_alpha|'all', limit_price_rao: int, allow_partial=False, claim=False)
bt.UnstakeAll(hotkey_ss58, claim=False)   # all subnets incl. root, NO price limit, silently skips failing positions;
                                          # fails outright if ANY position is lock-constrained
bt.UnstakeAllAlpha(hotkey_ss58)           # alpha -> root, NO price limit
bt.SwapStake(hotkey_ss58, origin_netuid, dest_netuid, amount_alpha, slippage_protection=True, rate_tolerance=0.05)
    # swap_stake_limit, limit = (origin_price*1e9 // dest_price)*(1-tol), fill-or-kill
bt.MoveStake(origin_hotkey_ss58, origin_netuid, dest_hotkey_ss58, dest_netuid, amount_alpha,
             slippage_protection=True, rate_tolerance=0.05, claim=False)
    # cross-subnet: single move_stake_limit (runtime v448+); same subnet: plain move_stake (no swap fee, Pays::Yes)
bt.MoveSwapStake(...)                     # deprecated in favour of MoveStake: moves FULL amount, swaps amount-1 rao (1 rao dust)
bt.TransferStake(dest_coldkey_ss58, hotkey_ss58, origin_netuid, dest_netuid, amount_alpha, dest_hotkey_ss58=None)  # irreversible
bt.Batch(intents=[...])                   # Utility.batch_all — NOT allowed via Staking proxy (CallFiltered)
bt.AddProxy(delegate_ss58, proxy_type="Staking", delay=0); bt.RemoveProxy; bt.RemoveProxies; bt.CreatePureProxy; bt.ExecuteProxyAnnounced
bt.ClaimRootWithHotkey(hotkey_ss58=...); bt.SwapBasket(...)
```
- `'all'` for TAO = free − ED − 500,000 rao of headroom. That headroom is **below the actual add_stake fee (about 850k rao)**, so do not use `'all'` for buys.
- Amount fields accept float, str, Decimal or `'all'`.
- **Balance (v11):** `Balance(rao, netuid=0)`, `.from_tao(x)` (TAO only), `.from_alpha(x, netuid)` (netuid ≠ 0), `bt.tao()`, `bt.alpha(x, netuid)`, `bt.rao(n, netuid=0)`. `.tao` on an alpha balance raises `UnitMismatchError`, and so does cross-unit arithmetic. `==` across units returns False; comparing with a float raises `TypeError`.

### 5.6 Chain extrinsics (SubtensorModule; amounts in rao; limit_price in rao per alpha)

| Call | Index | Params |
|---|---|---|
| add_stake | 2 | hotkey, netuid, amount_staked |
| remove_stake | 3 | hotkey, netuid, amount_unstaked |
| unstake_all / unstake_all_alpha | 83 / 84 | hotkey |
| move_stake | 85 | origin_hotkey, destination_hotkey, origin_netuid, destination_netuid, alpha_amount |
| transfer_stake | 86 | destination_coldkey, hotkey, origin_netuid, destination_netuid, alpha_amount |
| swap_stake | 87 | hotkey, origin_netuid, destination_netuid, alpha_amount |
| add_stake_limit | 88 | hotkey, netuid, amount_staked, limit_price, allow_partial |
| remove_stake_limit | 89 | hotkey, netuid, amount_unstaked, limit_price, allow_partial |
| swap_stake_limit | 90 | …, limit_price, allow_partial |
| start_call | 92 | — |
| remove_stake_full_limit | 103 | hotkey, netuid, limit_price: Option |
| root_dissolve_network | 120 | origin = root |
| add_stake_burn | 132 | hotkey, netuid, amount, limit: Option |
| transfer_stake_and_hotkey | 143 | — |
| move_stake_limit | 149 | …, limit_price (min dest/origin ×1e9), allow_partial |
| increase_take / decrease_take | 66 / 65 | hotkey, take: PerU16 (Pays::Yes) |

- Since spec 469, `alpha_amount = u64::MAX` means "the whole live position" on move, transfer and swap.
- RPC 1010 "Invalid Transaction" Custom codes: 1 StakeAmountTooLow, 5 NotEnoughStakeToWithdraw, 6 RateLimitExceeded, 7 InsufficientLiquidity, 8 SlippageTooHigh, 9 TransferDisallowed, 26/27 DelegateTakeTooLow/High.
- Other errors: `SubtokenDisabled`, `NotEnoughBalanceToStake`, `HotKeyAccountNotExists`, `TooManyStakingHotkeys` (256 per coldkey; third parties can add at most 128), `StakeUnavailable` (locks or collateral), `TransferDisallowed` (only for transfer_stake*), `CallFiltered` (proxy), `Swap::PriceLimitExceeded`, `ReservesTooLow`, `SwapInputTooLarge`.
- Staking to a hotkey only requires that the hotkey exists. Whether it earns depends on §3.6.

### 5.7 Executor, plan, results, Policy
```python
plan   = await client.plan(intent, wallet, *, policy=None, proxy_for=None, proxy_type=None)  # fee, effects, warnings, violations
result = await client.execute(intent, wallet, *, policy=None, proxy_for=None, proxy_type=None, period=128,
                              wait_for_inclusion=True, wait_for_finalization=True, retries=0,
                              wait_for_registration=True, registration_timeout=None, on_progress=None)
result = await client.submit_shielded(intent, wallet, *, policy=None, proxy_for=None, proxy_type=None, period=8,
                                      wait_for_inclusion=True, wait_for_finalization=False)
await client.estimate_shielded_carrier_fee(fee_payer)
await client.submit_call(bt.calls.SubtensorModule.add_stake_limit(hotkey=..., netuid=..., amount_staked=rao,
                         limit_price=rao_per_alpha, allow_partial=False), wallet)
# ExtrinsicResult: success, message, block_hash, extrinsic_id, explorer_url, fee, events, error(.code,.remediation), data
#   result.raise_for_failure(); data['inner_extrinsic_hash'] for shielded
bt.Policy(max_fee_tao=None, max_spend_tao=None, allowed_netuids=None, allow_raw_calls=False)
```
- **`execute()` never MEV-shields.** `mev_shield_default=True` only sets the btcli default (`--mev-shield/--no-mev-shield`). The SDK redirects to shielding only for `mev_shield_required` intents (collateral buys). A Python bot must call `submit_shielded` explicitly.
- `retries` resubmits only on transient pool errors ("priority too low", stale nonce), never on dispatch failures. With `wait_for_finalization`, the SDK re-reads the canonical hash and rescans after a reorg.
- `Policy` checks are client-side and per intent, not cumulative.
  - A fee estimate that is unavailable while `max_fee_tao` is set fails closed.
  - `'all'` or TransferStake amounts count as unbounded spend.
  - UnstakeAll and UnstakeAllAlpha violate `allowed_netuids`.
  - Raw calls are refused only when a policy is active and `allow_raw_calls=False`.
  - A per-call policy **replaces** the client policy; they are not merged.
  - A Policy does not stop a thief who holds the key.

### 5.8 Proxy-based key safety: use `ProxyType::Staking` (index 8)
- Enum: Any 0, Owner 1, NonCritical 2, NonTransfer 3, Senate 4, NonFungible 5, Triumvirate 6, Governance 7, **Staking 8**, Registration 9, Transfer 10, SmallTransfer 11, RootWeights 12, ChildKeys 13, SudoUncheckedSetCode 14, SwapHotkey 15, SubnetLeaseBeneficiary 16, RootClaim 17, BasketTrading 18.
- **Staking allow-list:** add_stake, add_stake_limit, remove_stake, remove_stake_limit, remove_stake_full_limit, unstake_all, unstake_all_alpha, move_stake, move_stake_limit, swap_stake, swap_stake_limit, stake_into_basket, add_collateral, set_min_collateral.
- **Not allowed:** balance transfers, transfer_stake*, Utility.batch_all.
- NonTransfer allows batches, but also value-destroying calls (add_stake_burn, lock_stake, recycle_alpha, burn_alpha, register_network) and proxy management. Do not give it to the bot.
- Setup (signed once by the real coldkey; Ledger, Polkadot Vault QR and extension signers are supported):
  ```
  btcli proxy add --delegate ops --proxy-type Staking --delay 0 -w safe
  # or bt.AddProxy(delegate_ss58, proxy_type="Staking", delay=0)
  ```
- The bot runs `execute(intent, ops_wallet, proxy_for=REAL_COLDKEY, proxy_type="Staking")`, which wraps the call as `Proxy.proxy(real, force_proxy_type, call)`.
- Shielding composes with the proxy: `submit_shielded` encrypts the Proxy.proxy call, and the outer carrier is signed by the delegate.
- Inner-call fees are paid by the **delegate**, unless the real account opts in with `Proxy.set_real_pays_fee(delegate, true)`. Keep a TAO fee buffer on the delegate.
- **A proxied inner error appears as `Proxy.ProxyExecuted{result: Err}` under `ExtrinsicSuccess`.** Reconcile on `ProxyExecuted`.
- Deposits: 0.06 TAO base + 0.033 TAO per proxy; at most 20 proxies. Announcements: 0.036 + 0.068 TAO, at most 75.
- Delayed proxies need `announce`, then the delay, then execute; the real account can veto with `reject_announcement`.
- **Residual risk:** a leaked zero-delay Staking proxy cannot withdraw funds, but it can "drain value by forcing repeated high-slippage stake/unstake round trips that counterparties profit from". For example, an attacker can pre-buy a thin pool and push your stake into it without a limit.
- Hotkey files are unencrypted on disk. Coldkey password environment variables: `BT_WALLET_PASSWORD`, `BT_WALLET_PASSWORD_FILE`.

### 5.9 MEV protection
- **Mechanics:**
  - `MevShield.submit_encrypted(ciphertext ≤ 8192 B)` (pallet 30, call 1).
  - Encryption: ML-KEM-768 + XChaCha20-Poly1305 to `MevShield.NextKey`, the ML-KEM key of the author at slot+2.
  - Ciphertext layout: key_hash(16) ‖ kem_len(2) ‖ kem_ct ‖ nonce(24) ‖ aead_ct.
  - Era must be ≤8 blocks (`CheckMortality`; longer is rejected as `Stale`). The carrier is signed at nonce N and the inner extrinsic at N+1.
  - The SDK anchors the mortal era at the **finalized** head.
  - The proposer includes a carrier only if `key_hash == twox128(PendingKey)` at the parent block, then decrypts it and puts the inner extrinsic **immediately after** it in the same block. Undecryptable or invalid inners are silently dropped, and the carrier fee is still charged.
  - **Inclusion window is exactly block N+2** (12–24 s; `NextKeyExpiresAt` = now+3, exclusive). A carrier that misses N+2 can never be decrypted.
- **Events:** only `EncryptedSubmitted{id, who}`. **Common misconception:** there are no `DecryptedExecuted` / `DecryptedRejected` events. Find the inner by hash at carrier index + 1.
- **Measured** (2,673 carriers, blocks 9,239,851–9,240,850 and 9,236,200–9,237,399):
  - Inner decrypted and executed in the same block: **98.9%**. About 99.4% or better excluding one non-SDK cohort; 99.84% in one window.
  - Era-birth-to-inclusion latency: 1: 9, 2: 78, 3: 362, **4: 1,931**, 5: 293 blocks.
  - 0 missed Aura slots in 14,400 blocks.
  - The rate of carriers never included is **unmeasurable from chain data**.
- **Policy limits:** the SDK and CLI refuse to shield hotkey-signed calls. The chain itself accepts any signed origin.
- Priority is flat (Normal = 1), so **tips buy nothing**.
- Shielding hides intent only while pending. The signer and timing remain visible: if an account holds a single alpha position, a shielded transaction still signals an unstake (Talisman). The PoA author decrypts your trade. The docs say swaps well under about 1 TAO are unattractive front-running targets.
- MEV Shield mainnet activation about 2025-12-24 (secondary). SDK 10.0.0 (2025-12-10) already supported it.
- **Nonce lockout after a miss:** the stale carrier holds nonce n until its era expires (about 6 blocks). Flat priority prevents replacement. Wait for expiry, or rotate between 2–3 funded Staking-proxy delegates. This comes from pool semantics and is untested.

### 5.10 Limit orders (on-chain pallet)
- `LimitOrders.LimitOrdersEnabled` = true.
- Orders are signed off-chain as `VersionedOrder` V1/V2 with fields: signer, hotkey, netuid, order_type (LimitBuy: price ≤ limit; TakeProfit: price ≥ limit; StopLoss: price ≤ limit), amount, limit_price (u64::MAX means no ceiling, 0 means no floor), expiry (unix ms), fee_rate (Perbill), fee_recipient, relayer (up to 10), max_slippage, chain_id, partial_fills_enabled.
- A relayer submits them with `execute_orders(orders ≤100, should_fail)` (best-effort) or `execute_batched_orders(netuid, orders)` (atomic; buys and sells netted, only the residual hits the pool). The signer can `cancel_order`.
- **There is no SDK intent.** Use `bt.calls.LimitOrders.*`. Who runs relayers on mainnet, and at what fee, is (UNVERIFIED).

### 5.11 Rate limits
- **Staking:** none. There is no per-block stake/unstake limit and no cooldown. `StakingRateLimitExceeded` is declared at `errors.rs:95` and never raised; the auto-generated error page is stale. `CheckRateLimits` covers only weight commits/sets and `register_network`.
- Root only: `RootStakeUnlockInterval` is 0. Root staking is paused during a beta-basket seed.
- Take increases: once per 216,000 blocks. Childkey take: 216,000. Set children: 150. Single-subnet hotkey swap: once per 7,200 blocks.
- Owner-driven hotkey swaps that move delegated alpha have a 7,200-block cooldown (v473); delegator withdrawals are unrestricted.
- Subnet registration: 14,400 blocks network-wide.
- Public RPC: about 1 req/s documented; observed limits vary by endpoint (§6.4).

### 5.12 Fees (after the spec-467 halving)
- **Formula:** fee = base (~27,039 rao) + 0.5 rao/byte + 0.00025 rao per ref_time unit (`WEIGHT_FEE_PER_REF_TIME` = Perbill 250,000; `LENGTH_FEE_PER_BYTE` = Perbill 500,000,000). The multiplier is constant. `docs/concepts/transactions.mdx` still says 1 rao/byte; it is stale.
- **Measured `actual_fee`:**

| Call | rao |
|---|---|
| Carrier | p50 ≈ 94,554–94,566 (τ0.0000946); estimate_shielded_carrier_fee 98,000 worst case |
| add_stake_limit (direct) | 850,536 |
| Proxy(add_stake_limit) | 933,081 |
| remove_stake_limit | 645,119 |
| Proxy(remove_stake_limit) | 742,666 (quote) |
| Proxy(remove_stake_full_limit) | 749,660 |
| swap_stake_limit | 1,069,370 |

- **All-in per order on the production path:**
  - Buy ≈ 1,028k rao ≈ **τ0.00103**.
  - Sell ≈ **τ0.00083**.
  - Round trip ≈ τ0.00186, plus a 0.0504% swap fee per pool leg.
  - Relative to order size: 1.03% of a 0.1-TAO buy, 0.21% at 0.5 TAO, 0.10% at 1 TAO.
  - A→B rotation via `Proxy(swap_stake_limit)` ≈ τ0.00125, against τ0.00186 for a sell plus a buy.
- **A failed inner call pays the full fee**; since spec 469 some failed calls are refunded down to the work performed. The SDK comment "typical fees ~τ0.000125" is wrong for staking calls.
- **Disposition:** TAO transaction fees and tips are recycled, reducing issuance. They do not go to the author; only swap fees do.
- **Alpha-fee trap:** when the fee payer lacks TAO, a single-subnet staking call can pay its fee in alpha. If the price moves so that alpha cannot cover the fee, "the transaction still executes and all Alpha is withdrawn from the account". Always keep a TAO fee buffer.

### 5.13 Reconciliation rules
- **Do not size follow-ups from `StakeAdded`.** Share-pool truncation can make it 1 rao above the real position. Limit stakes refund unswapped TAO while `StakeAdded` may report the pre-refund amount, and `SubnetVolume` may be overstated.
- Read post-state balances. Use `u64::MAX` or `'all'` for whole-position move, swap or transfer.
- After `UnstakeAll`, re-read stake, because it can skip positions.
- Shielded orders: if the carrier is missing at N+2, the order missed, and that is final. Otherwise check the inner at carrier index + 1 and read `ExtrinsicFailed` / `ProxyExecuted` / `ItemFailed`.

### 5.14 Reference production buy (from docs; not executed in this research)
```python
async def shielded_limit_buy(client, ops_wallet, real_ck, vali_hk, netuid, amount_tao, max_marginal_move=0.02):
    px = await client.prices.alpha_price(netuid)                 # read from the SAME head node used for NextKey
    q = await client.prices.quote_stake(netuid, amount_tao)
    if not q.alpha: raise RuntimeError("sim failed (all-zero result)")
    limit = int(px["price_rao"] * (1 + max_marginal_move))       # strictly above spot or PriceLimitExceeded
    intent = bt.AddStakeLimit(hotkey_ss58=vali_hk, netuid=netuid, amount_tao=amount_tao,
                              limit_price_rao=limit, allow_partial=False)  # size <= max_buy_to_limit()
    plan = await client.plan(intent, ops_wallet, proxy_for=real_ck, proxy_type="Staking")
    if plan.violations: raise RuntimeError(plan.violations)
    return await client.submit_shielded(intent, ops_wallet, proxy_for=real_ck, proxy_type="Staking")
```

### 5.15 v10 (10.5.0) legacy summary
- `add_stake(wallet, netuid, hotkey_ss58, amount, safe_staking=False, allow_partial_stake=False, rate_tolerance=0.005, *, mev_protection=DEFAULT_MEV_PROTECTION, period=128, raise_error=False, wait_for_inclusion=True, wait_for_finalization=True, wait_for_revealed_execution=True)`.
- `unstake(..., safe_unstaking=False)`, `unstake_all(wallet, netuid, hotkey_ss58, rate_tolerance=0.005)` (= remove_stake_full_limit), `swap_stake(..., safe_swapping=False)`, `move_stake(...)` (no limit), `sim_swap(origin, dest, amount, block)`, `all_subnets(block)`, `get_subnet_price(s)`.
- Protection is **opt-in**. MEV protection is on only if `BT_MEV_PROTECTION` ∈ {1, true, yes, on}. `blocks_for_revealed_execution` = 3 (a parameter of `mev_submit_encrypted`, not `add_stake`). SDK v11 removed AsyncSubtensor, bt.config, bt.logging, axon/dendrite and MockSubtensor.

---

## 6. Data sources

### 6.1 Taostats (most complete indexed history)
- **Base:** `https://api.taostats.io`. Legacy routes are `/api/<group>/.../v1`. The new API is `/v1/...`: OpenAPI 3.1 at `/openapi.json`, 124 endpoints, backed by ClickHouse.
- **Auth:** raw header `Authorization: <key>` (no Bearer); the new API also accepts `?authorization=`. Key format `tao-<material>:<signature>`.
- **Errors:** 401/403 bad key; 429 over quota (honour `Retry-After`). On legacy routes a 404 means a missing `/api` prefix or `/v1` suffix. **Unknown `/v1` paths return 401, not 404.**
- **Paging:** `page` is 1-indexed; `limit` defaults to 50, max 200. (page−1)·limit must be below 1,000,000, otherwise 400; walk block or timestamp ranges for deep history.
- **Envelope:** `{data, pagination{current_page, per_page, total_items (exact), total_pages (capped), next_page, prev_page}}`. Amounts are RAO decimal strings. Timestamps are ISO-8601 with ms; range filters take Unix seconds.
- **Plans:** Free $0 (5 credits/min, 10k/month); Power $9 (20/min, 20k); Develop $49 (60/min, 50k); Scale $199 (240/min, 500k). Top-ups: 250k credits for $99, 1M for $199. The legacy table was 5/60/240 per min and 10k/50k/500k per month. **Credit cost per endpoint and tier gating are unpublished (UNVERIFIED).**
- **Terms:** no reselling data without written consent; respect rate limits.
- **Key endpoints:**
  - `/v1/subnets/pools/history?netuid=(required)&frequency=by_block|by_hour|by_day&block_start&block_end&timestamp_start&timestamp_end&order_dir&limit&page`
    - Fields: price, market_cap, liquidity, tao_in_pool, alpha_in_pool, alpha_staked, total_alpha, root_prop, startup_mode, subnet_emission_enabled (null below spec 411), subnet_protocol_alpha (always null on history).
    - **by_day = block % 7200 == 0 (stamped about 10:02:36Z); by_hour = block % 300.** One subnet had 594 daily rows against 4,253,418 by_block rows.
  - `/v1/subnets/pools?netuid=` (latest; has subnet_protocol_alpha); `/v1/subnets/pools/aggregate` (24h buy/sell volume, buyers/sellers, price_change 1h/1d/1w/1m, sentiment_index, seven_day_prices); `/v1/subnets/pools/total-price/history` (wall-clock buckets at 23:59:48Z).
  - `/v1/tradingview/udf/history?symbol=SUB-<netuid>&resolution=1|5|15|60|240|1D|7D|30D&from=&to=(required, inclusive)&countback=` → `{s,t,o,h,l,c,v,nextTime}`. v is alpha volume and includes liquidation payouts; `SUB--1` is the total series.
  - `/v1/price/ohlc?asset=TAO&period=1m|1h|1d` (**end is exclusive**, 4 dp); `/v1/price/history` (end inclusive).
  - `/v1/subnets/trades`: **no netuid filter**; use `from_name=TAO&to_name=SN{n}` for buys, the reverse for sells. Also tao_value_min/max (rao) and block/timestamp ranges. `extrinsic_id` is not unique.
  - `/v1/subnets/stake-events?netuid&coldkey&hotkey&action=stake|unstake|all&is_transfer&trades_only=true`
    - `trades_only` drops stake transfers, hotkey-swap legs and (from block 6,067,944) within-subnet moves.
    - `alpha_price_in_tao` is the **execution** price. `alpha_price_in_usd` is 2 dp ("0.00" below half a cent).
    - Other fields: fee, slippage, validator_swap, registration_collateral (spec 435+).
  - `/v1/subnets/swaps`; `/v1/subnets/epochs?netuid&block_start&block_end` (per-block tao_in/alpha_in/alpha_out emission; **owner_cut, root_alpha_divs, server_emission and validator_emission are accumulated pending balances**).
  - `/v1/subnets/history?frequency=` (by_block = every 300th block; `excess_tao` = "0" before spec 411).
  - `/v1/subnets/registrations`, `/owners` (is_coldkey_swap), `/deregistrations[?is_immune=]` and `/history` (rank **includes immune subnets interleaved**; moving_price as raw I96F32 bits). Use the lowest-rank row with `is_immune = false`.
  - `/v1/subnets/metrics`, `/identities`, `/hyperparameters`, `/metagraph(...)`, `/distribution/coldkey|incentive|ip`, `/conviction(/history)`, `/burns`.
  - `/v1/alpha/leaderboard?netuid=`, `/v1/alpha/hotkey-shares`.
  - `/v1/validators/yield` (formula undocumented), `/v1/tokenomics/emission` (block_number, emission, total_issuance, timestamp), `/v1/historic/stake-events` (pre-dTAO).
  - `/v1/coingecko/events?fromBlock&toBlock` (≤20 blocks), `/v1/cmc/*`.
  - RPC: `POST /v1/rpc/http` (forwards to finney_lite) and `WS /v1/rpc/ws/{finney_lite|finney_archive}` (credit cost unknown).
  - Legacy: `/api/dtao/pool/history/v1` (v3-era fields: alpha_sqrt_price, current_tick, liquidity_raw, fee_rate, …), `/api/dtao/pool/latest/v1`, `/api/dtao/tao_flow/v1`, `/api/dtao/slippage/v1`, `/api/dtao/trade/v1`, `/api/dtao/subnet_emission/v1`, `/api/subnet/pruning/(latest|history)/v1`, `/api/dtao/hotkey_alpha_shares/history/v1`, `/api/dtao/validator/dividends/history/v1`, `/api/dtao/validator/yield/history/v1`, `/api/dtao/validator/basket/history/v1`, `/api/dev_activity/*`.
  - **Common misconception:** `/api/subnet/get-pool-latest`, `/api/delegation/get-slippage` and `/api/delegation/get-trade` do not exist.
- **Not available from Taostats pool history:** MinerBurned, EmissionGateBar, a clean SubnetMovingPrice series, or any generation key.

### 6.2 tao.app (`https://api.tao.app`, header `X-API-Key`; OpenAPI at `/openapi.json`)
- **"Free" endpoints still require a key:**
  - `/api/beta/analytics/dynamic-info/aggregated?interval=1min|5min|15min|1hour|1day&netuid=5,14&start&end&page_size≤1000`. Intervals under 1 hour are capped at 10,000 buckets. Fields: price, alpha_in/out, tao_in, *_emission, pending_*.
  - `/api/beta/accounting/price-at-block?netuid&block`; `/api/beta/analytics/subnets/holders`; `/api/beta/block/events?event_name&netuid&start&end`; `/api/beta/subnet_screener`; `/api/beta/price-sustainability`; `/api/beta/subnets/identity-changes`; `/api/beta/chain/runtime-version`.
- **Paid:** `/api/beta/subnets/ohlc?netuid&start&end&interval_minutes=1..43200`, `/api/beta/analytics/subnets/aggregated`, `/transactions`, `/api/beta/apy/alpha`.
- **Public, no key:** `/api/v1/subnets/ohlc?netuid=19&period=24h|7d|30d` (15-minute / 1-hour / 4-hour candles; the newest candle is still open).
- Pricing, rate limits and terms are (UNVERIFIED); the developer page was blocked by Cloudflare.

### 6.3 Others
- **TaoMarketCap** (undocumented, no key, fragile): `https://api.taomarketcap.com/public/v1/subnets/`, `/subnets/{netuid}/`, `/blocks/`. Raw `latest_snapshot` chain fields.
- **CoinGecko:** category `bittensor-subnets` (117–118 coins). Headers `x-cg-demo-api-key` / `x-cg-pro-api-key`. Its feed derives from Taostats, slugs are stale after netuid reuse, and the free tier has 365 days of history.
- **CMC:** lists only tokens with tracked liquidity; historical quotes need a paid plan.
- **Open-source indexers:** `unitone-labs/bittensor-indexer` (crate last updated 2025-07, likely stale), `sagarregmi2056/subnet-data-indexer-bittensor`, `RyanMercier/OpenTaoAPI` (30-minute poller). No maintained Subsquid or SubQuery indexer was found.

### 6.4 Archive approach (only an archive node holds every series)

| Endpoint | Observed behaviour (2026-10-08, one IP) |
|---|---|
| `https://bittensor-finney.api.onfinality.io/public` | Full archive (block 3,000,000 readable). `state_queryStorageAt` with 2,332 keys ≈ 220 KB in 0.45 s. **Sustained 3.4 req/s (1.7 snapshots/s) unthrottled.** Bursts above ~25 req/s give -32029; ~8 concurrent requests also trip it. JSON-RPC batch arrays rejected. `state_queryStorage` and `state_getKeysPaged` work historically. Documented public limit 5 req/s; RU-based generic limits. |
| OnFinality keyed plans | Free Developer: 400k RU/day, ≤40 RU/s, archive included. Growth $49/mo (20M RU, 200 RU/s). Accelerate $249/mo (100M RU). Bittensor responses cost 1 RU each. Terms: free tier as-is, internal use, no resale. |
| `archive.chain.opentensor.ai` | Every dTAO block. The JSON-RPC **-32004 "Historical work rate limit exceeded"** budget (`historical_references`) runs out after ~10–100 historical calls and can stay exhausted for minutes. Also `http_60s` limits. Fallback only. |
| Own archive node | `--pruning archive`, ≥3.5 TB NVMe and growing, 4+ cores, 16 GB+ RAM, sync takes days. The only practical option for every block × every subnet × many hotkeys. |

**Recommended pipeline:**
1. Snapshot every 60 blocks (a multiple of both 300 and 360). Each snapshot is `chain_getBlockHash` plus one `state_queryStorageAt` of up to about 2,300 keys:
   - per subnet: SubnetTAO, SubnetAlphaIn, SubnetAlphaOut, SubnetMovingPrice, SubnetVolume, RootProp, MinerBurned, SubnetEmissionEnabled, FirstEmissionBlockNumber, NetworkRegisteredAt, SubnetOwner, SubtokenEnabled, NetworkRegistrationAllowed, EMAPriceHalvingBlocks, SubnetMechanism, SubnetTaoFlow, SubnetEmaTaoFlow, SubnetProtocolAlpha, plus Swap.SwapBalancer / FeeRate (Balancer era) or AlphaSqrtPrice / CurrentLiquidity / FeeRate (v3 era);
   - globals: SubnetMovingAlpha, EmissionGateBar, EmissionBarQuantile/Rank, EmissionGateExponent, TaoWeight, SubnetLimit, NetworkRateLimit, NetworkImmunityPeriod, TotalIssuance.
2. Add a second call for selected hotkeys: TotalHotkeyAlpha, TotalHotkeySharesV2 and legacy TotalHotkeyShares.
3. Cost from dTAO launch: about 72k snapshots, about 144k RU, which is under half a day of the free OnFinality key or about 12 h on the public URL. Every 300 blocks is about 14.4k snapshots, about 2.4 h.
4. Fill in absent ValueQuery keys from `state_getMetadata` **of that block's runtime**, cached per spec version; defaults change between runtimes.
5. Between snapshots, interpolate or rebuild the EMA with the verified formula.
6. Track only the relevant hotkeys. At head there are at least 40k TotalHotkeyAlpha keys.
7. Use Taostats only as an independent check (at block % 300 == 0) and for trades and volume. Volume can also come from differencing `SubnetVolume`.
- **Exact-event indexing** (System.Events across ~4.32M blocks) requires your own archive node.
- Event layouts (positional): `StakeAdded/StakeRemoved(cold, hot, tao, alpha, netuid, fee)`, `StakeMoved(cold, hot_o, netuid_o, hot_d, netuid_d, tao)`, `StakeTransferred(...)`, `StakeSwapped(...)`, `NetworkAdded(netuid, mechid)`, `NetworkRemoved(netuid)`, `NetworkRegistrationQueued{…}`, `SubnetOwnerChanged{…}`, `AlphaRecycled`, `AlphaBurned`, `TransactionFeePaidWithAlpha{…}`, `StakeLocked/Unlocked`, `SubnetEmissionEnabledSet`, and the `Basket*` events.
- Drop StakeRemoved+StakeAdded pairs with fee = 0 and the same netuid in one extrinsic (`transfer_stake_within_subnet`).

### 6.5 Price series and API availability by era

| Era | Blocks | Price | Runtime API |
|---|---|---|---|
| A | 4,920,351 → v3 init per subnet | SubnetTAO/SubnetAlphaIn | `current_alpha_price` absent at 5.0M and 5.9M |
| B | 5,947,549 → 8,486,593 | AlphaSqrtPrice² (T/A acceptable only before 6,205,195); a reused netuid is uninitialised (price T/A) until its first swap | `current_alpha_price` from ≤6.2M; `sim_swap` from 6,262,253; `current_alpha_price_all` from spec 391 |
| C | 8,486,594 → | ((1−q)/q)·T/A | all |

- `get_all_dynamic_info` works at historical blocks but needs SCALE type metadata per runtime (13–51 KB per call).
- The block that contains `setCode` already reports the new spec, but the new logic runs from the next block. Examples: 8,466,530 reports 421; 8,486,593 reports 423.

### 6.6 History depth
- dTAO began at block 4,920,351 (2025-02-13 21:41 UTC). Head is about 9,240,4xx, so about 4.32M blocks, about 600 days.
- Taoflow storage exists only from about 2025-11-04. RootProp from 7,135,420. SubnetExcessTao and SubnetEmissionEnabled from spec 411 (8,283,784). MinerBurned from 8,466,597.
- AlphaV2 from 8,036,577; legacy Alpha fully drained by 9,217,507. Inside that window, read both, and legacy wins on overlap.
- Before dTAO, stake history is only `/v1/historic/stake-events`.

### 6.7 Quirks list (mask or flag in backtests)
1. Early dTAO pools were tiny (SN19 at block 4,920,400: 0.85 TAO / 18.56 alpha), so the first weeks are extremely noisy.
2. The v3 price source applies per subnet. **T/A mismeasures v3-era prices in small pools:** median gap 0.03–0.05%, but 16% on SN103 at 7.5M and 7.7% on SN104 at 6.5M. Any factor computed on T/A for Aug 2025–Jun 2026 is biased in exactly the micro caps.
3. Default fee was 0.3% in specs 290–292. Per-subnet fee overrides existed in the v3 era.
4. Within-subnet moves bypass the pool from block 6,067,944, so earlier volume includes them.
5. Halving at 7,103,975.
6. `SubnetEmaTaoFlow` is frozen at 8,466,530 (115 entries). `SubnetTaoFlow` has since become a running total of net user flow: use ΔSubnetTaoFlow for net flow. It is invalid across re-registration (deleted on dissolve) and includes registration-collateral buys (spec 435+).
7. Σ `SubnetTaoInEmission` undercounts from v3.3.1-362 (2025-12-12) until spec 411, because chain buys were not recorded (0.303 of 0.5 TAO at block 8.0M). Chain-buy accounting was patched in v3.4.1-413 and v3.4.2-415.
8. `RootProp` reads 0 before 7,135,420; compute it yourself.
9. The Balancer migration on 2026-06-25 can cause a per-subnet price jump and a depth discontinuity (SN103 lost about 10% of depth).
10. v3.3.15-402 (2026-05-08) removed initial owner alpha, which can make supply jump.
11. `SubnetEmissionEnabled`: an absent key means true, but new subnets explicitly write false.
12. Netuid reuse about weekly; generations splice in pool history; CoinGecko slugs go stale.
13. Liquidation payouts are counted in UDF volume but excluded from Taostats `sells_24_hr`.
14. USD fields are rounded to 2 dp, which is useless for micro caps.
15. Root is priced at 1.0 by convention.
16. The EMA input is capped at 1.0 TAO.
17. `BlockEmission` storage is stale. `DynamicInfo.emission` and `pending_root_emission` are hard-coded 0.
18. `SubnetAlphaOut` includes protocol and burned alpha. The staker base is approximately AlphaOut − ProtocolAlpha; for yield use the TotalHotkeyAlpha sum.
19. The `StakeAdded` 1-rao overstatement and partial-limit refund distortions.
20. Docs and runtime-initial constants differ from chain:

| Parameter | Docs / runtime initial | Chain |
|---|---|---|
| Immunity | 1,296,000 | 864,000 |
| Min lock | 1,000 TAO | 1 TAO |
| Registration rate limit | 7,200 per coldkey | 14,400 global |
| Lock reduction interval | 100,800 | 115,200 (57,600 effective) |
| SubnetMovingAlpha | 0.000003 | 0.0003 |
| TaoWeight | 5.27% | 0.18 |
| MaturityRate | 934,866 | 311,622 |

21. "Recycle" reduces issuance and "burn" keeps it counted. Some sources write "burned (issuance reduced)" for transaction fees, which is recycling.
22. The v441 release page labels its snapshot "block 8,922,321 · July 24, 2026", but that block's timestamp is 2026-08-25. The actual v441 deploy was block 8,765,683 (2026-08-03).
23. 2025-05-20 chain freeze: block 5,611,658 at 22:05:12 UTC, next block 2025-05-21 00:38:48 (no blocks for about 2h34m; cause unknown).
24. Spec 444 never ran on mainnet.
25. Taostats `/v1/subnets/epochs` pending fields are accumulations; deregistration rank includes immune subnets.

### 6.8 Monitoring feeds the bot needs every block or epoch
- **Prune state:** SubnetMovingPrice, NetworkRegisteredAt, NetworkImmunityPeriod, `LastRateLimitedBlock(0x02)`, NetworkLastLockCost, `get_subnet_to_prune()`.
- **Emission and protocol state:** SubnetEmissionEnabled (or the `SubnetEmissionEnabledSet` event), MinerBurned, EmissionGateBar, Σ EMA (root sell flag).
- **Pool state:** SwapBalancer, FeeRate, reserves.
- **Owner and conviction:** owner stake, lock events, OwnerCutAutoLockEnabled.
- **Validator:** Delegates, AlphaDividendsPerSubnet membership.
- **Baskets:** escrow E per subnet via `get_all_validator_baskets`, and `BasketRedeemedTao`.
- **Chain health:** spec_version, `SafeMode.EnteredUntil`, block-time/finality stall.

---

## 7. Market structure facts (2026-10-08, TAO ≈ $268.37)

### 7.1 Size distribution and micro-cap definition
- **Pool TAO across 128 subnets:** sum ≈ 1.983M TAO (≈$532M); the top 10 hold 48.1%.

| Percentile | Nearest-rank | Interpolated |
|---|---|---|
| min | 241 (SN70) | |
| p10 | 984 | 1,157 |
| p25 | 3,070 | 3,038 |
| median | 6,741 | 6,738 |
| p75 | 14,025 | 13,733 |
| p90 | 33,675 | 32,724 |
| max | 202,368 (SN64) | |

  Next largest: SN51 ≈ 170k and SN4 ≈ 133.5k.
- **Spot market cap (spot × AlphaOut):** sum ≈ 3.65M TAO (≈$981M). p10 1.1k–1.9k, p25 6.1k–6.2k, median 11.1k–11.2k, p75 25.4k–26.3k, p90 68k–76k TAO.

| Pool-size tercile | Pool range (TAO) | Median pool | Median spot mcap |
|---|---|---|---|
| Bottom | 241–4,825 | 1,647–1,662 | 3,857 TAO (~$1.0M) |
| Middle | 5,261–10,455 | 6,759 | 11,221 (~$3.0M) |
| Top | 10,779–202,379 | 23,460 | 33,054–33,100 (~$8.9M) |

- **Working definition:**
  - **Micro cap:** pool TAO < ~3,000 (bottom quartile, ~32 subnets, < ~$0.8M) or spot mcap < ~6,000 TAO.
  - **Nano:** pool < ~1,000–1,150 TAO (bottom decile).
- Smallest pools: SN70 241, SN103 293, SN36 305, SN16 320, SN35 382, SN99 390, SN76 472, SN59 530. These are mostly new, recently re-registered, emission-disabled or near the prune line.
- Liquidity context (FalconX, May 2026, secondary): only 4 subnets had ≥$1M of 2% AMM depth; over a third had volume driven mainly by their top 20 holders; only 9 subnets had more than 1,000 traders.
- CoinGecko tail: several micro caps print under $1K of 24h volume (UNVERIFIED snapshot).

### 7.2 Emission concentration and dilution
- TAO/day received, median by pool tercile: bottom **0** (sum 140), middle 0.17–0.18, top 29.4.
- Every started subnet mints 7,200 alpha/day. Typical micro cap (pool 1,000–2,000 TAO, price 0.002–0.004): the miner share alone is about 6–12 TAO/day of potential sells, 0.4–1.2% of the pool per day, against a median injection of 0.
- MinerBurned ≥ 0.99 on 26–27 subnets; above 0 on 64.

### 7.3 Volatility and correlation
- Average daily cross-sectional SD of TAO-denominated log returns: 10.9% (Feb–Nov 2025), 5.5% (Taoflow era), **5.3–5.4% (gate era)** (verified).
- (UNVERIFIED, single computation) 88 days to 2026-10-08, TAO/USD annualised vol 72.5%:

| Tercile | USD beta (median) | Ratio vol (alpha/TAO) | USD vol |
|---|---|---|---|
| Bottom | 1.04 (IQR 0.93–1.23) | 135% | 157% |
| Middle | 0.98 | 65% | 98% |
| Top | 1.00 | 46% | 84% |

  The alpha/TAO ratio was roughly uncorrelated with TAO/USD (≈0). Over 365 days, bottom-tercile ratio vol was 173%.

### 7.4 Launch dynamics (regime-dependent)
- **Cohort A (Feb–Sep 2025, n = 65; UNVERIFIED):**
  - Pool at first price move: median 4 TAO.
  - 14-day maximum run-up: median +44%, p75 +164%.
  - Path: day 30 −72%, day 90 −83%, day 180 −99.9% (18% positive).
- **Cohort B (Oct 2025–Apr 2026, n = 30; UNVERIFIED):** near-empty pools; day 30 −29%, day 180 −58%.
- **Cohort C (full-lock seeding since ~2026-04-25, n = 21):**

| Metric | Verifier replication | Original computation |
|---|---|---|
| Pool at first move | ~1,004 TAO | 1,227 TAO |
| Median 14-day max run-up | +6% | +9% |
| Day 7 / day 14 | 0% (60% positive) | — |
| Day 30 | −4.7% (50% positive) | −25% |
| Day 60 | −57.7% (29% positive) | −66% |
| Day 90 | **−68.3% (n = 11, 0% positive)** | −74% |

- Right tail: SN107 (re-registered Feb 2026) now ranks 5th by emission; SN90 (re-registered 2026-07-15) ranks 14th (96 TAO/day). **Common misconception:** SN90 is not top-10.
- First-block sniping cannot be seen at daily resolution, and no quantitative study exists.

### 7.5 Deregistration history
52 prunes from 2025-10-19 to 2026-10-04, about one every 6–8 days (§4.4). Current target: SN92 (EMA 0.00136, registered at block 8,352,006, non-immune since 9,216,006).

### 7.6 Manipulation and failure patterns
- **SN28 memecoin loop (Mar 2025):** miners were scored on holding SN28 alpha. The token reached about #7 by market cap, then fell about 98% within hours after OTF ran root validator code (secondary; UNVERIFIED).
- **MEV sandwiching** of add/remove_stake was the top owner complaint in 2025 (Macrocosmos sentiment study). Slippage limits mitigated it, then MEV Shield (about 2025-12-24).
- **Taoflow gaming:** 67 of 128 subnets burned ≥99% of miner incentive to avoid recorded outflows (Taostats via FalconX, 2026-04-16). Under Taoflow, SN64 and SN4 received $0/day while SN97 received about 8% of its pool per day (secondary).
- **Covenant exit (2026-04-09/10):** Templar SN3, Basilica SN39, Grail SN81. About 37k TAO (~$11M) of alpha was sold. Subnet tokens fell up to 40% almost immediately (Templar >50%), and TAO fell 24–30%. This led to Conviction (2026-05) and the 18% ownership gate (v447). Const's "<1%" statement is (UNVERIFIED).
- **ORO SN15 owner wallet drain (2026-07-13):** social engineering, ~147k alpha sold over ~10 h (~$630K). SN102 lost 2,500 TAO (secondary).
- **Quasar SN24** fell about 75% in hours on plagiarism allegations, and its emissions about 86% in the v440 reshuffle (secondary; UNVERIFIED).
- **Emission purge:** 2026-06-22, 54–57 subnets switched off, with weekly reviews.
- **Concentration:** single traders can move even large subnets by 10% or more (FalconX phrasing: "a handful of traders").
- **SN8 listed on Kraken spot (2026-07-16):** creates CEX/on-chain basis for large caps only.

### 7.7 Aggregate context (UNVERIFIED where single-source)
- Alpha market cap: $1.47B (2026-03-29, CoinDesk); 4.06M TAO (2026-07-24, v441 page); $1.20B on CoinGecko vs $981M on-chain (2026-10-08).
- Root holds about 5.45M TAO of 7.44M total stake.
- Equal-weight all-subnet index, TAO-denominated cumulative log return: −0.335 over 365 days and −0.334 in the gate era. Bottom tercile: −0.582 and −0.668. Median bottom-tercile subnet over 365 days: −0.563 (67% negative).

---

## 8. Evidence on strategies

Source quality key: **[P]** primary on-chain or code; **[A]** academic preprint; **[R]** researcher replication from archive state, independently re-run; **[S]** secondary or marketing.

### 8.1 Size premium (small minus big)
- **[A]** Maymin, "Common Risk Factors in Decentralized AI Subnets", arXiv 2603.29751 (2026-03-31). The author co-owns a Bittensor project (Djinn).
  - Data: Taostats daily, 2025-02-14 to 2026-03-26; equal-weight terciles; daily rebalance; TAO-denominated.
  - **SMB +1.01%/day** (NW t 3.28, Sharpe 3.84). It fell from 1.17% to 0.51%/day around the Dec 2025 halving / Taoflow start (p = 0.044).
  - Proposed mechanism: emission staking moves price by about 2Δτ/τ.
  - **Capacity:** small-tercile median reserve about 540 TAO. One-way slippage 0.64% at $10K AUM, 6.39% at $100K, 63.9% at $1M. **Net SMB +0.36%/day at $10K and −5.48%/day at $100K.**
- **[R]** Replication from archive state reproduces SMB at +1.00%/day over Maymin's window. **Since 2026-06-22 (price-based emission), price-only SMB has reversed:**

| Period | SMB %/day | t |
|---|---|---|
| Post-2026-06-22 | −0.50 to −0.59 | −2.0 to −2.5 |
| Gate era (from 2026-07-28) | −0.47 to −0.56 | about −2.0 to −2.6 |
| Gate era, including unstarted subnets | about −0.39 | about −1.5 |

  Gate-era small tercile: −0.57 to −0.67%/day. Big: −0.01 to −0.12%/day. Median gate-era cumulative log return by pool tercile: −0.34 to −0.42 (72–81% negative), −0.27 to −0.30, −0.13.
- **Caveats that change the reading:**
  1. These are **price-only**. Nominator yield runs about 0.3–0.6%/day on micro caps against about 0.1%/day on large caps **[P]**, a spread of +0.2 to +0.5%/day, the same order as the reversal. **Total-return SMB has not been computed, and its sign is uncertain.**
  2. Pre-June eras used T/A, which mismeasures v3-era micro-cap prices (§6.7).
  3. The post-June sample is only 72–109 days.
  4. All figures are gross of fees and slippage, and capacity is tiny.

### 8.2 Momentum and reversal
- **[A]** Maymin full sample: WML7 +0.75%/day (t 3.05), WML30 +0.68 (t 3.69), REV −0.86 (t −3.62), interpreted as continuation. Both weaken after the split at 2025-09-05 (WML30 0.94 → 0.47; REV −1.40 → −0.31).
- **[R]** 1-day hold, equal-weight terciles:

| Period | WML7 %/day | WML30 %/day | REV %/day |
|---|---|---|---|
| Taoflow | +0.38 (t 2.47) | +0.25 | −0.46 (t −3.08) |
| Post-June 2026 | +0.36 to +0.55 (t 1.9–3.9) | +0.19 (t 0.84) | −0.49 to −0.53 (t −3.2 to −3.5) |
| Gate era | +0.43 to +0.45 (t 2.4–2.8) | +0.23 (n.s.) | −0.53 to −0.57 (t −2.8 to −3.3) |

  **1-day continuation and 7-day momentum persist; 30-day momentum is no longer significant.**
- Information-coefficient tests, last 365 days (naive t): 1d→1d IC +0.026 (t 3.8); 7d→7d −0.014 (n.s.). Micro-cap tercile in the Taoflow era: 7d→7d IC −0.083, i.e. weekly reversal driven by flows. Gate era: 1-day spread +0.77%/day (t 4.6). Not independently re-run.
- **Plausible mechanism [P]:** price → EMA (8 h half-life) → emission share → chain buys is reflexive, and strongest near the gate bar ("rank 36 +10% demand → +26% emission", official v440 page).
- **[S]** SubnetEdge: large inflows (e.g. SN56 +1,521 TAO) hold for about 2 days, then correct.

### 8.3 What has failed or is structurally disadvantaged
- **Passive equal-weight micro-cap long, price-only, in the gate era:** negative (above). TAO-denominated EW indices fell over the last year (§7.7).
- **Buying seeded new launches:** −68% to −74% by day 90, with 0 of 11 positive **[R]**.
- **Holding through a prune:** recovery about 0.35–0.65× spot **[P]**.
- **Holding gated or disabled subnets:** dilution with no bid **[P]**.
- **Using v10 defaults** (no slippage protection, no shield) or v11 default `AddStake` sizes above 2.47% of the reserve (guaranteed `SlippageTooHigh`) **[P]**.

### 8.4 Claimed or untested approaches (all [S], UNVERIFIED)
- `buckZz7/dtao-trader`: uses `SubnetExcessTao` as a "chain-buy floor", tracks kill switches, and claims emission harvesting of about 1%/day is best. Its "1% swap fee" is wrong (the real fee is 0.05%). Measured micro-cap nominator yield is 0.3–0.6%/day net.
- `0xRozier/Bittensor-trading`: hourly z-scored factors (24h/7d momentum, price vs EMA, net flow / liquidity, emissions / mcap). The backtest is synthetic: +22–34% over 180 days, Sharpe 1.8–2.9.
- `tududes/btt-subnet-dca`: EMA mean-reversion DCA with slippage-targeted sizing.
- `unconst` gist: (emission − price)/emission rotation.
- FlowSniper ($29/month, no published returns; its "Yield Illusion": median 30-day return −7.2%, 73% of subnets losing), Stakao, SubnetRadar, chainbuying screener.
- Yuma Asset Management Composite: −31.9% vs TAO −46.1% since launch (self-reported). DSV Fund: discretionary.

### 8.5 Structural hypotheses worth testing (derived from mechanics, not evidence)
- **Gate-boundary trading:** subnets near ranks 25–40 by burn-adjusted share have the highest emission elasticity and get chain-buy support once above θ.
- **Prune avoidance and prune-candidate shorting are not possible.** There is no shorting: `ShortsEnabled` = false. Avoidance rules follow §4.4.
- **Emission enable events:** a new or disabled subnet being enabled starts tao_in and chain buys. Watch `SubnetEmissionEnabledSet`.
- **Basket claim bursts:** pro-rata claim sales hit every escrow holding at once.
- **Always evaluate on total return:** price × share-price index, net of fees and slippage, on era-correct prices and generation-keyed assets, including dereg payouts.

---

## 9. Risk register

| # | Risk | Likelihood | Impact | Concrete mitigation |
|---|---|---|---|---|
| 1 | **Held subnet pruned** | High for the bottom ~5 non-immune by EMA; one prune per ~6–8 days | Critical: same-block removal, recovery ~0.35–0.65× spot, no mempool warning | Every block compute the target and each holding's t* (§4.4). Exit if t* < unwind time and the rate-limit window is open or opens within that time; exit if in the bottom 3 non-immune and cost ≤ ~1.2× last lock; never hold the current target. Track immunity expiries (SN16 at block 9,324,646, SN99 at 9,436,056). Use the registration hazard CDF; hazard is 0 within 14,400 blocks of the last registration. |
| 2 | Root switches off emission | Medium (waves Jun/Sep 2026; new subnets start off) | High: bid disappears, dilution continues; leading indicator of prune | Subscribe to `SubnetEmissionEnabledSet`; treat as an exit signal; don't count on injection for new subnets until enabled. |
| 3 | Gate starvation and dilution of tail subnets | High below rank ~32–40 | High: persistent sell pressure | Model g(s) and chain buys explicitly; net expected yield against dilution; avoid g ≈ 0 subnets unless there is strong independent flow. |
| 4 | Protocol regime or parameter change | High (≈6 emission regimes in 11 months; governance-set constants) | High: invalidates models and backtests | Read every parameter live; never hard-code; halt on `spec_version` change until revalidated; watch RaoFoundation releases. |
| 5 | Runtime upgrade breaks the SDK or call semantics | High (~30 releases in 3 months; hotfixes) | Medium | Pin SDK with hashes; testnet CI per release; smoke-test reads, quotes and plan() before resuming. |
| 6 | Exit liquidity and slippage | High | High | Cap each position at 1–2% of the pool's TAO reserve (V_max = T·s/(1−s)); limit orders; tranche exits only where flow refills; pre-trade `sim_swap`. |
| 7 | Owner or insider dump | Medium | High (−40–50% within hours) | Monitor owner coldkey and hotkey stake, `OwnerCutAutoLockEnabled` (off on ~115 subnets), lock-to-decaying events and holder concentration; size down where the owner cut is liquid. |
| 8 | Bot key compromise | Medium | Critical | Coldkey on Ledger or Vault; bot gets only a zero-delay **Staking** proxy holding fee TAO; per-strategy coldkeys holding only strategy capital; revoke idle proxies; delayed NonTransfer manager guarded like a coldkey; alert on unexpected stake moves. |
| 9 | Supply-chain compromise | Medium (2024, 2025 incidents) | Critical | `--require-hashes`, Trusted Publishing provenance, no typosquats, isolated host/WSL, review new releases before use. |
| 10 | MEV or sandwich | Medium (lower with shield) | Medium | `submit_shielded` plus a tight limit; avoid a single-position account signature; shielding adds little value under ~1 TAO. |
| 11 | Shield miss or nonce lockout | Low–Medium (~1% non-decrypt; never-included rate unmeasured) | Low–Medium | Read NextKey from a head-synced node; reconcile at N+2; rotate 2–3 delegates; refuse to submit if the node lags. |
| 12 | Limit failures (`PriceLimitExceeded`/`SlippageTooHigh`) with fees paid | Medium (1–3% shielded; 7–19% in the unshielded population) | Low | Re-quote right before submit; account for N+2 latency; size ≤ `max_buy_to_limit`; `allow_partial` where acceptable. |
| 13 | SafeMode or chain halt | Low (2024: 10-day SafeMode; 2025-05-20: 2h34m freeze) | High (no exits) | Stall detector on block time and finality; read `SafeMode.EnteredUntil`; position limits are the only defence. |
| 14 | Validator take hike or permit/dividend loss | Medium | Low–Medium | Each epoch check `Delegates`, `AlphaDividendsPerSubnet` membership and `ChildkeyTake`; switch hotkeys with `move_stake` (no swap fee). |
| 15 | Nominator dust force-unstake; partial-sell minimum | Medium | Low | Exit fully or keep ≥0.02 TAO-equivalent; partial sells ≥0.002 TAO of output. |
| 16 | `UnstakeAll` fails or skips; locked alpha | Low–Medium | Medium | Per-position `RemoveStakeLimit`; never use unbounded `UnstakeAll`; reconcile; avoid conviction locks on trading coldkeys. |
| 17 | Alpha-fee trap | Low | Medium (all alpha withdrawn) | Keep a TAO fee buffer on the paying delegate. |
| 18 | Basket claim bursts; dissolution basket sale | Medium | Medium | Monitor E/x and the dissolution haircut E/(x+E); avoid subnets with high E/x near the prune line. |
| 19 | Backtest bias (era prices, netuid splicing, survivorship, missing yield, regime splits) | High | High | Era-correct price (§6.5); key on (netuid, NetworkRegisteredAt); include dereg payouts; include share-price yield; split regimes at block+1; flag quirks (§6.7). |
| 20 | RPC throttling or provider outage | High on public endpoints | Medium | Own lite node for live data; keyed archive provider for history; fallback endpoints; backoff on 429/-32004/-32029. |
| 21 | Conviction ownership takeover on subnets older than 1 year | Low | Medium | Monitor conviction and `SubnetOwnerChanged`. |
| 22 | Future features (shorts `ShortsEnabled`, Null consensus adoption, possible 256-subnet cap, superellipse pools) | Unknown | Medium | Monitor the flags and releases; re-run models on activation. |
| 23 | Windows host incompatibility | Certain (native Windows unsupported) | Low | Run in WSL or Linux. |
| 24 | Tax treatment | — | — | No dTAO-specific IRS guidance; staking rewards are income on receipt (Rev. Rul. 2023-14; Paschall v. Commissioner, T.C. Memo. 2026-46). Keep per-subnet lot and per-epoch income records. Seek professional advice. (UNVERIFIED) |

---

## 10. Open questions and items to verify at build time

1. **SDK client shape:** blocking `bt.Subtensor()` versus async-only plus `bt.SyncClient`. Check `sdk/python/bittensor/__init__.py`. Also confirm that every signature in §5.3–5.7 matches 11.3.0 exactly; the examples come from source and docs and were not executed.
2. **Shield inclusion and expiry rate:** measure the share of carriers never included from your own submit logs on test.finney, then tiny mainnet probes (needs explicit approval). Also confirm the nonce-lockout behaviour after a miss.
3. **Total-return size factor:** recompute SMB, WML and REV with era-correct prices plus the share-price yield index, net of costs, at realistic AUM.
4. **Governance parameters:** `EmissionBarRank`, `EmissionGateExponent`, `SubnetMovingAlpha`, `TaoWeight`, `SubnetLimit` (a 256 cap was discussed and deferred), `NetworkRateLimit`, `NetworkImmunityPeriod`, `FeeRate`. Watch for changes; there is no stability guarantee.
5. **Spec 475/476 changes:** "precise emissions", Null consensus, PoW registration. Re-verify the emission and dividend formulas against the live runtime.
6. **Emission-enable criteria:** when and why root enables new subnets (SN36 was disabled for about 49 days), and whether weekly reviews continue.
7. **Taostats:** credit cost per endpoint; tier gating (trades, stake-events, by_block pools, tradingview); undocumented field semantics (`nominator_return_per_kt_alpha`, APY formulas, v3-era `price`/`liquidity_raw`); whether pool history for reused netuids mixes generations (inferred from the schema).
8. **tao.app** pricing, limits and terms.
9. **Basket flows:** event-scan decomposition of escrow E (root dividends vs seed migration vs curated-era buys vs compounding); stability of the ~2%/day claim rate; whether Σ EMA approaches 1 (root sell flag off).
10. **Dereg payout precision:** replay an actual dissolution (e.g. SN116 at block 9,210,610) to calibrate the recovery formula, including basket sales and pending emission.
11. **LimitOrders:** relayer availability, fees and integration.
12. **v3-era gaps:** exact first-v3 block per subnet; user-LP positions outside the two checked blocks; the exact block for the 0.3% → 0.05% fee change.
13. **Pre-spec-411 chain-buy accounting** (2025-12-12 to 2026-05-28) for reconstructing emission share.
14. **`SubnetTaoFlow` future:** whether a runtime resets or migrates the accumulator.
15. **Exact public RPC limits** per endpoint, and whether `state_call` is weighted more heavily.
16. **Balancer weight drift** away from 0.5 over time; the chain's move/swap limit math assumes constant product.
17. **Superellipse pools (PR #3211):** live or not. Would change replay of swap state.
18. **Shorting** (`ShortsEnabled`, PR #2764, call indices 139–142) activation timing.
19. **Validator dividend lag** under classic Yuma for permit holders, and ex-ante validator ranking stability.
20. **2025-05-20 freeze cause**; current governance of the upgrade multisig and the triumvirate after the org move to RaoFoundation.
21. **(UNVERIFIED)** USD beta and volatility table (§7.3), cohort A/B launch statistics, aggregate index returns (§7.7), the MEV Shield activation date, secondary incident details (SN28, ORO, Quasar), and `bittensor-burn-message`.

---

## 11. Sources (deduplicated)

**Chain and SDK source (RaoFoundation/subtensor; opentensor/subtensor redirects)**
- https://github.com/RaoFoundation/subtensor · https://github.com/RaoFoundation/subtensor/releases · https://api.github.com/repos/RaoFoundation/subtensor/releases · https://api.github.com/repos/opentensor/subtensor · https://api.github.com/repos/opentensor/bittensor
- Swap: https://raw.githubusercontent.com/RaoFoundation/subtensor/main/pallets/swap/src/pallet/balancer.rs · …/pallets/swap/src/pallet/mod.rs · …/impls.rs · …/swap_step.rs · …/migrations/migrate_swapv3_to_balancer.rs · …/pallets/swap/runtime-api/src/lib.rs · …/primitives/swap-interface/src/order.rs
- Coinbase: …/pallets/subtensor/src/coinbase/subnet_emissions.rs · run_coinbase.rs · block_step.rs · block_emission.rs · root.rs
- Staking: …/pallets/subtensor/src/staking/stake_utils.rs · add_stake.rs · remove_stake.rs · move_stake.rs · helpers.rs · claim_root.rs · basket_flush.rs · basket_trade.rs · lock.rs · increase_take.rs · decrease_take.rs
- Subnets: …/pallets/subtensor/src/subnets/subnet.rs · dissolution.rs · collateral.rs · mechanism.rs
- Core: …/pallets/subtensor/src/lib.rs · macros/dispatches.rs · macros/errors.rs · macros/events.rs · macros/hooks.rs · guards/check_rate_limits.rs · utils/rate_limiting.rs · utils/misc.rs · epoch/run_epoch.rs · rpc_info/dynamic_info.rs · rpc_info/delegate_info.rs · rpc_info/stake_info.rs · rpc_info/basket_info.rs · migrations/migrate_alpha_v2.rs · migrations/migrate_enable_basket_trading.rs · migrations/migrate_network_lock_reduction_interval.rs · migrations/migrate_network_immunity_period.rs · …/pallets/subtensor/runtime-api/src/lib.rs
- Other pallets: …/pallets/admin-utils/src/lib.rs · …/pallets/shield/src/lib.rs · extension.rs · README.md · …/pallets/limit-orders/README.md · src/lib.rs · …/pallets/transaction-fee/src/lib.rs · …/pallets/proxy/src/lib.rs · …/primitives/share-pool/src/lib.rs
- Runtime: …/runtime/src/lib.rs · check_mortality.rs · staking_fee.rs · transaction_payment_wrapper.rs · proxy_filters/mod.rs · proxy_filters/call_groups.rs · …/runtime/tests/fee_baseline/pins.tsv · …/common/src/proxy.rs · …/common/src/transaction_error.rs · …/ts-tests/suites/zombienet_shield
- SDK v11: …/sdk/python/bittensor/__init__.py · client.py · executor.py · intents/staking.py · intents/base.py · intents/plan.py · intents/proxy.py · intents/batch.py · balance.py · settings.py · result.py · wallet.py · reads/prices.py · reads/subnets.py · reads/delegation.py · namespaces.pyi · _transport/interface.py · …/sdk/python/pyproject.toml
- Repo docs: …/docs/migration.mdx · docs/concepts/{emissions,staking-pools,money,transactions,advanced,client,network}.mdx · docs/guides/{staking,proxies,subnets,root-reborn,basket-trading,conviction,null-consensus,running-a-node}.mdx · docs/internals/{transaction-priority,release-process}.mdx · docs/tx/add-stake-limit.mdx · docs/errors/chain/StakingRateLimitExceeded.mdx
- Release pages: …/website/apps/bittensor-website/src/app/(pages-without-footer)/releases/{page.tsx, v431-upgrade, v440-upgrade}
- Historical tags: https://raw.githubusercontent.com/RaoFoundation/subtensor/v475/… ; v3.4.7-422, v3.4.8-423, v3.2.1–v3.2.18-351, v3.3.9-377, v3.3.10-380, v3.3.12-391, v3.1.6, v2.0.0
- PRs and tags: https://github.com/RaoFoundation/subtensor/pull/2066 · /pull/2649 · /pull/2781 · /pull/2800 · /pull/3192 · /pull/3199 · /pull/3203 · /releases/tag/v3.2.4 · v3.2.5 · v3.4.2-415 · v437 · v441 · v464 · v473 · commits on subnet_emissions.rs / stake_utils.rs (GitHub API)
- Legacy SDK (archived): https://github.com/opentensor/bittensor → RaoFoundation/bittensor; https://raw.githubusercontent.com/RaoFoundation/bittensor/master/{bittensor/core/subtensor.py, core/settings.py, core/extrinsics/staking.py, unstaking.py, mev_shield.py, pallets/subtensor_module.py, chain_data/dynamic_info.py, CHANGELOG.md, README.md}; https://github.com/RaoFoundation/bittensor/releases/tag/v10.3.2
- Node proposer: https://raw.githubusercontent.com/RaoFoundation/polkadot-sdk/cacb4310f20c7cac83eb3ccd8ed5a5ad4212608a/substrate/client/basic-authorship/src/basic_authorship.rs · …/client/shield/src/keystore.rs · key_rotation.rs

**Official docs and packages**
- https://www.bittensor.com/docs (docs.learnbittensor.org redirects here) · https://www.bittensor.com/docs/migration · /docs/concepts/staking-pools · /docs/concepts/emissions · /docs/concepts/money · /docs/guides/subnets · /docs/guides/staking · /docs/guides/root-reborn · /docs/query/subnet-emission-enabled · /docs/hyperparameters · /docs/tx/set-take · https://www.bittensor.com/llms.mdx/docs/… (wallets, proxies, advanced, transactions, chain-consensus, running-a-node, network, conviction, beta-tokens, query/subnet-tao-flows) · https://bittensor.com/llms.txt · https://www.bittensor.com/code/search.json
- https://www.bittensor.com/releases · /releases/v436-upgrade · /v440-upgrade · /v441-upgrade · /v450-upgrade
- https://bittensor.com/catalog/reads.json · https://bittensor.com/catalog/intents.json
- https://guides.learnbittensor.org/subnets/subnet-deregistration · https://guides.learnbittensor.org/sdk/mev-protection · https://docs.learnbittensor.org/concepts/mev-shield
- https://pypi.org/project/bittensor/ · https://pypi.org/pypi/bittensor/json · https://pypi.org/project/bittensor/11.3.0/ · https://pypi.org/integrity/bittensor/11.3.0/bittensor-11.3.0-py3-none-any.whl/provenance · https://pypi.org/pypi/bittensor-cli/json · https://pypi.org/pypi/bittensor-wallet/json
- https://x.com/opentensor/status/2003918709916946769 · https://blog.bittensor.com/bittnesor-community-update-july-3-2024-45661b1d542d · https://blog.bittensor.com/bittensor-community-update-july-4-2024-cd0f51ceee58

**Chain endpoints (live reads 2026-10-08)**
- wss://entrypoint-finney.opentensor.ai:443 · https://archive.chain.opentensor.ai · wss://lite.chain.opentensor.ai:443 · wss://lite.sub.latent.to:443 · wss://archive.sub.latent.to:443 · https://bittensor-finney.api.onfinality.io/public

**Data providers**
- https://api.taostats.io/openapi.json · https://api.taostats.io/api/openapi.json · https://taostats.io/docs/api-reference/quickstart · https://taostats.io/docs/api-reference/subnet/get-pool-history · https://taostats.io/docs/new · https://taostats.io/docs/new/subnets/get-subnets-pools-history.md · https://taostats.io/docs/new/rpc/post-rpc-http.md · https://taostats.io/docs/api-reference/subnet/get-tao-flow · https://taostats.io/docs/api-reference/subnet/get-subnet-pruning-history.md · https://taostats.io/docs/api-reference/dev-activity · https://taostats.io/docs/understanding-taostats/taostats-pro/your-taostats-account.md · https://taostats.io/docs/understanding-taostats/extrinsics/list-of-all-extrinsics · https://taostats.io/docs/concepts/subnets/subnet-deregistration · https://taostats.io/docs/concepts/subnets/subnet-registration · https://taostats.io/docs/concepts/legacy-deprecated/tao-flow · https://taostats.io/pro/api-keys · https://taostats.io/terms · https://taostats.io/subnets
- https://docs2.taostats.io/concepts/protocol-changes/emission-gate/ · …/price-based-emission-shares/ · …/shorting/ · https://docs2.taostats.io/concepts/slippage/ · …/root-validator-baskets/ · …/calculating-nominator-returns/ · https://docs2.taostats.io/api/validator/get-validator-yield-history/ · …/get-hotkey-alpha-shares-history/ · …/get-validator-dividends-history/ · …/get-validator-basket-history/ · https://docs2.taostats.io/api/subnet/get-pool-history/ · https://docs2.taostats.io/api/dtao/get-liquidity-position-event/ · …/get-liquidity-distribution/ · https://docs2.taostats.io/api/quickstart/
- https://api.tao.app/openapi.json · https://api.tao.app/docs · https://api.tao.app/api/v1/subnets/ohlc?netuid=19&period=24h
- https://api.taomarketcap.com/public/v1/ · https://api.taomarketcap.com/public/v1/subnets/
- https://api.coingecko.com/api/v3/coins/categories/list · https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&category=bittensor-subnets · https://www.coingecko.com/en/categories/bittensor-subnets · https://coinmarketcap.com/academy/article/how-to-build-a-tao-subnet-monitor-with-coinmarketcap-api
- https://onfinality.io/en/networks/bittensor-finney · https://documentation.onfinality.io/support/public-rate-limits · https://documentation.onfinality.io/support/response-units · https://onfinality.io/en/pricing · https://onfinality.io/en/terms · https://onfinality.io/en/learn/bittensor-rpc-rate-limits
- https://github.com/unitone-labs/bittensor-indexer · https://crates.io/api/v1/crates/flamewire-bittensor-indexer · https://github.com/sagarregmi2056/subnet-data-indexer-bittensor · https://github.com/RyanMercier/OpenTaoAPI

**Academic and research**
- https://arxiv.org/abs/2603.29751 · https://arxiv.org/html/2603.29751v1

**Secondary (labelled where used)**
- https://www.falconx.io/newsroom/state-of-bittensor-subnet-adoption-trends-network-mechanics-and-covenants-departure
- https://www.tao.media/covenant-ais-bittensor-exit-what-happened-how-bittensor-responded-and-whats-next-for-the-network/ · https://www.tao.media/the-conviction-upgrade-bittensor-just-made-subnet-owner-exits-a-public-event/ · https://www.tao.media/bittensors-hotfix-ends-subnet-owners-free-alpha-at-registration/ · https://www.tao.media/bittensor-ceases-emissions-for-dozens-of-inactive-subnets-in-cleanup-push/ · https://www.tao.media/mark-creaser-says-bittensors-rule-changes-are-making-dtao-harder-to-underwrite/ · https://www.tao.media/what-is-tao-august-2026-guide/
- https://www.cryptotimes.io/2026/04/10/covenant-ais-bittensor-exit-triggers-23-tao-price-crash/ · https://www.coindesk.com/tech/2026/03/25/bittensor-ecosystem-tokens-value-hit-usd1-5-billion-as-jensen-huang-endorsement-supports-tao-rally · https://ourcryptotalk.com/news/bittensor-subnet-emissions-halted-57-subnets · https://hackernoon.com/bittensor-cleans-house-inactive-subnets-to-no-longer-get-weekly-emissions-says-const · https://www.kucoin.com/news/insight/TAO/69e01d21ece611000748cd00 · https://www.kucoin.com/news/articles/bittensor-tao-launches-mev-shield-protecting-users-from-front-running-and-sandwich-attacks · https://simplytao.ai/blog/bittensor-major-emissions-update · https://www.bitget.com/news/detail/12560605017310 · https://en.coinotag.com/breakingnews/bittensor-tao-completes-its-first-halving-as-daily-emission-drops-to-3600-tao-and-block-reward-to-0-5-tao · https://crypto-economy.com/es/?p=141440 · https://phemex.com/news/article/north-korean-hackers-steal-630000-in-crypto-from-ai-developer-oro-94141 · https://blog.kraken.com/product/asset-listings/sn8-is-available-for-trading · https://www.theblock.co/post/303235/bittensor-halts-network-after-reported-security-attack-on-wallets-zachxbt · https://members.delphidigital.io/feed/the-bittensor-halt-centralization-saves-the-day-2
- https://subnetedge.substack.com/p/reality-check · https://subnetedge.substack.com/p/mechanism-design · https://subnetedge.substack.com/p/maestro-strategy-note-012926 · https://subnetedge.substack.com/p/bittensor-protective-inertia · https://www.abittensorjourney.com/p/navigating-bittensor-july-2026 · https://www.abittensorjourney.com/p/navigating-bittensor-august-2026 · https://www.abittensorjourney.com/p/bittensor-2025-end-of-year-report · https://www.abittensorjourney.com/p/bittensor-101-v440-the-emission-gate · https://tzedonn.substack.com/p/26-why-dtao-is-broken · https://taotimes.beehiiv.com/p/tao-times-35-scalper-s-paradise · https://macrocosmosai.substack.com/p/from-tao-price-to-flow-emissions · https://x.com/taostats/status/1999520528285979035
- Security incidents: https://manifold.inc/releases/bittensor-pypi-hack · https://about.gitlab.com/blog/gitlab-uncovers-bittensor-theft-campaign-via-pypi · https://safedep.io/ti/packages/pypi/bitensor
- Wallets: https://docs.talisman.xyz/talisman/bittensor-features/stake-tao-trade-dtao/mev-shield · https://docs.talisman.xyz/talisman/bittensor-features/tao-dtao-staking/mev-shield
- Tools: https://github.com/0xRozier/Bittensor-trading · https://github.com/buckZz7/dtao-trader · https://github.com/tududes/btt-subnet-dca · https://gist.github.com/josephjacks/32a4b1db0c191dff26687b6b5da1f984 · https://flowsniper.ai/ · https://stakao.com/ · https://subnetradar.com/radar/momentum · https://chainbuying.netlify.app/
- Tax: https://kpmg.com/us/en/home/insights/2023/07/tnf-rev-rul-2023-14-cryptocurrency-rewards-included-in-income-when-taxpayer-gains-dominion-and-control-over-rewards.html · https://crokefairchild.com/2026/06/the-tax-court-weighs-in-staking-rewards-are-taxable-on-receipt-in-paschall-v-commissioner/ · https://taostats.io/docs/understanding-taostats/taostats-pro/bittensor-tax-reporting · https://feedback.koinly.io/integrations/p/bittensor-tao