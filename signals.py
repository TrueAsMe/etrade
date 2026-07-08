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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config   # no try/except around this — let it fail loudly
print("config imported OK from:", config.__file__)

try:                                    # package run: python -m etrade_stage.signals
    from alerts import send_alert
except ImportError:                     # direct run from inside etrade_stage/
    from alerts import send_alert

# ── LOGGING ───────────────────────────────────
def log(msg):
    ts = datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)
    with open(config.LOG_FILE, "a") as f:
        f.write(line + "\n")

# ── DATA LOADING (active: databento) ───────────
def load_from_databento():
    """Live MYM=F bars via Databento (free, 15-min delay)."""
    from data_feed import load_all_timeframes
    df_5m, df_15m, df_1h = load_all_timeframes()
    log("Live data loaded from Databento (15min delay)")
    return df_5m, df_15m, df_1h

# ── CANDLE METRICS & SESSION (identical to strategy.py) ──
# Create candle metric fields
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

# ── MARKET CONTEXT (v6: curve on 15m/60, trend on 5m/16) ──
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

def is_base_candle(row, avg_range, max_body_pct=35, range_multiplier=0.36):
    if row["range"] == 0: return False
    if row["range"] < 1:  return False
    return (row["body_pct"] <= max_body_pct and
            row["range"]    <= avg_range * range_multiplier)

def is_valid_leg(candle, base_height, min_body_pct=42, min_range_ratio=0.631):
    if base_height == 0: return False
    return (candle["body_pct"] >= min_body_pct and
            candle["range"]    >= base_height * min_range_ratio)
    
# ── ZONE DETECTION (v6 vectorized; floats coerced for JSON) ──
def detect_zones(df, max_base_candles=6, min_body_pct=42,
                 range_multiplier=0.36, min_zone_height=13,
                 min_range_ratio=0.631):
    max_body_pct = 35
    zones = []
    # ── Bug 2 fix: capture datetime index before reset_index() ──
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
    
    # ── Rolling median — robust against log-normal range distribution ──
    avg_rng = df["range"].rolling(50).median().iloc[-1]

    is_base = (
        (ranges != 0) &
        (ranges >= 1) &
        (body_pcts <= max_body_pct) &
        (ranges <= avg_rng * range_multiplier)
    )
    
    # ── Bug 3 fix: start at i=2 — need at least 1 bar before base_start ──
    i = 2
    while i < n - 1:
        if not is_base[i]:
            i += 1; continue

        base_start = base_end = i
        while base_end + 1 < n - 1:
            if is_base[base_end + 1]:
                base_end += 1
            else:
                break

        base_count  = base_end - base_start + 1
        if base_count > max_base_candles:
            i = base_end + 1; continue

        base_high    = highs[base_start:base_end + 1].max()
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

        lid = directions[leg_in_idx]
        lod = directions[leg_out_idx]

        if   lid == "BEAR" and lod == "BULL": zone_type = "DBR"
        elif lid == "BULL" and lod == "BEAR": zone_type = "RBD"
        else:
            i = base_end + 1; continue

        # ── Bug 2 fix: use _dt column captured before reset_index() ──
        formed_ts = str(dts[base_end])

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
            "touched"       : 0,
            "active"        : True
        })

        i = base_end + 2

    return zones

print("detect_zones loaded ✅")


# ── SCORING (identical in strategy.py and here — verified) ──
def score_zone(zone, trend, curve_bias, all_zones):
    score = 0
    # Enhancer 1 — Strength
    move_ratio = zone["leg_out_range"] / zone["base_height"]
    score += 2 if move_ratio >= 2 else 1 if move_ratio >= 1.5 else 0
    
    # Enhancer 2 — Time
    score += 1 if zone["base_count"] <= 3 else 0.5 if zone["base_count"] <= 6 else 0
    
    # Enhancer 3 — Freshness
    score += 2 if zone["touched"] == 0 else 1 if zone["touched"] == 1 else 0
    
    # Enhancer 4 — Trend Alignment
    if (zone["type"] == "DBR" and trend == "UPTREND") or \
       (zone["type"] == "RBD" and trend == "DOWNTREND"):
        score += 1
    else:
        score += 0    # no points   
        
    # Enhancer 5 — Curve Location (Upper/Lower Thirds)
    if (zone["type"] == "DBR" and curve_bias == "WHOLESALE") or \
       (zone["type"] == "RBD" and curve_bias == "RETAIL"):
        score += 2    # aligned with curve — full points
    elif curve_bias == "NEUTRAL":
        score += 1    # neutral zone — partial credit
    else:
        score += 0    # counter-curve — no points (was giving 1 before)
        
    # Enhancer 6 — R:R to nearest opposing zone
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

print("score_zone loaded ✅")

