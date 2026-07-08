# config.py — MYM Signal Alert System
# ---------------------------------------------------------------
# Secrets and environment-specific paths are loaded from the
# environment — NOT hard-coded here. Same file runs unchanged on
# your Mac (.env) and on the cloud host (Variables/Secrets UI).
# Never commit real secrets to this file.
# ---------------------------------------------------------------

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # cloud host sets env vars directly

def _require(name):
    """Return a required env var, or fail loudly with a clear message."""
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in your local .env or the cloud host's environment. "
            f"See .env.example for the full list."
        )
    return val


# ── PATHS (environment-overridable) ───────────────────────────
BASE      = Path(os.getenv("MYM_BASE", os.path.expanduser("~/prod/em_algo")))
DATA_DIR  = BASE / "data"
DATA_1M   = str(DATA_DIR / "MYM_1m_tv_bento.csv")   # backtest base bars
LOG_FILE  = str(BASE / "logs" / "signal_log.txt")

# ── STRATEGY PARAMETERS (v6 — must match strategy.py backtest) ─
TICKER            = "MYM"
ACCOUNT_SIZE      = 50000
RISK_PCT          = 0.005      # 0.5% per trade (v7 backtest value)
TICK_VALUE        = 0.50       # MYM = $0.50/point (CME-verified; YM is $5)
MIN_SCORE         = 7
T2_CAP            = 2.5          # R:R cap on T2
MAX_BODY_PCT      = 30         # base candle: body <= 40% of range
MIN_BODY_PCT      = 35         # leg candle:  body >= 50% of range
MIN_RANGE_RATIO   = 0.631      # leg range >= base_height * this
RANGE_MULTIPLIER  = 0.36       # base range <= rolling-median * this
MIN_ZONE_HEIGHT   = 30         # pts
MAX_BASE_CANDLES  = 6
STOP_BUFFER       = 8          # static fallback; live run computes dynamically
ENTRY_SIGMA       = 7    # proximity band = this many std devs of bar range
ENTRY_SIGMA_WINDOW= 14   # rolling window (bars) for the range std

# Lookbacks — note the candles, they differ:
ZONE_LOOKBACK_DAYS  = 60       # set_curve: last 60 x 15m bars
TREND_WINDOW        = 120       # classify_trend: rolling window of (last 10hrs) 5m bars
ZONE_WINDOW_DAYS    = 120       # detect_zones: last 120 candles (last 10hrs) of 5m

# ── SCHEDULER ─────────────────────────────────────────────────
RUN_HOURS = [8, 9, 10, 11, 12]
TIMEZONE  = "US/Eastern"

# ── SECRETS (loaded from environment — never hard-coded) ──────
EMAIL_SENDER   = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")   # Gmail APP password
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER", EMAIL_SENDER)

# Discord: channel settings -> Integrations -> Webhooks -> copy URL
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# TradersPost: strategy webhook URL (used by alerts.traderspost_alert)
TRADERSPOST_WEBHOOK_URL = os.getenv("TRADERSPOST_WEBHOOK_URL", "")

# ── DATABENTI API CREDS ──────
DATABENTO_API_KEY = os.getenv("DATABENTO_API_KEY", "")

# ── CHARLES SCHWAB TRADER API CREDS ──────
SCHWAB_API_KEY          = "gAvjFUyE1AiLJIccGQ0DrsoYwfqOIXfidEJ3moGUKSHwPHxb" 
SCHWAB_APP_SECRET       = "AgmDCFGiwoaSjs0IZfEuAhdnF6H0Ad9qJn3sLAtsXQKzL02Xy9PGii4D9jgrsTAm" 
SCHWAB_CALLBACK_URL     = "https://127.0.0.1:8182" 
SCHWAB_TOKEN_PATH       = "./schwab_token.json"
SCHWAB_HEADLESS= ""

# Live data providers (optional until activated)
#ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
#ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")

# Secrets required ONLY to send alerts. Enforced at send time, not import.
ALERT_REQUIRED = ["EMAIL_SENDER", "EMAIL_PASSWORD", "DISCORD_WEBHOOK_URL",
                  "TRADERSPOST_WEBHOOK_URL"]

def require_alert_secrets():
    """Raise if any alert secret is missing. Call before sending alerts."""
    missing = [k for k in ALERT_REQUIRED if not globals().get(k)]
    if missing:
        raise RuntimeError(
            f"Cannot send alert — missing secrets: {', '.join(missing)}. "
            f"Set them in your local .env or the cloud host's environment. "
            f"(Backtests/notebooks do not need these.)"
        )
