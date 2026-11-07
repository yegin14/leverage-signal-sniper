"""Deterministic technical signal, shared by the backtest and the live scanner.

The logic follows the project's original 15m trend and pullback idea, without inputs that have
no history (TradingView rating, top-trader ratio), so that it can be tested on past data.
evaluate() reads only bars up to index i, and entries happen after bar i closes.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

MIN_BARS = 60


@dataclass(frozen=True)
class Signal:
    direction: str
    score: int
    raw_stop_distance: float
    atr: float
    close: float
    reasons: tuple = field(default_factory=tuple)


def ema(values: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1.0)
    out = np.empty_like(values, dtype=np.float64)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def wilder(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return out
    out[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def compute_features(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray) -> Dict[str, np.ndarray]:
    prev = np.roll(closes, 1)
    prev[0] = closes[0]
    tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev), np.abs(lows - prev)))
    delta = np.diff(closes, prepend=closes[0])
    avg_gain = wilder(np.maximum(delta, 0.0), 14)
    avg_loss = wilder(np.maximum(-delta, 0.0), 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = np.where(avg_loss == 0, 100.0, rsi)

    ema12, ema26 = ema(closes, 12), ema(closes, 26)
    macd = ema12 - ema26
    hist = macd - ema(macd, 9)

    volume_cum = np.concatenate(([0.0], np.cumsum(volumes)))
    high_20 = np.full(len(closes), np.nan)
    low_20 = np.full(len(closes), np.nan)
    avg_volume = np.full(len(closes), np.nan)
    for i in range(20, len(closes)):
        high_20[i] = highs[i - 20:i].max()
        low_20[i] = lows[i - 20:i].min()
        avg_volume[i] = (volume_cum[i] - volume_cum[i - 20]) / 20.0
    with np.errstate(divide="ignore", invalid="ignore"):
        rvol = volumes / avg_volume

    return {
        "highs": highs, "lows": lows, "closes": closes,
        "ema20": ema(closes, 20), "ema50": ema(closes, 50),
        "atr": wilder(tr, 14), "rsi": rsi, "macd_hist": hist,
        "high_20": high_20, "low_20": low_20, "rvol": rvol,
    }


def evaluate(f: Dict[str, np.ndarray], i: int, min_score: int = 60,
             min_rvol: float = 1.15, atr_mult: float = 1.8) -> Optional[Signal]:
    """Signal for the closed bar i, or None. A score above min_score is required."""
    if i < MIN_BARS:
        return None
    close, atr, rsi, rvol = f["closes"][i], f["atr"][i], f["rsi"][i], f["rvol"][i]
    if not np.isfinite(atr) or not np.isfinite(rsi) or not np.isfinite(rvol):
        return None
    e20, e50 = f["ema20"][i], f["ema50"][i]
    highs, lows, closes, hist = f["highs"], f["lows"], f["closes"], f["macd_hist"]

    score = 0
    reasons: List[str] = []
    if e20 > e50 and close >= e50:
        direction = "LONG"
        if rsi > 72.0:
            return None
        score += 25
        reasons.append("Trend yukarı (EMA20 > EMA50)")
        pulled_back = lows[i] <= e20 * 1.006 or lows[i - 1] <= f["ema20"][i - 1] * 1.006
        turning = close >= e20 and (close >= closes[i - 1] or hist[i] > hist[i - 1])
        breakout = close > f["high_20"][i]
        momentum = hist[i] > 0
        healthy_rsi = 42.0 <= rsi <= 68.0
        raw_stop = max(close - lows[i - 4:i + 1].min(), atr * atr_mult)
    elif e20 < e50 and close <= e50:
        direction = "SHORT"
        if rsi < 28.0:
            return None
        score += 25
        reasons.append("Trend aşağı (EMA20 < EMA50)")
        pulled_back = highs[i] >= e20 * 0.994 or highs[i - 1] >= f["ema20"][i - 1] * 0.994
        turning = close <= e20 and (close <= closes[i - 1] or hist[i] < hist[i - 1])
        breakout = close < f["low_20"][i]
        momentum = hist[i] < 0
        healthy_rsi = 32.0 <= rsi <= 58.0
        raw_stop = max(highs[i - 4:i + 1].max() - close, atr * atr_mult)
    else:
        return None

    if rvol >= min_rvol:
        score += 25
        reasons.append(f"Hacim güçlü (RVOL {rvol:.2f})")
    elif rvol >= 1.05:
        score += 15
        reasons.append(f"Hacim ortalama üstü (RVOL {rvol:.2f})")

    if pulled_back and turning:
        score += 20
        reasons.append("EMA20'ye geri çekilip döndü")
    elif breakout and rvol >= 1.30:
        score += 15
        reasons.append("20 mumluk seviye hacimle kırıldı")
    elif momentum:
        score += 10
        reasons.append("MACD yönü destekliyor")

    if healthy_rsi:
        score += 10
        reasons.append(f"RSI dengeli ({rsi:.0f})")

    if score < min_score:
        return None
    return Signal(direction, score, float(raw_stop), float(atr), float(close), tuple(reasons))
