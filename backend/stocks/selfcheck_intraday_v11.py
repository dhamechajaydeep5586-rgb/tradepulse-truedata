"""Verify the v1.1 audit fixes behave as intended."""
import os, django, sys
sys.path.insert(0, "/home/jd/tradeplusai/tradepulse-ai/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from stocks.services.signal_utils import compute_session_vwap, compute_vwap
from stocks.services.intraday_service import (
    position_size, _build_intraday_candidate,
    ROUND_TRIP_COST_PCT, MIN_TARGET_COST_MULTIPLE,
    INTRADAY_ACCOUNT_EQUITY, INTRADAY_FIXED_RISK_RUPEES,
)
from stocks.services.trading_engine.backtest import run_backtest_for_signal
from stocks.services.trading_engine.config import get_market_rules

fails = []
def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)

print("=" * 70)
print("1. POSITION SIZING (§5.1)")
print("=" * 70)
from stocks.services.intraday_service import INTRADAY_MAX_POSITION_PCT
qty, risk = position_size(entry=1000.0, stop_loss=995.0)   # 0.5% stop
expected_budget = INTRADAY_FIXED_RISK_RUPEES
check("risk-based qty", qty == int(expected_budget // 5.0),
      f"qty={qty} risk=Rs.{risk} (budget Rs.{expected_budget})")
check("rupee risk ~= budget", abs(risk - expected_budget) < 5.0, f"Rs.{risk}")

# The whole point of risk parity: rupee risk must be ~constant across stop widths,
# which means qty must scale INVERSELY with stop distance (equal-notional would not).
# Stop distances (Rs.5 / Rs.10, not the old Rs.3 / Rs.6) are chosen so neither uncapped
# qty (2500/5=500, 2500/10=250) exceeds INTRADAY_MAX_POSITION_PCT's notional cap (500
# units at entry=1000) — otherwise the cap, not risk-parity, would drive this specific
# comparison and the inverse-scaling assertion below would fail for the wrong reason.
q_tight, r_tight = position_size(1000.0, 995.0)   # 0.5% stop
q_wide, r_wide = position_size(1000.0, 990.0)     # 1.0% stop
check("tight stop sized larger than wide stop", q_tight > q_wide,
      f"tight={q_tight} wide={q_wide}")
check("rupee risk equalised across stop widths", abs(r_tight - r_wide) < 10.0,
      f"tight=Rs.{r_tight} wide=Rs.{r_wide}")
check("qty scales inversely with stop", abs(q_tight / q_wide - 2.0) < 0.05,
      f"ratio={q_tight / q_wide:.2f} (expect ~2.0 for half the stop)")

# Under the cap, risk must never EXCEED budget — capping may only reduce it.
for sl in (999.5, 999.0, 997.0, 994.0, 990.0):
    _q, _r = position_size(1000.0, sl)
    if _r > expected_budget + 1:
        check(f"risk never exceeds budget (stop {1000 - sl})", False, f"Rs.{_r}")
        break
else:
    check("risk never exceeds budget across stop widths", True)

# Cap should bind only on unusually tight stops, not on normal ones.
notional_cap_qty = int(INTRADAY_ACCOUNT_EQUITY * (INTRADAY_MAX_POSITION_PCT / 100) // 1000.0)
check("normal stop NOT capped", q_wide < notional_cap_qty,
      f"qty={q_wide} < cap={notional_cap_qty}")
q_vtight, _ = position_size(1000.0, 999.5)        # 0.05% stop
check("very tight stop IS capped", q_vtight == notional_cap_qty,
      f"qty={q_vtight} cap={notional_cap_qty}")
check("zero-distance stop rejected", position_size(1000.0, 1000.0) == (0, 0.0))

print()
print("=" * 70)
print("2. COST GATE (§0.1)")
print("=" * 70)
min_pct = MIN_TARGET_COST_MULTIPLE * ROUND_TRIP_COST_PCT
print(f"   round-trip cost={ROUND_TRIP_COST_PCT}%  min target={min_pct}%")

LIQ = {"adv_inr": 5e8, "daily_vol_pct": 1.5}   # realistic large-cap liquidity
def build(entry, sl, tgt, liquidity=LIQ):
    return _build_intraday_candidate(
        ticker_sym="TEST", strategy_key="VP", strategy_name="VP", signal="BUY",
        price=entry, entry=entry, stop_loss=sl, target=tgt, rr=2.0,
        reason="t", score=4.0, priority=1, vol_ratio=1.5, liquidity=liquidity,
    )

# 0.16% stop / 0.32% target — the audit's "typical" case. Below 0.42% → must reject.
thin = build(1000.0, 998.4, 1003.2)
check("thin-target trade rejected (impact-priced)", thin is None,
      "target 0.32% below 3x modelled cost")
# Same trade with no liquidity data must fall back to the conservative constant.
check("no-liquidity path uses conservative fallback",
      build(1000.0, 998.4, 1003.2, liquidity=None) is None)

# 0.30% stop / 0.60% target — clears the gate.
fat = build(1000.0, 997.0, 1006.0)
check("viable trade accepted", fat is not None)
if fat:
    check("qty attached", fat.get("qty", 0) > 0, f"qty={fat['qty']}")
    # Entry is slipped by the half-spread (§3.1g), so the achievable target is
    # slightly under the 0.60% measured from the mid — that reduction is real.
    check("target_pct measured off the slipped entry",
          0.57 <= fat["target_pct"] <= 0.60, f"{fat['target_pct']}%")
    check("modelled cost recorded", 0.0 < fat["cost_pct"] < 0.5, f"{fat['cost_pct']}%")
    check("relaxed flag defaults False", fat["relaxed"] is False)

relaxed_sig = _build_intraday_candidate(
    ticker_sym="T", strategy_key="VP", strategy_name="VP", signal="BUY",
    price=1000.0, entry=1000.0, stop_loss=997.0, target=1006.0, rr=2.0,
    reason="t", score=4.0, priority=1, vol_ratio=1.0, relaxed=True,
)
check("relaxed flag propagates", relaxed_sig["relaxed"] is True)

print()
print("=" * 70)
print("3. BACKTEST PESSIMISM + OFF-BY-ONE (§8.1)")
print("=" * 70)
rules = get_market_rules("intraday")
print(f"   pending_max_candles={rules.pending_max_candles}")

# Bar 1 activates; bar 2 range spans BOTH target and stop → must book the LOSS.
amb = pd.DataFrame(
    {"Open": [100.0, 100.0], "High": [100.0, 106.0],
     "Low": [100.0, 94.0], "Close": [100.0, 100.0]},
    index=pd.date_range("2026-07-24 09:15", periods=2, freq="5min"),
)
res = run_backtest_for_signal(amb, {"signal": "BUY", "entry": 100.0,
                                    "stop_loss": 95.0, "target": 105.0}, rules)
check("ambiguous bar books the stop", res["status"] == "HIT_SL",
      f"got {res['status']} (was HIT_TARGET before fix)")

# Unambiguous win must still register.
win = pd.DataFrame(
    {"Open": [100.0, 100.0], "High": [100.0, 106.0],
     "Low": [100.0, 99.5], "Close": [100.0, 105.5]},
    index=pd.date_range("2026-07-24 09:15", periods=2, freq="5min"),
)
res = run_backtest_for_signal(win, {"signal": "BUY", "entry": 100.0,
                                    "stop_loss": 95.0, "target": 105.0}, rules)
check("clean target still books the win", res["status"] == "HIT_TARGET", res["status"])

# Signal that triggers on bar 2 must NOT be cancelled first (off-by-one).
late = pd.DataFrame(
    {"Open": [98.0, 100.0], "High": [98.5, 100.5],
     "Low": [97.5, 99.5], "Close": [98.0, 100.0]},
    index=pd.date_range("2026-07-24 09:15", periods=2, freq="5min"),
)
res = run_backtest_for_signal(late, {"signal": "BUY", "entry": 100.0,
                                     "stop_loss": 95.0, "target": 105.0}, rules)
check("bar-2 activation not pre-cancelled", res["status"] == "ACTIVE",
      f"got {res['status']} (was CANCELLED before fix)")

print()
print("=" * 70)
print("4. SESSION-ANCHORED VWAP (§2.2)")
print("=" * 70)
idx = pd.to_datetime([
    "2026-07-23 09:15", "2026-07-23 09:20",   # day 1, price ~100
    "2026-07-24 09:15", "2026-07-24 09:20",   # day 2, price ~200
])
df = pd.DataFrame({"High": [100, 100, 200, 200], "Low": [100, 100, 200, 200],
                   "Close": [100, 100, 200, 200], "Volume": [1000] * 4}, index=idx)
sess = float(compute_session_vwap(df).iloc[-1])
cum = float(compute_vwap(df).iloc[-1])
check("session VWAP resets daily", abs(sess - 200.0) < 0.01, f"session={sess:.2f}")
check("cumulative VWAP is dragged by prior day", abs(cum - 150.0) < 0.01, f"cumulative={cum:.2f}")

print()
print("=" * 70)
print("5. BAR-BASED EXIT AUDIT (§4.1)")
print("=" * 70)
from stocks.services.live_signal_service import _scan_bars_for_exit, _scan_bars_for_activation
from stocks.models import SignalHistory

class FakeSig:
    def __init__(self, t, sl, e, typ="BUY"):
        self.target, self.stop_loss, self.entry_price, self.signal_type = t, sl, e, typ

bars = pd.DataFrame(
    {"High": [101.0, 106.0], "Low": [99.0, 94.0], "Close": [100.0, 100.0]},
    index=pd.date_range("2026-07-24 10:00", periods=2, freq="1min"),
)
out = _scan_bars_for_exit(FakeSig(105.0, 95.0, 100.0), bars)
check("intrabar stop detected on bar high/low", out is not None and out[0] == SignalHistory.Status.HIT_SL,
      f"{out[0] if out else None}")

# A touch that a 15-min LTP poll would miss entirely (price returns to 100).
spike = pd.DataFrame(
    {"High": [105.5], "Low": [99.8], "Close": [100.0]},
    index=pd.date_range("2026-07-24 10:00", periods=1, freq="1min"),
)
out = _scan_bars_for_exit(FakeSig(105.0, 95.0, 100.0), spike)
check("target touch invisible to LTP is caught", out is not None and out[0] == SignalHistory.Status.HIT_TARGET,
      f"{out[0] if out else None}")

# SELL mirror.
sell_bars = pd.DataFrame(
    {"High": [106.0], "Low": [100.0], "Close": [105.0]},
    index=pd.date_range("2026-07-24 10:00", periods=1, freq="1min"),
)
out = _scan_bars_for_exit(FakeSig(95.0, 105.0, 100.0, "SELL"), sell_bars)
check("SELL stop detected", out is not None and out[0] == SignalHistory.Status.HIT_SL,
      f"{out[0] if out else None}")

act = _scan_bars_for_activation(FakeSig(105.0, 95.0, 100.0), bars)
check("activation detected from bar range", act is not None, str(act))

print()
print("=" * 70)
print(f"RESULT: {'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
print("=" * 70)
if fails:
    sys.exit(1)