# ── ORDER CALCULATION (v6 calculate_sets, ported 1:1) ──
def calculate_sets(zone, all_zones, account_size=50000,
                   risk_pct=0.005, tick_value=0.50, stop_buffer=config.STOP_BUFFER,t2_cap=2.5):

    # ── ENTRY & STOP ─────────────────────────────────────────────
    if zone["type"] == "DBR":
        entry = zone["base_high"]
        stop  = zone["base_low"] - stop_buffer
    else:
        entry = zone["base_low"]
        stop  = zone["base_high"] + stop_buffer

    stop_points = abs(entry - stop)
    if stop_points == 0: return None

    direction = 1 if zone["type"] == "DBR" else -1

    # ── POSITION SIZING ──────────────────────────────────────────
    max_risk_dollars  = account_size * risk_pct
    risk_per_contract = stop_points * tick_value
    contracts         = int(max_risk_dollars / risk_per_contract) if risk_per_contract > 0 else 0
    if contracts < 1: return None

    # ── NEAREST OPPOSING ZONE ────────────────────────────────────
    if zone["type"] == "DBR":
        opposing   = [z for z in all_zones
                      if z["type"] == "RBD" and z["proximal"] > entry and z["active"]]
        raw_target = min(opposing, key=lambda z: z["proximal"])["proximal"] if opposing else None
    else:
        opposing   = [z for z in all_zones
                      if z["type"] == "DBR" and z["proximal"] < entry and z["active"]]
        raw_target = max(opposing, key=lambda z: z["proximal"])["proximal"] if opposing else None

    if raw_target is None: return None

    raw_target_pts = abs(raw_target - entry)
    rr_raw         = round(raw_target_pts / stop_points, 2)
    
    # ── TWO-TARGET LOGIC ─────────────────────────────────────────
    if rr_raw < 0.75:
        # Reject — insufficient R:R
        return None
    elif rr_raw <= 1.1:
        # Single target — full position exits at opposing zone
        t1           = raw_target
        t2           = None
        t1_points    = raw_target_pts
        t2_points    = None
        rr_t1        = rr_raw
        rr_t2        = None
        contracts_t1 = contracts
        contracts_t2 = None
    elif rr_raw <= 2.0:
        t1 = round(entry + direction * stop_points * 1.0, 1)
        cap_price = round(entry + direction * stop_points * t2_cap,1)        # 6R cap
        # DOUBLE target — half position exits at t1
        t2 = round(min(raw_target, cap_price), 1) if direction == 1 \
            else round(max(raw_target, cap_price), 1)
        t1_points    = round(abs(t1 - entry), 1)
        t2_points    = round(abs(t2 - entry), 1)
        rr_t1        = round(t1_points / stop_points, 2)
        rr_t2        = round(t2_points / stop_points, 2)
        contracts_t1 = max(1, contracts // 2)
        contracts_t2 = contracts - contracts_t1
    else:
        # Two targets — T1 at 3x stop, T2 capped at opposing zone
        t1 = round(entry + direction * stop_points * 1.5, 1)
        cap_price = round(entry + direction * stop_points * t2_cap,1)        # 6R cap
        #DOUBLE target — half position exits at t2
        t2 = round(min(raw_target, cap_price), 1) if direction == 1 \
            else round(max(raw_target, cap_price), 1)       
        t1_points    = round(abs(t1 - entry), 1)
        t2_points    = round(abs(t2 - entry), 1)
        rr_t1        = round(t1_points / stop_points, 2)
        rr_t2        = round(t2_points / stop_points, 2)
        contracts_t1 = max(1, contracts // 2)
        contracts_t2 = contracts - contracts_t1 if contracts >= 2 else 0
    return {
        "zone_type"           : zone["type"],
        "formed_at"           : zone["formed_at"],
        "entry"               : entry,
        "stop"                : stop,
        "t1"                  : t1,
        "t2"                  : t2,
        "raw_target"          : raw_target,
        "rr_raw"              : rr_raw,
        "stop_points"         : round(stop_points, 1),
        "t1_points"           : t1_points,
        "t2_points"           : t2_points,
        "rr_t1"               : rr_t1,
        "rr_t2"               : rr_t2,
        "contracts"           : contracts,
        "contracts_t1"        : contracts_t1,
        "contracts_t2"        : contracts_t2,
        "risk_per_contract_$" : round(risk_per_contract, 2),
        "total_risk_$"        : round(contracts * risk_per_contract, 2),
        "potential_t1_$"      : round(contracts_t1 * t1_points * tick_value, 2),
        "potential_t2_$"      : round(contracts_t2 * t2_points * tick_value, 2) if contracts_t2 > 0 else 0,
    }

print("calculate_sets loaded ✅")

# ── MAIN SIGNAL CHECK ─────────────────────────
def run_signal_check():
    log("=" * 50)
    log("Running signal check (v7 strategy)...")

    try:
        df_5m, df_15m, df_1h = load_from_databento()
    except Exception as e:
        log(f"Data load failed: {e}")
        return

    df_5m  = add_session(add_candle_metrics(df_5m))
    df_15m = add_session(add_candle_metrics(df_15m))
    df_1h  = add_session(add_candle_metrics(df_1h))

    ny = ["NY_AM", "NY_PM", "NY_PreMarket"]
    df_5m_ny  = df_5m[df_5m["session"].isin(ny)].copy()
    df_15m_ny = df_15m[df_15m["session"].isin(ny)].copy()
    df_1h_ny  = df_1h[df_1h["session"].isin(ny)].copy()

    if len(df_15m_ny) < 50 or len(df_1h_ny) < config.ZONE_LOOKBACK_DAYS:
        log("Not enough NY-session bars for context — aborting run.")
        return

    # v7 stop buffer: half rolling-median 15m range, capped vs zone height
    stop_buffer = int(min(df_15m_ny["range"].rolling(20).median().iloc[-1] / 2,
                          config.MIN_ZONE_HEIGHT * 0.69))
    log(f"Stop buffer (v7 dynamic, capped): {stop_buffer} pts")

    # Entry proximity band: keep only signals whose entry is within
    # ENTRY_SIGMA std devs of the 14-bar range, measured from current price.
    range_std     = df_15m_ny["range"].rolling(config.ENTRY_SIGMA_WINDOW).std().iloc[-1]
    current_price = df_15m_ny["close"].iloc[-1]
    entry_band    = config.ENTRY_SIGMA * range_std

    log("=" * 62)
    if pd.isna(range_std) or range_std == 0:
        log(f"ENTRY BAND | range_std={range_std} -> GATE DISABLED THIS RUN (fails open)")
    else:
        log(f"ENTRY BAND | {config.ENTRY_SIGMA}sigma x {range_std:.2f} range-std "
            f"({config.ENTRY_SIGMA_WINDOW}-bar) = +/-{entry_band:.1f} pts")
        log(f"ENTRY BAND | price {current_price:.1f}  ->  "
            f"VALID RANGE [{current_price - entry_band:.1f} , {current_price + entry_band:.1f}]")
    log("=" * 62)

    try:
        curve_bias = set_curve(df_1h_ny)        # v7: curve on 1h
        trend      = classify_trend(df_15m_ny)  # v7: trend on 15m
    except Exception as e:
        log(f"Context failed: {e}")
        return
    log(f"Trend: {trend} | Curve: {curve_bias}")

    # zones detected on the recent window only (matches backtest)
    zone_cutoff = df_15m_ny.index.max() - pd.Timedelta(days=config.ZONE_WINDOW_DAYS)
    zones = detect_zones(df_15m_ny[df_15m_ny.index >= zone_cutoff])
    log(f"Zones detected (last {config.ZONE_WINDOW_DAYS}d): {len(zones)}")
    for z in zones:
        score = score_zone(z, trend, curve_bias, zones)
        log(f"  {z['type']} prox={z['proximal']} distal={z['distal']} "
            f"h={z['base_height']} base={z['base_count']} touch={z['touched']} "
            f"score={score} formed={z['formed_at']}")
        
    signals_sent = 0
    signals_skipped_band = 0
    for zone in zones:
        score = score_zone(zone, trend, curve_bias, zones)
        sets  = calculate_sets(zone, zones, stop_buffer=stop_buffer) \
                if score >= config.MIN_SCORE else None
        entry = sets["entry"] if sets else None
        dist  = abs(entry - current_price) if entry is not None else None

        # ── disposition ──
        if score < config.MIN_SCORE:
            disp = f"SKIP score {score} < {config.MIN_SCORE}"
            skipped_score += 1
        elif sets is None:
            disp = "SKIP no valid sets (no target / RR too low)"
            skipped_sets += 1
        elif pd.notna(range_std) and range_std > 0 and dist > entry_band:
            disp = f"SKIP {dist:.0f} pts from {current_price:.0f} > {entry_band:.0f} band"
            skipped_band += 1
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

        log(f"Signal: {zone['type']} @ {sets['entry']} (formed {zone['formed_at']}) | Score {score} | "
            f"R:R T1 {sets['rr_t1']} | {sets['contracts']} contracts | "
            f"risk ${sets['total_risk_$']}")
        result = send_alert(alert_data)        # test_mode default=True in alerts.py
        log(f"TradersPost: ok={result.ok} id={result.signal_id} detail={result.detail}")
        signals_sent += 1
    log("-" * 62)
    log(f"Done. {sent} sent | {skipped_score} low score | "
        f"{skipped_sets} no sets | {skipped_band} outside band")
    log("=" * 62)
    
    if signals_sent == 0:
        log("No qualifying signals found this run.")
    log(f"Done. {signals_sent} signal(s) sent, {signals_skipped_band} skipped by entry band.")
    log("=" * 50)

if __name__ == "__main__":
    run_signal_check()
