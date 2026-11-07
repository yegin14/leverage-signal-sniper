"""Paper leverage book with every cost charged: fees, slippage, funding, transfers, liquidation.

Fill rules are deliberately pessimistic:
  entry      taker, walks the real order book
  stop       taker, the worse of the walked book and stop price less stop_slippage_bps
  target     resting limit, maker fee, fills only when price trades strictly through it
  same range when a range touches both stop and target, the stop counts first
  time stop  taker, walks the real order book
"""

import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .costs import PERP_VENUES, VenueCosts, fee_usd, liquidation_price, sign, walk_book
from .risk import Filters, Rejected, Sized, entry_block_reason, size_position
from .settings import RiskLimits

MAX_HISTORY = 1000


@dataclass(frozen=True)
class Plan:
    symbol: str
    direction: str
    stop: float
    target: Optional[float]  # None means no fixed target
    origin: str  # "rules" or "desk"
    strategy: str = ""
    reason: str = ""
    size_factor: float = 1.0
    session_id: Optional[str] = None
    trail_atr: Optional[float] = None
    bar_hours: float = 1
    max_hold_hours: Optional[float] = None


@dataclass
class Quote:
    bids: Sequence[Sequence[float]]
    asks: Sequence[Sequence[float]]
    ts: float
    # Traded high and low of full candles after the position opened; None if none closed yet.
    high: Optional[float] = None
    low: Optional[float] = None
    funding_rate: Optional[float] = None
    funding_next_ts: Optional[float] = None

    @property
    def mid(self) -> float:
        return (self.bids[0][0] + self.asks[0][0]) / 2.0


