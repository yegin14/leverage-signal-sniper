"""Measures five kinds of arbitrage and paper-trades only what survives every cost and a re-check.

  perp_cex_cex  Binance perps vs Bybit perps
  perp_cex_dex  a CEX perp vs Hyperliquid perps
  spot_cex_cex  Binance spot vs Bybit spot
  spot_cex_dex  Binance spot vs Arbitrum pools
  dex_dex       Arbitrum pool vs Arbitrum pool

Perp pairs carry convergence risk: they open with a long and a short, and pay two more taker fees
to close. Spot trades assume funds already wait on both sides, and each trade adds its share of a
transfer to move them back. Polling cannot see sub-second gaps, but a $100 bot could not win those
anyway: faster bots take them first.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import aiohttp

from . import settings
from .costs import (PERP_VENUES, REBALANCE_TRANSFER_USD, SPOT_TAKER_PCT, fee_usd, gas_usd, walk_book,
                    walk_book_notional)
from .venues import Book, DataError, Market

KINDS = {
    "perp_cex_cex": "Vadeli CEX-CEX",
    "perp_cex_dex": "Vadeli CEX-DEX",
    "spot_cex_cex": "Spot CEX-CEX",
    "spot_cex_dex": "Spot CEX-DEX",
    "dex_dex": "DEX-DEX",
}
SPOT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
MAX_HISTORY = 200

USDC = ("0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6)
TOKENS = {
    "WETH": ("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18, "ETHUSDC"),
    "WBTC": ("0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f", 8, "BTCUSDC"),
    "ARB": ("0x912CE59144191C1204E64559FE8253a0e49E6548", 18, "ARBUSDC"),
}
UNI_QUOTER = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
PANCAKE_QUOTER = "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"
SUSHI_ROUTER = "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506"
# The public RPC rejects JSON-RPC batches, so many quotes go through one Multicall3 eth_call.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
POOLS = (
    ("Uniswap %0.05", "v3", UNI_QUOTER, 500),
    ("Uniswap %0.3", "v3", UNI_QUOTER, 3000),
    ("Pancake %0.01", "v3", PANCAKE_QUOTER, 100),
    ("Pancake %0.05", "v3", PANCAKE_QUOTER, 500),
    ("Sushi V2", "v2", SUSHI_ROUTER, 0),
)


@dataclass(frozen=True)
class Opportunity:
    kind: str
    symbol: str
    buy_at: str
    sell_at: str
    notional: float
    gross_usd: float
    fee_usd: float
    other_usd: float
    net_usd: float

    @property
    def gross_bps(self) -> float:
        return self.gross_usd / self.notional * 10_000.0

    @property
    def net_bps(self) -> float:
        return self.net_usd / self.notional * 10_000.0


def book_edge(kind: str, symbol: str, buy: Book, sell: Book, notional: float, buy_fee_pct: float,
              sell_fee_pct: float, close_fee_pct: float = 0.0, other_usd: float = 0.0) -> Optional[Opportunity]:
    """Buy notional on one book's asks and sell the same quantity into the other's bids."""
    _, qty = walk_book_notional(buy.asks, notional)
    if qty <= 0:
        return None
    price_paid, qty_filled = walk_book(buy.asks, qty)
    spent = price_paid * qty_filled
    if spent < notional * 0.999:
        return None  # The book is too thin for this size.
    price_got, filled = walk_book(sell.bids, qty)
    if filled + 1e-12 < qty:
        return None
    proceeds = price_got * qty
    fees = fee_usd(spent, buy_fee_pct) + fee_usd(proceeds, sell_fee_pct) + fee_usd(notional, close_fee_pct)
    gross = proceeds - spent
    return Opportunity(kind, symbol, buy.venue, sell.venue, notional, gross, fees, other_usd, gross - fees - other_usd)


def best_of(ops: Sequence[Optional[Opportunity]]) -> Optional[Opportunity]:
    real = [o for o in ops if o is not None]
    return max(real, key=lambda o: o.net_usd) if real else None


def rebalance_usd(notional: float, inventory_per_side: float) -> float:
    """A share of one transfer: moving a whole side's inventory back costs one transfer."""
    return REBALANCE_TRANSFER_USD * notional / max(inventory_per_side, 1e-9)


