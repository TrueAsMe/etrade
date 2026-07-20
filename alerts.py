# alerts.py - Valid Zone & Entry Signal Workflow

from __future__ import annotations

import os
import json
import logging
import smtplib
from dataclasses import dataclass
from typing import Any, Mapping
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
from dotenv import load_dotenv

load_dotenv()
import config

logger = logging.getLogger(__name__)
# ─────────────────────────────────────────────
# alerts.py - MYM Signal Alert System
# Email via Gmail SMTP + Push notifications via Discord Webhook + TradersPost Send
# ─────────────────────────────────────────────

# DECLARE TEST MODE
# test_mode: when True, adds "test": true so TradersPost validates only.
test_mode = config.TEST_MODE

# ── EMAIL ─────────────────────────────────────
def send_email(subject, body):
    try:
        msg = MIMEMultipart()
        msg["From"]    = config.EMAIL_SENDER
        msg["To"]      = config.EMAIL_RECEIVER
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
                    server.ehlo()
                    server.starttls()
                    server.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
                    server.sendmail(config.EMAIL_SENDER, config.EMAIL_RECEIVER, msg.as_string())

        print("✅ Email sent successfully")
        return True

    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False

# ── DISCORD ──────────────────────────────────

def discord_alert(message, alert_type="signal"):
    """
    Send alert via Discord webhook.
    
    Args:
        message (str): The alert message (entry, stop, contracts, etc.)
        alert_type (str): Type of alert ("signal", "fill", "loss", etc.)
    
    Returns:
        bool: True if sent successfully, False if failed
    """
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    
    if not webhook_url:
        print("❌ DISCORD_WEBHOOK_URL not found in .env")
        return False
    
    try:
        # Format the message with emoji and type
        emoji = {
            "signal": "🎯",
            "fill": "✅",
            "loss": "❌",
            "warning": "⚠️",
            "update": "📊"
        }.get(alert_type, "📢")
        
        # Build Discord message
        discord_message = f"{emoji} **{alert_type.upper()}**\n\n{message}"
        
        # Send via webhook
        payload = {"content": discord_message}
        response = requests.post(webhook_url, json=payload, timeout=5)
        
        # Check if successful (204 = No Content, which is expected)
        if response.status_code == 204:
            print(f"✅ Discord alert sent: {alert_type}")
            return True
        else:
            print(f"⚠️ Discord returned status {response.status_code}")
            return False
    
    except requests.exceptions.Timeout:
        print("❌ Discord request timed out")
        return False
    except Exception as e:
        print(f"❌ Discord send failed: {e}")
        return False


# ── TRADERSPOST PAPER TRADING ──────────────────────────────────
# """TradersPost webhook client for sending OTA trade signals.

# All payload fields and response shapes are confirmed against the official
# TradersPost OpenAPI specification:
# https://docs.traderspost.io/docs/developer-resources/webhook-reference

# Spec-confirmed invariants this module depends on:
#   * Required request fields: "ticker", "action" (action enum: buy/sell/exit/cancel/add).
#   * Field names are case-sensitive.
#   * "signalPrice" is required when the broker has no market data (Tradovate/Lucid).
#   * "test": true validates the payload without submitting an order.
#   * Non-standard data must go under "extras" to avoid colliding with reserved fields.
#   * 200 success body: {success, id, logId, payload}.
#   * 400 error body: {success, messageCode, message}.
# """
# --------------------------------------------------------------------------- #
# Configuration (surfaced as constants so a maintainer changes them in one place)
# --------------------------------------------------------------------------- #
test_mode = test_mode
DEFAULT_TICKER = "MYM"
DEFAULT_TIMEOUT = 10  # seconds

# Signal types that mean "go long". Demand zones (drop-base-rally) buy;
# everything else sells. Extend this set as new long patterns are added.
LONG_SIGNAL_TYPES = frozenset({"DBR"})

