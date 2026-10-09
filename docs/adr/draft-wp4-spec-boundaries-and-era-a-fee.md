# ADR draft (WP4): spec boundaries, era-A swap fee, registration recording

**Status:** proposed by WP4, 2026-10-09. The lead decides and applies it to `protocol/regimes.py`. WP4 does not edit that file.
**Context:**
- DESIGN §8.6 regime rows with unknown blocks: taoflow, chainbuy_unrecorded and spec475.
- §8.2 / §13 Q21: the era-A swap fee.
- §13 Q9: OnFinality range-call limits.
- One finding about how `LastRateLimitedBlock(0x02)` is recorded.

All numbers come from read-only JSON-RPC against `https://bittensor-finney.api.onfinality.io/public`, at 3 req/s or less.

**Reproduce:**
- `python -m taotrader.data.refine spec-boundaries --hi 9240388 [--write]`
- `python -m taotrader.data.refine verify-prune-log`
- `python -m taotrader.data.refine range-probe`
- `python -m taotrader.data.events_decoder era-a-fee [--blocks ...]`

## 1. Spec boundaries (`refine.spec_boundaries`, binary search on `state_getRuntimeVersion`)

The block that contains `setCode` already reports the new spec, so `first_logic_block = setcode_block + 1`. The search used 145 probes (290 calls). The network test re-ran 421/432/440/441 and got the same blocks.

| spec | setCode block | first logic block | previous spec | UTC time of the setCode block | §8.6 / brief |
|---|---|---|---|---|---|
| 334 | 6,811,690 | 6,811,691 | 326 | 2025-11-04 22:27:00 | taoflow "334/338" (new) |
| 338 | 6,813,653 | 6,813,654 | 334 | 2025-11-05 05:00:12 | taoflow "334/338" (new) |
| 362 | 7,091,126 | 7,091,127 | 361 | 2025-12-13 18:41:36 | chainbuy_unrecorded start (brief said 2025-12-12; new) |
| 421 | 8,466,530 | 8,466,531 | 419 | 2026-06-23 01:25:24 | **reproduced** (price_ema_rp) |
| 432 | 8,636,190 | 8,636,191 | 424 | 2026-07-16 19:30:12 | **reproduced** (price_ema) |
| 440 | 8,713,793 | 8,713,794 | 439 | 2026-07-27 14:16:12 | **reproduced** (gate_qmass) |
| 441 | 8,765,683 | 8,765,684 | 440 | 2026-08-03 19:18:24 | **reproduced** (gate_rank32) |
| 475 | 9,233,781 | 9,233,782 | 473 | 2026-10-07 21:18:48 | spec475 (new) |

**Proposed `regimes.py` rows** (first block = setCode + 1):
- `taoflow`:
  - first block **6,811,691** if spec 334 is the release that switched to flow shares;
  - otherwise **6,813,654** (338).
  - 334 ran on mainnet for only 1,963 blocks (about 6.5 h).
  - The search cannot tell which of the two carried the taoflow logic. Release notes or the source diff decide it. The fail-closed choice is 6,811,691, because flow logic may already have applied there.
- `chainbuy_unrecorded`: **7,091,127** to 8,283,783.
- `spec475`: **9,233,782**.

`touches_econ` is unchanged by this ADR. That needs the release diffs (§13 Q2).

## 2. Era-A swap fee (§13 Q21; `events_decoder.measure_era_a_fee`)

**Method.**
- System.Events was decoded with each block's own runtime metadata (scalecodec).
- Only "clean" stake events were kept: exactly one StakeAdded or StakeRemoved on a non-root subnet in the block.
- Each event was checked against the constant-product pool rebuilt from the post-block reserves `(SubnetAlphaIn, SubnetTAO)`.
- The coinbase injection runs in `on_initialize`, before extrinsics.
- **Samples:**
  - first pass: 5 blocks;
  - second pass: 1 block per era-A spec, 234 to 277, plus 5,947,000;
  - third pass: 8 blocks around specs 257 to 261;
  - **64 samples in total, 17 specs.**

**Findings:**
1. **The era-A AMM has no proportional fee.** The TAO the pool took on an add matches the event's TAO to within 1.6e-5 relative, and to within 1e-7 for most samples. The gross TAO it paid on a remove matches the event's TAO plus the fee field to within 2e-5.
2. **The fee is a separate staking fee, withheld outside the pool:**

| specs | StakeAdded | StakeRemoved | evidence |
|---|---|---|---|
| 233/234–252 | flat 50,000 rao, deducted before the swap. The event TAO is net: 999,950,000 / 9,950,000 / 49,950,000 against round gross amounts | flat 50,000 rao from the TAO output: implied 50,000–50,139 | no fee field in the event |
| 257–258 | flat 50,000 | **0.00005 TAO per whole alpha unstaked** (fee = 5.000e-5 × alpha rao; e.g. 125,035,427 on 2,500.7 alpha, 595,390 on 11.9 alpha) | fee field |
| 261 | flat 50,000 (one sample showed 25,000) | **max(50,000, 0.005% of the gross TAO out)** (70,900 on 1.418 TAO; 259,260 on 5.185 TAO) | fee field |
| 265–277 | flat 50,000 | 50,000 on every sampled remove. All sampled removes were under 1 TAO, so a max(50,000, 0.005%) rule cannot be excluded | fee field |

