"""
Audit script for AUDIT_REMEDIATION_PLAN.md Phase 1 item #8 ("Naked short strangle
despite documented protective legs"), step 4: count actual legs on every currently-open
`category="specialist"` position so the account owner knows today's real exposure.

DO NOT RUN THIS IN AN AI-AGENT SANDBOX SESSION.

Like every other script in backend/scratch/, this needs `django.setup()` to reach the
ORM, and `django.setup()` runs `StocksConfig.ready()` (backend/stocks/apps.py) for the
`stocks` app as a side effect — which (a) starts the live APScheduler background jobs
(`stocks.updater.start()`) and (b) logs into the real Angel One broker session
(`truedata_service.initialize_truedata(...)`), unless today happens to be a static
market holiday. There is no read-only way to import the ORM here that skips this file's
own app-startup side effects — see CLAUDE.md and this project's saved memory
("Never run manage.py locally").

Run this only from a real trusted context (the account owner's shell, or CI) where a
broker login/scheduler start is expected/safe — e.g. a Render shell, or locally with
full awareness that it will log into the live Angel One session.

Usage:
    cd backend && python scratch/audit_specialist_legs.py
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from stocks.models import SignalHistory

open_specialist = SignalHistory.objects.filter(
    category="specialist",
    status__in=["ACTIVE", "PENDING"],
).order_by("-generated_at")

print("=" * 80)
print(f"OPEN SPECIALIST (STRANGLE) POSITIONS — LEG COUNT AUDIT")
print("=" * 80)

if not open_specialist.exists():
    print("No open (ACTIVE/PENDING) specialist positions found.")
else:
    naked_count = 0
    other_count = 0
    for sig in open_specialist:
        legs = (sig.metadata or {}).get("legs", [])
        actions = [leg.get("action") for leg in legs]
        sell_legs = sum(1 for a in actions if a == "SELL")
        buy_legs = sum(1 for a in actions if a == "BUY")
        tag = "NAKED (2-leg, sell-only, expected)" if len(legs) == 2 and buy_legs == 0 else "UNEXPECTED LEG COUNT — investigate"
        if len(legs) == 2 and buy_legs == 0:
            naked_count += 1
        else:
            other_count += 1
        print(
            f"id={sig.id:<6} symbol={sig.symbol:<12} status={sig.status:<10} "
            f"legs={len(legs)} (sell={sell_legs}, buy={buy_legs}) -> {tag}"
        )

    print("-" * 80)
    print(f"Total open: {open_specialist.count()}  |  As-expected naked 2-leg: {naked_count}  |  Unexpected: {other_count}")

print("=" * 80)
