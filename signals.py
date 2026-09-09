# signals.py — MYM Live Signal Runner (v7-aligned)
# ---------------------------------------------------------------
# Runs ONE detection cycle: load data -> metrics -> context ->
# detect zones -> score -> calculate_sets -> send_alert (email +
# Discord + TradersPost).
#
# STRATEGY FUNCTIONS ARE PORTED 1:1 FROM strategy.py (the v6 build
# that produced the 607-trade / PF 6.08 backtest). Any change to the
# strategy belongs in strategy.py FIRST, re-backtested, then ported
# here — never edited here alone.
#
# Run (from em_algo root):  python -m etrade_stage.signals
# ---------------------------------------------------------------
from __future__ import annotations

import os
import sys
from datetime import datetime

import pandas as pd
import pytz
import numpy as np
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config   # no try/except around this — let it fail loudly
print("config imported OK from:", config.__file__)

from alerts import send_alert
import json

# -- OPTIONAL PERSISTENT DEDUP (currently UNUSED) --------------
# Support for the optional intraday re-entry hook in run_signal_check.
# Dedup is delegated to TradersPost by default; these are staged for when
# you decide to also block same-day re-entry after a position closes.
# -- ZONE SENT LOGGING--------------------------
SENT_ZONES_PATH = os.path.join(os.path.dirname(config.LOG_FILE), "sent_zones.json")

# For zones that have been fired already
def _load_sent():
    try:
        with open(SENT_ZONES_PATH) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()   # missing/corrupt file -> treat as nothing sent yet

def _save_sent(sent):
    os.makedirs(os.path.dirname(SENT_ZONES_PATH), exist_ok=True)
    with open(SENT_ZONES_PATH, "w") as f:
        json.dump(sorted(sent), f)
        
# ── LOGGING ───────────────────────────────────
def log(msg):
    ts = datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)
    with open(config.LOG_FILE, "a") as f:
        f.write(line + "\n")
        
# ── DATA LOADING (Databento snapshot: Historical + Live stitch) ──
def load_from_databento():
    """Batch snapshot. Returns (df_1m, df_5m, df_15m, df_1h) — df_1m is needed
    for the close-through entry check that mirrors the backtest."""
    from data_feed import load_all_timeframes
    df_1m, df_5m, df_15m, df_1h = load_all_timeframes()   # 4-tuple
    log(f"Databento snapshot | 1m:{len(df_1m)} 5m:{len(df_5m)} 15m:{len(df_15m)} 1h:{len(df_1h)}")
    return df_1m, df_5m, df_15m, df_1h

# ── CANDLE METRICS & SESSION (identical to strategy.py) ──
# Create candle metric fields
# Cell 2a — Strategy Functions | CANDLE METRICS & SESSION  (== signals.py)
def add_candle_metrics(df):
    df = df.copy()
    df["range"]     = df["high"] - df["low"]
    df["body"]      = abs(df["close"] - df["open"])
    df["body_pct"]  = (df["body"] / df["range"] * 100).round(1)
    df["direction"] = df.apply(
        lambda row: "BULL" if row["close"] > row["open"] else "BEAR", axis=1
    )
    return df
    
# Create session filter field
def add_session(df):
    df = df.copy()
    df.index = df.index.tz_convert("US/Eastern")
    def get_session(ts):
        hour = ts.hour
        if 7 <= hour < 9: return "NY_PreMarket"
        elif 9 <= hour < 13:  return "NY_AM"
        elif 13 <= hour < 17 : return "NY_PM"    
        elif 3 <= hour < 7: return "LONDON"
        else:               return "ASIA"
    df["session"] = df.index.map(get_session)
    return df


# Cell 2b — Strategy Functions | MARKET CONTEXT  (== signals.py)
def set_curve(df_1h, lookback=config.ZONE_LOOKBACK_DAYS):
    recent        = df_1h.tail(lookback)
    highest_high  = recent["high"].max()
    lowest_low    = recent["low"].min()
    total_range   = highest_high - lowest_low
    upper_third   = highest_high - (total_range * 0.3333)
    lower_third   = lowest_low   + (total_range * 0.3333)
    current_price = df_1h["close"].iloc[-1]
    if current_price >= upper_third:   return "RETAIL"
    elif current_price <= lower_third: return "WHOLESALE"
    else:                              return "NEUTRAL"

