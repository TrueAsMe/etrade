from __future__ import annotations

import os
import asyncio
import threading
import time
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
REPLAY_TIMEOUT_S      = config.LIVE_REPLAY_TIMEOUT_S
REPLAY_OVERLAP        = config.LIVE_REPLAY_OVERLAP
REPLAY_MAX_LOOKBACK   = config.LIVE_REPLAY_MAX_LOOKBACK


class DataFeedError(RuntimeError):
    """The 1m series could not be brought up to the live edge. Callers must
    treat this as fatal for the run (fail closed) — never fall back to
    Historical-only bars, which lag real time."""


class ReplayTimeout(DataFeedError, TimeoutError):
    """Live replay did not report REPLAY_COMPLETED within the timeout."""

# -- CREDENTIALS -----------------------------------------------
def _client():
    key = getattr(config, "DATABENTO_API_KEY", "") or os.getenv("DATABENTO_API_KEY", "")
    if not key:
        raise RuntimeError("DATABENTO_API_KEY not set (config.py or .env).")
    return key

# -- CLEANING / RESAMPLE (shared by Historical + Live) ---------
def _empty_1m():
    return pd.DataFrame(columns=["open", "high", "low", "close"], dtype=float,
                        index=pd.DatetimeIndex([], tz="UTC", name="datetime"))

def _clean_1m(df, label="1m"):
    """Raw Databento ohlcv-1m -> tz-aware UTC 'datetime' index, spreads & dupes
    removed, OHLC only — the SAME cleaning the backtest CSV got, so live and
    backtest bars are treated identically."""
    if df.empty:
        print(f"{label:6} |      0 bars | (no data returned)")
        return _empty_1m()
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
def _historical_end(now=None):
    """Exclusive end for Historical pulls: the dataset's published edge for
    SCHEMA (metadata.get_dataset_range), floored to the minute so every bar
    returned has closed. If the metadata call fails, fall back to
    now - DATA_LAG; the Live replay stitch covers whatever gap remains."""
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    try:
        rng = db.Historical(_client()).metadata.get_dataset_range(dataset=DATASET)
        per_schema = (rng.get("schema") or {}).get(SCHEMA) or {}
        edge = pd.Timestamp(per_schema.get("end") or rng["end"])
        edge = edge.tz_localize("UTC") if edge.tzinfo is None else edge.tz_convert("UTC")
    except Exception as e:
        print(f"Historical edge lookup failed ({type(e).__name__}: {e}) — "
              f"falling back to now - {DATA_LAG}")
        edge = now - DATA_LAG
    return min(edge, now).floor("min")


def _safe_end(end=None):
    """End-time for Historical pulls (explicit `end`, else the published edge)."""
    return end if end is not None else _historical_end()


def _pull_1m(start, end=None):
    """Low-level Databento ohlcv-1m pull -> cleaned 1m DataFrame."""
    end = _safe_end(end)
    if pd.Timestamp(start) >= pd.Timestamp(end):
        return _empty_1m()
    store = db.Historical(_client()).timeseries.get_range(
        dataset=DATASET, symbols=[SYMBOL], schema=SCHEMA, stype_in=STYPE_IN,
        start=start, end=end,
    )
    return _clean_1m(store.to_df())

# -- LIVE-REPLAY GAP-FILL (one-shot, for cron-triggered processes) ----
# Historical trails the live edge; db.Live's intraday REPLAY serves the same
# 1m bars (verified identical on overlap) up to the last closed minute. This
# opens Live, buffers bars until REPLAY_COMPLETED, then closes — bounded
# (seconds), never held open. Replay reaches back at most 24h.
class _ReplayCollector:
    """Record callback for a one-shot replay. Runs on Databento's I/O thread;
    `done` is a threading.Event so the caller can wait on it safely. Gateway
    ErrorMsg and callback exceptions end the wait immediately instead of
    letting it run to the timeout."""

    def __init__(self):
        self.bars = []
        self.errors = []
        self.done = threading.Event()

    def __call__(self, record):
        if isinstance(record, db.OHLCVMsg):
            self.bars.append((record.ts_event, record.open, record.high,
                              record.low, record.close))
        elif isinstance(record, db.ErrorMsg):
            self.errors.append(f"gateway error {record.code}: {record.err}")
            self.done.set()
        elif isinstance(record, db.SystemMsg) and record.code == db.SystemCode.REPLAY_COMPLETED:
            self.done.set()

    def on_exception(self, exc):
        self.errors.append(f"record callback raised {type(exc).__name__}: {exc}")
        self.done.set()

    def to_df(self):
        if not self.bars:
            return _empty_1m()
        df = pd.DataFrame(self.bars, columns=["ts_event", "open", "high", "low", "close"])
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts_event"), unit="ns", utc=True),
                                    name="datetime")
        return df.astype(float) / PX_SCALE      # Live prices are fixed-point ints


