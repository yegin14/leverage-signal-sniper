"""
LeverageSignalSniper - Configuration Module
Gelişmiş Çok Faktörlü Kurumsal Yapılandırma
"""

import os
from dotenv import load_dotenv

# .env dosyasını yükle
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# --- TELEGRAM AYARLARI ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9255550428:AAFUnIhge5_tWy0odDvtorm-xRWSWX_R2CI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- KASA VE RİSK YÖNETİMİ ($100 Bütçe) ---
ACCOUNT_SIZE_USD = float(os.getenv("ACCOUNT_SIZE_USD", "100.0"))
MAX_RISK_PER_TRADE_USD = float(os.getenv("MAX_RISK_PER_TRADE_USD", "2.0"))  # %2 Sabit Risk
MAX_ALLOWED_LEVERAGE = int(os.getenv("MAX_ALLOWED_LEVERAGE", "20"))
MIN_ALLOWED_LEVERAGE = int(os.getenv("MIN_ALLOWED_LEVERAGE", "3"))

# --- GELİŞMİŞ FİLTRE VE EŞİK DEĞERLERİ (15M KURUMSAL MOD) ---
MIN_RVOL = float(os.getenv("MIN_RVOL", "1.15"))  # 15D Hacim Eşiği (Ortalamanın %15 üzeri)
MIN_CONFIDENCE_SCORE = int(os.getenv("MIN_CONFIDENCE_SCORE", "65"))  # Yüksek Güven Eşiği (0-100)
SIGNAL_COOLDOWN_SECONDS = int(os.getenv("SIGNAL_COOLDOWN_SECONDS", "1800"))  # 30 dk cooldown
SCAN_INTERVAL_SECONDS = float(os.getenv("SCAN_INTERVAL_SECONDS", "3.0"))
TOP_SYMBOLS_COUNT = int(os.getenv("TOP_SYMBOLS_COUNT", "50"))  # İzlenen aktif parite sayısı (50-60 adet)

# --- DERİN ANALİZ MODÜLLERİ ---
ENABLE_TRADINGVIEW_FILTER = os.getenv("ENABLE_TRADINGVIEW_FILTER", "true").lower() == "true"
ENABLE_WHALE_SENTIMENT = os.getenv("ENABLE_WHALE_SENTIMENT", "true").lower() == "true"
ENABLE_NEWS_SHIELD = os.getenv("ENABLE_NEWS_SHIELD", "true").lower() == "true"

# Başlangıç geniş izleme listesi (Otomatik olarak en yüksek hacimlilerle güncellenir)
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
    "SUIUSDT", "PEPEUSDT", "NEARUSDT", "AVAXUSDT", "BNBUSDT",
    "APTUSDT", "LINKUSDT", "WIFUSDT", "FETUSDT", "RENDERUSDT",
    "SHIBUSDT", "ENAUSDT", "TAOUSDT", "SEIUSDT", "INJUSDT",
    "TIAUSDT", "ARBUSDT", "OPUSDT", "LDOUSDT", "FILUSDT",
    "KASUSDT", "STXUSDT", "AAVEUSDT", "MKRUSDT", "PENDLEUSDT",
    "RUNEUSDT", "FTMUSDT", "GALAUSDT", "SANDUSDT", "MANAUSDT",
    "AXSUSDT", "FLOKIUSDT", "BONKUSDT", "NOTUSDT", "TONUSDT",
    "WLDUSDT", "ORDIUSDT", "1000SATSUSDT", "DYDXUSDT", "CRVUSDT",
    "BLURUSDT", "JUPUSDT", "PYTHUSDT", "STRKUSDT", "ZROUSDT"
]
