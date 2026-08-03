"""Verify v1.2 / v2.0 modules: regime, ranking, portfolio risk, backtester, validation."""
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

print("=" * 72)
print("1. COST MODEL (§0.1 / §8.2)")
print("=" * 72)
from stocks.services.trading_engine.cost_model import CostModel, DEFAULT_COST_MODEL
cm = DEFAULT_COST_MODEL
rt = cm.round_trip_pct(symbol="RELIANCE", price=2900.0, qty=100)
print(f"   modelled round-trip on Rs.2.9L notional: {rt:.4f}%")
check("round-trip cost in the audit's 0.10-0.20% band", 0.08 <= rt <= 0.22, f"{rt:.4f}%")

buy = cm.slipped_fill(1000.0, is_buy=True)
sell = cm.slipped_fill(1000.0, is_buy=False)
check("buy fills above mid", buy > 1000.0, f"{buy:.4f}")
check("sell fills below mid", sell < 1000.0, f"{sell:.4f}")

sc = cm.side_cost(symbol="X", price=1000.0, qty=100, is_buy=False)
check("STT charged on sell only", sc.stt > 0)
check("stamp duty not charged on sell", sc.stamp_duty == 0.0)
big = cm.side_cost(symbol="X", price=1000.0, qty=100000, is_buy=True,
                   adv_inr=5e8, daily_vol_pct=1.5)
check("market impact scales with participation", big.impact > 0, f"Rs.{big.impact:.0f}")

print()
print("=" * 72)
print("2. REGIME MODEL (§2)")
print("=" * 72)
from stocks.services.regime_service import (
    RegimeState, _resolve_permissions, strategy_allowed,
    MOMENTUM_STRATEGIES, MEAN_REVERSION_STRATEGIES,
)
trend_exp = _resolve_permissions(RegimeState(trend_state="UP", vol_state="EXPANDING"))
range_con = _resolve_permissions(RegimeState(trend_state="NEUTRAL", vol_state="CONTRACTING"))
chop = _resolve_permissions(RegimeState(trend_state="NEUTRAL", vol_state="EXPANDING"))

check("trend+expanding allows momentum", trend_exp.allow_momentum)
check("trend+expanding blocks mean-reversion", not trend_exp.allow_mean_reversion)
check("range+contracting allows mean-reversion", range_con.allow_mean_reversion)
check("range+contracting blocks momentum", not range_con.allow_momentum)
check("cleanest regime sizes up", trend_exp.size_multiplier > 1.0, f"{trend_exp.size_multiplier}x")
check("chop sizes down", chop.size_multiplier <= 0.5, f"{chop.size_multiplier}x")

check("POC flip gated as momentum",
      strategy_allowed("POC Bullish Flip", trend_exp) and
      not strategy_allowed("POC Bullish Flip", range_con))
check("VA rejection gated as mean-reversion",
      strategy_allowed("Value Area Low Rejection", range_con) and
      not strategy_allowed("Value Area Low Rejection", trend_exp))

print()
print("=" * 72)
print("3. RANKING MODEL (§6.3)")
print("=" * 72)
from stocks.services.ranking_service import score_candidates, apply_threshold, FACTOR_WEIGHTS
check("weights sum to 100", sum(FACTOR_WEIGHTS.values()) == 100, str(sum(FACTOR_WEIGHTS.values())))

cands = [
    {"symbol": "STRONG", "signal": "BUY", "relative_strength": 2.5, "vol_ratio": 2.0,
     "target_pct": 1.2, "cost_pct": 0.14, "trend_quality": 0.9, "sector": "IT"},
    {"symbol": "WEAK", "signal": "SELL", "relative_strength": -1.5, "vol_ratio": 1.0,
     "target_pct": 0.45, "cost_pct": 0.14, "trend_quality": 0.1, "sector": "FMCG"},
    {"symbol": "MID", "signal": "BUY", "relative_strength": 0.4, "vol_ratio": 1.4,
     "target_pct": 0.7, "cost_pct": 0.14, "trend_quality": 0.5, "sector": "IT"},
]
bull = _resolve_permissions(RegimeState(trend_state="UP", vol_state="EXPANDING",
                                        directional_bias="BULLISH"))
stats = {"STRONG": {"adv_inr": 9e8, "daily_vol_pct": 1.5},
         "WEAK": {"adv_inr": 6e7, "daily_vol_pct": 4.0},
         "MID": {"adv_inr": 3e8, "daily_vol_pct": 1.6}}
