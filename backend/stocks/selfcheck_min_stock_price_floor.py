"""Self-check for delta_hedge_service.MIN_STOCK_PRICE — the structural price floor
that skips sub-Rs.150 F&O names (IDEA, SUZLON, YESBANK, NHPC, NMDC, GMRAIRPORT,
IREDA, VMM, PNB, IRFC as of 2026-07-31) before any option-chain REST calls are made
for them. Not an IV/liquidity call (that's IV_GUARD/RANGE_GUARD, already live) —
this is purely "don't waste a scan slot on a strike ladder too coarse to work with".

Both scan call sites (delta_hedge_service.py's main scanner, views.py's Force Scan
mini-scan) use the identical `0 < current_price < MIN_STOCK_PRICE` guard — mirrored
here rather than imported, since it's inline in a much larger loop in both places.
"""
import os
import sys
import django

sys.path.insert(0, "/home/jd/tradeplusai/tradepulse-ai/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


from stocks.services.delta_hedge_service import MIN_STOCK_PRICE


def is_skipped(current_price: float) -> bool:
    return 0 < current_price < MIN_STOCK_PRICE


print("=" * 72)
print("MIN_STOCK_PRICE boundary behavior")
print("=" * 72)
check("floor is Rs.150", MIN_STOCK_PRICE == 150, str(MIN_STOCK_PRICE))
check("data-unavailable (price=0) is NOT skipped on price grounds", not is_skipped(0))
check("IDEA-like penny stock (Rs.12.94) is skipped", is_skipped(12.94))
check("just below floor (Rs.149.99) is skipped", is_skipped(149.99))
check("exactly at floor (Rs.150) is NOT skipped", not is_skipped(150))
check("well above floor (Rs.5000) is NOT skipped", not is_skipped(5000))

print()
if fails:
    print(f"FAILED: {fails}")
    sys.exit(1)
print("ALL PASS — MIN_STOCK_PRICE floor behaves correctly at every boundary.")
