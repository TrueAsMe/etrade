################################################################################

## schwabdev Youtube Video example
##### LINK: https://www.youtube.com/watch?v=69cniU1CTf8&t=208s

import schwabdev
import os 
from dotenv import load_dotenv
import logging

load_dotenv()

import config
logging.basicConfig(level=logging.INFO)

app_key = config.SCHWAB_API_KEY
app_secret = config.SCHWAB_APP_SECRET

client = schwabdev.Client(app_key, app_secret)

print(client.quotes("MYMU6").json())


################################################################################

# Databento Historical + Live API Example (Gemini)
import os
import databento as db

# 1. Automatically pulls 'DATABENTO_API_KEY' from your environment variables
API_KEY = os.getenv("DATABENTO_API_KEY", "your_api_key_here")

def run_historical_analysis():
    """Fetches historical batch/time-series data for backtesting."""
    print("Initializing Historical Client...")
    hist_client = db.Historical(API_KEY)

    # Query historic trades from a specific dataset
    data = hist_client.timeseries.get_range(
        dataset="GLBX.MDP3",
        symbols=["ES.FUT"],
        start="2026-01-15T09:30:00",
        end="2026-01-15T16:00:00",
        schema="trades"
    )
    return data

def stream_live_data():
    """Connects to the low-latency venue gateway for real-time execution."""
    print("Initializing Live Client...")
    live_client = db.Live(API_KEY)

    # Subscribe to the exact same dataset, symbols, and schema structures
    live_client.subscribe(
        dataset="GLBX.MDP3",
        symbols=["ES.FUT"],
        schema="trades"
    )

    print("Streaming live records...")
    for record in live_client:
        # Both APIs pass back matching DBN structural record objects
        print(f"Timestamp: {record.ts_event} | Price: {record.price}")

if __name__ == "__main__":
    # In a real trading system, you would typically pass your historical
    # data to a backtester, then spin up the live stream inside an async loop.
    historical_data = run_historical_analysis()
    stream_live_data()