def next_minute_ms(ts: float) -> int:
    return (int(ts // 60) + 1) * 60_000


class PaperBook:
    def __init__(self, name: str, venue: str, source: str, limits: RiskLimits, state: Optional[dict] = None):
        self.name = name
        self.venue: VenueCosts = PERP_VENUES[venue]
        self.source = source
        self.limits = limits
        self.state = state or self.fresh_state()

    def fresh_state(self) -> dict:
        transfer = self.venue.transfer_usd
        return {
            "name": self.name,
            "venue": self.venue.name,
            "source": self.source,
            "started_at": time.time(),
            "initial": self.limits.capital_usd,
            "balance": self.limits.capital_usd - transfer,
            "peak_equity": self.limits.capital_usd - transfer,
            "max_drawdown": 0.0,
            "costs": {"taker_fee": 0.0, "maker_fee": 0.0, "slippage": 0.0, "funding": 0.0, "transfer": transfer},
            "wins": 0,
            "losses": 0,
            "liquidations": 0,
            "open": [],
            "history": [],
            "rejected": {},
            "last_open_ts": {},
            "halted": None,
        }

    def reset(self):
        self.state = self.fresh_state()

    # ---------------------------------------------------------------- queries

    def used_margin(self) -> float:
        return sum(p["margin"] for p in self.state["open"])

    def unrealized(self, marks: Optional[Dict[str, float]] = None) -> float:
        total = 0.0
        for p in self.state["open"]:
            mark = (marks or {}).get(p["symbol"], p.get("last_mark", p["entry"]))
            total += (mark - p["entry"]) * p["qty"] * sign(p["direction"])
        return total

    def equity(self, marks: Optional[Dict[str, float]] = None) -> float:
        return self.state["balance"] + self.unrealized(marks)

    def daily_net(self, now: float) -> float:
        day = int(now // 86400)
        return sum(h["net"] for h in self.state["history"] if int(h["closed_at"] // 86400) == day)

    def stats(self) -> dict:
        history = self.state["history"]
        gains = sum(h["net"] for h in history if h["net"] > 0)
        losses = -sum(h["net"] for h in history if h["net"] <= 0)
        count = len(history)
        days = (time.time() - self.state["started_at"]) / 86400.0
        return {
            "name": self.name,
            "venue": self.venue.name,
            "source": self.source,
            "trades": count,
            "wins": self.state["wins"],
            "losses": self.state["losses"],
            "win_rate": self.state["wins"] / count * 100.0 if count else 0.0,
            "net": sum(h["net"] for h in history),
            "profit_factor": gains / losses if losses > 0 else (float("inf") if gains > 0 else 0.0),
            "max_drawdown": self.state["max_drawdown"],
            "balance": self.state["balance"],
            "equity": self.equity(),
            "unrealized": self.unrealized(),
            "rejected": dict(self.state["rejected"]),
            "open": len(self.state["open"]),
            "days": days,
            "costs": dict(self.state["costs"]),
            "liquidations": self.state["liquidations"],
            "halted": self.state["halted"],
        }

    # ---------------------------------------------------------------- entry

    def try_open(self, plan: Plan, quote: Quote, filters: Filters, now: Optional[float] = None) -> dict:
        """Returns {"opened": position} or {"rejected": reason}. Rejections are counted."""
        now = now if now is not None else quote.ts
        block = entry_block_reason(
            plan.direction, plan.symbol, self.state["open"], self.daily_net(now), self.equity(),
            self.limits, self.state["halted"], self.state["last_open_ts"].get(plan.symbol), now,
        )
        if block:
            return self._reject(block)
        s = sign(plan.direction)
        levels = quote.asks if s > 0 else quote.bids
        if not levels:
            return self._reject("tahtada karşı taraf yok")
        reference = levels[0][0]
        if (reference - plan.stop) * s <= 0:
            return self._reject("fiyat stopun öbür tarafında")
        if plan.target is not None and (plan.target - reference) * s <= 0:
            return self._reject("fiyat hedefin öbür tarafında")
        sized = size_position(
            reference, plan.stop, self.venue, self.limits, filters,
            self.equity(), self.state["balance"] - self.used_margin(), plan.size_factor,
        )
        if isinstance(sized, Rejected):
            return self._reject(sized.reason)
        fill, filled = walk_book(levels, sized.qty)
        if filled + 1e-12 < sized.qty:
            return self._reject("tahta derinliği yetersiz")
        return {"opened": self._record_entry(plan, sized, fill, quote, now)}

    def _record_entry(self, plan: Plan, sized: Sized, fill: float, quote: Quote, now: float) -> dict:
        notional = fill * sized.qty
        fee = fee_usd(notional, self.venue.taker_pct)
        slip = abs(fill - quote.mid) * sized.qty
        leverage = sized.leverage
        position = {
            "id": uuid.uuid4().hex[:10],
            "symbol": plan.symbol,
            "direction": plan.direction,
            "origin": plan.origin,
            "strategy": plan.strategy,
            "reason": plan.reason,
            "session_id": plan.session_id,
            "qty": sized.qty,
            "entry": fill,
            "stop": plan.stop,
            "initial_stop": plan.stop,
            "target": plan.target,
            "trail_atr": plan.trail_atr,
            "bar_hours": plan.bar_hours,
            "max_hold_hours": plan.max_hold_hours or self.limits.max_hold_hours,
            "leverage": leverage,
            "margin": notional / leverage,
            "liquidation": liquidation_price(plan.direction, fill, leverage, self.venue.maintenance_pct),
            "opened_at": now,
            "entry_fee": fee,
            "entry_slippage": slip,
            "funding": 0.0,
            "next_funding_ts": quote.funding_next_ts,
            "pending_funding_rate": quote.funding_rate,
            "last_mark": quote.mid,
            # Candles that started before this time are ignored by the exit check.
            "range_from_ms": next_minute_ms(now),
        }
        self.state["balance"] -= fee
        self.state["costs"]["taker_fee"] += fee
        self.state["costs"]["slippage"] += slip
        self.state["open"].append(position)
        self.state["last_open_ts"][plan.symbol] = now
        return position

    def _reject(self, reason: str) -> dict:
        key = reason.split(":")[0]
        self.state["rejected"][key] = self.state["rejected"].get(key, 0) + 1
        return {"rejected": reason}

    # ---------------------------------------------------------------- position updates

    def on_quote(self, symbol: str, quote: Quote) -> List[dict]:
        """Applies funding and exits for symbol. Returns the closed trades."""
        closed = []
        for p in [x for x in self.state["open"] if x["symbol"] == symbol]:
            p["last_mark"] = quote.mid
            self._apply_funding(p, quote)
            trade = self._check_exit(p, quote)
            if trade:
                closed.append(trade)
            else:
                p["range_from_ms"] = max(p["range_from_ms"], int(quote.ts // 60) * 60_000)
        self._update_drawdown({symbol: quote.mid})
        return closed

    def update_trail(self, symbol: str, bar_hours: float, close: float, atr: float, now: Optional[float] = None) -> None:
        """Moves trailing stops after a bar closes. A stop only ever moves toward profit."""
        now = time.time() if now is None else now
        for p in self.state["open"]:
            if p["symbol"] != symbol or not p.get("trail_atr") or p.get("bar_hours") != bar_hours:
                continue
            s = sign(p["direction"])
            candidate = close - s * p["trail_atr"] * atr
            moved = max(p["stop"], candidate) if s > 0 else min(p["stop"], candidate)
            if moved != p["stop"]:
                # A wick from before this move must not trigger the new stop.
                p["stop"] = moved
                p["range_from_ms"] = next_minute_ms(now)

    def _apply_funding(self, p: dict, quote: Quote):
        if quote.funding_rate is None or quote.funding_next_ts is None:
            return
        due = p.get("next_funding_ts")
        if due is None:
            p["next_funding_ts"] = quote.funding_next_ts
            p["pending_funding_rate"] = quote.funding_rate
            return
        if quote.ts < due:
            # The rate quoted before settlement is the one that settles.
            p["pending_funding_rate"] = quote.funding_rate
            return
        if quote.funding_next_ts <= due:
            return  # The venue has not rolled to the next period yet.
        rate = p.get("pending_funding_rate")
        rate = quote.funding_rate if rate is None else rate
        # More than one period can pass while the bot is offline.
        periods = max(1, round((quote.funding_next_ts - due) / self.venue.funding_interval_seconds))
        # Longs pay shorts when the rate is positive.
        payment = quote.mid * p["qty"] * rate * sign(p["direction"]) * periods
        p["funding"] += payment
        self.state["balance"] -= payment
        self.state["costs"]["funding"] += payment
        p["next_funding_ts"] = quote.funding_next_ts
        p["pending_funding_rate"] = quote.funding_rate

    def exit_signal(self, p: dict, quote: Quote) -> Optional[str]:
        """Which exit this quote triggers: "stop", "target", "time" or None. Fills nothing."""
        s = sign(p["direction"])
        levels = quote.bids if s > 0 else quote.asks
        if not levels:
            return None
        touch = levels[0][0]
        low = min(x for x in (quote.low, touch) if x is not None)
        high = max(x for x in (quote.high, touch) if x is not None)
        adverse, favorable = (low, high) if s > 0 else (high, low)
        if (adverse - p["stop"]) * s <= 0:
            return "stop"
        # A resting limit needs a trade strictly through its price.
        if p["target"] is not None and (favorable - p["target"]) * s > 0:
            return "target"
        if quote.ts - p["opened_at"] >= p["max_hold_hours"] * 3600.0:
            return "time"
        return None

    def _check_exit(self, p: dict, quote: Quote) -> Optional[dict]:
        kind = self.exit_signal(p, quote)
        if kind is None:
            return None
        levels = quote.bids if sign(p["direction"]) > 0 else quote.asks
        if kind == "stop":
            return self._exit_stop(p, quote, levels)
        if kind == "target":
            return self._close(p, p["target"], is_maker=True, reason="hedef", slippage=0.0, now=quote.ts)
        return self._exit_market(p, quote, levels, "süre doldu")

    def _exit_stop(self, p: dict, quote: Quote, levels) -> dict:
        s = sign(p["direction"])
        walked, filled = walk_book(levels, p["qty"])
        slip_rate = self.limits.stop_slippage_bps / 10_000.0
        slipped_stop = p["stop"] * (1.0 - s * slip_rate)
        fill = min(walked, slipped_stop) if s > 0 else max(walked, slipped_stop)
        if filled + 1e-12 < p["qty"]:
            fill = slipped_stop
        slippage = abs(fill - p["stop"]) * p["qty"]
        return self._close(p, fill, is_maker=False, reason="stop", slippage=slippage, now=quote.ts)

    def _exit_market(self, p: dict, quote: Quote, levels, reason: str) -> dict:
        walked, filled = walk_book(levels, p["qty"])
        fill = walked if filled + 1e-12 >= p["qty"] else levels[-1][0]
        return self._close(p, fill, is_maker=False, reason=reason,
                           slippage=abs(fill - quote.mid) * p["qty"], now=quote.ts)

    def close_all(self, quotes: Dict[str, Quote], reason: str = "elle") -> List[dict]:
        closed = []
        for p in list(self.state["open"]):
            q = quotes.get(p["symbol"])
            if q:
                levels = q.bids if sign(p["direction"]) > 0 else q.asks
                closed.append(self._exit_market(p, q, levels, reason))
        return closed

    def _close(self, p: dict, fill: float, is_maker: bool, reason: str, slippage: float, now: float) -> dict:
        s = sign(p["direction"])
        gross = (fill - p["entry"]) * p["qty"] * s
        liquidated = (fill - p["liquidation"]) * s <= 0
        if liquidated:
            # Isolated margin: the margin is lost and no exit fee is charged here.
            gross = -p["margin"]
            exit_fee = 0.0
            reason = "tasfiye"
            self.state["liquidations"] += 1
        else:
            pct = self.venue.maker_pct if is_maker else self.venue.taker_pct
            exit_fee = fee_usd(fill * p["qty"], pct)
        self.state["balance"] += gross - exit_fee
        self.state["costs"]["maker_fee" if is_maker else "taker_fee"] += exit_fee
        self.state["costs"]["slippage"] += slippage
        net = gross - p["entry_fee"] - exit_fee - p["funding"]
        self.state["wins" if net > 0 else "losses"] += 1
        trade = {
            **p,
            "exit": fill,
            "exit_reason": reason,
            "closed_at": now,
            "gross": gross,
            "exit_fee": exit_fee,
            "exit_slippage": slippage,
            "net": net,
            "liquidated": liquidated,
        }
        self.state["open"].remove(p)
        self.state["history"].append(trade)
        del self.state["history"][:-MAX_HISTORY]
        return trade

    def _update_drawdown(self, marks: Dict[str, float]):
        equity = self.equity(marks)
        self.state["peak_equity"] = max(self.state["peak_equity"], equity)
        self.state["max_drawdown"] = max(self.state["max_drawdown"], self.state["peak_equity"] - equity)
        kill = self.limits.kill_drawdown_usd
        if kill > 0 and equity <= self.limits.capital_usd - kill and not self.state["halted"]:
            self.state["halted"] = f"özsermaye {equity:.2f}$ zarar limitine indi"
