# Backtest Performance Optimization — EM Algo v4

## Overview

**Target:** `run_backtest()` in `strategy/EM_Algo_dev_v4.ipynb` — Cell 3  
**Problem:** Full backtest (1,777 trading days, ~2.36M 1-minute bars) ran for ~16 hours before crashing the kernel  
**Approach:** Profile first with `pyinstrument`, measure actual costs, then fix in order of impact  
**Result:** Projected full-run time reduced to ~33 minutes (extrapolated from 180-day partial profile)

---

## 1. Profiling Setup

### Tool Choice — `pyinstrument` over `cProfile`

| Feature | `cProfile` (deterministic) | `pyinstrument` (sampling) |
|---|---|---|
| How it works | Instruments every function call/return via C callback | Samples call stack on a timer (~1ms interval) |
| Overhead | Scales with call count — can be 2–10× on call-dense code | ~1–3% regardless of call count |
| Output | Flat table, sorted by cumulative time | Hierarchical call tree with % of total |
| Best for | Code with few, expensive calls | Code with many, cheap calls (pandas loops) |

`cProfile` was rejected because the hot path (`detect_zones`'s `.iloc[i]` row scanning) is extremely call-dense — instrumenting every call would have multiplied overhead unpredictably. `pyinstrument`'s sampling overhead is roughly constant regardless of call volume.

### Profiler Wrapper Pattern

Wrapped the `run_backtest(...)` invocation in a `try/except KeyboardInterrupt/finally` block so that:
- Interrupting via **Kernel → Interrupt** mid-run still triggers `finally` and writes the partial profile
- `trade_log_v5` is always defined (assigned `[]` on interrupt) so downstream Cell 4 doesn't raise `NameError`

```python
from pyinstrument import Profiler

profiler = Profiler()
profiler.start()
try:
    trade_log_v5 = run_backtest(df_1m_run, df_5m_run, df_15m_run, min_score=min_score)
except KeyboardInterrupt:
    trade_log_v5 = []
    print("Interrupted — partial profile from elapsed time.\n")
finally:
    profiler.stop()
    profiler.print(color=False, show_all=False)
    profiler.write_html("run_backtest_profile.html")
```

### Partial vs Full Run Toggle

```python
USE_PARTIAL = True   # flip to False for a full run

if USE_PARTIAL:
    cutoff_day = df_5m_ny.index.normalize().unique()[180]
    df_1m_run  = df_1m_ny[df_1m_ny.index.normalize()  <= cutoff_day]
    df_5m_run  = df_5m_ny[df_5m_ny.index.normalize()  <= cutoff_day]
    df_15m_run = df_15m_ny[df_15m_ny.index.normalize() <= cutoff_day]
else:
    df_1m_run, df_5m_run, df_15m_run = df_1m_ny, df_5m_ny, df_15m_ny
```

**Why the `else` branch matters:** variables must be defined regardless of which branch runs — the `run_backtest(...)` call uses `df_1m_run` / `df_5m_run` / `df_15m_run` in both cases. A `USE_PARTIAL = False` branch that referenced `cutoff_day` from the `if` block would raise `NameError`.

---

## 2. Profile Results (180-Day Partial Run)

```
Duration: 198.053s   CPU time: 181.133s   Samples: 182,914

run_backtest              197.790s  (99.9%)
├─ detect_zones           190.152s  (96.1%)  ← dominant cost
│  ├─ _iLocIndexer        148.699s  (75.2%)  ← .iloc[i] row construction
│  ├─ is_base_candle       24.531s  (12.4%)
│  │  └─ Series.__getitem__ 22.093s          ← row["range"], row["body_pct"]
│  ├─ wrapper               5.600s
│  └─ DataFrame.__getitem__ 4.726s
└─ DatetimeIndex.method     2.329s
```

**Key insight:** Everything outside `detect_zones` — all slicing, `classify_trend`, `set_curve`, `score_zone`, `simulate_trade_outcome_dual`, and all `df_1m_ny` queries — accounts for only **~5.3s, about 2.7%** of total runtime.

**Extrapolation:** 198s / 180 days × 1,777 days ≈ **~33 minutes** for the full dataset (vs. original ~16 hours — a ~30× improvement, primarily from the `.normalize()` caching fixes applied before this profile was captured).

---

## 3. Bottlenecks Identified

### Priority 1 — `detect_zones`: `.iloc[i]` Row-by-Row Scanning (96% of runtime)

**Location:** `Cell 2d`, `detect_zones()`, the `while i < n - 1` scanning loop  
**Root cause:** Every `bars.iloc[i]` call constructs a full `Series` object (index alignment, dtype inference, bounds checking), then `row["range"]`/`row["body_pct"]` does a label-based lookup on that Series — for what is ultimately a simple scalar comparison. This happens on (nearly) every bar in the window, for every day, for every call to `detect_zones`.

```python
# Slow — Series construction + label lookup on every row
row = bars.iloc[i]
if row["range"] <= avg_rng * range_multiplier:  # is_base_candle check
```

```python
# Fast — direct numpy scalar read
if ranges[i] <= avg_rng * range_multiplier:
```

### Priority 2 — Repeated `.index.normalize()` on Full Frames (pre-fix dominant cost)

**Location:** `Cell 3`, `run_backtest()`, per-day slicing block  
**Root cause:** `df_5m_ny.index.normalize()` was called 4 times per day iteration, `df_15m_ny.index.normalize()` 2 times — on the **full, un-sliced** 484,945-row and 161,766-row frames. Over ~1,777 days, that's 10,600+ full-frame normalize computations, each recomputing the same floor-to-midnight values on data that never changes between iterations.

Similarly, `df_1m_ny.index.normalize()` was called **inside the zone loop** (once per candidate zone) against the full 2,362,875-row frame.

---

## 4. Optimizations Applied

### A — `run_backtest`: Normalize Index Caching

**Where:** Top of `run_backtest()` function body, before the day loop  
**Change:** Compute `.normalize()` once per function call and cache as local variables

```python
# Added after trading_days = df_5m_ny.index.normalize().unique()
day_1m_norm  = df_1m_ny.index.normalize()
day_5m_norm  = df_5m_ny.index.normalize()
day_15m_norm = df_15m_ny.index.normalize()
```

**Why it works:** `df_1m_ny`, `df_5m_ny`, `df_15m_ny` are parameters — they're never mutated inside `run_backtest`. Their `.index` (and therefore `.index.normalize()`) is identical on every loop iteration. Caching eliminates redundant recomputation; the boolean comparisons (`day_5m_norm <= day`) are equally fast since they're vectorized comparisons either way.

**Why these live inside the function (not outside it):** The function accepts different DataFrames as parameters (e.g., the profiling slices `df_1m_prof` vs. the full `df_1m_ny`). Variables defined outside would reference the outer-scope `df_1m_ny`, creating a length mismatch with whatever smaller frame was passed in — causing `ValueError` or `IndexingError` in boolean indexing.

### B — `run_backtest`: Per-Day Slicing Uses Cached Arrays

```python
# Before
bars_5m_zones = df_5m_ny[(df_5m_ny.index.normalize() >= zone_lookback) &
                          (df_5m_ny.index.normalize() <= day)].copy()
bars_5m       = df_5m_ny[(df_5m_ny.index.normalize() >= lookback_start) &
                          (df_5m_ny.index.normalize() <= day)].copy()
bars_15m      = df_15m_ny[(df_15m_ny.index.normalize() >= lookback_start) &
                           (df_15m_ny.index.normalize() <= day)].copy()

# After
bars_5m_zones = df_5m_ny[(day_5m_norm >= zone_lookback) & (day_5m_norm <= day)].copy()
bars_5m       = df_5m_ny[(day_5m_norm >= lookback_start) & (day_5m_norm <= day)].copy()
bars_15m      = df_15m_ny[(day_15m_norm >= lookback_start) & (day_15m_norm <= day)].copy()
```

### C — `run_backtest`: `candidate_bars` Uses Cached Array

```python
# Before
candidate_bars = df_1m_ny[
    (df_1m_ny.index > signal_time) &
    (df_1m_ny.index.normalize() <= day)
].copy()

# After
candidate_bars = df_1m_ny[
    (df_1m_ny.index > signal_time) &
    (day_1m_norm <= day)
].copy()
```

### D — `run_backtest`: `future_bars` Derived from `candidate_bars`

**Proof that `future_bars ⊆ candidate_bars`:**
- `entry_bar` comes from `touch_bars.index[0]`, which is a row already inside `candidate_bars`
- Therefore `entry_bar > signal_time` ✓, `entry_bar.normalize() <= day` ✓, `entry_bar.hour >= 6` ✓
- Any row `r` with `r >= entry_bar` on the same calendar day (`entry_day`) satisfies all three — it's already in `candidate_bars`

```python
# Before — full 2.36M-row df_1m_ny scan
future_bars = df_1m_ny[
    (df_1m_ny.index >= entry_bar) &
    (df_1m_ny.index.normalize() == entry_day)
].copy()

# After — slice from the already-small candidate_bars
future_bars = candidate_bars[
    (candidate_bars.index >= entry_bar) &
    (candidate_bars.index.normalize() == entry_day)
].copy()
```

### E — `detect_zones` Stage 1: Extract Columns as NumPy Arrays

**Where:** `Cell 2d`, after `bars = df.reset_index(drop=True)`

```python
ranges     = bars["range"].to_numpy()
body_pcts  = bars["body_pct"].to_numpy()
highs      = bars["high"].to_numpy()
lows       = bars["low"].to_numpy()
directions = bars["direction"].to_numpy()
dts        = bars["_dt"].to_numpy()
```

`.to_numpy()` returns the underlying memory buffer as a raw `ndarray` — no index, no dtype wrapper, no per-access overhead. Indexing with `ranges[i]` is a direct memory read vs. `bars.iloc[i]["range"]`'s multi-step Series construction + label lookup.

### F — `detect_zones` Stage 2: Precomputed `is_base` Boolean Array

**Why fully vectorizable:** `is_base_candle`'s condition depends only on each row's own `range`/`body_pct` and the constant `avg_rng`. No dependency on anything that changes as the scan progresses — so the answer for row `i` is the same whether computed on-demand or all at once upfront.

```python
is_base = (
    (ranges != 0) &
    (ranges >= 1) &
    (body_pcts <= max_body_pct) &
    (ranges <= avg_rng * range_multiplier)
)
```

Call sites in the loop:
```python
# Before
row = bars.iloc[i]
if not is_base_candle(row, avg_rng, range_multiplier=range_multiplier):

# After
if not is_base[i]:
```
```python
# Before
if is_base_candle(bars.iloc[base_end + 1], avg_rng, range_multiplier=range_multiplier):

# After
if is_base[base_end + 1]:
```

### G — `detect_zones` Stage 3: NumPy Slicing for Base High/Low

```python
# Before
base_slice  = bars.iloc[base_start:base_end + 1]
base_high   = base_slice["high"].max()
base_low    = base_slice["low"].min()

# After
base_high = highs[base_start:base_end + 1].max()
base_low  = lows[base_start:base_end + 1].min()
```

NumPy slice is a zero-copy view; `.max()`/`.min()` run compiled C reductions — vs. constructing a sub-DataFrame and running pandas' generality-overhead versions on a few rows.

### H — `detect_zones` Stages 4–5: Eliminate `leg_in`/`leg_out` Row Objects

Stages 4 and 5 are coupled because `leg_in`/`leg_out` are shared across the validity check, the direction lookup, `formed_ts`, and the zone dict fields. Removing the `.iloc` calls requires converting **all** downstream references in the same pass.

```python
# Before — two .iloc calls + is_valid_leg with Series rows + direction label lookups
leg_in  = bars.iloc[base_start - 1]
leg_out = bars.iloc[base_end  + 1]

if not is_valid_leg(leg_in,  base_height, min_body_pct, min_range_ratio=min_range_ratio):
    i = base_end + 1; continue
if not is_valid_leg(leg_out, base_height, min_body_pct, min_range_ratio=min_range_ratio):
    i = base_end + 1; continue

lid = leg_in["direction"]
lod = leg_out["direction"]
...
formed_ts = str(bars.iloc[base_end]["_dt"])
...
"leg_in_range"  : round(leg_in["range"],  1),
"leg_out_range" : round(leg_out["range"], 1),

# After — all reads from precomputed numpy arrays
leg_in_idx, leg_out_idx = base_start - 1, base_end + 1

if not (body_pcts[leg_in_idx] >= min_body_pct and
        ranges[leg_in_idx]    >= base_height * min_range_ratio):
    i = base_end + 1; continue
if not (body_pcts[leg_out_idx] >= min_body_pct and
        ranges[leg_out_idx]    >= base_height * min_range_ratio):
    i = base_end + 1; continue

lid = directions[leg_in_idx]
lod = directions[leg_out_idx]
...
formed_ts = str(dts[base_end])
...
"leg_in_range"  : round(ranges[leg_in_idx],  1),
"leg_out_range" : round(ranges[leg_out_idx], 1),
```

---

## 5. Additional Fixes

### Bare `except:` → `except Exception:`

```python
# Before — swallows KeyboardInterrupt, SystemExit, and all other signals
try:
    curve_bias = set_curve(bars_15m)
    trend      = classify_trend(bars_5m)
except: continue

# After — catches real errors only; lets kernel interrupt propagate
except Exception: continue
```

A bare `except:` in Python catches **everything** — including `KeyboardInterrupt` (what fires when you press Kernel → Interrupt) and `SystemExit`. Inside a tight loop, this silently swallows the interrupt signal and just `continue`s to the next iteration, making it impossible to stop a long-running backtest cleanly.

---

## 6. Reference Dictionary — Classes, Methods, and Commands

Organized hierarchically from language level down to library internals.

---

### Python Language Level

| Construct | Type | Behavior |
|---|---|---|
| `if x:` | Control flow | Evaluates truthiness of `x` — for booleans, `True` enters the branch; no need to write `if x == True:` |
| `if x == True:` | Comparison | Redundant for booleans — tests equality rather than truthiness; flagged by linters (PEP 8 E712) |
| `if x = True:` | ❌ SyntaxError | `=` is assignment, not valid in a condition. Python 3.8+ has `:=` (walrus) for assignment-in-expression, but plain `=` is never valid here |
| `except:` | Bare except clause | Catches **all** exceptions including `KeyboardInterrupt`, `SystemExit`, `GeneratorExit` — avoid in production code |
| `except Exception:` | Exception clause | Catches all standard errors; allows system-level signals (`KeyboardInterrupt` etc.) to propagate |
| `except ExceptionType:` | Typed except clause | Catches only the named type and its subclasses |
| `arr[i]` | Subscript / `__getitem__` | On a Python list or numpy array: direct integer-position read |
| `#` | Comment | Everything after `#` on a line is ignored by the interpreter at runtime — does not execute |
| `set()` | Built-in type | Hash-based unordered collection; `in` lookups are O(1) average vs. O(n) for lists |
| `round(x, n)` | Built-in function | Rounds float `x` to `n` decimal places |
| `str(x)` | Built-in function | Converts `x` to its string representation |
| `len(x)` | Built-in function | Returns number of elements; O(1) for lists, dicts, sets, numpy arrays |

---

### pandas — `DataFrame`

| Method / Property | Returns | Notes |
|---|---|---|
| `df.copy()` | `DataFrame` | Deep copy — modifications to the copy don't affect the original |
| `df.reset_index(drop=True)` | `DataFrame` | Resets the index to 0, 1, 2… integers; `drop=True` discards the old index instead of moving it to a column |
| `df["col"]` | `Series` | Column selection by label — returns a view or copy depending on pandas internals |
| `df.iloc[i]` | `Series` | **Integer-positional** row access — constructs a new `Series` for that row (expensive in loops) |
| `df.iloc[start:end]` | `DataFrame` | Integer-positional row slice — constructs a sub-DataFrame |
| `df["col"].to_numpy()` | `ndarray` | Extracts raw numpy array from the column — bypasses all pandas overhead for subsequent index access |
| `df.index` | `Index` / `DatetimeIndex` | The row labels of the DataFrame |

---

### pandas — `Series`

| Method / Property | Returns | Notes |
|---|---|---|
| `s["label"]` / `s.__getitem__` | scalar | Label-based lookup — involves hash lookup into the index, dtype coercion |
| `s.rolling(n)` | `Rolling` | Creates a rolling-window object; no computation yet |
| `s.rolling(n).median()` | `Series` | Computes rolling median over `n` periods — returns a Series of same length |
| `s.rolling(n).median().iloc[-1]` | scalar | Last value of the rolling-median Series — a single float |
| `s.max()` / `s.min()` | scalar | Reduction over all elements |
| `s.to_numpy()` | `ndarray` | Extract underlying buffer; same as `df["col"].to_numpy()` |

---

### pandas — `DatetimeIndex`

| Method / Property | Returns | Notes |
|---|---|---|
| `.normalize()` | `DatetimeIndex` | Floors every timestamp to midnight (00:00:00) — day boundary for date-only comparisons. **Expensive on large indexes; cache the result if calling inside a loop.** |
| `.unique()` | `DatetimeIndex` | Deduplicated, sorted array of unique values |
| `.tz_convert(tz)` | `DatetimeIndex` | Converts timezone — requires the index to already be tz-aware |
| `.hour` | `ndarray` (int) | Integer hour component (0–23) of each timestamp — vectorized property |

---

### numpy — `ndarray`

| Operation | Behavior | Notes |
|---|---|---|
| `arr[i]` | scalar read | Direct memory offset — no object construction, no bounds-checking overhead beyond C |
| `arr[start:end]` | `ndarray` view | Zero-copy slice — shares memory with the original; no allocation |
| `arr.max()` / `arr.min()` | scalar | Compiled C reduction — faster than pandas' equivalent for small slices |
| `(arr > x)` | `ndarray` (bool) | Vectorized element-wise comparison — returns boolean array of same shape |
| `bool_a & bool_b` | `ndarray` (bool) | Element-wise AND across two boolean arrays — all in one compiled pass |
| `bool_a \| bool_b` | `ndarray` (bool) | Element-wise OR |
| `~bool_a` | `ndarray` (bool) | Element-wise NOT |
| `arr.to_numpy()` | — | Not applicable — `ndarray` already is the raw buffer; `.to_numpy()` is a pandas method |

---

### pyinstrument — `Profiler`

| Method | Behavior |
|---|---|
| `Profiler()` | Instantiate a new profiling session |
| `.start()` | Begin sampling the call stack (~every 1ms) |
| `.stop()` | Stop sampling; freeze the session data |
| `.print(color=False, show_all=False)` | Render hierarchical call tree to stdout. `color=False` suppresses ANSI escape codes in notebooks. `show_all=False` collapses library-internal frames for a more readable summary; the full data is still captured and visible in the HTML report |
| `.write_html(path)` | Write interactive, collapsible call-tree report to an HTML file — open in a browser to explore |

**Sampling vs. deterministic:** `pyinstrument` checks "what is currently executing?" on a timer, rather than hooking every function entry/exit. This means its overhead is roughly constant (~1–3%) regardless of how many function calls your code makes — unlike `cProfile`, which pays a fixed cost per call/return event and can multiply runtimes by 5–20× on call-dense code.

---

### Pandas `.iloc` vs. NumPy Array Access — Side-by-Side

| | `bars.iloc[i]["range"]` | `ranges[i]` |
|---|---|---|
| **What it does** | Constructs a `Series` for row `i`, then does a label lookup for `"range"` | Reads a scalar from position `i` in a pre-extracted C array |
| **Allocation** | New `Series` object + Index object per call | None |
| **Index alignment** | Performed on every access | Not needed — positional by construction |
| **Typical cost** | ~10–100 µs per call in a loop | ~10–100 ns per call |
| **Profile signature** | `_iLocIndexer.__getitem__`, `Series.__getitem__` | No pandas frames in the call tree |
| **When to use** | Interactive exploration, one-off access outside loops | Any hot loop over rows |

---

*Generated during optimization case study — EM Algo v4, June 2026*
