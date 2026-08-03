"""Self-check for option_buying_service._compute_target_sl()'s fixed 2-lot rupee
target/SL — Rs.5,000 profit / Rs.2,500 loss (clean 1:2), replacing the old
ADX-scaled 1.6x-2.0x target / fixed 0.625x SL.

Bug the old formula caused: SUNPHARMA 2020 CE ran to +Rs.8,785 unrealized profit
(entry 45.00 -> peak 57.55) with no exit condition anywhere near that level, then
decayed back to a loss by the 2:30 PM time-stop. The account owner asked for a
flat, simple rule instead: exit at Rs.5,000 profit or Rs.2,500 loss (2 lots), and
nothing else — no ADX scaling, no trailing stop.

Mocks get_lot_size instead of hitting Angel One — this only checks the arithmetic
that converts a rupee amount into a premium price via lot size.
"""
import os
import sys
import django

sys.path.insert(0, "/home/jd/tradeplusai/tradepulse-ai/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from unittest.mock import patch

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


from stocks.services import option_buying_service as obs

check("profit target is Rs.5,000 (2 lots)", obs.OPTION_BUYING_PROFIT_RUPEES == 5000, str(obs.OPTION_BUYING_PROFIT_RUPEES))
check("stop-loss is Rs.2,500 (2 lots)", obs.OPTION_BUYING_LOSS_RUPEES == 2500, str(obs.OPTION_BUYING_LOSS_RUPEES))
check("clean 1:2 ratio", obs.OPTION_BUYING_PROFIT_RUPEES == 2 * obs.OPTION_BUYING_LOSS_RUPEES)

print("=" * 72)
print("SUNPHARMA-shaped case: entry 45.00, lot_size 350")
print("=" * 72)

with patch("stocks.services.delta_hedge_service.get_lot_size", return_value=350):
    target, sl = obs._compute_target_sl("SUNPHARMA", 45.00)

# profit_move = 5000 / (350*2) = 7.142857 -> target = 45.00 + 7.142857 = 52.142857 -> rounded to tick
# loss_move   = 2500 / (350*2) = 3.571428 -> sl     = 45.00 - 3.571428 = 41.428571 -> rounded to tick
check("target ~= 52.14 (entry + Rs.5000/700)", abs(target - 52.14) < 0.10, str(target))
check("sl ~= 41.43 (entry - Rs.2500/700)", abs(sl - 41.43) < 0.10, str(sl))

reward = target - 45.00
risk = 45.00 - sl
# Tolerance loosened for tick rounding (round_to_tick snaps to the nearest Rs.0.05
# independently on each side, so the ratio drifts slightly off exactly 2.0 — not a bug).
check("reward:risk is ~2:1 (within tick-rounding noise)", abs(reward / risk - 2.0) < 0.03, f"reward={reward:.2f} risk={risk:.2f}")

# 2-lot P&L at target/SL must be close to the rupee amounts requested (tick rounding
# means it won't be exact — Rs.0.05 tick x 700 (lot_size x 2) = up to Rs.35 off).
check("2-lot profit at target ~= Rs.5,000", abs((target - 45.00) * 350 * 2 - 5000) < 35, str((target - 45.00) * 350 * 2))
check("2-lot loss at SL ~= Rs.2,500", abs((45.00 - sl) * 350 * 2 - 2500) < 35, str((45.00 - sl) * 350 * 2))

print()
print("=" * 72)
print("Degenerate case: cheap option + small lot size can drive SL negative")
print("=" * 72)
# Small lot size + low entry premium -> loss_move can exceed entry itself. Caller
# (get_option_buying_signals) rejects with `sl <= 0`, not this function's job.
with patch("stocks.services.delta_hedge_service.get_lot_size", return_value=100):
    target, sl = obs._compute_target_sl("CHEAPOPT", 1.00)
check("small lot size + cheap premium can drive SL negative (caller's job to reject)", sl < 0, str(sl))

print()
if fails:
    print(f"FAILED: {fails}")
    sys.exit(1)
print("ALL PASS — fixed 2-lot Rs.5,000/Rs.2,500 target/SL is a clean 1:2, correctly converted via lot size.")
