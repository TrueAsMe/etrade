# Testing Walkthrough — `test_databento.py` & `test_bracket.py`

End-to-end setup and run guide for the two connection tests, before the live
forward test. Run everything from the repo root: `/Users/efiles/prod/em_algo`.

**What each test proves**

- `test_databento.py` → the **data** path: Databento key works, the 120-day pull
  is affordable, and a live pull returns clean 1m bars that resample to 5m/15m.
- `test_bracket.py` → the **order** path: Schwab Market Data connects and returns a
  real-time quote, and a signal reaches TradersPost — optionally placing one real
  bracket on your Lucid/Tradovate account to confirm stop+target attach.

---

## 1. One-time setup

### 1.1 Environment + dependencies

```bash
cd /Users/efiles/prod/em_algo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Ensure these are present (add to requirements.txt if missing):
pip install databento schwab-py python-dotenv pandas requests
```

### 1.2 Add one line to `config.py`

`data_feed.py` reads `config.DATABENTO_API_KEY`. In the secrets block of
`config.py` add:

```python
DATABENTO_API_KEY = os.getenv("DATABENTO_API_KEY", "")
```

(There's an env-var fallback in `data_feed.py`, but adding it keeps your
"config centralizes secrets" pattern.)

### 1.3 Fill in `.env`

| Variable                                                        | Needed by               | Notes                                                                   |
| --------------------------------------------------------------- | ----------------------- | ----------------------------------------------------------------------- |
| `DATABENTO_API_KEY`                                           | databento test          | `db-...` from your Databento portal                                   |
| `SCHWAB_API_KEY`                                              | bracket test            | App Key (consumer key)                                                  |
| `SCHWAB_APP_SECRET`                                           | bracket test            | App Secret                                                              |
| `SCHWAB_CALLBACK_URL`                                         | token mint              | `https://127.0.0.1:8182` (must match the portal exactly)              |
| `SCHWAB_TOKEN_PATH`                                           | bracket test            | `./schwab_token.json` (run tests from repo root)                      |
| `TRADERSPOST_WEBHOOK_URL`                                     | bracket test            | full strategy webhook URL                                               |
| `EMAIL_SENDER` / `EMAIL_PASSWORD` / `DISCORD_WEBHOOK_URL` | `--live` flatten only | only needed so the auto-flatten can run; otherwise you flatten manually |

Do **not** set `SCHWAB_HEADLESS=1` locally — leave it unset so the first run can
open a browser to mint the token.

### 1.4 Mint the Schwab token (one time, local, needs a browser)

`test_bracket.py` needs a valid `schwab_token.json`. Mint it by triggering the
same auth your code uses:

```bash
python -c "import schwab_data; schwab_data.build_client(); print('token ready')"
```

A browser opens → log in to Schwab → it writes `schwab_token.json` in the repo
root. The access token auto-refreshes after this; the **refresh token lasts ~7
days**, so re-run this when it expires. (Requires the Market Data app to be
**Ready For Use**, not "Approved – Pending".)

### 1.5 Confirm the contract month in `data_feed.py`

`SYMBOL = "MYMU6"` is the **Sep 2026** contract. If you're past a quarterly roll
(H=Mar, M=Jun, U=Sep, Z=Dec), bump it to the new front month and keep
`STYPE_IN = "raw_symbol"`.

### 1.6 TradersPost strategy (for the `--live` bracket only)

- The strategy's broker (Lucid/Tradovate) is connected and the subscription active.
- **"Use signal take profit / stop loss" is enabled** on the subscription — without
  it, TradersPost ignores the bracket and you won't see the stop/target attach.

---

## 2. Pre-flight (run before the tests)

```bash
cd /Users/efiles/prod/em_algo
source .venv/bin/activate

# a) Alert-secret presence (prints presence only, never values)
python verify_env.py

# b) Token freshness — should print "token ready" with no browser if still valid
python -c "import schwab_data; schwab_data.build_client(); print('token ready')"
```

`verify_env.py` covers the alert secrets; the two test scripts self-check their own
keys (Databento / Schwab / TradersPost) and will tell you exactly what's missing.

---

## 3. Run `test_databento.py`

```bash
python test_databento.py            # 120-day cost estimate + a small 3-day pull
python test_databento.py --days 5   # widen the connectivity pull if you want
```

The 120-day figure is only an **estimate** (free); the script then pulls a small
window so it costs only a few cents of credit.

**Expected (healthy) output**

```
key ...XXXX | contract MYMU6 (raw_symbol) | dataset GLBX.MDP3
Estimated cost of the 120-day 1m pull: $0.xxxx
Pulling 3 day(s) of 1m to validate the connection...
Checks:
  [PASS] 1m frame non-empty
  [PASS] columns are OHLC
  [PASS] index is tz-aware
  ... (12 checks total)
ALL CHECKS PASSED
```

