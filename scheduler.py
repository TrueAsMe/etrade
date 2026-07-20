# scheduler.py - MYM Signal Alert Scheduler
# Runs signal_check.py every 1 mins, 8 AM – 4PM ET, Mon–Fri

# Ensure logs directory exists on cloud
import os
os.makedirs("/app/logs", exist_ok=True)
import schedule
import time
from datetime import datetime
import pytz
from dotenv import load_dotenv
load_dotenv()

import alerts
import signals
from etrade_stage.alerts import flatten
from etrade_stage.signals import run_signal_check
import config

TIMEZONE = pytz.timezone("US/Eastern")

def is_trading_window():
    now = datetime.now(TIMEZONE)
    # Monday=0, Friday=4
    if now.weekday() > 4:
        return False
    # 8:00AM to 12:00PM ET
    if now.hour < config.ENTRY_HOUR_MIN or now.hour >= config.CUTOFF_HOUR:
        return False
    return True

def guarded_run():
    now = datetime.now(TIMEZONE)
    if is_trading_window():
        print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')} ET] ▶ Running signal check...")
        run_signal_check()
    else:
        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')} ET] ⏸ Outside trading window — skipping.")

def cutoff_flatten():
    """Close any open position at the 1:00 PM ET cutoff (weekdays only)."""
    now = datetime.now(TIMEZONE)
    if now.weekday() > 4:
        return
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} ET] Cutoff — flattening any open position...")
    result = flatten(test_mode=TEST_MODE)     # set True while validating
    print(f"[{stamp} ET] Flatten: ok={result.ok} id={result.signal_id} {result.detail}")

# ── SCHEDULE ──────────────────────────────────
# Every 30 minutes at :00 and :30
schedule.every().minute.at(":00").do(guarded_run)
schedule.every().day.at("16:10", "US/Eastern").do(cutoff_flatten)   # 4:10 PM ET flatten


# ── RUN TIMES ─────────────────────────────────
# 8:00 - 1:00 ET Mon–Fri

print("=" * 50)
print("  MYM Signal Alert Scheduler")
print("  Running 8:00AM – 1:00PM ET | Mon–Fri")
print("  Every 30 minutes")
print("=" * 50)

# Run once immediately on startup so you don't
# have to wait for the next :00 or :30
print("\nRunning initial check on startup...")
guarded_run()

# ── MAIN LOOP ─────────────────────────────────
while True:
    schedule.run_pending()
    time.sleep(30)  # check every 30 seconds
   # test_mode=True while validating   
