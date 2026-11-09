"""The three strategies and the check list every entry has to pass.

Chosen on 2026-11-09 from tradebot.research (20 coins, two years, costs included, last 40% scored once):
  trend4h_confirmed  4h 20-bar breakout, 5 of 6 confirmations: +0.20R a trade, n=118, beat 95% of random runs
  trend1h            1h 24-bar breakout:                        +0.11R a trade, n=503, beat 100% of random runs
  trend1h_confirmed  1h 72-bar breakout, all 6 confirmations:   +0.08R a trade, n=229, beat 88% of random runs
None is statistically proven (t below 2). Each runs from the same $100 budget at most $10 margin a trade.
"""

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np

from tradebot.strategies import (CONFIRMATIONS, TREND_1H_EXITS, TREND_4H_EXITS, Bars, ExitRule, atr, confirmations,
                                 trend_breakout)

from . import config
from .binance import SymbolRules, round_qty

HOUR_MS = 3_600_000


@dataclass(frozen=True)
class StrategyDef:
    key: str
    label: str
    description: str
    interval: str
    bar_hours: int
    bars_needed: int
    base: Callable[[Bars], list]
    need: int          # confirmations that must agree (0: shown for information only)
    htf_bars: int      # bars of this interval in one bar of the higher timeframe
    exits: ExitRule


STRATEGIES = (
    StrategyDef("trend4h_confirmed", "4 saat trend + onay",
                "4 saatlik mum son 20 mumun en yükseğini (en düşüğünü) kırar ve 50/200 ortalamalar aynı yönü gösterir. "
                "6 onaydan en az 5'i tutmalı. Zarar-kes 2,5 ATR, kâr 3 ATR geriden izlenir.",
                "4h", 4, 500, trend_breakout(20), 5, 6, TREND_4H_EXITS),
    StrategyDef("trend1h", "1 saat trend",
                "1 saatlik mum son 24 mumun en yükseğini (en düşüğünü) kırar ve 50/200 ortalamalar aynı yönü gösterir. "
                "Onaylar bilgi için gösterilir. Zarar-kes 2 ATR, kâr 3 ATR geriden izlenir.",
                "1h", 1, 500, trend_breakout(24), 0, 4, TREND_1H_EXITS),
    StrategyDef("trend1h_confirmed", "1 saat trend + tam onay",
                "1 saatlik mum son 72 mumun en yükseğini (en düşüğünü) kırar, ortalamalar aynı yönde ve 6 onayın hepsi tutar. "
                "Zarar-kes 2 ATR, kâr 3 ATR geriden izlenir.",
                "1h", 1, 500, trend_breakout(72), 6, 4, TREND_1H_EXITS),
)
BY_KEY = {s.key: s for s in STRATEGIES}


@dataclass
class Check:
    group: str
    name: str
    ok: bool
    detail: str
    info_only: bool = False


@dataclass
class Plan:
    entry: float
    stop: float
    qty: float
    notional: float
    leverage: int
    margin: float
    risk: float
    liq_distance_pct: float
    stop_pct: float
    cost: float


@dataclass
class Decision:
    strategy: str
    symbol: str
    direction: str
    bar_ts: float
    at: float
    checks: List[Check] = field(default_factory=list)
    plan: Optional[Plan] = None
    result: str = ""

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks if not c.info_only)

    def failed(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and not c.info_only]


def bars_from_klines(symbol: str, klines: List[list], bar_hours: int, funding: List[dict], now_ms: float) -> Bars:
    """Closed bars only: the bar still forming is dropped."""
    rows = [k for k in klines if float(k[6]) < now_ms]
    a = np.array([[float(x) for x in k[:6]] for k in rows], dtype=np.float64)
    f = np.array([[float(x["fundingTime"]), float(x["fundingRate"])] for x in funding], dtype=np.float64) if funding else np.zeros((0, 2))
    return Bars(symbol, bar_hours * HOUR_MS, a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5], f[:, 0], f[:, 1])


