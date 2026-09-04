### Major Updates with v4

- Added Schwab API Live Quote Snapshot to capture the instance-time quote price
  for more precise real-time entries
- Backtested & optimized config.py configuration for best results from the
  current strategy set up.
- Version Architecture

  scheduler.py (loop every 4 min, 8am–4pm ET, Mon–Fri)
    └─ signals.py :: run_signal_check()
         └─ load_from_databento()
              └─ data_feed.py :: load_all_timeframes(days=config.DATA_DAYS, cache_path=None)
                   ├─ _pull_1m(start=cutoff)          # db.Historical().timeseries.get_range(...)
                   ├─ _clean_1m(...)                   # tz-aware UTC index, dedupe, OHLC-only
                   └─ resample_ohlc(...) × 3           # -> 5m, 15m, 1h