# Internal signal keys that have no reserved TradersPost field and should be
# forwarded as opaque metadata under "extras".
EXTRA_KEYS = ("formed_at", "score","untouched?","stop_points", "total_risk", "potential_t1", "potential_t2")
    
# Internal signal keys that MUST be present to build a valid order.
REQUIRED_KEYS = ("type", "entry", "stop", "t1_points", "t2_points", "contracts")

# --------------------------------------------------------------------------- #
# Result type (so callers branch on structured data, never on log strings)
@dataclass(frozen=True)
class AlertResult:
    # """Outcome of a webhook send.

    # Attributes:
    #     ok:           True only on HTTP 200.
    #     status_code:  HTTP status, or None if the request never completed.
    #     signal_id:    TradersPost signal id from a 200 body (for reconciliation).
    #     message_code: Structured error code from a 4xx body (for branching).
    #     detail:       Human-readable message for logs/alerts.
    # """

    ok: bool
    status_code: int | None = None
    signal_id: str | None = None
    message_code: str | None = None
    detail: str = ""

# --------------------------------------------------------------------------- #
# Validation + payload construction (pure, no I/O — unit-testable in isolation)
# --------------------------------------------------------------------------- #

class SignalError(ValueError):
    """Raised when an internal signal cannot form a valid TradersPost order."""


def validate_signal(signal: Mapping[str, Any]) -> None:
    # """Fail fast, with context, before any network call.

    # Raises:
    #     SignalError: if a required key is missing.
    # """
    missing = [key for key in REQUIRED_KEYS if key not in signal]
    if missing:
        raise SignalError(f"signal missing required keys: {missing}")   

def build_payload(
    signal: Mapping[str, Any],
    *,
    ticker: str = DEFAULT_TICKER,
    test_mode: bool = test_mode,
) -> dict[str, Any]:
    # """Architecture B entry: full position, stop at `stop`, take-profit at the
    # runner target (t2 if a split, else t1). The T1 partial exit and breakeven
    # are fired separately at the T1-fill moment — see partial_exit_t1 / breakeven."""
    validate_signal(signal)

    entry  = signal["entry"]
    action = "buy" if signal["type"] in LONG_SIGNAL_TYPES else "sell"
    # Single-target signals (rr_raw <= 1.1) have t2 = None → target is t1.
    runner_target = signal["t2_points"] if signal.get("t2_points") is not None else signal["t1_points"]

    payload: dict[str, Any] = {
        "test": test_mode,
        "ticker": ticker,
        "action": action,
        "orderType": "limit",
        "price": entry,
        # no-quote brokers (Tradovate/Lucid) require a price on limit/bracket orders
        "signalPrice": entry,
        "quantity": signal["contracts"],
        "stopLoss":   {"type": "stop", "amount": signal["stop_points"]},   
        "takeProfit": {"amount": runner_target},                  
        "extras": {key: signal[key] for key in EXTRA_KEYS if key in signal},
    }
    
    if test_mode:
        payload["test"] = True

    return payload


def _parse_response(resp: requests.Response) -> AlertResult:
    """Map a raw HTTP response onto a structured AlertResult."""
    try:
        body = resp.json()
    except ValueError:
        body = {}

    if resp.status_code == 200:
        return AlertResult(
            ok=True,
            status_code=200,
            signal_id=body.get("id"),
            detail="accepted",
        )

    # 400/404/500: the spec provides messageCode + message on validation errors.
    return AlertResult(
        ok=False,
        status_code=resp.status_code,
        message_code=body.get("messageCode"),
        detail=body.get("message", resp.text),
    )

