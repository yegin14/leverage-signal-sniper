"""
LeverageSignalSniper - Advanced Multi-Factor Strategy Engine
Dengeli ve Aktif Mod: Trend, RVOL (>1.2x), Kırılım, TradingView ve Balina Teyidi.
"""

import time
import numpy as np
from indicator_math import (
    calc_ema, calc_rsi, calc_atr, calc_bollinger_bands,
    calc_rvol, calc_macd_histogram, detect_donchian_breakout
)
from config import (
    ACCOUNT_SIZE_USD, MAX_RISK_PER_TRADE_USD,
    MAX_ALLOWED_LEVERAGE, MIN_ALLOWED_LEVERAGE,
    MIN_RVOL, MIN_CONFIDENCE_SCORE,
    ENABLE_TRADINGVIEW_FILTER, ENABLE_WHALE_SENTIMENT, ENABLE_NEWS_SHIELD
)

class StrategyEngine:
    def __init__(self):
        self.cooldown_tracker: dict[str, float] = {}

    def is_in_cooldown(self, symbol: str, cooldown_seconds: int = 1800) -> bool:
        last_time = self.cooldown_tracker.get(symbol, 0)
        return (time.time() - last_time) < cooldown_seconds

    def register_signal(self, symbol: str):
        self.cooldown_tracker[symbol] = time.time()

    def evaluate_bundle(self, bundle: dict, tv_data: dict | None = None, news_info: dict | None = None) -> dict | None:
        symbol = bundle["symbol"]
        k5m = bundle["k5m"]
        k15m = bundle["k15m"]
        funding = bundle["funding_rate"]
        whale = bundle.get("whale_sentiment", {})
        
        t0 = time.time()

        closes_5m = k5m["closes"]
        highs_5m = k5m["highs"]
        lows_5m = k5m["lows"]
        volumes_5m = k5m["volumes"]
        current_price = k5m["last_price"]

        closes_15m = k15m["closes"]

        # --- 1. TEKNİK FİYAT HAREKETİ VE TREND ---
        ema20_15m = calc_ema(closes_15m, 20)
        ema50_15m = calc_ema(closes_15m, 50)
        
        is_macro_bull = ema20_15m[-1] > ema50_15m[-1] and closes_15m[-1] > ema20_15m[-1]
        is_macro_bear = ema20_15m[-1] < ema50_15m[-1] and closes_15m[-1] < ema20_15m[-1]

        rvol = calc_rvol(volumes_5m, 20)
        rsi_5m = calc_rsi(closes_5m, 14)
        atr_5m = calc_atr(highs_5m, lows_5m, closes_5m, 14)
        bb_upper, bb_mid, bb_lower, _ = calc_bollinger_bands(closes_5m, 20, 2.0)
        donchian = detect_donchian_breakout(highs_5m, lows_5m, closes_5m, 20)
        _, macd_hist = calc_macd_histogram(closes_5m)

        signal_direction = None
        confidence = 0
        reasons = []

        tv_rating = tv_data.get("rating", "NEUTRAL") if tv_data else "NEUTRAL"
        tv_badge = tv_data.get("badge", "⚪ NÖTR") if tv_data else "⚪ NÖTR"

        # --- LONG DEĞERLENDİRMESİ ---
        if is_macro_bull:
            if ENABLE_NEWS_SHIELD and news_info and news_info.get("is_bearish"):
                return None

            # Sadece zıt yönde güçlü satış varsa engelle
            if ENABLE_TRADINGVIEW_FILTER and tv_rating == "STRONG_SELL":
                return None

            confidence += 25
            reasons.append("15D Makro Trend: Yükseliş Kanalı (EMA20 > EMA50)")

            if rvol >= MIN_RVOL:
                confidence += 25
                reasons.append(f"Hacim Teyidi: RVOL {rvol:.2f}x (Ortalama üzeri hacim)")

            if current_price >= bb_upper or donchian == "BREAKOUT_UP":
                confidence += 15
                reasons.append("Kırılım: Bollinger Üst Bant / Donchian Patlaması")
            elif closes_5m[-1] > bb_mid and macd_hist > 0:
                confidence += 10
                reasons.append("Momentum: Pozitif MACD İvmesi")

            if 45.0 <= rsi_5m <= 75.0:
                confidence += 10
                reasons.append(f"RSI Dengesi: {rsi_5m:.1f} (Sağlıklı Yükseliş)")

            # TradingView Puanı
            if tv_rating in ["STRONG_BUY", "BUY"]:
                confidence += 15
                reasons.append(f"TradingView: {tv_badge}")
            elif tv_rating == "NEUTRAL":
                confidence += 5

            # Balina Duygusu
            if ENABLE_WHALE_SENTIMENT and whale.get("long_pct", 50) >= 54.0:
                confidence += 10
                reasons.append(f"Balina Teyidi: %{whale['long_pct']:.1f} Long")

            if confidence >= MIN_CONFIDENCE_SCORE:
                signal_direction = "LONG"

        # --- SHORT DEĞERLENDİRMESİ ---
        elif is_macro_bear:
            if ENABLE_TRADINGVIEW_FILTER and tv_rating == "STRONG_BUY":
                return None

            confidence += 25
            reasons.append("15D Makro Trend: Düşüş Kanalı (EMA20 < EMA50)")

            if rvol >= MIN_RVOL:
                confidence += 25
                reasons.append(f"Satış Hacmi: RVOL {rvol:.2f}x (Satıcı Baskısı)")

            if current_price <= bb_lower or donchian == "BREAKOUT_DOWN":
                confidence += 15
                reasons.append("Kırılım: Bollinger Alt Bant / Donchian Tabanı")
            elif closes_5m[-1] < bb_mid and macd_hist < 0:
                confidence += 10
                reasons.append("Momentum: Negatif MACD İvmesi")

            if 25.0 <= rsi_5m <= 55.0:
                confidence += 10
                reasons.append(f"RSI Dengesi: {rsi_5m:.1f} (Satış Baskısı)")

            if tv_rating in ["STRONG_SELL", "SELL"]:
                confidence += 15
                reasons.append(f"TradingView: {tv_badge}")
            elif tv_rating == "NEUTRAL":
                confidence += 5

            if ENABLE_WHALE_SENTIMENT and whale.get("short_pct", 50) >= 54.0:
                confidence += 10
                reasons.append(f"Balina Teyidi: %{whale['short_pct']:.1f} Short")

            if confidence >= MIN_CONFIDENCE_SCORE:
                signal_direction = "SHORT"

        if not signal_direction:
            return None

        # --- 2. DİNAMİK STOP VE HEDEFLER (ATR) ---
        stop_buffer = atr_5m * 1.5
        if signal_direction == "LONG":
            stop_loss = current_price - stop_buffer
            risk_distance = current_price - stop_loss
            tp1 = current_price + (risk_distance * 1.5)
            tp2 = current_price + (risk_distance * 2.5)
            tp3 = current_price + (risk_distance * 4.0)
        else:
            stop_loss = current_price + stop_buffer
            risk_distance = stop_loss - current_price
            tp1 = current_price - (risk_distance * 1.5)
            tp2 = current_price - (risk_distance * 2.5)
            tp3 = current_price - (risk_distance * 4.0)

        sl_distance_pct = (risk_distance / current_price) * 100.0
        if sl_distance_pct <= 0.1:
            return None

        # --- 3. KASA VE DİNAMİK KALDIRAÇ ---
        target_position_usd = MAX_RISK_PER_TRADE_USD / (sl_distance_pct / 100.0)
        ideal_margin_usd = min(ACCOUNT_SIZE_USD * 0.15, 20.0)
        raw_leverage = target_position_usd / ideal_margin_usd
        recommended_leverage = int(np.clip(raw_leverage, MIN_ALLOWED_LEVERAGE, MAX_ALLOWED_LEVERAGE))
        actual_margin_needed = target_position_usd / recommended_leverage

        calc_latency_ms = (time.time() - t0) * 1000.0

        return {
            "symbol": symbol,
            "direction": signal_direction,
            "confidence": min(confidence, 99),
            "entry_price": current_price,
            "stop_loss": stop_loss,
            "sl_distance_pct": sl_distance_pct,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "recommended_leverage": recommended_leverage,
            "target_position_usd": target_position_usd,
            "margin_needed_usd": actual_margin_needed,
            "max_risk_usd": MAX_RISK_PER_TRADE_USD,
            "tp1_profit_usd": MAX_RISK_PER_TRADE_USD * 1.5,
            "tp2_profit_usd": MAX_RISK_PER_TRADE_USD * 2.5,
            "tp3_profit_usd": MAX_RISK_PER_TRADE_USD * 4.0,
            "reasons": reasons,
            "tv_badge": tv_badge,
            "whale_info": whale,
            "news_info": news_info,
            "rvol": rvol,
            "rsi": rsi_5m,
            "funding_rate": funding,
            "calc_latency_ms": calc_latency_ms,
            "timestamp": time.time()
        }
