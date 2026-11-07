"""Public market data from Binance, Bybit and Hyperliquid. No keys and no order code here.

A failed request raises DataError. Nothing substitutes a price silently, so callers can report
"no data" instead of showing a wrong number.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import aiohttp
import numpy as np

from .risk import Filters
from .venue_meta import FALLBACK_FILTERS

BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_SPOT = "https://api.binance.com"
BYBIT = "https://api.bybit.com"
HYPERLIQUID = "https://api.hyperliquid.xyz/info"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LeverageSignalSniper/2.0)"}
HL_MIN_NOTIONAL = 10.0
DEFAULT_BACKOFF_SECONDS = 60.0
# Bybit answers its IP limit with HTTP 200 and a code. Its window is a few seconds long.
BYBIT_RATE_LIMIT_CODES = (10006, 10018)
BYBIT_BACKOFF_SECONDS = 15.0
MAX_BACKOFF_SECONDS = 3600.0
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
        self.backoff_until: Dict[str, float] = {}
        self.feeds: Dict[str, object] = {}
        self.errors: Dict[str, str] = {}

    # ---------------------------------------------------------------- transport

    def _check_backoff(self, source: str) -> None:
        """A venue that said 418 or 429 gets no request until its wait is over. Asking again during
        a ban makes it longer, and Binance can extend it to days."""
        if time.time() < self.backoff_until.get(source, 0.0):
            raise DataError(source, f"{self.errors.get(source, 'sınır')}, bekleniyor")

    def _note_rate_limit(self, source: str, response) -> None:
        if response.status not in (418, 429):
            return
        try:
            wait = float(response.headers.get("Retry-After", DEFAULT_BACKOFF_SECONDS))
        except ValueError:
            wait = DEFAULT_BACKOFF_SECONDS
        self.backoff_until[source] = time.time() + min(max(wait, DEFAULT_BACKOFF_SECONDS), MAX_BACKOFF_SECONDS)

    async def _get(self, source: str, url: str, params: Optional[dict] = None, timeout: float = 8.0):
        self._check_backoff(source)
        try:
            async with self.session.get(url, params=params, headers=HEADERS,
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                self._note_rate_limit(source, r)
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
        self._check_backoff(source)
        try:
            async with self.session.post(HYPERLIQUID, json=body, headers=HEADERS,
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                self._note_rate_limit(source, r)
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

    def attach_feed(self, feed) -> None:
        self.feeds[feed.venue] = feed

    def _bybit_result(self, data: dict) -> dict:
        code = data.get("retCode")
        if code != 0:
            if code in BYBIT_RATE_LIMIT_CODES:
                self.backoff_until["bybit"] = time.time() + BYBIT_BACKOFF_SECONDS
            detail = f"retCode {code}"
            self.errors["bybit"] = detail
            raise DataError("bybit", detail)
        return data["result"]

    # ---------------------------------------------------------------- order books

    async def book(self, venue: str, symbol: str, depth: int = 20) -> Book:
        feed = self.feeds.get(venue)
        if feed:
            live = feed.book(symbol, depth)
            if live:
                return live
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

    async def tops(self, venue: str) -> Dict[str, Tuple[float, float]]:
        """Best bid and ask of every symbol in one request. Hyperliquid REST gives the mid for both."""
        feed = self.feeds.get(venue)
        if feed:
            live = feed.tops()
            if live:
                return live
        if venue == "binance":
            rows = await self._get("binance", f"{BINANCE_FAPI}/fapi/v1/ticker/bookTicker")
            return {r["symbol"]: (float(r["bidPrice"]), float(r["askPrice"])) for r in rows}
        if venue == "bybit":
            d = self._bybit_result(await self._get("bybit", f"{BYBIT}/v5/market/tickers", {"category": "linear"}))
            return {r["symbol"]: (float(r["bid1Price"]), float(r["ask1Price"]))
                    for r in d["list"] if r.get("bid1Price") and r.get("ask1Price")}
        if venue == "hyperliquid":
            mids = await self._hl({"type": "allMids"})
            return {f"{k}USDT": (float(v), float(v)) for k, v in mids.items() if not k.startswith("@")}
        raise ValueError(f"unknown venue {venue}")

    async def spot_book(self, venue: str, symbol: str, depth: int = 20) -> Book:
        if venue == "binance":
            # A separate source name: spot being reachable must not hide blocked futures.
            d = await self._get("binance spot", f"{BINANCE_SPOT}/api/v3/depth", {"symbol": symbol, "limit": depth})
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
        feed = self.feeds.get(venue)
        if feed and interval == "1m":
            live = feed.candles(symbol, limit)
            if live is not None:
                return live
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
        feed = self.feeds.get(venue)
        if feed:
            live = feed.funding(symbol)
            if live:
                return live
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
        if cached is not None and time.time() - at < 120.0:
            return cached
        try:
            meta, ctxs = await self._hl({"type": "metaAndAssetCtxs"})
        except DataError:
            if cached is not None:
                return cached  # Slightly old funding beats none.
            raise
        cached = {u["name"]: {**c, "szDecimals": u["szDecimals"], "maxLeverage": u["maxLeverage"]}
                  for u, c in zip(meta["universe"], ctxs)}
        self._hl_ctx = (time.time(), cached)
        return cached

    # ---------------------------------------------------------------- exchange filters

    async def filters(self, venue: str, symbol: str) -> Filters:
        key = f"{venue}:{symbol}"
        if key in self._filters:
            return self._filters[key]
        try:
            return await self._live_filters(venue, symbol, key)
        except DataError:
            row = FALLBACK_FILTERS.get(venue, {}).get(symbol)
            if row is None:
                raise
            return Filters(row[0], row[1], 0.0, row[2])

    async def _live_filters(self, venue: str, symbol: str, key: str) -> Filters:
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
            c = ctx[coin(symbol)]
            self._filters[key] = Filters(HL_MIN_NOTIONAL, 10.0 ** -int(c["szDecimals"]), 0.0, int(c["maxLeverage"]))
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
        if "open_interest_usd" not in out:
            out.update(await self._bybit_flow(symbol))
        return out

    async def _bybit_flow(self, symbol: str) -> dict:
        """Same questions asked of Bybit, for hosts where Binance futures data is blocked."""
        out: dict = {"flow_source": "bybit"}
        results = await asyncio.gather(
            self._get("bybit", f"{BYBIT}/v5/market/open-interest",
                      {"category": "linear", "symbol": symbol, "intervalTime": "15min", "limit": 8}),
            self._get("bybit", f"{BYBIT}/v5/market/account-ratio",
                      {"category": "linear", "symbol": symbol, "period": "15min", "limit": 1}),
            self.funding("bybit", symbol),
            return_exceptions=True,
        )
        oi, ratio, funding = results
        if not isinstance(oi, Exception) and oi.get("retCode") == 0:
            rows = oi["result"]["list"]
            if len(rows) >= 2:  # Newest first.
                last, first = float(rows[0]["openInterest"]), float(rows[-1]["openInterest"])
                out["open_interest_change_2h_pct"] = round((last / first - 1.0) * 100.0, 3) if first else None
        if not isinstance(ratio, Exception) and ratio.get("retCode") == 0 and ratio["result"]["list"]:
            out["accounts_long_pct"] = round(float(ratio["result"]["list"][0]["buyRatio"]) * 100.0, 1)
        if not isinstance(funding, Exception):
            out["bybit_funding_8h_pct"] = round(funding.rate * 100.0, 5)
        return out

    async def fear_greed(self) -> Optional[dict]:
        try:
            d = await self._get("alternative.me", "https://api.alternative.me/fng/", {"limit": 1})
            item = d["data"][0]
            return {"value": int(item["value"]), "label": item["value_classification"]}
        except (DataError, KeyError, IndexError, ValueError):
            return None

    async def pick_cex(self, preferred: str = "auto", attempts: int = 3, retry_seconds: float = 3.0) -> Optional[str]:
        """First CEX reachable from this host. One failed request at a cold start proves nothing."""
        order = ["binance", "bybit"] if preferred == "auto" else [preferred]
        for venue in order:
            for attempt in range(attempts):
                try:
                    await self.book(venue, "BTCUSDT", depth=5)
                    return venue
                except DataError:
                    if attempt + 1 < attempts:
                        await asyncio.sleep(retry_seconds)
        return None
