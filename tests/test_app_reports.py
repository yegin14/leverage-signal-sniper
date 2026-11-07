import json
import time
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from tradebot import reports, settings
from tradebot.app import Bot, DeskRequest, bars_from, build_plan, drift_is_acceptable, venue_with_results
from tradebot.desk import Desk
from tradebot.news import NewsItem
from tradebot.paper import PaperBook, Plan, Quote
from tradebot.risk import Filters
from tradebot.settings import LIMITS
from tradebot.strategies import (FUNDING_EXITS, LIVE_STRATEGIES, SIGNAL_15M_EXITS, SOURCE_LABELS, TREND_4H_EXITS,
                                 funding_contrarian, signal_15m, trend_breakout)
from tradebot.telegram import BACKUP_FILENAME, COMMANDS, KEYBOARD, Telegram, normalize_command
from tradebot.venues import Book, Candles, DataError, Market
from tradebot.web import make_app, sanitize

FILTERS = Filters(5.0, 0.001)
NOW = 1_800_000_000.0


class FakeMarket:
    def __init__(self, price=100.0):
        self.price = price
        self.errors = {}
        self.fail = False
        self.book_calls = 0

    async def book(self, venue, symbol, depth=20):
        self.book_calls += 1
        if self.fail:
            raise DataError(venue, "HTTP 500")
        return Book(venue, symbol, [[self.price - 0.05, 1000]], [[self.price + 0.05, 1000]], time.time())

    async def tops(self, venue):
        if self.fail:
            raise DataError(venue, "HTTP 500")
        return {s: (self.price - 0.05, self.price + 0.05) for s in (*settings.SYMBOLS, "SOLUSDT")}

    async def filters(self, venue, symbol):
        return FILTERS

    async def funding(self, venue, symbol):
        return SimpleNamespace(rate=0.0001, next_ts=time.time() + 3600)

    async def candles(self, venue, symbol, interval, limit=200):
        now = time.time() * 1000
        ts = np.array([now - 60000 * i for i in range(limit, 0, -1)])
        c = np.full(limit, self.price)
        return Candles(ts, c, c + 0.1, c - 0.1, c, np.full(limit, 10.0))

    async def flow(self, symbol):
        return {"top_trader_long_pct": 55.0}

    async def fear_greed(self):
        return {"value": 25, "label": "Fear"}


class FakeSession:
    pass


def make_bot(tmp_path):
    bot = Bot(FakeSession(), desk=Desk(client=None, model="claude-opus-5-5"), data_dir=tmp_path)
    bot.market = FakeMarket()
    bot.cex_venue = "binance"
    bot._make_books(None)
    bot.sent = []

    async def capture(text, keyboard=False):
        bot.sent.append(text)
    bot.telegram.send = capture
    return bot


def strategy(name):
    return next(s for s in LIVE_STRATEGIES if s.name == name)


# ------------------------------------------------------------------- plans

def test_plan_with_fixed_target():
    p = build_plan("S", "LONG", 100.0, 1.0, FUNDING_EXITS, LIMITS, "rules", "funding_1h", 1)
    assert p.stop == pytest.approx(98.0)
    assert p.target == pytest.approx(104.0)
    assert p.trail_atr is None
    assert p.max_hold_hours == 48


def test_plan_with_trailing_stop_has_no_target():
    p = build_plan("S", "SHORT", 100.0, 2.0, TREND_4H_EXITS, LIMITS, "rules", "trend_4h", 4)
    assert p.stop == pytest.approx(105.0)
    assert p.target is None and p.trail_atr == 3.0 and p.bar_hours == 4


def test_plan_stop_stays_inside_general_band():
    p = build_plan("S", "LONG", 100.0, 100.0, FUNDING_EXITS, LIMITS, "rules", "x", 1)
    assert p.stop == pytest.approx(100.0 * (1 - LIMITS.max_stop_pct / 100.0))


def test_plan_uses_the_strategy_own_stop_band():
    tight = build_plan("S", "LONG", 100.0, 0.1, SIGNAL_15M_EXITS, LIMITS, "x", "signal_15m", 0.25)
    wide = build_plan("S", "LONG", 100.0, 50.0, SIGNAL_15M_EXITS, LIMITS, "x", "signal_15m", 0.25)
    assert tight.stop == pytest.approx(98.75)
    assert wide.stop == pytest.approx(97.0)


