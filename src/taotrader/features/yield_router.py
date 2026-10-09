"""taotrader/features/yield_router.py - the book-independent YieldRouter candidate panel (WP5; DESIGN.md sections 3.8,
5.8, 8.7). The per-book choice (Q_MAX against own shares, hysteresis, MOVE_STAKE) is WP8 risk/router.py.

Realised yield. A hotkey's share-price index I = TotalHotkeyAlpha / shares (HotkeyIdx.index(): V1 shares where
present, else V2) rises only at epoch drains. `YieldPanel` records ln I per tracked hotkey per epoch, where the epoch
is identified by LastEpochBlock and the value is the LAST FULL-snapshot observation inside that epoch (HEAD snapshots
carry a stale panel, so they are never recorded). The per-epoch return between two recorded epochs of the same
hotkey is d ln I / n, n = the number of drains in between (round(dLastEpochBlock / Tempo), at least 1;
LastEpochBlock advances by Tempo blocks per epoch, e.g. SN70 9,240,222 -> 9,240,582 at Tempo 360).

Score (section 3.8): EWMA with a half-life of 20 epochs over the last K = 40 per-epoch returns, times 7,200 / Tempo:
a fraction of value per day, already NET of the validator take (the take is paid as new shares, so I grows by the
nominators' part only). Candidates are published best first by (eligible, score, lower take, larger
TotalHotkeyAlpha, hotkey); `best` is the first eligible one.

Book-independent filters (all must hold for `eligible`):
- a score exists (at least one recorded per-epoch return);
- membership: dividend recipient (HotkeyIdx.earns, i.e. a key of AlphaDividendsPerSubnet(n, .)) in >= 90% of the
  last 20 recorded epochs of the subnet (the denominator is always 20: unobserved epochs count as non-member, which
  is fail-closed for newly tracked hotkeys) AND in each of the last 2;
- take = Delegates / 65535 <= TAKE_MAX (5%) and ChildkeyTake <= TAKE_MAX;
- no take increase within 216,000 blocks (Delegates diffs between FULL observations at most 7,200 blocks apart;
  a hotkey without a previous observation is allowed, i.e. unknown counts as no increase);
- permit rank by TotalHotkeyAlpha among the CURRENT dividend recipients of the snapshot <= 0.8 * MaxAllowedValidators
  (MaxAllowedValidators unknown -> fail closed). The snapshot holds only TRACKED hotkeys (top 5 earners, every take-0
  earner, held/chosen and owner hotkeys), so the rank is exact for the top 5 and a lower bound beyond them;
- ratio: realised score / closed-form net yield (protocol.yield_model.closed_form_yield_net) in [0.65, 1.35]. After a
  per-subnet consensus-mode change (spec 475 Null consensus; PARAM_CHANGED "SubnetEpochConsensus") the closed form is
  re-validated: ratio_ok is False until 20 epochs have been recorded after the change, and the ratio then uses only
  post-change returns.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..core.config import RiskCfg
from ..core.fixed import to_ppm
from ..core.state import ChainGlobals, HotkeyIdx, SubnetState
from ..core.units import BLOCKS_PER_DAY, FEE_DEN, PPM, Hotkey, Ppm
from ..core.views import RouterCandidate
from ..protocol.yield_model import closed_form_yield_net
from .micro import ewma_recent_first, ln

TAKE_INCREASE_WINDOW_BLOCKS: Final[int] = 216_000     # section 3.8: no take increase in the last 216,000 blocks
TAKE_OBS_MAX_GAP_BLOCKS: Final[int] = 7_200            # a take diff needs a previous observation at most 1 day older
EPOCH_RETENTION_BLOCKS: Final[int] = 216_000           # recorded epochs older than this are dropped


@dataclass(frozen=True, slots=True)
class RouterParams:
    """Book-independent thresholds of section 3.8 (RiskCfg defaults; see from_risk)."""
    take_max_ppm: int = 50_000                 # RiskCfg.take_max_ppm
    permit_rank_frac_ppm: int = 800_000        # RiskCfg.permit_rank_frac_ppm
    member_window_epochs: int = 20             # RiskCfg.k_epochs: membership over the last 20 epochs
    member_min_ppm: int = 900_000              # member in >= 90% of them
    half_life_epochs: int = 20                 # score EWMA half-life
    ewma_k_epochs: int = 40                    # score EWMA truncation K
    ratio_lo_ppm: int = 650_000                # realised / closed form in [0.65, 1.35]
    ratio_hi_ppm: int = 1_350_000
    take_increase_window_blocks: int = TAKE_INCREASE_WINDOW_BLOCKS
    take_obs_max_gap_blocks: int = TAKE_OBS_MAX_GAP_BLOCKS
    revalidate_epochs: int = 20                # epochs after a consensus-mode change before ratio_ok can pass

    @staticmethod
    def from_risk(cfg: RiskCfg) -> RouterParams:
        return RouterParams(take_max_ppm=int(cfg.take_max_ppm), permit_rank_frac_ppm=int(cfg.permit_rank_frac_ppm),
                            member_window_epochs=cfg.k_epochs)

    @property
    def epochs_kept(self) -> int:
        return max(self.ewma_k_epochs + 1, self.member_window_epochs, self.revalidate_epochs + 1)


def take_ok(take_u16: int, take_max_ppm: int) -> bool:
    """take_u16 / 65535 <= take_max_ppm / 1e6, exactly."""
    return take_u16 * PPM <= take_max_ppm * FEE_DEN


# ------------------------------------------------------------------------------------------------- take history
class TakeBook:
    """Delegates take per hotkey (a global value) observed on FULL snapshots, and the blocks of observed increases."""

    __slots__ = ("_increases", "_last", "_max_gap", "_window")

    def __init__(self, window: int = TAKE_INCREASE_WINDOW_BLOCKS, max_gap: int = TAKE_OBS_MAX_GAP_BLOCKS) -> None:
        self._window = window
        self._max_gap = max_gap
        self._last: dict[str, tuple[int, int]] = {}          # hotkey -> (block, take_u16)
        self._increases: dict[str, list[int]] = {}           # hotkey -> blocks of increases, ascending

    def observe(self, hotkey: str, take_u16: int, block: int) -> None:
        prev = self._last.get(hotkey)
        if prev is not None and prev[0] < block <= prev[0] + self._max_gap and take_u16 > prev[1]:
            self._increases.setdefault(hotkey, []).append(block)
        if prev is None or block >= prev[0]:
            self._last[hotkey] = (block, take_u16)

    def evict(self, now: int) -> None:
        for hk in sorted(self._last):
            if self._last[hk][0] < now - self._max_gap:
                del self._last[hk]
        for hk in sorted(self._increases):
            kept = [b for b in self._increases[hk] if b > now - self._window]
            if kept:
                self._increases[hk] = kept
            else:
                del self._increases[hk]

    def increased_since(self, hotkey: str, after: int) -> bool:
        return any(b > after for b in self._increases.get(hotkey, ()))

    def state(self) -> tuple[tuple[tuple[str, int, int], ...], tuple[tuple[str, tuple[int, ...]], ...]]:
        return (tuple((hk, b, t) for hk, (b, t) in sorted(self._last.items())),
                tuple((hk, tuple(bs)) for hk, bs in sorted(self._increases.items())))


# ------------------------------------------------------------------------------------------------- epoch panel
class YieldPanel:
    """Per generation: ln I and dividend membership of every tracked hotkey at each recorded epoch."""

    __slots__ = ("_consensus", "_epochs", "_keep", "_ln_cache", "_obs", "_revalidate_from")

    def __init__(self, epochs_kept: int) -> None:
        self._keep = max(epochs_kept, 2)
        self._epochs: list[int] = []                                # LastEpochBlock values, ascending
        self._obs: dict[str, dict[int, tuple[float, bool]]] = {}    # hotkey -> epoch -> (ln I, earns)
        self._consensus: int | None = None
        self._revalidate_from: int | None = None                    # epoch id at which the consensus mode changed
        self._ln_cache: dict[str, tuple[int, Decimal, float | None]] = {}   # derived only (not state)

    def _ln_index(self, h: HotkeyIdx) -> float | None:
        c = self._ln_cache.get(h.hotkey)
        if c is not None and c[0] == h.total_alpha and c[1] == h.total_shares:
            return c[2]
        v = ln(h.index())
        self._ln_cache[h.hotkey] = (int(h.total_alpha), h.total_shares, v)
        return v

    @property
    def epochs(self) -> tuple[int, ...]:
        return tuple(self._epochs)

    def observe(self, s: SubnetState, block: int) -> None:
        """Record a FULL snapshot of the generation (callers skip HEAD snapshots: their panel is carried)."""
        if s.consensus_mode is not None:
            if self._consensus is not None and s.consensus_mode != self._consensus:
                self._revalidate_from = int(s.last_epoch_block)
            self._consensus = s.consensus_mode
        e = int(s.last_epoch_block)
        if not self._epochs or e > self._epochs[-1]:
            self._epochs.append(e)
        elif e < self._epochs[-1]:
            return                                                  # LastEpochBlock never decreases in a generation
        for h in s.hotkeys:
            lnv = self._ln_index(h)
            if lnv is None:
                continue
            self._obs.setdefault(h.hotkey, {})[e] = (lnv, h.earns)
        self._evict(block)

    def _evict(self, block: int) -> None:
        drop = max(len(self._epochs) - self._keep, 0)
        while drop < len(self._epochs) - 1 and self._epochs[drop] < block - EPOCH_RETENTION_BLOCKS:
            drop += 1
        if drop:
            gone = self._epochs[:drop]
            del self._epochs[:drop]
            for hk in sorted(self._obs):
                per = self._obs[hk]
                for e in gone:
                    per.pop(e, None)
                if not per:
                    del self._obs[hk]
                    self._ln_cache.pop(hk, None)
        if self._revalidate_from is not None and self._epochs and self._revalidate_from < self._epochs[0]:
            self._revalidate_from = None

    def returns(self, hotkey: str, tempo: int, k: int, after_epoch: int | None = None) -> list[float]:
        """Per-epoch returns of `hotkey`, most recent first, at most k; with after_epoch only those ending after it."""
        per = self._obs.get(hotkey)
        if not per or tempo <= 0:
            return []
        period = tempo                      # LastEpochBlock steps by Tempo (SN70 fixture: 9,240,222 -> 9,240,582)
        seen = [e for e in self._epochs if e in per]
        out: list[float] = []
        for i in range(len(seen) - 1, 0, -1):
            e1, e0 = seen[i], seen[i - 1]
            if after_epoch is not None and e0 < after_epoch:
                break
            n = max(1, (e1 - e0 + period // 2) // period)
            out.append((per[e1][0] - per[e0][0]) / n)
            if len(out) >= k:
                break
        return out

    def membership(self, hotkey: str, window: int) -> tuple[int, bool]:
        """(member epochs among the last `window` recorded epochs, member in each of the last 2)."""
        per = self._obs.get(hotkey, {})
        last = self._epochs[-window:] if window > 0 else []
        count = sum(1 for e in last if e in per and per[e][1])
        tail = self._epochs[-2:]
        last2 = len(tail) == 2 and all(e in per and per[e][1] for e in tail)
        return count, last2

    def revalidating(self, revalidate_epochs: int) -> tuple[bool, int | None]:
        """(ratio_ok blocked, epoch id after which ratio returns count) for a consensus-mode change."""
        r = self._revalidate_from
        if r is None:
            return False, None
        after = sum(1 for e in self._epochs if e > r)
        return after < revalidate_epochs, r

    def state(self) -> tuple[object, ...]:
        obs = tuple((hk, tuple((e, v[0], v[1]) for e, v in sorted(per.items()))) for hk, per in sorted(self._obs.items()))
        return (tuple(self._epochs), obs, self._consensus, self._revalidate_from)


# ------------------------------------------------------------------------------------------------- candidates
@dataclass(frozen=True, slots=True)
class RouterResult:
    candidates: tuple[RouterCandidate, ...]      # best first
    best: Hotkey | None                          # first eligible candidate
    yield_net_day: float | None                  # realised score of `best` (fraction per day, net of take)
    realised: tuple[tuple[str, float], ...]      # (hotkey, realised score per day) for every scored candidate


def _sort_key(c: RouterCandidate, stake: dict[Hotkey, int]) -> tuple[bool, int, int, int, str]:
    return (not c.eligible, -c.score_ppm_day, c.take_u16, -stake.get(c.hotkey, 0), c.hotkey)


def router_candidates(s: SubnetState, glob: ChainGlobals, panel: YieldPanel, takes: TakeBook, block: int,
                      params: RouterParams) -> RouterResult:
    """The section 3.8 candidate panel of one generation at `block` (tracked hotkeys of the snapshot)."""
    earners: list[HotkeyIdx] = sorted((h for h in s.hotkeys if h.earns), key=lambda h: (-h.total_alpha, h.hotkey))
    permit_rank = {h.hotkey: i + 1 for i, h in enumerate(earners)}
    mav = s.max_allowed_validators
    tempo = s.tempo
    per_day = BLOCKS_PER_DAY / tempo if tempo > 0 else 0.0
    blocked, after = panel.revalidating(params.revalidate_epochs)
    stake = {h.hotkey: int(h.total_alpha) for h in s.hotkeys}
    out: list[RouterCandidate] = []
    realised: list[tuple[str, float]] = []
    for h in s.hotkeys:
        rets = panel.returns(h.hotkey, tempo, params.ewma_k_epochs)
        ew = ewma_recent_first(rets, params.half_life_epochs, params.ewma_k_epochs)
        score = None if ew is None or tempo <= 0 else ew * per_day
        count, last2 = panel.membership(h.hotkey, params.member_window_epochs)
        member_ppm = Ppm(count * PPM // params.member_window_epochs) if params.member_window_epochs > 0 else Ppm(0)
        rank = permit_rank.get(h.hotkey)
        permit_ok = rank is not None and mav is not None and rank * PPM <= params.permit_rank_frac_ppm * mav
        increase = takes.increased_since(h.hotkey, block - params.take_increase_window_blocks)
        ratio_ok = False
        if score is not None and not blocked:
            check: float | None = score
            if after is not None:
                post = ewma_recent_first(panel.returns(h.hotkey, tempo, params.ewma_k_epochs, after_epoch=after),
                                         params.half_life_epochs, params.ewma_k_epochs)
                check = None if post is None else post * per_day
            cf = float(closed_form_yield_net(s, glob, h))
            if check is not None and cf > 0:
                ratio_ok = params.ratio_lo_ppm <= (check / cf) * PPM <= params.ratio_hi_ppm
        eligible = (score is not None and member_ppm >= params.member_min_ppm and last2
                    and take_ok(h.take_u16, params.take_max_ppm) and take_ok(h.childkey_take_u16, params.take_max_ppm)
                    and not increase and permit_ok and ratio_ok)
        if score is not None:
            realised.append((h.hotkey, score))
        out.append(RouterCandidate(
            hotkey=h.hotkey, score_ppm_day=int(to_ppm(score)) if score is not None else 0, take_u16=h.take_u16,
            childkey_take_u16=h.childkey_take_u16, member_frac_ppm=member_ppm, member_last2=last2, permit_rank=rank,
            ratio_ok=ratio_ok, take_increase_recent=increase, eligible=eligible))
    out.sort(key=lambda c: _sort_key(c, stake))
    best = next((c.hotkey for c in out if c.eligible), None)
    score_of = dict(realised)
    return RouterResult(candidates=tuple(out), best=best, yield_net_day=score_of.get(best) if best is not None else None,
                        realised=tuple(sorted(realised)))


def best_of(candidates: Sequence[RouterCandidate]) -> Hotkey | None:
    """First eligible candidate of a best-first panel."""
    return next((c.hotkey for c in candidates if c.eligible), None)
