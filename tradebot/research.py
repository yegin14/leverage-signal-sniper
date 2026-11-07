"""Compares a fixed, pre-declared set of strategy families on two years of hourly data.

Run: python -m tradebot.research --days 730

Rules that keep this honest:
  - The family and parameter list below is fixed before looking at results.
  - One variant per family is chosen on the first 60% of the data, by mean R.
  - The last 40% is scored once. Scores there are the only ones that count.
  - Comparison with random entries uses mean R per trade, not total dollars, because trade
    counts differ. Random entries use the same symbols, stop sizes and exits.
  - Trades overlap and crypto is correlated, so t-stats here overstate the evidence.
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import aiohttp
import numpy as np

from . import settings
from .costs import PERP_VENUES, VenueCosts, fee_usd, sign
from .risk import Filters, Rejected, size_position
from .settings import RiskLimits
from .strategies import (FUNDING_EXITS, TREND_1H_EXITS, TREND_4H_EXITS, Bars, ExitRule, atr,
                         funding_contrarian, mean_reversion, resample, trend_breakout)
from .venues import BINANCE_FAPI, HEADERS

HOUR_MS = 3_600_000
CACHE = settings.DATA_DIR / "history"
TAKER_SLIPPAGE_BPS = 2.0
MIN_NOTIONAL = {"BTCUSDT": 50.0, "ETHUSDT": 20.0, "LINKUSDT": 20.0}
TRAIN_FRACTION = 0.6


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_ts: float
    exit_ts: float
    r: float
    net: float
    costs: float
    funding: float
    reason: str


Candidate = Tuple[Bars, int, str, float]  # bars, entry bar, direction, ATR at signal


# ------------------------------------------------------------------- data

async def _pages(session, url: str, params: dict, next_cursor, stop_ms: int) -> list:
    rows: list = []
    cursor = params.pop("startTime")
    while cursor < stop_ms:
        async with session.get(url, headers=HEADERS, params={**params, "startTime": cursor},
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            page = await r.json()
        if not page:
            break
        rows.extend(page)
        new_cursor = next_cursor(page[-1])
        if new_cursor <= cursor or len(page) < params["limit"]:
            break
        cursor = new_cursor
        await asyncio.sleep(0.12)
    return rows


async def load_hourly(symbols, days: int) -> Dict[str, Bars]:
    CACHE.mkdir(parents=True, exist_ok=True)
    end = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    start = end - days * 86_400_000
    out: Dict[str, Bars] = {}
    async with aiohttp.ClientSession() as session:
        for symbol in symbols:
            path = CACHE / f"{symbol}_1h_{days}d_{end // 86_400_000}.json"
            if path.exists():
                data = json.loads(path.read_text())
            else:
                klines = await _pages(session, f"{BINANCE_FAPI}/fapi/v1/klines",
                                      {"symbol": symbol, "interval": "1h", "limit": 1500, "startTime": start},
                                      lambda k: int(k[0]) + HOUR_MS, end)
                funding = await _pages(session, f"{BINANCE_FAPI}/fapi/v1/fundingRate",
                                       {"symbol": symbol, "limit": 1000, "startTime": start},
                                       lambda x: int(x["fundingTime"]) + 1, end)
                data = {"k": [[k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in klines],
                        "f": [[int(x["fundingTime"]), float(x["fundingRate"])] for x in funding]}
                path.write_text(json.dumps(data))
                print(f"  {symbol}: {len(data['k'])} bars, {len(data['f'])} funding rows", flush=True)
            k = np.array(data["k"], dtype=np.float64)
            f = np.array(data["f"], dtype=np.float64)
            out[symbol] = Bars(symbol, HOUR_MS, k[:, 0], k[:, 1], k[:, 2], k[:, 3], k[:, 4], k[:, 5], f[:, 0], f[:, 1])
    return out


@dataclass(frozen=True)
class Variant:
    family: str
    name: str
    hours: int
    make: Callable
    exits: ExitRule


VARIANTS = [
    Variant("trend_4h", "4s kırılım 20", 4, trend_breakout(20), TREND_4H_EXITS),
    Variant("trend_4h", "4s kırılım 55", 4, trend_breakout(55), TREND_4H_EXITS),
    Variant("trend_1h", "1s kırılım 24", 1, trend_breakout(24), TREND_1H_EXITS),
    Variant("trend_1h", "1s kırılım 72", 1, trend_breakout(72), TREND_1H_EXITS),
    Variant("mean_rev_1h", "RSI 25/75 trend yönünde 1R", 1, mean_reversion(25, 75, True), ExitRule(1.5, None, 1.0, 24)),
    Variant("mean_rev_1h", "RSI 25/75 trend yönünde 1.5R", 1, mean_reversion(25, 75, True), ExitRule(1.5, None, 1.5, 24)),
    Variant("mean_rev_1h", "RSI 20/80 filtresiz 1R", 1, mean_reversion(20, 80, False), ExitRule(1.5, None, 1.0, 24)),
    Variant("funding_1h", "fonlama +0.03/-0.01 2R", 1, funding_contrarian(0.0003, -0.0001), ExitRule(2.0, None, 2.0, 48)),
    Variant("funding_1h", "fonlama +0.05/-0.02 2R", 1, funding_contrarian(0.0005, -0.0002), FUNDING_EXITS),
]


# ------------------------------------------------------------------- simulation

def simulate(b: Bars, i0: int, direction: str, signal_atr: float, rule: ExitRule, limits: RiskLimits,
             venue: VenueCosts, atr_series: np.ndarray) -> Optional[Trade]:
    s = sign(direction)
    entry = b.opens[i0] * (1.0 + s * TAKER_SLIPPAGE_BPS / 10_000.0)
    distance = float(np.clip(signal_atr * rule.stop_atr, entry * limits.min_stop_pct / 100.0,
                             entry * limits.max_stop_pct / 100.0))
    stop = entry - s * distance
    initial_stop = stop
    target = entry + s * distance * rule.target_r if rule.target_r else None
    minimum = 10.0 if venue.name == "hyperliquid" else MIN_NOTIONAL.get(b.symbol, 5.0)
    sized = size_position(entry, stop, venue, limits, Filters(minimum, 0.0), limits.capital_usd, limits.capital_usd)
    if isinstance(sized, Rejected):
        return None
    qty = sized.qty
    max_bars = max(1, int(rule.max_hold_hours * HOUR_MS / b.bar_ms))
    last = min(i0 + max_bars, len(b.ts) - 1)
    stop_slip = limits.stop_slippage_bps / 10_000.0
    fill, is_maker, reason, exit_i = None, False, "süre doldu", last
    for i in range(i0, last + 1):
        adverse = b.lows[i] if s > 0 else b.highs[i]
        favorable = b.highs[i] if s > 0 else b.lows[i]
        if (adverse - stop) * s <= 0:
            base = min(stop, b.opens[i]) if s > 0 else max(stop, b.opens[i])
            fill, reason, exit_i = base * (1.0 - s * stop_slip), "stop", i
            break
        if target is not None and (favorable - target) * s > 0:
            fill, is_maker, reason, exit_i = target, True, "hedef", i
            break
        if rule.trail_atr and np.isfinite(atr_series[i]):
            # Trail moves only after the bar closes, so the next bar uses it.
            candidate = b.closes[i] - s * rule.trail_atr * atr_series[i]
            stop = max(stop, candidate) if s > 0 else min(stop, candidate)
    if fill is None:
        fill = b.closes[exit_i] * (1.0 - s * TAKER_SLIPPAGE_BPS / 10_000.0)
    in_trade = (b.funding_ts > b.ts[i0]) & (b.funding_ts <= b.ts[exit_i] + b.bar_ms)
    funding = float(np.sum(b.funding_rate[in_trade])) * entry * qty * s
    entry_fee = fee_usd(entry * qty, venue.taker_pct)
    exit_fee = fee_usd(fill * qty, venue.maker_pct if is_maker else venue.taker_pct)
    gross = (fill - entry) * qty * s
    net = gross - entry_fee - exit_fee - funding
    risk = abs(entry - initial_stop) * qty
    return Trade(b.symbol, direction, float(b.ts[i0]), float(b.ts[exit_i]), net / risk, net,
                 entry_fee + exit_fee, funding, reason)


def portfolio(cands: List[Candidate], rule: ExitRule, limits: RiskLimits, venue: VenueCosts,
              atrs: Dict[str, np.ndarray]) -> List[Trade]:
    cands = sorted(cands, key=lambda c: c[0].ts[c[1]])
    trades: List[Trade] = []
    live: List[Trade] = []
    last_open: Dict[str, float] = {}
    day_net: Dict[int, float] = {}
    cooldown_ms = limits.symbol_cooldown_seconds * 1000.0
    for b, i0, direction, a in cands:
        now = b.ts[i0]
        live = [t for t in live if t.exit_ts + b.bar_ms > now]
        if (len(live) >= limits.max_open_positions
                or any(t.symbol == b.symbol for t in live)
                or sum(t.direction == direction for t in live) >= limits.max_same_direction
                or now - last_open.get(b.symbol, -1e18) < cooldown_ms
                or day_net.get(int(now // 86_400_000), 0.0) <= -limits.daily_loss_stop_usd
                or not np.isfinite(a)):
            continue
        t = simulate(b, i0, direction, a, rule, limits, venue, atrs[b.symbol])
        if not t:
            continue
        trades.append(t)
        live.append(t)
        last_open[b.symbol] = now
        d = int(t.exit_ts // 86_400_000)
        day_net[d] = day_net.get(d, 0.0) + t.net
    return trades


def score(trades: List[Trade]) -> dict:
    if not trades:
        return {"n": 0, "mean_r": 0.0, "t": 0.0, "net": 0.0, "win": 0.0, "pf": 0.0, "dd": 0.0, "costs": 0.0, "funding": 0.0}
    r = np.array([t.r for t in trades])
    nets = np.array([t.net for t in sorted(trades, key=lambda t: t.exit_ts)])
    eq = np.cumsum(nets)
    peak = np.maximum.accumulate(np.concatenate(([0.0], eq)))[1:]
    gains, losses = nets[nets > 0].sum(), -nets[nets <= 0].sum()
    return {
        "n": len(trades),
        "mean_r": float(r.mean()),
        "t": float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))) if len(r) > 1 and r.std() > 0 else 0.0,
        "net": float(nets.sum()),
        "win": float((nets > 0).mean() * 100.0),
        "pf": float(gains / losses) if losses > 0 else float("inf"),
        "dd": float((peak - eq).max()),
        "costs": float(sum(t.costs for t in trades)),
        "funding": float(sum(t.funding for t in trades)),
    }


def random_mean_r(cands: List[Candidate], rule: ExitRule, limits: RiskLimits, venue: VenueCosts,
                  atrs: Dict[str, np.ndarray], lo_ms: float, hi_ms: float, runs: int, seed: int = 11) -> np.ndarray:
    """Mean R per trade for random entries over the same period."""
    rng = np.random.default_rng(seed)
    results = []
    for _ in range(runs):
        drawn: List[Candidate] = []
        for b, _i, _d, _a in cands:
            valid = np.flatnonzero((b.ts >= lo_ms) & (b.ts < hi_ms))
            valid = valid[valid > 200]
            if len(valid) == 0:
                continue
            i = int(rng.choice(valid))
            a_series = atrs[b.symbol]
            drawn.append((b, i, "LONG" if rng.random() < 0.5 else "SHORT", a_series[i - 1]))
        trades = portfolio(drawn, rule, limits, venue, atrs)
        if trades:
            results.append(float(np.mean([t.r for t in trades])))
    return np.array(results)


def evaluate_all(hourly: Dict[str, Bars], limits: RiskLimits, runs: int) -> dict:
    limits = replace(limits, min_stop_pct=0.5, max_stop_pct=15.0)
    first = min(b.ts[0] for b in hourly.values())
    last = max(b.ts[-1] for b in hourly.values())
    boundary = first + (last - first) * TRAIN_FRACTION
    venue = PERP_VENUES["binance"]
    frames = {h: {s: (b if h == 1 else resample(b, h)) for s, b in hourly.items()} for h in (1, 4)}
    atrs = {h: {s: atr(b) for s, b in frames[h].items()} for h in (1, 4)}
    rows = []
    cands_by_variant = {}
    for v in VARIANTS:
        # A signal on the final bar has no next bar to enter on.
        cands: List[Candidate] = [(b, i + 1, d, a) for b in frames[v.hours].values()
                                  for i, d, a in v.make(b) if i + 1 < len(b.ts)]
        cands_by_variant[v.name] = cands
        train = [c for c in cands if c[0].ts[c[1]] < boundary]
        rows.append({"family": v.family, "name": v.name, "train": score(portfolio(train, v.exits, limits, venue, atrs[v.hours]))})
    report = {"boundary_ms": boundary, "first_ms": first, "last_ms": last, "variants": rows, "families": {}}
    for family in sorted({v.family for v in VARIANTS}):
        members = [v for v in VARIANTS if v.family == family]
        scored = [(next(r for r in rows if r["name"] == v.name), v) for v in members]
        eligible = [x for x in scored if x[0]["train"]["n"] >= 30] or scored
        _, best = max(eligible, key=lambda x: x[0]["train"]["mean_r"])
        cands = [c for c in cands_by_variant[best.name] if c[0].ts[c[1]] >= boundary]
        trades = portfolio(cands, best.exits, limits, venue, atrs[best.hours])
        test = score(trades)
        rand = random_mean_r(cands, best.exits, limits, venue, atrs[best.hours], boundary, last, runs)
        report["families"][family] = {
            "chosen": best.name, "test": test,
            "random_mean_r": float(rand.mean()) if len(rand) else None,
            "random_p95_r": float(np.percentile(rand, 95)) if len(rand) else None,
            "beats_random_pct": float((test["mean_r"] > rand).mean() * 100.0) if len(rand) else None,
            "exits": {r: sum(t.reason == r for t in trades) for r in {t.reason for t in trades}},
        }
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--out", default=str(settings.DATA_DIR / "research_report.json"))
    args = parser.parse_args()
    print(f"Loading {args.days} days...", flush=True)
    hourly = asyncio.run(load_hourly(settings.SYMBOLS, args.days))
    report = evaluate_all(hourly, settings.LIMITS, args.runs)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