def test_drift_guard():
    assert drift_is_acceptable("LONG", 100.0, 100.5, 2.0) is True
    assert drift_is_acceptable("LONG", 100.0, 101.0, 2.0) is False
    assert drift_is_acceptable("LONG", 100.0, 99.0, 2.0) is False
    assert drift_is_acceptable("SHORT", 100.0, 99.0, 2.0) is False


# ------------------------------------------------------------------- strategies

def series(closes):
    closes = np.array(closes, dtype=np.float64)
    ts = np.arange(len(closes)) * 3_600_000.0
    return bars_from("S", Candles(ts, closes, closes + 0.2, closes - 0.2, closes, np.full(len(closes), 100.0)), None, 3_600_000)


def test_trend_breakout_fires_long_on_new_high_in_uptrend():
    closes = list(np.linspace(100, 200, 260)) + [215.0]
    hits = trend_breakout(20, 50, 200)(series(closes))
    assert hits and hits[-1][0] == len(closes) - 1 and hits[-1][1] == "LONG"


def test_trend_breakout_ignores_breakout_against_trend():
    closes = list(np.linspace(200, 100, 260)) + [150.0]
    hits = trend_breakout(20, 50, 200)(series(closes))
    assert not hits or hits[-1][0] != len(closes) - 1


def test_trend_breakout_does_not_look_ahead():
    closes = list(np.linspace(100, 200, 300))
    full = trend_breakout(20)(series(closes))
    cut = trend_breakout(20)(series(closes[:280]))
    assert [h for h in full if h[0] < 280] == cut


def test_funding_contrarian_needs_extreme_and_cross():
    closes = list(np.linspace(100, 120, 100)) + [100.0]
    base = series(closes)
    high = np.array([[0.0, 0.001]])
    bars = bars_from("S", Candles(base.ts, base.opens, base.highs, base.lows, base.closes, base.volumes), high, 3_600_000)
    hits = funding_contrarian(0.0005, -0.0002)(bars)
    assert hits and hits[-1][1] == "SHORT" and hits[-1][0] == len(closes) - 1
    calm = bars_from("S", Candles(base.ts, base.opens, base.highs, base.lows, base.closes, base.volumes),
                     np.array([[0.0, 0.0001]]), 3_600_000)
    assert funding_contrarian(0.0005, -0.0002)(calm) == []


def test_signal_15m_only_reads_the_newest_bar():
    rng = np.random.default_rng(1)
    closes = 100 + np.cumsum(rng.normal(0.2, 0.3, 200))
    hits = signal_15m(25)(series(list(closes)))
    assert all(h[0] == len(closes) - 1 for h in hits)


def test_every_live_strategy_has_a_source_label():
    assert list(SOURCE_LABELS)[:4] == [s.name for s in LIVE_STRATEGIES]
    assert {"news", "desk"} <= set(SOURCE_LABELS)


# ------------------------------------------------------------------- bot wiring

def test_each_source_has_a_cex_and_a_dex_book(tmp_path):
    bot = make_bot(tmp_path)
    assert set(bot.books) == {f"{s}_{k}" for s in SOURCE_LABELS for k in ("cex", "dex")}
    assert bot.books["news_dex"].venue.name == "hyperliquid"
    assert bot.books["trend_4h_cex"].venue.name == "binance"


def test_venue_with_results_keeps_a_book_on_its_venue():
    assert venue_with_results(None) is None
    assert venue_with_results({"news_cex": {"venue": "bybit", "history": [], "open": []}}) is None
    assert venue_with_results({"news_cex": {"venue": "bybit", "history": [{"net": 1}], "open": []}}) == "bybit"
    assert venue_with_results({"desk_cex": {"venue": "binance", "history": [], "open": [{"symbol": "S"}]}}) == "binance"
    assert venue_with_results({"news_dex": {"venue": "hyperliquid", "history": [{"net": 1}], "open": []}}) is None


async def test_pick_cex_retries_before_giving_up_on_binance():
    market = Market(None)
    calls = []

    async def flaky(venue, symbol, depth=20):
        calls.append(venue)
        if len(calls) == 1:
            raise DataError(venue, "TimeoutError")
        return Book(venue, symbol, [[1, 1]], [[2, 1]], 0.0)
    market.book = flaky
    assert await market.pick_cex("auto", retry_seconds=0) == "binance"
    assert calls == ["binance", "binance"]


async def test_pick_cex_falls_back_when_binance_stays_blocked():
    market = Market(None)

    async def blocked(venue, symbol, depth=20):
        if venue == "binance":
            raise DataError(venue, "HTTP 451")
        return Book(venue, symbol, [[1, 1]], [[2, 1]], 0.0)
    market.book = blocked
    assert await market.pick_cex("auto", retry_seconds=0) == "bybit"


