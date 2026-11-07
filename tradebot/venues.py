"""Public market data from Binance, Bybit and Hyperliquid. No keys and no order code here.

A failed request raises DataError. Nothing substitutes a price silently, so callers can report
"no data" instead of showing a wrong number.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import aiohttp
import numpy as np

from .risk import Filters

BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_SPOT = "https://api.binance.com"
BYBIT = "https://api.bybit.com"
HYPERLIQUID = "https://api.hyperliquid.xyz/info"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LeverageSignalSniper/2.0)"}
HL_MIN_NOTIONAL = 10.0
BYBIT_INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240"}
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


class DataError(RuntimeError):
    def __init__(self, source: str, detail: str):
        super().__init__(f"{source}: {detail}")
        self.source = source
        self.detail = detail


@dataclass(frozen=True)
class Book:
    venue: str
    symbol: str
    bids: List[List[float]]
    asks: List[List[float]]
    ts: float

    @property
    def mid(self) -> float:
        return (self.bids[0][0] + self.asks[0][0]) / 2.0


@dataclass(frozen=True)
class Candles:
    ts: np.ndarray  # open time, ms
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    volumes: np.ndarray

    def closed(self, now_ms: float, interval_ms: int) -> "Candles":
        """Drops the bar that is still forming."""
        keep = self.ts + interval_ms <= now_ms
        return Candles(self.ts[keep], self.opens[keep], self.highs[keep], self.lows[keep],
                       self.closes[keep], self.volumes[keep])


@dataclass(frozen=True)
class Funding:
    rate: float
    next_ts: float  # seconds


def coin(symbol: str) -> str:
    return symbol.removesuffix("USDT")


def _levels(raw) -> List[List[float]]:
    return [[float(p), float(q)] for p, q, *_ in raw]


class Market:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._filters: Dict[str, Filters] = {}
        self._hl_ctx: tuple = (0.0, None)
        self.errors: Dict[str, str] = {}

    # ---------------------------------------------------------------- transport

    async def _get(self, source: str, url: str, params: Optional[dict] = None, timeout: float = 8.0):
        try:
            async with self.session.get(url, params=params, headers=HEADERS,
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status != 200:
                    raise DataError(source, f"HTTP {r.status}")
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            error = DataError(source, type(e).__name__)
            self.errors[source] = error.detail
            raise error from e
        except DataError as e:
            self.errors[source] = e.detail
            raise
        self.errors.pop(source, None)
        return data

    async def _hl(self, body: dict, timeout: float = 8.0):
        source = "hyperliquid"
        try:
            async with self.session.post(HYPERLIQUID, json=body, headers=HEADERS,
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status != 200:
                    raise DataError(source, f"HTTP {r.status}")
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            error = DataError(source, type(e).__name__)
            self.errors[source] = error.detail
            raise error from e
        except DataError as e:
            self.errors[source] = e.detail
            raise
        self.errors.pop(source, None)
        return data

    @staticmethod
    def _bybit_result(data: dict) -> dict:
        if data.get("retCode") != 0:
            raise DataError("bybit", f"retCode {data.get('retCode')}")
        return data["result"]

    # ---------------------------------------------------------------- order books

    async def book(self, venue: str, symbol: str, depth: int = 20) -> Book:
        if venue == "binance":
            d = await self._get("binance", f"{BINANCE_FAPI}/fapi/v1/depth", {"symbol": symbol, "limit": depth})
            bids, asks = _levels(d["bids"]), _levels(d["asks"])
        elif venue == "bybit":
            d = self._bybit_result(await self._get(
                "bybit", f"{BYBIT}/v5/market/orderbook",
                {"category": "linear", "symbol": symbol, "limit": min(depth, 50)}))
            bids, asks = _levels(d["b"]), _levels(d["a"])
        elif venue == "hyperliquid":
            d = await self._hl({"type": "l2Book", "coin": coin(symbol)})
            bids = [[float(x["px"]), float(x["sz"])] for x in d["levels"][0][:depth]]
            asks = [[float(x["px"]), float(x["sz"])] for x in d["levels"][1][:depth]]
        else:
            raise ValueError(f"unknown venue {venue}")
        if not bids or not asks:
            raise DataError(venue, f"{symbol}: boş tahta")
        return Book(venue, symbol, bids, asks, time.time())

    async def spot_book(self, venue: str, symbol: str, depth: int = 20) -> Book:
        if venue == "binance":
            d = await self._get("binance", f"{BINANCE_SPOT}/api/v3/depth", {"symbol": symbol, "limit": depth})
            bids, asks = _levels(d["bids"]), _levels(d["asks"])
        elif venue == "bybit":
            d = self._bybit_result(await self._get(
                "bybit", f"{BYBIT}/v5/market/orderbook",
                {"category": "spot", "symbol": symbol, "limit": min(depth, 50)}))
            bids, asks = _levels(d["b"]), _levels(d["a"])
        else:
            raise ValueError(f"unknown spot venue {venue}")
        if not bids or not asks:
            raise DataError(venue, f"{symbol}: boş tahta")
        return Book(venue, symbol, bids, asks, time.time())

    # ---------------------------------------------------------------- candles

    async def candles(self, venue: str, symbol: str, interval: str, limit: int = 200) -> Candles:
        if venue == "binance":
            raw = await self._get("binance", f"{BINANCE_FAPI}/fapi/v1/klines",
                                  {"symbol": symbol, "interval": interval, "limit": limit})
            rows = [(k[0], k[1], k[2], k[3], k[4], k[5]) for k in raw]
        elif venue == "bybit":
            d = self._bybit_result(await self._get(
                "bybit", f"{BYBIT}/v5/market/kline",
                {"category": "linear", "symbol": symbol, "interval": BYBIT_INTERVAL[interval], "limit": limit}))
            rows = [(k[0], k[1], k[2], k[3], k[4], k[5]) for k in reversed(d["list"])]
        elif venue == "hyperliquid":
            end = int(time.time() * 1000)
            raw = await self._hl({"type": "candleSnapshot", "req": {
                "coin": coin(symbol), "interval": interval,
                "startTime": end - limit * INTERVAL_MS[interval], "endTime": end}})
            rows = [(k["t"], k["o"], k["h"], k["l"], k["c"], k["v"]) for k in raw]
        else:
            raise ValueError(f"unknown venue {venue}")
        if not rows:
            raise DataError(venue, f"{symbol}: mum yok")
        a = np.array(rows, dtype=np.float64)
        return Candles(a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5])

    # ---------------------------------------------------------------- funding

    async def funding(self, venue: str, symbol: str) -> Funding:
        if venue == "binance":
            d = await self._get("binance", f"{BINANCE_FAPI}/fapi/v1/premiumIndex", {"symbol": symbol})
            return Funding(float(d["lastFundingRate"]), float(d["nextFundingTime"]) / 1000.0)
        if venue == "bybit":
            d = self._bybit_result(await self._get(
                "bybit", f"{BYBIT}/v5/market/tickers", {"category": "linear", "symbol": symbol}))
            t = d["list"][0]
            return Funding(float(t["fundingRate"]), float(t["nextFundingTime"]) / 1000.0)
        if venue == "hyperliquid":
            ctx = await self._hl_contexts()
            # Hyperliquid quotes and settles funding every hour on the hour.
            return Funding(float(ctx[coin(symbol)]["funding"]), (time.time() // 3600 + 1) * 3600)
        raise ValueError(f"unknown venue {venue}")

    async def funding_history(self, venue: str, symbol: str, limit: int = 5) -> np.ndarray:
        """Rows of (time_ms, rate), oldest first."""
        if venue == "binance":
            rows = await self._get("binance", f"{BINANCE_FAPI}/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})
            data = [[float(x["fundingTime"]), float(x["fundingRate"])] for x in rows]
        elif venue == "bybit":
            d = self._bybit_result(await self._get(
                "bybit", f"{BYBIT}/v5/market/funding/history", {"category": "linear", "symbol": symbol, "limit": limit}))
            data = [[float(x["fundingRateTimestamp"]), float(x["fundingRate"])] for x in d["list"]]
        else:
            raise ValueError(f"no funding history for {venue}")
        return np.array(sorted(data), dtype=np.float64)

    async def _hl_contexts(self) -> Dict[str, dict]:
        at, cached = self._hl_ctx
        if cached is not None and time.time() - at < 30.0:
            return cached
        meta, ctxs = await self._hl({"type": "metaAndAssetCtxs"})
        cached = {u["name"]: {**c, "szDecimals": u["szDecimals"], "maxLeverage": u["maxLeverage"]}
                  for u, c in zip(meta["universe"], ctxs)}
        self._hl_ctx = (time.time(), cached)
        return cached

    # ---------------------------------------------------------------- exchange filters

    async def filters(self, venue: str, symbol: str) -> Filters:
        key = f"{venue}:{symbol}"
        if key in self._filters:
            return self._filters[key]
        if venue == "binance":
            info = await self._get("binance", f"{BINANCE_FAPI}/fapi/v1/exchangeInfo", timeout=15.0)
            for s in info["symbols"]:
                f = {x["filterType"]: x for x in s["filters"]}
                self._filters[f"binance:{s['symbol']}"] = Filters(
                    float(f["MIN_NOTIONAL"]["notional"]), float(f["MARKET_LOT_SIZE"]["stepSize"]),
                    float(f["PRICE_FILTER"]["tickSize"]))
        elif venue == "bybit":
            d = self._bybit_result(await self._get(
                "bybit", f"{BYBIT}/v5/market/instruments-info", {"category": "linear", "symbol": symbol}))
            s = d["list"][0]
            self._filters[key] = Filters(
                float(s["lotSizeFilter"].get("minNotionalValue", 5)), float(s["lotSizeFilter"]["qtyStep"]),
                float(s["priceFilter"]["tickSize"]))
        elif venue == "hyperliquid":
            ctx = await self._hl_contexts()
            self._filters[key] = Filters(HL_MIN_NOTIONAL, 10.0 ** -int(ctx[coin(symbol)]["szDecimals"]))
        if key not in self._filters:
            raise DataError(venue, f"{symbol}: parite bulunamadı")
        return self._filters[key]

    # ---------------------------------------------------------------- derivatives positioning

    async def flow(self, symbol: str) -> dict:
        """Positioning data for the desk. Each field is omitted when its source failed."""
        out: dict = {}

        async def binance(path: str, **params):
            return await self._get("binance", f"{BINANCE_FAPI}{path}",
                                   {"symbol": symbol, "period": "15m", **params})

        results = await asyncio.gather(
            binance("/futures/data/openInterestHist", limit=8),
            binance("/futures/data/topLongShortPositionRatio", limit=1),
            binance("/futures/data/takerlongshortRatio", limit=4),
            self.funding("binance", symbol),
            self.funding("hyperliquid", symbol),
            return_exceptions=True,
        )
        oi, top, taker, fund_binance, fund_hl = results
        if not isinstance(oi, Exception) and len(oi) >= 2:
            first, last = float(oi[0]["sumOpenInterest"]), float(oi[-1]["sumOpenInterest"])
            out["open_interest_change_2h_pct"] = round((last / first - 1.0) * 100.0, 3) if first else None
            out["open_interest_usd"] = round(float(oi[-1]["sumOpenInterestValue"]))
        if not isinstance(top, Exception) and top:
            out["top_trader_long_pct"] = round(float(top[0]["longAccount"]) * 100.0, 1)
        if not isinstance(taker, Exception) and taker:
            out["taker_buy_sell_ratio_1h"] = round(
                sum(float(x["buySellRatio"]) for x in taker) / len(taker), 3)
        if not isinstance(fund_binance, Exception):
            out["binance_funding_8h_pct"] = round(fund_binance.rate * 100.0, 5)
        if not isinstance(fund_hl, Exception):
            out["hyperliquid_funding_1h_pct"] = round(fund_hl.rate * 100.0, 5)
        return out

    async def fear_greed(self) -> Optional[dict]:
        try:
            d = await self._get("alternative.me", "https://api.alternative.me/fng/", {"limit": 1})
            item = d["data"][0]
            return {"value": int(item["value"]), "label": item["value_classification"]}
        except (DataError, KeyError, IndexError, ValueError):
            return None

    async def pick_cex(self, preferred: str = "auto") -> Optional[str]:
        """First CEX reachable from this host. Render regions can be geo-blocked."""
        order = ["binance", "bybit"] if preferred == "auto" else [preferred]
        for venue in order:
            try:
                await self.book(venue, "BTCUSDT", depth=5)
                return venue
            except DataError:
                continue
        return None
