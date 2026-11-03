"""
LeverageSignalSniper - Interactive Telegram Command & Menu Listener
Kullanıcı /start yazdığında veya butonlara bastığında anında yanıt veren etkileşimli modül.
"""

import asyncio
import aiohttp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
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

    async def send_response(self, session: aiohttp.ClientSession, chat_id: str, text: str, reply_markup: dict = None):
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                pass
        except Exception:
            pass

    async def answer_callback(self, session: aiohttp.ClientSession, callback_id: str):
        url = f"{self.base_url}/answerCallbackQuery"
        try:
            async with session.post(url, json={"callback_query_id": callback_id}, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                pass
        except Exception:
            pass

    async def handle_command(self, session: aiohttp.ClientSession, chat_id: str, command: str):
        summary = self.paper_trader.get_summary()

        if command in ["/start", "/menu", "cmd_menu"]:
            text = (
                f"⚡ <b>[LEVERAGE SIGNAL SNIPER KONTROL PANELİ]</b> ⚡\n\n"
                f"👋 Merhaba Halil! Bu bot <b>50+ vadeli pariteyi</b> 7/24 tarayan ve "
                f"<b>$100.00</b> bütçeli sanal kasayı canlı canlı işleten özel sistemindir.\n\n"
                f"💰 <b>Güncel Kasa:</b> <code>${summary['balance']:.2f}</code>\n"
                f"⏳ <b>Açık İşlem:</b> {summary['open_count']} adet\n"
                f"📈 <b>Net Kâr:</b> {'+' if summary['realized_pnl'] >= 0 else ''}${summary['realized_pnl']:.2f} (%{summary['return_pct']:.1f})\n\n"
                f"Aşağıdaki menü butonlarını kullanarak sistemi anlık sorgulayabilirsin:"
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
                    f"Şu anda açıkta bekleyen pozisyon yok.\n"
                    f"Radar 50+ pariteyi tarıyor; %80+ güvenli sinyal geldiğinde otomatik açılacaktır."
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
                f"🌐 <b>Sunucu:</b> Render Virginia (US-East) 7/24 Aktif\n"
                f"📡 <b>Hız:</b> ~1.2 saniye / 50 parite (Sub-second)\n"
                f"🔍 <b>Filtreler:</b>\n"
                f"  ✓ 15D Makro EMA Trend Uyumu\n"
                f"  ✓ 5D RVOL Hacim Patlaması (>1.7x)\n"
                f"  ✓ TradingView 26-İndikatör Konsensüsü (GÜÇLÜ AL/SAT)\n"
                f"  ✓ Elit Balina Pozisyon Teyidi (>%58)\n"
                f"  ✓ Flaş Haber & Kriz Kalkanı (Cointelegraph)\n\n"
                f"🛡️ <i>Sistem her saniye piyasayı analiz etmeye devam ediyor.</i>"
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

    async def poll_updates(self, session: aiohttp.ClientSession):
        """Telegram getUpdates arka plan döngüsü"""
        url = f"{self.base_url}/getUpdates"
        params = {"offset": self.last_update_id + 1, "timeout": 2}
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("result", []):
                        self.last_update_id = item["update_id"]
                        
                        # 1. Normal Mesaj
                        if "message" in item:
                            msg = item["message"]
                            cid = str(msg.get("chat", {}).get("id", ""))
                            text = msg.get("text", "").strip()
                            if text:
                                await self.handle_command(session, cid, text)

                        # 2. Butona Tıklama (Callback Query)
                        elif "callback_query" in item:
                            cb = item["callback_query"]
                            cid = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                            data = cb.get("data", "")
                            cb_id = cb.get("id", "")
                            await self.answer_callback(session, cb_id)
                            if data:
                                await self.handle_command(session, cid, data)
        except Exception:
            pass
