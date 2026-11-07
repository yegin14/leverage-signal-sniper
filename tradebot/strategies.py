"""Strategy families, shared by tradebot.research and the live scanner so both run the same code.

A strategy function takes Bars and returns (signal_bar_index, direction, atr_at_signal). Entry
happens after that bar closes. Nothing reads bars after the signal bar.

Evidence, 2024-11-07 to 2026-11-07, last 40% of the data, after costs, 10 symbols, $1.50 risk:
  trend_4h    +0.03R per trade, n=99, t=0.1, beat 67% of random runs
  funding_1h  +0.12R per trade, n=31, t=0.5, beat 89% of random runs
  trend_1h    -0.07R per trade, n=277, no better than random
  signal_15m  -0.12R per trade, n=359, 180-day test, no better than random
None is statistically distinguishable from chance. All run on paper, each in its own book, because
the owner wants many trades during the trial. A trade count here is not evidence of an edge.
"""

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np

from .signals import compute_features, ema, evaluate, wilder

HOUR_MS = 3_600_000
Hit = Tuple[int, str, float]


@dataclass
class Bars:
    symbol: str
    bar_ms: int
    ts: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    volumes: np.ndarray
    funding_ts: np.ndarray
    funding_rate: np.ndarray


@dataclass(frozen=True)
class ExitRule:
    stop_atr: float
    trail_atr: Optional[float]
    target_r: Optional[float]
    max_hold_hours: float
    # Stop distance band as a percent of price; None uses the general risk limits.
    min_stop_pct: Optional[float] = None
    max_stop_pct: Optional[float] = None


TREND_4H_EXITS = ExitRule(stop_atr=2.5, trail_atr=3.0, target_r=None, max_hold_hours=30 * 24)
TREND_1H_EXITS = ExitRule(stop_atr=2.0, trail_atr=3.0, target_r=None, max_hold_hours=10 * 24)
FUNDING_EXITS = ExitRule(stop_atr=2.0, trail_atr=None, target_r=2.0, max_hold_hours=48)
# signal_15m supplies its stop distance in place of an ATR, hence stop_atr=1.
SIGNAL_15M_EXITS = ExitRule(stop_atr=1.0, trail_atr=None, target_r=2.0, max_hold_hours=24,
                            min_stop_pct=1.25, max_stop_pct=3.0)
# Exits for trades started by news, when no strategy supplied them.
NEWS_EXITS = ExitRule(stop_atr=2.0, trail_atr=None, target_r=2.0, max_hold_hours=48)