def classify_trend(df_15m, window=config.TREND_WINDOW):
    if len(df_15m) < window * 2: return "SIDEWAYS"
    rh   = df_15m["high"].rolling(window).max()
    rl   = df_15m["low"].rolling(window).min()
    ph   = rh.shift(window)
    pl   = rl.shift(window)
    last = df_15m.index[-1]
    if rh[last] > ph[last] and rl[last] > pl[last]:   return "UPTREND"
    elif rh[last] < ph[last] and rl[last] < pl[last]: return "DOWNTREND"
    else:                                             return "SIDEWAYS"
    
# ── Base Candle & Leg Candle HELPERS FUNCTIONS ───────────────────────────────────────────────────
def is_base_candle(row, avg_range, max_body_pct=config.MAX_BODY_PCT, range_multiplier=config.RANGE_MULTIPLIER):
    if row["range"] == 0: return False
    if row["range"] < 1:  return False
    return (row["body_pct"] <= max_body_pct and
            row["range"]    <= avg_range * range_multiplier)

def is_valid_leg(candle, base_height, min_body_pct=config.MIN_BODY_PCT, min_range_ratio=config.MIN_RANGE_RATIO):
    if base_height == 0: return False
    return (candle["body_pct"] >= min_body_pct and
            candle["range"]    >= base_height * min_range_ratio)

# ── TAP COUNTER ───────────────────────────────
def _count_taps(price_series, level, side):
    """Distinct returns to `level` after the zone formed. side='below' for DBR
    (low <= level), 'above' for RBD (high >= level). Consecutive in-zone bars
    count as ONE tap — price must leave and return for the next."""
    if price_series.size == 0:
        return 0
    in_zone = (price_series <= level) if side == "below" else (price_series >= level)
    prev = np.concatenate(([False], in_zone[:-1]))
    return int(np.sum(in_zone & ~prev))

# ── ZONE DETECTION ────────────────────────────
def detect_zones(df, max_base_candles=config.MAX_BASE_CANDLES, min_body_pct=config.MIN_BODY_PCT,
                 range_multiplier=config.RANGE_MULTIPLIER, min_zone_height=config.MIN_ZONE_HEIGHT,
                 min_range_ratio=config.MIN_RANGE_RATIO,max_body_pct=config.MAX_BODY_PCT):
    zones = []
    df          = df.copy()
    df["_dt"]   = df.index
    bars        = df.reset_index(drop=True)
    n           = len(bars)
    ranges      = bars["range"].to_numpy()
    body_pcts   = bars["body_pct"].to_numpy()
    highs       = bars["high"].to_numpy()
    lows        = bars["low"].to_numpy()
    directions  = bars["direction"].to_numpy()
    dts         = bars["_dt"].to_numpy()

    dynamic_min_zone_height = config.ZONE_HEIGHT_K * df["range"].rolling(20).std().iloc[-1]
    min_zone_height = min(min_zone_height, dynamic_min_zone_height)
    avg_rng = df["range"].rolling(20).median().iloc[-1]

    is_base = ((ranges != 0) & (ranges >= 1) &
               (body_pcts <= max_body_pct) &
               (ranges <= avg_rng * range_multiplier))

    i = 2
    while i < n - 1:
        if not is_base[i]:
            i += 1; continue
        base_start = base_end = i
        while base_end + 1 < n - 1:
            if is_base[base_end + 1]: base_end += 1
            else: break
        base_count = base_end - base_start + 1
        if base_count > max_base_candles:
            i = base_end + 1; continue
        base_high   = highs[base_start:base_end + 1].max()
        base_low    = lows[base_start:base_end + 1].min()
        base_height = base_high - base_low
        if base_height < min_zone_height:
            i = base_end + 1; continue
        leg_in_idx  = base_start - 1
        leg_out_idx = base_end + 1
        if not (body_pcts[leg_in_idx] >= min_body_pct and
                ranges[leg_in_idx]    >= base_height * min_range_ratio):
            i = base_end + 1; continue
        if not (body_pcts[leg_out_idx] >= min_body_pct and
                ranges[leg_out_idx]    >= base_height * min_range_ratio):
            i = base_end + 1; continue
        lid = directions[leg_in_idx]; lod = directions[leg_out_idx]
        if   lid == "BEAR" and lod == "BULL": zone_type = "DBR"
        elif lid == "BULL" and lod == "BEAR": zone_type = "RBD"
        else:
            i = base_end + 1; continue
        # ── Bug 2 fix: use _dt column captured before reset_index() ──
        formed_ts = str(dts[base_end])
        
        # count taps: returns to the proximal line after the zone was established
        prox = base_high if zone_type == "DBR" else base_low
        side = "below" if zone_type == "DBR" else "above"
        post = (lows if zone_type == "DBR" else highs)[leg_out_idx + 1:]
        touched = _count_taps(post, prox, side)
        
        zones.append({
            "type"          : zone_type,
            "proximal"      : round(base_high if zone_type == "DBR" else base_low,  1),
            "distal"        : round(base_low  if zone_type == "DBR" else base_high, 1),
            "base_high"     : round(base_high,        1),
            "base_low"      : round(base_low,         1),
            "base_height"   : round(base_height,      1),
            "base_count"    : base_count,
            "leg_in_range"  : round(ranges[leg_in_idx],  1),
            "leg_out_range" : round(ranges[leg_out_idx], 1),
            "formed_at"     : formed_ts,
            "touched"       : touched,
            "active"        : True,
        })
        i = base_end + 2
    return zones

