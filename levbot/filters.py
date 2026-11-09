"""
Extra entry filters against false breakouts, declared 2026-11-09 before testing (levbot.filter_study):

  btc_regime      the market leader agrees: BTC above its 50-day EMA for longs, below for shorts
  adx20 / adx25   the trend is strong: ADX(14) at least 20 / 25
  strength        a clean break: close beyond the breakout level by 0.25 to 2 ATR (not a poke, not an exhaustion spike)
  squeeze         the break comes out of a quiet range: ATR of the 10 bars before is in the bottom 40% of the last 200
  rel_strength    the coin is stronger (weaker) than BTC over the last 7 days for longs (shorts)
  retest          the next bar also closes beyond the breakout level; entry one bar later

Each takes (bars, i, direction, ctx) and reads only bars up to i (retest reads i+1 and moves the entry there).
"""

from typing import Callable, Dict, List, Optional

import numpy as np

from tradebot.strategies import Bars, atr, ema, rolling_extreme, wilder

DAY_MS = 86_400_000


def adx(b: Bars, period: int = 14) -> np.ndarray:
    up = np.diff(b.highs, prepend=b.highs[0])
    down = -np.diff(b.lows, prepend=b.lows[0])
    plus = np.where((up > down) & (up > 0), up, 0.0)
    minus = np.where((down > up) & (down > 0), down, 0.0)
    prev = np.roll(b.closes, 1)
    prev[0] = b.closes[0]
    tr = np.maximum(b.highs - b.lows, np.maximum(np.abs(b.highs - prev), np.abs(b.lows - prev)))
    atr_ = wilder(tr, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * wilder(plus, period) / atr_
        mdi = 100 * wilder(minus, period) / atr_
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    out = np.full(len(dx), np.nan)
    finite = np.flatnonzero(np.isfinite(dx))
    if len(finite) > period:
        s = finite[0]
        out[s:] = wilder(np.nan_to_num(dx[s:]), period)
    return out


class Context:
    """Per-symbol caches plus the BTC series of the same timeframe."""

    def __init__(self, btc: Optional[Bars] = None):
        self.btc = btc
        self.cache: Dict[tuple, np.ndarray] = {}

    def series(self, key: tuple, fn):
        if key not in self.cache:
            self.cache[key] = fn()
        return self.cache[key]

    def btc_at(self, ts: float) -> Optional[int]:
        if self.btc is None:
            return None
        j = int(np.searchsorted(self.btc.ts, ts, side="right") - 1)
        return j if j >= 0 else None


def btc_regime(b: Bars, i: int, d: str, ctx: Context) -> bool:
    j = ctx.btc_at(b.ts[i])
    if j is None:
        return False
    per_day = DAY_MS / ctx.btc.bar_ms
    e = ctx.series(("btc_ema", ctx.btc.bar_ms), lambda: ema(ctx.btc.closes, int(50 * per_day)))
    return ctx.btc.closes[j] > e[j] if d == "LONG" else ctx.btc.closes[j] < e[j]


def make_adx(level: float) -> Callable:
    def f(b: Bars, i: int, d: str, ctx: Context) -> bool:
        a = ctx.series(("adx", b.symbol, b.bar_ms), lambda: adx(b))
        return bool(np.isfinite(a[i]) and a[i] >= level)
    return f


def make_strength(lookback: int) -> Callable:
    def f(b: Bars, i: int, d: str, ctx: Context) -> bool:
        a = ctx.series(("atr", b.symbol, b.bar_ms), lambda: atr(b))
        if d == "LONG":
            level = ctx.series(("hi", b.symbol, b.bar_ms, lookback), lambda: rolling_extreme(b.highs, lookback, np.max))[i]
            beyond = (b.closes[i] - level) / a[i]
        else:
            level = ctx.series(("lo", b.symbol, b.bar_ms, lookback), lambda: rolling_extreme(b.lows, lookback, np.min))[i]
            beyond = (level - b.closes[i]) / a[i]
        return bool(np.isfinite(beyond) and 0.25 <= beyond <= 2.0)
    return f


def squeeze(b: Bars, i: int, d: str, ctx: Context) -> bool:
    ap = ctx.series(("atr_pct", b.symbol, b.bar_ms), lambda: atr(b) / b.closes)
    if i < 210:
        return False
    recent = np.nanmean(ap[i - 10:i])
    history = ap[i - 200:i]
    history = history[np.isfinite(history)]
    return bool(len(history) > 100 and recent <= np.percentile(history, 40))


def rel_strength(b: Bars, i: int, d: str, ctx: Context) -> bool:
    if b.symbol == "BTCUSDT":
        return True
    n = int(7 * DAY_MS / b.bar_ms)
    j = ctx.btc_at(b.ts[i])
    if j is None or i < n or j < n:
        return False
    mine = b.closes[i] / b.closes[i - n] - 1
    btc = ctx.btc.closes[j] / ctx.btc.closes[j - n] - 1
    return mine > btc if d == "LONG" else mine < btc


def make_retest(lookback: int) -> Callable:
    """Needs bar i+1: true when it also closes beyond the level broken at bar i."""
    def f(b: Bars, i: int, d: str, ctx: Context) -> bool:
        if i + 1 >= len(b.ts):
            return False
        if d == "LONG":
            level = ctx.series(("hi", b.symbol, b.bar_ms, lookback), lambda: rolling_extreme(b.highs, lookback, np.max))[i]
            return bool(b.closes[i + 1] > level)
        level = ctx.series(("lo", b.symbol, b.bar_ms, lookback), lambda: rolling_extreme(b.lows, lookback, np.min))[i]
        return bool(b.closes[i + 1] < level)
    return f


def catalogue(lookback: int) -> Dict[str, Callable]:
    return {"btc_regime": btc_regime, "adx20": make_adx(20), "adx25": make_adx(25), "strength": make_strength(lookback),
            "squeeze": squeeze, "rel_strength": rel_strength, "retest": make_retest(lookback)}


LABELS = {"btc_regime": "BTC aynı yönde", "adx20": "Trend gücü ADX ≥ 20", "adx25": "Trend gücü ADX ≥ 25",
          "strength": "Temiz kırılım (0,25–2 ATR)", "squeeze": "Sıkışmadan çıkış", "rel_strength": "BTC'ye göre güçlü/zayıf",
          "retest": "Sonraki mum da seviyenin ötesinde"}


def apply(hits: List[tuple], b: Bars, names: List[str], lookback: int, ctx: Context) -> List[tuple]:
    """Keeps hits that pass every named filter; retest moves the signal bar one bar later."""
    cat = catalogue(lookback)
    out = []
    for i, d, a in hits:
        if all(cat[n](b, i, d, ctx) for n in names):
            out.append((i + 1, d, a) if "retest" in names else (i, d, a))
    return out
