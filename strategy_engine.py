"""
LeverageSignalSniper - 15m Institutional Multi-Factor Strategy Engine
15 Dakikalık Yüksek Kazanma Oranlı (High Win-Rate) Kurumsal Trend & Pullback Motoru.
15D Makro EMA20/50, 15D Yapısal Destek/Direnç, RVOL Hacim Teyidi, TradingView ve Balina Konsensüsü.
Stop-Loss: Minimum %1.25 - %3.00 Yapısal Tampon (Piyasa gürültüsüne karşı zırhlı).
Kâr Alma: TP1 (+1.5R) Vurulduğunda %100 Kapat & Kârı Kasaya Kilitle.
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
        k15m = bundle["k15m"]
        whale = bundle.get("whale_sentiment", {})
        
        t0 = time.time()

        closes_15m = k15m["closes"]
        highs_15m = k15m["highs"]
        lows_15m = k15m["lows"]
        volumes_15m = k15m["volumes"]
        current_price = closes_15m[-1]

        if len(closes_15m) < 30:
            return None

        # --- 1. 15 DAKİKALIK TEKNİK GÖSTERGELER (KURUMSAL STANDART) ---
        ema20_15m = calc_ema(closes_15m, 20)
        ema50_15m = calc_ema(closes_15m, 50)
        atr_15m = calc_atr(highs_15m, lows_15m, closes_15m, 14)
        rsi_15m = calc_rsi(closes_15m, 14)
        rvol_15m = calc_rvol(volumes_15m, 20)
        bb_upper, bb_mid, bb_lower, _ = calc_bollinger_bands(closes_15m, 20, 2.0)
        donchian = detect_donchian_breakout(highs_15m, lows_15m, closes_15m, 20)
        _, macd_hist = calc_macd_histogram(closes_15m)

        # Trend Filtresi (15m Makro Yapı)
        is_macro_bull = ema20_15m[-1] > ema50_15m[-1] and current_price >= ema50_15m[-1]
        is_macro_bear = ema20_15m[-1] < ema50_15m[-1] and current_price <= ema50_15m[-1]

        signal_direction = None
        confidence = 0
        reasons = []

        tv_rating = tv_data.get("rating", "NEUTRAL") if tv_data else "NEUTRAL"
        tv_badge = tv_data.get("badge", "⚪ NÖTR") if tv_data else "⚪ NÖTR"

        # --- LONG DEĞERLENDİRMESİ ---
        if is_macro_bull:
            # Sert Kalkanlar: Zıt TradingView veya Kötü Haber varsa ANINDA İPTAL
            if ENABLE_TRADINGVIEW_FILTER and tv_rating in ["SELL", "STRONG_SELL"]:
                return None
            if ENABLE_NEWS_SHIELD and news_info and news_info.get("is_bearish"):
                return None
            if ENABLE_WHALE_SENTIMENT and whale.get("long_pct", 50) < 46.0:
                return None

            # 1. 15D Trend Puanı
            confidence += 25
            reasons.append("15D Trend: Net Boğa Kanalı (EMA20 > EMA50)")

            # 2. 15D Hacim Teyidi (RVOL)
            if rvol_15m >= MIN_RVOL:
                confidence += 25
                reasons.append(f"15D Hacim İvmesi: RVOL {rvol_15m:.2f}x (Kurumsal Giriş)")
            elif rvol_15m >= 1.05:
                confidence += 15
                reasons.append(f"15D Hacim: RVOL {rvol_15m:.2f}x (Ortalama Üzeri)")

            # 3. Pullback / Giriş Kalitesi (Tepeden alımı engelle!)
            # EMA20'ye doğru sağlıklı geri çekilme ve oradan destek alma (En yüksek kazanma oranı!)
            has_pulled_back = (lows_15m[-1] <= ema20_15m[-1] * 1.006 or lows_15m[-2] <= ema20_15m[-2] * 1.006)
            is_bouncing = current_price >= ema20_15m[-1] and (closes_15m[-1] >= closes_15m[-2] or macd_hist[-1] > 0)

            if has_pulled_back and is_bouncing:
                confidence += 20
                reasons.append("Pullback Teyidi: 15D EMA20 Desteğinden Başarılı Dönüş")
            elif donchian == "BREAKOUT_UP" and rvol_15m >= 1.30:
                confidence += 15
                reasons.append("Yapısal Kırılım: 15D Donchian Zirvesi Hacimle Aşıldı")
            elif current_price > bb_mid and macd_hist[-1] > 0:
                confidence += 10
                reasons.append("Momentum: 15D MACD Pozitif Genişleme")

            # 4. RSI Sağlıklı Bölge (Aşırı alımda alımı engelle: RSI <= 68 olmalı)
            if 42.0 <= rsi_15m <= 68.0:
                confidence += 10
                reasons.append(f"15D RSI: {rsi_15m:.1f} (Dengeli Yükseliş Alanı)")
            elif rsi_15m > 72.0:
                # Aşırı şişmiş, tepe tuzağı riski!
                return None

            # 5. TradingView Konsensüsü
            if tv_rating in ["STRONG_BUY", "BUY"]:
                confidence += 15
                reasons.append(f"TradingView Konsensüsü: {tv_badge}")
            elif tv_rating == "NEUTRAL":
                confidence += 5

            # 6. Balina Oranı
            if ENABLE_WHALE_SENTIMENT and whale.get("long_pct", 50) >= 53.0:
                confidence += 10
                reasons.append(f"Elit Balina Teyidi: %{whale['long_pct']:.1f} Long")

            if confidence >= MIN_CONFIDENCE_SCORE:
                signal_direction = "LONG"

        # --- SHORT DEĞERLENDİRMESİ ---
        elif is_macro_bear:
            # Sert Kalkanlar: Zıt TradingView varsa ANINDA İPTAL
            if ENABLE_TRADINGVIEW_FILTER and tv_rating in ["BUY", "STRONG_BUY"]:
                return None
            if ENABLE_WHALE_SENTIMENT and whale.get("short_pct", 50) < 46.0:
                return None

            # 1. 15D Trend Puanı
            confidence += 25
            reasons.append("15D Trend: Net Ayı Kanalı (EMA20 < EMA50)")

            # 2. 15D Hacim Teyidi (RVOL)
            if rvol_15m >= MIN_RVOL:
                confidence += 25
                reasons.append(f"15D Satış Hacmi: RVOL {rvol_15m:.2f}x (Satıcı Baskısı)")
            elif rvol_15m >= 1.05:
                confidence += 15
                reasons.append(f"15D Hacim: RVOL {rvol_15m:.2f}x (Ortalama Üzeri)")

            # 3. Pullback / Giriş Kalitesi (Dipten satışı engelle!)
            has_pulled_back = (highs_15m[-1] >= ema20_15m[-1] * 0.994 or highs_15m[-2] >= ema20_15m[-2] * 0.994)
            is_rejecting = current_price <= ema20_15m[-1] and (closes_15m[-1] <= closes_15m[-2] or macd_hist[-1] < 0)

            if has_pulled_back and is_rejecting:
                confidence += 20
                reasons.append("Pullback Teyidi: 15D EMA20 Direncinden Net Ret")
            elif donchian == "BREAKOUT_DOWN" and rvol_15m >= 1.30:
                confidence += 15
                reasons.append("Yapısal Kırılım: 15D Donchian Tabanı Hacimle Kırıldı")
            elif current_price < bb_mid and macd_hist[-1] < 0:
                confidence += 10
                reasons.append("Momentum: 15D MACD Negatif Genişleme")

            # 4. RSI Sağlıklı Bölge (Aşırı satımda satış yapma: RSI >= 32 olmalı)
            if 32.0 <= rsi_15m <= 58.0:
                confidence += 10
                reasons.append(f"15D RSI: {rsi_15m:.1f} (Dengeli Düşüş Alanı)")
            elif rsi_15m < 28.0:
                # Aşırı satılmış, dip sıçraması riski!
                return None

            # 5. TradingView Konsensüsü
            if tv_rating in ["STRONG_SELL", "SELL"]:
                confidence += 15
                reasons.append(f"TradingView Konsensüsü: {tv_badge}")
            elif tv_rating == "NEUTRAL":
                confidence += 5

            # 6. Balina Oranı
            if ENABLE_WHALE_SENTIMENT and whale.get("short_pct", 50) >= 53.0:
                confidence += 10
                reasons.append(f"Elit Balina Teyidi: %{whale['short_pct']:.1f} Short")

            if confidence >= MIN_CONFIDENCE_SCORE:
                signal_direction = "SHORT"

        if not signal_direction:
            return None

        # --- 2. 15 DAKİKALIK YAPISAL VE GÜVENLİ STOP-LOSS (GÜRÜLTÜ KALKANI) ---
        min_stop_distance = current_price * 0.0125  # Minimum %1.25 nefes alma tamponu!
        max_stop_distance = current_price * 0.0300  # Maksimum %3.00 stop sınırı
        
        atr_buffer = atr_15m * 1.8

        if signal_direction == "LONG":
            swing_low = float(np.min(lows_15m[-5:]))
            raw_stop = min(swing_low, current_price - atr_buffer)
            raw_distance = current_price - raw_stop
            actual_distance = float(np.clip(raw_distance, min_stop_distance, max_stop_distance))
            stop_loss = current_price - actual_distance
            tp1 = current_price + (actual_distance * 1.5)  # +1.5R Kâr Hedefi (100% Kapat)
            tp2 = current_price + (actual_distance * 2.5)
            tp3 = current_price + (actual_distance * 4.0)
        else:
            swing_high = float(np.max(highs_15m[-5:]))
            raw_stop = max(swing_high, current_price + atr_buffer)
            raw_distance = raw_stop - current_price
            actual_distance = float(np.clip(raw_distance, min_stop_distance, max_stop_distance))
            stop_loss = current_price + actual_distance
            tp1 = current_price - (actual_distance * 1.5)  # +1.5R Kâr Hedefi (100% Kapat)
            tp2 = current_price - (actual_distance * 2.5)
            tp3 = current_price - (actual_distance * 4.0)

        sl_distance_pct = (actual_distance / current_price) * 100.0

        # --- 3. KASA YÖNETİMİ VE MATEMATİKSEL RİSK KALKANI (%2 SABİT RİSK) ---
        target_position_usd = MAX_RISK_PER_TRADE_USD / (sl_distance_pct / 100.0)
        
        # İdeal kaldıraç: Stop mesafesine göre dinamik (Örn: %1.5 stop -> 7x-10x)
        ideal_margin_usd = min(ACCOUNT_SIZE_USD * 0.20, 200.0)
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
            "tp1_profit_usd": MAX_RISK_PER_TRADE_USD * 1.5,  # +1.5R Net Kâr ($30.00)
            "tp2_profit_usd": MAX_RISK_PER_TRADE_USD * 2.5,
            "tp3_profit_usd": MAX_RISK_PER_TRADE_USD * 4.0,
            "reasons": reasons,
            "tv_badge": tv_badge,
            "whale_info": whale,
            "news_info": news_info,
            "calc_latency_ms": calc_latency_ms,
            "rvol": rvol_15m
        }
