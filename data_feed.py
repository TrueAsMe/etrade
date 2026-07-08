# data_feed.py — MYM Data Layer (Databento)
# ---------------------------------------------------------------
# Single source of market data for the v6 strategy.
#   Databento ohlcv-1m  ->  _clean  ->  resample_ohlc  ->  5m / 15m
# 1-minute is the only timeframe pulled; everything else is resampled
# so the source stays consistent with the backtest base.
#
# Public entry point (unchanged contract used by signals.py):
#   load_all_timeframes()  ->  (df_5m, df_15m)
#
# No live connection is required — Databento Historical serves bars up
# to the latest available minute (a few minutes behind real time), which
# is plenty for a 5m/15m strategy. Schwab is used ONLY for live bracket
# order testing (see schwab_data.py), not for data.
# ---------------------------------------------------------------

from __future__ import annotations

import os
import pandas as pd
import config

# -- DATABENTO CONTRACT/DATASET --------------------------------
DATASET   = "GLBX.MDP3"     # CME Globex (covers CBOT, where MYM lists)
# Explicit contract, pinned manually for now to avoid continuous-roll splicing.
# MYMU6 = Micro E-mini Dow, SEPTEMBER 2026 (CME format: root+month+1-digit year;
# U=Sep, 6=2026). Matches TradersPost MYMU2026 / Schwab /MYMU26.
# >>> UPDATE THIS at each quarterly roll (H=Mar, M=Jun, U=Sep, Z=Dec) <<<
SYMBOL    = "MYMU6"
SCHEMA    = "ohlcv-1m"      # 1-minute OHLCV; 5m/15m are resampled from this
STYPE_IN  = "raw_symbol"    # explicit native contract symbol (not a continuous code)

# Databento Historical for this plan/dataset lags real time by ~8 hours; requesting
# closer to "now" returns 422 'dataset_unavailable_range'. Cap the request end this
# far back (8h + 30m margin to stay safely inside the available window). Lower toward
# 8h for slightly fresher data, or shrink it with a live CME subscription.
DATA_LAG = pd.Timedelta(minutes=16)#hours=4,
DATA_LOOKBACK = 200

def _client():
    """Databento Historical client. Key from config.DATABENTO_API_KEY or env."""
    key = getattr(config, "DATABENTO_API_KEY", "") or os.getenv("DATABENTO_API_KEY", "")
    if not key:
        raise RuntimeError("DATABENTO_API_KEY not set (add it to config.py or your .env).")
    import databento as db
    return db.Historical(key)


# -- RESAMPLE HELPER (retained verbatim) -----------------------
def resample_ohlc(df, rule):
    df_resampled = df.resample(rule).agg({
        "open"  : "first",
        "high"  : "max",
        "low"   : "min",
        "close" : "last",
    }).dropna()

    start_date     = df_resampled.index[0].date()
    end_date       = df_resampled.index[-1].date()
    range_day_cnt  = (end_date - start_date).days
    range_week_cnt = int(range_day_cnt / 7)

    print(f"{rule:6} | {len(df_resampled):>6} bars | "
          f"{start_date} to {end_date} | "
          f"Price range: {df_resampled['low'].min():.0f} -- {df_resampled['high'].max():.0f} | "
          f"{range_day_cnt} days | "
          f"{range_week_cnt} weeks")

    return df_resampled


# -- 1m CLEANING (mirrors the old load_tv_csv treatment) -------
def _clean_1m(df, label="1m"):
    """Raw Databento ohlcv-1m -> tz-aware UTC 'datetime' index, spreads & dupes
    removed, OHLC only -- the SAME cleaning the backtest CSV got, so live and
    backtest bars are treated identically."""
    df = df.copy()
    if df.empty:
        print(f"{label:6} |      0 bars | (no data returned)")
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df.index = pd.to_datetime(df.index, utc=True)        # ts_event -> tz-aware UTC
    df.index.name = "datetime"
    df = df.dropna().sort_index()

    if "symbol" in df.columns:                            # drop contract-roll spread rows
        df = df[~df["symbol"].astype(str).str.contains("-")]

    dupes = int(df.index.duplicated().sum())              # keep last tick for the minute
    df = df[~df.index.duplicated(keep="last")]
    df = df[["open", "high", "low", "close"]].copy()

    start_date    = df.index[0].date()
    end_date      = df.index[-1].date()
    range_day_cnt = (end_date - start_date).days
    print(f"{label:6} | {len(df):>6} bars | {start_date} to {end_date} | "
          f"Price range: {df['low'].min():.0f} -- {df['high'].max():.0f} | "
          f"{range_day_cnt} days | {int(range_day_cnt / 7)} weeks | dupes removed: {dupes}")
    return df


