"""Settings of the Binance leverage bot. Every value can be overridden in .env."""

import os
from pathlib import Path


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
STATE_FILE = DATA_DIR / "levbot_state.json"

# "demo" trades on Binance's demo futures account (fake money). "live" needs LIVE_ARMED=YES as well.
MODE = os.getenv("TRADE_MODE", "demo").lower()
LIVE_ARMED = os.getenv("LIVE_ARMED", "") == "YES"
TRADE_HOST = "https://fapi.binance.com" if MODE == "live" else "https://demo-fapi.binance.com"
DATA_HOST = "https://fapi.binance.com"  # Signals always read the real market.
API_KEY = os.getenv("BINANCE_API_KEY" if MODE == "live" else "BINANCE_TESTNET_API_KEY", "")
KEY_FILE = os.getenv("BINANCE_KEY_FILE" if MODE == "live" else "BINANCE_TESTNET_KEY_FILE", "")

# Money rules: the whole bot has BUDGET_USD; one trade ties up at most MARGIN_PER_TRADE_USD and may lose
# at most RISK_PER_TRADE_USD if its stop is hit.
BUDGET_USD = _float("BUDGET_USD", 100.0)
MARGIN_PER_TRADE_USD = _float("MARGIN_PER_TRADE_USD", 10.0)
RISK_PER_TRADE_USD = _float("RISK_PER_TRADE_USD", 1.0)
MAX_LEVERAGE = int(_float("MAX_LEVERAGE", 5))
MAX_OPEN_POSITIONS = int(_float("MAX_OPEN_POSITIONS", 6))
MAX_SAME_DIRECTION = int(_float("MAX_SAME_DIRECTION", 4))
DAILY_LOSS_STOP_USD = _float("DAILY_LOSS_STOP_USD", 5.0)
DRAWDOWN_HALT_USD = _float("DRAWDOWN_HALT_USD", 20.0)

# Entry checks.
MAX_SPREAD_PCT = _float("MAX_SPREAD_PCT", 0.05)
MAX_RUNAWAY_ATR = _float("MAX_RUNAWAY_ATR", 0.5)        # Price may move this many ATRs past the signal close.
MAX_FUNDING_AGAINST_PCT = _float("MAX_FUNDING_AGAINST_PCT", 0.10)
MIN_LIQ_TO_STOP = _float("MIN_LIQ_TO_STOP", 2.0)         # Liquidation at least this many stop distances away.
MAX_COST_SHARE_OF_RISK = _float("MAX_COST_SHARE_OF_RISK", 0.15)
MIN_STOP_PCT = _float("MIN_STOP_PCT", 0.5)
MAX_STOP_PCT = _float("MAX_STOP_PCT", 15.0)
SCAN_WINDOW_SECONDS = _float("SCAN_WINDOW_SECONDS", 600.0)  # Signals older than this after the bar close are skipped.
TAKER_FEE_PCT = _float("TAKER_FEE_PCT", 0.05)
SLIPPAGE_PCT = _float("SLIPPAGE_PCT", 0.02)

SYMBOLS = tuple(s.strip() for s in os.getenv(
    "SYMBOLS",
    "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,SUIUSDT,"
    "ZECUSDT,HYPEUSDT,NEARUSDT,ENAUSDT,UNIUSDT,WLDUSDT,BCHUSDT,AAVEUSDT,ARBUSDT,LTCUSDT",
).split(",") if s.strip())

SYNC_SECONDS = _float("SYNC_SECONDS", 15.0)
PORT = int(_float("PORT", 8080))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
