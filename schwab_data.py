"""
schwab_data.py
==============
Market-data loader for a futures trading algorithm, built on the Charles Schwab
Trader API (Market Data Production) via the `schwab-py` wrapper.

It exposes the data your algo needs in `datetime, open, high, low, close` (OHLC)
form, and is written for a scheduled "pipeline" run (e.g. 08:00–13:00 ET).

────────────────────────────────────────────────────────────────────────────
THREE DATA PATHS (pick per use case)
────────────────────────────────────────────────────────────────────────────
1. LIVE SNAPSHOT  -> get_live_quote_ohlc()
   One REST call (Client.get_quotes). Returns the *current session* OHLC plus
   last/bid/ask for the contract. Best for "fire a test order based on
   near-current market conditions." Stateless: no connection to hold open.

2. LIVE OHLC BARS -> stream_ohlc_bars()  [the open/receive/close model]
   Opens the Schwab streaming WebSocket, subscribes to CHART_FUTURES, and emits
   completed 1-minute OHLCV bars as they print. login() = open, logout() = close.
   This is the native datetime+OHLC feed for FUTURES.

3. HISTORICAL OHLC -> get_historical_ohlc()  [equities/ETF only — futures raise]
   Uses Client.get_price_history_*. IMPORTANT: Schwab serves price history for
   EQUITIES/ETFs ONLY — NOT futures, so this will NOT return MYM history. For MYM
   history use databento_data.get_historical_ohlc_mym(). The equities path stays
   here (guarded) so a '/'-prefixed futures symbol fails loudly.

────────────────────────────────────────────────────────────────────────────
SYMBOLS
────────────────────────────────────────────────────────────────────────────
Schwab futures format is slash-prefixed with a 2-digit year:  /MYMU26
TradersPost (your execution leg) uses a 4-digit year:          MYMU2026
Both refer to the Micro E-mini Dow, September 2026 contract.

────────────────────────────────────────────────────────────────────────────
ORDER OUT
────────────────────────────────────────────────────────────────────────────
This module is the DATA layer only. Order routing is delegated to your existing
alerts.py (alerts.traderspost_alert). validate_chain() and make_bar_handler()
call it; your strategy supplies the internal signal (alert_data) dict.

────────────────────────────────────────────────────────────────────────────
SETUP
────────────────────────────────────────────────────────────────────────────
    pip install schwab-py
Set these environment variables (do NOT hardcode secrets):
    SCHWAB_API_KEY      = your app key (consumer key)
    SCHWAB_APP_SECRET   = your app secret
    SCHWAB_CALLBACK_URL = e.g. https://127.0.0.1:8182  (must match the portal)
    SCHWAB_TOKEN_PATH   = e.g. ./schwab_token.json     (where tokens persist)
    SCHWAB_HEADLESS     = 1 on the cloud host (Render) — load token, no browser

First run (local) opens a browser for the one-time OAuth login. After that the
token file auto-refreshes: access token ~30 min, refresh token ~7 days. On a
headless host, mint the token locally and ship it (see schwab_py_essentials.md).
"""

from __future__ import annotations

import asyncio
import os
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from schwab import auth
from schwab.streaming import StreamClient

# ── Config ──────────────────────────────────────────────────────────────────
API_KEY      = os.environ.get("SCHWAB_API_KEY", "")
APP_SECRET   = os.environ.get("SCHWAB_APP_SECRET", "")
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH   = os.environ.get("SCHWAB_TOKEN_PATH", "./schwab_token.json")
# SCHWAB_HEADLESS=1 on the cloud host (Render): load an existing token WITHOUT a
# browser. Leave unset locally so the first run can open a browser to mint it.
HEADLESS     = os.environ.get("SCHWAB_HEADLESS", "").lower() in ("1", "true", "yes")

# Order routing is delegated to your existing alerts.py (which reads
# TRADERSPOST_WEBHOOK_URL from the environment itself) — see "ORDER OUT" below.

EASTERN = ZoneInfo("America/New_York")  # you're in FL; bars are timestamped in ET

# The contract under test, in each system's dialect.
SCHWAB_SYMBOL      = "/MYMU26"     # Schwab Market Data API
TRADERSPOST_SYMBOL = "MYMU2026"    # TradersPost webhook payload

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("schwab_data")


# ── Symbol helpers ──────────────────────────────────────────────────────────
_MONTHS = "FGHJKMNQUVXZ"  # CME month codes Jan..Dec

