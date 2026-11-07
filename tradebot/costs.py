"""Every trading cost in one place.

Fee sources, checked 2026-11-07, base tier, no VIP or token discount:
  Binance USD-M perps 0.02% maker, 0.05% taker (Binance fee page, third-party guides agree)
  Bybit perps         0.02% maker, 0.055% taker (Bybit help center)
  Hyperliquid perps   0.015% maker, 0.045% taker (Hyperliquid docs, trading/fees)
Not checked against a primary source, treat as estimates:
  Binance and Bybit spot 0.1% (widely published base tier)
  transfer_usd and REBALANCE_TRANSFER_USD (moving money between venues and chains)
"""

import math
from dataclasses import dataclass
from typing import Iterable, Sequence, Tuple

Level = Tuple[float, float]


@dataclass(frozen=True)
class VenueCosts:
    name: str
    kind: str  # "cex" or "dex"
    maker_pct: float
    taker_pct: float
    # Conservative flat maintenance margin; real first brackets are lower on these symbols.
    maintenance_pct: float
    transfer_usd: float
    funding_interval_seconds: int


PERP_VENUES = {
    "binance": VenueCosts("binance", "cex", 0.02, 0.05, 2.5, 1.0, 8 * 3600),
    "bybit": VenueCosts("bybit", "cex", 0.02, 0.055, 2.5, 1.0, 8 * 3600),
    "hyperliquid": VenueCosts("hyperliquid", "dex", 0.015, 0.045, 2.5, 1.0, 3600),
}

SPOT_TAKER_PCT = {"binance": 0.10, "bybit": 0.10}
# Estimated cost of one on-chain inventory transfer between a CEX and Arbitrum.
REBALANCE_TRANSFER_USD = 0.30
# Two swaps on Arbitrum.
DEX_ROUND_TRIP_GAS_UNITS = 450_000
ARBITRUM_L1_DATA_UPLIFT = 1.2


def sign(direction: str) -> int:
    if direction == "LONG":
        return 1
    if direction == "SHORT":
        return -1
    raise ValueError(f"unknown direction {direction!r}")


def walk_book(levels: Iterable[Sequence[float]], qty: float) -> Tuple[float, float]:
    """Average fill price for qty against book levels. Returns (avg_price, filled_qty)."""
    remaining = qty
    cost = 0.0
    for price, size in levels:
        take = min(remaining, size)
        cost += take * price
        remaining -= take
        if remaining <= 1e-12:
            break
    filled = qty - max(remaining, 0.0)
    return (cost / filled if filled > 0 else 0.0), filled


def walk_book_notional(levels: Iterable[Sequence[float]], notional: float) -> Tuple[float, float]:
    """Fill a USD notional. Returns (avg_price, filled_base_qty)."""
    remaining = notional
    bought = 0.0
    for price, size in levels:
        level_usd = price * size
        take_usd = min(remaining, level_usd)
        bought += take_usd / price
        remaining -= take_usd
        if remaining <= 1e-9:
            break
    spent = notional - max(remaining, 0.0)
    return (spent / bought if bought > 0 else 0.0), bought


def fee_usd(notional: float, pct: float) -> float:
    return abs(notional) * pct / 100.0


def liquidation_price(direction: str, entry: float, leverage: float, maintenance_pct: float) -> float:
    """Isolated margin liquidation price."""
    move = 1.0 / leverage - maintenance_pct / 100.0
    return entry * (1.0 - move) if direction == "LONG" else entry * (1.0 + move)


def breakeven_winrate(reward_r: float, cost_r: float) -> float:
    """Win rate at which expectancy is zero. A win pays reward_r - cost_r, a loss costs 1 + cost_r."""
    return (1.0 + cost_r) / (1.0 + reward_r)


def round_trip_cost_pct(venue: VenueCosts, exit_is_maker: bool, slippage_bps: float) -> float:
    """Round-trip cost as a percent of notional: taker entry, maker or taker exit, slippage both ways."""
    exit_fee = venue.maker_pct if exit_is_maker else venue.taker_pct
    legs = 1 if exit_is_maker else 2
    return venue.taker_pct + exit_fee + legs * slippage_bps / 100.0


def floor_to_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    # Small tolerance so 0.3 / 0.1 = 2.9999999 does not lose a whole step.
    return math.floor(qty / step + 1e-9) * step


def gas_usd(gas_price_wei: int, eth_usd: float) -> float:
    return DEX_ROUND_TRIP_GAS_UNITS * gas_price_wei / 1e18 * eth_usd * ARBITRUM_L1_DATA_UPLIFT