scored = score_candidates(cands, bull, stats, {"IT": 0.9, "FMCG": 0.2})
for c in scored:
    print(f"   {c['symbol']:7s} score={c['rank_score']:6.2f}")
check("sorted best-first", scored[0]["symbol"] == "STRONG")
check("scores bounded 0-100", all(0 <= c["rank_score"] <= 100 for c in scored))
check("counter-bias weak name ranks last", scored[-1]["symbol"] == "WEAK")
check("threshold can reject everything", apply_threshold(scored, threshold=99.9) == [])
check("threshold keeps qualifiers", len(apply_threshold(scored, threshold=50.0)) >= 1)

print()
print("=" * 72)
print("4. PORTFOLIO RISK (§5.3)")
print("=" * 72)
from stocks.services.portfolio_risk import (
    apply_portfolio_constraints, effective_bets, volatility_scalar, kelly_fraction,
)
n_eff = effective_bets(5, 0.6)
check("N_eff matches the audit's 1.47", abs(n_eff - 1.47) < 0.01, f"{n_eff:.2f}")
check("uncorrelated book gives full diversification",
      abs(effective_bets(5, 0.0) - 5.0) < 0.01)

many = [{"symbol": s, "rank_score": 90 - i} for i, s in
        enumerate(["HDFCBANK", "ICICIBANK", "AXISBANK", "SBIN", "TCS"])]
sectors = {s: "FINANCIAL" for s in ["HDFCBANK", "ICICIBANK", "AXISBANK", "SBIN"]}
sectors["TCS"] = "IT"
acc, rej = apply_portfolio_constraints(many, [], sectors, {}, max_positions=5)
fin = sum(1 for a in acc if sectors[a["symbol"]] == "FINANCIAL")
check("sector cap limits financials to 2", fin == 2, f"{fin} accepted")
check("non-financial still admitted", any(a["symbol"] == "TCS" for a in acc))
check("rejections carry a reason", all("reject_reason" in r for r in rej))

clusters = {"HDFCBANK": 0, "ICICIBANK": 0, "AXISBANK": 0, "TCS": 1, "SBIN": 0}
acc2, rej2 = apply_portfolio_constraints(many, [], {s: "MIXED" for s in sectors}, clusters, 5)
c0 = sum(1 for a in acc2 if clusters.get(a["symbol"]) == 0)
check("correlation cluster cap limits to 2", c0 == 2, f"{c0} from cluster 0")

check("vol targeting scales down in high vol", volatility_scalar(2.0, 1.0) == 0.5)
check("vol targeting scales up in low vol", volatility_scalar(0.5, 1.0) == 2.0)
check("Kelly gated below 300 trades", kelly_fraction(0.45, 2.0, n_trades=100) == 0.0)
k = kelly_fraction(0.45, 2.0, n_trades=500)
check("fractional Kelly active past the gate", 0 < k <= 0.25, f"f={k:.4f}")

print()
print("=" * 72)
print("5. PORTFOLIO BACKTESTER (§8.2)")
print("=" * 72)
from stocks.services.trading_engine.portfolio_backtest import (
    run_portfolio_backtest, BacktestConfig,
)
idx = pd.date_range("2026-01-01 09:15", periods=60, freq="5min")
up = pd.DataFrame({"Open": np.linspace(1000, 1040, 60), "High": np.linspace(1002, 1042, 60),
                   "Low": np.linspace(998, 1038, 60), "Close": np.linspace(1000, 1040, 60)},
                  index=idx)
dn = pd.DataFrame({"Open": np.linspace(1000, 960, 60), "High": np.linspace(1002, 962, 60),
                   "Low": np.linspace(998, 958, 60), "Close": np.linspace(1000, 960, 60)},
                  index=idx)
sigs = pd.DataFrame([
    {"timestamp": idx[2], "symbol": "WIN", "direction": "BUY",
     "entry": 1001.0, "stop_loss": 995.0, "target": 1015.0},
    {"timestamp": idx[2], "symbol": "LOSE", "direction": "BUY",
     "entry": 1001.0, "stop_loss": 995.0, "target": 1015.0},
])
res = run_portfolio_backtest(sigs, {"WIN": up, "LOSE": dn}, BacktestConfig())
print(f"   {res.summary()}")
m = res.metrics
check("both trades executed", m["n_trades"] == 2, str(m["n_trades"]))
check("winner and loser both recorded", 0 < m["win_rate"] < 1, f"{m['win_rate']:.0%}")
check("costs actually charged", m["total_costs"] > 0, f"Rs.{m['total_costs']:.0f}")
check("net is below gross", m["net_pnl"] < m["gross_pnl"],
      f"net={m['net_pnl']:.0f} gross={m['gross_pnl']:.0f}")