def schwab_to_traderspost(schwab_sym: str) -> str:
    """'/MYMU26' -> 'MYMU2026'. Expands the 2-digit year to 4 digits."""
    s = schwab_sym.lstrip("/")
    root, mcode, yy = s[:-3], s[-3], s[-2:]
    return f"{root}{mcode}20{yy}"


# ── Client factories ────────────────────────────────────────────────────────
def build_client(asyncio_mode: bool = False):
    """Authenticated schwab-py client (auto-refreshes the 30-min access token).

    asyncio_mode=True is REQUIRED for the streaming client (Path 2).

    Local first run: easy_client opens a browser once to mint the token file.
    Cloud/headless (SCHWAB_HEADLESS=1, e.g. Render): client_from_token_file loads
    an EXISTING token with no browser. Mint the token locally, ship it as a Render
    Secret File, and copy it to a writable path before running (schwab-py rewrites
    the token file whenever it refreshes the access token).
    """
    if not API_KEY or not APP_SECRET:
        raise RuntimeError("Set SCHWAB_API_KEY and SCHWAB_APP_SECRET in the environment.")
    if HEADLESS:
        return auth.client_from_token_file(
            token_path=TOKEN_PATH, api_key=API_KEY, app_secret=APP_SECRET,
            asyncio=asyncio_mode,
        )
    return auth.easy_client(
        api_key=API_KEY, app_secret=APP_SECRET, callback_url=CALLBACK_URL,
        token_path=TOKEN_PATH, asyncio=asyncio_mode,
    )


# ── Path 1: LIVE SNAPSHOT (REST) ────────────────────────────────────────────
def get_live_quote_ohlc(client, symbol: str = SCHWAB_SYMBOL) -> dict:
    """Single REST quote -> current-session OHLC snapshot.

    Note on 'close': Schwab's `closePrice` is the PRIOR session's close, so for a
    live "current bar" we use `lastPrice` as the close. `openPrice/highPrice/
    lowPrice` are the running values for the current session.

    Returns a dict: {datetime, open, high, low, close, last, bid, ask, symbol}.
    Use get_quotes (NOT get_quote) for futures — '/' breaks URL-path encoding.
    """
    resp = client.get_quotes([symbol])
    resp.raise_for_status()
    payload = resp.json().get(symbol, {})
    q = payload.get("quote", {})
    if not q:
        raise RuntimeError(f"No quote returned for {symbol}. Check entitlement/symbol.")

    # quoteTime / tradeTime are epoch milliseconds when present.
    ts_ms = q.get("tradeTime") or q.get("quoteTime")
    ts = (datetime.fromtimestamp(ts_ms / 1000, tz=EASTERN)
          if ts_ms else datetime.now(tz=EASTERN))

    return {
        "datetime": ts,
        "open":  q.get("openPrice"),
        "high":  q.get("highPrice"),
        "low":   q.get("lowPrice"),
        "close": q.get("lastPrice"),       # live close = last traded price
        "last":  q.get("lastPrice"),
        "bid":   q.get("bidPrice"),
        "ask":   q.get("askPrice"),
        "symbol": symbol,
    }


# ── Path 2: LIVE OHLC BARS (streaming) — open / receive / close ──────────────
# CHART_FUTURES field map (verified from StreamClient.ChartFuturesFields):
#   0 SYMBOL | 1 CHART_TIME_MILLIS | 2 OPEN_PRICE | 3 HIGH_PRICE
#   4 LOW_PRICE | 5 CLOSE_PRICE | 6 VOLUME
_CF = StreamClient.ChartFuturesFields

def _decode_chart_bar(item: dict) -> dict:
    """Translate one raw CHART_FUTURES content item (numeric keys) into OHLC."""
    g = lambda field: item.get(str(field.value))
    t_ms = g(_CF.CHART_TIME_MILLIS)
    ts = datetime.fromtimestamp(t_ms / 1000, tz=EASTERN) if t_ms else None
    return {
        "datetime": ts,
        "open":  g(_CF.OPEN_PRICE),
        "high":  g(_CF.HIGH_PRICE),
        "low":   g(_CF.LOW_PRICE),
        "close": g(_CF.CLOSE_PRICE),
        "volume": g(_CF.VOLUME),
        "symbol": g(_CF.SYMBOL),
    }


