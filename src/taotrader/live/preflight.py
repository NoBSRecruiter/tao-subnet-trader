"""taotrader/live/preflight.py - live preflight and the V2/V3/V6 risk-exit checks (WP11; DESIGN.md 9.3, 9.5).

Preflight runs at every start and every 7,200 blocks. At start a failure is a GateError (`require_preflight`); at a
periodic run it is a key alarm (FROZEN with its emergency-exit exception; reconcile.LiveReconciler journals it).
Every check fails closed: an exception while checking is a failure, never a pass.

1. ops delegate != the real coldkey; each delegate's free balance <= max_ops_balance_tao (a fee buffer only);
2. Proxy.Proxies(real) holds (delegate, Staking, delay 0) for every configured delegate and NO other proxy type for any
   ops delegate (Any, NonTransfer, Transfer, ... -> refuse);
3. RealPaysFee(real, delegate) is false for every delegate (else inner fees and the alpha-fee trap move to the real);
4. stake_availability(real, n).locked == 0 for every held netuid (a conviction lock makes exits fail, brief risk 16);
5. bittensor == 11.3.0; SafeMode clear; head lag < 2; finality lag <= 5; fee float (sum of delegate free balances)
   >= min_fee_float_tao; real free balance >= RiskCfg.min_free_real_rao (MIN_FREE_REAL). A spec outside accepted_specs
   is not a failure here: it is reported (`spec_accepted`) and the gate decides UNARMED;
6. a tiny plan() of every intent shape has no violations: a buy under the buy Policy, and a partial sell, a full exit
   and a same-subnet move with exact alpha amounts under the sell/move Policy; the call indices and
   ProxyType::Staking match V6;
7. reconciliation of chain vs journal is clean (no orphans, no pending ReconAdjusted).

`RiskExitChecks` evaluates V2 (local AMM vs runtime sim_swap on 3 subnets x 2 sizes x buy/sell, <= 1e-6 relative on
outputs and fees), V3 (local prune target == get_subnet_to_prune) and V6 (metadata indices) for the UNARMED
risk-exit exception, cached per (spec_version, tx_version) and refreshed every 7,200 blocks.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..core.config import LiveCfg, RiskCfg
from ..core.errors import GateError
from ..core.events import HealthObs
from ..core.fixed import DEC, floor_int
from ..core.orders import OrderKind
from ..core.state import ChainSnapshot, SubnetState
from ..core.units import RAO_PER_TAO, AlphaRao, Hotkey, NetUid, Rao, SubnetKey
from ..protocol.amm import SwapError, quote_buy, quote_sell
from ..protocol.prune import prune_target
from .sdk_port import EXPECTED_INDICES, PROXY_TYPE_STAKING, SDK_VERSION, LiveCall, LiveReader, SdkPort, ss58_encode

__all__ = [
    "MAX_FINALITY_LAG_BLOCKS", "MAX_HEAD_LAG_BLOCKS", "PREFLIGHT_EVERY_BLOCKS", "Check", "PreflightReport", "RiskExitChecks",
    "SpecCheckResult", "require_preflight", "run_preflight", "tao_to_rao",
]

log = logging.getLogger("taotrader.live.preflight")

PREFLIGHT_EVERY_BLOCKS: Final[int] = 7_200
MAX_HEAD_LAG_BLOCKS: Final[int] = 1             # head lag < 2 blocks
MAX_FINALITY_LAG_BLOCKS: Final[int] = 5
PROBE_TAO_RAO: Final[int] = 10_000_000          # 0.01 TAO plan() probes (above DefaultMinStake + fee)
PROBE_LIMIT_PPM: Final[int] = 50_000            # probe limits 5% away from spot (plan() only; never sent)
V2_SIZES_RAO: Final[tuple[int, ...]] = (1 * RAO_PER_TAO, 10 * RAO_PER_TAO)
V2_SUBNETS: Final[int] = 3
V2_TOL_PPB: Final[int] = 1_000                  # 1e-6 relative
V_REFRESH_BLOCKS: Final[int] = 7_200


def tao_to_rao(tao: float) -> int:
    """Config TAO (a float in LiveCfg) -> rao, exactly from its decimal repr (floored)."""
    return floor_int(DEC.multiply(Decimal(repr(tao)), Decimal(RAO_PER_TAO)))


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PreflightReport:
    block: int
    checks: tuple[Check, ...]
    spec_accepted: bool                     # False -> the gate starts UNARMED (or refuses without the flag)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(f"{c.name}: {c.detail}" for c in self.checks if not c.ok)


def require_preflight(report: PreflightReport) -> None:
    """Start-time preflight: any failure is a GateError."""
    if not report.ok:
        raise GateError("preflight failed: " + "; ".join(report.failures))


async def _guard(name: str, fn: Callable[[], Awaitable[Check | list[Check]]]) -> list[Check]:
    try:
        out = await fn()
    except Exception as e:                       # fail closed: a check that cannot run is a failure
        return [Check(name, False, f"could not check ({type(e).__name__}: {e})"[:300])]
    return out if isinstance(out, list) else [out]


def _probe_subnet(snap: ChainSnapshot, live: LiveCfg, held: Sequence[tuple[SubnetKey, Hotkey]]) -> SubnetState | None:
    if held:
        s = snap.get(sorted(held)[0][0])
        if s is not None:
            return s
    tradable = [s for s in snap.subnets if s.subtoken_enabled and s.pool.px_tao > 0 and s.pool.px_alpha > 0]
    allowed = [s for s in tradable if int(s.key.netuid) in live.allowed_netuids]
    pool = allowed or tradable
    return max(pool, key=lambda s: (int(s.pool.tao), -int(s.key.netuid))) if pool else None


def _probe_hotkeys(s: SubnetState, held: Sequence[tuple[SubnetKey, Hotkey]]) -> list[Hotkey]:
    out: list[Hotkey] = [h for k, h in sorted(held) if k == s.key]
    if s.owner_hotkey is not None and s.owner_hotkey not in out:
        out.append(s.owner_hotkey)
    out += [h.hotkey for h in s.hotkeys if h.hotkey not in out]
    return out


async def run_preflight(sdk: SdkPort, *, live: LiveCfg, risk: RiskCfg, snap: ChainSnapshot, health: HealthObs,
                        held: Sequence[tuple[SubnetKey, Hotkey]], recon_clean: bool) -> PreflightReport:
    """Run every section 9.3 preflight check (reads only; nothing is signed or sent)."""
    real = live.real_coldkey_ss58
    checks: list[Check] = []
    addrs: dict[str, str] = {}
    max_ops = tao_to_rao(live.max_ops_balance_tao)

    async def delegates() -> list[Check]:
        out: list[Check] = []
        if not live.delegate_wallets:
            return [Check("delegates", False, "no delegate wallets configured")]
        for d in live.delegate_wallets:
            addrs[d] = sdk.delegate_ss58(d)
            out.append(Check(f"delegate_not_real[{d}]", addrs[d] != real, "the ops delegate is the real coldkey"
                             if addrs[d] == real else ""))
        for d in live.delegate_wallets:
            free = await sdk.free_balance(addrs[d])
            out.append(Check(f"delegate_balance[{d}]", free <= max_ops,
                             f"free {free} rao > max_ops_balance_tao {live.max_ops_balance_tao}" if free > max_ops else ""))
        return out

    checks += await _guard("delegates", delegates)
    ops = set(addrs.values())

    async def proxy_set() -> Check:
        rows = await sdk.proxies(real)
        bad = sorted(f"{d}:{t}:{delay}" for d, t, delay in rows if d in ops and (t != PROXY_TYPE_STAKING or delay != 0))
        missing = sorted(a for a in ops if (a, PROXY_TYPE_STAKING, 0) not in rows)
        detail = "; ".join(x for x in (f"forbidden proxy entries for ops delegates {bad}" if bad else "",
                                       f"missing (delegate, Staking, 0) for {missing}" if missing else "") if x)
        return Check("proxy_set", not bad and not missing and bool(ops), detail or ("" if ops else "no delegates"))

    async def fee_payer() -> list[Check]:
        out: list[Check] = []
        for d, a in sorted(addrs.items()):
            on = await sdk.real_pays_fee(real, a)
            out.append(Check(f"real_pays_fee[{d}]", not on, "RealPaysFee is on: inner fees would hit the real coldkey"
                             if on else ""))
        return out

    async def locks() -> list[Check]:
        out: list[Check] = []
        for n in sorted({int(k.netuid) for k, _ in held}):
            locked = await sdk.locked_alpha(real, n)
            out.append(Check(f"locks[{n}]", locked == 0, f"{locked} alpha rao locked on a held netuid" if locked else ""))
        return out

    async def balances() -> list[Check]:
        fee_float = 0
        for a in sorted(ops):
            fee_float += await sdk.free_balance(a)
        need = tao_to_rao(live.min_fee_float_tao)
        real_free = await sdk.free_balance(real)
        return [Check("fee_float", fee_float >= need, f"delegate balances {fee_float} rao < {need}" if fee_float < need else ""),
                Check("min_free_real", real_free >= risk.min_free_real_rao,
                      f"real free {real_free} rao < MIN_FREE_REAL {risk.min_free_real_rao}"
                      if real_free < risk.min_free_real_rao else "")]

    async def indices() -> Check:
        got = await sdk.metadata_indices()
        diff = sorted(k for k, v in EXPECTED_INDICES.items() if got.get(k) != v)
        return Check("v6_indices", not diff, f"changed: {[(k, got.get(k)) for k in diff]}" if diff else "")

    async def plans() -> list[Check]:
        return await _plan_checks(sdk, live, snap, held, sorted(addrs))

    checks += await _guard("proxy_set", proxy_set)
    checks += await _guard("real_pays_fee", fee_payer)
    checks += await _guard("locks", locks)
    version = sdk.sdk_version()
    checks.append(Check("sdk_version", version == SDK_VERSION, f"bittensor {version!r} != {SDK_VERSION}"
                        if version != SDK_VERSION else ""))
    safe = snap.glob.safe_mode_until is not None and snap.glob.safe_mode_until >= snap.block
    checks.append(Check("safe_mode", not safe, f"SafeMode until {snap.glob.safe_mode_until}" if safe else ""))
    checks.append(Check("head_lag", health.head_lag_blocks <= MAX_HEAD_LAG_BLOCKS,
                        f"head lag {health.head_lag_blocks} >= 2" if health.head_lag_blocks > MAX_HEAD_LAG_BLOCKS else ""))
    checks.append(Check("finality_lag", health.finality_lag_blocks <= MAX_FINALITY_LAG_BLOCKS,
                        f"finality lag {health.finality_lag_blocks} > 5"
                        if health.finality_lag_blocks > MAX_FINALITY_LAG_BLOCKS else ""))
    checks += await _guard("balances", balances)
    checks += await _guard("plan_shapes", plans)
    checks += await _guard("v6_indices", indices)
    checks.append(Check("reconciliation", recon_clean, "" if recon_clean else "chain vs journal is not clean (orphans or "
                        "an un-cleared ReconAdjusted)"))
    report = PreflightReport(block=int(snap.block), checks=tuple(checks),
                             spec_accepted=snap.glob.spec_version in live.accepted_specs)
    if not report.ok:
        log.warning("preflight at %s failed: %s", snap.block, "; ".join(report.failures))
    return report


async def _plan_checks(sdk: SdkPort, live: LiveCfg, snap: ChainSnapshot, held: Sequence[tuple[SubnetKey, Hotkey]],
                       delegates: Sequence[str]) -> list[Check]:
    """plan() of a buy, a partial sell, a full exit and a same-subnet move (exact amounts; nothing is sent)."""
    if not delegates:
        return [Check("plan_shapes", False, "no delegate to plan with")]
    s = _probe_subnet(snap, live, held)
    if s is None:
        return [Check("plan_shapes", False, "no tradable subnet in the snapshot to probe")]
    spot = int(s.pool.spot_rao())
    if spot <= 0:
        return [Check("plan_shapes", False, f"SN{int(s.key.netuid)} has no spot price")]
    hks = _probe_hotkeys(s, held)
    if not hks:
        return [Check("plan_shapes", False, f"SN{int(s.key.netuid)} has no tracked hotkey to probe")]
    n = int(s.key.netuid)
    allowed = tuple(live.allowed_netuids) or None
    sell_allowed = tuple(sorted(set(live.allowed_netuids) | {int(k.netuid) for k, _ in held} | {n}))
    alpha = max(PROBE_TAO_RAO * RAO_PER_TAO // spot, 1)
    hk0 = ss58_encode(hks[0])
    up = -(-spot * (1_000_000 + PROBE_LIMIT_PPM) // 1_000_000)
    down = max(spot * (1_000_000 - PROBE_LIMIT_PPM) // 1_000_000, 1)
    calls: list[tuple[str, LiveCall]] = [
        ("plan_buy", LiveCall(OrderKind.ADD_STAKE_LIMIT, hk0, n, PROBE_TAO_RAO, up, False, None, live.max_order_tao, allowed)),
        ("plan_partial_sell", LiveCall(OrderKind.REMOVE_STAKE_LIMIT, hk0, n, alpha, down, False, None, None, sell_allowed)),
        ("plan_full_exit", LiveCall(OrderKind.REMOVE_STAKE_LIMIT, hk0, n, 2 * alpha, down, False, None, None, sell_allowed)),
    ]
    out: list[Check] = []
    if len(hks) > 1:
        calls.append(("plan_move", LiveCall(OrderKind.MOVE_STAKE, hk0, n, alpha, 0, False, ss58_encode(hks[1]), None,
                                            sell_allowed)))
    else:
        out.append(Check("plan_move", False, f"SN{n} has no second tracked hotkey for the move probe"))
    max_fee = tao_to_rao(live.max_fee_tao)
    for name, call in calls:
        violations, fee = await sdk.plan(call, delegates[0])
        bad = list(violations) + ([f"fee {fee} rao > max_fee_tao"] if fee > max_fee else [])
        out.append(Check(name, not bad, "; ".join(bad)))
    return out


# ------------------------------------------------------------------------------------------------- V2 / V3 / V6
@dataclass(frozen=True, slots=True)
class SpecCheckResult:
    spec: tuple[int, int]
    block: int
    v2: bool
    v3: bool
    v6: bool
    detail: str

    @property
    def ok(self) -> bool:
        return self.v2 and self.v3 and self.v6


def _close(a: int, b: int) -> bool:
    return abs(a - b) <= max(1, abs(b) * V2_TOL_PPB // 1_000_000_000)


class RiskExitChecks:
    """V2 (sim_swap parity), V3 (prune-target parity) and V6 (call indices, ProxyType::Staking) on the current spec:
    the precondition of the UNARMED risk-exit exception. Fails closed (any exception -> that check fails)."""

    def __init__(self, reader: LiveReader, sdk: SdkPort, *, n_subnets: int = V2_SUBNETS) -> None:
        self.reader = reader
        self.sdk = sdk
        self.n_subnets = n_subnets
        self.last: SpecCheckResult | None = None

    async def ok(self, snap: ChainSnapshot) -> bool:
        return (await self.run(snap)).ok

    async def run(self, snap: ChainSnapshot) -> SpecCheckResult:
        spec = (snap.glob.spec_version, snap.glob.tx_version)
        last = self.last
        if last is not None and last.spec == spec and int(snap.block) - last.block < V_REFRESH_BLOCKS:
            return last
        notes: list[str] = []
        v2 = await self._safe(self._v2(snap, notes), "V2", notes)
        v3 = await self._safe(self._v3(snap, notes), "V3", notes)
        v6 = await self._safe(self._v6(notes), "V6", notes)
        self.last = SpecCheckResult(spec, int(snap.block), v2, v3, v6, "; ".join(notes))
        if not self.last.ok:
            log.warning("risk-exit checks failed on spec %s: %s", spec, self.last.detail)
        return self.last

    @staticmethod
    async def _safe(coro: Awaitable[bool], name: str, notes: list[str]) -> bool:
        try:
            return await coro
        except Exception as e:
            notes.append(f"{name} could not run ({type(e).__name__}: {e})"[:200])
            return False

    async def _v2(self, snap: ChainSnapshot, notes: list[str]) -> bool:
        subs = sorted((s for s in snap.subnets if s.subtoken_enabled and s.pool.px_tao > 0 and s.pool.px_alpha > 0),
                      key=lambda s: (-int(s.pool.tao), int(s.key.netuid)))[: self.n_subnets]
        if not subs:
            notes.append("V2: no tradable subnet")
            return False
        ok = True
        compared = 0
        for s in subs:
            n = NetUid(int(s.key.netuid))
            for size in V2_SIZES_RAO:
                try:
                    qb = quote_buy(s.pool, Rao(size))
                    qs = quote_sell(s.pool, AlphaRao(qb.amount_out))
                except SwapError:
                    continue                      # size infeasible on this pool locally; the chain would reject too
                compared += 1
                sb = await self.reader.sim_swap_buy(n, size, snap.block_hash)
                ss = await self.reader.sim_swap_sell(n, qb.amount_out, snap.block_hash)
                if sb.alpha_amount == 0 or ss.tao_amount == 0:
                    notes.append(f"V2: SN{n} all-zero sim_swap")
                    ok = False
                    continue
                pairs = (("buy out", sb.alpha_amount, qb.amount_out), ("buy fee", sb.tao_fee, qb.fee),
                         ("sell out", ss.tao_amount, qs.amount_out), ("sell fee", ss.alpha_fee, qs.fee))
                for what, chain, local in pairs:
                    if not _close(local, chain):
                        notes.append(f"V2: SN{n} {size} {what} local {local} != chain {chain}")
                        ok = False
        if compared == 0:                         # fail closed: parity that was never compared is not parity
            notes.append("V2: no probe size was feasible on any probed subnet; nothing compared")
            return False
        return ok

    async def _v3(self, snap: ChainSnapshot, notes: list[str]) -> bool:
        local = prune_target(snap)
        chain = await self.reader.subnet_to_prune(snap.block_hash)
        same = (None if local is None else int(local.netuid)) == (None if chain is None else int(chain))
        if not same:
            notes.append(f"V3: local prune target {None if local is None else int(local.netuid)} != chain {chain}")
        return same

    async def _v6(self, notes: list[str]) -> bool:
        got = await self.sdk.metadata_indices()
        diff = sorted(k for k, v in EXPECTED_INDICES.items() if got.get(k) != v)
        if diff:
            notes.append(f"V6: changed {[(k, got.get(k)) for k in diff]}")
        return not diff
