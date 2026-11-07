import numpy as np
import pytest

from tradebot.costs import (PERP_VENUES, breakeven_winrate, liquidation_price, round_trip_cost_pct,
                            walk_book, walk_book_notional)
from tradebot.paper import PaperBook, Plan, Quote
from tradebot.risk import Filters, Rejected, Sized, entry_block_reason, size_position, stop_and_target
from tradebot.settings import RiskLimits
from tradebot.signals import compute_features, evaluate

LIMITS = RiskLimits(
    capital_usd=100, risk_per_trade_usd=1.5, max_leverage=5, max_margin_per_trade_pct=25,
    max_open_positions=3, max_same_direction=2, daily_loss_stop_usd=5, kill_drawdown_usd=25,
    reward_r=2.0, min_stop_pct=1.25, max_stop_pct=3.0, max_hold_hours=24,
    symbol_cooldown_seconds=1800, stop_slippage_bps=3.0,
)
FILTERS = Filters(min_notional=5, qty_step=0.001)


def book(name="b", venue="binance"):
    return PaperBook(name, venue, "rules", LIMITS)


def quote(bid=100.0, ask=100.1, ts=1000.0, **kw):
    return Quote(bids=[[bid, 1000]], asks=[[ask, 1000]], ts=ts, **kw)


def long_plan(symbol="SOLUSDT", stop=98.0, target=104.0):
    return Plan(symbol, "LONG", stop, target, "rules")


def test_walk_book_averages_levels():
    price, filled = walk_book([[100, 1], [101, 1]], 1.5)
    assert price == pytest.approx(150.5 / 1.5)
    assert filled == pytest.approx(1.5)


def test_walk_book_reports_partial_fill():
    _, filled = walk_book([[100, 1]], 5)
    assert filled == pytest.approx(1)


def test_walk_book_notional():
    price, qty = walk_book_notional([[100, 1], [200, 1]], 200)
    assert qty == pytest.approx(1.5)
    assert price == pytest.approx(200 / 1.5)


def test_liquidation_price_long_and_short():
    assert liquidation_price("LONG", 100, 5, 2.5) == pytest.approx(82.5)
    assert liquidation_price("SHORT", 100, 5, 2.5) == pytest.approx(117.5)


def test_breakeven_winrate_rises_with_costs():
    assert breakeven_winrate(1.5, 0.0) == pytest.approx(0.40)
    assert breakeven_winrate(1.5, 0.075) == pytest.approx(0.43)


def test_round_trip_cost_binance_taker_maker():
    assert round_trip_cost_pct(PERP_VENUES["binance"], True, 0) == pytest.approx(0.07)
    assert round_trip_cost_pct(PERP_VENUES["binance"], False, 2) == pytest.approx(0.14)


def test_stop_is_clamped_to_band():
    stop, target = stop_and_target("LONG", 100.0, 0.1, LIMITS)
    assert stop == pytest.approx(98.75)
    assert target == pytest.approx(102.5)
    stop, target = stop_and_target("SHORT", 100.0, 9.0, LIMITS)
    assert stop == pytest.approx(103.0)
    assert target == pytest.approx(94.0)


def test_sizing_keeps_loss_including_costs_inside_budget():
    s = size_position(100, 98, PERP_VENUES["binance"], LIMITS, FILTERS, 100, 100)
    assert isinstance(s, Sized)
    assert s.est_loss_at_stop <= 1.5 + 1e-9
    # Stop alone would allow 0.75 units; costs must reduce that.
    assert s.qty < 0.75


def test_sizing_rejects_below_exchange_minimum_instead_of_exceeding_risk():
    btc = Filters(min_notional=50, qty_step=0.001)
    s = size_position(76000, 76000 * 0.97, PERP_VENUES["binance"], LIMITS, btc, 100, 100)
    assert isinstance(s, Rejected)


def test_sizing_uses_lowest_leverage_within_margin_target():
    s = size_position(100, 98.5, PERP_VENUES["binance"], LIMITS, FILTERS, 100, 100)
    assert isinstance(s, Sized)
    assert s.leverage <= LIMITS.max_leverage
    assert s.margin <= 25.0 + 1e-6 or s.leverage == LIMITS.max_leverage


def test_sizing_rejects_without_free_margin():
    s = size_position(100, 98, PERP_VENUES["binance"], LIMITS, FILTERS, 100, 1.0)
    assert isinstance(s, Rejected)


def test_size_factor_scales_risk():
    full = size_position(100, 98, PERP_VENUES["binance"], LIMITS, FILTERS, 100, 100)
    half = size_position(100, 98, PERP_VENUES["binance"], LIMITS, FILTERS, 100, 100, size_factor=0.5)
    assert half.qty == pytest.approx(full.qty / 2, abs=0.001)


