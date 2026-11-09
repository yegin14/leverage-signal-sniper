"""
Intraday strategies on 15-minute bars: python -m levbot.intraday_study

Declared 2026-11-09 before any result. Four families, two parameter sets each, exits within a day:
  pullback   1h trend up (EMA50 > EMA200 on 1h) and 15m RSI dips under 35 then closes back over 40 -> long (mirror short)
  session    after 07:00 UTC, a 15m close beyond the 00:00-07:00 UTC range, in the 1h trend's direction, until 12:00 UTC
  vwap       ADX(14) under 20 (no trend) and price 2.5 ATR from the day's VWAP -> fade back towards it
  burst      volume over 3x the 20-bar average, range over 2 ATR, close in the outer 20%, with the 1h trend
Each is scored with taker fees on both sides and with a maker entry (limit order at the signal close, only filled
when the next bar trades through it). Selection on the first 60%, one score on the last 40% against random entries.
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List

import aiohttp
import numpy as np

from tradebot import settings
from tradebot.costs import PERP_VENUES
from tradebot.research import TRAIN_FRACTION, portfolio, random_mean_r, score
from tradebot.strategies import Bars, ExitRule, atr, ema, rsi

from . import config
from .filters import adx

M15 = 900_000
HOUR = 3_600_000


async def load_15m(symbols, days: int) -> Dict[str, Bars]:
    cache = config.DATA_DIR / "history15"
    cache.mkdir(parents=True, exist_ok=True)
    end = int(time.time() * 1000) // HOUR * HOUR
    start = end - days * 86_400_000
    out = {}
    async with aiohttp.ClientSession() as s:
        for sym in symbols:
            path = cache / f"{sym}_15m_{days}d_{end // 86_400_000}.json"
            if path.exists():
                data = json.loads(path.read_text())
            else:
                k, cur = [], start
                while cur < end:
                    async with s.get("https://fapi.binance.com/fapi/v1/klines",
                                     params={"symbol": sym, "interval": "15m", "limit": 1500, "startTime": cur}) as r:
                        r.raise_for_status()
                        rows = await r.json()
                    if not rows:
                        break
                    k += rows
                    cur = int(rows[-1][0]) + M15
                    await asyncio.sleep(0.25)  # Stay far under the weight limit: the live bot shares this IP.
                f, cur = [], start
                while cur < end:
                    async with s.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": sym, "limit": 1000, "startTime": cur}) as r:
                        r.raise_for_status()
                        rows = await r.json()
                    if not rows:
                        break
                    f += rows
                    cur = int(rows[-1]["fundingTime"]) + 1
                    await asyncio.sleep(0.25)
                data = {"k": [[x[0], float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in k if x[0] < end],
                        "f": [[int(x["fundingTime"]), float(x["fundingRate"])] for x in f]}
                path.write_text(json.dumps(data))
                print(f"  {sym}: {len(data['k'])} bars", flush=True)
            k = np.array(data["k"], dtype=np.float64)
            f = np.array(data["f"], dtype=np.float64) if data["f"] else np.zeros((0, 2))
            out[sym] = Bars(sym, M15, k[:, 0], k[:, 1], k[:, 2], k[:, 3], k[:, 4], k[:, 5], f[:, 0], f[:, 1])
    return out


def hourly_trend(b: Bars) -> np.ndarray:
    """+1/-1/0 per 15m bar: EMA50 vs EMA200 of hourly closes, using only hours already closed."""
    n = len(b.ts) // 4 * 4
    closes_1h = b.closes[3:n:4]
    f, s = ema(closes_1h, 50), ema(closes_1h, 200)
    sign_1h = np.where(f > s, 1, np.where(f < s, -1, 0))
    sign_1h[:200] = 0
    out = np.zeros(len(b.ts), dtype=int)
    for i in range(len(b.ts)):
        h = (i + 1) // 4 - 1  # last fully closed hour
        if h >= 0 and h < len(sign_1h):
            out[i] = sign_1h[h]
    return out


def pullback(lo: float, back: float):
    def f(b: Bars):
        r, t, a = rsi(b.closes), hourly_trend(b), atr(b)
        out = []
        for i in range(300, len(b.ts)):
            if t[i] > 0 and r[i - 1] < lo and r[i] >= back:
                out.append((i, "LONG", a[i]))
            elif t[i] < 0 and r[i - 1] > 100 - lo and r[i] <= 100 - back:
                out.append((i, "SHORT", a[i]))
        return out
    return f


def session(end_hour: int):
    def f(b: Bars):
        t, a = hourly_trend(b), atr(b)
        hours = ((b.ts // HOUR) % 24).astype(int)
        days = (b.ts // 86_400_000).astype(int)
        out, rng, done = [], {}, set()
        for i in range(len(b.ts)):
            d = days[i]
            if hours[i] < 7:
                hi, lo = rng.get(d, (-np.inf, np.inf))
                rng[d] = (max(hi, b.highs[i]), min(lo, b.lows[i]))
                continue
            if d in done or d not in rng or hours[i] >= end_hour or i < 300:
                continue
            hi, lo = rng[d]
            if b.closes[i] > hi and t[i] > 0:
                out.append((i, "LONG", a[i])); done.add(d)
            elif b.closes[i] < lo and t[i] < 0:
                out.append((i, "SHORT", a[i])); done.add(d)
        return out
    return f


def vwap(k: float):
    def f(b: Bars):
        a, x = atr(b), adx(b)
        days = (b.ts // 86_400_000).astype(int)
        typical = (b.highs + b.lows + b.closes) / 3
        out, pv, vol, last = [], 0.0, 0.0, None
        for i in range(len(b.ts)):
            if days[i] != last:
                pv, vol, last = 0.0, 0.0, days[i]
            pv += typical[i] * b.volumes[i]
            vol += b.volumes[i]
            if i < 300 or vol <= 0 or not np.isfinite(x[i]) or x[i] >= 20:
                continue
            v = pv / vol
            if b.closes[i] < v - k * a[i]:
                out.append((i, "LONG", a[i]))
            elif b.closes[i] > v + k * a[i]:
                out.append((i, "SHORT", a[i]))
        return out
    return f


def burst(mult: float):
    def f(b: Bars):
        a, t = atr(b), hourly_trend(b)
        out = []
        for i in range(300, len(b.ts)):
            avg = b.volumes[i - 20:i].mean()
            span = b.highs[i] - b.lows[i]
            if avg <= 0 or span <= 0 or b.volumes[i] < mult * avg or span < 2 * a[i - 1]:
                continue
            place = (b.closes[i] - b.lows[i]) / span
            if place >= 0.8 and t[i] > 0:
                out.append((i, "LONG", a[i]))
            elif place <= 0.2 and t[i] < 0:
                out.append((i, "SHORT", a[i]))
        return out
    return f


@dataclass(frozen=True)
class V:
    family: str
    name: str
    make: Callable
    exits: ExitRule


VARIANTS = [
    V("pullback", "RSI 35→40, 2R", pullback(35, 40), ExitRule(1.5, None, 2.0, 12)),
    V("pullback", "RSI 30→35, izleyen", pullback(30, 35), ExitRule(1.5, 2.0, None, 24)),
    V("session", "Asya aralığı kırılımı 12:00'ye kadar, 2R", session(12), ExitRule(1.5, None, 2.0, 12)),
    V("session", "Asya aralığı kırılımı 16:00'ya kadar, izleyen", session(16), ExitRule(1.5, 2.0, None, 24)),
    V("vwap", "VWAP 2.5 ATR dönüş, 1.5R", vwap(2.5), ExitRule(1.0, None, 1.5, 6)),
    V("vwap", "VWAP 3 ATR dönüş, 2R", vwap(3.0), ExitRule(1.0, None, 2.0, 8)),
    V("burst", "hacim 3x patlaması, izleyen", burst(3.0), ExitRule(1.5, 2.0, None, 12)),
    V("burst", "hacim 4x patlaması, 2R", burst(4.0), ExitRule(1.5, None, 2.0, 12)),
]


def maker_venue(v):
    """Maker entry: the entry fee is the maker rate; exits stay taker (stops) except targets, already maker."""
    return replace(v, taker_pct=(v.maker_pct + v.taker_pct) / 2)


def run(data: Dict[str, Bars], runs: int) -> dict:
    limits = replace(settings.RESEARCH_LIMITS, min_stop_pct=0.3, max_stop_pct=8.0)
    first = min(b.ts[0] for b in data.values())
    last = max(b.ts[-1] for b in data.values())
    boundary = first + (last - first) * TRAIN_FRACTION
    atrs = {s: atr(b) for s, b in data.items()}
    out = {"days": round((last - first) / 86_400_000), "families": {}}
    for fee_name, venue in (("taker", PERP_VENUES["binance"]), ("maker giriş", maker_venue(PERP_VENUES["binance"]))):
        for family in sorted({v.family for v in VARIANTS}):
            best = None
            for v in [x for x in VARIANTS if x.family == family]:
                cands = [(b, i + 1, d, a) for b in data.values() for i, d, a in v.make(b) if i + 1 < len(b.ts)]
                train = [c for c in cands if c[0].ts[c[1]] < boundary]
                sc = score(portfolio(train, v.exits, limits, venue, atrs))
                if best is None or (sc["n"] >= 30 and sc["mean_r"] > best[1]["mean_r"]):
                    best = (v, sc, cands)
            v, train_sc, cands = best
            test_c = [c for c in cands if c[0].ts[c[1]] >= boundary]
            trades = portfolio(test_c, v.exits, limits, venue, atrs)
            sc = score(trades)
            rand = random_mean_r(test_c, v.exits, limits, venue, atrs, boundary, last, runs)
            days = (last - boundary) / 86_400_000
            out["families"][f"{family} / {fee_name}"] = {
                "chosen": v.name, "train": train_sc, "test": sc, "trades_per_day": round(sc["n"] / days, 2),
                "random_mean_r": float(rand.mean()) if len(rand) else None,
                "beats_random_pct": float((sc["mean_r"] > rand).mean() * 100) if len(rand) else None}
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--runs", type=int, default=100)
    args = parser.parse_args()
    data = asyncio.run(load_15m(config.SYMBOLS, args.days))
    report = run(data, args.runs)
    (config.DATA_DIR / "intraday_study.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    for k, r in report["families"].items():
        t = r["test"]
        print(f"{k:28s} {r['chosen']:42s} train {r['train']['mean_r']:+.3f} | test n={t['n']} ({r['trades_per_day']}/gün) "
              f"meanR={t['mean_r']:+.3f} t={t['t']:+.2f} net={t['net']:+.1f} win={t['win']:.0f}% dd={t['dd']:.1f} beats={r['beats_random_pct']}")


if __name__ == "__main__":
    main()
