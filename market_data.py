"""
LeverageSignalSniper - Market Data Engine
Binance Futures USDT-M asenkron, paralel veri çekme ve derin piyasa mikro yapı modülü.
15m/5m mumlar, Açık Pozisyon (OI), Fonlama ve Elit Balina oranlarını paralel çeker.
"""

import asyncio
import time
import aiohttp
import numpy as np

BINANCE_FUTURES_BASE = "https://fapi.binance.com"

class MarketDataEngine:
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        self.last_status = 0  # Son Binance HTTP yanıt kodu (teşhis için)

    async def get_top_volume_symbols(self, session: aiohttp.ClientSession, limit: int = 50) -> list[str]:
        """En yüksek 24 saatlik işlem hacmine sahip vadeli pariteleri bulur"""
        url = f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/24hr"
        try:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    usdt_pairs = [x for x in data if x.get("symbol", "").endswith("USDT")]
                    usdt_pairs.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
                    top = [x["symbol"] for x in usdt_pairs[:limit]]
                    return top
        except Exception:
            pass
        return []

    async def fetch_klines(self, session: aiohttp.ClientSession, symbol: str, interval: str = "5m", limit: int = 50) -> dict | None:
        """Belirtilen periyotta mum verilerini çeker ve NumPy dizilerine çevirir"""
        url = f"{BINANCE_FUTURES_BASE}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
        try:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                self.last_status = resp.status
                if resp.status == 200:
                    raw = await resp.json()
                    if not raw or len(raw) < 25:
                        return None
                    
                    closes = np.array([float(k[4]) for k in raw], dtype=np.float64)
                    highs = np.array([float(k[2]) for k in raw], dtype=np.float64)
                    lows = np.array([float(k[3]) for k in raw], dtype=np.float64)
                    volumes = np.array([float(k[5]) for k in raw], dtype=np.float64)
                    
                    return {
                        "symbol": symbol,
                        "interval": interval,
                        "closes": closes,
                        "highs": highs,
                        "lows": lows,
                        "volumes": volumes,
                        "last_price": closes[-1]
                    }
        except Exception:
            return None

    async def fetch_funding_rate(self, session: aiohttp.ClientSession, symbol: str) -> float:
        """Anlık fonlama oranını çeker"""
        url = f"{BINANCE_FUTURES_BASE}/fapi/v1/premiumIndex?symbol={symbol}"
        try:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("lastFundingRate", 0.0)) * 100.0
        except Exception:
            pass
        return 0.0

    async def fetch_open_interest(self, session: aiohttp.ClientSession, symbol: str) -> float:
        """Açık Pozisyon (OI - Open Interest) miktarını çeker"""
        url = f"{BINANCE_FUTURES_BASE}/fapi/v1/openInterest?symbol={symbol}"
        try:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("openInterest", 0.0))
        except Exception:
            pass
        return 0.0

    async def fetch_top_trader_ratio(self, session: aiohttp.ClientSession, symbol: str) -> dict:
        """Binance elit balina hesaplarının Long/Short oranını çeker"""
        url = f"{BINANCE_FUTURES_BASE}/futures/data/topLongShortAccountRatio?symbol={symbol}&period=5m&limit=1"
        try:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and data:
                        first = data[0]
                        return {
                            "long_pct": float(first.get("longAccount", 0.5)) * 100.0,
                            "short_pct": float(first.get("shortAccount", 0.5)) * 100.0,
                            "ratio": float(first.get("longShortRatio", 1.0))
                        }
        except Exception:
            pass
        return {"long_pct": 50.0, "short_pct": 50.0, "ratio": 1.0}

    async def fetch_full_symbol_bundle(self, session: aiohttp.ClientSession, symbol: str) -> dict | None:
        """Tek bir sembol için tüm verileri mikro saniyede paralel çeker"""
        t0 = time.time()
        k5m_task = self.fetch_klines(session, symbol, "5m", 50)
        k15m_task = self.fetch_klines(session, symbol, "15m", 50)
        fund_task = self.fetch_funding_rate(session, symbol)
        oi_task = self.fetch_open_interest(session, symbol)
        whale_task = self.fetch_top_trader_ratio(session, symbol)
        
        k5m, k15m, fund, oi, whale = await asyncio.gather(
            k5m_task, k15m_task, fund_task, oi_task, whale_task
        )
        
        if not k5m or not k15m:
            return None
            
        latency_ms = (time.time() - t0) * 1000.0
        return {
            "symbol": symbol,
            "k5m": k5m,
            "k15m": k15m,
            "funding_rate": fund,
            "open_interest": oi,
            "whale_sentiment": whale,
            "latency_ms": latency_ms
        }