# ── ZONE ALLOW/NO-TRADE GATE (curve x trend hard veto) ──────────
# Direct table lookup from the strategy's curve/trend decision matrix.
# zone_type is already fixed by detect_zones's candle shape (RBD = Supply,
# DBR = Demand) — this gate never changes direction, only allow/forbid.
# Does NOT encode the matrix's "Minimum 2:1 RR" notes — those tighten the
# RR floor on an already-allowed trade and belong in calculate_sets, not
# here; this table only answers "is this zone eligible for consideration
# at all, given current curve + trend."
_ZONE_GATE = {
    # (zone_type, curve_bias, trend): allowed?
    ("RBD", "RETAIL",    "DOWNTREND"): True,
    ("RBD", "RETAIL",    "SIDEWAYS"):  True,
    ("RBD", "RETAIL",    "UPTREND"):   True,
    ("RBD", "NEUTRAL",   "DOWNTREND"): True,
    ("RBD", "NEUTRAL",   "SIDEWAYS"):  True,
    ("RBD", "NEUTRAL",   "UPTREND"):   False,
    ("RBD", "WHOLESALE", "DOWNTREND"): True,
    ("RBD", "WHOLESALE", "SIDEWAYS"):  False,
    ("RBD", "WHOLESALE", "UPTREND"):   False,

    ("DBR", "RETAIL",    "DOWNTREND"): False,
    ("DBR", "RETAIL",    "SIDEWAYS"):  False,
    ("DBR", "RETAIL",    "UPTREND"):   True,
    ("DBR", "NEUTRAL",   "DOWNTREND"): False,
    ("DBR", "NEUTRAL",   "SIDEWAYS"):  True,
    ("DBR", "NEUTRAL",   "UPTREND"):   True,
    ("DBR", "WHOLESALE", "DOWNTREND"): True,
    ("DBR", "WHOLESALE", "SIDEWAYS"):  True,
    ("DBR", "WHOLESALE", "UPTREND"):   True,
}