class RateLimited:
    status = 429

    def __init__(self, headers=None):
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class CountingSession:
    def __init__(self, response=None):
        self.posts = 0
        self.gets = 0
        self.response = response or RateLimited()

    def post(self, *args, **kwargs):
        self.posts += 1
        return self.response

    def get(self, *args, **kwargs):
        self.gets += 1
        return self.response


async def test_hyperliquid_rate_limit_stops_requests_for_a_while():
    session = CountingSession()
    market = Market(session)
    for _ in range(3):
        with pytest.raises(DataError):
            await market.book("hyperliquid", "SOLUSDT")
    assert session.posts == 1
    assert "429" in market.errors["hyperliquid"]


async def test_binance_ban_stops_requests_for_as_long_as_it_asks():
    banned = RateLimited({"Retry-After": "600"})
    banned.status = 418
    session = CountingSession(banned)
    market = Market(session)
    for _ in range(3):
        with pytest.raises(DataError):
            await market.book("binance", "SOLUSDT")
    assert session.gets == 1
    assert "418" in market.errors["binance"]
    assert market.backoff_until["binance"] - time.time() > 500
    # Another venue is not affected by Binance's ban.
    assert time.time() >= market.backoff_until.get("bybit", 0.0)


async def test_a_ban_with_an_unreadable_wait_still_waits():
    banned = RateLimited({"Retry-After": "soon"})
    banned.status = 418
    market = Market(CountingSession(banned))
    with pytest.raises(DataError):
        await market.book("binance", "SOLUSDT")
    assert market.backoff_until["binance"] > time.time()


async def test_signals_survive_a_blocked_venue(tmp_path):
    bot = make_bot(tmp_path)
    asked = []
    original = bot.market.candles

    async def blocked(venue, symbol, interval, limit=200):
        asked.append(venue)
        if venue == "binance":
            raise DataError("binance", "HTTP 418")
        return await original(venue, symbol, interval, limit)
    bot.market.candles = blocked
    bars = await bot._bars("SOLUSDT", strategy("trend_4h"))
    assert len(bars.ts) > 0
    assert asked == ["binance", "bybit"]


async def test_reachability_is_asked_again_and_reaches_the_scanner(tmp_path):
    from tradebot.arbitrage import ArbScanner
    bot = make_bot(tmp_path)
    bot.scanner = ArbScanner(bot.market, None, bot.arb, [], [])
    bot.market.spot_book = bot.market.book
    await bot.refresh_reachability()
    assert bot.scanner.cex_venues == ("binance", "bybit")
    bot.market.fail = True
    await bot.refresh_reachability()
    assert bot.scanner.cex_venues == () and bot.perp_venues == []


def test_saved_book_on_other_venue_is_not_reused(tmp_path):
    bot = make_bot(tmp_path)
    saved = {"news_cex": {**bot.books["news_cex"].state, "venue": "bybit", "balance": 5.0}}
    bot.books = {}
    bot._make_books(saved)
    assert bot.books["news_cex"].state["balance"] != 5.0


def test_snapshot_round_trip(tmp_path):
    bot = make_bot(tmp_path)
    bot.books["news_cex"].state["balance"] = 77.0
    bot.log("sistem", "deneme")
    bot.sample_curve()
    bot.save_local()
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert data["books"]["news_cex"]["balance"] == 77.0
    assert data["version"] == 3
    assert data["events"][0]["text"] == "deneme"
    assert data["curve"][0]["e"]["news_cex"] == 77.0


def test_curve_thins_old_points_and_keeps_its_time_range(tmp_path):
    bot = make_bot(tmp_path)
    bot.curve = [{"t": i, "e": {}} for i in range(1500)]
    bot.sample_curve()
    assert len(bot.curve) < 1500
    assert bot.curve[0]["t"] == 0
    assert bot.curve[-2]["t"] == 1499


async def test_strategy_opens_only_on_its_own_books(tmp_path):
    bot = make_bot(tmp_path)
    s = strategy("funding_1h")
    await bot.open_for_source(s.name, "SOLUSDT", "LONG", 1.0, s.exits, s.name, s.hours, "neden")
    assert len(bot.books["funding_1h_cex"].state["open"]) == 1
    assert len(bot.books["funding_1h_dex"].state["open"]) == 1
    assert bot.books["trend_4h_cex"].state["open"] == []
    assert bot.books["desk_cex"].state["open"] == []


