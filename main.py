"""
LeverageSignalSniper - Production Daemon (7/24 Kesintisiz & Uyanık Kalkanı)
Render ücretsiz planının uykuya dalmasını (idle shutdown) engelleyen self-pinger,
bağımsız Telegram iş parçacığı ve 50 parite tarayıcısı içerir.
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
    SIGNAL_COOLDOWN_SECONDS, TOP_SYMBOLS_COUNT, TELEGRAM_CHAT_ID,
    MIN_CONFIDENCE_SCORE, MIN_RVOL, MAX_RISK_PER_TRADE_USD
)
from market_data import MarketDataEngine
from strategy_engine import StrategyEngine
from tradingview_client import TradingViewClient
from news_scanner import NewsScanner
from paper_trader import PaperTrader
from telegram_notifier import TelegramNotifier
from telegram_listener import TelegramCommandListener

GLOBAL_PAPER_TRADER = None
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://leverage-signal-sniper.onrender.com").strip().rstrip("/")

# --- CANLI TEŞHİS DURUMU (/status sayfasında gösterilir) ---
import json as _json
DIAG = {
    "started_at": time.time(),
    "cycles": 0,
    "last_cycle_at": 0,
    "symbols": 0,
    "bundles_ok": 0,
    "binance_http": 0,
    "signals_total": 0,
    "trades_opened": 0,
    "data_source": "binance",
    "last_error": "",
    "top_scores": [],
}

# --- BULUT SUNUCULAR İÇİN HEALTH-CHECK SUNUCUSU ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/status"):
            resp_data = dict(DIAG)
            if GLOBAL_PAPER_TRADER:
                resp_data["open_positions"] = GLOBAL_PAPER_TRADER.state.get("open_positions", {})
                resp_data["summary"] = GLOBAL_PAPER_TRADER.get_summary()
            body = _json.dumps(resp_data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
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

# --- 1. 7/24 UYANIK TUTMA MOTORU (RENDER UYKU ENGELLEYİCİ) ---
async def keep_awake_worker():
    """Render'ın 15 dakikalık boşta kalma uykusunu her 4 dakikada bir kendini pingleyerek engeller"""
    print(f"[*] 24/7 Keep-Awake Kalkanı Aktif: {RENDER_EXTERNAL_URL}")
    await asyncio.sleep(60)  # İlk başlatmadan 1 dakika sonra başla
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                async with s.get(f"{RENDER_EXTERNAL_URL}/", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    pass
            except Exception:
                pass
            await asyncio.sleep(240)  # Her 4 dakikada bir ping (<15 dk sınır)

# --- 2. PERİYODİK NABIZ / KALBİN ÇARPIŞI RAPORU ---
async def heartbeat_worker(notifier: TelegramNotifier, paper_trader: PaperTrader):
    """Kullanıcının aklında soru işareti kalmaması için 4 saatte bir Telegram'a durum raporu atar"""
    while True:
        await asyncio.sleep(14400)  # 4 Saatte bir
        try:
            summary = paper_trader.get_summary()
            text = (
                f"💓 <b>[7/24 RADAR DURUM RAPORU]</b> 💓\n\n"
                f"🤖 Bot kesintisiz olarak 50+ vadeli pariteyi taramaya devam ediyor.\n\n"
                f"💼 <b>Sanal Kasa:</b> <code>${summary['balance']:.2f}</code>\n"
                f"🎯 <b>Açık İşlem:</b> {summary['open_count']} adet\n"
                f"📈 <b>Toplam PnL:</b> {'+' if summary['realized_pnl']>=0 else ''}${summary['realized_pnl']:.2f}\n"
                f"🌐 <b>Sunucu Durumu:</b> 🟢 Render Frankfurt 7/24 (Uyanık & Aktif)\n\n"
                f"<i>Dengeli Mod: %{MIN_CONFIDENCE_SCORE}+ güven ve RVOL ≥ {MIN_RVOL:.2f}x şartı sağlandığında anında açılır.</i>"
            )
            async with aiohttp.ClientSession() as s:
                await notifier.send_message(s, text)
        except Exception:
            pass

# --- 3. BAĞIMSIZ TELEGRAM İŞ PARÇACIĞI (KESİNTİSİZ <100MS YANIT) ---
async def telegram_worker(cmd_listener: TelegramCommandListener):
    print("[*] Bağımsız Telegram Komut Dinleyicisi Devrede.")
    async with aiohttp.ClientSession() as tg_session:
        while True:
            try:
                await cmd_listener.poll_once(tg_session)
            except Exception:
                await asyncio.sleep(1)
            await asyncio.sleep(0.3)

# --- 4. PİYASA TARAYICI VE SANAL KASA İŞ PARÇACIĞI ---
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
                DIAG["cycles"] += 1
                DIAG["last_cycle_at"] = time.time()
                DIAG["symbols"] = len(symbols)
                DIAG["bundles_ok"] = sum(1 for b in bundles if b)
                DIAG["binance_http"] = market.last_status
                DIAG["data_source"] = "bybit" if market.use_bybit else "binance" 

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
                        DIAG["signals_total"] += 1
                        strategy.register_signal(sym)
                        
                        # Sinyali İlet
                        sig_msg = notifier.build_signal_message(sig)
                        await notifier.send_message(session, sig_msg)

                        # Sanal İşlem Aç
                        opened = paper_trader.open_virtual_trade(sig)
                        if opened:
                            DIAG["trades_opened"] += 1
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
                DIAG["last_error"] = str(e)[:200]
                print(f"\n[Tarayıcı Döngü Uyarısı]: {e}")

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

async def main():
    print("""
╔═══════════════════════════════════════════════════════════════╗
║     ⚡ LEVERAGE SIGNAL SNIPER - CLOUD & PAPER ENGINE ⚡       ║
║   24/7 Keep-Awake | Bağımsız Telegram | 50+ Parite Tarayıcı  ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    start_health_server()

    market = MarketDataEngine()
    strategy = StrategyEngine()
    tv_client = TradingViewClient()
    news_scanner = NewsScanner()
    global GLOBAL_PAPER_TRADER
    paper_trader = PaperTrader()
    GLOBAL_PAPER_TRADER = paper_trader
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

    # 4 Bağımsız Görevi Paralel Çalıştır:
    # 1. Telegram Buton Yanıtlayıcı
    # 2. Piyasa Tarayıcısı
    # 3. 24/7 Uyanık Tutma (Keep-Awake Pinger)
    # 4. Periyodik Nabız / Rapor Gönderici
    await asyncio.gather(
        telegram_worker(cmd_listener),
        market_scanner_worker(
            market, strategy, tv_client, news_scanner,
            paper_trader, notifier, top_symbols
        ),
        keep_awake_worker(),
        heartbeat_worker(notifier, paper_trader)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Program kapatıldı.")
