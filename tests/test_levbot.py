import time

import numpy as np
import pytest

from levbot import config, strategy as strategy_mod
from levbot.binance import SymbolRules, fmt
from levbot.engine import Engine, fresh_state
from levbot.strategy import BY_KEY, bars_from_klines, decide, latest_signal, size, trail_stop

HOUR = 3600
RULES = SymbolRules(step=0.01, min_qty=0.01, tick=0.01, min_notional=5.0)


def klines(n=520, breakout=True, now=None):
    """An hourly uptrend whose newest closed bar breaks the 24-bar high on high volume and closes at its top."""
    now = now or time.time()
    last_close = now - (now % HOUR) - 0.001
    rows = []
    price = 100.0
    for i in range(n):
        open_t = (last_close + 0.001) - (n - i) * HOUR
        wiggle = 0.6 * np.sin(i / 3.0)
        o = price
        c = price * 1.0015 + wiggle
        h, l = max(o, c) + 0.3, min(o, c) - 0.3
        v = 1000.0
        if breakout and i == n - 1:
            c = max(r[2] for r in rows[-24:]) + 2.0
            h, l, v = c + 0.05, o - 0.2, 3000.0
        rows.append([open_t * 1000, o, h, l, c, v, (open_t + HOUR) * 1000 - 1])
        price = c
    rows.append([(last_close + 0.001) * 1000, price, price, price, price, 1.0, (last_close + HOUR) * 1000])  # still forming
    return rows


def test_bars_drop_the_forming_bar_and_signal_on_the_last_closed_one():
    now = time.time()
    b = bars_from_klines("SOLUSDT", klines(now=now), 1, [], now * 1000)
    assert len(b.ts) == 520
    assert latest_signal(BY_KEY["trend1h"], b)[0] == "LONG"
    flat = bars_from_klines("SOLUSDT", klines(breakout=False, now=now), 1, [], now * 1000)
    assert latest_signal(BY_KEY["trend1h"], flat) is None


def test_size_keeps_margin_and_risk_inside_the_limits():
    p = size("LONG", 100.0, 1.0, BY_KEY["trend1h"].exits, RULES)
    assert p.margin <= config.MARGIN_PER_TRADE_USD + 1e-9
    assert p.risk <= config.RISK_PER_TRADE_USD + 1e-9
    assert 1 <= p.leverage <= config.MAX_LEVERAGE
    assert p.stop < 100.0


def book(**kw):
    base = {"free": 100.0, "open_count": 0, "symbol_open": False, "same_direction": 0, "day_net": 0.0, "drawdown": 0.0}
    return {**base, **kw}


def test_every_check_is_listed_and_one_red_check_blocks_entry():
    now = time.time()
    now = now - (now % HOUR) + 30
    b = bars_from_klines("SOLUSDT", klines(now=now), 1, [], now * 1000)
    d, a = latest_signal(BY_KEY["trend1h"], b)
    close = float(b.closes[-1])
    market = {"bid": close - 0.01, "ask": close + 0.01, "funding": 0.0001}
    ok = decide(BY_KEY["trend1h"], b, d, a, market, RULES, book(), now)
    assert ok.passed, [c.name for c in ok.failed()]
    groups = {c.group for c in ok.checks}
    assert groups == {"Sinyal", "Onaylar", "Piyasa", "Risk", "Boyut"}
    blocked = decide(BY_KEY["trend1h"], b, d, a, market, RULES, book(symbol_open=True), now)
    assert not blocked.passed and [c.name for c in blocked.failed()] == ["Bu paritede açık pozisyon yok"]
    wide = decide(BY_KEY["trend1h"], b, d, a, {**market, "ask": close * 1.01}, RULES, book(), now)
    assert "Alış-satış farkı dar" in [c.name for c in wide.failed()]


def test_trailing_stop_only_moves_in_favour():
    ex = BY_KEY["trend1h"].exits
    assert trail_stop("LONG", 95.0, 110.0, 2.0, ex) == 104.0
    assert trail_stop("LONG", 95.0, 96.0, 2.0, ex) == 95.0
    assert trail_stop("SHORT", 105.0, 90.0, 2.0, ex) == 96.0


def test_fmt_uses_the_exchange_step():
    assert fmt(284.0, 1.0) == "284"
    assert fmt(0.12345, 0.001) == "0.123"
    assert fmt(75209.123, 0.1) == "75209.1"


