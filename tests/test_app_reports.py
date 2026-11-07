import time
from types import SimpleNamespace

import numpy as np
import pytest

from tradebot import reports
from tradebot.app import Bot, DeskRequest, bars_from, build_plan, drift_is_acceptable
from tradebot.desk import Desk
from tradebot.paper import PaperBook, Plan, Quote
from tradebot.risk import Filters
from tradebot.settings import LIMITS
from tradebot.strategies import FUNDING_EXITS, LIVE_STRATEGIES, TREND_4H_EXITS, funding_contrarian, trend_breakout
from tradebot.telegram import BACKUP_FILENAME, COMMANDS, KEYBOARD, Telegram, normalize_command
from tradebot.venues import Book, Candles, DataError

FILTERS = Filters(5.0, 0.001)
NOW = 1_800_000_000.0


class FakeMarket:
    def __init__(self, price=100.0):
        self.price = price
        self.errors = {}
        self.fail = False

    async def book(self, venue, symbol, depth=20):
        if self.fail:
            raise DataError(venue, "HTTP 500")
        return Book(venue, symbol, [[self.price - 0.05, 1000]], [[self.price + 0.05, 1000]], time.time())

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


def make_bot(tmp_path, answers=None):
    bot = Bot.__new__(Bot)
    Bot.__init__(bot, FakeSession(), desk=Desk(client=None, model="claude-opus-5-5"), data_dir=tmp_path)
    bot.market = FakeMarket()
    bot.cex_venue = "binance"
    bot._make_books(None)
    sent = []

    async def capture(text, keyboard=False):
        sent.append(text)
    bot.telegram.send = capture
    bot.sent = sent
    return bot


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


def test_plan_stop_stays_inside_band():
    p = build_plan("S", "LONG", 100.0, 100.0, FUNDING_EXITS, LIMITS, "rules", "x", 1)
    assert p.stop == pytest.approx(100.0 * (1 - LIMITS.max_stop_pct / 100.0))


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
    bars = series(closes)
    hits = trend_breakout(20, 50, 200)(bars)
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


def test_live_strategies_declare_what_rules_may_trade():
    by = {s.name: s for s in LIVE_STRATEGIES}
    assert by["trend_4h"].rules_trade and by["funding_1h"].rules_trade
    assert by["trend_1h"].rules_trade is False  # It lost money on its own: desk candidates only.


# ------------------------------------------------------------------- bot wiring

def test_four_paper_books_with_separate_venues(tmp_path):
    bot = make_bot(tmp_path)
    assert set(bot.books) == {"rules_cex", "rules_dex", "desk_cex", "desk_dex"}
    assert bot.books["rules_dex"].venue.name == "hyperliquid"
    assert bot.books["desk_cex"].venue.name == "binance"


def test_saved_book_on_other_venue_is_not_reused(tmp_path):
    bot = make_bot(tmp_path)
    saved = {"rules_cex": {**bot.books["rules_cex"].state, "venue": "bybit", "balance": 5.0}}
    bot.books = {}
    bot._make_books(saved)
    assert bot.books["rules_cex"].state["balance"] != 5.0


def test_snapshot_round_trip(tmp_path):
    bot = make_bot(tmp_path)
    bot.books["rules_cex"].state["balance"] = 77.0
    bot.save_local()
    assert (tmp_path / "state.json").exists()
    import json
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert data["books"]["rules_cex"]["balance"] == 77.0
    assert data["version"] == 2


async def test_rules_strategy_opens_on_rules_books_only(tmp_path):
    bot = make_bot(tmp_path)
    strategy = next(s for s in LIVE_STRATEGIES if s.name == "funding_1h")
    await bot._open_rules(strategy, "SOLUSDT", "LONG", 1.0)
    assert len(bot.books["rules_cex"].state["open"]) == 1
    assert len(bot.books["rules_dex"].state["open"]) == 1
    assert bot.books["desk_cex"].state["open"] == []
    assert any("SAHTE İŞLEM AÇILDI" in m for m in bot.sent)


async def test_data_failure_is_reported_and_no_trade_opens(tmp_path):
    bot = make_bot(tmp_path)
    bot.market.fail = True
    strategy = next(s for s in LIVE_STRATEGIES if s.name == "funding_1h")
    await bot._open_rules(strategy, "SOLUSDT", "LONG", 1.0)
    assert bot.books["rules_cex"].state["open"] == []
    assert any("veri yok" in m for m in bot.sent)


async def test_positions_close_through_update_loop_and_notify(tmp_path):
    bot = make_bot(tmp_path)
    strategy = next(s for s in LIVE_STRATEGIES if s.name == "funding_1h")
    await bot._open_rules(strategy, "SOLUSDT", "LONG", 1.0)
    bot.market.price = 90.0
    await bot.update_positions()
    assert bot.books["rules_cex"].state["open"] == []
    assert bot.books["rules_cex"].state["history"][0]["exit_reason"] == "stop"
    assert any("SAHTE İŞLEM KAPANDI" in m for m in bot.sent)