async def test_trades_stay_out_of_telegram_and_go_to_the_event_log(tmp_path):
    bot = make_bot(tmp_path)
    s = strategy("funding_1h")
    await bot.open_for_source(s.name, "SOLUSDT", "LONG", 1.0, s.exits, s.name, s.hours, "neden")
    bot.market.price = 90.0
    await bot.update_positions()
    assert bot.sent == []
    kinds = [e["kind"] for e in bot.events]
    assert "açılış" in kinds and "kapanış" in kinds


async def test_no_limit_on_position_count(tmp_path):
    bot = make_bot(tmp_path)
    s = strategy("funding_1h")
    symbols = [f"S{i}" for i in range(12)]
    for symbol in symbols:
        await bot.open_for_source(s.name, symbol, "LONG", 1.0, s.exits, s.name, s.hours, "neden")
    book = bot.books["funding_1h_cex"]
    assert len(book.state["open"]) >= 9
    # Only money can stop a trade, and the reason is recorded.
    assert set(book.state["rejected"]) <= {"serbest teminat yetersiz"}


async def test_data_failure_is_logged_and_no_trade_opens(tmp_path):
    bot = make_bot(tmp_path)
    bot.market.fail = True
    s = strategy("funding_1h")
    results = await bot.open_for_source(s.name, "SOLUSDT", "LONG", 1.0, s.exits, s.name, s.hours, "neden")
    assert bot.books["funding_1h_cex"].state["open"] == []
    assert "veri yok" in results["funding_1h_cex"]["rejected"]
    assert any("veri yok" in e["text"] for e in bot.events)


async def test_positions_close_through_update_loop(tmp_path):
    bot = make_bot(tmp_path)
    s = strategy("funding_1h")
    await bot.open_for_source(s.name, "SOLUSDT", "LONG", 1.0, s.exits, s.name, s.hours, "neden")
    bot.market.price = 97.5  # Past the 2% stop, not as far as liquidation.
    await bot.update_positions()
    book = bot.books["funding_1h_cex"]
    assert book.state["open"] == []
    assert book.state["history"][0]["exit_reason"] == "stop"


async def test_gap_past_liquidation_loses_the_margin(tmp_path):
    bot = make_bot(tmp_path)
    s = strategy("funding_1h")
    await bot.open_for_source(s.name, "SOLUSDT", "LONG", 1.0, s.exits, s.name, s.hours, "neden")
    book = bot.books["funding_1h_cex"]
    margin = book.state["open"][0]["margin"]
    bot.market.price = 80.0
    await bot.update_positions()
    [trade] = book.state["history"]
    assert trade["exit_reason"] == "tasfiye"
    assert trade["gross"] == pytest.approx(-margin)


async def test_polling_fetches_a_real_book_only_to_fill_an_exit(tmp_path):
    bot = make_bot(tmp_path)
    s = strategy("funding_1h")
    await bot.open_for_source(s.name, "SOLUSDT", "LONG", 1.0, s.exits, s.name, s.hours, "neden")
    before = bot.market.book_calls
    await bot.update_positions()
    await bot.update_positions()
    assert bot.market.book_calls == before
    bot.market.price = 90.0
    await bot.update_positions()
    assert bot.market.book_calls > before


async def test_exit_waits_when_real_book_is_unavailable(tmp_path):
    bot = make_bot(tmp_path)
    s = strategy("funding_1h")
    await bot.open_for_source(s.name, "SOLUSDT", "LONG", 1.0, s.exits, s.name, s.hours, "neden")
    bot.market.price = 90.0
    original = bot.market.book

    async def broken(venue, symbol, depth=20):
        raise DataError(venue, "HTTP 500")
    bot.market.book = broken
    await bot.update_positions()
    assert len(bot.books["funding_1h_cex"].state["open"]) == 1
    bot.market.book = original
    await bot.update_positions()
    assert bot.books["funding_1h_cex"].state["open"] == []


def _wicked(market, low):
    original = market.candles

    async def candles(venue, symbol, interval, limit=200):
        c = await original(venue, symbol, interval, limit)
        return Candles(c.ts, c.opens, c.highs, np.full(len(c.lows), low), c.closes, c.volumes)
    market.candles = candles


async def test_wick_before_position_opened_does_not_stop_it(tmp_path):
    bot = make_bot(tmp_path)
    book = bot.books["news_cex"]
    plan = Plan("SOLUSDT", "LONG", 99.0, None, "news", max_hold_hours=100)
    book.try_open(plan, Quote([[100.0, 1000]], [[100.1, 1000]], time.time()), FILTERS)
    _wicked(bot.market, 90.0)
    await bot.update_positions()
    assert len(book.state["open"]) == 1