def latest_signal(s: StrategyDef, b: Bars) -> Optional[tuple]:
    """(direction, atr) when the newest closed bar gives the base signal."""
    if len(b.ts) < 210:
        return None
    hits = [h for h in s.base(b) if h[0] == len(b.ts) - 1]
    return (hits[0][1], hits[0][2]) if hits else None


def size(direction: str, entry: float, signal_atr: float, exits: ExitRule, rules: SymbolRules) -> Optional[Plan]:
    distance = float(np.clip(signal_atr * exits.stop_atr, entry * config.MIN_STOP_PCT / 100.0, entry * config.MAX_STOP_PCT / 100.0))
    sgn = 1.0 if direction == "LONG" else -1.0
    stop = entry - sgn * distance
    qty = config.RISK_PER_TRADE_USD / distance
    qty = min(qty, config.MARGIN_PER_TRADE_USD * config.MAX_LEVERAGE / entry)
    qty = round_qty(qty, rules)
    if qty <= 0:
        return None
    notional = qty * entry
    leverage = int(min(config.MAX_LEVERAGE, max(1, math.ceil(notional / config.MARGIN_PER_TRADE_USD - 1e-9))))
    margin = notional / leverage
    liq_distance_pct = max(0.0, 100.0 / leverage - 0.5)  # isolated margin, maintenance rate about 0.5%
    cost = notional * 2 * (config.TAKER_FEE_PCT + config.SLIPPAGE_PCT) / 100.0
    return Plan(entry, stop, qty, notional, leverage, margin, qty * distance, liq_distance_pct, distance / entry * 100.0, cost)