def traderspost_alert(
    signal: Mapping[str, Any],
    *,
    ticker: str = DEFAULT_TICKER,
    test_mode: bool = test_mode,
    timeout: int = DEFAULT_TIMEOUT,
    webhook_url: str | None = None,
) -> AlertResult:
    # """Send one OTA signal to the TradersPost webhook.

    # Args:
    #     signal:      internal signal mapping.
    #     ticker:      instrument symbol.
    #     test_mode:   if True, TradersPost validates but places no order.
    #     timeout:     seconds before the request is abandoned.
    #     webhook_url: override; defaults to env var TRADERSPOST_WEBHOOK_URL.

    # Returns:
    #     AlertResult — always returned, never raises on network/HTTP failure.
    #     (A SignalError from build_payload is the one intentional exception:
    #     a malformed signal is a programming error, not a runtime condition.)
    # """
    url = webhook_url or os.getenv("TRADERSPOST_WEBHOOK_URL")
    if not url:
        logger.error("TRADERSPOST_WEBHOOK_URL is not set")
        return AlertResult(ok=False, detail="missing webhook url")

    payload = build_payload(signal, ticker=ticker, test_mode=test_mode)
    logger.info("POST payload: %s", json.dumps(payload))

    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.Timeout:
        logger.error("Request timed out after %ss", timeout)
        return AlertResult(ok=False, detail=f"timeout after {timeout}s")
    except requests.exceptions.RequestException as exc:
        logger.error("Request failed: %s", exc)
        return AlertResult(ok=False, detail=str(exc))

    result = _parse_response(resp)
    if result.ok:
        logger.info("Accepted (200) signal_id=%s", result.signal_id)
    else:
        logger.warning(
            "Rejected (%s) code=%s: %s",
            result.status_code,
            result.message_code,
            result.detail,
        )
    return result

    
# ── COMBINED ALERT ────────────────────────────

def send_alert(zone, *, test_mode: bool = test_mode):
    # When ready for real signals to be sent for orders, change test_mode: to bool = True
    # Enforce that alert secrets exist ONLY when actually sending.
    # config.py reads secrets lazily (empty if unset) so backtests/notebooks
    # can import config freely; this is where the requirement is enforced.
    config.require_alert_secrets()
    direction = "BUY  🟢" if zone["type"] == "DBR" else "SELL 🔴"

    subject = f"MYM Signal — {direction} @ {zone['entry']}"

    email_body = f"""
MYM SIGNAL ALERT
─────────────────────────
Direction  : {direction}
Zone Type  : {zone['type']}
Entry      : {zone['entry']}
Stop       : {zone['stop']}
T1 Target  : {zone['t1']}
T2 Target  : {zone['t2']}
Stop Pts   : {zone['stop_points']}
T1 Points  : {zone['t1_points']}
T2 Points  : {zone['t2_points']}
R:R T1     : {zone['rr_t1']}:1
R:R T2     : {zone['rr_t2']}:1
Contracts  : {zone['contracts']}
Score      : {zone['score']}
Formed At  : {zone['formed_at']}
─────────────────────────
Max Risk   : ${zone['total_risk']}
T1 Profit  : ${zone['potential_t1']}
T2 Profit  : ${zone['potential_t2']}
"""
    # Concise push notification
    message = (
        f"{zone['type']} Zone\n"
        f"Entry     : {zone['entry']}\n"
        f"Stop      : {zone['stop']}\n"
        f"T1        : {zone['t1']}\n"
        f"T2        : {zone['t2']}\n"
        f"Formed At : {zone['formed_at']}\n"
        f"Score     : {zone['score']} | R:R {zone['rr_t1']}:1\n"
        f"Risk      : ${zone['total_risk']}\n"
        f"Contracts : {zone['contracts']}\n"  
        f"Stop Pts  : {zone['stop_points']}\n"
        f"T1 Points : {zone['t1_points']}\n"
        f"T2 Points : {zone['t2_points']}\n"
    )
        
    send_email(subject, email_body)
    discord_alert(message, alert_type="signal")
    return traderspost_alert(zone, test_mode=test_mode)
     
# ── TEST FUNCTION ─────────────────────────────

