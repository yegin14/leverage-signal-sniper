"""Live order books over WebSocket, for venues that rate-limit our shared server IP over REST.

Render's outbound IP is shared with other customers, so Hyperliquid answers REST with HTTP 429 and
Binance with HTTP 418 even when this bot asks rarely. A subscription has no request weight.

Free server plans have little CPU. Messages are only stored when they arrive, and parsed at most
once per second per symbol, or when a reader needs the book. So high and low are sampled once per
second: a wick shorter than that can be missed.
"""

import asyncio
import json
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import aiohttp
import numpy as np

from .venues import Book, Candles, Funding, coin

logger = logging.getLogger("tradebot.feeds")

# Hyperliquid pushes a book only when it changes, so one quiet symbol proves nothing. Silence from
# the whole connection does.
ALIVE_SECONDS = 15.0
DIGEST_SECONDS = 1.0
MINUTES_KEPT = 240
PING_SECONDS = 30.0
RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 120.0
BLOCKED_RETRY_SECONDS = 300.0
HEALTHY_CONNECTION_SECONDS = 60.0
FUNDING_PARSE_SECONDS = 30.0


class BookFeed:
    venue = ""
    url = ""

    def __init__(self, session: Optional[aiohttp.ClientSession], symbols):
        self.session = session
        self.symbols = tuple(symbols)
        self.raw: Dict[str, str] = {}
        self.dirty: set = set()
        self.levels: Dict[str, Tuple[list, list]] = {}
        self.minutes: Dict[str, Dict[int, List[float]]] = {}
        self.last_msg_at = 0.0
        self.last_digest_at = 0.0
        self.last_ping_at = 0.0
        self.connected_since: Optional[float] = None
        self.messages = 0
        self.reconnects = 0
        self.last_error: Optional[str] = None

    # ---------------------------------------------------------------- for subclasses

    def subscriptions(self) -> List[dict]:
        return []

    def route(self, text: str) -> Optional[str]:
        """Stores the message if it is a book. Returns its symbol."""
        raise NotImplementedError

    def parse_levels(self, text: str) -> Tuple[list, list]:
        raise NotImplementedError

    def ping_message(self) -> Optional[dict]:
        return None

    # ---------------------------------------------------------------- reading

    def alive(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) - self.last_msg_at < ALIVE_SECONDS

    def _digest(self, symbol: str, now: float) -> None:
        if symbol not in self.dirty:
            return
        self.dirty.discard(symbol)
        try:
            bids, asks = self.parse_levels(self.raw[symbol])
        except (KeyError, ValueError, IndexError, TypeError) as e:
            self.last_error = f"çözülemeyen mesaj: {type(e).__name__}"
            return
        if not bids or not asks:
            return
        self.levels[symbol] = (bids, asks)
        mid = (bids[0][0] + asks[0][0]) / 2.0
        minute = int(now // 60) * 60_000
        bars = self.minutes.setdefault(symbol, {})
        bar = bars.get(minute)
        if bar is None:
            bars[minute] = [mid, mid, mid, mid]
            for old in [m for m in bars if m < minute - MINUTES_KEPT * 60_000]:
                del bars[old]
        else:
            bar[1], bar[2], bar[3] = max(bar[1], mid), min(bar[2], mid), mid

    def book(self, symbol: str, depth: int = 20, now: Optional[float] = None) -> Optional[Book]:
        """The latest book, or None if this feed cannot vouch for it."""
        now = now if now is not None else time.time()
        if not self.alive(now):
            return None
        self._digest(symbol, now)
        levels = self.levels.get(symbol)
        if not levels:
            return None
        return Book(self.venue, symbol, levels[0][:depth], levels[1][:depth], now)

    def tops(self, now: Optional[float] = None) -> Dict[str, Tuple[float, float]]:
        now = now if now is not None else time.time()
        if not self.alive(now):
            return {}
        for symbol in list(self.dirty):
            self._digest(symbol, now)
        return {s: (b[0][0], a[0][0]) for s, (b, a) in self.levels.items() if b and a}

    def candles(self, symbol: str, limit: int, now: Optional[float] = None) -> Optional[Candles]:
        """One-minute bars built from sampled mid prices. None if there are none yet."""
        now = now if now is not None else time.time()
        if not self.alive(now):
            return None
        self._digest(symbol, now)
        bars = self.minutes.get(symbol)
        if not bars:
            return None
        keys = sorted(bars)[-limit:]
        a = np.array([bars[k] for k in keys], dtype=np.float64)
        return Candles(np.array(keys, dtype=np.float64), a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.zeros(len(keys)))

    def funding(self, symbol: str, now: Optional[float] = None) -> Optional[Funding]:
        return None

    def status(self, now: Optional[float] = None) -> dict:
        now = now if now is not None else time.time()
        return {
            "venue": self.venue,
            "alive": self.alive(now),
            "symbols": len(self.levels),
            "messages": self.messages,
            "reconnects": self.reconnects,
            "last_message_age": round(now - self.last_msg_at, 1) if self.last_msg_at else None,
            "last_error": self.last_error,
        }

    # ---------------------------------------------------------------- connection

    def on_text(self, text: str, now: float) -> None:
        self.messages += 1
        self.last_msg_at = now
        symbol = self.route(text)
        if symbol:
            self.dirty.add(symbol)
        if now - self.last_digest_at >= DIGEST_SECONDS:
            self.last_digest_at = now
            for s in list(self.dirty):
                self._digest(s, now)

    async def _session_once(self) -> None:
        async with self.session.ws_connect(self.url, heartbeat=25.0, timeout=aiohttp.ClientWSTimeout(ws_close=10.0),
                                           max_msg_size=4 * 1024 * 1024) as ws:
            self.connected_since = time.time()
            self.last_error = None
            for message in self.subscriptions():
                await ws.send_json(message)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    now = time.time()
                    self.on_text(msg.data, now)
                    ping = self.ping_message()
                    if ping and now - self.last_ping_at >= PING_SECONDS:
                        self.last_ping_at = now
                        await ws.send_json(ping)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def run(self) -> None:
        """Runs for the life of the bot. A lost connection comes back without anyone's help."""
        wait = RETRY_SECONDS
        while True:
            started = time.time()
            try:
                await self._session_once()
                self.last_error = self.last_error or "bağlantı kapandı"
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as e:
                self.last_error = f"HTTP {e.status}"
                if e.status in (403, 418, 429, 451):
                    wait = BLOCKED_RETRY_SECONDS
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.last_error = type(e).__name__
            except Exception as e:  # This task must never die, or prices stop for good.
                logger.exception("Feed %s failed", self.venue)
                self.last_error = type(e).__name__
            self.connected_since = None
            self.reconnects += 1
            if time.time() - started >= HEALTHY_CONNECTION_SECONDS:
                wait = RETRY_SECONDS
            await asyncio.sleep(wait)
            wait = min(wait * 2, max(MAX_RETRY_SECONDS, wait))


_HL_COIN = re.compile(r'"coin":"([^"]+)"')


class HyperliquidFeed(BookFeed):
    venue = "hyperliquid"
    url = "wss://api.hyperliquid.xyz/ws"

    def __init__(self, session, symbols):
        super().__init__(session, symbols)
        self.ctx_raw: Dict[str, str] = {}

    def subscriptions(self) -> List[dict]:
        out = []
        for symbol in self.symbols:
            out.append({"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin(symbol)}})
            out.append({"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": coin(symbol)}})
        return out

    def ping_message(self) -> Optional[dict]:
        return {"method": "ping"}

    def route(self, text: str) -> Optional[str]:
        is_book = text.startswith('{"channel":"l2Book"')
        if not is_book and not text.startswith('{"channel":"activeAssetCtx"'):
            return None
        match = _HL_COIN.search(text, 0, 200)
        if not match:
            return None
        symbol = f"{match.group(1)}USDT"
        if is_book:
            self.raw[symbol] = text
            return symbol
        self.ctx_raw[symbol] = text
        return None

    def parse_levels(self, text: str) -> Tuple[list, list]:
        levels = json.loads(text)["data"]["levels"]
        return ([[float(x["px"]), float(x["sz"])] for x in levels[0]],
                [[float(x["px"]), float(x["sz"])] for x in levels[1]])

    def funding(self, symbol: str, now: Optional[float] = None) -> Optional[Funding]:
        now = now if now is not None else time.time()
        raw = self.ctx_raw.get(symbol)
        if not raw or not self.alive(now):
            return None
        try:
            rate = float(json.loads(raw)["data"]["ctx"]["funding"])
        except (KeyError, ValueError, TypeError):
            return None
        # Hyperliquid quotes and settles funding every hour on the hour.
        return Funding(rate, (now // 3600 + 1) * 3600)


class BinanceFeed(BookFeed):
    venue = "binance"

    def __init__(self, session, symbols):
        super().__init__(session, symbols)
        streams = "/".join(f"{s.lower()}@depth10@500ms" for s in self.symbols)
        # One all-symbol message every few seconds carries funding, and is parsed only when needed.
        self.url = f"wss://fstream.binance.com/stream?streams={streams}/!markPrice@arr"
        self.mark_raw: Optional[str] = None
        self.funding_rows: Dict[str, Funding] = {}
        self.funding_parsed_at = 0.0

    def route(self, text: str) -> Optional[str]:
        # {"stream":"btcusdt@depth10@500ms","data":{...
        if text.startswith('{"stream":"!markPrice'):
            self.mark_raw = text
            return None
        if not text.startswith('{"stream":"'):
            return None
        end = text.find("@", 11, 60)
        if end < 0:
            return None
        symbol = text[11:end].upper()
        self.raw[symbol] = text
        return symbol

    def parse_levels(self, text: str) -> Tuple[list, list]:
        data = json.loads(text)["data"]
        return ([[float(p), float(q)] for p, q in data["b"]],
                [[float(p), float(q)] for p, q in data["a"]])

    def funding(self, symbol: str, now: Optional[float] = None) -> Optional[Funding]:
        now = now if now is not None else time.time()
        if not self.alive(now) or not self.mark_raw:
            return None
        if now - self.funding_parsed_at >= FUNDING_PARSE_SECONDS or symbol not in self.funding_rows:
            try:
                rows = json.loads(self.mark_raw)["data"]
                self.funding_rows = {r["s"]: Funding(float(r["r"]), float(r["T"]) / 1000.0) for r in rows if r.get("r") not in (None, "")}
                self.funding_parsed_at = now
            except (KeyError, ValueError, TypeError):
                return None
        return self.funding_rows.get(symbol)
