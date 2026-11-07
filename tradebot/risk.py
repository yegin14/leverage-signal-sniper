"""Hard risk rules. Pure functions, so the backtest and the live paper engine share them."""

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from .costs import VenueCosts, floor_to_step, sign
from .settings import RiskLimits


@dataclass(frozen=True)
class Filters:
    min_notional: float
    qty_step: float
    tick: float = 0.0


@dataclass(frozen=True)
class Sized:
    qty: float
    notional: float
    leverage: int
    margin: float
    est_loss_at_stop: float


@dataclass(frozen=True)
class Rejected:
    reason: str


def stop_and_target(direction: str, entry: float, raw_stop_distance: float, limits: RiskLimits):
    """Clamp the stop distance to the allowed band and place the target at reward_r."""
    lo = entry * limits.min_stop_pct / 100.0
    hi = entry * limits.max_stop_pct / 100.0
    distance = min(max(raw_stop_distance, lo), hi)
    s = sign(direction)
    return entry - s * distance, entry + s * distance * limits.reward_r


def size_position(
    entry: float,
    stop: float,
    venue: VenueCosts,
    limits: RiskLimits,
    filters: Filters,
    equity: float,
    free_margin: float,
    size_factor: float = 1.0,
):
    """Largest size whose loss at the stop, fees and slippage included, stays inside the budget."""
    distance = abs(entry - stop)
    if distance <= 0 or entry <= 0:
        return Rejected("geçersiz stop")
    budget = limits.risk_per_trade_usd * max(0.0, min(size_factor, 1.0))
    cost_rate = venue.taker_pct / 100.0
    slip_rate = limits.stop_slippage_bps / 10_000.0
    per_unit_cost = entry * cost_rate + stop * (cost_rate + slip_rate)
    qty = floor_to_step(budget / (distance + per_unit_cost), filters.qty_step)
    notional = qty * entry
    if qty <= 0 or notional < filters.min_notional:
        return Rejected(
            f"pozisyon çok küçük: {notional:.2f}$ < borsa alt sınırı {filters.min_notional:.2f}$"
        )
    margin_target = max(equity, 1e-9) * limits.max_margin_per_trade_pct / 100.0
    leverage = int(min(max(math.ceil(notional / margin_target), 1), limits.max_leverage))
    margin = notional / leverage
    if margin > free_margin:
        return Rejected(f"serbest teminat yetersiz: gerekli {margin:.2f}$, serbest {free_margin:.2f}$")
    est_loss = qty * (distance + per_unit_cost)
    return Sized(qty, notional, leverage, margin, est_loss)


def entry_block_reason(
    direction: str,
    symbol: str,
    open_positions: Sequence[dict],
    daily_net_usd: float,
    equity: float,
    limits: RiskLimits,
    halted: Optional[str],
    last_open_ts: Optional[float],
    now: float,
) -> Optional[str]:
    """Why a new entry is not allowed, or None when it is."""
    if halted:
        return f"kasa durduruldu: {halted}"
    if equity <= limits.capital_usd - limits.kill_drawdown_usd:
        return f"zarar limiti: özsermaye {equity:.2f}$"
    if daily_net_usd <= -limits.daily_loss_stop_usd:
        return f"günlük zarar limiti: {daily_net_usd:.2f}$"
    if len(open_positions) >= limits.max_open_positions:
        return f"en fazla {limits.max_open_positions} açık pozisyon"
    if any(p["symbol"] == symbol for p in open_positions):
        return "bu paritede açık pozisyon var"
    if sum(1 for p in open_positions if p["direction"] == direction) >= limits.max_same_direction:
        return f"aynı yönde en fazla {limits.max_same_direction} pozisyon"
    if last_open_ts is not None and now - last_open_ts < limits.symbol_cooldown_seconds:
        return "parite bekleme süresinde"
    return None
