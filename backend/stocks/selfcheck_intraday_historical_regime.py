"""Correctness checks for historical regime reconstruction and its wiring into replay."""
import os, sys, django
sys.path.insert(0, "/home/jd/tradeplusai/tradepulse-ai/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import numpy as np
import pandas as pd

fails = []
def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)

IST = "Asia/Kolkata"

print("=" * 72)
print("1. VWAP ZERO-VOLUME FALLBACK (the production bug fix)")
print("=" * 72)
from stocks.services.signal_utils import compute_session_vwap

idx = pd.date_range("2026-01-01 09:15", periods=10, freq="15min", tz=IST)
zero_vol = pd.DataFrame({
    "High": np.linspace(100, 110, 10), "Low": np.linspace(99, 109, 10),
    "Close": np.linspace(99.5, 109.5, 10), "Volume": [0.0] * 10,
}, index=idx)
vwap = compute_session_vwap(zero_vol)
check("no NaN on zero-volume (index) data", vwap.isna().sum() == 0, f"{vwap.isna().sum()} NaN")
check("falls back to unweighted typical-price average",
      abs(vwap.iloc[-1] - ((zero_vol["High"] + zero_vol["Low"] + zero_vol["Close"]) / 3).mean()) < 0.01)

real_vol = zero_vol.copy()
real_vol["Volume"] = np.linspace(1000, 2000, 10)
vwap2 = compute_session_vwap(real_vol)
check("normal volume path unaffected by the fallback", not vwap2.isna().any())
check("volume-weighted differs from simple average when volume varies",
      abs(vwap2.iloc[-1] - vwap.iloc[-1]) > 0.001)

print()
print("=" * 72)
print("2. CAUSALITY — regime at bar i must not see bar i+1..n")
print("=" * 72)
from stocks.services.trading_engine.historical_regime import build_nifty_regime_series

n = 300
rng = np.random.default_rng(11)
# Flat/quiet for the first 200 bars, then a sharp sustained rally for the rest —
# a regime computed causally must show NEUTRAL/SIDEWAYS well before the rally starts.
close = np.concatenate([100 + rng.normal(0, 0.3, 200).cumsum() * 0.05,
                        np.linspace(100, 140, 100)])
idx2 = pd.date_range("2026-01-01 09:15", periods=n, freq="15min", tz=IST)
nifty = pd.DataFrame({
    "Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close,
    "Volume": np.zeros(n),
}, index=idx2)

reg = build_nifty_regime_series(nifty)
early = reg.iloc[100:150]["directional_bias"]
check("no lookahead: early-quiet-period regime is not dominated by BULLISH",
      (early == "BULLISH").mean() < 0.5, f"{(early == 'BULLISH').mean():.0%} BULLISH")

late = reg.iloc[-30:]["directional_bias"]
check("regime eventually recognises the sustained rally as BULLISH",
      (late == "BULLISH").mean() > 0.3, f"{(late == 'BULLISH').mean():.0%} BULLISH")

print()
print("=" * 72)
print("3. HYSTERESIS — a single-bar blip must not flip the held state")
print("=" * 72)
stable = 100 + rng.normal(0, 0.05, 150).cumsum() * 0.01
idx3 = pd.date_range("2026-01-01 09:15", periods=150, freq="15min", tz=IST)
blip = pd.DataFrame({"Open": stable, "High": stable + 0.1, "Low": stable - 0.1,
                     "Close": stable, "Volume": np.zeros(150)}, index=idx3)
blip.iloc[100, blip.columns.get_loc("Close")] = stable[100] + 20  # one wild spike bar
reg3 = build_nifty_regime_series(blip)
check("single-bar spike does not immediately flip trend_state",
      reg3["trend_state"].iloc[100] == reg3["trend_state"].iloc[99],
      f"bar99={reg3['trend_state'].iloc[99]} bar100={reg3['trend_state'].iloc[100]}")

print()
print("=" * 72)
print("4. AS-OF LOOKUP — never returns a regime from the future")
print("=" * 72)
from stocks.services.trading_engine.historical_regime import lookup_asof

small_reg = pd.DataFrame(
    {"directional_bias": ["SIDEWAYS", "BULLISH", "BEARISH"],
     "allow_momentum": [True, True, False],
     "allow_mean_reversion": [True, False, True],
     "size_multiplier": [0.75, 1.25, 1.0]},
    index=pd.to_datetime(["2026-01-01 09:15", "2026-01-01 09:30", "2026-01-01 09:45"]).tz_localize(IST),
)
query_ts = pd.to_datetime(["2026-01-01 09:20", "2026-01-01 09:44", "2026-01-01 10:00"]).tz_localize(IST)
looked_up = lookup_asof(small_reg, query_ts)
check("09:20 query resolves to the 09:15 regime (backward, not forward)",
      looked_up["directional_bias"].iloc[0] == "SIDEWAYS",
      looked_up["directional_bias"].iloc[0])
