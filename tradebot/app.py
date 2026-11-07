"""Runs everything: strategies, positions, news, desk, arbitrage, Telegram and persistence."""

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
import numpy as np
from aiohttp import web

from . import reports, settings
from .arbitrage import ArbLedger, ArbScanner
from .costs import PERP_VENUES, breakeven_winrate, round_trip_cost_pct, sign
from .desk import Candidate, Desk
from .news import NewsDesk
from .paper import PaperBook, Plan, Quote
from .risk import Filters
from .signals import ema
from .strategies import LIVE_STRATEGIES, NEWS_EXITS, Bars, ExitRule, LiveStrategy, atr, rsi
from .telegram import Telegram
from .venues import Candles, DataError, Funding, Market

logger = logging.getLogger("tradebot")

POSITION_POLL_SECONDS = 5.0
STRATEGY_POLL_SECONDS = 20.0
BAR_CLOSE_GRACE_SECONDS = 8.0
NEWS_POLL_SECONDS = 180.0
PERSIST_SECONDS = 30.0
BACKUP_SECONDS = 600.0
BACKUP_CHECK_SECONDS = 20.0
KEEP_AWAKE_SECONDS = 600.0
FUNDING_CACHE_SECONDS = 60.0
NEWS_SYMBOL_COOLDOWN_SECONDS = 3600.0
# After a desk session the price must still be near the plan. Share of the stop distance.
MAX_ENTRY_DRIFT = 0.35
# A signal older than this is not traded, for example right after a restart.
MAX_SIGNAL_AGE_SECONDS = 300.0
ASSUMED_REWARD_R_WITHOUT_TARGET = 2.0


@dataclass
class DeskRequest:
    symbol: str
    trigger: str
    label: str
    direction: Optional[str]
    technical: dict
    signal_atr: float
    exits: ExitRule
    hours: int
STATE_VERSION = 2


def bars_from(symbol: str, candles: Candles, funding: Optional[np.ndarray], bar_ms: int) -> Bars:
    fts, frate = (funding[:, 0], funding[:, 1]) if funding is not None and len(funding) else (np.zeros(0), np.zeros(0))
    return Bars(symbol, bar_ms, candles.ts, candles.opens, candles.highs, candles.lows, candles.closes,
                candles.volumes, fts, frate)


def build_plan(symbol: str, direction: str, reference: float, signal_atr: float, exits: ExitRule, limits,
               origin: str, strategy: str, hours: int, reason: str = "", size_factor: float = 1.0,
               session_id: Optional[str] = None) -> Plan:
    """Stop and target from strategy exits, with the stop distance held inside the allowed band."""
    distance = float(np.clip(signal_atr * exits.stop_atr, reference * limits.min_stop_pct / 100.0,
                             reference * limits.max_stop_pct / 100.0))
    s = sign(direction)
    target = reference + s * distance * exits.target_r if exits.target_r else None
    return Plan(symbol, direction, reference - s * distance, target, origin, strategy, reason, size_factor,
                session_id, exits.trail_atr, hours, exits.max_hold_hours)


def venue_with_results(saved_books: Optional[dict]) -> Optional[str]:
    """The CEX venue of a saved book that has trades or open positions, if any."""
    for name in ("rules_cex", "desk_cex"):
        state = (saved_books or {}).get(name)
        if state and (state.get("history") or state.get("open")):
            return state.get("venue")
    return None


def drift_is_acceptable(direction: str, planned_price: float, current_price: float, stop_distance: float) -> bool:
    """False if price ran away in the trade's direction or already fell most of the way to the stop."""
    moved = (current_price - planned_price) * sign(direction)
    return abs(moved) <= MAX_ENTRY_DRIFT * stop_distance


