"""Cross-sectional signal scan over all TAO subnets (long-only, total return in TAO, net of costs).

PRE-REGISTERED RULES (fixed 2026-10-12 before any result was seen):
- Universe per day: started, emission-enabled subnets with pool >= 500 TAO.
- Return: total return in TAO = (spot x I)_t+h / (spot x I)_t, where I is the sticky tracked hotkey's share-price index.
  It falls back to price-only when I is missing or the hotkey changed. A subnet deregistered inside the horizon returns
  RECOVERY - 1 (RECOVERY = 0.5, brief section 4.5: 0.35-0.65 x spot).
- Signal book: equal-weight top quintile (at least 3 names), formed daily, held h days, one full round trip charged
  per formation (round-trip cost at the panel's study size, 5 TAO). Daily return series = (fwd_h - cost) / h, t-stat by
  Newey-West with lag h.
- Trials: every signal x {+, -} direction x h in {1, 3, 7} days.
- TRAIN = days before 2026-09-01, TEST = from 2026-09-01.
- PROMOTE (train): mean net/day > 0, beats the equal-weight universe (also net of cost) on mean, and NW t >= 2.0.
- SURVIVE (test, evaluated once): mean net/day > 0, beats EW, and NW t >= 1.5.
- Report deflated Sharpe over the full window against the total trial count.
"""
from __future__ import annotations

import math
import pickle
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

import numpy as np

sys.path.insert(0, "src")
from taotrader.backtest.stats import deflated_sharpe, newey_west_t, spearman  # noqa: E402

RECOVERY = 0.5
MIN_POOL = 500.0
SPLIT_DAY = datetime(2026, 9, 1, tzinfo=timezone.utc).date()
HORIZONS = (1, 3, 7)


def day_of(ts_ms: int):  # type: ignore[no-untyped-def]
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()


def load(path: str = "data/research/panel.pkl"):  # type: ignore[no-untyped-def]
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    # first row per (key, UTC day)
    daily: dict = {}
    for r in d["rows"]:
        k = (r.key, day_of(r.ts_ms))
        if k not in daily or r.block < daily[k].block:
            daily[k] = r
    days = sorted({dd for (_, dd) in daily})
    by_day: dict = defaultdict(dict)
    for (key, dd), r in daily.items():
        by_day[dd][key] = r
    dereg = {k: b for b, k in d["dereg"]}
    return days, by_day, dereg


def value(r) -> float | None:  # type: ignore[no-untyped-def]
    return r.spot * r.index if (r.index is not None and r.spot > 0) else None


def fwd_return(by_day, days, i: int, key, h: int, dereg) -> float | None:  # type: ignore[no-untyped-def]
    if i + h >= len(days):
        return None
    r0 = by_day[days[i]][key]
    r1 = by_day[days[i + h]].get(key)
    if r1 is None:
        b = dereg.get(key)
        return RECOVERY - 1.0 if b is not None and b > r0.block else None
    if r0.hotkey == r1.hotkey and value(r0) and value(r1):
        return value(r1) / value(r0) - 1.0
    return r1.spot / r0.spot - 1.0 if r0.spot > 0 else None


def past(by_day, days, i: int, key, h: int, attr: str = "spot") -> float | None:  # type: ignore[no-untyped-def]
    if i - h < 0:
        return None
    r0 = by_day[days[i - h]].get(key)
    r1 = by_day[days[i]][key]
    if r0 is None:
        return None
    if attr == "tr":
        if r0.hotkey == r1.hotkey and value(r0) and value(r1):
            return math.log(value(r1) / value(r0))
        return None
    return math.log(r1.spot / r0.spot) if r0.spot > 0 and r1.spot > 0 else None


def vol(by_day, days, i: int, key, n: int = 7) -> float | None:  # type: ignore[no-untyped-def]
    xs = [past(by_day, days, j, key, 1) for j in range(i - n + 1, i + 1)]
    xs = [x for x in xs if x is not None]
    return float(np.std(xs)) if len(xs) >= 5 else None


Signal = Callable[..., float | None]
SIGNALS: dict[str, Signal] = {
    "mom_1d": lambda bd, ds, i, k, r: past(bd, ds, i, k, 1),
    "mom_3d": lambda bd, ds, i, k, r: past(bd, ds, i, k, 3),
    "mom_7d": lambda bd, ds, i, k, r: past(bd, ds, i, k, 7),
    "mom_14d": lambda bd, ds, i, k, r: past(bd, ds, i, k, 14),
    "tr_mom_7d": lambda bd, ds, i, k, r: past(bd, ds, i, k, 7, "tr"),
    "vol_7d": lambda bd, ds, i, k, r: vol(bd, ds, i, k),
    "yield_net": lambda bd, ds, i, k, r: r.yield_net_day,
    "yield_cf": lambda bd, ds, i, k, r: r.yield_cf_day,
    "small_size": lambda bd, ds, i, k, r: -math.log(r.pool_tao) if r.pool_tao > 0 else None,
    "low_sell_push": lambda bd, ds, i, k, r: -r.sell_push_day,
    "chain_buy_push": lambda bd, ds, i, k, r: r.cb_push_day,
    "struct": lambda bd, ds, i, k, r: r.cb_push_day - r.sell_push_day,
    "carry_proxy": lambda bd, ds, i, k, r: (r.yield_net_day if r.yield_net_day is not None else r.yield_cf_day)
    + r.cb_push_day - r.sell_push_day,
    "flow_1d": lambda bd, ds, i, k, r: r.flow_1d,
    "flow_z_1d": lambda bd, ds, i, k, r: r.flow_z_1d,
    "emission_per_pool": lambda bd, ds, i, k, r: r.emis_day / r.pool_tao if r.pool_tao > 0 else None,
    "prune_distance": lambda bd, ds, i, k, r: float(r.prune_rank) if r.prune_rank is not None else 999.0,
    "age_days": lambda bd, ds, i, k, r: (r.block - k.reg_at) / 7200.0,
}