async def stream_ohlc_bars(on_bar, symbol: str = SCHWAB_SYMBOL,
                           stop_at: dtime | None = None):
    """Open the stream, emit 1-min OHLC bars to `on_bar(dict)`, close cleanly.

    `on_bar` is your callback: this is where your algo consumes each completed
    bar (and, when it decides to trade, POSTs to the TradersPost webhook).
    `stop_at` is a local (ET) wall-clock time to log out and return, e.g.
    dtime(13, 0) for a 1pm shutdown.
    """
    client = build_client(asyncio_mode=True)     # async client required here
    stream = StreamClient(client)

    await stream.login()                          # ── OPEN connection ──
    log.info("Stream login OK; subscribing CHART_FUTURES %s", symbol)

    def handler(msg):
        for item in msg.get("content", []):
            bar = _decode_chart_bar(item)
            if bar["datetime"] is not None:
                on_bar(bar)

    stream.add_chart_futures_handler(handler)
    await stream.chart_futures_subs([symbol])

    try:
        while True:
            if stop_at is not None and datetime.now(tz=EASTERN).time() >= stop_at:
                log.info("Reached stop time %s — shutting down.", stop_at)
                break
            await stream.handle_message()         # ── RECEIVE data ──
    finally:
        await stream.logout()                     # ── CLOSE connection ──
        log.info("Stream logout complete.")


def run_pipeline(on_bar, symbol: str = SCHWAB_SYMBOL,
                 start: dtime = dtime(8, 0), end: dtime = dtime(13, 0)):
    """Scheduled-window runner (your 8am–1pm ET job).

    If launched before `start`, it sleeps until the window opens, streams bars
    to `on_bar`, and logs out at `end`. Schedule this with cron / a CI runner;
    the function self-terminates at `end`.
    """
    now = datetime.now(tz=EASTERN)
    if now.time() < start:
        wait_s = (datetime.combine(now.date(), start, tzinfo=EASTERN) - now).total_seconds()
        log.info("Before window; sleeping %.0fs until %s ET.", wait_s, start)
        import time as _t; _t.sleep(max(0, wait_s))
    elif now.time() >= end:
        log.warning("Launched after window close (%s ET). Nothing to do.", end)
        return
    asyncio.run(stream_ohlc_bars(on_bar, symbol=symbol, stop_at=end))


# ── Path 3: HISTORICAL OHLC (REST) — EQUITIES/ETF ONLY ──────────────────────
def get_historical_ohlc(client, symbol: str, minutes: bool = True) -> list[dict]:
    """Historical OHLC candles via Client.get_price_history_*.

    ⚠ HARD LIMIT: Schwab provides price history for EQUITIES and ETFs ONLY.
      It does NOT provide history for futures (or options). Passing a '/'-prefixed
      futures symbol here will raise — by design — so you don't silently get
      empty data. For MYM historical bars, source from a futures data vendor.

    For an equity/ETF symbol it returns [{datetime, open, high, low, close,
    volume}, ...] (e.g. pass 'YM'-proxy ETFs like 'DIA' to sanity-check logic).
    """
    if symbol.startswith("/"):
        raise ValueError(
            f"Schwab has no price history for futures ({symbol}). For MYM history "
            "use databento_data.get_historical_ohlc_mym(); use the Schwab stream "
            "for the live edge."
        )
    resp = (client.get_price_history_every_minute(symbol) if minutes
            else client.get_price_history_every_day(symbol))
    resp.raise_for_status()
    candles = resp.json().get("candles", [])
    return [{
        "datetime": datetime.fromtimestamp(c["datetime"] / 1000, tz=EASTERN),
        "open": c["open"], "high": c["high"], "low": c["low"],
        "close": c["close"], "volume": c["volume"],
    } for c in candles]


# ── ORDER OUT (delegated to your existing alerts.py) ────────────────────────
# This module is the DATA layer. Order routing stays in alerts.py
# (alerts.traderspost_alert / build_payload / AlertResult) so there is ONE
# sender. Your strategy returns an internal signal dict (the alert_data schema);
# alerts.build_payload turns it into the TradersPost payload. Keys it needs:
#   type, entry, stop_points, t2_points (or t1_points), contracts   (+ extras)


