"""Self-check for delta_hedge_service.NIFTY_50_STOCKS() — the live-fetch-with-fallback
wrapper that replaced the old hardcoded NIFTY_50_STOCKS list, so an NSE index
reconstitution (e.g. TCS added, INFY dropped) reaches the specialist/strangle
engine automatically instead of silently drifting out of sync.

Mocks signal_utils.fetch_nifty_symbols_live — never touches the network or DB.
"""
import os
import sys
import django

sys.path.insert(0, "/home/jd/tradeplusai/tradepulse-ai/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from unittest.mock import patch

from stocks.services import delta_hedge_service as dhs

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


print("=" * 72)
print("1. NIFTY_50_STOCKS() returns the live list when the NSE fetch succeeds")
print("=" * 72)
with patch.object(dhs, "fetch_nifty_symbols_live", return_value=["TCS", "RELIANCE"]):
    live = dhs.NIFTY_50_STOCKS()
check("uses the live list, not the static fallback", live == ["TCS", "RELIANCE"], str(live))
check("live list excludes INFY (simulated reconstitution)", "INFY" not in live)

print()
print("=" * 72)
print("2. NIFTY_50_STOCKS() falls back to the static snapshot when the NSE fetch fails")
print("=" * 72)
with patch.object(dhs, "fetch_nifty_symbols_live", return_value=[]):
    fallback = dhs.NIFTY_50_STOCKS()
check("falls back to the static snapshot", fallback == dhs._NIFTY_50_FALLBACK)
check("fallback snapshot is non-empty", len(fallback) > 0, str(len(fallback)))

print()
if fails:
    print(f"FAILED: {fails}")
    sys.exit(1)
print("ALL PASS — NIFTY_50_STOCKS() live-fetch + fallback verified.")