check("09:44 query resolves to 09:30, not the not-yet-arrived 09:45 regime",
      looked_up["directional_bias"].iloc[1] == "BULLISH",
      looked_up["directional_bias"].iloc[1])
check("10:00 query resolves to the latest available (09:45)",
      looked_up["directional_bias"].iloc[2] == "BEARISH")

print()
print("=" * 72)
print("5. REPLAY WIRING — trigger family gating actually drops candidates")
print("=" * 72)
import stocks.services.trading_engine.replay as replay_mod

n2 = 60
close2 = np.full(n2, 1000.0)
stock_df = pd.DataFrame({
    "Open": close2, "High": close2 + 1, "Low": close2 - 1, "Close": close2,
    "Volume": np.full(n2, 10000.0),
}, index=pd.date_range("2026-01-01 09:15", periods=n2, freq="15min", tz=IST))

# Deterministic stand-in: every bar "fires" one momentum trigger and one mean-reversion
# trigger, regardless of price action, so the gating logic is tested directly rather
# than depending on the real strategy organically producing both trigger types.
def fake_volume_profile_logic(symbol, window, nifty_bias="SIDEWAYS", liquidity=None,
                              relaxed=False, size_multiplier=1.0):
    price = float(window["Close"].iloc[-1])
    return [
        {"signal": "BUY", "entry": price, "stop_loss": price - 5, "target": price + 10,
         "reason": "[VOL_PROFILE] POC Bullish Flip", "vol_ratio": 1.5,
         "target_pct": 5.0, "cost_pct": 0.1, "qty": 10},
        {"signal": "BUY", "entry": price, "stop_loss": price - 5, "target": price + 10,
         "reason": "[VOL_PROFILE] Value Area Low Rejection", "vol_ratio": 1.5,
         "target_pct": 5.0, "cost_pct": 0.1, "qty": 10},
    ]

original = replay_mod._volume_profile_logic if hasattr(replay_mod, "_volume_profile_logic") else None
import stocks.services.intraday_service as isvc
real_fn = isvc._volume_profile_logic
isvc._volume_profile_logic = fake_volume_profile_logic
try:
    blocking_regime = pd.DataFrame({
        "directional_bias": "SIDEWAYS", "allow_momentum": False, "allow_mean_reversion": True,
        "size_multiplier": 1.0,
    }, index=stock_df.index)

    liq = {"adv_inr": 5e8, "daily_vol_pct": 1.5}
    from stocks.services.trading_engine.replay import replay_symbol
    without_gate = replay_symbol("TEST", stock_df.copy(), liquidity=liq, regime_lookup=None, step=5)
    with_gate = replay_symbol("TEST", stock_df.copy(), liquidity=liq, regime_lookup=blocking_regime, step=5)
finally:
    isvc._volume_profile_logic = real_fn

from stocks.services.shared.regime import MOMENTUM_STRATEGIES, MEAN_REVERSION_STRATEGIES
mom_without = sum(1 for c in without_gate if c["reason"].split("] ", 1)[-1] in MOMENTUM_STRATEGIES)
mom_with = sum(1 for c in with_gate if c["reason"].split("] ", 1)[-1] in MOMENTUM_STRATEGIES)
mr_with = sum(1 for c in with_gate if c["reason"].split("] ", 1)[-1] in MEAN_REVERSION_STRATEGIES)
print(f"   momentum candidates: no-gate={mom_without}  momentum-blocked-gate={mom_with}")
print(f"   mean-reversion candidates (should survive): {mr_with}")
check("without a gate, both trigger families come through",
      mom_without > 0, f"{mom_without} momentum candidates with no gate")
check("blocking momentum in the regime actually removes momentum candidates",
      mom_with == 0 and mom_without > 0, f"{mom_with} leaked through of {mom_without} generated")
check("mean-reversion trigger still survives the same gate (only momentum blocked)",
      mr_with > 0, f"{mr_with}")
check("candidates carry directional_bias for ranking to consume",
      all("directional_bias" in c for c in with_gate) if with_gate else False)

print()
print("=" * 72)
print("6. RANKING PREFERS PER-CANDIDATE BIAS OVER THE BATCH REGIME OBJECT")
print("=" * 72)
from stocks.services.shared.ranking import _alignment_score

class FakeRegime:
    directional_bias = "BEARISH"

buy_with_own_bullish = {"signal": "BUY", "directional_bias": "BULLISH"}
buy_no_override = {"signal": "BUY"}
check("per-candidate BULLISH overrides batch BEARISH for a BUY (should score high)",
      _alignment_score(buy_with_own_bullish, FakeRegime()) == 1.0,
      str(_alignment_score(buy_with_own_bullish, FakeRegime())))
check("falls back to batch regime when candidate has no override",
      _alignment_score(buy_no_override, FakeRegime()) == 0.0,
      "BUY against batch BEARISH should score 0")

print()
print("=" * 72)
print(f"RESULT: {'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
print("=" * 72)
if fails:
    sys.exit(1)
