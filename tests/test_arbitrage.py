import pytest

from tradebot.arbitrage import (POOLS, ArbLedger, ArbScanner, Opportunity, book_edge, decode_aggregate3, decode_quote,
                                dex_round_trip, encode_aggregate3, encode_quote, rebalance_usd)
from tradebot.costs import gas_usd
from tradebot.venues import Book


def book(venue="binance", bid=100.0, ask=100.1, size=100.0):
    return Book(venue, "SOLUSDT", [[bid, size]], [[ask, size]], 0.0)


def test_no_edge_when_books_are_equal():
    op = book_edge("spot_cex_cex", "S", book(), book("bybit"), 50, 0.1, 0.1)
    assert op.gross_usd < 0 and op.net_usd < op.gross_usd


def test_edge_math_includes_all_fees():
    buy, sell = book(bid=99.9, ask=100.0), book("bybit", bid=101.0, ask=101.1)
    op = book_edge("spot_cex_cex", "S", buy, sell, 100, 0.1, 0.1, other_usd=0.5)
    assert op.gross_usd == pytest.approx(1.0)
    assert op.fee_usd == pytest.approx(0.1 + 0.101)
    assert op.net_usd == pytest.approx(1.0 - 0.201 - 0.5)
    assert op.gross_bps == pytest.approx(100.0)


def test_close_fees_are_charged_for_perp_pairs():
    buy, sell = book(bid=99.9, ask=100.0), book("bybit", bid=101.0, ask=101.1)
    without = book_edge("perp_cex_cex", "S", buy, sell, 100, 0.05, 0.055)
    with_close = book_edge("perp_cex_cex", "S", buy, sell, 100, 0.05, 0.055, close_fee_pct=0.105)
    assert with_close.fee_usd - without.fee_usd == pytest.approx(0.105)


def test_thin_books_give_no_opportunity():
    assert book_edge("k", "S", book(size=0.01), book("bybit"), 100, 0.1, 0.1) is None
    assert book_edge("k", "S", book(), book("bybit", size=0.01), 100, 0.1, 0.1) is None


def test_rebalance_share():
    assert rebalance_usd(50, 50) == pytest.approx(0.30)
    assert rebalance_usd(25, 50) == pytest.approx(0.15)


def test_gas_formula():
    assert gas_usd(20_000_000, 2000) == pytest.approx(450_000 * 2e7 / 1e18 * 2000 * 1.2)


def test_v3_quote_encoding_is_abi_correct():
    pool = POOLS[0]
    to, data = encode_quote(pool, "0x" + "a" * 40, "0x" + "b" * 40, 100_000_000)
    assert to == pool[2]
    assert data.startswith("0xc6a5026a")
    words = [data[10 + i * 64:10 + (i + 1) * 64] for i in range(5)]
    assert words[0].endswith("a" * 40) and words[1].endswith("b" * 40)
    assert int(words[2], 16) == 100_000_000
    assert int(words[3], 16) == 500
    assert int(words[4], 16) == 0


def test_v2_quote_encoding_is_abi_correct():
    pool = next(p for p in POOLS if p[1] == "v2")
    _, data = encode_quote(pool, "0x" + "a" * 40, "0x" + "b" * 40, 7)
    assert data.startswith("0xd06ca61f")
    assert int(data[10:74], 16) == 7
    assert int(data[74:138], 16) == 64
    assert int(data[138:202], 16) == 2


def test_decode_quote():
    assert decode_quote("v3", "0x" + hex(5)[2:].rjust(64, "0") + "f" * 64) == 5
    assert decode_quote("v2", "0x" + "0" * 63 + "1" + hex(9)[2:].rjust(64, "0")) == 9
    assert decode_quote("v3", None) is None
    assert decode_quote("v3", "0x") is None


def test_aggregate3_encoding_layout():
    data = encode_aggregate3([("0x" + "a" * 40, "0x12345678"), ("0x" + "b" * 40, "0xabcd")])
    assert data.startswith("0x82ad56cb")
    body = data[10:]
    words = [body[i:i + 64] for i in range(0, len(body), 64)]
    assert int(words[0], 16) == 32
    assert int(words[1], 16) == 2
    # Offsets count from the first offset word: two offset words, then a five-word element.
    assert int(words[2], 16) == 64
    assert int(words[3], 16) == 64 + 5 * 32
    assert words[4].endswith("a" * 40)
    assert int(words[5], 16) == 1  # allowFailure
    assert int(words[6], 16) == 96
    assert int(words[7], 16) == 4
    assert words[8].startswith("12345678") and len(words[8]) == 64