async def test_wick_before_position_opened_does_not_stop_it(tmp_path):
    """Candles from before entry must be ignored, whichever book opened first."""
    bot = make_bot(tmp_path)
    book = bot.books["rules_cex"]
    plan = Plan("SOLUSDT", "LONG", 99.0, None, "rules", max_hold_hours=100)
    q = Quote([[100.0, 1000]], [[100.1, 1000]], time.time())
    book.try_open(plan, q, FILTERS)
    # FakeMarket candle lows are price - 0.1. Make them cross the stop for candles before entry only.
    original = bot.market.candles

    async def wicked(venue, symbol, interval, limit=200):
        c = await original(venue, symbol, interval, limit)
        lows = c.lows.copy()
        lows[:-1] = 90.0  # Everything older than the newest candle wicked through the stop.
        return Candles(c.ts, c.opens, c.highs, lows, c.closes, c.volumes)
    bot.market.candles = wicked
    bot.market.price = 100.0
    await bot.update_positions()
    assert len(book.state["open"]) == 1


async def test_signals_fall_back_to_hyperliquid_without_a_cex(tmp_path):
    bot = make_bot(tmp_path)
    bot.cex_venue = None
    assert bot.signal_venue == "hyperliquid"
    bars = await bot._bars("SOLUSDT", next(s for s in LIVE_STRATEGIES if s.name == "trend_4h"))
    assert len(bars.ts) > 0
    with pytest.raises(DataError):
        await bot._bars("SOLUSDT", next(s for s in LIVE_STRATEGIES if s.name == "funding_1h"))