# ------------------------------------------------------------------- Arbitrum quotes

def _word(value: int) -> str:
    return hex(value)[2:].rjust(64, "0")


def _addr(address: str) -> str:
    return address[2:].lower().rjust(64, "0")


def encode_quote(pool: tuple, token_in: str, token_out: str, amount_in: int) -> Tuple[str, str]:
    """eth_call target and data. QuoterV2 for V3 pools, getAmountsOut for V2."""
    _, kind, target, fee = pool
    if kind == "v3":
        # quoteExactInputSingle((tokenIn, tokenOut, amountIn, fee, sqrtPriceLimitX96))
        return target, "0xc6a5026a" + _addr(token_in) + _addr(token_out) + _word(amount_in) + _word(fee) + _word(0)
    # getAmountsOut(uint256, address[])
    return target, "0xd06ca61f" + _word(amount_in) + _word(64) + _word(2) + _addr(token_in) + _addr(token_out)


def decode_quote(kind: str, result: Optional[str]) -> Optional[int]:
    if not result or result == "0x":
        return None
    body = result[2:]
    value = int(body[:64], 16) if kind == "v3" else int(body[-64:], 16)
    return value or None


def encode_aggregate3(calls: Sequence[Tuple[str, str]]) -> str:
    """aggregate3((address target, bool allowFailure, bytes callData)[]), every call may fail."""
    elements = []
    for target, data in calls:
        body = data[2:]
        padded = body + "0" * (-len(body) % 64)
        elements.append(_addr(target) + _word(1) + _word(96) + _word(len(body) // 2) + padded)
    offsets, position = [], 32 * len(elements)
    for element in elements:
        offsets.append(_word(position))
        position += len(element) // 2
    return "0x82ad56cb" + _word(32) + _word(len(elements)) + "".join(offsets) + "".join(elements)


def decode_aggregate3(result: str) -> List[Optional[str]]:
    """Return data of each call, or None for a call that failed."""
    body = result[2:]

    def word_at(byte_pos: int) -> int:
        return int(body[byte_pos * 2:byte_pos * 2 + 64], 16)

    start = word_at(0)
    count = word_at(start)
    base = start + 32
    out: List[Optional[str]] = []
    for i in range(count):
        element = base + word_at(base + 32 * i)
        success = word_at(element)
        data_pos = element + word_at(element + 32)
        length = word_at(data_pos)
        data = body[(data_pos + 32) * 2:(data_pos + 32 + length) * 2]
        out.append("0x" + data if success and data else None)
    return out


class ArbitrumQuoter:
    def __init__(self, session: aiohttp.ClientSession, rpc: str = settings.ARBITRUM_RPC):
        self.session = session
        self.rpc = rpc

    async def _rpc(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            async with self.session.post(self.rpc, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    raise DataError("arbitrum", f"HTTP {r.status}")
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise DataError("arbitrum", type(e).__name__) from e
        if "result" not in data:
            raise DataError("arbitrum", str(data.get("error", "yanıt yok"))[:100])
        return data["result"]

    async def quotes(self, requests: List[Tuple[tuple, str, str, int]]) -> List[Optional[int]]:
        """Each request is (pool, token_in, token_out, amount_in). A failed quote is None."""
        if not requests:
            return []
        calls = [encode_quote(pool, token_in, token_out, amount) for pool, token_in, token_out, amount in requests]
        result = await self._rpc("eth_call", [{"to": MULTICALL3, "data": encode_aggregate3(calls)}, "latest"])
        return [decode_quote(req[0][1], raw) for req, raw in zip(requests, decode_aggregate3(result))]

    async def gas_price(self) -> int:
        return int(await self._rpc("eth_gasPrice", []), 16)


def dex_round_trip(buy_pool: str, sell_pool: str, symbol: str, notional: float, usdc_back: float,
                   gas: float) -> Opportunity:
    """Pool fees are already inside the quotes, so the fee field stays zero and gas is the other cost."""
    gross = usdc_back - notional
    return Opportunity("dex_dex", symbol, buy_pool, sell_pool, notional, gross, 0.0, gas, gross - gas)


# ------------------------------------------------------------------- ledger

class ArbLedger:
    def __init__(self, state: Optional[dict] = None, capital: float = settings.ARB_CAPITAL_USD,
                 min_net_bps: float = settings.ARB_MIN_NET_BPS):
        self.capital = capital
        self.min_net_bps = min_net_bps
        self.state = state or self.fresh_state()

    def fresh_state(self) -> dict:
        return {"started_at": time.time(), "kinds": {k: self._fresh_kind() for k in KINDS}}

    def _fresh_kind(self) -> dict:
        return {"balance": self.capital, "scans": 0, "positive": 0, "rejected_on_recheck": 0, "best_gross_bps": None,
                "best_net_bps": None, "last_net_bps": None, "last_gross_bps": None, "last_pair": None,
                "last_scan_at": None, "trades": 0, "net_usd": 0.0, "fees_usd": 0.0, "other_usd": 0.0,
                "open": [], "history": [], "last_error": None}

    def reset(self):
        self.state = self.fresh_state()

    def mark_unavailable(self, kind: str, reason: str, now: float):
        k = self.state["kinds"][kind]
        k["unavailable"] = reason
        k["last_scan_at"] = now

    def observe(self, kind: str, best: Optional[Opportunity], now: float):
        k = self.state["kinds"][kind]
        k.pop("unavailable", None)
        k["scans"] += 1
        k["last_scan_at"] = now
        k["last_error"] = None
        if best is None:
            k["last_net_bps"] = k["last_gross_bps"] = k["last_pair"] = None
            return
        k["last_net_bps"], k["last_gross_bps"] = best.net_bps, best.gross_bps
        k["last_pair"] = f"{best.symbol}: {best.buy_at} al, {best.sell_at} sat"
        k["best_net_bps"] = best.net_bps if k["best_net_bps"] is None else max(k["best_net_bps"], best.net_bps)
        k["best_gross_bps"] = best.gross_bps if k["best_gross_bps"] is None else max(k["best_gross_bps"], best.gross_bps)
        if best.net_bps >= self.min_net_bps:
            k["positive"] += 1

    def record_error(self, kind: str, detail: str, now: float):
        self.state["kinds"][kind]["last_error"] = {"at": now, "detail": detail}

    def wants(self, op: Optional[Opportunity]) -> bool:
        return op is not None and op.net_bps >= self.min_net_bps

    def record_recheck_failure(self, kind: str):
        self.state["kinds"][kind]["rejected_on_recheck"] += 1

    def book_instant(self, op: Opportunity, now: float) -> dict:
        """Spot and DEX trades finish immediately, so the result is known."""
        k = self.state["kinds"][op.kind]
        k["balance"] += op.net_usd
        k["trades"] += 1
        k["net_usd"] += op.net_usd
        k["fees_usd"] += op.fee_usd
        k["other_usd"] += op.other_usd
        trade = {"id": uuid.uuid4().hex[:10], "at": now, "symbol": op.symbol, "buy_at": op.buy_at, "sell_at": op.sell_at,
                 "gross": op.gross_usd, "fees": op.fee_usd, "other": op.other_usd, "net": op.net_usd}
        k["history"].append(trade)
        del k["history"][:-MAX_HISTORY]
        return trade

    def open_pair(self, op: Opportunity, qty: float, buy_price: float, sell_price: float, funding_per_hour: float,
                  now: float) -> dict:
        """Long on op.buy_at and short on op.sell_at. Only entry fees are charged now."""
        k = self.state["kinds"][op.kind]
        entry_fees = op.fee_usd - fee_usd(op.notional, self._close_fee_pct(op))
        k["balance"] -= entry_fees
        k["fees_usd"] += entry_fees
        pair = {"id": uuid.uuid4().hex[:10], "opened_at": now, "symbol": op.symbol, "long_at": op.buy_at,
                "short_at": op.sell_at, "qty": qty, "long_entry": buy_price, "short_entry": sell_price,
                "entry_fees": entry_fees, "funding_per_hour": funding_per_hour, "notional": op.notional}
        k["open"].append(pair)
        return pair

    @staticmethod
    def _close_fee_pct(op: Opportunity) -> float:
        return PERP_VENUES[op.buy_at].taker_pct + PERP_VENUES[op.sell_at].taker_pct

    def maybe_close_pair(self, kind: str, pair: dict, long_book: Book, short_book: Book, now: float,
                         max_hold_hours: float = settings.ARB_MAX_HOLD_HOURS) -> Optional[dict]:
        """Closes once the spread that opened the pair is gone, or when the time limit passes."""
        qty = pair["qty"]
        long_exit, f1 = walk_book(long_book.bids, qty)
        short_exit, f2 = walk_book(short_book.asks, qty)
        if f1 + 1e-12 < qty or f2 + 1e-12 < qty:
            return None
        gross = (long_exit - pair["long_entry"]) * qty + (pair["short_entry"] - short_exit) * qty
        exit_fees = (fee_usd(long_exit * qty, PERP_VENUES[pair["long_at"]].taker_pct)
                     + fee_usd(short_exit * qty, PERP_VENUES[pair["short_at"]].taker_pct))
        funding = pair["funding_per_hour"] * (now - pair["opened_at"]) / 3600.0
        net = gross - pair["entry_fees"] - exit_fees - funding
        converged = short_exit <= long_exit
        expired = now - pair["opened_at"] >= max_hold_hours * 3600.0
        if not (net > 0 or converged or expired):
            return None
        k = self.state["kinds"][kind]
        k["balance"] += gross - exit_fees - funding
        k["fees_usd"] += exit_fees
        k["other_usd"] += funding
        k["trades"] += 1
        k["net_usd"] += net
        k["open"].remove(pair)
        trade = {"id": pair["id"], "at": now, "symbol": pair["symbol"], "buy_at": pair["long_at"],
                 "sell_at": pair["short_at"], "gross": gross, "fees": pair["entry_fees"] + exit_fees,
                 "other": funding, "net": net, "held_hours": (now - pair["opened_at"]) / 3600.0,
                 "reason": "süre doldu" if expired and not (net > 0 or converged) else "yakınsadı"}
        k["history"].append(trade)
        del k["history"][:-MAX_HISTORY]
        return trade


# ------------------------------------------------------------------- scanners

class ArbScanner:
    def __init__(self, market: Market, session: aiohttp.ClientSession, ledger: ArbLedger,
                 cex_venues: Sequence[str] = ("binance", "bybit"), spot_venues: Sequence[str] = ("binance", "bybit"),
                 notional: float = settings.ARB_NOTIONAL_USD, recheck_seconds: float = settings.ARB_RECHECK_SECONDS):
        self.market = market
        self.ledger = ledger
        self.quoter = ArbitrumQuoter(session)
        self.cex_venues = tuple(cex_venues)
        self.spot_venues = tuple(spot_venues)
        self.notional = notional
        self.recheck_seconds = recheck_seconds
        self.events: List[dict] = []

    async def _perp_books(self, symbol: str, venues: Sequence[str]) -> Dict[str, Book]:
        results = await asyncio.gather(*(self.market.book(v, symbol, 20) for v in venues), return_exceptions=True)
        return {v: r for v, r in zip(venues, results) if not isinstance(r, Exception)}

    def _perp_opportunity(self, kind: str, symbol: str, a: Book, b: Book) -> Optional[Opportunity]:
        fa, fb = PERP_VENUES[a.venue].taker_pct, PERP_VENUES[b.venue].taker_pct
        return best_of([
            book_edge(kind, symbol, a, b, self.notional, fa, fb, close_fee_pct=fa + fb),
            book_edge(kind, symbol, b, a, self.notional, fb, fa, close_fee_pct=fa + fb),
        ])

    def set_venues(self, perp: Sequence[str], spot: Sequence[str]) -> None:
        self.cex_venues = tuple(perp)
        self.spot_venues = tuple(spot)

    async def scan_perps(self, symbols: Sequence[str], now: Optional[float] = None):
        venues = (*self.cex_venues, "hyperliquid")
        best: Dict[str, Optional[Opportunity]] = {}
        if len(self.cex_venues) >= 2:
            best["perp_cex_cex"] = None
        else:
            self.ledger.mark_unavailable("perp_cex_cex", self._need_two("vadeli", self.cex_venues), time.time())
        if self.cex_venues:
            best["perp_cex_dex"] = None
        else:
            self.ledger.mark_unavailable("perp_cex_dex", "Bu sunucudan vadeli CEX'e erişilemiyor", time.time())
        for symbol in symbols:
            books = await self._perp_books(symbol, venues)
            await self._close_pairs(symbol, books)
            names = list(books)
            for i, x in enumerate(names):
                for y in names[i + 1:]:
                    kind = "perp_cex_dex" if "hyperliquid" in (x, y) else "perp_cex_cex"
                    if kind in best:
                        best[kind] = best_of([best[kind], self._perp_opportunity(kind, symbol, books[x], books[y])])
        for kind, op in best.items():
            self.ledger.observe(kind, op, time.time())
            if self.ledger.wants(op):
                await self._try_pair(op)

    @staticmethod
    def _need_two(market: str, reachable: Sequence[str]) -> str:
        names = ", ".join(reachable) if reachable else "hiçbiri"
        return f"İki {market} borsa gerekir, bu sunucudan erişilen: {names}"

    async def _try_pair(self, op: Opportunity):
        await asyncio.sleep(self.recheck_seconds)
        books = await self._perp_books(op.symbol, (op.buy_at, op.sell_at))
        if len(books) < 2:
            return
        buy, sell = books[op.buy_at], books[op.sell_at]
        fa, fb = PERP_VENUES[op.buy_at].taker_pct, PERP_VENUES[op.sell_at].taker_pct
        fresh = book_edge(op.kind, op.symbol, buy, sell, op.notional, fa, fb, close_fee_pct=fa + fb)
        if not self.ledger.wants(fresh):
            self.ledger.record_recheck_failure(op.kind)
            return
        if any(p["symbol"] == op.symbol for p in self.ledger.state["kinds"][op.kind]["open"]):
            return
        _, qty = walk_book_notional(buy.asks, op.notional)
        buy_price, _ = walk_book(buy.asks, qty)
        sell_price, _ = walk_book(sell.bids, qty)
        funding = await self._funding_per_hour(op.symbol, op.buy_at, op.sell_at, op.notional)
        pair = self.ledger.open_pair(fresh, qty, buy_price, sell_price, funding, time.time())
        self.events.append({"type": "arb_open", "kind": op.kind, "pair": pair, "op": fresh})

    async def _funding_per_hour(self, symbol: str, long_at: str, short_at: str, notional: float) -> float:
        """Net dollars per hour the pair pays in funding. Unknown rates count as zero and are logged."""
        total = 0.0
        for venue, side in ((long_at, 1), (short_at, -1)):
            try:
                f = await self.market.funding(venue, symbol)
            except DataError:
                continue
            total += f.rate / (PERP_VENUES[venue].funding_interval_seconds / 3600.0) * side
        return total * notional

    async def _close_pairs(self, symbol: str, books: Dict[str, Book]):
        for kind in ("perp_cex_cex", "perp_cex_dex"):
            for pair in list(self.ledger.state["kinds"][kind]["open"]):
                if pair["symbol"] != symbol or pair["long_at"] not in books or pair["short_at"] not in books:
                    continue
                trade = self.ledger.maybe_close_pair(kind, pair, books[pair["long_at"]], books[pair["short_at"]], time.time())
                if trade:
                    self.events.append({"type": "arb_close", "kind": kind, "trade": trade})

    async def scan_spot(self, symbols: Sequence[str] = SPOT_SYMBOLS):
        if len(self.spot_venues) < 2:
            self.ledger.mark_unavailable("spot_cex_cex", self._need_two("spot", self.spot_venues), time.time())
            return
        a, b = self.spot_venues[:2]
        best = None
        other = rebalance_usd(self.notional, settings.ARB_CAPITAL_USD / 2.0)
        fa, fb = SPOT_TAKER_PCT[a], SPOT_TAKER_PCT[b]
        for symbol in symbols:
            results = await asyncio.gather(self.market.spot_book(a, symbol), self.market.spot_book(b, symbol),
                                           return_exceptions=True)
            if any(isinstance(r, Exception) for r in results):
                continue
            x, y = results
            best = best_of([best,
                            book_edge("spot_cex_cex", symbol, x, y, self.notional, fa, fb, other_usd=other),
                            book_edge("spot_cex_cex", symbol, y, x, self.notional, fb, fa, other_usd=other)])
        self.ledger.observe("spot_cex_cex", best, time.time())
        if self.ledger.wants(best):
            await asyncio.sleep(self.recheck_seconds)
            await self._recheck_spot(best, other)

    async def _recheck_spot(self, op: Opportunity, other: float):
        try:
            buy, sell = await asyncio.gather(self.market.spot_book(op.buy_at, op.symbol),
                                             self.market.spot_book(op.sell_at, op.symbol))
        except DataError:
            return
        fresh = book_edge("spot_cex_cex", op.symbol, buy, sell, op.notional, SPOT_TAKER_PCT[op.buy_at],
                          SPOT_TAKER_PCT[op.sell_at], other_usd=other)
        if self.ledger.wants(fresh):
            self.events.append({"type": "arb_trade", "kind": fresh.kind, "trade": self.ledger.book_instant(fresh, time.time())})
        else:
            self.ledger.record_recheck_failure("spot_cex_cex")

    async def _dex_scan_once(self) -> Tuple[Optional[Opportunity], Optional[Opportunity]]:
        """Best DEX-DEX and CEX-DEX opportunities right now, from fresh quotes."""
        gas_price = await self.quoter.gas_price()
        amount_in = int(self.notional * 10 ** USDC[1])
        buys = [(pool, USDC[0], token[0], amount_in) for token in TOKENS.values() for pool in POOLS]
        buy_out = await self.quoter.quotes(buys)
        per_token = {name: dict(zip([p[0] for p in POOLS], buy_out[i * len(POOLS):(i + 1) * len(POOLS)]))
                     for i, name in enumerate(TOKENS)}

        eth_usd = None
        weth = [v for v in per_token["WETH"].values() if v]
        if weth:
            eth_usd = self.notional / (max(weth) / 10 ** TOKENS["WETH"][1])
        if not eth_usd:
            raise DataError("arbitrum", "WETH fiyatı alınamadı")
        gas = gas_usd(gas_price, eth_usd)

        sells, labels = [], []
        for name, (address, decimals, _) in TOKENS.items():
            for buy_pool in POOLS:
                out = per_token[name][buy_pool[0]]
                if not out:
                    continue
                for sell_pool in POOLS:
                    if sell_pool[0] != buy_pool[0]:
                        sells.append((sell_pool, address, USDC[0], out))
                        labels.append((name, buy_pool[0], sell_pool[0]))
        results = await self.quoter.quotes(sells)
        dex_best = None
        for (name, buy_name, sell_name), back in zip(labels, results):
            if back:
                dex_best = best_of([dex_best, dex_round_trip(buy_name, sell_name, name, self.notional,
                                                             back / 10 ** USDC[1], gas)])

        cex_best = await self._cex_dex(per_token, gas)
        return dex_best, cex_best

    async def _cex_dex(self, per_token: Dict[str, dict], gas: float) -> Optional[Opportunity]:
        """A CEX spot book against the best Arbitrum pool, both ways."""
        best = None
        if not self.spot_venues:
            return None
        venue = self.spot_venues[0]
        fee_pct = SPOT_TAKER_PCT[venue]
        one_swap_gas = gas / 2.0
        other = one_swap_gas + rebalance_usd(self.notional, settings.ARB_CAPITAL_USD / 2.0)
        for name, (address, decimals, pair) in TOKENS.items():
            try:
                book = await self.market.spot_book(venue, pair)
            except DataError:
                continue
            quotes = {k: v for k, v in per_token[name].items() if v}
            if not quotes:
                continue
            # Buy on the pool that gives the most tokens, sell into Binance bids.
            pool_name, out = max(quotes.items(), key=lambda kv: kv[1])
            qty = out / 10 ** decimals
            price, filled = walk_book(book.bids, qty)
            if filled + 1e-12 >= qty:
                proceeds = price * qty
                fee = fee_usd(proceeds, fee_pct)
                gross = proceeds - self.notional
                best = best_of([best, Opportunity("spot_cex_dex", name, pool_name, venue, self.notional, gross, fee,
                                                  other, gross - fee - other)])
            # Buy on the CEX asks, sell on the best pool.
            ask_price, qty2 = walk_book_notional(book.asks, self.notional)
            if qty2 > 0:
                amount = int(qty2 * 10 ** decimals)
                back = await self.quoter.quotes([(pool, address, USDC[0], amount) for pool in POOLS])
                valid = [(p[0], b) for p, b in zip(POOLS, back) if b]
                if valid:
                    sell_name, back_units = max(valid, key=lambda x: x[1])
                    proceeds = back_units / 10 ** USDC[1]
                    fee = fee_usd(self.notional, fee_pct)
                    gross = proceeds - self.notional
                    best = best_of([best, Opportunity("spot_cex_dex", name, venue, sell_name, self.notional, gross,
                                                      fee, other, gross - fee - other)])
        return best

    async def scan_dex(self):
        now = time.time()
        try:
            dex_best, cex_best = await self._dex_scan_once()
        except DataError as e:
            self.ledger.record_error("dex_dex", str(e), now)
            self.ledger.record_error("spot_cex_dex", str(e), now)
            return
        self.ledger.observe("dex_dex", dex_best, now)
        if self.spot_venues:
            self.ledger.observe("spot_cex_dex", cex_best, now)
        else:
            self.ledger.mark_unavailable("spot_cex_dex", "Bu sunucudan spot CEX'e erişilemiyor", now)
        if self.ledger.wants(dex_best) or self.ledger.wants(cex_best):
            await asyncio.sleep(self.recheck_seconds)
            try:
                dex_again, cex_again = await self._dex_scan_once()
            except DataError:
                return
            for first, again in ((dex_best, dex_again), (cex_best, cex_again)):
                if not self.ledger.wants(first):
                    continue
                if self.ledger.wants(again):
                    self.events.append({"type": "arb_trade", "kind": again.kind,
                                        "trade": self.ledger.book_instant(again, time.time())})
                else:
                    self.ledger.record_recheck_failure(first.kind)
