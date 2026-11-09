"""Small Binance USD-M futures client: public data from the real market, orders on the trading host.

Requests are signed with an Ed25519 key whose private half never leaves the server (Binance keeps only the public
half). Stop orders go through the Algo Order API: since late 2025 /fapi/v1/order refuses STOP_MARKET with -4120.
"""

import base64
import hashlib
import hmac
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

import aiohttp

from . import config


class BinanceError(Exception):
    def __init__(self, status: int, code: Any, msg: str):
        super().__init__(f"HTTP {status} code {code}: {msg}")
        self.status, self.code, self.msg = status, code, msg


@dataclass
class SymbolRules:
    step: float
    min_qty: float
    tick: float
    min_notional: float


def _round_down(value: float, step: float) -> float:
    return float(int(value / step + 1e-9)) * step


def fmt(value: float, step: float) -> str:
    """The value written with exactly as many decimals as the exchange step allows."""
    decimals = max(0, -Decimal(str(step)).normalize().as_tuple().exponent)
    return f"{value:.{decimals}f}"


class Signer:
    def __init__(self, key_file: str = "", secret: str = ""):
        self.private = None
        self.secret = secret.encode() if secret else b""
        if key_file:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            with open(key_file, "rb") as f:
                self.private = load_pem_private_key(f.read(), password=None)

    @property
    def ready(self) -> bool:
        return self.private is not None or bool(self.secret)

    def sign(self, query: str) -> str:
        if self.private is not None:
            return base64.b64encode(self.private.sign(query.encode())).decode()
        return hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()


class Binance:
    def __init__(self, session: aiohttp.ClientSession, api_key: str = config.API_KEY, signer: Optional[Signer] = None,
                 trade_host: str = config.TRADE_HOST, data_host: str = config.DATA_HOST):
        self.session = session
        self.api_key = api_key
        self.signer = signer
        self.trade_host = trade_host
        self.data_host = data_host
        self.rules: Dict[str, SymbolRules] = {}
        self.last_error: Optional[str] = None

    async def _request(self, method: str, host: str, path: str, params: Optional[dict] = None, signed: bool = False) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
        query = urllib.parse.urlencode(params)
        if signed:
            query += "&signature=" + urllib.parse.quote(self.signer.sign(query))
            headers["X-MBX-APIKEY"] = self.api_key
        url = f"{host}{path}" + (f"?{query}" if query else "")
        async with self.session.request(method, url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400 or (isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0):
                code = data.get("code") if isinstance(data, dict) else None
                msg = data.get("msg") if isinstance(data, dict) else str(data)[:200]
                self.last_error = f"{path}: {msg}"
                raise BinanceError(resp.status, code, msg)
            return data

    # ---------------------------------------------------------------- public market data (real market)

    async def klines(self, symbol: str, interval: str, limit: int) -> List[list]:
        return await self._request("GET", self.data_host, "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    async def funding_history(self, symbol: str, limit: int = 50) -> List[dict]:
        return await self._request("GET", self.data_host, "/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})

    async def premium(self, symbol: str) -> dict:
        return await self._request("GET", self.data_host, "/fapi/v1/premiumIndex", {"symbol": symbol})

    async def book_ticker(self, symbol: str, real_market: bool = True) -> dict:
        """Best bid and ask. The entry checks read the real market: the demo book is much thinner (spreads 0.04-0.24%
        against 0.001-0.01% on 2026-11-09) and would block nearly every signal."""
        return await self._request("GET", self.data_host if real_market else self.trade_host, "/fapi/v1/ticker/bookTicker", {"symbol": symbol})

    async def load_rules(self) -> Dict[str, SymbolRules]:
        info = await self._request("GET", self.trade_host, "/fapi/v1/exchangeInfo")
        for s in info["symbols"]:
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
            self.rules[s["symbol"]] = SymbolRules(float(lot["stepSize"]), float(lot["minQty"]),
                                                  float(f["PRICE_FILTER"]["tickSize"]), float(f["MIN_NOTIONAL"]["notional"]))
        return self.rules

    # ---------------------------------------------------------------- account (trading host, signed)

    async def positions(self) -> List[dict]:
        return await self._request("GET", self.trade_host, "/fapi/v2/positionRisk", {}, signed=True)

    async def balance(self) -> List[dict]:
        return await self._request("GET", self.trade_host, "/fapi/v2/balance", {}, signed=True)

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._request("POST", self.trade_host, "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    async def set_isolated(self, symbol: str):
        try:
            await self._request("POST", self.trade_host, "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except BinanceError as e:
            if e.code != -4046:  # "No need to change margin type."
                raise

    async def market(self, symbol: str, side: str, qty: float, reduce_only: bool = False, client_id: Optional[str] = None) -> dict:
        rules = self.rules[symbol]
        return await self._request("POST", self.trade_host, "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": fmt(qty, rules.step),
            "reduceOnly": "true" if reduce_only else None, "newClientOrderId": client_id, "newOrderRespType": "RESULT"}, signed=True)

    async def place_stop(self, symbol: str, side: str, stop_price: float) -> dict:
        rules = self.rules[symbol]
        return await self._request("POST", self.trade_host, "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": "STOP_MARKET",
            "triggerPrice": fmt(stop_price, rules.tick), "closePosition": "true", "workingType": "MARK_PRICE"}, signed=True)

    async def cancel_stop(self, algo_id: int) -> dict:
        return await self._request("DELETE", self.trade_host, "/fapi/v1/algoOrder", {"algoId": algo_id}, signed=True)

    async def open_stops(self, symbol: Optional[str] = None) -> List[dict]:
        return await self._request("GET", self.trade_host, "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)

    async def cancel_all_stops(self, symbol: str):
        try:
            await self._request("DELETE", self.trade_host, "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)
        except BinanceError:
            pass

    async def trades(self, symbol: str, start_ms: int) -> List[dict]:
        return await self._request("GET", self.trade_host, "/fapi/v1/userTrades", {"symbol": symbol, "startTime": start_ms, "limit": 1000}, signed=True)

    async def income(self, symbol: str, start_ms: int, kind: str = "FUNDING_FEE") -> List[dict]:
        return await self._request("GET", self.trade_host, "/fapi/v1/income",
                                   {"symbol": symbol, "incomeType": kind, "startTime": start_ms, "limit": 1000}, signed=True)


def round_qty(qty: float, rules: SymbolRules) -> float:
    return _round_down(qty, rules.step)


def round_price(price: float, rules: SymbolRules) -> float:
    return round(round(price / rules.tick) * rules.tick, 10)
