"""Cost-inclusive backtest of the technical signal, compared with random entries.

Run: python -m tradebot.backtest --days 180

The agent desk cannot be tested here: news history is not available, and replaying it would cost
API money. This answers one question only: does the technical signal beat chance after costs?

Fill rules match tradebot.paper. Entry at the next bar open plus slippage. Stop first when a bar
touches both levels. Target needs a trade strictly through it. Funding comes from real history.
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
import numpy as np

from . import settings
from .costs import PERP_VENUES, VenueCosts, fee_usd, sign
from .risk import Filters, Rejected, size_position, stop_and_target
from .settings import RiskLimits
from .signals import MIN_BARS, compute_features, evaluate
from .venues import BINANCE_FAPI, HEADERS

BAR_MS = 900_000
CACHE = settings.DATA_DIR / "history"
# Assumed taker slippage per side when no historical order book exists; see measure_slippage().
ENTRY_SLIPPAGE_BPS = 2.0
EXIT_SLIPPAGE_BPS = 2.0

MIN_NOTIONAL = {"BTCUSDT": 50.0, "ETHUSDT": 20.0, "LINKUSDT": 20.0}


@dataclass
class History:
    symbol: str
    ts: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    volumes: np.ndarray
    funding_ts: np.ndarray
    funding_rate: np.ndarray


async def _download_klines(session, symbol: str, start_ms: int, end_ms: int) -> List[list]:
    rows: List[list] = []
    cursor = start_ms
    while cursor < end_ms:
        async with session.get(f"{BINANCE_FAPI}/fapi/v1/klines", headers=HEADERS, params={
            "symbol": symbol, "interval": "15m", "startTime": cursor, "limit": 1500,
        }, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            page = await r.json()
        if not page:
            break
        rows.extend([k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in page)
        cursor = int(page[-1][0]) + BAR_MS
        await asyncio.sleep(0.15)
    return rows


async def _download_funding(session, symbol: str, start_ms: int, end_ms: int) -> List[list]:
    rows: List[list] = []
    cursor = start_ms
    while cursor < end_ms:
        async with session.get(f"{BINANCE_FAPI}/fapi/v1/fundingRate", headers=HEADERS, params={
            "symbol": symbol, "startTime": cursor, "limit": 1000,
        }, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            page = await r.json()
        if not page:
            break
        rows.extend([int(x["fundingTime"]), float(x["fundingRate"])] for x in page)
        cursor = int(page[-1]["fundingTime"]) + 1
        if len(page) < 1000:
            break
        await asyncio.sleep(0.15)
    return rows


async def load_history(symbols, days: int) -> Dict[str, History]:
    CACHE.mkdir(parents=True, exist_ok=True)
    end_ms = int(time.time() * 1000) // BAR_MS * BAR_MS
    start_ms = end_ms - days * 86_400_000
    out: Dict[str, History] = {}
    async with aiohttp.ClientSession() as session:
        for symbol in symbols:
            path = CACHE / f"{symbol}_{days}d_{end_ms // 86_400_000}.json"
            if path.exists():
                data = json.loads(path.read_text())
            else:
                data = {"k": await _download_klines(session, symbol, start_ms, end_ms),
                        "f": await _download_funding(session, symbol, start_ms, end_ms)}
                path.write_text(json.dumps(data))
                print(f"  {symbol}: {len(data['k'])} bars, {len(data['f'])} funding rows", flush=True)
            k = np.array(data["k"], dtype=np.float64)
            f = np.array(data["f"], dtype=np.float64) if data["f"] else np.zeros((0, 2))
            out[symbol] = History(symbol, k[:, 0], k[:, 1], k[:, 2], k[:, 3], k[:, 4], k[:, 5], f[:, 0], f[:, 1])
    return out


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_i: int
    exit_i: int
    entry_ts: float
    exit_ts: float
    r_net: float
    net: float
    costs: float
    funding: float
    reason: str


def simulate_trade(h: History, entry_i: int, direction: str, raw_stop: float, limits: RiskLimits,
                   venue: VenueCosts) -> Optional[Trade]:
    """One trade from the open of bar entry_i until an exit. None when it could not be sized."""
    s = sign(direction)
    entry = h.opens[entry_i] * (1.0 + s * ENTRY_SLIPPAGE_BPS / 10_000.0)
    stop, target = stop_and_target(direction, entry, raw_stop, limits)
    filters = Filters(MIN_NOTIONAL.get(h.symbol, 5.0), 0.0)
    if venue.name == "hyperliquid":
        filters = Filters(10.0, 0.0)
    sized = size_position(entry, stop, venue, limits, filters, limits.capital_usd, limits.capital_usd)
    if isinstance(sized, Rejected):
        return None
    qty = sized.qty
    max_bars = int(limits.max_hold_hours * 3600 / 900)
    last = min(entry_i + max_bars, len(h.ts) - 1)
    exit_i, fill, is_maker, reason = last, None, False, "süre doldu"
    stop_slip = limits.stop_slippage_bps / 10_000.0
    for i in range(entry_i, last + 1):
        adverse = h.lows[i] if s > 0 else h.highs[i]
        favorable = h.highs[i] if s > 0 else h.lows[i]
        if (adverse - stop) * s <= 0:
            # A gap past the stop fills at the open, not at the stop.
            base = min(stop, h.opens[i]) if s > 0 else max(stop, h.opens[i])
            exit_i, fill, reason = i, base * (1.0 - s * stop_slip), "stop"
            break
        if (favorable - target) * s > 0:
            exit_i, fill, is_maker, reason = i, target, True, "hedef"
            break
    if fill is None:
        fill = h.closes[exit_i] * (1.0 - s * EXIT_SLIPPAGE_BPS / 10_000.0)

    in_trade = (h.funding_ts > h.ts[entry_i]) & (h.funding_ts <= h.ts[exit_i] + BAR_MS)
    # Binance funding history is used for both venues: Hyperliquid's hourly history is not loaded.
    funding = float(np.sum(h.funding_rate[in_trade])) * entry * qty * s
    entry_fee = fee_usd(entry * qty, venue.taker_pct)
    exit_fee = fee_usd(fill * qty, venue.maker_pct if is_maker else venue.taker_pct)
    gross = (fill - entry) * qty * s
    net = gross - entry_fee - exit_fee - funding
    risk = abs(entry - stop) * qty
    return Trade(h.symbol, direction, entry_i, exit_i, float(h.ts[entry_i]), float(h.ts[exit_i]),
                 net / risk, net, entry_fee + exit_fee, funding, reason)


def gather_signals(h: History, features, min_score: int) -> List[Tuple[int, str, float]]:
    out = []
    for i in range(MIN_BARS, len(h.ts) - 1):
        s = evaluate(features, i, min_score=min_score)
        if s:
            out.append((i + 1, s.direction, s.raw_stop_distance))
    return out


def portfolio(candidates: List[Tuple[History, int, str, float]], limits: RiskLimits,
              venue: VenueCosts) -> List[Trade]:
    """Applies position limits in time order, as the live book does."""
    candidates = sorted(candidates, key=lambda c: c[0].ts[c[1]])
    trades: List[Trade] = []
    open_trades: List[Trade] = []
    last_open: Dict[str, float] = {}
    day_net: Dict[int, float] = {}
    cooldown_ms = limits.symbol_cooldown_seconds * 1000.0
    for h, entry_i, direction, raw_stop in candidates:
        now = h.ts[entry_i]
        open_trades = [t for t in open_trades if t.exit_ts + BAR_MS > now]
        if len(open_trades) >= limits.max_open_positions:
            continue
        if any(t.symbol == h.symbol for t in open_trades):
            continue
        if sum(1 for t in open_trades if t.direction == direction) >= limits.max_same_direction:
            continue
        if now - last_open.get(h.symbol, -1e18) < cooldown_ms:
            continue
        day = int(now // 86_400_000)
        if day_net.get(day, 0.0) <= -limits.daily_loss_stop_usd:
            continue
        trade = simulate_trade(h, entry_i, direction, raw_stop, limits, venue)
        if not trade:
            continue
        trades.append(trade)
        open_trades.append(trade)
        last_open[h.symbol] = now
        exit_day = int(trade.exit_ts // 86_400_000)
        day_net[exit_day] = day_net.get(exit_day, 0.0) + trade.net
    return trades


def summarize(trades: List[Trade]) -> dict:
    if not trades:
        return {"trades": 0, "net": 0.0, "win_rate": 0.0, "profit_factor": 0.0, "avg_r": 0.0,
                "max_drawdown": 0.0, "costs": 0.0, "funding": 0.0}
    nets = np.array([t.net for t in sorted(trades, key=lambda t: t.exit_ts)])
    equity = np.cumsum(nets)
    peak = np.maximum.accumulate(np.concatenate(([0.0], equity)))[1:]
    gains = nets[nets > 0].sum()
    losses = -nets[nets <= 0].sum()
    return {
        "trades": len(trades),
        "net": float(nets.sum()),
        "win_rate": float((nets > 0).mean() * 100.0),
        "profit_factor": float(gains / losses) if losses > 0 else float("inf"),
        "avg_r": float(np.mean([t.r_net for t in trades])),
        "max_drawdown": float((peak - equity).max()),
        "costs": float(sum(t.costs for t in trades)),
        "funding": float(sum(t.funding for t in trades)),
    }


def random_baseline(trade_template: List[Tuple[History, int, str, float]], limits: RiskLimits,
                    venue: VenueCosts, runs: int, seed: int = 7) -> dict:
    """Same symbols, stop sizes and trade count, with entry bar and direction chosen at random."""
    rng = np.random.default_rng(seed)
    results = []
    for _ in range(runs):
        shuffled = []
        for h, entry_i, _direction, raw_stop in trade_template:
            i = int(rng.integers(MIN_BARS, len(h.ts) - 1))
            shuffled.append((h, i, "LONG" if rng.random() < 0.5 else "SHORT", raw_stop))
        results.append(summarize(portfolio(shuffled, limits, venue)))
    nets = np.array([r["net"] for r in results])
    avg_r = np.array([r["avg_r"] for r in results])
    return {"runs": runs, "net_mean": float(nets.mean()), "avg_r_mean": float(avg_r.mean()),
            "avg_r_p95": float(np.percentile(avg_r, 95)), "avg_r_values": avg_r}


def split(candidates, boundary_ms: float):
    train = [c for c in candidates if c[0].ts[c[1]] < boundary_ms]
    test = [c for c in candidates if c[0].ts[c[1]] >= boundary_ms]
    return train, test


def run(history: Dict[str, History], limits: RiskLimits, baseline_runs: int = 200) -> dict:
    first = min(h.ts[0] for h in history.values())
    last = max(h.ts[-1] for h in history.values())
    boundary = first + (last - first) * 0.6
    features = {s: compute_features(h.highs, h.lows, h.closes, h.volumes) for s, h in history.items()}
    report: dict = {"boundary_ms": boundary, "first_ms": first, "last_ms": last, "grid": []}

    # Parameters are chosen on the first 60% only. The last 40% is scored once.
    for min_score in (60, 70):
        candidates = [(h, i, d, r) for s, h in history.items()
                      for i, d, r in gather_signals(h, features[s], min_score)]
        for reward in (1.5, 2.0, 3.0):
            lim = replace(limits, reward_r=reward)
            train, test = split(candidates, boundary)
            report["grid"].append({
                "min_score": min_score, "reward_r": reward,
                "train": summarize(portfolio(train, lim, PERP_VENUES["binance"])),
                "test": summarize(portfolio(test, lim, PERP_VENUES["binance"])),
            })
    best = max(report["grid"], key=lambda g: g["train"]["avg_r"] if g["train"]["trades"] >= 30 else -1e9)
    report["chosen"] = {"min_score": best["min_score"], "reward_r": best["reward_r"]}

    lim = replace(limits, reward_r=best["reward_r"])
    candidates = [(h, i, d, r) for s, h in history.items()
                  for i, d, r in gather_signals(h, features[s], best["min_score"])]
    _, test = split(candidates, boundary)
    for venue_name in ("binance", "hyperliquid"):
        venue = PERP_VENUES[venue_name]
        trades = portfolio(test, lim, venue)
        summary = summarize(trades)
        baseline = random_baseline(
            [c for c in test], lim, venue, baseline_runs)
        # Compare per trade: random portfolios take a different number of trades.
        values = baseline.pop("avg_r_values")
        summary["beats_random_pct"] = float((summary["avg_r"] > values).mean() * 100.0)
        report[f"test_{venue_name}"] = {"signal": summary, "random": baseline,
                                        "exit_reasons": _reasons(trades), "by_symbol": _by_symbol(trades)}

    report["buy_and_hold_test_pct"] = {
        s: float((h.closes[-1] / h.closes[h.ts >= boundary][0] - 1.0) * 100.0) for s, h in history.items()}
    return report


def _reasons(trades: List[Trade]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for t in trades:
        out[t.reason] = out.get(t.reason, 0) + 1
    return out


def _by_symbol(trades: List[Trade]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for t in trades:
        row = out.setdefault(t.symbol, {"trades": 0, "net": 0.0})
        row["trades"] += 1
        row["net"] = round(row["net"] + t.net, 2)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--runs", type=int, default=200)
    parser.add_argument("--out", default=str(settings.DATA_DIR / "backtest_report.json"))
    args = parser.parse_args()
    print(f"Downloading {args.days} days of history...", flush=True)
    history = asyncio.run(load_history(settings.SYMBOLS, args.days))
    report = run(history, replace(settings.RESEARCH_LIMITS, min_stop_pct=1.25, max_stop_pct=3.0), args.runs)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
