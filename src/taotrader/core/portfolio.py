"""taotrader/core/portfolio.py - typed portfolio state + an independent double-entry ledger.

Both are updated from the same journal events by engine.reducer and cross-checked by check_invariants
after EVERY commit. A breach halts entries and alerts; it never crash-loops.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, Context, Decimal
from typing import Final

from .events import CapitalChanged, CarrierFeeSettled, DeregSettled, OrderFailed, YieldAccrued
from .orders import Fill, OrderKind
from .units import Block, Hotkey, NetUid, PositionKey, Rao, StrategyId, SubnetKey

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

    Messages are prefixed "inv<n>:" with n the class above. A ledger position account that holds more than the
    2-rao tolerance without a matching Position is reported under class 2. Iteration is sorted, so the list is
    deterministic.
    """
    out: list[str] = []

    def bal(account: str, unit: str) -> int:
        return ledger_balances.get((account, unit), 0)

    # 1. cash and fee float agree with the ledger and are non-negative
    cash_l, float_l = bal("cash", TAO_UNIT), bal("fee_float", TAO_UNIT)
    if portfolio.cash != cash_l:
        out.append(f"inv1: cash {portfolio.cash} != ledger cash {cash_l}")
    if portfolio.fee_float != float_l:
        out.append(f"inv1: fee_float {portfolio.fee_float} != ledger fee_float {float_l}")
    if portfolio.cash < 0:
        out.append(f"inv1: cash {portfolio.cash} < 0")
    if portfolio.fee_float < 0:
        out.append(f"inv1: fee_float {portfolio.fee_float} < 0")

    # 2. every position's share value matches its ledger alpha within the rounding tolerance
    held: dict[str, PositionKey] = {}
    for p in portfolio.positions:
        acct = pos_account(p.key, p.hotkey)
        held[acct] = p.pkey
        led = bal(acct, alpha_unit(p.key))
        value = position_alpha.get(p.pkey)
        if value is None:
            out.append(f"inv2: no value_of supplied for {acct}")
        elif abs(value - led) > POSITION_TOLERANCE_RAO:
            out.append(f"inv2: {acct} value_of {value} != ledger {led} (tolerance {POSITION_TOLERANCE_RAO})")
    for (acct, unit), amount in sorted(ledger_balances.items()):
        if acct.startswith("pos:") and acct not in held and abs(amount) > POSITION_TOLERANCE_RAO:
            out.append(f"inv2: ledger {acct} holds {amount} {unit} with no Position")

    # 3. sleeve shares sum to the physical position (exact); sleeve cash sums to cash
    sleeve_sum: dict[SubnetKey, Decimal] = {}
    for h in portfolio.sleeves:
        sleeve_sum[h.key] = _EXACT_SUM.add(sleeve_sum.get(h.key, Decimal(0)), h.shares)
    phys: dict[SubnetKey, Decimal] = {}
    for p in portfolio.positions:
        phys[p.key] = _EXACT_SUM.add(phys.get(p.key, Decimal(0)), p.shares)
    for key in sorted(set(sleeve_sum) | set(phys)):
        s, q = sleeve_sum.get(key, Decimal(0)), phys.get(key, Decimal(0))
        if s != q:
            out.append(f"inv3: sleeve shares {s} != position shares {q} on {key.netuid}:{key.reg_at}")
    cash_sum = sum((c for _, c in portfolio.sleeve_cash), 0)
    if cash_sum != portfolio.cash:
        out.append(f"inv3: sleeve cash sums to {cash_sum} != cash {portfolio.cash}")

    # 4. each unit sums to zero across all accounts
    sums: dict[str, int] = {}
    for (_, unit), amount in sorted(ledger_balances.items()):
        sums[unit] = sums.get(unit, 0) + amount
    for unit, total in sorted(sums.items()):
        if total != 0:
            out.append(f"inv4: unit {unit} sums to {total}")

    # 5. no empty or negative positions; at most one position per generation
    seen: dict[SubnetKey, int] = {}
    for p in portfolio.positions:
        if p.shares <= 0:
            out.append(f"inv5: position {p.key.netuid}:{p.key.reg_at}:{p.hotkey} has shares {p.shares} <= 0")
        seen[p.key] = seen.get(p.key, 0) + 1
    for key, n in sorted(seen.items()):
        if n > 1:
            out.append(f"inv5: {n} positions on {key.netuid}:{key.reg_at}")
    return out


