"""
LeverageSignalSniper - Indicator Math Engine
NumPy tabanlı, mikrosaniye (sub-millisecond) seviyesinde çalışan teknik hesaplama motoru.
Hiçbir hantal kütüphane bağımlılığı olmadan saf ve ultra hızlı çalışır.
"""

import numpy as np

def calc_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """Üstel Hareketli Ortalama (EMA) - Vektörize Mikrosaniye Hızı"""
    if len(prices) < period:
        return np.full_like(prices, np.nan)
    alpha = 2.0 / (period + 1.0)
    ema = np.empty_like(prices)
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = alpha * prices[i] + (1.0 - alpha) * ema[i - 1]
    return ema

def calc_rsi(prices: np.ndarray, period: int = 14) -> float:
    """Göreceli Güç Endeksi (RSI) - Son değer"""
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.maximum(deltas, 0)
    losses = np.maximum(-deltas, 0)
    
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))

def calc_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    """Ortalama Gerçek Aralık (ATR) - Dinamik Stop Seviyesi için"""
    if len(closes) < period + 1:
        return float(np.mean(highs - lows))
    
    tr1 = highs[1:] - lows[1:]
    tr2 = np.abs(highs[1:] - closes[:-1])
    tr3 = np.abs(lows[1:] - closes[:-1])
    
    true_ranges = np.maximum(tr1, np.maximum(tr2, tr3))
    atr = np.mean(true_ranges[-period:])
    return float(atr)

def calc_bollinger_bands(prices: np.ndarray, period: int = 20, num_std: float = 2.0):
    """Bollinger Bantları (Üst, Orta, Alt ve Bant Genişliği)"""
    if len(prices) < period:
        last = prices[-1]
        return last, last, last, 0.0
    
    recent = prices[-period:]
    sma = np.mean(recent)
    std = np.std(recent)
    
    upper = float(sma + (std * num_std))
    lower = float(sma - (std * num_std))
    bandwidth = float((upper - lower) / sma * 100.0) if sma > 0 else 0.0
    
    return upper, float(sma), lower, bandwidth

def calc_rvol(volumes: np.ndarray, period: int = 20) -> float:
    """Göreceli Hacim (RVOL - Relative Volume)
    Son hacmin geçmiş 20 mumluk ortalamaya oranı.
    RVOL > 2.0 = Ciddi balina / hacim patlaması demektir.
    """
    if len(volumes) < period + 1:
        return 1.0
    avg_vol = np.mean(volumes[-period-1:-1])
    if avg_vol <= 0:
        return 1.0
    current_vol = volumes[-1]
    return float(current_vol / avg_vol)

def calc_macd_histogram(prices: np.ndarray) -> tuple[float, float]:
    """MACD Hattı ve Histogram Değeri"""
    if len(prices) < 35:
        return 0.0, 0.0
    ema12 = calc_ema(prices, 12)
    ema26 = calc_ema(prices, 26)
    macd_line = ema12 - ema26
    signal_line = calc_ema(macd_line[25:], 9)
    hist = macd_line[-1] - signal_line[-1]
    return float(macd_line[-1]), float(hist)

def detect_donchian_breakout(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 20) -> str:
    """Donchian Kanal Kırılımı (20 periyotluk en yüksek/en düşük seviye testi)
    Dönüş: 'BREAKOUT_UP', 'BREAKOUT_DOWN' veya 'NONE'
    """
    if len(closes) < period + 2:
        return "NONE"
    
    prev_highest = np.max(highs[-period-1:-1])
    prev_lowest = np.min(lows[-period-1:-1])
    current_close = closes[-1]
    
    if current_close > prev_highest:
        return "BREAKOUT_UP"
    elif current_close < prev_lowest:
        return "BREAKOUT_DOWN"
    return "NONE"