def resample(b: Bars, hours: int) -> Bars:
    """Aggregates hourly bars on UTC boundaries, like an exchange's own higher timeframe."""
    bucket_ms = hours * HOUR_MS
    keys = (b.ts // bucket_ms).astype(np.int64)
    edges = np.flatnonzero(np.diff(keys)) + 1
    starts = np.concatenate(([0], edges))
    ends = np.concatenate((edges, [len(keys)]))
    complete = (ends - starts) == hours
    starts, ends = starts[complete], ends[complete]
    return Bars(
        b.symbol, bucket_ms, b.ts[starts], b.opens[starts],
        np.array([b.highs[s:e].max() for s, e in zip(starts, ends)]),
        np.array([b.lows[s:e].min() for s, e in zip(starts, ends)]),
        b.closes[ends - 1],
        np.array([b.volumes[s:e].sum() for s, e in zip(starts, ends)]),
        b.funding_ts, b.funding_rate,
    )


def atr(b: Bars) -> np.ndarray:
    prev = np.roll(b.closes, 1)
    prev[0] = b.closes[0]
    tr = np.maximum(b.highs - b.lows, np.maximum(np.abs(b.highs - prev), np.abs(b.lows - prev)))
    return wilder(tr, 14)


def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(closes, prepend=closes[0])
    gain, loss = wilder(np.maximum(delta, 0.0), period), wilder(np.maximum(-delta, 0.0), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100.0 - 100.0 / (1.0 + gain / loss)


def rolling_extreme(values: np.ndarray, n: int, fn) -> np.ndarray:
    """fn over the n bars before each index, excluding the bar itself."""
    out = np.full(len(values), np.nan)
    for i in range(n, len(values)):
        out[i] = fn(values[i - n:i])
    return out


def trend_breakout(lookback: int, fast: int = 50, slow: int = 200) -> Callable[[Bars], List[Hit]]:
    """Donchian breakout with the two moving averages agreeing on direction."""
    def signals(b: Bars) -> List[Hit]:
        a, f, s = atr(b), ema(b.closes, fast), ema(b.closes, slow)
        hi, lo = rolling_extreme(b.highs, lookback, np.max), rolling_extreme(b.lows, lookback, np.min)
        out: List[Hit] = []
        for i in range(slow, len(b.ts)):
            if b.closes[i] > hi[i] and f[i] > s[i]:
                out.append((i, "LONG", a[i]))
            elif b.closes[i] < lo[i] and f[i] < s[i]:
                out.append((i, "SHORT", a[i]))
        return out
    return signals


def mean_reversion(low: float, high: float, with_trend: bool) -> Callable[[Bars], List[Hit]]:
    """Buys oversold and sells overbought. Research only: it lost money out of sample."""
    def signals(b: Bars) -> List[Hit]:
        a, r, s = atr(b), rsi(b.closes), ema(b.closes, 200)
        out: List[Hit] = []
        for i in range(200, len(b.ts)):
            if r[i] < low and r[i - 1] >= low and (not with_trend or b.closes[i] > s[i]):
                out.append((i, "LONG", a[i]))
            elif r[i] > high and r[i - 1] <= high and (not with_trend or b.closes[i] < s[i]):
                out.append((i, "SHORT", a[i]))
        return out
    return signals


def funding_contrarian(high_rate: float, low_rate: float) -> Callable[[Bars], List[Hit]]:
    """Fades crowded positioning once price crosses the 20-bar average against the crowd."""
    def signals(b: Bars) -> List[Hit]:
        a, f = atr(b), ema(b.closes, 20)
        out: List[Hit] = []
        for i in range(60, len(b.ts)):
            k = np.searchsorted(b.funding_ts, b.ts[i] + b.bar_ms, side="right") - 1
            if k < 0:
                continue
            rate = b.funding_rate[k]
            if rate >= high_rate and b.closes[i] < f[i] and b.closes[i - 1] >= f[i - 1]:
                out.append((i, "SHORT", a[i]))
            elif rate <= low_rate and b.closes[i] > f[i] and b.closes[i - 1] <= f[i - 1]:
                out.append((i, "LONG", a[i]))
        return out
    return signals


def signal_15m(min_score: int = 60) -> Callable[[Bars], List[Hit]]:
    """The project's original 15-minute signal, evaluated on the newest bar only."""
    def signals(b: Bars) -> List[Hit]:
        i = len(b.ts) - 1
        if i < 0:
            return []
        s = evaluate(compute_features(b.highs, b.lows, b.closes, b.volumes), i, min_score=min_score)
        return [(i, s.direction, s.raw_stop_distance)] if s else []
    return signals


@dataclass(frozen=True)
class LiveStrategy:
    name: str
    label: str
    interval: str
    hours: float
    bars_needed: int
    make: Callable[[Bars], List[Hit]]
    exits: ExitRule
    desk_candidate: bool  # False keeps a frequent signal from using up the desk's API budget


LIVE_STRATEGIES = (
    LiveStrategy("trend_4h", "4 saatlik trend", "4h", 4, 500, trend_breakout(20), TREND_4H_EXITS, True),
    LiveStrategy("funding_1h", "Aşırı fonlama", "1h", 1, 120,
                 funding_contrarian(0.0005, -0.0002), FUNDING_EXITS, True),
    LiveStrategy("trend_1h", "1 saatlik trend", "1h", 1, 500, trend_breakout(72), TREND_1H_EXITS, True),
    LiveStrategy("signal_15m", "15 dakikalık sinyal", "15m", 0.25, 200, signal_15m(), SIGNAL_15M_EXITS, False),
)
# Fixed order: the panel assigns chart colors by this position, so a color always means one source.
SOURCE_LABELS = {**{s.name: s.label for s in LIVE_STRATEGIES}, "news": "Haber", "desk": "Yapay zeka masası"}