def _safe_end(end=None):
    """End-time capped safely in the past (Historical API rejects the recent edge)."""
    if end is not None:
        return end
    return pd.Timestamp.now(tz="UTC") - DATA_LAG


def _pull_1m(start, end=None):
    """Low-level Databento ohlcv-1m pull -> cleaned 1m DataFrame (end=None -> now-DATA_LAG)."""
    store = _client().timeseries.get_range(
        dataset=DATASET, symbols=[SYMBOL], schema=SCHEMA, stype_in=STYPE_IN,
        start=start, end=_safe_end(end),
    )
    return _clean_1m(store.to_df())


# -- OPTION 3: Databento | API CONNECTION ----------------------
def load_from_databento(days=DATA_LOOKBACK, cache_path=None):
    """Pull `days` of MYM 1-minute bars and resample to 5m + 15m.

    cache_path=None (DEFAULT): fresh pull of the full window each run -- simplest,
        recommended to get to production fast. A 200-day 1m pull is quick.
    cache_path set (optional): incremental -- read the cache, pull only the bars
        since the last cached minute, append, dedupe, trim to `days`, rewrite the
        cache. Saves Databento credits on repeat runs. Use a DEDICATED cache file
        (not config.DATA_1M, which is your backtest base).

    Returns (df_1m, df_5m, df_15m, df_1h); each OHLC with a tz-aware UTC index.
    """
    now    = pd.Timestamp.now(tz="UTC")
    cutoff = now - pd.Timedelta(days=days)

    if cache_path and os.path.exists(cache_path):
        cached = pd.read_csv(cache_path)
        cached["datetime"] = pd.to_datetime(cached["datetime"], utc=True)
        cached = cached.set_index("datetime").sort_index()
        last  = cached.index.max()
        fresh = _pull_1m(start=last)                       # only the gap since last cached bar
        df_1m = pd.concat([cached[["open", "high", "low", "close"]], fresh])
        df_1m = df_1m[~df_1m.index.duplicated(keep="last")].sort_index()
    else:
        df_1m = _pull_1m(start=cutoff)

    df_1m = df_1m[df_1m.index >= cutoff]                   # trim to rolling window
    if cache_path:
        df_1m.reset_index().to_csv(cache_path, index=False)

    df_5m  = resample_ohlc(df_1m, "5min")
    df_15m = resample_ohlc(df_1m, "15min")
    df_1h = resample_ohlc(df_1m, "1h")

    print(f"OK Databento data loaded | 1m: {len(df_1m)} | 5m: {len(df_5m)} | 15m: {len(df_15m)} | 1h: {len(df_1h)}")
    return df_1m, df_5m, df_15m, df_1h


def load_all_timeframes(days=DATA_LOOKBACK, cache_path=None):
    """Entry point used by signals.py. Returns (df_5m, df_15m)."""
    df_1m, df_5m, df_15m, df_1h = load_from_databento(days=days, cache_path=cache_path)
    return df_5m, df_15m, df_1h


def estimate_databento_cost(days=DATA_LOOKBACK):
    """$ cost of the `days`-day 1m pull, WITHOUT spending credits. Call before a backfill."""
    start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    return _client().metadata.get_cost(
        dataset=DATASET, symbols=[SYMBOL], schema=SCHEMA, stype_in=STYPE_IN,
        start=start, end=_safe_end(),
    )


if __name__ == "__main__":
    print(f"Est. Databento cost (200d 1m): ${estimate_databento_cost():.4f}")
    df_1m, df_5m, df_15m, df_1h = load_from_databento(200)