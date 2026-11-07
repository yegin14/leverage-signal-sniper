import asyncio
import json
import time

import aiohttp
import pytest
from aiohttp import web

from tradebot import feeds as feeds_module
from tradebot.feeds import BinanceFeed, HyperliquidFeed
from tradebot.venues import DataError, Market

NOW = 1_800_000_000.0


def hl_book(coin="SOL", bid=100.0, ask=100.2):
    return json.dumps({"channel": "l2Book", "data": {"coin": coin, "time": 1, "levels": [
        [{"px": str(bid), "sz": "5", "n": 1}, {"px": str(bid - 1), "sz": "9", "n": 2}],
        [{"px": str(ask), "sz": "4", "n": 1}, {"px": str(ask + 1), "sz": "8", "n": 2}]]}}, separators=(",", ":"))


def hl_ctx(coin="SOL", funding="0.0000125"):
    return json.dumps({"channel": "activeAssetCtx", "data": {"coin": coin, "ctx": {"funding": funding, "markPx": "100"}}}, separators=(",", ":"))


def bn_book(symbol="SOLUSDT", bid="100.0", ask="100.2"):
    return json.dumps({"stream": f"{symbol.lower()}@depth10@500ms", "data": {"e": "depthUpdate", "s": symbol,
                       "b": [[bid, "5"], ["99.0", "9"]], "a": [[ask, "4"], ["101.2", "8"]]}}, separators=(",", ":"))


def bn_marks():
    return json.dumps({"stream": "!markPrice@arr", "data": [
        {"e": "markPriceUpdate", "s": "SOLUSDT", "r": "0.0001", "T": 1_800_003_600_000},
        {"e": "markPriceUpdate", "s": "OTHER", "r": "", "T": 0}]}, separators=(",", ":"))


def hl():
    return HyperliquidFeed(None, ["SOLUSDT", "BTCUSDT"])


def bn():
    return BinanceFeed(None, ["SOLUSDT", "BTCUSDT"])


def test_hyperliquid_book_is_parsed_on_read():
    f = hl()
    f.on_text(hl_book(), NOW)
    b = f.book("SOLUSDT", 20, NOW)
    assert b.venue == "hyperliquid" and b.symbol == "SOLUSDT"
    assert b.bids[0] == [100.0, 5.0] and b.asks[1] == [101.2, 8.0]
    assert f.book("SOLUSDT", 1, NOW).asks == [[100.2, 4.0]]


def test_binance_book_is_parsed_on_read():
    f = bn()
    f.on_text(bn_book(), NOW)
    b = f.book("SOLUSDT", 20, NOW)
    assert b.venue == "binance" and b.bids[0] == [100.0, 5.0] and b.asks[0] == [100.2, 4.0]


def test_unknown_messages_are_ignored_but_prove_the_connection_lives():
    f = hl()
    f.on_text('{"channel":"pong"}', NOW)
    f.on_text('{"channel":"subscriptionResponse","data":{}}', NOW)
    assert f.levels == {} and f.alive(NOW)


def test_no_book_means_no_answer():
    f = hl()
    f.on_text(hl_book(), NOW)
    assert f.book("BTCUSDT", 20, NOW) is None


def test_silent_connection_gives_no_prices():
    f = hl()
    f.on_text(hl_book(), NOW)
    assert f.book("SOLUSDT", 20, NOW + 60) is None
    assert f.tops(NOW + 60) == {}
    assert f.candles("SOLUSDT", 5, NOW + 60) is None
    assert f.alive(NOW + 60) is False


def test_quiet_symbol_stays_valid_while_the_connection_lives():
    f = hl()
    f.on_text(hl_book(), NOW)
    f.on_text(hl_book("BTC", 70000.0, 70001.0), NOW + 60)
    assert f.book("SOLUSDT", 20, NOW + 60).bids[0][0] == 100.0


def test_corrupt_message_keeps_the_last_good_book():
    f = hl()
    f.on_text(hl_book(), NOW)
    f.book("SOLUSDT", 20, NOW)
    f.on_text('{"channel":"l2Book","data":{"coin":"SOL","levels":"oops"}}', NOW + 1)
    assert f.book("SOLUSDT", 20, NOW + 1).bids[0][0] == 100.0
    assert "çözülemeyen" in f.last_error


def test_parsing_happens_at_most_once_per_second_on_receive():
    f = hl()
    calls = []
    original = f.parse_levels
    f.parse_levels = lambda text: calls.append(1) or original(text)
    f.on_text(hl_book(), NOW)
    for i in range(50):
        f.on_text(hl_book(bid=100.0 + i * 0.01), NOW + i * 0.001)
    assert len(calls) == 1
    # A reader still gets the newest book.
    assert f.book("SOLUSDT", 20, NOW + 0.1).bids[0][0] == pytest.approx(100.49)


def test_tops_lists_every_known_symbol():
    f = hl()
    f.on_text(hl_book(), NOW)
    f.on_text(hl_book("BTC", 70000.0, 70001.0), NOW)
    assert f.tops(NOW) == {"SOLUSDT": (100.0, 100.2), "BTCUSDT": (70000.0, 70001.0)}