def test_size_factor_above_one_cannot_raise_risk():
    full = size_position(100, 98, PERP_VENUES["binance"], LIMITS, FILTERS, 100, 100)
    big = size_position(100, 98, PERP_VENUES["binance"], LIMITS, FILTERS, 100, 100, size_factor=5.0)
    assert big.qty == pytest.approx(full.qty)


def test_book_charges_transfer_on_creation():
    b = book()
    assert b.state["balance"] == pytest.approx(99.0)
    assert b.state["costs"]["transfer"] == pytest.approx(1.0)


def test_entry_walks_the_ask_and_charges_taker_fee():
    b = book()
    out = b.try_open(long_plan(), quote(), FILTERS)
    p = out["opened"]
    assert p["entry"] == pytest.approx(100.1)
    assert p["entry_fee"] == pytest.approx(p["qty"] * 100.1 * 0.0005)
    assert b.state["balance"] == pytest.approx(99.0 - p["entry_fee"])
    assert p["entry_slippage"] == pytest.approx(0.05 * p["qty"])


def test_stop_exit_net_includes_all_costs():
    b = book()
    p = b.try_open(long_plan(), quote(), FILTERS)["opened"]
    [t] = b.on_quote("SOLUSDT", quote(bid=97.9, ask=98.0, ts=2000))
    assert t["exit_reason"] == "stop"
    # Worse of the walked book (97.9) and the slipped stop (97.97).
    assert t["exit"] == pytest.approx(97.9)
    exit_fee = t["qty"] * 97.9 * 0.0005
    expected = (97.9 - p["entry"]) * p["qty"] - p["entry_fee"] - exit_fee
    assert t["net"] == pytest.approx(expected)
    assert b.state["losses"] == 1
    assert b.state["balance"] == pytest.approx(99.0 + expected)


def test_stop_touched_by_candle_low_then_recovered_still_exits_at_slipped_stop():
    b = book()
    b.try_open(long_plan(), quote(), FILTERS)
    [t] = b.on_quote("SOLUSDT", quote(bid=99.5, ask=99.6, ts=2000, low=97.5, high=100))
    assert t["exit"] == pytest.approx(98.0 * (1 - 3 / 10000))


def test_target_requires_trade_strictly_through_and_pays_maker():
    b = book()
    p = b.try_open(long_plan(), quote(), FILTERS)["opened"]
    assert b.on_quote("SOLUSDT", quote(bid=103.9, ask=104.0, ts=2000, high=104.0, low=103)) == []
    [t] = b.on_quote("SOLUSDT", quote(bid=104.0, ask=104.1, ts=3000, high=104.01, low=103))
    assert t["exit"] == pytest.approx(104.0)
    assert t["exit_fee"] == pytest.approx(p["qty"] * 104.0 * 0.0002)
    assert t["exit_reason"] == "hedef"


def test_stop_wins_when_range_touches_both():
    b = book()
    b.try_open(long_plan(), quote(), FILTERS)
    [t] = b.on_quote("SOLUSDT", quote(bid=100, ask=100.1, ts=2000, high=105, low=97))
    assert t["exit_reason"] == "stop"


def test_short_position_exits():
    b = book()
    plan = Plan("SOLUSDT", "SHORT", 102.0, 96.0, "rules")
    p = b.try_open(plan, quote(), FILTERS)["opened"]
    assert p["entry"] == pytest.approx(100.0)
    [t] = b.on_quote("SOLUSDT", quote(bid=95.9, ask=95.95, ts=2000, low=95.9, high=100))
    assert t["exit_reason"] == "hedef"
    assert t["gross"] == pytest.approx(4.0 * p["qty"])


def test_funding_long_pays_when_positive_and_short_receives():
    for direction, plan, expected_sign in (
        ("LONG", long_plan(), 1),
        ("SHORT", Plan("SOLUSDT", "SHORT", 102.0, 96.0, "rules"), -1),
    ):
        b = book()
        p = b.try_open(plan, quote(ts=1000, funding_rate=0.001, funding_next_ts=1500), FILTERS)["opened"]
        before = b.state["balance"]
        assert b.on_quote("SOLUSDT", quote(ts=1200, funding_rate=0.001, funding_next_ts=1500)) == []
        assert b.state["balance"] == pytest.approx(before)
        b.on_quote("SOLUSDT", quote(ts=1600, funding_rate=0.001, funding_next_ts=30300))
        payment = 100.05 * p["qty"] * 0.001 * expected_sign
        assert p["funding"] == pytest.approx(payment)
        assert b.state["balance"] == pytest.approx(before - payment)


