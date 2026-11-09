"""Runtime settings. Risk limits live here and only here: no agent output can change them."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
PORT = _int("PORT", 8080)
EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Every symbol must exist on Binance, Bybit and Hyperliquid perps. The past-test evidence covers
# the first ten only. The rest were added for more trades during the paper trial.
SYMBOLS = tuple(os.getenv(
    "SYMBOLS",
    "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,SUIUSDT,"
    "ZECUSDT,HYPEUSDT,NEARUSDT,ENAUSDT,UNIUSDT,WLDUSDT,BCHUSDT,AAVEUSDT,ARBUSDT,LTCUSDT",
).split(","))

CEX_VENUE = os.getenv("CEX_VENUE", "auto")  # auto picks the first reachable of binance, bybit
DEX_VENUE = "hyperliquid"
SIGNAL_INTERVAL = "15m"
SIGNAL_INTERVAL_SECONDS = 900


@dataclass(frozen=True)
class RiskLimits:
    """Paper-trial defaults: size per trade is capped, but the number of trades is not.

    A value of 0 turns a limit off. Open-position, direction, daily-loss, drawdown and cooldown
    limits are off because the owner wants as much data as possible during the trial. They must
    be set before any real money. Running out of free margin still blocks a trade, as on a real
    exchange.
    """
    capital_usd: float = _float("CAPITAL_USD", 100.0)
    risk_per_trade_usd: float = _float("RISK_PER_TRADE_USD", 1.5)
    max_leverage: int = _int("MAX_LEVERAGE", 10)
    max_margin_per_trade_pct: float = _float("MAX_MARGIN_PER_TRADE_PCT", 10.0)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 0)
    max_same_direction: int = _int("MAX_SAME_DIRECTION", 0)
    daily_loss_stop_usd: float = _float("DAILY_LOSS_STOP_USD", 0.0)
    kill_drawdown_usd: float = _float("KILL_DRAWDOWN_USD", 0.0)
    # Only used by the 15m backtest; live strategies carry their own exits.
    reward_r: float = _float("REWARD_R", 2.0)
    min_stop_pct: float = _float("MIN_STOP_PCT", 0.5)
    max_stop_pct: float = _float("MAX_STOP_PCT", 15.0)
    max_hold_hours: float = _float("MAX_HOLD_HOURS", 24.0)
    symbol_cooldown_seconds: int = _int("SYMBOL_COOLDOWN_SECONDS", 0)
    # Extra adverse price applied to stop fills on top of the walked book.
    stop_slippage_bps: float = _float("STOP_SLIPPAGE_BPS", 3.0)


LIMITS = RiskLimits()
# The limits the published past-test results were measured under. Only research tools use them.
RESEARCH_LIMITS = RiskLimits(max_leverage=5, max_margin_per_trade_pct=25.0, max_open_positions=3,
                             max_same_direction=2, daily_loss_stop_usd=5.0,
                             kill_drawdown_usd=25.0, symbol_cooldown_seconds=1800)
TELEGRAM_DIGEST_HOURS = _float("TELEGRAM_DIGEST_HOURS", 6.0)

# Agent desk. The model id can be changed with DESK_MODEL; spend is capped per UTC day.
DESK_MODEL = os.getenv("DESK_MODEL", "claude-opus-5-5")
DESK_EFFORT = os.getenv("DESK_EFFORT", "low")
DESK_HEAD_EFFORT = os.getenv("DESK_HEAD_EFFORT", "medium")
DESK_DAILY_BUDGET_USD = _float("DESK_DAILY_BUDGET_USD", 1.0)
DESK_MAX_SESSIONS_PER_DAY = _int("DESK_MAX_SESSIONS_PER_DAY", 1000)
DESK_DEBATE_ROUNDS = _int("DESK_DEBATE_ROUNDS", 2)
DESK_MIN_CONVICTION = _int("DESK_MIN_CONVICTION", 60)
DESK_ENABLED = os.getenv("DESK_ENABLED", "true").lower() == "true"


