"""Scans for signals at each bar close, runs the checks, opens with an exchange stop, trails it, and books results.

The bot's own ledger holds the $100 budget: the exchange account may hold more (the demo account has 5000 USDT),
but the bot never ties up more than its budget. Results come from the exchange's own fills, fees and funding.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict
from typing import Dict, List, Optional

import aiohttp

from . import config
from .binance import Binance, BinanceError, round_price
from .strategy import BY_KEY, STRATEGIES, Decision, bar_atr, bars_from_klines, decide, latest_signal, trail_stop

logger = logging.getLogger("levbot")
MAX_ROWS = 300


def fresh_state() -> dict:
    return {"started_at": time.time(), "budget": config.BUDGET_USD, "open": [], "closed": [], "decisions": [],
            "last_bar": {}, "curve": [], "peak": config.BUDGET_USD, "events": [], "unknown_positions": []}


class Engine:
    def __init__(self, client: Binance, state: Optional[dict] = None, notify=None):
        self.client = client
        self.state = state or fresh_state()
        self.notify = notify
        self.marks: Dict[str, dict] = {}
        self.errors: Dict[str, str] = {}
        self.last_scan: Dict[str, float] = {}
        self.next_scan_at = 0.0
        self.lock = asyncio.Lock()

    # ---------------------------------------------------------------- bookkeeping

    def log(self, kind: str, text: str):
        self.state["events"].insert(0, {"at": time.time(), "kind": kind, "text": text})
        del self.state["events"][MAX_ROWS:]
        logger.info("%s: %s", kind, text)

    async def tell(self, text: str):
        if self.notify:
            try:
                await self.notify(text)
            except Exception as e:  # A Telegram outage must never stop trading.
                logger.warning("Telegram: %s", e)

    def unrealized(self) -> float:
        return sum(float(self.marks.get(t["symbol"], {}).get("unrealized", 0.0)) for t in self.state["open"])

    def closed_net(self) -> float:
        return sum(t["net"] for t in self.state["closed"])

    def equity(self) -> float:
        open_fees = sum(t.get("entry_fee", 0.0) for t in self.state["open"])
        return self.state["budget"] + self.closed_net() + self.unrealized() - open_fees

    def realized_equity(self) -> float:
        """Budget plus closed trades only. The drawdown stop reads this: giving back open profit is not a loss."""
        return self.state["budget"] + self.closed_net()

    def drawdown(self) -> float:
        return max(0.0, self.state["peak"] - self.realized_equity())

    def day_net(self, now: Optional[float] = None) -> float:
        day = int((now or time.time()) // 86400)
        return sum(t["net"] for t in self.state["closed"] if int(t["closed_at"] // 86400) == day)

    def book_view(self, symbol: str, direction: str) -> dict:
        open_ = self.state["open"]
        margin = sum(t["margin"] for t in open_)
        eq = self.equity()
        return {"free": max(0.0, eq - margin), "open_count": len(open_),
                "symbol_open": any(t["symbol"] == symbol for t in open_),
                "same_direction": sum(t["direction"] == direction for t in open_),
                "day_net": self.day_net(), "drawdown": self.drawdown()}

    def record_decision(self, d: Decision):
        row = {"strategy": d.strategy, "symbol": d.symbol, "direction": d.direction, "bar_ts": d.bar_ts, "at": d.at,
               "result": d.result, "passed": d.passed, "checks": [asdict(c) for c in d.checks],
               "plan": asdict(d.plan) if d.plan else None}
        self.state["decisions"].insert(0, row)
        del self.state["decisions"][MAX_ROWS:]

    # ---------------------------------------------------------------- scanning

    @staticmethod
    def bar_closed_at(interval_hours: int, now: float) -> float:
        step = interval_hours * 3600
        return now - (now % step)

    async def scan_once(self, now: Optional[float] = None):
        """Runs once per closed bar of each interval, within SCAN_WINDOW_SECONDS of its close."""
        now = now or time.time()
        due = {}
        for hours in sorted({s.bar_hours for s in STRATEGIES}):
            closed = self.bar_closed_at(hours, now)
            if now - closed <= config.SCAN_WINDOW_SECONDS and self.last_scan.get(f"{hours}h") != closed and now - closed >= 3:
                due[hours] = closed
        self.next_scan_at = self.bar_closed_at(1, now) + 3600 + 3
        if not due:
            return
        async with self.lock:
            for hours, closed in due.items():
                self.last_scan[f"{hours}h"] = closed
                await self._scan_interval(hours, now)

    async def _scan_interval(self, hours: int, now: float):
        interval = f"{hours}h"
        strategies = [s for s in STRATEGIES if s.bar_hours == hours]
        sem = asyncio.Semaphore(5)

        async def load(symbol):
            async with sem:
                try:
                    k = await self.client.klines(symbol, interval, max(s.bars_needed for s in strategies) + 1)
                    f = await self.client.funding_history(symbol, 50)
                    return symbol, bars_from_klines(symbol, k, hours, f, now * 1000)
                except Exception as e:
                    self.errors[f"veri {symbol}"] = f"{type(e).__name__}: {e}"
                    return symbol, None

        loaded = dict(await asyncio.gather(*(load(s) for s in config.SYMBOLS)))
        for trade in list(self.state["open"]):
            b = loaded.get(trade["symbol"])
            if b is not None and BY_KEY[trade["strategy"]].bar_hours == hours:
                await self._manage(trade, b, now)
        signals = scanned = 0
        for s in strategies:
            for symbol, b in loaded.items():
                if b is None:
                    continue
                key = f"{s.key}|{symbol}"
                bar_ts = float(b.ts[-1])
                if self.state["last_bar"].get(key, 0) >= bar_ts:
                    continue
                self.state["last_bar"][key] = bar_ts
                scanned += 1
                sig = latest_signal(s, b)
                if not sig:
                    continue
                signals += 1
                await self._consider(s, b, sig[0], sig[1], now)
        if scanned:
            missing = sum(b is None for b in loaded.values())
            self.log("tarama", f"{interval} mumları: {len(loaded) - missing} parite, {len(strategies)} strateji tarandı, "
                               f"{signals} sinyal" + (f", {missing} paritenin verisi okunamadı" if missing else ""))
        self.errors.pop("tarama", None)

    async def _consider(self, s, b, direction: str, signal_atr: float, now: float):
        try:
            book = await self.client.book_ticker(b.symbol)
            prem = await self.client.premium(b.symbol)
            market = {"bid": float(book["bidPrice"]), "ask": float(book["askPrice"]), "funding": float(prem["lastFundingRate"])}
        except Exception as e:
            d = Decision(s.key, b.symbol, direction, float(b.ts[-1]), now, result=f"Girilmedi: fiyat okunamadı ({type(e).__name__})")
            self.record_decision(d)
            return
        rules = self.client.rules.get(b.symbol)
        if not rules:
            return
        d = decide(s, b, direction, signal_atr, market, rules, self.book_view(b.symbol, direction), now)
        if not d.passed:
            d.result = "Girilmedi: " + "; ".join(c.name for c in d.failed())
            self.record_decision(d)
            self.log("sinyal", f"{s.label} {b.symbol} {direction}: {d.result}")
            return
        await self._open(s, d)

    # ---------------------------------------------------------------- orders

    async def _open(self, s, d: Decision):
        plan, symbol = d.plan, d.symbol
        side = "BUY" if d.direction == "LONG" else "SELL"
        exit_side = "SELL" if side == "BUY" else "BUY"
        t0 = int(time.time() * 1000) - 1000
        try:
            await self.client.set_isolated(symbol)
            await self.client.set_leverage(symbol, plan.leverage)
            order = await self.client.market(symbol, side, plan.qty, client_id=f"lb{uuid.uuid4().hex[:20]}")
        except BinanceError as e:
            d.result = f"Emir reddedildi: {e.msg}"
            self.record_decision(d)
            self.log("hata", f"{symbol} giriş emri reddedildi: {e.msg}")
            await self.tell(f"⚠️ {symbol} giriş emri reddedildi: {e.msg}")
            return
        qty = float(order.get("executedQty") or 0.0)
        fills = [t for t in await self._safe_trades(symbol, t0) if str(t.get("orderId")) == str(order.get("orderId"))]
        if fills:
            qty = sum(float(t["qty"]) for t in fills)
            entry = sum(float(t["price"]) * float(t["qty"]) for t in fills) / qty
            fee = sum(float(t["commission"]) for t in fills)
        else:
            entry = float(order.get("avgPrice") or plan.entry)
            fee = qty * entry * config.TAKER_FEE_PCT / 100.0
        if qty <= 0:
            d.result = "Emir dolmadı"
            self.record_decision(d)
            return
        rules = self.client.rules[symbol]
        distance = abs(plan.entry - plan.stop)
        stop = round_price(entry - distance if d.direction == "LONG" else entry + distance, rules)
        try:
            algo = await self.client.place_stop(symbol, exit_side, stop)
        except BinanceError as e:
            # Never hold a position without a stop on the exchange.
            await self.client.market(symbol, exit_side, qty, reduce_only=True)
            d.result = f"Zarar-kes kurulamadı, pozisyon hemen kapatıldı: {e.msg}"
            self.record_decision(d)
            self.log("hata", f"{symbol}: {d.result}")
            await self.tell(f"⚠️ {symbol}: {d.result}")
            return
        trade = {"id": uuid.uuid4().hex[:10], "strategy": s.key, "symbol": symbol, "direction": d.direction, "qty": qty,
                 "entry": entry, "entry_fee": fee, "opened_at": time.time(), "stop": stop, "initial_stop": stop,
                 "stop_algo_id": algo.get("algoId"), "risk": qty * distance, "margin": qty * entry / plan.leverage,
                 "leverage": plan.leverage, "notional": qty * entry, "bar_ts": d.bar_ts, "trail_moves": 0}
        self.state["open"].append(trade)
        d.result = f"Açıldı: {qty:g} @ {entry:.6g}, zarar-kes {stop:.6g}"
        self.record_decision(d)
        self.log("açılış", f"{s.label} {symbol} {d.direction} {qty:g} @ {entry:.6g}, zarar-kes {stop:.6g}, "
                           f"{plan.leverage}x, teminat {trade['margin']:.2f}$, en çok kayıp {trade['risk']:.2f}$")
        await self.tell(f"🟢 {s.label}: {symbol} {'ALIŞ' if d.direction == 'LONG' else 'SATIŞ'} açıldı\n"
                        f"Giriş {entry:.6g} · Zarar-kes {stop:.6g} · {plan.leverage}x · teminat {trade['margin']:.2f}$ · "
                        f"en çok kayıp {trade['risk']:.2f}$")

    async def _safe_trades(self, symbol: str, start_ms: int) -> List[dict]:
        try:
            return await self.client.trades(symbol, start_ms)
        except Exception:
            return []

    async def _manage(self, trade: dict, b, now: float):
        """After each bar of the trade's interval: close after the hold limit, otherwise trail the stop."""
        s = BY_KEY[trade["strategy"]]
        exit_side = "SELL" if trade["direction"] == "LONG" else "BUY"
        if now - trade["opened_at"] >= s.exits.max_hold_hours * 3600:
            trade["close_reason"] = "süre doldu"
            try:
                await self.client.market(trade["symbol"], exit_side, trade["qty"], reduce_only=True)
            except BinanceError as e:
                self.log("hata", f"{trade['symbol']} süre dolunca kapatılamadı: {e.msg}")
            return
        new = round_price(trail_stop(trade["direction"], trade["stop"], float(b.closes[-1]), bar_atr(b), s.exits),
                          self.client.rules[trade["symbol"]])
        better = new > trade["stop"] if trade["direction"] == "LONG" else new < trade["stop"]
        if not better:
            return
        try:
            algo = await self.client.place_stop(trade["symbol"], exit_side, new)
        except BinanceError as e:
            self.log("uyarı", f"{trade['symbol']} izleyen stop taşınamadı ({e.msg}); eski stop yerinde")
            return
        old = trade.get("stop_algo_id")
        if old:
            try:
                await self.client.cancel_stop(old)
            except BinanceError:
                pass
        self.log("stop", f"{trade['symbol']} zarar-kes {trade['stop']:.6g} → {new:.6g}")
        trade.update(stop=new, stop_algo_id=algo.get("algoId"), trail_moves=trade.get("trail_moves", 0) + 1)

    async def close_trade(self, trade_id: str, reason: str = "elle kapatıldı"):
        trade = next((t for t in self.state["open"] if t["id"] == trade_id), None)
        if not trade:
            return False
        trade["close_reason"] = reason
        await self.client.market(trade["symbol"], "SELL" if trade["direction"] == "LONG" else "BUY", trade["qty"], reduce_only=True)
        await self.sync_once()
        return True

    # ---------------------------------------------------------------- syncing with the exchange

    async def sync_once(self):
        try:
            positions = await self.client.positions()
        except Exception as e:
            self.errors["hesap"] = f"{type(e).__name__}: {e}"
            return
        self.errors.pop("hesap", None)
        live = {p["symbol"]: p for p in positions if float(p["positionAmt"]) != 0.0}
        ours = {t["symbol"] for t in self.state["open"]}
        self.state["unknown_positions"] = [{"symbol": s, "amount": float(p["positionAmt"])} for s, p in live.items() if s not in ours]
        for t in self.state["open"]:
            p = live.get(t["symbol"])
            if p:
                self.marks[t["symbol"]] = {"mark": float(p["markPrice"]), "unrealized": float(p["unRealizedProfit"]),
                                           "liquidation": float(p.get("liquidationPrice") or 0.0)}
        for t in list(self.state["open"]):
            if t["symbol"] not in live and time.time() - t["opened_at"] > 5:
                await self._book_close(t)
        eq = self.equity()
        self.state["peak"] = max(self.state["peak"], self.realized_equity())
        curve = self.state["curve"]
        if not curve or time.time() - curve[-1][0] >= 300:
            curve.append([time.time(), round(eq, 4)])
            del curve[:-2000]

    async def _book_close(self, t: dict):
        symbol = t["symbol"]
        start = int(t["opened_at"] * 1000) - 2000
        exit_side = "SELL" if t["direction"] == "LONG" else "BUY"
        fills = [f for f in await self._safe_trades(symbol, start) if f["side"] == exit_side and f["time"] / 1000 >= t["opened_at"] - 1]
        realized = sum(float(f["realizedPnl"]) for f in fills)
        exit_fee = sum(float(f["commission"]) for f in fills)
        qty = sum(float(f["qty"]) for f in fills)
        exit_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / qty if qty else self.marks.get(symbol, {}).get("mark", t["entry"])
        if not fills:
            sgn = 1 if t["direction"] == "LONG" else -1
            realized = (exit_price - t["entry"]) * t["qty"] * sgn
            exit_fee = exit_price * t["qty"] * config.TAKER_FEE_PCT / 100.0
        try:
            funding = sum(float(x["income"]) for x in await self.client.income(symbol, start))
        except Exception:
            funding = 0.0
        await self.client.cancel_all_stops(symbol)
        net = realized - t["entry_fee"] - exit_fee + funding
        reason = t.get("close_reason") or ("izleyen stop" if t.get("trail_moves") else "zarar-kes")
        closed = {**t, "exit": exit_price, "exit_fee": exit_fee, "realized": realized, "funding": funding, "net": net,
                  "r": net / t["risk"] if t["risk"] else 0.0, "closed_at": time.time(), "reason": reason}
        self.state["open"].remove(t)
        self.state["closed"].insert(0, closed)
        del self.state["closed"][2000:]
        self.marks.pop(symbol, None)
        label = BY_KEY[t["strategy"]].label
        self.log("kapanış", f"{label} {symbol} {t['direction']} kapandı ({reason}): net {net:+.2f}$")
        await self.tell(f"{'✅' if net > 0 else '🔻'} {label}: {symbol} kapandı ({reason})\nNet {net:+.2f}$ · "
                        f"giriş {t['entry']:.6g} → çıkış {exit_price:.6g} · toplam {self.closed_net():+.2f}$")

    # ---------------------------------------------------------------- loops

    async def run(self, save):
        await self.client.load_rules()
        await self.sync_once()
        save()
        last_save = time.time()
        while True:
            try:
                await self.scan_once()
            except Exception as e:
                logger.exception("scan failed")
                self.errors["tarama"] = f"{type(e).__name__}: {e}"
            await self.sync_once()
            if time.time() - last_save >= 30:
                save()
                last_save = time.time()
            await asyncio.sleep(config.SYNC_SECONDS)


def load_state() -> Optional[dict]:
    try:
        return json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(state: dict):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(config.STATE_FILE)
