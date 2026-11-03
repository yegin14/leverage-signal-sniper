"""
LeverageSignalSniper - Production Daemon (Sanal İşlem ve Bulut Sunucu Uyumlu)
50+ Pariteyi Paralel Tarayan, Sanal Kasa Yöneten ve 7/24 Kesintisiz Çalışan Ana Motor.
"""

import asyncio
import time
import sys
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import aiohttp

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from config import (
    DEFAULT_SYMBOLS, SCAN_INTERVAL_SECONDS,
    SIGNAL_COOLDOWN_SECONDS, TOP_SYMBOLS_COUNT, TELEGRAM_CHAT_ID
)
from market_data import MarketDataEngine
from strategy_engine import StrategyEngine
from tradingview_client import TradingViewClient
from news_scanner import NewsScanner
from paper_trader import PaperTrader
from telegram_notifier import TelegramNotifier

# --- BULUT SUNUCULAR İÇİN HEALTH-CHECK SUNUCUSU ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"LEVERAGE_SNIPER_OK")

    def log_message(self, format, *args):
        pass  # Konsol kirliliğini engelle

def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[✓] Bulut Sağlık Sunucusu (Health-Check) Port {port} üzerinde aktif.")

async def run_single_scan(
    session: aiohttp.ClientSession,
    market: MarketDataEngine,
    strategy: StrategyEngine,
    tv_client: TradingViewClient,
    news_scanner: NewsScanner,
    paper_trader: PaperTrader,
    notifier: TelegramNotifier,
    symbols: list[str]
):
    cycle_start = time.time()
    
    # 1. TradingView ve Haberleri Paralel Güncelle
    tv_task = tv_client.fetch_batch_ratings(session, symbols)
    news_task = news_scanner.update_news_feed(session)
    await asyncio.gather(tv_task, news_task)

    # 2. Tüm pariteleri (50+) paralel çek
    chunk_size = 15
    bundles = []
    t_fetch_start = time.time()
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        tasks = [market.fetch_full_symbol_bundle(session, sym) for sym in chunk]
        results = await asyncio.gather(*tasks)
        bundles.extend(results)
        
    fetch_time_ms = (time.time() - t_fetch_start) * 1000.0

    # 3. Canlı fiyatları topla ve Sanal İşlemleri Güncelle
    current_prices = {}
    for b in bundles:
        if b and "k5m" in b:
            current_prices[b["symbol"]] = b["k5m"]["last_price"]

    # Sanal işlemlerin TP/SL durumunu kontrol et
    trade_events = paper_trader.update_positions(current_prices)
    for event in trade_events:
        summary = paper_trader.get_summary()
        msg = notifier.build_trade_event_message(event, summary)
        print("\n" + "="*60)
        print(f"🎯 SANAL İŞLEM ETKİNLİĞİ: {event['event']} ({event['position']['symbol']}) PnL: ${event.get('pnl', 0):.2f}")
        print("="*60)
        await notifier.send_message(session, msg)

    # 4. Mikrosaniye Seviyesinde Derin Çok Faktörlü Değerlendirme
    signals_found = 0
    t_eval = time.time()
    
    for bundle in bundles:
        if not bundle:
            continue
            
        sym = bundle["symbol"]
        if strategy.is_in_cooldown(sym, SIGNAL_COOLDOWN_SECONDS):
            continue

        tv_data = tv_client.get_symbol_rating(sym)
        news_info = news_scanner.check_coin_news(sym)

        sig = strategy.evaluate_bundle(bundle, tv_data=tv_data, news_info=news_info)
        if sig:
            signals_found += 1
            strategy.register_signal(sym)
            
            # 1. Sinyali Telegram'a İlet
            sig_msg = notifier.build_signal_message(sig)
            print("\n" + "="*60)
            print(f"🔥 SİNYAL ONAYLANDI: {sig['symbol']} {sig['direction']} (Skor: %{sig['confidence']})")
            print("="*60)
            await notifier.send_message(session, sig_msg)

            # 2. Sanal Kasada Otomatik İşleme Gir
            opened = paper_trader.open_virtual_trade(sig)
            if opened:
                summary = paper_trader.get_summary()
                open_msg = notifier.build_trade_open_message(opened, summary)
                print(f"[🎮 Sanal İşlem Başlatıldı]: {opened['symbol']} {opened['direction']} ({opened['leverage']}x)")
                await notifier.send_message(session, open_msg)

    eval_time_ms = (time.time() - t_eval) * 1000.0
    total_cycle_ms = (time.time() - cycle_start) * 1000.0
    
    summary = paper_trader.get_summary()
    sys.stdout.write(
        f"\r⚡ [RADAR] {len(symbols)} Parite | "
        f"Döngü: {total_cycle_ms:.0f}ms | Açık İşlem: {summary['open_count']} | "
        f"Kasa: ${summary['balance']:.2f} (PnL: {'+' if summary['realized_pnl']>=0 else ''}${summary['realized_pnl']:.2f})"
    )
    sys.stdout.flush()

async def main():
    print("""
╔═══════════════════════════════════════════════════════════════╗
║     ⚡ LEVERAGE SIGNAL SNIPER - CLOUD & PAPER ENGINE ⚡       ║
║   50+ Parite | TradingView 26-İndikatör | Sanal Kasa Takibi   ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    # Bulut sunucusu (Render/Railway) için health-check başlat
    start_health_server()

    print("[*] Kurumsal modüller ayağa kaldırılıyor...")
    market = MarketDataEngine()
    strategy = StrategyEngine()
    tv_client = TradingViewClient()
    news_scanner = NewsScanner()
    paper_trader = PaperTrader()
    notifier = TelegramNotifier()
    
    summary = paper_trader.get_summary()
    print(f"[✓] Sanal Kasa Aktif: Başlangıç ${summary['initial_balance']:.2f} | Güncel Bakiye: ${summary['balance']:.2f}")

    async with aiohttp.ClientSession() as session:
        print("[*] En yüksek hacimli vadeli pariteler taranıyor...")
        top_symbols = await market.get_top_volume_symbols(session, limit=TOP_SYMBOLS_COUNT)
        if not top_symbols or len(top_symbols) < 10:
            top_symbols = DEFAULT_SYMBOLS[:TOP_SYMBOLS_COUNT]
        print(f"[✓] {len(top_symbols)} Parite Havuzu Aktifleştirildi!")

        if not TELEGRAM_CHAT_ID:
            print("\n[!] UYARI: .env dosyasında TELEGRAM_CHAT_ID henüz tanımlanmamış.")
            print("    Sinyaller ve sanal işlemler konsola basılacak; Chat ID girildiğinde Telegram'a gidecektir.\n")
        else:
            print(f"[✓] Telegram Bildirim Kanalı Aktif ({TELEGRAM_CHAT_ID})\n")

        print("[*] 7/24 Kesintisiz Tarama ve Sanal Kasa İşletimi Başladı.\n")
        
        while True:
            try:
                await run_single_scan(session, market, strategy, tv_client, news_scanner, paper_trader, notifier, top_symbols)
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"\n[Döngü Uyarısı]: {e}")
                await asyncio.sleep(2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Program kullanıcı tarafından kapatıldı.")
