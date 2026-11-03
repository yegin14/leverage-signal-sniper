"""
LeverageSignalSniper - TradingView Scanner Client
TradingView'in 26 teknik göstergeden (RSI, Stoch, MACD, ADX, Ichimoku, EMA'lar)
oluşan resmi teknik analiz konsensüsünü saniyenin altında çeker.
"""

import time
import aiohttp

TV_SCANNER_URL = "https://scanner.tradingview.com/crypto/scan"

class TradingViewClient:
    def __init__(self):
        self.cache: dict[str, dict] = {}
        self.last_batch_time: float = 0
        self.cache_ttl: float = 45.0  # 45 saniyede bir güncellenir

    async def fetch_batch_ratings(self, session: aiohttp.ClientSession, symbols: list[str]) -> dict[str, dict]:
        """Tüm paritelerin TradingView puanlarını TEK BİR istekte paralel çeker"""
        now = time.time()
        if (now - self.last_batch_time) < self.cache_ttl and self.cache:
            return self.cache

        # Binance sembollerini TradingView formatına çevir: BTCUSDT -> BINANCE:BTCUSDT.P
        tv_tickers = [f"BINANCE:{s}.P" for s in symbols]
        
        payload = {
            "filter": [{"left": "exchange", "operation": "equal", "right": "BINANCE"}],
            "options": {"lang": "en"},
            "symbols": {"query": {"types": []}, "tickers": tv_tickers},
            "columns": ["Recommend.All", "Recommend.MA", "Recommend.Other", "RSI", "close"]
        }
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json"
        }

        try:
            async with session.post(TV_SCANNER_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = {}
                    for item in data.get("data", []):
                        ticker_full = item.get("s", "")  # Örn: BINANCE:BTCUSDT.P
                        raw_sym = ticker_full.replace("BINANCE:", "").replace(".P", "")
                        d = item.get("d", [])
                        
                        rec_all = float(d[0]) if len(d) > 0 and d[0] is not None else 0.0
                        rec_ma = float(d[1]) if len(d) > 1 and d[1] is not None else 0.0
                        rsi_val = float(d[3]) if len(d) > 3 and d[3] is not None else 50.0

                        # Derecelendirme etiketi
                        if rec_all >= 0.5:
                            rating = "STRONG_BUY"
                            badge = "🟢 GÜÇLÜ AL"
                        elif rec_all >= 0.1:
                            rating = "BUY"
                            badge = "🟢 AL"
                        elif rec_all <= -0.5:
                            rating = "STRONG_SELL"
                            badge = "🔴 GÜÇLÜ SAT"
                        elif rec_all <= -0.1:
                            rating = "SELL"
                            badge = "🔴 SAT"
                        else:
                            rating = "NEUTRAL"
                            badge = "⚪ NÖTR"

                        results[raw_sym] = {
                            "rating": rating,
                            "badge": badge,
                            "score": rec_all,
                            "ma_score": rec_ma,
                            "rsi": rsi_val
                        }
                    
                    self.cache = results
                    self.last_batch_time = now
                    return results
        except Exception:
            pass
            
        return self.cache

    def get_symbol_rating(self, symbol: str) -> dict | None:
        return self.cache.get(symbol)