def test_candles_track_high_low_of_sampled_mids():
    f = hl()
    start = (int(NOW) // 60 + 1) * 60.0
    for i, (bid, ask) in enumerate(((100.0, 100.2), (102.0, 102.2), (98.0, 98.2), (99.0, 99.2))):
        f.on_text(hl_book(bid=bid, ask=ask), start + i * 2)
        f.book("SOLUSDT", 20, start + i * 2)
    f.on_text(hl_book(bid=110.0, ask=110.2), start + 61)
    f.book("SOLUSDT", 20, start + 61)
    c = f.candles("SOLUSDT", 10, start + 61)
    assert len(c.ts) == 2 and c.ts[0] == start * 1000
    assert c.opens[0] == pytest.approx(100.1) and c.highs[0] == pytest.approx(102.1)
    assert c.lows[0] == pytest.approx(98.1) and c.closes[0] == pytest.approx(99.1)
    assert f.candles("SOLUSDT", 1, start + 61).closes[0] == pytest.approx(110.1)


def test_old_minutes_are_dropped():
    f = hl()
    for m in range(feeds_module.MINUTES_KEPT + 20):
        t = NOW + m * 60
        f.on_text(hl_book(), t)
        f.book("SOLUSDT", 20, t)
    assert len(f.minutes["SOLUSDT"]) <= feeds_module.MINUTES_KEPT + 1


def test_hyperliquid_funding_is_hourly_and_comes_from_the_feed():
    f = hl()
    f.on_text(hl_ctx(), NOW)
    r = f.funding("SOLUSDT", NOW)
    assert r.rate == pytest.approx(0.0000125)
    assert r.next_ts == (NOW // 3600 + 1) * 3600
    assert f.funding("BTCUSDT", NOW) is None
    assert f.funding("SOLUSDT", NOW + 60) is None


def test_binance_funding_comes_from_the_all_symbol_stream():
    f = bn()
    f.on_text(bn_marks(), NOW)
    r = f.funding("SOLUSDT", NOW)
    assert r.rate == pytest.approx(0.0001) and r.next_ts == 1_800_003_600.0
    assert f.funding("OTHER", NOW) is None


def test_binance_url_names_every_stream():
    assert "solusdt@depth10@500ms" in bn().url and "btcusdt@depth10@500ms" in bn().url and "!markPrice@arr" in bn().url


def test_hyperliquid_subscribes_to_books_and_funding():
    subs = hl().subscriptions()
    assert {"method": "subscribe", "subscription": {"type": "l2Book", "coin": "SOL"}} in subs
    assert {"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": "BTC"}} in subs


# ------------------------------------------------------------------- Market prefers a live feed

class NoNetwork:
    def get(self, *a, **k):
        raise AssertionError("REST must not be used while the feed answers")

    post = get


async def test_market_answers_from_a_live_feed_without_rest():
    market = Market(NoNetwork())
    f = HyperliquidFeed(None, ["SOLUSDT"])
    market.attach_feed(f)
    now = time.time()
    f.on_text(hl_book(), now)
    f.on_text(hl_ctx(), now)
    assert (await market.book("hyperliquid", "SOLUSDT")).bids[0][0] == 100.0
    assert (await market.tops("hyperliquid"))["SOLUSDT"] == (100.0, 100.2)
    assert (await market.funding("hyperliquid", "SOLUSDT")).rate == pytest.approx(0.0000125)
    assert len((await market.candles("hyperliquid", "SOLUSDT", "1m", 5)).ts) == 1


class Refused:
    def __init__(self):
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1
        raise aiohttp.ClientConnectionError()

    get = post


async def test_market_falls_back_to_rest_when_the_feed_is_silent():
    session = Refused()
    market = Market(session)
    f = HyperliquidFeed(None, ["SOLUSDT"])
    market.attach_feed(f)
    f.on_text(hl_book(), time.time() - 60)
    with pytest.raises(DataError):
        await market.book("hyperliquid", "SOLUSDT")
    assert session.calls == 1


# ------------------------------------------------------------------- a real connection

@pytest.fixture
async def ws_server():
    state = {"received": [], "connections": 0, "drop_first": True}

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state["connections"] += 1
        first = state["connections"] == 1
        await ws.send_str(hl_book(bid=100.0 if first else 200.0, ask=100.2 if first else 200.2))
        async for msg in ws:
            state["received"].append(json.loads(msg.data))
            if first and state["drop_first"] and len(state["received"]) >= 4:
                await ws.close()
                break
        return ws

    app = web.Application()
    app.router.add_get("/ws", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state["url"] = f"ws://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws"
    yield state
    await runner.cleanup()


async def wait_until(condition, seconds=5.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_feed_subscribes_receives_and_reconnects(ws_server, monkeypatch):
    monkeypatch.setattr(feeds_module, "RETRY_SECONDS", 0.05)
    async with aiohttp.ClientSession() as session:
        f = HyperliquidFeed(session, ["SOLUSDT", "BTCUSDT"])
        f.url = ws_server["url"]
        task = asyncio.create_task(f.run())
        try:
            assert await wait_until(lambda: len(ws_server["received"]) >= 4)
            assert ws_server["received"][0]["method"] == "subscribe"
            assert await wait_until(lambda: ws_server["connections"] >= 2)
            assert await wait_until(lambda: (f.book("SOLUSDT") or None) is not None and f.book("SOLUSDT").bids[0][0] == 199.0 + 1.0)
            assert f.reconnects >= 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_blocked_handshake_is_recorded_and_waits_longer(monkeypatch):
    async def refuse(request):
        return web.Response(status=418)

    app = web.Application()
    app.router.add_get("/ws", refuse)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    waits = []
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        waits.append(seconds)
        await real_sleep(0)
    monkeypatch.setattr(feeds_module.asyncio, "sleep", fast_sleep)
    try:
        async with aiohttp.ClientSession() as session:
            f = BinanceFeed(session, ["SOLUSDT"])
            f.url = f"ws://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws"
            task = asyncio.create_task(f.run())
            deadline = time.time() + 5
            while time.time() < deadline and not waits:
                await real_sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await runner.cleanup()
    assert f.last_error == "HTTP 418"
    assert waits[0] == feeds_module.BLOCKED_RETRY_SECONDS
