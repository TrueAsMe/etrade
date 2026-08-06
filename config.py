# """
# Project Flow

# run etrade
#     ||
#     --
#     --
#     verify_env.py ---> signals.py ----> alerts.py ----> TradersPost ----> Tradovate ----> Lucid
#                                 V  ^^
#     config.py & .env check --> data_feed.py
# """

# config.py — MYM Signal Alert System
# ---------------------------------------------------------------
# Secrets and environment-specific paths are loaded from the
# environment — NOT hard-coded here. Same file runs unchanged on
# your Mac (.env) and on the cloud host (Variables/Secrets UI).
# Never commit real secrets to this file.
# ---------------------------------------------------------------
# ── PATHS (environment-overridable) ───────────────────────────
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # cloud host sets env vars directly
import pandas as pd

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


BASE      = Path(os.getenv("MYM_BASE", os.path.expanduser("~/etrade")))
DATA_DIR  = BASE / "data"
#DATA_1M   = str(DATA_DIR / "MYM_1m_tv_bento.csv")   # backtest base bars
LOG_FILE  = str(BASE / "logs" / "signal_log.txt")
# -- CONTRACT / DATASET ----------------------------------------
DATASET             = "GLBX.MDP3" # CME Globex (covers CBOT / MYM)
# Explicit contract, pinned manually to avoid continuous-roll splicing.
# MYMU6 = Micro E-mini Dow, SEPTEMBER 2026 (root+month+1-digit year; U=Sep, 6=2026).
# >>> UPDATE at each quarterly roll (H=Mar, M=Jun, U=Sep, Z=Dec) <<<
#SYMBOL              = "MYMU6"
#STYPE_IN            = "raw_symbol"                            # explicit contract symbol
# --- continuous layout (use INSTEAD of the two lines above) ---
SYMBOL               = "MYM.v.0"                               # volume-weighted continuous
STYPE_IN             = "continuous"
SCHEMA               = "ohlcv-1m"                              # 5m/15m/1h are resampled from this
DATA_LAG             = pd.Timedelta(minutes=19, seconds=32)    # keep Historical API end inside the available edge
PX_SCALE             = 1e9                                     # Live prices are raw ints; Historical .to_df() is pre-scaled

# ── STRATEGY PARAMETERS (v7 — must match strategy.py backtest) ─
TICKER              = "MYM"
ACCOUNT_SIZE        = 50000
RISK_PCT            = 0.005         # 0.5% per trade (v7 backtest value)
TICK_VALUE          = 0.50          # MYM = $0.50/point (CME-verified; YM is $5)
MIN_SCORE           = 4
T2_CAP              = 2.5           # R:R cap on T2
MAX_BODY_PCT        = 40            # base candle: body <= 40% of range
MIN_BODY_PCT        = 42            # leg candle:  body >= 50% of range
MIN_RANGE_RATIO     = 0.631         # leg range >= base_height * this
RANGE_MULTIPLIER    = 1.01          # base range <= rolling-median * this
MIN_ZONE_HEIGHT     = 13            # pts
MAX_BASE_CANDLES    = 9
STOP_BUFFER         = 8             # static fallback; live run computes dynamically
ENTRY_SIGMA         = 1             # proximity band = this many std devs of bar range
ENTRY_SIGMA_WINDOW  = 20            # rolling window (bars) for the range std
DATA_DAYS           = 75            # days of 1m history pulled from Databento each run
MAX_TAPS            = 16   
TEST_MODE           = False # reject a zone once it has been tapped this many times
# min_zone_height calc that goes just before detect_zones
ZONE_HEIGHT_K       = 0.38
ENTRY_HOUR_MIN      = 7             # earliest ET hour an entry may trigger
CUTOFF_HOUR         = 16            # 4:00 PM EST hard cutoff
ENTRY_HOUR_MAX      = CUTOFF_HOUR   # exclusive — no entries at/after this hour
# Schwab quote symbol — explicit contract (NOT the Databento continuous code).
# >>> bump at each quarterly roll, same as data_feed SYMBOL <<<
SCHWAB_QUOTE_SYMBOL = "/MYMU26"     # Micro Dow, Sep 2026

# Lookbacks — note the candles, they differ:
ZONE_LOOKBACK_DAYS  = 60       # set_curve: prev 60 x 1h bars
TREND_WINDOW        = 120       # classify_trend: prev 120 rolling window of 15m bars
ZONE_WINDOW_DAYS    = 120       # detect_zones: prev 120 candles (last 10hrs) of 15m

# ── SCHEDULER ─────────────────────────────────────────────────
RUN_HOURS = [7, 8, 9, 10, 11, 12]
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
SCHWAB_API_KEY          = os.getenv("SCHWAB_API_KEY", "") 
SCHWAB_APP_SECRET       = os.getenv("SCHWAB_APP_SECRET", "") 
SCHWAB_CALLBACK_URL     = os.getenv("SCHWAB_CALLBACK_URL", "")
SCHWAB_TOKEN_PATH       = os.getenv("SCHWAB_TOKEN_PATH", "")
SCHWAB_HEADLESS         = os.getenv("SCHWAB_HEADLESS", "")

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
