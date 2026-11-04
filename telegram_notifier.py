"""
LeverageSignalSniper - Telegram Notifier Module
Sinyalleri, sanal işlem açılışlarını, TP/SL vuruşlarını ve kasa durumunu iletir.
"""

import aiohttp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ACCOUNT_SIZE_USD

class TelegramNotifier:
    def __init__(self, bot_token: str = TELEGRAM_BOT_TOKEN, chat_id: str = TELEGRAM_CHAT_ID):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def format_price(self, price: float) -> str:
        if price >= 1000:
            return f"${price:,.2f}"
        elif price >= 1:
            return f"${price:.4f}"
        elif price >= 0.01:
            return f"${price:.6f}"
        else:
            return f"${price:.8f}"

    def build_signal_message(self, sig: dict) -> str:
        sym = sig["symbol"]
        clean_sym = sym.replace("USDT", "")
        direction = sig["direction"]
        dir_icon = "🟢 LONG / ALIM" if direction == "LONG" else "🔴 SHORT / SATIŞ"
        conf = sig["confidence"]
        
        entry_str = self.format_price(sig["entry_price"])
        sl_str = self.format_price(sig["stop_loss"])
        tp1_str = self.format_price(sig["tp1"])
        tp2_str = self.format_price(sig["tp2"])
        tp3_str = self.format_price(sig["tp3"])

        reasons_formatted = "\n".join([f"  • {r}" for r in sig["reasons"]])
        tv_badge = sig.get("tv_badge", "⚪ NÖTR")
        whale = sig.get("whale_info", {})
        whale_str = f"%{whale.get('long_pct', 50):.1f} Long vs %{whale.get('short_pct', 50):.1f} Short" if whale else "Dengeli"
        
        news_line = ""
        if sig.get("news_info"):
            n = sig["news_info"]
            news_line = f"\n📰 <b>Flaş Haber:</b> <i>{n['headline'][:55]}...</i> ({n['tag']})\n"

        tv_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{sym}.P"
        binance_link = f"https://www.binance.com/en/futures/{sym}"

        msg = (
            f"⚡ <b>[LEVERAGE SNIPER SİNYALİ - KURUMSAL]</b> ⚡\n\n"
            f"🎯 <b>Parite:</b> #{clean_sym} (Binance Vadeli)\n"
            f"🧭 <b>Yön:</b> <b>{dir_icon}</b>\n"
            f"🌟 <b>Güven Skoru:</b> %{conf} (Yüksek Olasılık)\n\n"
            f"📍 <b>Giriş Fiyatı:</b> <code>{entry_str}</code>\n"
            f"🛑 <b>Stop-Loss:</b> <code>{sl_str}</code> (-%{sig['sl_distance_pct']:.2f})\n"
            f"🎯 <b>Kâr Hedefi (TP1 - %100 Kapanış):</b> <code>{tp1_str}</code> [1.5R]\n"
            f"🎯 <b>İkincil Hedef (TP2):</b> <code>{tp2_str}</code> [2.5R]\n\n"
            f"💰 <b>Kasa Yönetimi (${ACCOUNT_SIZE_USD:.0f} Bütçe İçin):</b>\n"
            f"  • <b>Tavsiye Kaldıraç:</b> {sig['recommended_leverage']}x (Yapısal Koruma)\n"
            f"  • <b>Gereken Teminat (Margin):</b> ${sig['margin_needed_usd']:.2f}\n"
            f"  • <b>Toplam Pozisyon:</b> ${sig['target_position_usd']:.2f}\n"
            f"  • <b>Stop Olursa Maksimum Zarar:</b> -${sig['max_risk_usd']:.2f} (Sabit %2.0 Sınırı)\n"
            f"  • <b>TP1 Net Kâr (Kasaya Kilitlenir):</b> +${sig['tp1_profit_usd']:.2f}\n\n"
            f"🔍 <b>Derin Piyasa Mikro Göstergeleri:</b>\n"
            f"  • <b>TradingView 26-İndikatör:</b> {tv_badge}\n"
            f"  • <b>Elit Balina Pozisyonu:</b> {whale_str}\n"
            f"  • <b>Hacim İvmesi (RVOL):</b> {sig['rvol']:.1f}x\n"
            f"{news_line}\n"
            f"📊 <b>Teyit Gerekçeleri:</b>\n"
            f"{reasons_formatted}\n\n"
            f"🔗 <a href='{tv_link}'>📈 TradingView'da İncele</a> | <a href='{binance_link}'>⚡ Binance'te Aç</a>\n"
            f"⏱️ <i>Hesaplama Hızı: {sig['calc_latency_ms']:.1f} ms</i>"
        )
        return msg

    def build_trade_open_message(self, pos: dict, summary: dict) -> str:
        sym = pos["symbol"].replace("USDT", "")
        dir_icon = "🟢 LONG" if pos["direction"] == "LONG" else "🔴 SHORT"
        return (
            f"🎮 <b>[SANAL İŞLEM AÇILDI - 15D KURUMSAL]</b> 🎮\n\n"
            f"🎯 <b>Parite:</b> #{sym} ({dir_icon} {pos['leverage']}x)\n"
            f"📍 <b>Giriş:</b> {self.format_price(pos['entry_price'])}\n"
            f"🛑 <b>Yapısal Stop-Loss:</b> {self.format_price(pos['stop_loss'])}\n"
            f"🎯 <b>Kâr Hedefi (TP1):</b> {self.format_price(pos['tp1'])}\n"
            f"💵 <b>Bağlanan Teminat:</b> ${pos['margin_usd']:.2f}\n"
            f"🛡️ <b>Maksimum Risk:</b> -${pos['max_risk_usd']:.2f} (Sabit %2.0)\n\n"
            f"💼 <b>Sanal Kasa Bakiyesi:</b> <code>${summary['balance']:.2f}</code> (Açık: {summary['open_count']})"
        )

    def build_trade_event_message(self, event: dict, summary: dict) -> str:
        pos = event["position"]
        sym = pos["symbol"].replace("USDT", "")
        ev_type = event["event"]
        pnl = event.get("pnl", 0.0)
        pnl_sign = "+" if pnl > 0 else ""
        
        if ev_type == "TP1_HIT":
            header = "🎯 <b>[HEDEF 1 VURULDU - İŞLEM KAPATILDI]</b>"
            note = "✅ <b>KÂR KASAYA KİLİTLENDİ:</b> TP1 (+1.5R) hedefi vuruldu ve işlem <b>%100 kapatıldı</b>. Anapara teminatı ve net kâr kasaya eklendi!"
        elif ev_type == "TP2_HIT":
            header = "🎯🎯 <b>[HEDEF 2 VURULDU - TP2]</b>"
            note = "🚀 Kalan pozisyonun %30'u daha kârla kapatıldı!"
        elif ev_type == "TP3_HIT":
            header = "🏆 <b>[TÜM HEDEFLER TAMAMLANDI - TP3]</b>"
            note = "🔥 Muazzam başarı! Pozisyon tamamen kârla kapatıldı."
        else:  # SL_HIT
            if pos.get("is_risk_free"):
                header = "🛑 <b>[BREAK-EVEN ÇIKIŞ]</b>"
                note = "Giriş seviyesine çekilen stop çalıştı. Sıfır zarar ile işlem güvenle kapatıldı."
            else:
                header = "🛑 <b>[STOP-LOSS TETİKLENDİ]</b>"
                note = f"Kasa koruma kalkanı çalıştı. Sabit risk sınırı uygulandı: -${pos['max_risk_usd']:.2f}"

        return (
            f"{header}\n\n"
            f"🎯 <b>Parite:</b> #{sym}\n"
            f"💰 <b>Gerçekleşen Kâr/Zarar:</b> <code>{pnl_sign}${pnl:.2f}</code>\n"
            f"📌 {note}\n\n"
            f"📊 <b>Güncel Sanal Kasa:</b>\n"
            f"  • Bakiye: <b>${summary['balance']:.2f}</b>\n"
            f"  • Toplam Net PnL: <b>{'+' if summary['realized_pnl'] >= 0 else ''}${summary['realized_pnl']:.2f}</b> (%{summary['return_pct']:.1f})\n"
            f"  • Başarı Oranı: <b>%{summary['win_rate']:.1f}</b> ({summary['win_trades']}W / {summary['loss_trades']}L)"
        )

    def build_account_summary_message(self, summary: dict) -> str:
        return (
            f"📊 <b>[SANAL KASA PERFORMANS RAPORU]</b> 📊\n\n"
            f"💰 <b>Başlangıç Kasası:</b> ${summary['initial_balance']:.2f}\n"
            f"💵 <b>Güncel Bakiye:</b> <code>${summary['balance']:.2f}</code>\n"
            f"📈 <b>Toplam Net Kâr:</b> {'+' if summary['realized_pnl'] >= 0 else ''}${summary['realized_pnl']:.2f} (%{summary['return_pct']:.2f})\n"
            f"🎯 <b>Kazanma Oranı (Win Rate):</b> %{summary['win_rate']:.1f}\n"
            f"🔢 <b>Toplam İşlem Sayısı:</b> {summary['total_trades']} ({summary['win_trades']} Başarılı / {summary['loss_trades']} Stop)\n"
            f"⏳ <b>Şu An Açık Pozisyon:</b> {summary['open_count']}"
        )

    async def send_message(self, session: aiohttp.ClientSession, text: str, chat_id: str = None) -> bool:
        target_chat = chat_id or self.chat_id
        if not target_chat or not self.bot_token:
            print("\n[TELEGRAM ÇIKTISI]\n" + text)
            return False

        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    return True
                else:
                    err = await resp.text()
                    print(f"[Telegram Hatası] HTTP {resp.status}: {err}")
        except Exception as e:
            print(f"[Telegram Gönderim Hatası]: {e}")
        return False
