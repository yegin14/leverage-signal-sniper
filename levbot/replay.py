"""
Replays the live bot's rules hour by hour over past prices: python -m levbot.replay --days 90

Uses the same strategies, the same check list (decide), the same sizing and the same trailing stop as the live bot,
the $100 budget and the $10 margin cap. Differences from live, all on the careful side or unknowable:
  - entry at the next hour's open plus slippage; the historical spread is unknown, so the spread check sees zero;
  - a stop is filled at the stop price, or at the bar's open when the bar gapped through it, minus slippage;
  - fees are taker on both sides; funding is charged from the real funding history.
"""

import argparse
import asyncio
import json
import time
from collections import Counter, defaultdict
from dataclasses import replace
from typing import Dict, List

import numpy as np

from tradebot.research import load_hourly
from tradebot.strategies import Bars, atr, resample

from . import config
from .binance import SymbolRules
from .strategy import STRATEGIES, decide, size, trail_stop

HOUR = 3_600_000
SLIP = config.SLIPPAGE_PCT / 100.0
FEE = config.TAKER_FEE_PCT / 100.0
STOP_SLIP = 0.0005
MIN_NOTIONAL = {"BTCUSDT": 50.0, "ETHUSDT": 20.0}


def cut(b: Bars, end: int, keep: int = 700) -> Bars:
    s = max(0, end + 1 - keep)
    return Bars(b.symbol, b.bar_ms, b.ts[s:end + 1], b.opens[s:end + 1], b.highs[s:end + 1], b.lows[s:end + 1],
                b.closes[s:end + 1], b.volumes[s:end + 1], b.funding_ts, b.funding_rate)