def test_aggregate3_decode_handles_success_and_failure():
    def word(x):
        return hex(x)[2:].rjust(64, "0")
    first = word(1) + word(64) + word(32) + word(777)
    second = word(0) + word(64) + word(4) + "deadbeef" + "0" * 56
    body = word(32) + word(2) + word(64) + word(64 + len(first) // 2) + first + second
    assert decode_aggregate3("0x" + body) == ["0x" + word(777), None]


def test_dex_round_trip_has_gas_and_no_double_fee():
    op = dex_round_trip("A", "B", "WETH", 50, 50.2, 0.05)
    assert op.gross_usd == pytest.approx(0.2)
    assert op.fee_usd == 0.0
    assert op.net_usd == pytest.approx(0.15)


def opp(kind="perp_cex_cex", gross=1.0, fee=0.3, other=0.0, buy="binance", sell="bybit", notional=50.0):
    return Opportunity(kind, "SOLUSDT", buy, sell, notional, gross, fee, other, gross - fee - other)


def test_ledger_observe_tracks_best_and_positive_count():
    led = ArbLedger(min_net_bps=1.0)
    led.observe("dex_dex", opp("dex_dex", gross=0.1, fee=0.2), 1.0)
    led.observe("dex_dex", opp("dex_dex", gross=0.5, fee=0.2), 2.0)
    led.observe("dex_dex", None, 3.0)
    k = led.state["kinds"]["dex_dex"]
    assert k["scans"] == 3 and k["positive"] == 1
    assert k["best_net_bps"] == pytest.approx(60.0)
    assert k["last_scan_at"] == 3.0


def test_book_instant_updates_balance_by_net():
    led = ArbLedger(capital=100)
    op = opp("spot_cex_cex", gross=1.0, fee=0.2, other=0.3)
    trade = led.book_instant(op, 1.0)
    k = led.state["kinds"]["spot_cex_cex"]
    assert trade["net"] == pytest.approx(0.5)
    assert k["balance"] == pytest.approx(100.5)
    assert k["trades"] == 1


def pair_setup(led, long_entry=100.0, short_entry=101.0, qty=0.5):
    op = book_edge("perp_cex_cex", "SOLUSDT", book(bid=long_entry - 0.1, ask=long_entry),
                   book("bybit", bid=short_entry, ask=short_entry + 0.1), qty * long_entry, 0.05, 0.055, close_fee_pct=0.105)
    return op, led.open_pair(op, qty, long_entry, short_entry, 0.0, 0.0)


def test_pair_charges_only_entry_fees_at_open_and_nets_correctly_at_close():
    led = ArbLedger(capital=100)
    op, pair = pair_setup(led)
    k = led.state["kinds"]["perp_cex_cex"]
    entry_fees = pair["entry_fees"]
    assert entry_fees == pytest.approx(50 * 0.0005 + 50.5 * 0.00055)
    assert k["balance"] == pytest.approx(100 - entry_fees)
    assert led.maybe_close_pair("perp_cex_cex", pair, book(bid=100.0, ask=100.1),
                                book("bybit", bid=101.0, ask=101.1), 3600.0) is None
    trade = led.maybe_close_pair("perp_cex_cex", pair, book(bid=100.6, ask=100.7),
                                 book("bybit", bid=100.5, ask=100.55), 7200.0)
    assert trade is not None and trade["reason"] == "yakınsadı"
    assert trade["gross"] == pytest.approx(0.5 * (100.6 - 100.0) + 0.5 * (101.0 - 100.55))
    assert k["balance"] == pytest.approx(100 + trade["net"])
    assert k["open"] == []
    assert k["net_usd"] == pytest.approx(trade["net"])


def test_pair_closes_at_time_limit_even_at_a_loss():
    led = ArbLedger(capital=100)
    _, pair = pair_setup(led)
    trade = led.maybe_close_pair("perp_cex_cex", pair, book(bid=99.0, ask=99.1),
                                 book("bybit", bid=101.5, ask=101.6), 25 * 3600.0)
    assert trade["reason"] == "süre doldu"
    assert trade["net"] < 0
    assert led.state["kinds"]["perp_cex_cex"]["balance"] == pytest.approx(100 + trade["net"])


def test_pair_funding_is_charged():
    led = ArbLedger(capital=100)
    op = opp()
    pair = led.open_pair(op, 0.5, 100.0, 101.0, 0.01, 0.0)
    trade = led.maybe_close_pair("perp_cex_cex", pair, book(bid=100.6, ask=100.7),
                                 book("bybit", bid=100.5, ask=100.55), 7200.0)
    assert trade["other"] == pytest.approx(0.02)


class OneVenueMarket:
    async def book(self, venue, symbol, depth=20):
        return book(venue)

    async def spot_book(self, venue, symbol, depth=20):
        return book(venue)


async def test_blocked_venue_is_reported_instead_of_showing_empty_results():
    led = ArbLedger()
    scanner = ArbScanner(OneVenueMarket(), None, led, cex_venues=("bybit",), spot_venues=("bybit",))
    await scanner.scan_perps(["SOLUSDT"])
    await scanner.scan_spot()
    kinds = led.state["kinds"]
    assert "bybit" in kinds["perp_cex_cex"]["unavailable"]
    assert kinds["perp_cex_cex"]["scans"] == 0
    assert "bybit" in kinds["spot_cex_cex"]["unavailable"]
    # Bybit against Hyperliquid can still be measured.
    assert kinds["perp_cex_dex"]["scans"] == 1
    assert "unavailable" not in kinds["perp_cex_dex"]


def test_measuring_again_clears_the_unavailable_note():
    led = ArbLedger()
    led.mark_unavailable("dex_dex", "x", 1.0)
    led.observe("dex_dex", None, 2.0)
    assert "unavailable" not in led.state["kinds"]["dex_dex"]


def test_ledger_state_round_trip_and_reset():
    led = ArbLedger(capital=100)
    led.book_instant(opp("dex_dex"), 1.0)
    again = ArbLedger(led.state)
    assert again.state["kinds"]["dex_dex"]["trades"] == 1
    again.reset()
    assert again.state["kinds"]["dex_dex"]["trades"] == 0