async def test_old_signal_is_not_traded_after_a_restart(tmp_path):
    bot = make_bot(tmp_path)
    strategy = next(s for s in LIVE_STRATEGIES if s.name == "trend_4h")
    closes = np.array(list(np.linspace(100, 200, 260)) + [215.0])
    ts = (time.time() * 1000 // 14_400_000 - len(closes) - 3) * 14_400_000 + np.arange(len(closes)) * 14_400_000.0
    bars = bars_from("SOLUSDT", Candles(ts, closes, closes + 0.2, closes - 0.2, closes, np.full(len(closes), 100.0)),
                     None, 14_400_000)
    assert strategy.make(bars)[-1][0] == len(closes) - 1
    await bot._after_bar(strategy, bars)
    assert all(b.state["open"] == [] for b in bot.books.values())


async def test_desk_request_without_key_opens_nothing(tmp_path):
    bot = make_bot(tmp_path)
    req = DeskRequest("SOLUSDT", "trend_4h", "x", "LONG", {"last_close": 100.0}, 1.0, TREND_4H_EXITS, 4)
    await bot._queue_request(req)
    assert bot.desk_queue.empty()


async def test_desk_long_decision_opens_on_desk_books_with_half_size_and_records_session(tmp_path):
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
    assert pos["session_id"] == "s1"
    assert pos["origin"] == "desk"
    assert bot.books["rules_cex"].state["open"] == []
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
    assert any("çok oynadı" in m for m in bot.sent)


async def test_reset_keeps_llm_spend(tmp_path):
    bot = make_bot(tmp_path)
    bot.desk.state["spent_total"] = 3.0
    bot.books["rules_cex"].state["balance"] = 50.0
    bot.reset_paper()
    assert bot.books["rules_cex"].state["balance"] == pytest.approx(99.0)
    assert bot.desk.state["spent_total"] == 3.0


# ------------------------------------------------------------------- reports must tell the truth

def closed_trade(bot, name="rules_cex", net_bid=104.5):
    book = bot.books[name]
    plan = Plan("SOLUSDT", "LONG", 98.0, 104.0, "rules")
    q = Quote([[100.0, 1000]], [[100.1, 1000]], NOW)
    book.try_open(plan, q, FILTERS, now=NOW)
    return book.on_quote("SOLUSDT", Quote([[net_bid, 1000]], [[net_bid + 0.1, 1000]], NOW + 60))


def test_every_report_builds_on_empty_state_and_says_paper(tmp_path):
    bot = make_bot(tmp_path)
    for text in (reports.format_status(bot), reports.format_positions(bot), reports.format_trades(bot),
                 reports.format_arbitrage(bot), reports.format_costs(bot), reports.format_gate(bot)):
        assert "SAHTE" in text
    assert "Henüz kapanan işlem yok" in reports.format_trades(bot)
    assert "Açık pozisyon yok" in reports.format_positions(bot)
    assert "Henüz masa oturumu" in reports.format_session(None)
    assert "Henüz haber" in reports.format_news(bot)


def test_status_says_why_desk_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    bot = make_bot(tmp_path)
    assert "ANTHROPIC_API_KEY" in reports.format_status(bot)


def test_trade_report_matches_ledger(tmp_path):
    bot = make_bot(tmp_path)
    [t] = closed_trade(bot)
    text = reports.format_close_event(bot.books["rules_cex"], t)
    assert f"{t['net']:+,.2f}$" in text
    assert "SAHTE" in text
    assert f"{bot.books['rules_cex'].state['balance']:.2f}$" in text


def test_gate_fails_with_no_history_and_warns_it_is_not_proof(tmp_path):
    bot = make_bot(tmp_path)
    text = reports.format_gate(bot)
    assert "KANITLAMAZ" in text
    assert "✅ GEÇTİ" not in text


def test_gate_passes_only_when_every_check_passes(tmp_path):
    bot = make_bot(tmp_path)
    book = bot.books["rules_cex"]
    book.state["started_at"] = time.time() - 8 * 86400
    book.state["history"] = [{"net": 1.0, "closed_at": NOW} for _ in range(10)] + [{"net": -1.0, "closed_at": NOW}]
    book.state["wins"], book.state["losses"] = 10, 1
    row = next(r for r in reports.gate_result(bot) if r["book"] is book)
    assert row["passed"] is True
    book.state["max_drawdown"] = 50.0
    row = next(r for r in reports.gate_result(bot) if r["book"] is book)
    assert row["passed"] is False


def test_desk_gate_is_charged_for_llm_spend(tmp_path):
    bot = make_bot(tmp_path)
    book = bot.books["desk_cex"]
    book.state["started_at"] = time.time() - 8 * 86400
    book.state["history"] = [{"net": 1.0, "closed_at": NOW} for _ in range(10)] + [{"net": -1.0, "closed_at": NOW}]
    book.state["wins"], book.state["losses"] = 10, 1
    bot.desk.state["spent_total"] = 40.0
    row = next(r for r in reports.gate_result(bot) if r["book"] is book)
    assert row["passed"] is False


def test_session_report_shows_every_voice(tmp_path):
    record = {"symbol": "SOLUSDT", "label": "x", "action": "NO_TRADE", "reason": "Risk sorumlusu veto etti",
              "cost_usd": 0.12, "turns": [{"role": "Boğa", "stance": "LONG", "conviction": 70, "argument": "yükselir"},
                                          {"role": "Risk sorumlusu", "stance": "FLAT", "conviction": 50, "argument": "dur",
                                           "veto": True}]}
    text = reports.format_session(record)
    assert "İŞLEM YOK" in text and "yükselir" in text and "VETO" in text and "0.1200$" in text


def test_arbitrage_report_shows_errors_and_scans(tmp_path):
    bot = make_bot(tmp_path)
    bot.arb.record_error("dex_dex", "arbitrum: HTTP 429", time.time())
    text = reports.format_arbitrage(bot)
    assert "HTTP 429" in text
    assert "Henüz tarama yok" in text


def test_evidence_report_states_no_edge():
    text = reports.format_evidence()
    assert "rastgele" in text and "anlamlı" in text


def test_split_message_respects_limit_and_keeps_text():
    text = "\n".join(f"satır {i}" * 50 for i in range(100)) + "\n" + "x" * 9000
    chunks = reports.split_message(text)
    assert all(len(c) <= 3800 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


# ------------------------------------------------------------------- telegram

def telegram(tmp_path):
    bot = make_bot(tmp_path)
    t = Telegram(FakeSession(), bot, token="x", chat_id="42")
    sent = []

    async def capture(text, keyboard=False):
        sent.append(text)
    t.send = capture
    t.sent = sent
    return t, bot


def test_every_keyboard_button_is_a_command():
    for row in KEYBOARD["keyboard"]:
        for button in row:
            assert normalize_command(button["text"]) in COMMANDS


def test_turkish_letters_match_commands(tmp_path):
    t, _ = telegram(tmp_path)
    for text in ("🧾 İşlemler", "İŞLEMLER", "KANIT", "Kanıt", "Yardım"):
        assert "tanımıyorum" not in t.build_reply(text)


def test_every_command_builds_a_reply(tmp_path):
    t, _ = telegram(tmp_path)
    for text in COMMANDS:
        if COMMANDS[text] == "reset_do":
            continue
        assert t.build_reply(text)


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
    bot.books["rules_cex"].state["balance"] = 50.0
    update = {"message": {"chat": {"id": 42}, "text": "/sifirla onay", "date": NOW - 3600}}
    assert await t.handle_update(update, now=NOW) is None
    assert bot.books["rules_cex"].state["balance"] == 50.0


async def test_reset_needs_explicit_confirmation(tmp_path):
    t, bot = telegram(tmp_path)
    bot.books["rules_cex"].state["balance"] = 50.0
    assert "onay" in t.build_reply("/sifirla")
    assert bot.books["rules_cex"].state["balance"] == 50.0
    t.build_reply("/sifirla onay")
    assert bot.books["rules_cex"].state["balance"] == pytest.approx(99.0)


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
