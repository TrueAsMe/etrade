from __future__ import annotations

import os
import asyncio
import pandas as pd
import databento as db
import config

# -- CONTRACT / DATASET ----------------------------------------
DATASET  = config.DATASET                             # CME Globex (covers CBOT / MYM)
# Explicit contract, pinned manually to avoid continuous-roll splicing.
# MYMU6 = Micro E-mini Dow, SEPTEMBER 2026 (root+month+1-digit year; U=Sep, 6=2026).
# >>> UPDATE at each quarterly roll (H=Mar, M=Jun, U=Sep, Z=Dec) <<<
SYMBOL   = config.SYMBOL
STYPE_IN = config.STYPE_IN                            # explicit contract symbol
# --- continuous layout (use INSTEAD of the two lines above) ---
#SYMBOL   = "MYM.v.0"                               # volume-weighted continuous
#STYPE_IN = "continuous"
SCHEMA   = config.SCHEMA                               # 5m/15m/1h are resampled from this
DATA_LAG = config.DATA_LAG   # keep Historical end inside the available edge
PX_SCALE = config.PX_SCALE   

# -- CREDENTIALS -----------------------------------------------
def _client():
    key = getattr(config, "DATABENTO_API_KEY", "") or os.getenv("DATABENTO_API_KEY", "")
    if not key:
        raise RuntimeError("DATABENTO_API_KEY not set (config.py or .env).")
    return key

# -- CLEANING / RESAMPLE (shared by Historical + Live) ---------
def _clean_1m(df, label="1m"):
    """Raw Databento ohlcv-1m -> tz-aware UTC 'datetime' index, spreads & dupes
    removed, OHLC only — the SAME cleaning the backtest CSV got, so live and
    backtest bars are treated identically."""
    if df.empty:
        print(f"{label:6} |      0 bars | (no data returned)")
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "datetime"
    df = df.dropna().sort_index()
    if "symbol" in df.columns:                                       # drop roll-spread rows
        df = df[~df["symbol"].astype(str).str.contains("-")]
    dupes = int(df.index.duplicated().sum())
    df = df[~df.index.duplicated(keep="last")]
    df = df[["open", "high", "low", "close"]].copy()
    d0, d1 = df.index[0].date(), df.index[-1].date()
    days = (d1 - d0).days
    print(f"{label:6} | {len(df):>6} bars | {d0} to {d1} | "
          f"range {df['low'].min():.0f}-{df['high'].max():.0f} | "
          f"{days} days | {days // 7} weeks | dupes removed: {dupes}")
    return df

def resample_ohlc(df, rule):
    return df.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()

# -- HISTORICAL PULL -------------------------------------------
def _safe_end(end=None):
    """End-time capped safely in the past (Historical API rejects the recent edge)."""
    return end if end is not None else pd.Timestamp.now(tz="UTC") - DATA_LAG

def _pull_1m(start, end=None):
    """Low-level Databento ohlcv-1m pull -> cleaned 1m DataFrame."""
    store = db.Historical(_client()).timeseries.get_range(
        dataset=DATASET, symbols=[SYMBOL], schema=SCHEMA, stype_in=STYPE_IN,
        start=start, end=_safe_end(end),
    )
    return _clean_1m(store.to_df())

def load_all_timeframes(days=config.DATA_DAYS, cache_path=None):
    """Batch snapshot for signals.py. Returns (df_1m, df_5m, df_15m, df_1h).

    cache_path=None : fresh pull of the full window (simplest).
    cache_path set  : incremental — read cache, pull only the gap since the last
                      cached minute, append, dedupe, trim, rewrite. Saves credits.
                      Use a DEDICATED cache file, not the backtest base CSV.
    """
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)

    if cache_path and os.path.exists(cache_path):
        cached = pd.read_csv(cache_path)
        cached["datetime"] = pd.to_datetime(cached["datetime"], utc=True)
        cached = cached.set_index("datetime").sort_index()
        fresh = _pull_1m(start=cached.index.max())                  # only the gap
        df_1m = pd.concat([cached[["open", "high", "low", "close"]], fresh])
        df_1m = df_1m[~df_1m.index.duplicated(keep="last")].sort_index()
    else:
        df_1m = _pull_1m(start=cutoff)

    df_1m = df_1m[df_1m.index >= cutoff]                            # trim to rolling window
    if cache_path:
        df_1m.reset_index().to_csv(cache_path, index=False)

    df_5m  = resample_ohlc(df_1m, "5min")
    df_15m = resample_ohlc(df_1m, "15min")
    df_1h  = resample_ohlc(df_1m, "1h")
    print(f"OK Databento | 1m:{len(df_1m)} 5m:{len(df_5m)} 15m:{len(df_15m)} 1h:{len(df_1h)}")
    return df_1m, df_5m, df_15m, df_1h

def estimate_databento_cost(days=config.DATA_DAYS):
    """$ cost of the `days`-day 1m pull, WITHOUT spending credits."""
    start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    return db.Historical(_client()).metadata.get_cost(
        dataset=DATASET, symbols=[SYMBOL], schema=SCHEMA, stype_in=STYPE_IN,
        start=start, end=_safe_end(),
    )

# -- LIVE STREAM -----------------------------------------------
async def run_live_feed(days=config.DATA_DAYS, on_new_bar=None):
    """Historical backfill, then live 1m stream. Fires
    on_new_bar((df_1m, df_5m, df_15m, df_1h)) on each closed minute.
    All frame mutation stays on the asyncio loop thread."""
    loop, queue = asyncio.get_running_loop(), asyncio.Queue()
    window = pd.Timedelta(days=days)

    df_1m = _pull_1m(start=pd.Timestamp.now(tz="UTC") - window)      # seed
    start_ns = int(df_1m.index.max().value) + 1 if not df_1m.empty else None  # gapless seam
    print(f"OK backfill | 1m: {len(df_1m)} | live replay from ns={start_ns}")

    def enqueue(rec):                                               # runs on Databento's thread
        loop.call_soon_threadsafe(queue.put_nowait, rec)

    async def consume():
        nonlocal df_1m
        while True:
            rec = await queue.get()
            if isinstance(rec, db.OHLCVMsg):                        # ignore symbol-map/system/error msgs
                ts = pd.Timestamp(rec.ts_event, unit="ns", tz="UTC")
                df_1m.loc[ts] = [rec.open / PX_SCALE, rec.high / PX_SCALE,
                                 rec.low / PX_SCALE, rec.close / PX_SCALE]
                df_1m = df_1m[~df_1m.index.duplicated(keep="last")].sort_index()
                df_1m = df_1m[df_1m.index >= df_1m.index.max() - window]
                if on_new_bar:
                    on_new_bar((df_1m.copy(), resample_ohlc(df_1m, "5min"),
                                resample_ohlc(df_1m, "15min"), resample_ohlc(df_1m, "1h")))
            queue.task_done()

    consumer = asyncio.create_task(consume())

    live = db.Live(key=_client())
    live.subscribe(dataset=DATASET, schema=SCHEMA, stype_in=STYPE_IN, symbols=[SYMBOL], start=start_ns)
    live.add_callback(enqueue)
    await loop.run_in_executor(None, live.start)
    print(f"OK live | streaming {SYMBOL}")

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        live.stop()
        consumer.cancel()
        raise

if __name__ == "__main__":
    # Manual smoke test only — never runs on import.
    print(f"Est. cost ({config.DATA_DAYS}d 1m): ${estimate_databento_cost():.4f}")
    df_1m, df_5m, df_15m, df_1h = load_all_timeframes()