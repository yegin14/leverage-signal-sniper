"""
LeverageSignalSniper - Paper Trading (Sanal Kasa) Engine
Gerçek para riske atmadan, canlı piyasa fiyatlarıyla birebir çalışan
kurumsal sanal işlem ve kasa takip simülatörü.
"""

import json
import os
import time
from config import ACCOUNT_SIZE_USD

STATE_FILE = os.path.join(os.path.dirname(__file__), "paper_trading_state.json")

class PaperTrader:
    def __init__(self, initial_balance: float = ACCOUNT_SIZE_USD, max_open_positions: int = 3):
        self.initial_balance = initial_balance
        self.max_open_positions = max_open_positions
        self.state = {
            "initial_balance": initial_balance,
            "balance": initial_balance,
            "realized_pnl": 0.0,
            "total_trades": 0,
            "win_trades": 0,
            "loss_trades": 0,
            "open_positions": {},
            "history": []
        }
        self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.state.update(loaded)

                # Saf Kripto Temizliği: Eski oturumdan kalan Altın ve hisse pozisyonlarının teminatını iade et
                non_crypto = [s for s in list(self.state.get("open_positions", {}).keys()) if s in ("XAUUSDT", "SPCXUSDT", "SOXLUSDT", "CLUSDT")]
                for s in non_crypto:
                    pos = self.state["open_positions"].pop(s)
                    self.state["balance"] += pos.get("remaining_margin_usd", 0.0)
                    print(f"[PaperTrader] Kripto dışı sembol tasfiye edildi ve teminat iade edildi: {s}")
                if non_crypto:
                    self.save_state()
            except Exception as e:
                print(f"[PaperTrader] State yüklenirken hata: {e}")

    def save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[PaperTrader] State kaydedilirken hata: {e}")

    def get_summary(self) -> dict:
        total = self.state["total_trades"]
        wins = self.state["win_trades"]
        losses = self.state["loss_trades"]
        win_rate = (wins / total * 100.0) if total > 0 else 0.0
        bal = self.state["balance"]
        pnl = self.state["realized_pnl"]
        ret_pct = (pnl / self.initial_balance) * 100.0

        return {
            "balance": bal,
            "initial_balance": self.initial_balance,
            "realized_pnl": pnl,
            "return_pct": ret_pct,
            "total_trades": total,
            "win_trades": wins,
            "loss_trades": losses,
            "win_rate": win_rate,
            "open_count": len(self.state["open_positions"])
        }

    def open_virtual_trade(self, sig: dict) -> dict | None:
        """Sinyali sanal olarak işleme alır"""
        sym = sig["symbol"]
        
        # Zaten aynı paritede açık işlem varsa veya limit dolduysa açma
        if sym in self.state["open_positions"]:
            return None
        if len(self.state["open_positions"]) >= self.max_open_positions:
            return None

        margin = sig["margin_needed_usd"]
        if self.state["balance"] < margin:
            return None

        # Bakiyeden teminatı düş
        self.state["balance"] -= margin

        trade_id = f"T_{sym}_{int(time.time())}"
        position = {
            "trade_id": trade_id,
            "symbol": sym,
            "direction": sig["direction"],
            "entry_price": sig["entry_price"],
            "stop_loss": sig["stop_loss"],
            "original_stop": sig["stop_loss"],
            "tp1": sig["tp1"],
            "tp2": sig["tp2"],
            "tp3": sig["tp3"],
            "leverage": sig["recommended_leverage"],
            "margin_usd": margin,
            "remaining_margin_usd": margin,
            "position_size_usd": sig["target_position_usd"],
            "remaining_position_usd": sig["target_position_usd"],
            "max_risk_usd": sig["max_risk_usd"],
            "tp1_hit": False,
            "tp2_hit": False,
            "tp3_hit": False,
            "is_risk_free": False,  # TP1 vurulunca stop girişe çekilir
            "realized_pnl": 0.0,
            "open_time": time.time(),
            "tv_badge": sig.get("tv_badge", "N/A"),
            "confidence": sig["confidence"]
        }

        self.state["open_positions"][sym] = position
        self.save_state()
        return position

    def update_positions(self, current_prices: dict[str, float]) -> list[dict]:
        """Canlı borsa fiyatlarıyla tüm açık işlemleri denetler ve tetiklenenleri kapatır"""
        events = []
        closed_syms = []

        for sym, pos in list(self.state["open_positions"].items()):
            cur_price = current_prices.get(sym)
            if not cur_price:
                continue

            direction = pos["direction"]
            entry = pos["entry_price"]
            stop = pos["stop_loss"]
            tp1 = pos["tp1"]
            tp2 = pos["tp2"]
            tp3 = pos["tp3"]

            # --- 1. STOP LOSS KONTROLÜ ---
            is_sl_hit = (cur_price <= stop) if direction == "LONG" else (cur_price >= stop)
            if is_sl_hit:
                # Eğer işlem TP1 vurulduktan sonra girişe çekildiyse zarar $0'dır
                if pos["is_risk_free"]:
                    sl_pnl = 0.0
                    reason = "🛑 BREAK-EVEN STOP (Zararsız Kapanış)"
                else:
                    sl_pnl = -pos["max_risk_usd"]
                    reason = "🛑 STOP-LOSS TETİKLENDİ"

                # Kalan teminatı ve PnL'i bakiyeye iade et
                self.state["balance"] += pos["remaining_margin_usd"] + sl_pnl
                self.state["realized_pnl"] += pos["realized_pnl"] + sl_pnl
                self.state["total_trades"] += 1
                if (pos["realized_pnl"] + sl_pnl) > 0:
                    self.state["win_trades"] += 1
                else:
                    self.state["loss_trades"] += 1

                pos["close_price"] = cur_price
                pos["close_time"] = time.time()
                pos["total_pnl"] = pos["realized_pnl"] + sl_pnl
                pos["exit_reason"] = reason
                self.state["history"].append(pos)
                closed_syms.append(sym)

                events.append({
                    "event": "SL_HIT",
                    "position": pos,
                    "pnl": sl_pnl,
                    "close_price": cur_price,
                    "reason": reason
                })
                continue

            # --- 2. TP1 KONTROLÜ (Pozisyonun %100'ü Kapatılır - Anında Kâr Al ve Çık) ---
            is_tp1_hit = (cur_price >= tp1) if direction == "LONG" else (cur_price <= tp1)
            if is_tp1_hit or pos.get("tp1_hit"):
                rem_margin = pos["remaining_margin_usd"]
                # Eğer daha önce kısmi kâr alınmadıysa tam 1.5R kâr al
                if not pos.get("tp1_hit"):
                    tp1_pnl = pos["max_risk_usd"] * 1.5  # 1.5R Net Kâr ($3.00)
                    pos["realized_pnl"] += tp1_pnl
                else:
                    tp1_pnl = 0.0

                # Anapara teminatını ve kârı bakiyeye iade et
                self.state["balance"] += rem_margin + tp1_pnl
                self.state["realized_pnl"] += tp1_pnl
                self.state["total_trades"] += 1
                self.state["win_trades"] += 1

                pos["close_price"] = cur_price
                pos["close_time"] = time.time()
                pos["total_pnl"] = pos["realized_pnl"]
                pos["exit_reason"] = "🎯 TP1 KÂR ALINDI (Pozisyon %100 Başarıyla Kapatıldı)"
                self.state["history"].append(pos)
                closed_syms.append(sym)

                events.append({
                    "event": "TP1_HIT",
                    "position": pos,
                    "pnl": pos["realized_pnl"],
                    "close_price": cur_price,
                    "reason": "🎯 TP1 Hedefi Vuruldu - Pozisyon %100 Kapatıldı"
                })
                continue

            # --- 3. TP2 KONTROLÜ (Kalanın %30'u Kapatılır) ---
            is_tp2_hit = (cur_price >= tp2) if direction == "LONG" else (cur_price <= tp2)
            if is_tp2_hit and not pos["tp2_hit"]:
                close_ratio = 0.30
                freed_margin = pos["margin_usd"] * close_ratio
                tp2_pnl = pos["max_risk_usd"] * 2.5 * close_ratio  # 2.5R kâr
                
                pos["realized_pnl"] += tp2_pnl
                pos["remaining_margin_usd"] -= freed_margin
                pos["remaining_position_usd"] -= (pos["position_size_usd"] * close_ratio)
                pos["tp2_hit"] = True
                
                self.state["balance"] += freed_margin + tp2_pnl

                events.append({
                    "event": "TP2_HIT",
                    "position": pos,
                    "pnl": tp2_pnl,
                    "close_price": cur_price
                })

            # --- 4. TP3 KONTROLÜ (Tüm Kalan Kapatılır - Tam Başarı) ---
            is_tp3_hit = (cur_price >= tp3) if direction == "LONG" else (cur_price <= tp3)
            if is_tp3_hit and not pos["tp3_hit"]:
                rem_margin = pos["remaining_margin_usd"]
                tp3_pnl = pos["max_risk_usd"] * 4.0 * 0.20  # Kalan %20 için 4R kâr
                
                pos["realized_pnl"] += tp3_pnl
                self.state["balance"] += rem_margin + tp3_pnl
                self.state["realized_pnl"] += pos["realized_pnl"]
                self.state["total_trades"] += 1
                self.state["win_trades"] += 1

                pos["close_price"] = cur_price
                pos["close_time"] = time.time()
                pos["total_pnl"] = pos["realized_pnl"]
                pos["exit_reason"] = "🎯🎯🎯 TP3 FULL HEDEF VURULDU"
                self.state["history"].append(pos)
                closed_syms.append(sym)

                events.append({
                    "event": "TP3_HIT",
                    "position": pos,
                    "pnl": tp3_pnl,
                    "close_price": cur_price
                })

        for sym in closed_syms:
            if sym in self.state["open_positions"]:
                del self.state["open_positions"][sym]

        if events:
            self.save_state()

        return events