def make_bar_handler(strategy_decide, *, test_mode: bool = True):
    """Bridge the Schwab bar feed to your TradersPost sender (alerts.py).

    `strategy_decide(bar) -> signal dict | None` is YOUR algo: given a completed
    OHLC bar it returns an internal signal (alert_data schema) to trade, or None.
    Any returned signal is sent via alerts.traderspost_alert(signal, test_mode=...).
    Returns a callback for stream_ohlc_bars / run_pipeline.
    """
    import alerts  # lazy import: keeps this DATA module importable in backtests
    def on_bar(bar: dict):
        log.info("BAR %s O=%s H=%s L=%s C=%s V=%s",
                 bar["datetime"].strftime("%H:%M"),
                 bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"])
        signal = strategy_decide(bar)            # ← your strategy returns alert_data | None
        if not signal:
            return
        result = alerts.traderspost_alert(signal, test_mode=test_mode)
        log.info("alert ok=%s status=%s id=%s detail=%s",
                 result.ok, result.status_code, result.signal_id, result.detail)
    return on_bar


def validate_chain(symbol: str = SCHWAB_SYMBOL, *, contracts: int = 1,
                   stop_points: float = 25, t2_points: float = 50,
                   test_mode: bool = True) -> dict:
    """End-to-end chain check: Schwab snapshot -> TradersPost order, one call.

    Steps: (1) pull one live Schwab quote — also confirms your real-time futures
    entitlement, since the timestamp should be current; (2) build a minimal valid
    signal in your alert_data schema; (3) send it via alerts.traderspost_alert
    and print TradersPost's ids. Run before market open.

    test_mode=True  -> "test": true, validated only, NOT routed to a broker.
    test_mode=False -> a REAL bracket order on your connected Lucid/Tradovate
        account (pre-production bracket validation). Use 1 micro contract, confirm
        the stop + target attach on the broker, then FLATTEN manually. Only flip
        to False when you intend a real order.

    Returns {ok, status, signal_id, log_id, price, test_mode}.
    """
    import alerts  # lazy import of your order layer

    if not test_mode:
        log.warning("validate_chain test_mode=False — sending a REAL order to the "
                    "connected broker. Use minimal size and be ready to flatten.")

    # 1) DATA — one live snapshot (doubles as the real-time entitlement check)
    client = build_client()
    snap = get_live_quote_ohlc(client, symbol)
    price = snap["last"] or snap["close"]
    print(f"[1/3] Schwab live {symbol}: {price} "
          f"@ {snap['datetime']:%Y-%m-%d %H:%M:%S %Z}")

    # 2) BUILD — minimal valid signal in YOUR internal (alert_data) schema.
    #    type 'DBR' is a long signal (alerts.LONG_SIGNAL_TYPES) -> action 'buy'.
    signal = {
        "type": "DBR",
        "entry": price,
        "stop_points": stop_points,
        "t2_points": t2_points,
        "contracts": contracts,
    }
    print(f"[2/3] Signal (alert_data schema, test_mode={test_mode}): {signal}")

    # 3) ORDER OUT — send via your existing sender.
    #    alerts.traderspost_alert defaults ticker to config DEFAULT_TICKER ('MYM').
    result = alerts.traderspost_alert(signal, test_mode=test_mode)
    log_id = getattr(result, "log_id", None)   # present only if added to AlertResult (see note)
    print(f"[3/3] TradersPost ok={result.ok} status={result.status_code} "
          f"id={result.signal_id} logId={log_id} detail={result.detail!r}")

    return {"ok": result.ok, "status": result.status_code,
            "signal_id": result.signal_id, "log_id": log_id,
            "price": price, "test_mode": test_mode}


# ── Demo: complete data-in / order-out loop ─────────────────────────────────
def example_strategy(bar: dict):
    """STUB — replace with your real strategy. Return an internal signal dict
    (the alert_data schema: type/entry/stop_points/t2_points/contracts/...) to
    trade on this bar, or None to stand pat. This trivial example never trades."""
    # e.g.  if <your zone/entry logic>: return {"type": "DBR", "entry": ..., ...}
    return None


if __name__ == "__main__":
    # One-call end-to-end smoke test (Schwab data -> TradersPost test:true order):
    validate_chain(SCHWAB_SYMBOL)

    # Live run across the 8am–1pm ET window (uncomment for production):
    # handler = make_bar_handler(example_strategy, test_mode=True)
    # run_pipeline(handler, SCHWAB_SYMBOL, start=dtime(8, 0), end=dtime(13, 0))

    # Historical (equities/ETF only; futures raise by design):
    # c = build_client(); hist = get_historical_ohlc(c, "DIA")

    # MYM history -> Databento (Schwab has none for futures):
    # import databento_data as dbd; bars = dbd.get_historical_ohlc_mym("2026-06-20")