**Proposal (lead decides):**
- **(a) Parity.** Set `ERA_A_FEE_RATE = 0`, because Swap.FeeRate did not exist and the pool math is exact without it, and mark it verified. Model the staking fee separately:
  - in `protocol/fees.py` as an era-A flat fee of 50,000 rao per stake operation, on the TAO side;
  - with the 257–258 and 261 unstake variants above.

  This keeps era-A pool states and fills exact.
- **(b) Conservative cost proxy.** Keep 33/65535 for era-A fills. That is 0.05%, which overstates the true cost for any trade of 0.1 TAO or more (0.05% of 0.1 TAO ≈ 50,354 rao, about the flat fee), and every sleeve trades at least 0.5 TAO. Then mark `fee_rate_default_verified(spec < 290) = True` with this ADR as the evidence.

WP4 recommends (a) for parity studies and (b) only if the lead prefers not to add a fee type. Either way, the `Quality.DEFAULT_FILLED` flag for era-A fees can be dropped once this ADR lands.

**For comparison** (spec 475, block 9,240,388): a StakeAdded of 649,900,000 rao carries fee 327,255 = floor(649,900,000 × 33 / 65,535). The era-C proportional fee is confirmed.

## 3. `LastRateLimitedBlock(0x02)` was written at the add block in specs 438–450 (new finding)

`refine.verify_prune_log` checks every prune of the brief §4.4 log at P−1, P and P+30:
- the logged victim is NetworksAdded at P−1 and removed at P;
- the registration is recorded;
- Δreg holds.

**52 of 52 prunes reproduce block, netuid and Δreg.**

Five registrations were recorded late:

| prune P | recorded at | lag | spec at P |
|---|---|---|---|
| 8,693,261 | 8,693,284 | 23 | 438 |
| 8,762,355 | 8,762,380 | 25 | |
| 8,825,550 | 8,825,571 | 21 | |
| 8,884,341 | 8,884,359 | 18 | |
| 8,938,751 | 8,938,771 | 20 | 450 |

For these, the rate-limit key was written when the queued registration completed (the add block, P + 18..25), not at the registration block P. Registrations before them (spec 424 at 8,618,670) and after them (spec 454 at 9,003,827 onward) are recorded at P.

**Consequences:**
- In those specs the registration window (`last_reg_block + NetworkRateLimit`) opened 18–25 blocks later than "P + rate limit". `prune.window_open_block` and `registration_cost` read the stored value, so they stay chain-exact.
- Hazard and Δreg statistics should use P, the removal or registration block. `refine.lifecycle_rows` therefore anchors each registration on its victim's removal block when the recorded block is up to 30 blocks after it. `registration.queued_block` = P, and `blocks_since_prev` = P − previous P, which matches the brief's Δreg.
- REGISTRATION_SEEN (§4.3) fires at the add block in that spec range. No v1 rule depends on the difference.

## 4. OnFinality public range calls (§13 Q9; `refine.probe_range_calls` at 9,200,000)

`state_queryStorage(keys, from, to)` works on the public archive:

| keys | span (blocks) | result | time |
|---|---|---|---|
| 10 | 10 | 11 change sets | 0.19 s |
| 10 | 100 | 101 change sets | 0.91 s |
| 10 | 1,000 | 1,001 change sets | 6.97 s |
| 80 | 1,000 | 1,001 change sets | 9.31 s |

No errors were returned. A 3,600-block FT1b window at 80 keys is therefore about 4 calls, consistent with §6.11's "≈ 400 range calls" for 10 prunes. Per-call latency grows roughly linearly with the span, so keep each call to 1,000 blocks or fewer.

## 5. Collector throughput measured (input for the lead's backfill planning)

The reduced run covered 61 era-C 60-block snapshots, 9,230,400 to 9,234,000:
- it crossed the spec-475 setCode;
- one membership point listed 6,416 dividend recipients;
- the tracked panel after it was 1,755 pairs (about 13.7 per subnet: top-5, every take-0 earner and the owner);
- **result:** 389 calls in 205 s (0.30 snapshots/s, 1.9 req/s), calibration error 0.0 on 927 probe rows.

The panel triples the hotkey keys against the design's "about 6 per subnet", because of take-0 earners (`track_hotkeys` semantics, WP2).

**Era-C backfill estimate** (12.6k snapshots):

| setup | estimate |
|---|---|
| public endpoint, rate-bound at 3 req/s with snapshot_concurrency 3 | about 7–11 h |
| keyed OnFinality | about 2.5 h |

The lead runs it as a background job (commands in the WP4 report).