def universe(rows: dict) -> list:  # type: ignore[type-arg]
    return [k for k, r in rows.items() if r.started and r.enabled and r.pool_tao >= MIN_POOL]


def run() -> None:
    days, by_day, dereg = load()
    print(f"panel: {len(days)} days {days[0]} .. {days[-1]}; split at {SPLIT_DAY}")
    # precompute signals, forward returns and costs per (day, key)
    sig_vals: dict = defaultdict(dict)      # (name) -> {(i, key): value}
    fwd: dict = {}                           # (h, i, key) -> ret
    cost: dict = {}
    for i, dd in enumerate(days):
        rows = by_day[dd]
        for k in universe(rows):
            r = rows[k]
            cost[(i, k)] = (r.rt_cost_ppm or 20_000) / 1e6
            for h in HORIZONS:
                f = fwd_return(by_day, days, i, k, h, dereg)
                if f is not None:
                    fwd[(h, i, k)] = f
            for name, fn in SIGNALS.items():
                try:
                    v = fn(by_day, days, i, k, r)
                except (ValueError, ZeroDivisionError):
                    v = None
                if v is not None and math.isfinite(v):
                    sig_vals[name][(i, k)] = v

    n_trials = len(SIGNALS) * 2 * len(HORIZONS)
    results = []
    for name in SIGNALS:
        for sign in (+1, -1):
            for h in HORIZONS:
                series = {"train": [], "test": []}
                ew = {"train": [], "test": []}
                ics = {"train": [], "test": []}
                for i, dd in enumerate(days):
                    part = "train" if dd < SPLIT_DAY else "test"
                    ks = [k for k in universe(by_day[dd]) if (i, k) in sig_vals[name] and (h, i, k) in fwd]
                    if len(ks) < 15:
                        continue
                    sv = [sign * sig_vals[name][(i, k)] for k in ks]
                    fr = [fwd[(h, i, k)] - cost[(i, k)] for k in ks]
                    order = np.argsort(sv)[::-1]
                    top = order[: max(3, len(ks) // 5)]
                    series[part].append(float(np.mean([fr[j] for j in top])) / h)
                    ew[part].append(float(np.mean(fr)) / h)
                    ic = spearman(sv, [fwd[(h, i, k)] for k in ks])
                    if ic is not None:
                        ics[part].append(ic)
                row = {"signal": f"{'+' if sign > 0 else '-'}{name}", "h": h}
                for part in ("train", "test"):
                    s = series[part]
                    row[f"{part}_n"] = len(s)
                    row[f"{part}_mean"] = float(np.mean(s)) if s else float("nan")
                    row[f"{part}_ew"] = float(np.mean(ew[part])) if ew[part] else float("nan")
                    row[f"{part}_t"] = newey_west_t(s, lag=h) if len(s) > h + 5 else None
                    row[f"{part}_ic"] = float(np.mean(ics[part])) if ics[part] else float("nan")
                full = series["train"] + series["test"]
                row["dsr"] = deflated_sharpe(full, n_trials) if len(full) > 20 else None
                row["promoted"] = (row["train_mean"] > 0 and row["train_mean"] > row["train_ew"]
                                   and (row["train_t"] or 0) >= 2.0)
                row["survived"] = row["promoted"] and (row["test_mean"] > 0 and row["test_mean"] > row["test_ew"]
                                                        and (row["test_t"] or 0) >= 1.5)
                results.append(row)

    def fmt(x, pct=True):  # type: ignore[no-untyped-def]
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return "   n/a"
        return f"{100 * x:+.3f}" if pct else f"{x:+.2f}"

    print(f"trials: {n_trials}\n")
    print("EW universe (net of one round trip per h-day hold), %/day:")
    for h in HORIZONS:
        r0 = next(r for r in results if r["h"] == h)
        print(f"  h={h}: train {fmt(r0['train_ew'])}  test {fmt(r0['test_ew'])}")
    hdr = f"{'signal':22s} h  train%/d   t     IC    | test%/d    t     IC    | DSR   promoted survived"
    print("\nTOP 20 BY TRAIN NW t (all trials):\n" + hdr)
    for r in sorted(results, key=lambda r: -(r["train_t"] or -99))[:20]:
        print(f"{r['signal']:22s} {r['h']}  {fmt(r['train_mean'])} {fmt(r['train_t'], False)} {fmt(r['train_ic'], False)} | "
              f"{fmt(r['test_mean'])} {fmt(r['test_t'], False)} {fmt(r['test_ic'], False)} | "
              f"{'n/a' if r['dsr'] is None else format(r['dsr'], '.2f')}  {str(r['promoted']):8s} {r['survived']}")
    prom = [r for r in results if r["promoted"]]
    surv = [r for r in results if r["survived"]]
    print(f"\npromoted in train: {len(prom)} of {n_trials}; survived test: {len(surv)}")
    for r in surv:
        print(f"  SURVIVOR {r['signal']} h={r['h']}: train {fmt(r['train_mean'])}%/d (t {fmt(r['train_t'], False)}), "
              f"test {fmt(r['test_mean'])}%/d (t {fmt(r['test_t'], False)}), DSR {r['dsr']}")
    with open("data/research/scan_results.pkl", "wb") as fh:
        pickle.dump(results, fh)


if __name__ == "__main__":
    run()