def decide(s: StrategyDef, b: Bars, direction: str, signal_atr: float, market: Dict[str, float], rules: SymbolRules,
           book: Dict[str, float], now: Optional[float] = None) -> Decision:
    """
    Runs every check for one signal. market: bid, ask, funding (latest rate). book: free, open_count, symbol_open,
    same_direction, day_net, drawdown. Nothing here sends an order.
    """
    now = now or time.time()
    i = len(b.ts) - 1
    bar_close_s = (b.ts[i] + b.bar_ms) / 1000.0
    d = Decision(s.key, b.symbol, direction, float(b.ts[i]), now)
    add = d.checks.append
    long = direction == "LONG"

    age = now - bar_close_s
    add(Check("Sinyal", "Yeni kapanan mumda kırılım", True,
              f"{s.interval} mum {time.strftime('%H:%M', time.gmtime(bar_close_s))} UTC'de {b.closes[i]:.6g} ile kapandı"))
    add(Check("Sinyal", "Sinyal taze", age <= config.SCAN_WINDOW_SECONDS, f"mum kapanalı {age:.0f} sn (en çok {config.SCAN_WINDOW_SECONDS:.0f})"))

    oks = confirmations(b, i, direction, s.htf_bars, {})
    for name, ok in zip(CONFIRMATIONS, oks):
        add(Check("Onaylar", name, ok, "tuttu" if ok else "tutmadı", info_only=True))
    count = sum(oks)
    if s.need:
        add(Check("Onaylar", f"En az {s.need}/6 onay", count >= s.need, f"{count}/6 onay tuttu"))
    else:
        add(Check("Onaylar", "Onay sayısı (bilgi)", True, f"{count}/6 onay tuttu; bu strateji onay şartı aramaz", info_only=True))

    bid, ask = market["bid"], market["ask"]
    mid = (bid + ask) / 2.0
    spread = (ask - bid) / mid * 100.0
    add(Check("Piyasa", "Alış-satış farkı dar", spread <= config.MAX_SPREAD_PCT, f"%{spread:.3f} (en çok %{config.MAX_SPREAD_PCT})"))
    entry = ask if long else bid
    run = (entry - b.closes[i]) * (1 if long else -1) / signal_atr if signal_atr > 0 else 0.0
    add(Check("Piyasa", "Fiyat kaçmadı", run <= config.MAX_RUNAWAY_ATR, f"kapanıştan {run:+.2f} ATR (en çok {config.MAX_RUNAWAY_ATR})"))
    rate = market["funding"] * 100.0
    against = rate if long else -rate
    add(Check("Piyasa", "Fonlama aleyhte aşırı değil", against <= config.MAX_FUNDING_AGAINST_PCT,
              f"fonlama %{rate:+.4f} / 8 saat"))

    add(Check("Risk", "Toplam düşüş sınırı", book["drawdown"] < config.DRAWDOWN_HALT_USD,
              f"zirveden {book['drawdown']:.2f}$ (durma {config.DRAWDOWN_HALT_USD:g}$)"))
    add(Check("Risk", "Günlük zarar sınırı", -book["day_net"] < config.DAILY_LOSS_STOP_USD,
              f"bugün {book['day_net']:+.2f}$ (durma −{config.DAILY_LOSS_STOP_USD:g}$)"))
    add(Check("Risk", "Bu paritede açık pozisyon yok", not book["symbol_open"], "yok" if not book["symbol_open"] else "var"))
    add(Check("Risk", "Açık pozisyon sayısı", book["open_count"] < config.MAX_OPEN_POSITIONS,
              f"{book['open_count']} açık (en çok {config.MAX_OPEN_POSITIONS})"))
    add(Check("Risk", "Aynı yönde pozisyon sayısı", book["same_direction"] < config.MAX_SAME_DIRECTION,
              f"{book['same_direction']} {'alış' if long else 'satış'} (en çok {config.MAX_SAME_DIRECTION})"))

    plan = size(direction, entry, signal_atr, s.exits, rules)
    if plan is None:
        add(Check("Boyut", "Emir büyüklüğü hesaplandı", False, "miktar sıfıra yuvarlandı"))
        return d
    d.plan = plan
    add(Check("Boyut", "Teminat bütçede", plan.margin <= book["free"] and plan.margin <= config.MARGIN_PER_TRADE_USD + 1e-9,
              f"{plan.margin:.2f}$ teminat, boşta {book['free']:.2f}$ (işlem başı en çok {config.MARGIN_PER_TRADE_USD:g}$)"))
    add(Check("Boyut", "Borsanın en küçük emri", plan.notional >= rules.min_notional and plan.qty >= rules.min_qty,
              f"{plan.notional:.2f}$ pozisyon (borsa en az {rules.min_notional:g}$ ister)"))
    add(Check("Boyut", "Zarar-kes makul", config.MIN_STOP_PCT - 1e-9 <= plan.stop_pct <= config.MAX_STOP_PCT + 1e-9,
              f"%{plan.stop_pct:.2f} uzakta, en çok kayıp {plan.risk:.2f}$"))
    add(Check("Boyut", "Tasfiye zarar-kesten çok uzak", plan.liq_distance_pct >= config.MIN_LIQ_TO_STOP * plan.stop_pct,
              f"{plan.leverage}x kaldıraç: tasfiye ~%{plan.liq_distance_pct:.1f}, zarar-kes %{plan.stop_pct:.2f}"))
    add(Check("Boyut", "Masraf riske göre küçük", plan.cost <= config.MAX_COST_SHARE_OF_RISK * plan.risk,
              f"komisyon+kayma ~{plan.cost:.3f}$, riskin %{plan.cost / plan.risk * 100 if plan.risk else 0:.0f}'i"))
    return d


def trail_stop(direction: str, current: float, close: float, bar_atr: float, exits: ExitRule) -> float:
    """The stop after a bar closes: it only ever moves in the trade's favour."""
    if not exits.trail_atr or not np.isfinite(bar_atr):
        return current
    candidate = close - exits.trail_atr * bar_atr if direction == "LONG" else close + exits.trail_atr * bar_atr
    return max(current, candidate) if direction == "LONG" else min(current, candidate)


def bar_atr(b: Bars) -> float:
    a = atr(b)
    return float(a[-1]) if len(a) else float("nan")
