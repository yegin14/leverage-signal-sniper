"""
Which extra filters cut false breakouts? python -m levbot.filter_study

Rules fixed before looking at results:
  - The filters are the ones declared in levbot.filters; nothing is tuned here.
  - Every choice is made on the first 60% of the data only: single filters are scored there, then filters are added
    one at a time (greedy), each time the one that raises mean R most, while it adds at least 0.02R and keeps at least
    40 trades and 30% of the base strategy's trades.
  - The last 40% is scored once for the base and for the chosen set, against random entries with the same exits.
"""

import argparse
import asyncio
import json
from dataclasses import replace
from typing import Dict, List

import numpy as np

from tradebot import settings
from tradebot.costs import PERP_VENUES
from tradebot.research import TRAIN_FRACTION, load_hourly, portfolio, random_mean_r, score
from tradebot.strategies import TREND_1H_EXITS, TREND_4H_EXITS, atr, confirmed, resample, trend_breakout

from . import config
from .filters import LABELS, Context, apply, catalogue

BASES = {
    "4h trend + 5/6 onay (canlı)": dict(hours=4, make=confirmed(trend_breakout(20), 5, 6), lookback=20, exits=TREND_4H_EXITS),
    "1h trend 24": dict(hours=1, make=trend_breakout(24), lookback=24, exits=TREND_1H_EXITS),
}


def study(hourly, runs: int) -> dict:
    limits = replace(settings.RESEARCH_LIMITS, min_stop_pct=0.5, max_stop_pct=15.0)
    venue = PERP_VENUES["binance"]
    first = min(b.ts[0] for b in hourly.values())
    last = max(b.ts[-1] for b in hourly.values())
    boundary = first + (last - first) * TRAIN_FRACTION
    report = {"boundary_ms": boundary, "bases": {}}
    for name, base in BASES.items():
        h = base["hours"]
        frames = hourly if h == 1 else {s: resample(b, h) for s, b in hourly.items()}
        atrs = {s: atr(b) for s, b in frames.items()}
        ctx_by = {s: Context(frames.get("BTCUSDT")) for s in frames}
        raw = {s: base["make"](b) for s, b in frames.items()}

        def cands(names: List[str], lo: float, hi: float):
            out = []
            for s, b in frames.items():
                for i, d, a in apply(raw[s], b, names, base["lookback"], ctx_by[s]):
                    if i + 1 < len(b.ts) and lo <= b.ts[i + 1] < hi:
                        out.append((b, i + 1, d, a))
            return out

        def train_score(names):
            return score(portfolio(cands(names, first, boundary), base["exits"], limits, venue, atrs))

        base_train = train_score([])
        singles = {f: train_score([f]) for f in catalogue(base["lookback"])}
        chosen: List[str] = []
        current = base_train
        min_n = max(40, 0.3 * base_train["n"])
        while True:
            best = None
            for f in catalogue(base["lookback"]):
                if f in chosen or (f.startswith("adx") and any(c.startswith("adx") for c in chosen)):
                    continue
                sc = train_score(chosen + [f])
                if sc["n"] >= min_n and sc["mean_r"] >= current["mean_r"] + 0.02 and (best is None or sc["mean_r"] > best[1]["mean_r"]):
                    best = (f, sc)
            if not best:
                break
            chosen.append(best[0])
            current = best[1]

        def test(names):
            c = cands(names, boundary, last + 1)
            sc = score(portfolio(c, base["exits"], limits, venue, atrs))
            rand = random_mean_r(c, base["exits"], limits, venue, atrs, boundary, last, runs) if c else np.array([])
            sc["random_mean_r"] = float(rand.mean()) if len(rand) else None
            sc["beats_random_pct"] = float((sc["mean_r"] > rand).mean() * 100) if len(rand) else None
            return sc

        report["bases"][name] = {"train_base": base_train, "train_singles": singles, "chosen": chosen,
                                 "chosen_labels": [LABELS[c] for c in chosen], "train_chosen": current,
                                 "test_base": test([]), "test_chosen": test(chosen) if chosen else None}
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--out", default=str(config.DATA_DIR / "filter_study.json"))
    args = parser.parse_args()
    hourly = asyncio.run(load_hourly(config.SYMBOLS, 730))
    report = study(hourly, args.runs)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    for name, r in report["bases"].items():
        print("==", name)
        tb = r["train_base"]
        print(f"  train base: n={tb['n']} meanR={tb['mean_r']:+.3f} net={tb['net']:+.1f}")
        for f, sc in r["train_singles"].items():
            print(f"  train +{f:12s}: n={sc['n']:4d} meanR={sc['mean_r']:+.3f} net={sc['net']:+.1f}")
        print("  chosen:", r["chosen"], "train", {k: round(r["train_chosen"][k], 3) for k in ("n", "mean_r", "net")})
        for k in ("test_base", "test_chosen"):
            sc = r[k]
            if sc:
                print(f"  {k}: n={sc['n']} meanR={sc['mean_r']:+.3f} t={sc['t']:+.2f} net={sc['net']:+.1f} win={sc['win']:.0f}% "
                      f"dd={sc['dd']:.1f} random={sc['random_mean_r']} beats={sc['beats_random_pct']}")


if __name__ == "__main__":
    main()