class FakeClient:
    def __init__(self, now):
        self.now = now
        self.rules = {s: RULES for s in config.SYMBOLS}
        self.position = {}
        self.orders, self.stops, self.cancelled = [], [], []
        self.fills = []

    async def load_rules(self):
        return self.rules

    async def klines(self, symbol, interval, limit):
        return klines(now=self.now) if symbol == "SOLUSDT" and interval == "1h" else klines(breakout=False, now=self.now)

    async def funding_history(self, symbol, limit=50):
        return []

    async def book_ticker(self, symbol):
        c = klines(now=self.now)[-2][4]
        return {"bidPrice": str(c - 0.01), "askPrice": str(c + 0.01)}

    async def premium(self, symbol):
        return {"lastFundingRate": "0.0001", "markPrice": "100"}

    async def set_isolated(self, symbol):
        pass

    async def set_leverage(self, symbol, leverage):
        return {}

    async def market(self, symbol, side, qty, reduce_only=False, client_id=None):
        price = klines(now=self.now)[-2][4]
        oid = len(self.orders) + 1
        self.orders.append((symbol, side, qty, reduce_only))
        self.fills.append({"orderId": oid, "side": side, "qty": str(qty), "price": str(price), "commission": "0.01",
                           "realizedPnl": "0" if not reduce_only else "1.5", "time": time.time() * 1000})
        if reduce_only:
            self.position.pop(symbol, None)
        else:
            self.position[symbol] = qty if side == "BUY" else -qty
        return {"orderId": oid, "executedQty": str(qty)}

    async def place_stop(self, symbol, side, price):
        self.stops.append((symbol, side, price))
        return {"algoId": len(self.stops)}

    async def cancel_stop(self, algo_id):
        self.cancelled.append(algo_id)

    async def cancel_all_stops(self, symbol):
        pass

    async def trades(self, symbol, start_ms):
        return self.fills

    async def income(self, symbol, start_ms, kind="FUNDING_FEE"):
        return [{"income": "-0.02"}]

    async def positions(self):
        return [{"symbol": s, "positionAmt": str(q), "markPrice": "100", "unRealizedProfit": "0.3", "liquidationPrice": "80"}
                for s, q in self.position.items()]


@pytest.mark.asyncio
async def test_signal_opens_with_an_exchange_stop_and_the_close_is_booked_from_fills():
    now = time.time()
    now = now - (now % HOUR) + 30  # 30 seconds after an hourly close
    client = FakeClient(now)
    engine = Engine(client, fresh_state())
    import levbot.engine as eng
    eng.STRATEGIES = (strategy_mod.BY_KEY["trend1h"],)
    await engine.scan_once(now)
    opened = [t for t in engine.state["open"] if t["symbol"] == "SOLUSDT"]
    assert opened, engine.state["decisions"][:2]
    t = opened[0]
    assert t["direction"] == "LONG" and t["stop"] < t["entry"]
    assert client.stops and client.stops[0][1] == "SELL"
    assert t["margin"] <= config.MARGIN_PER_TRADE_USD + 1e-9
    d = next(x for x in engine.state["decisions"] if x["strategy"] == "trend1h")
    assert d["result"].startswith("Açıldı") and d["checks"]
    await engine.scan_once(now + 5)  # Same bar again: nothing new.
    assert len([x for x in engine.state["open"] if x["symbol"] == "SOLUSDT"]) == 1
    t["opened_at"] -= 60
    await client.market("SOLUSDT", "SELL", t["qty"], reduce_only=True)  # The exchange stop fires.
    await engine.sync_once()
    assert not engine.state["open"]
    c = engine.state["closed"][0]
    assert c["realized"] == pytest.approx(1.5) and c["funding"] == pytest.approx(-0.02)
    assert c["net"] == pytest.approx(1.5 - 0.01 - 0.01 - 0.02)
    assert engine.equity() == pytest.approx(config.BUDGET_USD + c["net"])


@pytest.mark.asyncio
async def test_a_failed_stop_closes_the_position_at_once():
    now = time.time()
    now = now - (now % HOUR) + 30
    client = FakeClient(now)

    async def broken(symbol, side, price):
        from levbot.binance import BinanceError
        raise BinanceError(400, -1, "rejected")
    client.place_stop = broken
    engine = Engine(client, fresh_state())
    import levbot.engine as eng
    eng.STRATEGIES = (strategy_mod.BY_KEY["trend1h"],)
    await engine.scan_once(now)
    assert not engine.state["open"]
    assert client.orders[-1][3] is True  # reduce-only close
    d = next(x for x in engine.state["decisions"] if x["strategy"] == "trend1h")
    assert "Zarar-kes kurulamadı" in d["result"]


def test_only_the_4h_strategy_runs_by_default():
    assert [s.key for s in strategy_mod.STRATEGIES] == ["trend4h_confirmed"]
