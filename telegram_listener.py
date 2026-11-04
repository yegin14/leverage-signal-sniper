"""
LeverageSignalSniper - Interactive Telegram Command & Menu Listener
Buton tıklamalarını <100ms içinde anında yanıtlayan, bağımsız asenkron modül.
"""

import asyncio
import aiohttp
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    MIN_RVOL, MIN_CONFIDENCE_SCORE, MAX_RISK_PER_TRADE_USD
)
from paper_trader import PaperTrader

class TelegramCommandListener:
    def __init__(self, paper_trader: PaperTrader, bot_token: str = TELEGRAM_BOT_TOKEN, chat_id: str = TELEGRAM_CHAT_ID):
        self.paper_trader = paper_trader
        self.bot_token = bot_token
        self.chat_id = str(chat_id).strip()
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.last_update_id = 0

    def get_menu_keyboard(self) -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "📊 Sanal Kasa", "callback_data": "cmd_kasa"},
                    {"text": "🎯 Açık İşlemler", "callback_data": "cmd_open"}
                ],
                [
                    {"text": "⚡ Canlı Radar", "callback_data": "cmd_radar"},
                    {"text": "🏆 Performans", "callback_data": "cmd_perf"}
                ]
            ]
        }

    async def answer_callback(self, session: aiohttp.ClientSession, callback_id: str, alert_text: str = None):
        url = f"{self.base_url}/answerCallbackQuery"
        payload = {"callback_query_id": callback_id}
        if alert_text:
            payload["text"] = alert_text
            payload["show_alert"] = False
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                pass
        except Exception:
            pass

    async def send_response(self, session: aiohttp.ClientSession, chat_id: str, text: str, reply_markup: dict = None):
        target_chat = chat_id or self.chat_id
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                pass
        except Exception:
            pass

    async def handle_command(self, session: aiohttp.ClientSession, chat_id: str, command: str):
        summary = self.paper_trader.get_summary()

        if command in ["/start", "/menu", "/durum", "cmd_menu"]:
            pnl_sign = "+" if summary["realized_pnl"] >= 0 else ""
            open_count = summary["open_count"]
            status_desc = f"{open_count} adet pozisyonda" if open_count > 0 else "Pusuda bekleniyor (Sinyal aranıyor)"
            text = (
                f"⚡ <b>[LEVERAGE SIGNAL SNIPER KONTROL PANELİ]</b> ⚡\n\n"
                f"👋 Merhaba Halil! Bot 7/24 kesintisiz uyanık ve aktiftir.\n\n"
                f"🌐 <b>Sunucu Durumu:</b> 🟢 Render Frankfurt (7/24 Kesintisiz)\n"
                f"📊 <b>Tarama Havuzu:</b> 50+ Vadeli Parite (Binance Futures)\n"
                f"💰 <b>Sanal Kasa Bakiyesi:</b> <code>${summary['balance']:.2f}</code>\n"
                f"⏳ <b>Açık İşlem Durumu:</b> {status_desc}\n"
                f"📈 <b>Toplam Net PnL:</b> {pnl_sign}${summary['realized_pnl']:.2f} (%{summary['return_pct']:.1f})\n\n"
                f"👇 <b>Aşağıdaki butonlara tıklayarak sistemi sorgulayabilirsin:</b>"
            )
            await self.send_response(session, chat_id, text, self.get_menu_keyboard())

        elif command in ["/kasa", "cmd_kasa"]:
            pnl_sign = "+" if summary["realized_pnl"] >= 0 else ""
            text = (
                f"📊 <b>[SANAL KASA GÜNCEL DURUMU]</b> 📊\n\n"
                f"💰 <b>Başlangıç Bakiyesi:</b> ${summary['initial_balance']:.2f}\n"
                f"💵 <b>Kullanılabilir Nakit:</b> <code>${summary['balance']:.2f}</code>\n"
                f"📈 <b>Toplam Net PnL:</b> <b>{pnl_sign}${summary['realized_pnl']:.2f}</b> (%{summary['return_pct']:.2f})\n"
                f"🎯 <b>Kazanma Oranı:</b> %{summary['win_rate']:.1f}\n"
                f"🔢 <b>Toplam Tamamlanan:</b> {summary['total_trades']} işlem\n"
                f"⏳ <b>Açıkta Bekleyen:</b> {summary['open_count']} işlem"
            )
            await self.send_response(session, chat_id, text, self.get_menu_keyboard())

        elif command in ["/islemler", "cmd_open"]:
            open_pos = self.paper_trader.state.get("open_positions", {})
            if not open_pos:
                text = (
                    f"🎯 <b>[AÇIK İŞLEM DURUMU]</b>\n\n"
                    f"🟢 <b>Şu an açıkta bekleyen pozisyon YOK.</b>\n\n"
                    f"⚙️ <b>Aktif Strateji Modu:</b> Dengeli & Aktif (%{MIN_CONFIDENCE_SCORE}+ Güven, RVOL ≥ {MIN_RVOL:.2f}x)\n"
                    f"🛡️ <b>Sermaye Güvenliği:</b> Sabit <b>${MAX_RISK_PER_TRADE_USD:.2f}</b> / işlem (%2 risk)\n"
                    f"🎯 <b>Kâr Stratejisi:</b> TP1 (+1.5R) hedefinde işlem <b>%100 kapatılır</b> ve kâr anında kasaya kilitlenir.\n\n"
                    f"📡 50+ parite saniyelik taranıyor; şartlar sağlandığında anında sanal işlem açılır ve telefonuna bildirim düşer."
                )
            else:
                lines = []
                for sym, pos in open_pos.items():
                    dir_icon = "🟢 LONG" if pos["direction"] == "LONG" else "🔴 SHORT"
                    rf_badge = " [🛡️ BREAK-EVEN ZIRHI AKTİF]" if pos.get("is_risk_free") else ""
                    lines.append(
                        f"• <b>#{sym}</b> ({dir_icon} {pos['leverage']}x){rf_badge}\n"
                        f"  Giriş: ${pos['entry_price']} | Stop: ${pos['stop_loss']}\n"
                        f"  Hedef 1: ${pos['tp1']} | Hedef 2: ${pos['tp2']}\n"
                        f"  Teminat: ${pos['margin_usd']:.2f}"
                    )
                text = f"🎯 <b>[AKTİF AÇIK İŞLEMLER ({len(open_pos)})]</b>\n\n" + "\n\n".join(lines)
            await self.send_response(session, chat_id, text, self.get_menu_keyboard())

        elif command in ["/radar", "cmd_radar"]:
            text = (
                f"⚡ <b>[CANLI RADAR MOTORU BİLGİSİ]</b> ⚡\n\n"
                f"🌐 <b>Sunucu:</b> Render 7/24 Kesintisiz\n"
                f"📡 <b>Hız:</b> ~1.2 saniye / 50 parite (Sub-second)\n"
                f"🛡️ <b>Keep-Awake Kalkanı:</b> Devrede (Uykuya asla dalmaz)\n\n"
                f"🔍 <b>Aktif Doğrulama Süzgeçleri (Dengeli Mod):</b>\n"
                f"  ✓ 15D Makro EMA Trend Uyumu\n"
                f"  ✓ 5D RVOL Hacim Teyidi (≥{MIN_RVOL:.2f}x)\n"
                f"  ✓ TradingView 26-İndikatör (ters yönde GÜÇLÜ sinyal varsa engeller)\n"
                f"  ✓ Elit Balina Pozisyon Teyidi (bonus puan)\n"
                f"  ✓ Flaş Haber & Kriz Kalkanı (Cointelegraph)\n"
                f"  ✓ Minimum Güven Skoru: %{MIN_CONFIDENCE_SCORE}\n\n"
                f"🎯 <i>Piyasa yapıcılar tahtayı kırdığı anda bildirim telefonuna düşecektir.</i>"
            )
            await self.send_response(session, chat_id, text, self.get_menu_keyboard())

        elif command in ["/perf", "/performans", "cmd_perf"]:
            text = (
                f"🏆 <b>[PERFORMANS VE BAŞARI İSTATİSTİĞİ]</b> 🏆\n\n"
                f"🎯 <b>Kazanma Oranı (Win Rate):</b> %{summary['win_rate']:.1f}\n"
                f"✅ <b>Kârlı Kapanan (Win):</b> {summary['win_trades']}\n"
                f"❌ <b>Stop Olan (Loss):</b> {summary['loss_trades']}\n"
                f"💵 <b>Kasa Büyümesi:</b> {'+' if summary['realized_pnl'] >= 0 else ''}${summary['realized_pnl']:.2f}\n\n"
                f"📌 <i>Not: TP1 vurulan tüm işlemlerde stop girişe çekildiğinden, kâr koruma zırhı devrededir.</i>"
            )
            await self.send_response(session, chat_id, text, self.get_menu_keyboard())

    async def poll_once(self, session: aiohttp.ClientSession):
        url = f"{self.base_url}/getUpdates"
        params = {"offset": self.last_update_id + 1, "timeout": 1}
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("result", []):
                        self.last_update_id = item["update_id"]
                        
                        if "callback_query" in item:
                            cb = item["callback_query"]
                            cb_id = cb.get("id", "")
                            cb_data = cb.get("data", "")
                            msg_obj = cb.get("message") or {}
                            chat_obj = msg_obj.get("chat") or {}
                            cid = str(chat_obj.get("id") or cb.get("from", {}).get("id") or self.chat_id)
                            
                            await self.answer_callback(session, cb_id)
                            if cb_data:
                                await self.handle_command(session, cid, cb_data)

                        elif "message" in item:
                            msg = item["message"]
                            cid = str(msg.get("chat", {}).get("id") or self.chat_id)
                            text = msg.get("text", "").strip()
                            if text:
                                await self.handle_command(session, cid, text)
        except Exception:
            pass