def zone_allowed(zone_type, curve_bias, trend):
    """Hard pass/fail gate from the curve x trend decision matrix.
    zone_type: "RBD" (Supply) or "DBR" (Demand) — from detect_zones.
    curve_bias: "RETAIL"/"NEUTRAL"/"WHOLESALE" — from set_curve.
    trend: "UPTREND"/"SIDEWAYS"/"DOWNTREND" — from classify_trend.
    Raises on an unrecognized combination rather than silently allowing
    or blocking — a new curve_bias/trend value should fail loudly, not
    fall through to a guessed default."""
    key = (zone_type, curve_bias, trend)
    if key not in _ZONE_GATE:
        raise ValueError(f"No gate entry for {key}")
    return _ZONE_GATE[key]

# Cell 2e — Strategy Functions | SCORING  (== signals.py)
def score_zone(zone, trend, curve_bias, all_zones):
    score = 0
    move_ratio = zone["leg_out_range"] / zone["base_height"]
    score += 2 if move_ratio >= 2 else 1 if move_ratio >= 1.5 else 0
    score += 1 if zone["base_count"] <= 3 else 0.5 if zone["base_count"] <= 6 else 0
    score += 2 if zone["touched"] == 0 else 1 if zone["touched"] == 1 else 0
    if (zone["type"] == "DBR" and trend == "UPTREND") or \
       (zone["type"] == "RBD" and trend == "DOWNTREND"):
        score += 1
    if (zone["type"] == "DBR" and curve_bias == "WHOLESALE") or \
       (zone["type"] == "RBD" and curve_bias == "RETAIL"):
        score += 2
    elif curve_bias == "NEUTRAL":
        score += 1
    opposing = [z for z in all_zones if z["type"] != zone["type"] and z["active"]]
    if zone["type"] == "DBR":
        above = [z for z in opposing if z["proximal"] > zone["proximal"]]
        if above:
            target = min(above, key=lambda z: z["proximal"])["proximal"]
            risk   = abs(zone["proximal"] - zone["distal"])
            rr     = abs(target - zone["proximal"]) / risk if risk > 0 else 0
            score += 2 if rr >= 5 else 1 if rr >= 3 else 0
    else:
        below = [z for z in opposing if z["proximal"] < zone["proximal"]]
        if below:
            target = max(below, key=lambda z: z["proximal"])["proximal"]
            risk   = abs(zone["proximal"] - zone["distal"])
            rr     = abs(zone["proximal"] - target) / risk if risk > 0 else 0
            score += 2 if rr >= 5 else 1 if rr >= 3 else 0
    return round(score, 1)