def run(hourly: Dict[str, Bars], days: int, only=None) -> dict:
    chosen = [s for s in STRATEGIES if not only or s.key in only]
    frames = {1: hourly, 4: {s: resample(b, 4) for s, b in hourly.items()}}
    atrs = {h: {s: atr(b) for s, b in frames[h].items()} for h in frames}
    end_ms = min(b.ts[-1] for b in hourly.values())
    start_ms = end_ms - days * 86_400_000
    # Every base signal over the whole history: each depends only on bars up to its own.
    hits = defaultdict(list)  # bar close time -> [(strategy, symbol, index, direction, atr)]
    for s in chosen:
        for sym, b in frames[s.bar_hours].items():
            for i, d, a in s.base(b):
                close_ms = b.ts[i] + b.bar_ms
                if start_ms <= close_ms <= end_ms:
                    hits[close_ms].append((s, sym, i, d, a))
    hidx = {s: {int(t): i for i, t in enumerate(b.ts)} for s, b in hourly.items()}
    rules = {s: SymbolRules(step=1e-8, min_qty=0.0, tick=1e-8, min_notional=MIN_NOTIONAL.get(s, 5.0)) for s in hourly}

    budget = config.BUDGET_USD
    open_: List[dict] = []
    closed: List[dict] = []
    peak = budget
    rejects: Counter = Counter()
    decisions = 0
    curve = []

    def equity_at(t_close_ms):
        unreal = 0.0
        for p in open_:
            i = hidx[p["symbol"]].get(int(t_close_ms - HOUR))
            if i is not None:
                px = hourly[p["symbol"]].closes[i]
                unreal += (px - p["entry"]) * p["qty"] * (1 if p["direction"] == "LONG" else -1)
        return budget + sum(c["net"] for c in closed) + unreal - sum(p["entry_fee"] for p in open_)

    def close(p, price, t_ms, reason):
        sgn = 1 if p["direction"] == "LONG" else -1
        b = hourly[p["symbol"]]
        held = (b.funding_ts > p["opened_ms"]) & (b.funding_ts <= t_ms)
        funding = -float(np.sum(b.funding_rate[held])) * p["entry"] * p["qty"] * sgn
        gross = (price - p["entry"]) * p["qty"] * sgn
        exit_fee = price * p["qty"] * FEE
        net = gross - p["entry_fee"] - exit_fee + funding
        open_.remove(p)
        closed.append({**p, "exit": price, "closed_ms": t_ms, "reason": reason, "net": net, "funding": funding,
                       "fees": p["entry_fee"] + exit_fee, "r": net / p["risk"] if p["risk"] else 0.0})

    t = start_ms - (start_ms % HOUR)
    while t + HOUR <= end_ms:
        close_ms = t + HOUR
        # 1. Stops inside this hour, on hourly highs and lows.
        for p in list(open_):
            i = hidx[p["symbol"]].get(int(t))
            if i is None or p["opened_ms"] > t:
                continue
            b = hourly[p["symbol"]]
            if p["direction"] == "LONG" and b.lows[i] <= p["stop"]:
                close(p, min(p["stop"], b.opens[i]) * (1 - STOP_SLIP), close_ms, "izleyen stop" if p["moves"] else "zarar-kes")
            elif p["direction"] == "SHORT" and b.highs[i] >= p["stop"]:
                close(p, max(p["stop"], b.opens[i]) * (1 + STOP_SLIP), close_ms, "izleyen stop" if p["moves"] else "zarar-kes")
        # 2. Bar closes: hold limit and trailing, per strategy interval.
        for p in list(open_):
            s = p["strategy"]
            if close_ms % (s.bar_hours * HOUR):
                continue
            fb = frames[s.bar_hours][p["symbol"]]
            j = int(np.searchsorted(fb.ts, close_ms - s.bar_hours * HOUR))
            if j >= len(fb.ts) or fb.ts[j] != close_ms - s.bar_hours * HOUR:
                continue
            if close_ms - p["opened_ms"] >= s.exits.max_hold_hours * HOUR:
                close(p, fb.closes[j] * (1 - SLIP if p["direction"] == "LONG" else 1 + SLIP), close_ms, "süre doldu")
                continue
            new = trail_stop(p["direction"], p["stop"], float(fb.closes[j]), float(atrs[s.bar_hours][p["symbol"]][j]), s.exits)
            if new != p["stop"]:
                p["stop"], p["moves"] = new, p["moves"] + 1
        eq = equity_at(close_ms)
        realized = budget + sum(c["net"] for c in closed)
        peak = max(peak, realized)
        if close_ms % (6 * HOUR) == 0:
            curve.append([close_ms / 1000, round(eq, 3)])
        # 3. New signals at this close, entered at the next hour's open.
        for s, sym, i, direction, a in hits.get(close_ms, []):
            nxt = hidx[sym].get(int(close_ms))
            if nxt is None:
                continue
            entry_open = float(hourly[sym].opens[nxt])
            decisions += 1
            fb = cut(frames[s.bar_hours][sym], i)
            margin = sum(p["margin"] for p in open_)
            day = int(close_ms // 86_400_000)
            book = {"free": max(0.0, eq - margin), "open_count": len(open_), "symbol_open": any(p["symbol"] == sym for p in open_),
                    "same_direction": sum(p["direction"] == direction for p in open_),
                    "day_net": sum(c["net"] for c in closed if int(c["closed_ms"] // 86_400_000) == day), "drawdown": max(0.0, peak - realized)}
            entry = entry_open * (1 + SLIP if direction == "LONG" else 1 - SLIP)
            market = {"bid": entry, "ask": entry, "funding": float(hourly[sym].funding_rate[
                max(0, np.searchsorted(hourly[sym].funding_ts, close_ms, side="right") - 1)])}
            d = decide(s, fb, direction, float(a), market, rules[sym], book, now=close_ms / 1000 + 30)
            if not d.passed:
                for c in d.failed():
                    rejects[c.name] += 1
                continue
            plan = d.plan
            open_.append({"strategy": s, "symbol": sym, "direction": direction, "entry": entry, "qty": plan.qty, "stop": plan.stop,
                          "risk": plan.risk, "margin": plan.margin, "leverage": plan.leverage, "entry_fee": entry * plan.qty * FEE,
                          "opened_ms": close_ms, "moves": 0})
        t += HOUR
    for p in list(open_):
        i = len(hourly[p["symbol"]].ts) - 1
        close(p, float(hourly[p["symbol"]].closes[i]), end_ms, "test bitti (açıktı)")

    by = {}
    for s in STRATEGIES:
        cs = [c for c in closed if c["strategy"].key == s.key]
        by[s.label] = {"trades": len(cs), "wins": sum(c["net"] > 0 for c in cs), "net": round(sum(c["net"] for c in cs), 2),
                       "avg_r": round(float(np.mean([c["r"] for c in cs])), 3) if cs else 0.0}
    nets = [c["net"] for c in sorted(closed, key=lambda c: c["closed_ms"])]
    eqc = budget + np.cumsum(nets) if nets else np.array([budget])
    worst_dd = float(np.max(np.maximum.accumulate(np.concatenate(([budget], eqc))) - np.concatenate(([budget], eqc))))
    return {
        "days": days, "from": time.strftime("%Y-%m-%d", time.gmtime(start_ms / 1000)), "to": time.strftime("%Y-%m-%d", time.gmtime(end_ms / 1000)),
        "signals": decisions, "signals_per_day": round(decisions / days, 2), "entered": len(closed),
        "final_equity": round(budget + sum(nets), 2), "net": round(sum(nets), 2), "wins": sum(n > 0 for n in nets),
        "win_rate": round(sum(n > 0 for n in nets) / len(nets) * 100, 1) if nets else 0.0,
        "fees": round(sum(c["fees"] for c in closed), 2), "funding": round(sum(c["funding"] for c in closed), 2),
        "worst_drawdown": round(worst_dd, 2), "best_trade": round(max(nets), 2) if nets else 0.0, "worst_trade": round(min(nets), 2) if nets else 0.0,
        "by_strategy": by, "rejects": dict(rejects.most_common()),
        "reasons": dict(Counter(c["reason"] for c in closed)), "curve": curve,
        "by_symbol": {k: round(v, 2) for k, v in sorted(Counter({c["symbol"]: 0 for c in closed}).items())},
        "symbol_net": {sym: round(sum(c["net"] for c in closed if c["symbol"] == sym), 2) for sym in sorted({c["symbol"] for c in closed})},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--out", default=str(config.DATA_DIR / "replay.json"))
    parser.add_argument("--only", nargs="*", help="strategy keys to replay (default: all)")
    args = parser.parse_args()
    hourly = asyncio.run(load_hourly(config.SYMBOLS, 730))  # cached by tradebot.research
    report = run(hourly, args.days, args.only)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)
    print(json.dumps({k: v for k, v in report.items() if k != "curve"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