check("equity curve produced", res.equity_curve is not None and len(res.equity_curve) > 0)
check("drawdown computed", "max_drawdown_pct" in m)
check("exit reasons attributed", len(m["exit_breakdown"]) > 0, str(m["exit_breakdown"]))

flat = pd.DataFrame({"Open": [1000.0]*60, "High": [1001.0]*60,
                     "Low": [999.0]*60, "Close": [1000.0]*60}, index=idx)
res2 = run_portfolio_backtest(
    pd.DataFrame([{"timestamp": idx[1], "symbol": "FLAT", "direction": "BUY",
                   "entry": 1000.0, "stop_loss": 994.0, "target": 1012.0}]),
    {"FLAT": flat}, BacktestConfig(time_stop_bars=8))
check("time stop fires on a flat tape",
      res2.metrics["exit_breakdown"].get("TIME_STOP", 0) == 1,
      str(res2.metrics["exit_breakdown"]))

thin = run_portfolio_backtest(
    pd.DataFrame([{"timestamp": idx[1], "symbol": "WIN", "direction": "BUY",
                   "entry": 1000.0, "stop_loss": 999.5, "target": 1001.0}]),
    {"WIN": up}, BacktestConfig())
check("cost gate rejects sub-threshold target in backtest",
      thin.metrics["n_trades"] == 0, str(thin.metrics["n_trades"]))

print()
print("=" * 72)
print("6. VALIDATION FRAMEWORK (§8.2 / §8.3)")
print("=" * 72)
from stocks.services.trading_engine.validation import (
    walk_forward_windows, purged_kfold_indices, monte_carlo_drawdown,
    deflated_sharpe_ratio, sharpe_standard_error, minimum_track_record_length,
    parameter_stability,
)
wins = walk_forward_windows(pd.date_range("2024-01-01", "2025-12-31", freq="D"))
check("walk-forward windows generated", len(wins) > 5, f"{len(wins)} windows")
check("train precedes test", all(w.train_end <= w.test_start for w in wins))

folds = purged_kfold_indices(1000, n_splits=5, embargo_pct=0.01)
check("purged k-fold produces 5 folds", len(folds) == 5)
overlap = any(set(tr).intersection(set(te)) for tr, te in folds)
check("no train/test overlap", not overlap)
gap_ok = all(min(te) - max([i for i in tr if i < min(te)], default=-99) > 1
             for tr, te in folds if any(i < min(te) for i in tr))
check("embargo gap enforced", gap_ok)

mc = monte_carlo_drawdown([500, -300, 800, -250, 600, -400] * 20, n_sims=500)
print(f"   median maxDD={mc['median_max_dd_pct']:.1f}%  p95={mc['p95_max_dd_pct']:.1f}%")
check("Monte Carlo returns a DD distribution",
      mc["p95_max_dd_pct"] >= mc["median_max_dd_pct"])
check("probability of loss reported", 0 <= mc["prob_of_loss"] <= 1)

d1 = deflated_sharpe_ratio(1.5, n_trials=1, n_obs=500)
d50 = deflated_sharpe_ratio(1.5, n_trials=50, n_obs=500)
print(f"   DSR 1 trial={d1['deflated_sharpe_probability']:.3f}  "
      f"50 trials={d50['deflated_sharpe_probability']:.3f}")
check("DSR penalises multiple testing",
      d50["deflated_sharpe_probability"] < d1["deflated_sharpe_probability"])

se4 = sharpe_standard_error(0.5, 4)
check("SE matches the audit's ~0.53 at 4y", abs(se4 - 0.53) < 0.03, f"{se4:.3f}")
mtrl = minimum_track_record_length(0.5)
check("min track record is multi-year", mtrl > 5, f"{mtrl:.1f} years")

plateau = parameter_stability({1: 0.90, 2: 0.95, 3: 1.00, 4: 0.96, 5: 0.91})
peak = parameter_stability({1: 0.20, 2: 0.25, 3: 1.00, 4: 0.22, 5: 0.19})
check("plateau identified as robust", plateau["is_plateau"], plateau["verdict"])
check("peak flagged as overfit", not peak["is_plateau"], peak["verdict"])

print()
print("=" * 72)
print(f"RESULT: {'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
print("=" * 72)
if fails:
    sys.exit(1)