def calculate_sets(zone, all_zones, account_size=config.ACCOUNT_SIZE,
                   risk_pct=config.RISK_PCT, tick_value=config.TICK_VALUE,
                   stop_buffer=config.STOP_BUFFER, t2_cap=config.T2_CAP):
    if zone["type"] == "DBR":
        entry = zone["base_high"]; stop = zone["base_low"] - stop_buffer
    else:
        entry = zone["base_low"];  stop = zone["base_high"] + stop_buffer
    stop_points = abs(entry - stop)
    if stop_points == 0: return None
    direction = 1 if zone["type"] == "DBR" else -1
    max_risk_dollars  = account_size * risk_pct
    risk_per_contract = stop_points * tick_value
    contracts = int(max_risk_dollars / risk_per_contract) if risk_per_contract > 0 else 0
    if contracts < 1: return None
    if zone["type"] == "DBR":
        opposing = [z for z in all_zones if z["type"] == "RBD" and z["proximal"] > entry and z["active"]]
        raw_target = min(opposing, key=lambda z: z["proximal"])["proximal"] if opposing else None
    else:
        opposing = [z for z in all_zones if z["type"] == "DBR" and z["proximal"] < entry and z["active"]]
        raw_target = max(opposing, key=lambda z: z["proximal"])["proximal"] if opposing else None
    if raw_target is None: return None
    raw_target_pts = abs(raw_target - entry)
    rr_raw = round(raw_target_pts / stop_points, 2)
    if rr_raw < 0.5:
        return None
    elif rr_raw <= 1.1:
        t1 = raw_target 
        t2 = None
        t1_points = raw_target_pts
        t2_points = None
        rr_t1 = rr_raw 
        rr_t2 = None
        contracts_t1 = contracts 
        contracts_t2 = None
    elif rr_raw <= 2.0:
        t1 = round(entry + direction * stop_points * 1.0, 1)
        cap_price = round(entry + direction * stop_points * t2_cap, 1)
        t2 = round(min(raw_target, cap_price), 1) if direction == 1 else round(max(raw_target, cap_price), 1)
        t1_points = round(abs(t1 - entry), 1)
        t2_points = round(abs(t2 - entry), 1)
        rr_t1 = round(t1_points / stop_points, 2) 
        rr_t2 = round(t2_points / stop_points, 2)
        contracts_t1 = max(1, contracts // 2)
        contracts_t2 = contracts - contracts_t1
    else:
        t1 = round(entry + direction * stop_points * 1.5, 1)
        cap_price = round(entry + direction * stop_points * t2_cap, 1)
        t2 = round(min(raw_target, cap_price), 1) if direction == 1 else round(max(raw_target, cap_price), 1)
        t1_points = round(abs(t1 - entry), 1)
        t2_points = round(abs(t2 - entry), 1)
        rr_t1 = round(t1_points / stop_points, 2) 
        rr_t2 = round(t2_points / stop_points, 2)
        contracts_t1 = max(1, contracts // 2)
        contracts_t2 = contracts - contracts_t1 if contracts >= 2 else 0
    return {
        "zone_type": zone["type"], 
        "formed_at": zone["formed_at"], 
        "entry": entry, 
        "stop": stop,
        "t1": t1, 
        "t2": t2, 
        "raw_target": raw_target,
        "rr_raw": rr_raw,
        "stop_points": round(stop_points, 1), 
        "t1_points": t1_points, 
        "t2_points": t2_points,
        "rr_t1": rr_t1, 
        "rr_t2": rr_t2, 
        "contracts": contracts,
        "contracts_t1": contracts_t1, 
        "contracts_t2": contracts_t2,
        "risk_per_contract_$": round(risk_per_contract, 2),
        "total_risk_$": round(contracts * risk_per_contract, 2),
        "potential_t1_$": round(contracts_t1 * t1_points * tick_value, 2),
        "potential_t2_$": round(contracts_t2 * t2_points * tick_value, 2) if contracts_t2 is not None else 0
        }
    
print("add_candle_metrics / add_session loaded ✅")
print("set_curve and classify_trend loaded ✅")
print("base/leg helpers loaded ✅")
print("detect_zones loaded ✅")
print("score_zone loaded ✅")
print("calculate_sets loaded ✅")

# ── MAIN SIGNAL CHECK ─────────────────────────
def run_signal_check():
    log("=" * 62)
    log("Running signal check (v7 — backtest-mirrored, close-through entry)...")

    try:
        df_1m, df_5m, df_15m, df_1h = load_from_databento()
    except Exception as e:
        log(f"Data load failed: {e}")
        return

    # metrics + session on every frame (df_1m included — it drives the entry check)
    df_1m  = add_session(add_candle_metrics(df_1m))
    df_15m = add_session(add_candle_metrics(df_15m))
    df_1h  = add_session(add_candle_metrics(df_1h))

    ny = ["NY_AM", "NY_PM", "NY_PreMarket"]
    df_1m_ny  = df_1m[df_1m["session"].isin(ny)].copy()
    df_15m_ny = df_15m[df_15m["session"].isin(ny)].copy()
    df_1h_ny  = df_1h[df_1h["session"].isin(ny)].copy()

    if len(df_15m_ny) < 50 or len(df_1h_ny) < config.ZONE_LOOKBACK_DAYS or len(df_1m_ny) == 0:
        log("Not enough NY-session bars — aborting run.")
        return

    # ── windows: mirror Cell 12 EXACTLY (note the backtest's naming is preserved
    #    as-is — ZONE_LOOKBACK_DAYS feeds trend/curve, TREND_WINDOW feeds zones —
    #    to stay faithful to the validated backtest, not "corrected"). ──
    today          = df_15m_ny.index.max().normalize()
    lookback_start = today - pd.Timedelta(days=config.ZONE_LOOKBACK_DAYS)   # trend/curve ctx
    zone_lookback  = today - pd.Timedelta(days=config.TREND_WINDOW)         # zone window

    d15 = df_15m_ny.index.normalize()
    d1h = df_1h_ny.index.normalize()
    bars_15m_zones = df_15m_ny[(d15 >= zone_lookback)  & (d15 <= today)]
    bars_15m       = df_15m_ny[(d15 >= lookback_start) & (d15 <= today)]
    bars_1h        = df_1h_ny[(d1h >= lookback_start)  & (d1h <= today)]
    
    if len(bars_15m_zones) < 50 or len(bars_15m) < 20 or len(bars_1h) < 50:
        log("Warm-up windows too small this run — aborting.")
        return

    stop_buffer = int(min(df_15m_ny["range"].rolling(20).median().iloc[-1] / 2,
                            config.MIN_ZONE_HEIGHT * 0.7))
    # σ-band (KEPT — live-only gate the backtest lacks)
    # Entry proximity band: keep only signals whose entry is within
    # ENTRY_SIGMA std devs of the 14-bar range, measured from current price.
    range_std     = df_15m_ny["range"].rolling(config.ENTRY_SIGMA_WINDOW).std().iloc[-1]
    # Schwab quote snapshot was a workaround for Databento Historical's data lag
    # before pull_via_live_replay (data_feed.py) closed most of that gap. Sunset —
    # df_1m is now current enough that its close IS the live price.
    current_price = float(df_1m_ny["close"].iloc[-1])
    log(f"Live price (Databento): {current_price} @ {df_1m_ny.index[-1]}")

    entry_band    = config.ENTRY_SIGMA * range_std
    log("=" * 62)
    
    if pd.isna(range_std) or range_std == 0:
        log(f"ENTRY BAND | range_std={range_std} -> GATE DISABLED (fails open)")
    else:
        log(f"ENTRY BAND | {config.ENTRY_SIGMA}sigma x {range_std:.2f} = +/-{entry_band:.1f} pts")
        log(f"ENTRY BAND | price {current_price:.1f} -> "
            f"[{current_price-entry_band:.1f}, {current_price+entry_band:.1f}]")
    log("=" * 62)

    try:
        curve_bias = set_curve(bars_1h)          # backtest passes the windowed frame
        trend      = classify_trend(bars_15m)
    except Exception as e:
        log(f"Context failed: {e}")
        return
    log(f"Trend: {trend} | Curve: {curve_bias}")

    zones = detect_zones(bars_15m_zones)
    log(f"Zones detected (last {config.TREND_WINDOW}d): {len(zones)}")

    # ── entry-search context: the latest closed 1m bar (mirrors backtest's
    #    close-through on df_1m; here it's the single most recent bar). ──
    last_1m_close = current_price                       # live price drives close-through
    now_et = datetime.now(pytz.timezone(config.TIMEZONE))
    entry_window_ok = config.ENTRY_HOUR_MIN <= now_et.hour < config.CUTOFF_HOUR

    log("-" * 62)
    sent = skipped_score = skipped_taps = skipped_sets = skipped_gate = 0
    skipped_band = skipped_bracket = skipped_notouch = skipped_cutoff = skipped_stopbreach = 0

    for zone in zones:
        # ── DEDUP: delegated to TradersPost ("Allow Add to Position" = OFF, confirmed).
        #    While a position is open, duplicate entries are rejected at the sink.
        #    OPTIONAL HOOK — if you later need to block intraday RE-ENTRY after a
        #    position closes (the one case TP's guard misses), add a persistent
        #    sent-zone check here, mirroring the backtest's used_zones:
        #        zone_id = f"{zone['type']}_{zone['proximal']}_{zone['formed_at']}"
        #        if zone_id in load_sent(): continue        # load_sent() from disk
        #    ...and record zone_id after a successful send. Not active now.

        if not zone_allowed(zone["type"], curve_bias, trend):
            score = "gate"
            disp = f"SKIP gate ({curve_bias}/{trend} forbids {zone['type']})"
            skipped_gate += 1
        else:
            score = score_zone(zone, trend, curve_bias, zones)
            sets  = calculate_sets(zone, zones, stop_buffer=stop_buffer) \
                    if score >= config.MIN_SCORE else None
            e     = sets["entry"] if sets else None
            dist  = abs(e - current_price) if sets else None

            if score < config.MIN_SCORE:
                disp = f"SKIP score {score} < {config.MIN_SCORE}"; skipped_score += 1
            elif zone["touched"] >= config.MAX_TAPS:
                disp = f"SKIP tapped {zone['touched']}x"; skipped_taps += 1
            elif sets is None:
                disp = "SKIP no valid sets"; skipped_sets += 1
            elif pd.notna(range_std) and range_std > 0 and dist > entry_band:
                disp = f"SKIP {dist:.0f} pts > {entry_band:.0f} band"; skipped_band += 1
            else:
                s, t1, t2 = sets["stop"], sets["t1"], sets["t2"]
                if zone["type"] == "DBR":
                    bracket_ok = (s < e < t1) and (t2 is None or e < t2)
                else:
                    bracket_ok = (t1 < e < s) and (t2 is None or t2 < e)
                if not bracket_ok:
                    disp = f"!! MALFORMED BRACKET stop={s} entry={e} t1={t1} t2={t2}"
                    skipped_bracket += 1
                elif (current_price <= sets["stop"]) if zone["type"] == "DBR" \
                     else (current_price >= sets["stop"]):
                    disp = f"SKIP price {current_price} past stop {sets['stop']}"
                    skipped_stopbreach += 1
                elif not entry_window_ok:
                    disp = f"SKIP outside entry window (hour {now_et.hour})"
                    skipped_cutoff += 1
                else:
                    # close-through: has the latest 1m bar closed through entry?
                    through = (last_1m_close >= e) if zone["type"] == "DBR" \
                              else (last_1m_close <= e)
                    if not through:
                        disp = f"SKIP no close-through (1m {last_1m_close} vs entry {e})"
                        skipped_notouch += 1
                    else:
                        disp = f"SEND {sets['contracts']}c risk ${sets['total_risk_$']} rr_t1 {sets['rr_t1']}"
                        sent += 1

        log(f"  {zone['type']} prox={zone['proximal']:<9} h={zone['base_height']:<6} "
            f"base={zone['base_count']} touch={zone['touched']} score={score:<5} "
            f"formed={zone['formed_at']} | {disp}")
     
        if not disp.startswith("SEND"):
            continue

        alert_data = {
            "type"        : zone["type"],
            "entry"       : sets["entry"],
            "stop"        : sets["stop"],
            "t1"          : sets["t1"],
            "t2"          : sets["t2"],
            "stop_points" : sets["stop_points"],
            "rr_t1"       : sets["rr_t1"],
            "rr_t2"       : sets["rr_t2"],
            "t1_points"   : sets["t1_points"],
            "t2_points"   : sets["t2_points"],
            "contracts"   : sets["contracts"],
            "score"       : score,
            "untouched?"  : zone["touched"],
            "formed_at"   : zone["formed_at"],
            "total_risk"  : sets["total_risk_$"],
            "potential_t1": sets["potential_t1_$"],
            "potential_t2": sets["potential_t2_$"],
        }
        result = send_alert(alert_data)   # test_mode default=True in alerts.py
        log(f"    TradersPost: ok={result.ok} id={result.signal_id} detail={result.detail}")

    log("-" * 62)
    if skipped_bracket:
        log(f"  !!!! {skipped_bracket} MALFORMED BRACKET(S) — CHECK calculate_sets !!!!")
    if sent == 0:
        log("No qualifying signals found this run.")
    log(f"Done. {sent} sent | {skipped_gate} gate | {skipped_score} score | {skipped_taps} taps | "
    f"{skipped_sets} no-sets | {skipped_band} band | {skipped_bracket} bracket | "
    f"{skipped_notouch} no-through | {skipped_cutoff} window | {skipped_stopbreach} past-stop")
    log("=" * 62)

if __name__ == "__main__":
    run_signal_check()