def test_funding_is_charged_once_per_period():
    b = book()
    p = b.try_open(long_plan(), quote(ts=1000, funding_rate=0.001, funding_next_ts=1500), FILTERS)["opened"]
    b.on_quote("SOLUSDT", quote(ts=1600, funding_rate=0.001, funding_next_ts=30300))
    once = p["funding"]
    b.on_quote("SOLUSDT", quote(ts=1700, funding_rate=0.001, funding_next_ts=30300))
    assert p["funding"] == pytest.approx(once)


def test_funding_uses_rate_quoted_before_settlement():
    b = book()
    p = b.try_open(long_plan(), quote(ts=1000, funding_rate=0.001, funding_next_ts=1500), FILTERS)["opened"]
    b.on_quote("SOLUSDT", quote(ts=1400, funding_rate=0.002, funding_next_ts=1500))
    b.on_quote("SOLUSDT", quote(ts=1600, funding_rate=0.009, funding_next_ts=30300))
    assert p["funding"] == pytest.approx(100.05 * p["qty"] * 0.002)


def test_funding_waits_until_the_venue_rolls_and_does_not_double_charge():
    b = book()
    p = b.try_open(long_plan(), quote(ts=1000, funding_rate=0.001, funding_next_ts=1500), FILTERS)["opened"]
    b.on_quote("SOLUSDT", quote(ts=1600, funding_rate=0.001, funding_next_ts=1500))
    assert p["funding"] == 0.0
    b.on_quote("SOLUSDT", quote(ts=1700, funding_rate=0.001, funding_next_ts=30300))
    first = p["funding"]
    assert first > 0
    b.on_quote("SOLUSDT", quote(ts=1800, funding_rate=0.001, funding_next_ts=30300))
    assert p["funding"] == pytest.approx(first)


def test_trailing_stop_moves_only_toward_profit():
    b = book()
    plan = Plan("SOLUSDT", "LONG", 95.0, None, "rules", trail_atr=3.0, bar_hours=4)
    p = b.try_open(plan, quote(), FILTERS)["opened"]
    b.update_trail("SOLUSDT", 4, 110.0, 2.0)
    assert p["stop"] == pytest.approx(104.0)
    b.update_trail("SOLUSDT", 4, 100.0, 2.0)
    assert p["stop"] == pytest.approx(104.0)
    b.update_trail("SOLUSDT", 1, 200.0, 2.0)  # A different timeframe leaves it alone.
    assert p["stop"] == pytest.approx(104.0)


def test_trailing_stop_short_and_exit_past_entry_is_a_win():
    b = book()
    plan = Plan("SOLUSDT", "SHORT", 105.0, None, "rules", trail_atr=3.0, bar_hours=4)
    p = b.try_open(plan, quote(), FILTERS)["opened"]
    b.update_trail("SOLUSDT", 4, 90.0, 2.0)
    assert p["stop"] == pytest.approx(96.0)
    [t] = b.on_quote("SOLUSDT", quote(bid=96.0, ask=96.1, ts=2000))
    assert t["exit_reason"] == "stop"
    assert t["net"] > 0


def test_no_target_position_stays_open_on_big_move():
    b = book()
    plan = Plan("SOLUSDT", "LONG", 95.0, None, "rules", trail_atr=3.0, bar_hours=4, max_hold_hours=720)
    b.try_open(plan, quote(ts=0), FILTERS)
    assert b.on_quote("SOLUSDT", quote(bid=150, ask=150.1, ts=3600 * 30, high=151, low=149)) == []


def test_position_hold_limit_comes_from_plan():
    b = book()
    plan = Plan("SOLUSDT", "LONG", 95.0, None, "rules", max_hold_hours=2)
    b.try_open(plan, quote(ts=0), FILTERS)
    [t] = b.on_quote("SOLUSDT", quote(bid=100.5, ask=100.6, ts=3 * 3600))
    assert t["exit_reason"] == "süre doldu"


def test_gap_through_liquidation_loses_exactly_the_margin():
    b = book()
    p = b.try_open(long_plan(), quote(), FILTERS)["opened"]
    before = b.state["balance"]
    [t] = b.on_quote("SOLUSDT", quote(bid=p["liquidation"] - 1, ask=p["liquidation"], ts=2000))
    assert t["liquidated"] is True
    assert t["gross"] == pytest.approx(-p["margin"])
    assert b.state["balance"] == pytest.approx(before - p["margin"])
    assert b.state["liquidations"] == 1


