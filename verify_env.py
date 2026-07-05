# verify_env.py — confirm all required secrets are present.
# Prints only PRESENCE, never the values. Run after setting up your
# .env (local) or the host environment (cloud).
#
#   python verify_env.py

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REQUIRED = ["EMAIL_SENDER", "EMAIL_PASSWORD", "DISCORD_WEBHOOK_URL"]
OPTIONAL = ["EMAIL_RECEIVER", "MYM_BASE", "ALPACA_KEY", "ALPACA_SECRET",
            "MASSIVE_API_KEY", "DATABENTO_API_KEY"]

print("Required:")
for k in REQUIRED:
    print(f"  {'OK ' if os.getenv(k) else 'MISSING'}  {k}")

print("Optional:")
for k in OPTIONAL:
    print(f"  {'set' if os.getenv(k) else '- - '}  {k}")

missing = [k for k in REQUIRED if not os.getenv(k)]
if missing:
    raise SystemExit(f"\nFAIL — missing required: {', '.join(missing)}")

print("\nAll required secrets present. Safe to run signal_check.py.")