**Pass criteria:** every line reads `[PASS]` and the footer says
`ALL CHECKS PASSED`. The `Window` block should show a 1m count roughly 5× the 5m
count and 15× the 15m count, ending near the current time (a few minutes of lag is
normal for Databento history).

**If it fails**

- `DATABENTO_API_KEY not set` → fix `.env` / `config.py` (§1.2–1.3).
- `pull failed: ... no data` or empty frame → the contract likely rolled; update
  `data_feed.SYMBOL` (§1.5).
- High cost estimate → expected for 120 days of 1m; the test itself only pulls 3.

---

## 4. Run `test_bracket.py`

### 4.1 Safe run first (no real order)

```bash
python test_bracket.py
```

Does two things only: pulls a Schwab quote, then sends a `test:true` signal that
TradersPost validates but does **not** route to a broker.

**Expected (healthy) output**

```
Secrets present: SCHWAB_API_KEY, SCHWAB_APP_SECRET, TRADERSPOST_WEBHOOK_URL
[1] Schwab live quote...
  /MYMU26: 44xxx.x @ 2026-... (current timestamp)
[2] TradersPost chain check (test:true, no broker routing)...
  OK -- accepted: id=... logId=...
[3] Skipped live bracket. Re-run with --live ...
DONE (safe checks passed).
```

**Pass criteria:** step [1] prints a price with a **current** timestamp (if you see
`WARNING: quote is N min old`, your account may be on delayed data — resolve before
trusting live signals), and step [2] prints `OK -- accepted`.

### 4.2 Live bracket run (places ONE real 1-micro order)

Run this **during active market hours** so the order can fill:

```bash
python test_bracket.py --live
```

1. It repeats [1] and [2], then prompts: type **`FIRE`** to place the order
   (anything else aborts — this is the safety gate).
2. It sends a **real 1-contract** bracket to your broker via TradersPost.
3. **Inspect your broker**: confirm the entry filled and a **stop + take-profit**
   appear as a bracket on the position.
4. Press **Enter** to flatten (calls `alerts.flatten()`), or **Ctrl-C** to leave the
   position open and flatten manually.

**Notes & safety**

- Only 1 contract, gated behind `--live` **and** the typed `FIRE`.
- Auto-flatten uses `alerts.flatten()`, which needs the alert secrets
  (`EMAIL_*`, `DISCORD_WEBHOOK_URL`). If they're unset, the script catches the error
  and tells you to **flatten manually** — the position is still closed by you, not
  left dangling silently.
- If TradersPost rejects, the printed `messageCode`/detail tells you why (e.g.
  unsupported-ticker, or brackets not enabled per §1.6).

---

## 5. Recommended order & checklist

1. `pip install -r requirements.txt` (+ databento, schwab-py, python-dotenv)
2. Add `DATABENTO_API_KEY` line to `config.py`
3. Fill `.env`
4. `python verify_env.py`
5. Mint Schwab token (browser, one time)
6. Confirm `data_feed.SYMBOL` is the current front month
7. `python test_databento.py` → expect `ALL CHECKS PASSED`
8. `python test_bracket.py` → expect `DONE (safe checks passed)`
9. `python test_bracket.py --live` (market open) → confirm bracket on broker, flatten

---

## 6. Troubleshooting quick table

| Symptom                                    | Likely cause                             | Fix                                                                  |
| ------------------------------------------ | ---------------------------------------- | -------------------------------------------------------------------- |
| `DATABENTO_API_KEY not set`              | key not in env/config                    | §1.2–1.3                                                           |
| Databento pull empty / symbol error        | contract rolled                          | update`data_feed.SYMBOL` (§1.5)                                   |
| Schwab quote fails / auth error            | token missing or >7 days old             | re-mint (§1.4)                                                      |
| `WARNING: quote is N min old`            | account on delayed data                  | check Schwab market-data entitlement                                 |
| TradersPost rejects signal                 | bad ticker or brackets disabled          | check ticker + enable signal TP/SL (§1.6)                           |
| `--live` order placed but no stop/target | "Use signal take profit / stop loss" off | enable it on the subscription                                        |
| Flatten call failed                        | alert secrets unset                      | flatten manually; set`EMAIL_*`/`DISCORD_WEBHOOK_URL` to automate |

When you've run both, report back the footer lines (`ALL CHECKS PASSED` /
`DONE`) plus anything that printed `FAIL`/`WARNING`, and we'll move to the live
forward-test wiring.
