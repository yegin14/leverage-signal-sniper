"""
LeverageSignalSniper - Resilient Multi-Source Market Data Engine
Binance Futures & Bybit Linear Futures Yüksek Hızlı Asenkron Veri Motoru.
Bulut IP engellemelerine (HTTP 403 / 418) karşı otomatik Bybit failover korumalı.
15m/5m mumlar, Açık Pozisyon (OI), Fonlama ve Elit Balina oranlarını milisaniyede çeker.
"""

import asyncio
import time
import aiohttp
import numpy as np
from config import DEFAULT_SYMBOLS

BINANCE_FUTURES_BASE = "https://fapi.binance.com"
BYBIT_BASE = "https://api.bybit.com"

class MarketDataEngine:
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        self.last_status = 200
        self.use_bybit = False  # Binance 403/418 verirse otomatik Bybit'e geçer
        self.cached_tickers: dict[str, dict] = {}
        self.last_tickers_update: float = 0.0

    async def update_cached_tickers(self, session: aiohttp.ClientSession):
        """Tüm paritelerin fonlama, açık pozisyon ve 24s hacim verilerini tek çağrıda önbelleğe alır"""
        now = time.time()
        if now - self.last_tickers_update < 15.0 and self.cached_tickers:
            return

        url = f"{BYBIT_BASE}/v5/market/tickers?category=linear"
        try:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tickers_list = data.get("result", {}).get("list", [])
                    self.cached_tickers = {t["symbol"]: t for t in tickers_list if t.get("symbol")}
                    self.last_tickers_update = now
        except Exception:
            pass

    async def get_top_volume_symbols(self, session: aiohttp.ClientSession, limit: int = 50) -> list[str]:
        """50 Premier Kripto Paritesini 24 saatlik hacimlerine göre sıralar (Saf Kripto Teyitli)"""
        await self.update_cached_tickers(session)
        if self.cached_tickers:
            # Sadece onaylı kripto paritelerini hacimlerine göre sırala
            valid_crypto = [s for s in DEFAULT_SYMBOLS if s in self.cached_tickers]
            valid_crypto.sort(key=lambda s: float(self.cached_tickers[s].get("turnover24h", 0)), reverse=True)
            missing = [s for s in DEFAULT_SYMBOLS if s not in valid_crypto]
            return (valid_crypto + missing)[:limit]

        return DEFAULT_SYMBOLS[:limit]

    async def fetch_klines(self, session: aiohttp.ClientSession, symbol: str, interval: str = "5m", limit: int = 50) -> dict | None:
        """Belirtilen periyotta mum verilerini çeker ve NumPy dizilerine çevirir (Binance & Bybit Failover)"""
        # 1. Binance'i dene (eğer Bybit moduna geçilmediyse)
        if not self.use_bybit:
            url = f"{BINANCE_FUTURES_BASE}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
            try:
                async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    self.last_status = resp.status
                    if resp.status == 200:
                        raw = await resp.json()
                        if raw and len(raw) >= 25:
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
                    elif resp.status in (403, 418, 429):
                        self.use_bybit = True
            except Exception:
                self.use_bybit = True

        # 2. Bybit Failover (Bulut IP kısıtlamalarına karşı kesintisiz alternatif)
        bybit_int = "5" if interval == "5m" else ("15" if interval == "15m" else "60")
        url_bybit = f"{BYBIT_BASE}/v5/market/kline?category=linear&symbol={symbol}&interval={bybit_int}&limit={limit}"
        try:
            async with session.get(url_bybit, headers=self.headers, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                self.last_status = resp.status
                if resp.status == 200:
                    data = await resp.json()
                    raw = data.get("result", {}).get("list", [])
                    if raw and len(raw) >= 25:
                        raw.reverse()  # Bybit azalan sırada verir, eskiye göre sırala
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
            pass
        return None

    async def fetch_funding_rate(self, session: aiohttp.ClientSession, symbol: str) -> float:
        """Anlık fonlama oranını çeker (Önbellekten veya API'den)"""
        if symbol in self.cached_tickers:
            try:
                return float(self.cached_tickers[symbol].get("fundingRate", 0.0)) * 100.0
            except Exception:
                pass

        if not self.use_bybit:
            url = f"{BINANCE_FUTURES_BASE}/fapi/v1/premiumIndex?symbol={symbol}"
            try:
                async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data.get("lastFundingRate", 0.0)) * 100.0
            except Exception:
                pass
        return 0.0

    async def fetch_open_interest(self, session: aiohttp.ClientSession, symbol: str) -> float:
        """Açık Pozisyon (OI - Open Interest) miktarını çeker"""
        if symbol in self.cached_tickers:
            try:
                return float(self.cached_tickers[symbol].get("openInterest", 0.0))
            except Exception:
                pass

        if not self.use_bybit:
            url = f"{BINANCE_FUTURES_BASE}/fapi/v1/openInterest?symbol={symbol}"
            try:
                async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data.get("openInterest", 0.0))
            except Exception:
                pass
        return 0.0

    async def fetch_top_trader_ratio(self, session: aiohttp.ClientSession, symbol: str) -> dict:
        """Elit balina hesaplarının Long/Short oranını çeker (Binance / Bybit Failover)"""
        # Bybit Balina Duygusu
        if self.use_bybit:
            url_bybit = f"{BYBIT_BASE}/v5/market/account-ratio?category=linear&symbol={symbol}&period=5min&limit=1"
            try:
                async with session.get(url_bybit, headers=self.headers, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("result", {}).get("list", [])
                        if items:
                            buy_r = float(items[0].get("buyRatio", 0.5))
                            sell_r = float(items[0].get("sellRatio", 0.5))
                            return {
                                "long_pct": buy_r * 100.0,
                                "short_pct": sell_r * 100.0,
                                "ratio": (buy_r / sell_r) if sell_r > 0 else 1.0
                            }
            except Exception:
                pass

        # Binance Balina Duygusu
        url = f"{BINANCE_FUTURES_BASE}/futures/data/topLongShortAccountRatio?symbol={symbol}&period=5m&limit=1"
        try:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=2)) as resp:
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

        # Alternatif Bybit denemesi
        url_bybit = f"{BYBIT_BASE}/v5/market/account-ratio?category=linear&symbol={symbol}&period=5min&limit=1"
        try:
            async with session.get(url_bybit, headers=self.headers, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("result", {}).get("list", [])
                    if items:
                        buy_r = float(items[0].get("buyRatio", 0.5))
                        sell_r = float(items[0].get("sellRatio", 0.5))
                        return {
                            "long_pct": buy_r * 100.0,
                            "short_pct": sell_r * 100.0,
                            "ratio": (buy_r / sell_r) if sell_r > 0 else 1.0
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