def _close_live(live):
    """Best-effort shutdown; never masks the caller's own outcome."""
    try:
        live.stop()
        live.block_for_close(timeout=5)
    except Exception:
        try:
            live.terminate()
        except Exception:
            pass


def pull_via_live_replay(start, timeout=REPLAY_TIMEOUT_S, now=None):
    """Synchronous one-shot pull of closed 1m bars from `start` (inclusive) to
    now via db.Live intraday replay. Raises DataFeedError on any failure —
    gateway error, connection closed early, timeout, or `start` beyond the
    replay window — so callers fail closed rather than run on lagged data."""
    start = pd.Timestamp(start)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if start < now - REPLAY_MAX_LOOKBACK:
        raise DataFeedError(
            f"Live replay start {start} is older than the intraday-replay window "
            f"({REPLAY_MAX_LOOKBACK}); Historical edge is too stale to stitch.")

    live = db.Live(key=_client())
    collector = _ReplayCollector()
    live.add_callback(collector, exception_callback=collector.on_exception)
    try:
        live.subscribe(dataset=DATASET, schema=SCHEMA, stype_in=STYPE_IN,
                       symbols=[SYMBOL], start=start)
        live.start()
        deadline = time.monotonic() + timeout
        while not collector.done.wait(0.1):
            if not live.is_connected() or time.monotonic() >= deadline:
                break
    except DataFeedError:
        raise
    except Exception as e:
        raise DataFeedError(f"Live replay failed: {type(e).__name__}: {e}") from e
    finally:
        _close_live(live)

    if collector.errors:
        raise DataFeedError("Live replay failed: " + "; ".join(collector.errors))
    if not collector.done.is_set():
        if time.monotonic() >= deadline:
            raise ReplayTimeout(f"Live replay did not complete within {timeout}s")
        raise DataFeedError("Live session closed before REPLAY_COMPLETED")
    return _clean_1m(collector.to_df(), label="replay")


def load_all_timeframes(days=config.DATA_DAYS, cache_path=None, live_fill=True, now=None):
    """Batch snapshot for signals.py. Returns (df_1m, df_5m, df_15m, df_1h).

    Bars come from Historical up to its published edge, then Live intraday
    replay from (edge - REPLAY_OVERLAP) to now. On overlap the Historical bar
    wins. If the replay fails this raises DataFeedError — there is no
    Historical-only fallback, because those bars trail real time.

    cache_path=None : fresh Historical pull of the full window.
    cache_path set  : incremental — read cache, pull Historical only after the
                      last cached minute, stitch, trim, rewrite.
    live_fill=False : Historical only (research/backtest use; never for live).
    """
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    cutoff = now - pd.Timedelta(days=days)

    base, hist_start = _empty_1m(), cutoff
    if cache_path and os.path.exists(cache_path):
        cached = pd.read_csv(cache_path)
        cached["datetime"] = pd.to_datetime(cached["datetime"], utc=True)
        base = cached.set_index("datetime").sort_index()[["open", "high", "low", "close"]]
        if not base.empty:
            hist_start = max(cutoff, base.index.max() + pd.Timedelta(minutes=1))

    hist_end = _historical_end(now)
    hist = _pull_1m(start=hist_start, end=hist_end)

    parts = [base]
    if live_fill:
        replay_start = hist_end - REPLAY_OVERLAP
        replay = pull_via_live_replay(replay_start, now=now)       # raises -> fail closed
        parts.append(replay)
    parts.append(hist)                                              # last wins on overlap
    parts = [p for p in parts if not p.empty]
    df_1m = pd.concat(parts) if parts else _empty_1m()
    df_1m = df_1m[~df_1m.index.duplicated(keep="last")].sort_index()
    df_1m = df_1m[df_1m.index >= cutoff]                            # trim to rolling window

    last = df_1m.index.max() if not df_1m.empty else None
    print(f"stitch | historical edge {hist_end} | live_fill={live_fill} | last 1m bar {last}")
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