# ------------------------------------------------------------------------------------------------- WP0 additions
POSITION_TOLERANCE_RAO: Final[int] = 2          # invariant 2: share-pool rounding between value_of and the ledger
_EXACT_SUM: Final[Context] = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)   # exact Decimal sums


def _txn(txn_id: str, block: Block, postings: list[Posting]) -> LedgerTxn:
    """Drop zero postings, validate the balance per unit, and freeze."""
    txn = LedgerTxn(txn_id, block, tuple(p for p in postings if p.amount != 0))
    txn.validate()
    return txn


def yield_txn(ev: YieldAccrued) -> LedgerTxn:
    """Nominator yield: our position's alpha value rises by delta_alpha (may be -1 rao from share-pool rounding).
    pos += delta, income:yield -= delta, in the generation's alpha unit."""
    au = alpha_unit(ev.key)
    return _txn(f"yield:{ev.book}:{ev.key.netuid}:{ev.key.reg_at}:{ev.block}", ev.block,
                [Posting(pos_account(ev.key, ev.hotkey), au, ev.delta_alpha),
                 Posting("income:yield", au, -ev.delta_alpha)])


def dereg_txn(ev: DeregSettled, pos_alpha: int | None = None) -> LedgerTxn:
    """Dissolution settlement: the DISSOLVING position's alpha is written off to loss:dereg and the payout is
    credited to cash out of loss:dereg. `pos_alpha` (default ev.alpha_value) lets the reducer post the exact
    ledger balance so the position account closes at 0. txn_id == ev.idem() ("dereg:<book>:<netuid>:<reg_at>")."""
    au = alpha_unit(ev.key)
    a = ev.alpha_value if pos_alpha is None else pos_alpha
    return _txn(ev.idem(), ev.block,
                [Posting(pos_account(ev.key, ev.hotkey), au, -a), Posting("loss:dereg", au, a),
                 Posting("loss:dereg", TAO_UNIT, -ev.payout_tao), Posting("cash", TAO_UNIT, ev.payout_tao)])


def fail_txn(ev: OrderFailed) -> LedgerTxn:
    """A failed inner call (or an included-but-undecrypted carrier) still pays its fee from the fee float."""
    return _txn(f"fail:{ev.order_id}:{ev.attempt}", ev.block,
                [Posting("fee_float", TAO_UNIT, -ev.tx_fee), Posting("fees:tx", TAO_UNIT, ev.tx_fee)])


def carrier_fee_txn(ev: CarrierFeeSettled) -> LedgerTxn:
    """Live carrier-fee settlement after a shield miss (valid on an EXPIRED order): fee_float -fee, fees:tx +fee."""
    return _txn(f"carrier:{ev.order_id}:{ev.attempt}", ev.block,
                [Posting("fee_float", TAO_UNIT, -ev.fee_rao), Posting("fees:tx", TAO_UNIT, ev.fee_rao)])


def capital_txn(ev: CapitalChanged) -> LedgerTxn:
    """Capital in/out: cash and fee float move against equity:capital. txn_id == ev.idem()."""
    return _txn(ev.idem(), ev.block,
                [Posting("cash", TAO_UNIT, ev.cash_delta), Posting("fee_float", TAO_UNIT, ev.fee_float_delta),
                 Posting("equity:capital", TAO_UNIT, -(ev.cash_delta + ev.fee_float_delta))])


def apply_txn(balances: dict[tuple[str, str], int], txn: LedgerTxn) -> None:
    """Validate txn and add its postings to `balances` ((account, unit) -> amount) in place."""
    txn.validate()
    for p in txn.postings:
        balances[(p.account, p.unit)] = balances.get((p.account, p.unit), 0) + p.amount


def parse_pos_account(account: str) -> PositionKey | None:
    """Inverse of pos_account; None for any other account name."""
    parts = account.split(":")
    if len(parts) != 4 or parts[0] != "pos" or not parts[1].isdigit() or not parts[2].isdigit():
        return None
    return PositionKey(SubnetKey(NetUid(int(parts[1])), Block(int(parts[2]))), Hotkey(parts[3]))