def test_time_stop_exits_with_taker_fee():
    b = book()
    p = b.try_open(long_plan(), quote(ts=0), FILTERS)["opened"]
    [t] = b.on_quote("SOLUSDT", quote(bid=100.5, ask=100.6, ts=24 * 3600))
    assert t["exit_reason"] == "süre doldu"
    assert t["exit_fee"] == pytest.approx(p["qty"] * 100.5 * 0.0005)


def test_entry_blocks():
    open_positions = [{"symbol": "A", "direction": "LONG"}, {"symbol": "B", "direction": "LONG"}]
    assert "aynı yönde" in entry_block_reason("LONG", "C", open_positions, 0, 100, LIMITS, None, None, 0)
    assert entry_block_reason("SHORT", "C", open_positions, 0, 100, LIMITS, None, None, 0) is None
    assert "açık pozisyon var" in entry_block_reason("SHORT", "A", open_positions, 0, 100, LIMITS, None, None, 0)
    assert "günlük" in entry_block_reason("LONG", "C", [], -5, 100, LIMITS, None, None, 0)
    assert "zarar limiti" in entry_block_reason("LONG", "C", [], 0, 75, LIMITS, None, None, 0)
    assert "bekleme" in entry_block_reason("LONG", "C", [], 0, 100, LIMITS, None, 900.0, 1000.0)
    assert "durduruldu" in entry_block_reason("LONG", "C", [], 0, 100, LIMITS, "x", None, 0)


def test_rejection_is_counted_and_no_position_opened():
    b = book()
    b.try_open(long_plan(), quote(ts=0), FILTERS)
    out = b.try_open(long_plan(), quote(ts=1), FILTERS)
    assert "rejected" in out
    assert len(b.state["open"]) == 1
    assert sum(b.state["rejected"].values()) == 1


def test_price_past_stop_or_target_is_rejected():
    b = book()
    assert "rejected" in b.try_open(long_plan(stop=101.0), quote(), FILTERS)
    assert "rejected" in b.try_open(long_plan(target=99.0), quote(), FILTERS)


def test_kill_switch_halts_new_entries():
    b = book()
    b.state["balance"] = 74.0
    b._update_drawdown({})
    assert b.state["halted"]
    assert "rejected" in b.try_open(long_plan(), quote(), FILTERS)


def test_stats_and_profit_factor():
    b = book()
    b.try_open(long_plan(), quote(ts=0), FILTERS)
    b.on_quote("SOLUSDT", quote(bid=104, ask=104.1, ts=10, high=104.5, low=103))
    b.try_open(long_plan("ETHUSDT"), quote(ts=20), FILTERS)
    b.on_quote("ETHUSDT", quote(bid=97, ask=97.1, ts=30))
    s = b.stats()
    assert s["trades"] == 2 and s["wins"] == 1 and s["losses"] == 1
    assert s["profit_factor"] > 1
    assert s["net"] == pytest.approx(sum(h["net"] for h in b.state["history"]))


def test_accounting_identity_balance_equals_start_plus_net_minus_transfer():
    b = book()
    for i, (sym, bid) in enumerate((("A", 104.2), ("B", 97.0), ("C", 104.2))):
        b.try_open(long_plan(sym), quote(ts=i * 100), FILTERS)
        b.on_quote(sym, quote(bid=bid, ask=bid + 0.1, ts=i * 100 + 50, high=bid + 0.5, low=bid - 0.1))
    assert b.state["balance"] == pytest.approx(100 - 1 + sum(h["net"] for h in b.state["history"]))


def trending(n=200, step=0.2, seed=1):
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(step, 0.3, n))
    highs = closes + 0.2
    lows = closes - 0.2
    volumes = rng.uniform(90, 110, n)
    volumes[-1] = 200
    return highs, lows, closes, volumes


def test_signal_needs_enough_history():
    h, l, c, v = trending(40)
    assert evaluate(compute_features(h, l, c, v), 39, min_score=0) is None


def test_signal_direction_follows_trend():
    h, l, c, v = trending()
    s = evaluate(compute_features(h, l, c, v), len(c) - 1, min_score=25)
    if s:
        assert s.direction == "LONG"
    h, l, c, v = trending(step=-0.2)
    s = evaluate(compute_features(h, l, c, v), len(c) - 1, min_score=25)
    if s:
        assert s.direction == "SHORT"


def test_signal_ignores_future_bars():
    h, l, c, v = trending()
    i = 150
    full = evaluate(compute_features(h, l, c, v), i, min_score=25)
    cut = evaluate(compute_features(h[:i + 1], l[:i + 1], c[:i + 1], v[:i + 1]), i, min_score=25)
    assert full == cut
