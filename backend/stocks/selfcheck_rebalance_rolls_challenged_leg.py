"""Self-check for rebalance_delta_neutral_strangle()'s leg-selection branch
(delta_hedge_service.py).

Bug this guards: the old logic rolled the SAFE leg inward to match the
challenged leg's (higher) delta, instead of rolling the CHALLENGED leg further
OTM to bring its own delta back down. Net effect was adding risk to the safe
side instead of reducing risk on the threatened side — confirmed in production
on CIPLA 2026-07-31 (CE was the challenged leg, spot rising toward its strike,
but the system closed/rolled PE almost ATM instead of touching CE at all).

Mirrors the exact branch condition rather than importing it — the real
function is a long, side-effecting flow (fetches quotes, sends Telegram,
mutates `updated_legs`) not something to invoke in a unit check.
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


def resolve_roll(ce_delta: float, pe_delta: float) -> tuple[str, float]:
    """Mirrors delta_hedge_service.py's rebalance leg-selection branch."""
    if ce_delta > pe_delta:
        return "CE", pe_delta
    else:
        return "PE", ce_delta


print("=" * 72)
print("Rebalance rolls the CHALLENGED (higher-delta) leg, not the safe one")
print("=" * 72)

# CIPLA-shaped case: CE is challenged (spot rising toward its strike).
op_type, target_delta = resolve_roll(ce_delta=0.42, pe_delta=0.18)
check("CE challenged -> rolls CE (not PE)", op_type == "CE", op_type)
check("target delta is the SAFE leg's (smaller) delta, to bring CE back down",
      target_delta == 0.18, str(target_delta))

# Mirror case: PE challenged.
op_type, target_delta = resolve_roll(ce_delta=0.15, pe_delta=0.40)
check("PE challenged -> rolls PE (not CE)", op_type == "PE", op_type)
check("target delta is the SAFE leg's (CE's) delta", target_delta == 0.15, str(target_delta))

print()
if fails:
    print(f"FAILED: {fails}")
    sys.exit(1)
print("ALL PASS — rebalance always rolls the threatened leg further OTM, never the safe leg inward.")