def test_alerts():
    print("Sending test alert...")
    test_zone = {
        "type"        : "DBR",
        "entry"       : 47800,
        "stop"        : 47762,
        "t1"          : 47971,
        "t2"          : 48180,
        "stop_points" : 38,
        "t1_points"   : 171,
        "t2_points"   : 380,
        "rr_t1"       : 4.5,
        "rr_t2"       : 10.0,
        "contracts"   : 2,
        "contracts_t1": 1,
        "contracts_t2": 1,
        "score"       : 9.0,
        "untouched?"  : 0,
        "formed_at"   : "2026-03-24 09:30:00",
        "total_risk"  : 38.00,
        "potential_t1": 171.00,
        "potential_t2": 380.00
    }

    send_alert(test_zone)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    test_alerts()

def partial_exit_t1(signal, *, ticker=DEFAULT_TICKER, test_mode=test_mode, timeout=DEFAULT_TIMEOUT):
    """Bank the T1 tranche: partial exit of contracts_t1 on the open position.
    To rest it as a limit at t1 instead of firing at the T1 moment, add
    orderType='limit' + limitPrice=signal['t1'] — VERIFY that combo works first."""
    config.require_alert_secrets()
    url = os.getenv("TRADERSPOST_WEBHOOK_URL")
    if not url:
        return AlertResult(ok=False, detail="missing webhook url")
    payload = {"test": test_mode, "ticker": ticker,
               "action": "exit", "quantity": signal["contracts_t1"]}
    logger.info("T1 PARTIAL EXIT %s", json.dumps(payload))
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return AlertResult(ok=False, detail=str(exc))
    return _parse_response(resp)

def breakeven(ticker=DEFAULT_TICKER, *, test_mode=test_mode, timeout=DEFAULT_TIMEOUT):
    """Move the runner's stop to entry after T1 fills (needs an open position
    and the bracket's stop order)."""
    config.require_alert_secrets()
    url = os.getenv("TRADERSPOST_WEBHOOK_URL")
    if not url:
        return AlertResult(ok=False, detail="missing webhook url")
    payload = {"test": test_mode, "ticker": ticker, "action": "breakeven", "orderType": "stop"}
    logger.info("BREAKEVEN %s", json.dumps(payload))
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return AlertResult(ok=False, detail=str(exc))
    return _parse_response(resp)
    
def flatten(ticker=config.TICKER, *, test_mode=test_mode, timeout=DEFAULT_TIMEOUT):
    """Close any open position for `ticker`. Documented exit: action=exit, no
    quantity (=> full position), ignoreTradingWindows so a window can't block it."""
    config.require_alert_secrets()
    url = os.getenv("TRADERSPOST_WEBHOOK_URL")
    if not url:
        logger.error("TRADERSPOST_WEBHOOK_URL is not set")
        return AlertResult(ok=False, detail="missing webhook url")
    payload = {"ticker": ticker, "action": "exit", "cancel": True, "ignoreTradingWindows": True}
    if test_mode:
        payload["test"] = test_mode
    logger.info("FLATTEN %s", json.dumps(payload))
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        logger.error("Flatten failed: %s", exc)
        return AlertResult(ok=False, detail=str(exc))
    return _parse_response(resp)


        # alert_data = {
        #     "type"        : zone["type"],
        #     "entry"       : sets["entry"],
        #     "stop"        : sets["stop"],
        #     "t1"          : sets["t1"],
        #     "t2"          : sets["t2"],
        #     "stop_points" : sets["stop_points"],
        #     "rr_t1"       : sets["rr_t1"],
        #     "rr_t2"       : sets["rr_t2"],
        #     "t1_points"   : sets["t1_points"],
        #     "t2_points"   : sets["t2_points"],
        #     "contracts"   : sets["contracts"],
        #     "score"       : score,
        #     "untouched?"  : zone["touched"],
        #     "formed_at"   : zone["formed_at"],
        #     "total_risk"  : sets["total_risk_$"],
        #     "potential_t1": sets["potential_t1_$"],
        #     "potential_t2": sets["potential_t2_$"],
        # }