async def test_wick_after_position_opened_stops_it(tmp_path):
    bot = make_bot(tmp_path)
    book = bot.books["news_cex"]
    plan = Plan("SOLUSDT", "LONG", 99.0, None, "news", max_hold_hours=100)
    book.try_open(plan, Quote([[100.0, 1000]], [[100.1, 1000]], time.time()), FILTERS)
    book.state["open"][0]["range_from_ms"] = 0
    _wicked(bot.market, 90.0)
    await bot.update_positions()
    assert book.state["open"] == []
    assert book.state["history"][0]["exit_reason"] == "stop"


async def test_signals_fall_back_to_hyperliquid_without_a_cex(tmp_path):
    bot = make_bot(tmp_path)
    bot.cex_venue = None
    assert bot.signal_venue == "hyperliquid"
    assert len((await bot._bars("SOLUSDT", strategy("trend_4h"))).ts) > 0
    with pytest.raises(DataError):
        await bot._bars("SOLUSDT", strategy("funding_1h"))


async def test_old_signal_is_not_traded_after_a_restart(tmp_path):
    bot = make_bot(tmp_path)
    s = strategy("trend_4h")
    closes = np.array(list(np.linspace(100, 200, 260)) + [215.0])
    ts = (time.time() * 1000 // 14_400_000 - len(closes) - 3) * 14_400_000 + np.arange(len(closes)) * 14_400_000.0
    bars = bars_from("SOLUSDT", Candles(ts, closes, closes + 0.2, closes - 0.2, closes, np.full(len(closes), 100.0)),
                     None, 14_400_000)
    assert s.make(bars)[-1][0] == len(closes) - 1
    await bot._after_bar(s, bars)
    assert all(b.state["open"] == [] for b in bot.books.values())


async def test_fresh_signal_opens_its_books(tmp_path):
    bot = make_bot(tmp_path)
    s = strategy("trend_4h")
    closes = np.array(list(np.linspace(100, 200, 260)) + [215.0])
    last_close_ms = time.time() * 1000 // 14_400_000 * 14_400_000
    ts = last_close_ms - 14_400_000 * (len(closes) - np.arange(len(closes)))
    bars = bars_from("SOLUSDT", Candles(ts, closes, closes + 0.2, closes - 0.2, closes, np.full(len(closes), 100.0)),
                     None, 14_400_000)
    if time.time() - (bars.ts[-1] + bars.bar_ms) / 1000.0 > 300:
        pytest.skip("Too far past the 4-hour boundary for a fresh signal.")
    await bot._after_bar(s, bars)
    assert len(bot.books["trend_4h_cex"].state["open"]) == 1


# ------------------------------------------------------------------- news trades

def headline(sentiment, symbols=("SOLUSDT",), id="n1", title="Solana haberi"):
    return NewsItem(id, title, "S", "https://x.test/a", time.time(), list(symbols), 2, False, sentiment)


async def test_good_news_goes_long_and_bad_news_goes_short(tmp_path):
    bot = make_bot(tmp_path)
    bot.news.ingest([headline(1), headline(-1, ("ETHUSDT",), id="n2", title="Ethereum haberi")], time.time())
    await bot.trade_news()
    by_symbol = {p["symbol"]: p for p in bot.books["news_cex"].state["open"]}
    assert by_symbol["SOLUSDT"]["direction"] == "LONG"
    assert by_symbol["ETHUSDT"]["direction"] == "SHORT"
    assert len(bot.books["news_dex"].state["open"]) == 2
    assert bot.books["desk_cex"].state["open"] == []


async def test_news_trade_records_what_happened_and_fires_once(tmp_path):
    bot = make_bot(tmp_path)
    bot.news.ingest([headline(1)], time.time())
    await bot.trade_news()
    [action] = bot.news.actions["n1"]
    assert action["direction"] == "LONG"
    assert set(action["opened"]) == {"binance", "hyperliquid"}
    await bot.trade_news()
    assert len(bot.news.actions["n1"]) == 1


async def test_unclear_news_opens_nothing(tmp_path):
    bot = make_bot(tmp_path)
    bot.news.ingest([headline(0)], time.time())
    await bot.trade_news()
    assert bot.books["news_cex"].state["open"] == []
    assert "n1" not in bot.news.actions


async def test_news_data_failure_is_recorded(tmp_path):
    bot = make_bot(tmp_path)
    bot.news.ingest([headline(1)], time.time())

    async def broken(*args, **kwargs):
        raise DataError("binance", "HTTP 500")
    bot.market.candles = broken
    await bot.trade_news()
    assert "veri yok" in bot.news.actions["n1"][0]["rejected"]["hepsi"]


# ------------------------------------------------------------------- desk

async def test_desk_request_without_key_opens_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    bot = make_bot(tmp_path)
    req = DeskRequest("SOLUSDT", "trend_4h", "x", "LONG", {"last_close": 100.0}, 1.0, TREND_4H_EXITS, 4)
    await bot._queue_request(req)
    assert bot.desk_queue.empty()


async def test_desk_long_decision_opens_on_desk_books_with_half_size(tmp_path):
    bot = make_bot(tmp_path)
    record = {"id": "s1", "action": "LONG", "reason": "Masa onayladı", "size_factor": 0.5, "symbol": "SOLUSDT",
              "label": "x", "cost_usd": 0.1, "turns": []}

    async def run_session(candidate, now):
        bot.seen_candidate = candidate
        return record
    bot.desk.run_session = run_session
    req = DeskRequest("SOLUSDT", "funding_1h", "x", "LONG", {"last_close": 100.0}, 1.0, FUNDING_EXITS, 1)
    await bot.run_desk_request(req)
    pos = bot.books["desk_cex"].state["open"][0]
    assert pos["session_id"] == "s1" and pos["origin"] == "desk"
    assert bot.books["funding_1h_cex"].state["open"] == []
    full = PaperBook("x", "binance", "desk", LIMITS)
    q = Quote([[99.95, 1000]], [[100.05, 1000]], time.time())
    p_full = full.try_open(Plan("SOLUSDT", "LONG", pos["stop"], pos["target"], "desk"), q, FILTERS)["opened"]
    assert pos["qty"] == pytest.approx(p_full["qty"] / 2, abs=0.002)
    assert "break_even_win_rate_pct" in str(bot.seen_candidate.briefing)
    assert "top_trader_long_pct" in str(bot.seen_candidate.briefing)


async def test_desk_decision_is_dropped_when_price_ran_away(tmp_path):
    bot = make_bot(tmp_path)

    async def run_session(candidate, now):
        bot.market.price = 110.0
        return {"id": "s1", "action": "LONG", "reason": "ok", "size_factor": 1.0, "symbol": "SOLUSDT",
                "label": "x", "cost_usd": 0.1, "turns": []}
    bot.desk.run_session = run_session
    req = DeskRequest("SOLUSDT", "funding_1h", "x", "LONG", {"last_close": 100.0}, 1.0, FUNDING_EXITS, 1)
    await bot.run_desk_request(req)
    assert bot.books["desk_cex"].state["open"] == []
    assert any("çok oynadı" in e["text"] for e in bot.events)


async def test_reset_keeps_llm_spend_and_clears_paper(tmp_path):
    bot = make_bot(tmp_path)
    bot.desk.state["spent_total"] = 3.0
    bot.books["news_cex"].state["balance"] = 50.0
    bot.sample_curve()
    bot.reset_paper()
    assert bot.books["news_cex"].state["balance"] == pytest.approx(99.0)
    assert bot.desk.state["spent_total"] == 3.0
    assert bot.curve == []


# ------------------------------------------------------------------- short telegram texts

def test_status_is_short_says_paper_and_explains_a_closed_desk(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    bot = make_bot(tmp_path)
    text = reports.format_status(bot)
    assert "SAHTE" in text and "ANTHROPIC_API_KEY" in text
    assert len(text) < 1500


def test_digest_totals_match_books(tmp_path):
    bot = make_bot(tmp_path)
    bot.books["news_cex"].state["balance"] = 110.0
    t = reports.totals(bot)
    expected = sum(b.equity() - b.state["initial"] for b in bot.books.values())
    assert t["net"] == pytest.approx(expected)
    assert reports.money(t["net"]) in reports.format_digest(bot)


def test_split_message_respects_limit_and_keeps_text():
    text = "\n".join(f"satır {i}" * 50 for i in range(100)) + "\n" + "x" * 9000
    chunks = reports.split_message(text)
    assert all(len(c) <= 3800 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


# ------------------------------------------------------------------- telegram

def telegram(tmp_path):
    bot = make_bot(tmp_path)
    t = Telegram(FakeSession(), bot, token="x", chat_id="42")
    t.sent = []

    async def capture(text, keyboard=False):
        t.sent.append(text)
    t.send = capture
    return t, bot


def test_every_keyboard_button_is_a_command():
    for row in KEYBOARD["keyboard"]:
        for button in row:
            assert normalize_command(button["text"]) in COMMANDS


def test_turkish_letters_match_commands(tmp_path):
    t, _ = telegram(tmp_path)
    for text in ("YARDIM", "Yardım", "SIFIRLA", "📊 Durum"):
        assert "tanımıyorum" not in t.build_reply(text)


def test_every_command_builds_a_reply(tmp_path):
    t, _ = telegram(tmp_path)
    for text in COMMANDS:
        if COMMANDS[text] != "reset_do":
            assert t.build_reply(text)


def test_moved_commands_point_to_the_panel(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EXTERNAL_URL", "https://panel.test")
    t, _ = telegram(tmp_path)
    for command in ("/pozisyon", "/islemler", "/masa", "/haber", "/arbitraj", "/maliyet", "/test", "/kanit"):
        assert "https://panel.test" in t.build_reply(command)


def test_unknown_text_does_not_match_by_substring(tmp_path):
    t, _ = telegram(tmp_path)
    assert "tanımıyorum" in t.build_reply("durum nedir acaba")
    assert "tanımıyorum" in t.build_reply("kasayı sıfırla lütfen")


async def test_only_owner_chat_is_served(tmp_path):
    t, _ = telegram(tmp_path)
    update = {"message": {"chat": {"id": 7}, "text": "/durum", "date": NOW}}
    assert await t.handle_update(update, now=NOW) is None
    assert t.sent == []


async def test_stale_message_is_ignored(tmp_path):
    t, bot = telegram(tmp_path)
    bot.books["news_cex"].state["balance"] = 50.0
    update = {"message": {"chat": {"id": 42}, "text": "/sifirla onay", "date": NOW - 3600}}
    assert await t.handle_update(update, now=NOW) is None
    assert bot.books["news_cex"].state["balance"] == 50.0


async def test_reset_needs_explicit_confirmation(tmp_path):
    t, bot = telegram(tmp_path)
    bot.books["news_cex"].state["balance"] = 50.0
    assert "onay" in t.build_reply("/sifirla")
    assert bot.books["news_cex"].state["balance"] == 50.0
    t.build_reply("/sifirla onay")
    assert bot.books["news_cex"].state["balance"] == pytest.approx(99.0)


async def test_report_bug_gives_error_not_numbers(tmp_path):
    t, bot = telegram(tmp_path)
    bot.books = None
    reply = await t.handle_update({"message": {"chat": {"id": 42}, "text": "/durum", "date": NOW}}, now=NOW)
    assert "çalıştırılamadı" in reply


class BackupApi(Telegram):
    """A Telegram client with the network replaced, for testing restore decisions."""

    def __init__(self, chat, downloads):
        super().__init__(FakeSession(), None, token="x", chat_id="42")
        self.chat, self.downloads = chat, downloads

    async def _api(self, method, timeout=15.0, **fields):
        return self.chat.pop(0) if method == "getChat" else None

    async def _download(self, file_id):
        return self.downloads.pop(0)


PINNED = {"pinned_message": {"message_id": 9, "document": {"file_name": BACKUP_FILENAME, "file_id": "f"}}}


async def test_restore_reads_pinned_backup_and_keeps_its_id():
    t = BackupApi([PINNED], [{"version": 2}])
    assert await t.load_backup() == {"version": 2}
    assert t.backup_message_id == 9


async def test_unreadable_backup_is_never_overwritten(monkeypatch):
    monkeypatch.setattr("tradebot.telegram.RESTORE_RETRY_SECONDS", 0)
    t = BackupApi([PINNED, PINNED, PINNED], [None, None, None])
    assert await t.load_backup() is None
    assert t.backup_message_id is None  # So the next save makes a new file.


async def test_restore_retries_after_temporary_failure(monkeypatch):
    monkeypatch.setattr("tradebot.telegram.RESTORE_RETRY_SECONDS", 0)
    t = BackupApi([None, PINNED], [{"version": 2}])
    assert await t.load_backup() == {"version": 2}
    assert t.backup_message_id == 9


async def test_no_pinned_backup_starts_fresh():
    t = BackupApi([{"pinned_message": {"message_id": 1, "text": "hi"}}], [])
    assert await t.load_backup() is None
    assert t.backup_message_id is None


# ------------------------------------------------------------------- web panel

def test_sanitize_removes_values_json_cannot_hold():
    assert sanitize({"a": float("inf"), "b": [float("nan"), 1.5], "c": "x"}) == {"a": None, "b": [None, 1.5], "c": "x"}


@pytest.fixture
async def panel(tmp_path):
    bot = make_bot(tmp_path)
    client = TestClient(TestServer(make_app(bot)))
    await client.start_server()
    yield client, bot
    await client.close()


async def test_panel_page_and_assets_load_with_a_strict_policy(panel):
    client, _ = panel
    page = await client.get("/")
    assert page.status == 200 and "text/html" in page.headers["Content-Type"]
    assert "SAHTE PARA" in await page.text()
    policy = page.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy and "unsafe-inline" not in policy
    for path, kind in (("/static/app.js", "javascript"), ("/static/style.css", "css")):
        response = await client.get(path)
        assert response.status == 200 and kind in response.headers["Content-Type"]


async def test_panel_serves_only_its_own_files(panel):
    client, _ = panel
    assert (await client.get("/static/secret.env")).status == 404
    assert (await client.get("/static/..%2f..%2fsettings.py")).status == 404


async def test_panel_has_no_way_to_change_anything(panel):
    client, _ = panel
    for path in ("/", "/api/state", "/api/curve", "/status"):
        assert (await client.post(path)).status == 405


async def test_state_reports_positions_and_trades_truthfully(panel):
    client, bot = panel
    book = bot.books["news_cex"]
    book.try_open(Plan("SOLUSDT", "LONG", 98.0, 104.0, "news", "news", "Haber: x"),
                  Quote([[100.0, 1000]], [[100.1, 1000]], NOW), FILTERS, now=NOW)
    book.try_open(Plan("ETHUSDT", "LONG", 98.0, 104.0, "news", "news", "Haber: y"),
                  Quote([[100.0, 1000]], [[100.1, 1000]], NOW), FILTERS, now=NOW)
    [closed] = book.on_quote("ETHUSDT", Quote([[104.5, 1000]], [[104.6, 1000]], NOW + 60))
    response = await client.get("/api/state")
    state = await response.json()
    assert state["mode"] == "paper"
    row = next(b for b in state["books"] if b["key"] == "news_cex")
    assert row["trades"] == 1 and row["open"] == 1
    assert row["balance"] == pytest.approx(book.state["balance"])
    assert row["total_net"] == pytest.approx(book.equity() - 100.0)
    [position] = state["positions"]
    assert position["symbol"] == "SOLUSDT" and position["source_label"] == "Haber"
    p = book.state["open"][0]
    gross = (p["last_mark"] - p["entry"]) * p["qty"]
    assert position["gross_now"] == pytest.approx(gross)
    assert position["net_now"] < gross
    [trade] = state["trades"]
    assert trade["net"] == pytest.approx(closed["net"])
    assert trade["fees"] == pytest.approx(closed["entry_fee"] + closed["exit_fee"])


async def test_state_json_is_valid_when_a_book_has_no_losses(panel):
    client, bot = panel
    book = bot.books["news_cex"]
    book.try_open(Plan("ETHUSDT", "LONG", 98.0, 104.0, "news"), Quote([[100.0, 1000]], [[100.1, 1000]], NOW), FILTERS, now=NOW)
    book.on_quote("ETHUSDT", Quote([[104.5, 1000]], [[104.6, 1000]], NOW + 60))
    response = await client.get("/api/state")
    assert "Infinity" not in await response.text()
    state = await response.json()
    assert next(b for b in state["books"] if b["key"] == "news_cex")["profit_factor"] is None


async def test_state_shows_news_with_its_action_and_desk_reason(panel, monkeypatch):
    client, bot = panel
    bot.news.ingest([headline(1)], time.time())
    await bot.trade_news()
    state = await (await client.get("/api/state")).json()
    [item] = state["news"]["items"]
    assert item["sentiment"] == 1
    assert set(item["actions"][0]["opened"]) == {"binance", "hyperliquid"}
    assert "desk" in state and "arbitrage" in state and "evidence" in state
    assert len(state["arbitrage"]["kinds"]) == 5


async def test_curve_endpoint_gives_points_and_book_identity(panel):
    client, bot = panel
    bot.sample_curve()
    data = await (await client.get("/api/curve")).json()
    assert len(data["points"]) == 1
    assert data["books"]["news_dex"] == {"source": "news", "kind": "DEX", "initial": 100.0}


async def test_status_endpoint_stays_small_for_health_checks(panel):
    client, _ = panel
    data = await (await client.get("/status")).json()
    assert data["ok"] is True and data["mode"] == "paper"