class Bot:
    def __init__(self, session: aiohttp.ClientSession, desk: Optional[Desk] = None, data_dir: Path = settings.DATA_DIR):
        self.session = session
        self.market = Market(session)
        self.started_at = time.time()
        self.data_dir = Path(data_dir)
        self.limits = settings.LIMITS
        self.cex_venue: Optional[str] = None
        self.books: Dict[str, PaperBook] = {}
        self.news = NewsDesk()
        self.desk = desk or Desk()
        self.arb = ArbLedger()
        self.scanner: Optional[ArbScanner] = None
        self.telegram = Telegram(session, self)
        self.seen_bars: Dict[str, float] = {}
        self.news_cooldown: Dict[str, float] = {}
        self.quote_times: Dict[str, float] = {}
        self.funding_cache: Dict[str, Tuple[float, Funding]] = {}
        self.desk_queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self.notified: Dict[str, float] = {}
        self.loop_errors: Dict[str, dict] = {}
        self.backup_dirty = False
        self.perp_venues: List[str] = []
        self.spot_venues: List[str] = []

    # ---------------------------------------------------------------- setup and persistence

    def _make_books(self, saved: Optional[dict]):
        saved = saved or {}
        venues = {"cex": self.cex_venue, "dex": settings.DEX_VENUE}
        for source in ("rules", "desk"):
            for kind, venue in venues.items():
                if not venue:
                    continue
                name = f"{source}_{kind}"
                state = saved.get(name)
                if state and state.get("venue") != venue:
                    # Another venue has other fees: its old results would mislead, so start clean.
                    logger.warning("Book %s moved from %s to %s: starting fresh", name, state.get("venue"), venue)
                    state = None
                self.books[name] = PaperBook(name, venue, source, self.limits, state)

    def snapshot(self) -> dict:
        return {
            "version": STATE_VERSION, "saved_at": time.time(),
            "books": {k: b.state for k, b in self.books.items()},
            "news": self.news.to_state(), "desk": self.desk.state, "arb": self.arb.state,
            "seen_bars": self.seen_bars, "news_cooldown": self.news_cooldown,
        }

    def _state_path(self) -> Path:
        return self.data_dir / "state.json"

    def save_local(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._state_path())

    async def load_state(self) -> Optional[dict]:
        """The newest of the local file and the Telegram backup. A corrupt file is not trusted."""
        local = None
        if self._state_path().exists():
            try:
                local = json.loads(self._state_path().read_text(encoding="utf-8"))
            except ValueError:
                logger.error("Local state unreadable, ignoring it")
        remote = await self.telegram.load_backup()
        candidates = [s for s in (local, remote) if s and s.get("version") == STATE_VERSION]
        return max(candidates, key=lambda s: s.get("saved_at", 0)) if candidates else None

    async def setup(self):
        saved = await self.load_state()
        saved_books = (saved or {}).get("books")
        # A book with results stays on its venue: changing venue would discard them.
        self.cex_venue = venue_with_results(saved_books) if settings.CEX_VENUE == "auto" else None
        if not self.cex_venue:
            self.cex_venue = await self.market.pick_cex(settings.CEX_VENUE)
        if not self.cex_venue:
            logger.error("No CEX reachable: CEX books are off")
        self._make_books(saved_books)
        if saved:
            self.news = NewsDesk(saved.get("news"))
            if saved.get("desk"):
                self.desk.state = saved["desk"]
            if saved.get("arb"):
                self.arb = ArbLedger(saved["arb"])
            self.seen_bars = saved.get("seen_bars", {})
            self.news_cooldown = saved.get("news_cooldown", {})
        self.perp_venues = [v for v in ("binance", "bybit") if await self._reachable(v)]
        self.spot_venues = [v for v in ("binance", "bybit") if await self._spot_reachable(v)]
        self.scanner = ArbScanner(self.market, self.session, self.arb, self.perp_venues, self.spot_venues)

    async def _reachable(self, venue: str) -> bool:
        try:
            await self.market.book(venue, "BTCUSDT", 5)
            return True
        except DataError:
            return False

    async def _spot_reachable(self, venue: str) -> bool:
        try:
            await self.market.spot_book(venue, "BTCUSDT", 5)
            return True
        except DataError:
            return False

    def reset_paper(self):
        for book in self.books.values():
            book.reset()
        self.arb.reset()

    # ---------------------------------------------------------------- report helpers

    def last_session(self) -> Optional[dict]:
        sessions = self.desk.state["sessions"]
        return sessions[-1] if sessions else None

    def quote_age(self, venue: str, symbol: str, now: float) -> Optional[float]:
        at = self.quote_times.get(f"{venue}:{symbol}")
        return now - at if at else None

    def data_errors(self, now: float) -> List[str]:
        out = [f"{k}: {v}" for k, v in self.market.errors.items()]
        out += [f"{k}: {v['detail']}" for k, v in self.loop_errors.items() if now - v["at"] < 900.0]
        return out

    async def notify_once(self, key: str, text: str, every_seconds: float = 3600.0):
        now = time.time()
        if now - self.notified.get(key, 0.0) >= every_seconds:
            self.notified[key] = now
            await self.telegram.send(text)

    # ---------------------------------------------------------------- market access

    async def _funding(self, venue: str, symbol: str) -> Optional[Funding]:
        key = f"{venue}:{symbol}"
        cached = self.funding_cache.get(key)
        if cached and time.time() - cached[0] < FUNDING_CACHE_SECONDS:
            return cached[1]
        try:
            f = await self.market.funding(venue, symbol)
        except DataError:
            return cached[1] if cached else None
        self.funding_cache[key] = (time.time(), f)
        return f

    async def _quote(self, venue: str, symbol: str) -> Quote:
        """A real order book with the latest funding. Candle range is added per position."""
        book = await self.market.book(venue, symbol, 20)
        funding = await self._funding(venue, symbol)
        self.quote_times[f"{venue}:{symbol}"] = book.ts
        return Quote(book.bids, book.asks, book.ts, None, None,
                     funding.rate if funding else None, funding.next_ts if funding else None)

    async def _filters(self, venue: str, symbol: str) -> Filters:
        return await self.market.filters(venue, symbol)

    # ---------------------------------------------------------------- strategies

    @property
    def signal_venue(self) -> str:
        return self.cex_venue or settings.DEX_VENUE

    async def _bars(self, symbol: str, strategy: LiveStrategy) -> Bars:
        candles = await self.market.candles(self.signal_venue, symbol, strategy.interval, strategy.bars_needed)
        bar_ms = strategy.hours * 3_600_000
        closed = candles.closed(time.time() * 1000, bar_ms)
        funding = None
        if strategy.name.startswith("funding"):
            if not self.cex_venue:
                # The thresholds are for 8-hour rates, so hourly Hyperliquid rates cannot stand in.
                raise DataError("strateji", "fonlama stratejisi CEX verisi ister")
            funding = await self.market.funding_history(self.cex_venue, symbol)
        return bars_from(symbol, closed, funding, bar_ms)

    async def scan_strategies(self):
        now_ms = time.time() * 1000
        for strategy in LIVE_STRATEGIES:
            bar_ms = strategy.hours * 3_600_000
            last_closed_open = (now_ms - BAR_CLOSE_GRACE_SECONDS * 1000) // bar_ms * bar_ms - bar_ms
            for symbol in settings.SYMBOLS:
                key = f"{strategy.name}:{symbol}"
                if self.seen_bars.get(key, 0) >= last_closed_open:
                    continue
                try:
                    bars = await self._bars(symbol, strategy)
                except DataError as e:
                    self.loop_errors[f"strateji {symbol}"] = {"at": time.time(), "detail": str(e)}
                    continue
                if len(bars.ts) == 0 or bars.ts[-1] < last_closed_open:
                    continue  # The venue has not published the new bar yet.
                self.seen_bars[key] = float(bars.ts[-1])
                await self._after_bar(strategy, bars)

    async def _after_bar(self, strategy: LiveStrategy, bars: Bars):
        a = atr(bars)
        if not np.isfinite(a[-1]):
            return
        for book in self.books.values():
            book.update_trail(bars.symbol, strategy.hours, float(bars.closes[-1]), float(a[-1]))
        closed_at = (bars.ts[-1] + bars.bar_ms) / 1000.0
        if time.time() - closed_at > MAX_SIGNAL_AGE_SECONDS:
            return
        last = len(bars.ts) - 1
        for i, direction, signal_atr in strategy.make(bars):
            if i != last or not np.isfinite(signal_atr):
                continue
            logger.info("Signal %s %s %s", strategy.name, bars.symbol, direction)
            if strategy.rules_trade:
                await self._open_rules(strategy, bars.symbol, direction, float(signal_atr))
            await self._queue_request(DeskRequest(bars.symbol, strategy.name, strategy.label, direction,
                                                  self._technical_summary(bars), float(signal_atr), strategy.exits,
                                                  strategy.hours))

    async def _open_rules(self, strategy: LiveStrategy, symbol: str, direction: str, signal_atr: float):
        reason = f"{strategy.label}: {direction}"
        for book in self.books.values():
            if book.source != "rules":
                continue
            await self._open_on_book(book, symbol, direction, signal_atr, strategy.exits, strategy.name,
                                     strategy.hours, reason)

    async def _open_on_book(self, book: PaperBook, symbol: str, direction: str, signal_atr: float, exits: ExitRule,
                            strategy_name: str, hours: int, reason: str, size_factor: float = 1.0,
                            session_id: Optional[str] = None) -> Optional[dict]:
        venue = book.venue.name
        try:
            quote = await self._quote(venue, symbol)
            filters = await self._filters(venue, symbol)
        except DataError as e:
            await self.telegram.send(f"⚠️ {book.name} {symbol}: işlem açılamadı, veri yok ({e}).")
            return None
        reference = quote.asks[0][0] if sign(direction) > 0 else quote.bids[0][0]
        plan = build_plan(symbol, direction, reference, signal_atr, exits, self.limits, book.source, strategy_name,
                          hours, reason, size_factor, session_id)
        result = book.try_open(plan, quote, filters)
        if "opened" in result:
            self.backup_dirty = True
            await self.telegram.send(reports.format_open_event(book, result["opened"]))
            return result["opened"]
        logger.info("%s rejected %s %s: %s", book.name, symbol, direction, result["rejected"])
        return None

    # ---------------------------------------------------------------- positions

    async def update_positions(self):
        targets = {(b.venue.name, p["symbol"]) for b in self.books.values() for p in b.state["open"]}
        for venue, symbol in targets:
            positions = [(b, p) for b in self.books.values() if b.venue.name == venue
                         for p in b.state["open"] if p["symbol"] == symbol]
            oldest = min(p["range_from_ms"] for _, p in positions)
            minutes = int((time.time() * 1000 - oldest) // 60_000) + 2
            try:
                quote = await self._quote(venue, symbol)
                candles = await self.market.candles(venue, symbol, "1m", min(max(minutes, 3), 1000))
            except DataError as e:
                self.loop_errors[f"fiyat {venue}"] = {"at": time.time(), "detail": str(e)}
                continue
            for book, p in positions:
                keep = candles.ts >= p["range_from_ms"]
                quote.high = float(candles.highs[keep].max()) if keep.any() else None
                quote.low = float(candles.lows[keep].min()) if keep.any() else None
                was_halted = book.state["halted"]
                for trade in book.on_quote(symbol, quote):
                    self.backup_dirty = True
                    await self.telegram.send(reports.format_close_event(book, trade))
                if book.state["halted"] and not was_halted:
                    await self.telegram.send(f"⛔ {book.name} durduruldu: {book.state['halted']}. Yeni işlem açmaz.")

    # ---------------------------------------------------------------- desk

    def _technical_summary(self, bars: Bars) -> dict:
        closes = bars.closes
        a = atr(bars)
        def change(n: int) -> Optional[float]:
            return round((closes[-1] / closes[-1 - n] - 1.0) * 100.0, 2) if len(closes) > n else None
        return {
            "bar_hours": bars.bar_ms / 3_600_000,
            "last_close": float(closes[-1]),
            "ema20": round(float(ema(closes, 20)[-1]), 6),
            "ema50": round(float(ema(closes, 50)[-1]), 6),
            "ema200": round(float(ema(closes, 200)[-1]), 6) if len(closes) >= 200 else None,
            "rsi14": round(float(rsi(closes)[-1]), 1),
            "atr14_pct_of_price": round(float(a[-1] / closes[-1] * 100.0), 3),
            "change_last_1_bar_pct": change(1),
            "change_last_6_bars_pct": change(6),
            "change_last_24_bars_pct": change(24),
            "high_of_last_20_bars": float(bars.highs[-20:].max()),
            "low_of_last_20_bars": float(bars.lows[-20:].min()),
            "volume_vs_20_bar_average": round(float(bars.volumes[-1] / max(bars.volumes[-21:-1].mean(), 1e-12)), 2),
        }

    async def _queue_request(self, request: DeskRequest):
        now = time.time()
        block = self.desk.block_reason(now)
        if block:
            if self.desk.enabled:
                await self.notify_once(f"desk-block-{int(now // 86400)}",
                                       f"🧠 Masa {request.symbol} sinyaline bakamadı: {block}", 86400.0)
            return
        try:
            self.desk_queue.put_nowait(request)
        except asyncio.QueueFull:
            logger.warning("Desk queue full, dropping %s", request.symbol)

    async def _complete_briefing(self, r: DeskRequest, now: float) -> Tuple[dict, List[str]]:
        """Adds costs, positioning, news and open exposure. Missing data stays visibly missing."""
        flow = await self.market.flow(r.symbol)
        fear = await self.market.fear_greed()
        target_r = r.exits.target_r or ASSUMED_REWARD_R_WITHOUT_TARGET
        venue = PERP_VENUES[self.cex_venue or settings.DEX_VENUE]
        stop_pct = r.signal_atr * r.exits.stop_atr / r.technical["last_close"] * 100.0
        cost_pct = round_trip_cost_pct(venue, exit_is_maker=bool(r.exits.target_r), slippage_bps=2.0)
        cost_r = cost_pct / max(stop_pct, 1e-9)
        news_lines = [f"- ({reports.age_text(now - n.published)}, {n.source}) {n.title}"
                      for n in self.news.for_symbol(r.symbol, now)]
        exposure = [{"book": b.name, "symbol": p["symbol"], "direction": p["direction"]}
                    for b in self.books.values() if b.source == "desk" for p in b.state["open"]]
        briefing = {
            "technical": r.technical,
            "flow": flow or "unavailable",
            "fear_and_greed": fear or "unavailable",
            "plan_set_by_code": {
                "stop_distance_pct": round(stop_pct, 3),
                "fixed_target_r": r.exits.target_r,
                "trailing_stop": bool(r.exits.trail_atr),
                "max_hold_hours": r.exits.max_hold_hours,
                "risk_usd": self.limits.risk_per_trade_usd,
            },
            "costs": {
                "round_trip_pct_of_position": round(cost_pct, 3),
                "cost_in_r": round(cost_r, 3),
                "reward_r_used_for_break_even": target_r,
                "break_even_win_rate_pct": round(breakeven_winrate(target_r, cost_r) * 100.0, 1),
            },
            "open_desk_positions": exposure,
        }
        return briefing, news_lines

    async def run_desk_request(self, r: DeskRequest):
        now = time.time()
        briefing, news_lines = await self._complete_briefing(r, now)
        candidate = Candidate(r.symbol, r.trigger, r.label, r.direction, briefing, news_lines)
        record = await self.desk.run_session(candidate, now)
        if record.get("skipped"):
            await self.notify_once(f"desk-skip-{int(now // 86400)}", f"🧠 Masa çalışmadı: {record['reason']}", 86400.0)
            return
        await self.telegram.send(reports.format_session(record, full=True))
        if record["action"] == "NO_TRADE":
            return
        stop_distance = r.signal_atr * r.exits.stop_atr
        for book in self.books.values():
            if book.source != "desk":
                continue
            try:
                snapshot = await self.market.book(book.venue.name, r.symbol, 5)
            except DataError as e:
                await self.telegram.send(f"⚠️ {book.name}: masa kararı uygulanamadı, veri yok ({e}).")
                continue
            if not drift_is_acceptable(record["action"], r.technical["last_close"], snapshot.mid, stop_distance):
                await self.telegram.send(f"🧠 {book.name} {r.symbol}: fiyat tartışma sırasında çok oynadı, işlem açılmadı.")
                continue
            await self._open_on_book(book, r.symbol, record["action"], r.signal_atr, r.exits, r.trigger, r.hours,
                                     f"Masa kararı: {record['reason']}", record["size_factor"], record["id"])

    async def trigger_news_sessions(self):
        now = time.time()
        for item in self.news.session_triggers(now):
            for symbol in item.symbols:
                if now - self.news_cooldown.get(symbol, 0.0) < NEWS_SYMBOL_COOLDOWN_SECONDS:
                    continue
                self.news_cooldown[symbol] = now
                try:
                    candles = await self.market.candles(self.signal_venue, symbol, "1h", 250)
                except DataError:
                    continue
                bars = bars_from(symbol, candles.closed(now * 1000, 3_600_000), None, 3_600_000)
                a = atr(bars)
                if len(bars.ts) < 60 or not np.isfinite(a[-1]):
                    continue
                await self._queue_request(DeskRequest(symbol, "haber", f"Haber: {item.title}", None,
                                                      self._technical_summary(bars), float(a[-1]), NEWS_EXITS, 1))

    # ---------------------------------------------------------------- loops

    async def _forever(self, name: str, seconds: float, fn):
        """A failure in one loop is logged, reported, and never stops the others."""
        while True:
            try:
                await fn()
                self.loop_errors.pop(name, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Loop %s failed", name)
                self.loop_errors[name] = {"at": time.time(), "detail": f"{type(e).__name__}: {str(e)[:120]}"}
            await asyncio.sleep(seconds)

    async def _news_step(self):
        await self.news.refresh(self.session)
        await self.trigger_news_sessions()

    async def _desk_worker(self):
        while True:
            request = await self.desk_queue.get()
            try:
                await self.run_desk_request(request)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Desk session failed")
                await self.telegram.send(f"⚠️ Masa oturumu hata verdi ({type(e).__name__}). İşlem açılmadı.")

    async def _arb_perp_step(self):
        await self.scanner.scan_perps(settings.SYMBOLS)
        await self.scanner.scan_spot()
        await self._flush_arb_events()

    async def _arb_dex_step(self):
        await self.scanner.scan_dex()
        await self._flush_arb_events()

    async def _flush_arb_events(self):
        events, self.scanner.events = self.scanner.events, []
        for event in events:
            if event["type"] == "arb_open":
                p = event["pair"]
                await self.telegram.send(f"💱 Sahte arbitraj açıldı: {p['symbol']}, {p['long_at']} long, {p['short_at']} short.\n"
                                         f"Beklenen net {event['op'].net_bps:+.1f} bps. Sonuç kapanınca belli olur.")
            else:
                t = event["trade"]
                await self.telegram.send(f"💱 Sahte arbitraj sonucu: {t['symbol']}, {t['buy_at']} al, {t['sell_at']} sat.\n"
                                         f"Brüt {reports.money(t['gross'])} | Komisyon {t['fees']:.3f}$ | Diğer {t['other']:.3f}$ | "
                                         f"NET {reports.money(t['net'])}")

    async def _persist_step(self):
        self.save_local()

    async def backup_now(self):
        await self.telegram.save_backup(self.snapshot())

    async def _backup_loop(self):
        """Saves on a timer and soon after any trade, so a redeploy loses almost nothing."""
        last = time.time()
        while True:
            await asyncio.sleep(BACKUP_CHECK_SECONDS)
            if self.backup_dirty or time.time() - last >= BACKUP_SECONDS:
                self.backup_dirty = False
                last = time.time()
                try:
                    await self.backup_now()
                except Exception:
                    logger.exception("Backup failed")

    async def _keep_awake_step(self):
        if settings.EXTERNAL_URL:
            async with self.session.get(settings.EXTERNAL_URL, timeout=aiohttp.ClientTimeout(total=20)):
                pass

    async def _health(self, _request) -> web.Response:
        now = time.time()
        return web.json_response({
            "ok": True, "mode": "paper", "uptime_hours": round((now - self.started_at) / 3600.0, 2),
            "cex": self.cex_venue, "dex": settings.DEX_VENUE,
            "books": {k: {x: b.stats()[x] for x in ("balance", "equity", "trades", "net", "open", "halted")}
                      for k, b in self.books.items()},
            "desk": {"enabled": self.desk.enabled, "reason": self.desk.disabled_reason, "model": self.desk.model,
                     "sessions": self.desk.state["sessions_total"], "spent_usd": round(self.desk.state["spent_total"], 4)},
            "news_items": len(self.news.items),
            "reachable": {"perp": self.perp_venues, "spot": self.spot_venues},
            "arbitrage": {key: {"scans": k["scans"], "gross_bps": k["last_gross_bps"], "net_bps": k["last_net_bps"],
                                "best_net_bps": k["best_net_bps"], "trades": k["trades"], "net_usd": k["net_usd"],
                                "unavailable": k.get("unavailable"), "error": k["last_error"]}
                          for key, k in self.arb.state["kinds"].items()},
            "errors": self.data_errors(now),
        })

    async def serve_health(self):
        app = web.Application()
        app.router.add_get("/", self._health)
        app.router.add_get("/status", self._health)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", settings.PORT).start()

    async def run(self):
        await self.serve_health()
        await self.setup()
        await self.telegram.send(reports.format_startup(self), keyboard=True)
        await asyncio.gather(
            self.telegram.poll_forever(),
            self._desk_worker(),
            self._forever("strateji", STRATEGY_POLL_SECONDS, self.scan_strategies),
            self._forever("pozisyon", POSITION_POLL_SECONDS, self.update_positions),
            self._forever("haber", NEWS_POLL_SECONDS, self._news_step),
            self._forever("arbitraj", settings.ARB_SCAN_SECONDS, self._arb_perp_step),
            self._forever("dex", settings.ARB_DEX_SCAN_SECONDS, self._arb_dex_step),
            self._forever("kayıt", PERSIST_SECONDS, self._persist_step),
            self._backup_loop(),
            self._forever("uyanık", KEEP_AWAKE_SECONDS, self._keep_awake_step),
        )


async def amain():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    async with aiohttp.ClientSession() as session:
        bot = Bot(session)
        task = asyncio.create_task(bot.run())
        stop = asyncio.Event()
        try:
            # Render sends SIGTERM before replacing the instance. Windows has no such handler.
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        except NotImplementedError:
            pass
        waiter = asyncio.create_task(stop.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            logger.info("SIGTERM: saving state")
            bot.save_local()
            await bot.backup_now()
        task.cancel()
        waiter.cancel()


def main():
    asyncio.run(amain())
