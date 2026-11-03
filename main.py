"""
LeverageSignalSniper - Production Daemon (Tamamen Ayrık Asenkron Mimari)
Telegram Dinleyicisi ve Piyasa Tarayıcısı iki bağımsız iş parçacığı olarak çalışır.
Butonlara tıklandığında piyasa taraması beklenmez; <100ms içinde anında yanıt verilir.
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
from telegram_listener import TelegramCommandListener

# --- BULUT SUNUCULAR İÇİN HEALTH-CHECK SUNUCUSU ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"LEVERAGE_SNIPER_OK")

    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[✓] Bulut Sağlık Sunucusu Port {port} üzerinde aktif.")

# --- 1. BAĞIMSIZ TELEGRAM İŞ PARÇACIĞI (KESİNTİSİZ <100MS YANIT) ---
async def telegram_worker(cmd_listener: TelegramCommandListener):
    print("[*] Bağımsız Telegram Komut Dinleyicisi Devrede.")
    async with aiohttp.ClientSession() as tg_session:
        while True:
            try:
                await cmd_listener.poll_once(tg_session)
            except Exception:
                await asyncio.sleep(1)
            await asyncio.sleep(0.3)

# --- 2. PİYASA TARAYICI VE SANAL KASA İŞ PARÇACIĞI ---
async def market_scanner_worker(
    market: MarketDataEngine,
    strategy: StrategyEngine,
    tv_client: TradingViewClient,
    news_scanner: NewsScanner,
    paper_trader: PaperTrader,
    notifier: TelegramNotifier,
    symbols: list[str]
):
    print(f"[*] 50+ Parite Tarayıcı Motoru Başlatıldı ({len(symbols)} Parite).")
    async with aiohttp.ClientSession() as session:
        while True:
            cycle_start = time.time()
            try:
                # 1. TradingView ve Haberleri Paralel Güncelle
                tv_task = tv_client.fetch_batch_ratings(session, symbols)
                news_task = news_scanner.update_news_feed(session)
                await asyncio.gather(tv_task, news_task)

                # 2. Tüm pariteleri çek (15'erli gruplar)
                chunk_size = 15
                bundles = []
                t_fetch_start = time.time()
                for i in range(0, len(symbols), chunk_size):
                    chunk = symbols[i:i + chunk_size]
                    tasks = [market.fetch_full_symbol_bundle(session, sym) for sym in chunk]
                    results = await asyncio.gather(*tasks)
                    bundles.extend(results)
                    
                fetch_time_ms = (time.time() - t_fetch_start) * 1000.0

                # 3. Canlı fiyatlarla Sanal İşlemleri Güncelle
                current_prices = {}
                for b in bundles:
                    if b and "k5m" in b:
                        current_prices[b["symbol"]] = b["k5m"]["last_price"]

                # TP / SL Kontrolleri
                trade_events = paper_trader.update_positions(current_prices)
                for event in trade_events:
                    summary = paper_trader.get_summary()
                    msg = notifier.build_trade_event_message(event, summary)
                    print("\n" + "="*60)
                    print(f"🎯 SANAL İŞLEM ETKİNLİĞİ: {event['event']} ({event['position']['symbol']}) PnL: ${event.get('pnl', 0):.2f}")
                    print("="*60)
                    await notifier.send_message(session, msg)

                # 4. Sinyal Arama
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
                        
                        # Sinyali İlet
                        sig_msg = notifier.build_signal_message(sig)
                        await notifier.send_message(session, sig_msg)

                        # Sanal İşlem Aç
                        opened = paper_trader.open_virtual_trade(sig)
                        if opened:
                            summary = paper_trader.get_summary()
                            open_msg = notifier.build_trade_open_message(opened, summary)
                            print(f"[🎮 Sanal İşlem]: {opened['symbol']} {opened['direction']} ({opened['leverage']}x)")
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

            except Exception as e:
                print(f"\n[Tarayıcı Döngü Uyarısı]: {e}")

            # Tarama aralığı (5 saniye)
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

async def main():
    print("""
╔═══════════════════════════════════════════════════════════════╗
║     ⚡ LEVERAGE SIGNAL SNIPER - CLOUD & PAPER ENGINE ⚡       ║
║   Bağımsız Telegram İş Parçacığı | 50+ Parite Tarayıcı        ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    start_health_server()

    market = MarketDataEngine()
    strategy = StrategyEngine()
    tv_client = TradingViewClient()
    news_scanner = NewsScanner()
    paper_trader = PaperTrader()
    notifier = TelegramNotifier()
    cmd_listener = TelegramCommandListener(paper_trader)

    summary = paper_trader.get_summary()
    print(f"[✓] Sanal Kasa: Başlangıç ${summary['initial_balance']:.2f} | Bakiye: ${summary['balance']:.2f}")

    async with aiohttp.ClientSession() as session:
        print("[*] Parite havuzu keşfediliyor...")
        top_symbols = await market.get_top_volume_symbols(session, limit=TOP_SYMBOLS_COUNT)
        if not top_symbols or len(top_symbols) < 10:
            top_symbols = DEFAULT_SYMBOLS[:TOP_SYMBOLS_COUNT]
        print(f"[✓] {len(top_symbols)} Parite Aktif.")

    # İki görevi tamamen bağımsız ve paralel çalıştır:
    # 1. Telegram Buton Yanıtlayıcı (Hiç beklemez, 0.3s döngü)
    # 2. Piyasa Tarayıcı
    await asyncio.gather(
        telegram_worker(cmd_listener),
        market_scanner_worker(
            market, strategy, tv_client, news_scanner,
            paper_trader, notifier, top_symbols
        )
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Program kapatıldı.")
