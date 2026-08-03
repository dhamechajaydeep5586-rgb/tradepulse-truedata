# 07 — Backtesting Framework

## 1. What exists today (real, minimal, worth preserving the core idea)

`trading_engine/backtest.py` + `config.py`, exposed via `SignalBacktestView`. `run_backtest_for_signal()` replays **one signal** against **one candle series**, using `is_signal_active()` — the exact same function the live system uses to decide PENDING→ACTIVE transitions. This shared-logic property is the single most valuable thing to preserve: a backtest that uses different trigger logic than live trading is worse than no backtest, because it creates false confidence. Keep this property as sacred through every extension below.

**Notable discovery**: `config.py`'s `MARKET_RULES` dict includes a fully-specified `"option_selling"` entry (`avoid_windows=(10:30,13:30)`, `candidate_limit=60`, `max_signals=8`) — real configuration for a category that, per `01_PROJECT_ARCHITECTURE.md` §1.6, has no live scanner writing signals to it anymore. This is orphaned config from the phantom "Option Selling Sniper" feature — further evidence it existed and was removed incompletely, not just under-documented.

**Current limitation**: single-signal, single-instrument replay only. No portfolio-level simulation (concurrent positions competing for the same capital/margin), no walk-forward, no Monte Carlo, no regime tagging, no automated historical-data ingestion pipeline (the backtest takes a candle DataFrame as a direct argument — sourcing that DataFrame for a full historical run isn't built).

## 2. Extension Plan

### 2.1 Historical Data Pipeline (prerequisite for everything else)
Angel One's Candle API (already integrated, `angel_one_service.py`) has practical lookback limits for intraday granularity. For a genuine multi-year backtest, need either: (a) a paid historical data vendor (NSE itself sells historical bhavcopy/F&O data cheaply), or (b) accept EOD-only backtesting for periods beyond Angel One's intraday lookback, with intraday-granularity backtesting limited to the recent window Angel One actually provides. Be explicit about this constraint in every backtest report's methodology section — a backtest run on EOD data cannot validate an intraday-specific strategy's execution quality, only its overall direction.

### 2.2 Portfolio-Level Simulation (net new — the biggest gap)
Extend `run_backtest_for_signal` to `run_portfolio_backtest(signals: list, capital: float, risk_engine_config)`: replay multiple signals concurrently against a shared capital pool, enforcing the same `RiskEngine` limits (`02`) a live run would — position sizing, sector caps, daily loss circuit breakers all apply *during* the simulation, not just live. This is what makes the CRUDEOIL-style loss visible in a backtest *before* it happens live: a portfolio-level simulation with a hard rupee-loss cap per trade would show the capped outcome, not the actual −₹10,910 outcome, letting you validate the risk engine's design against history before trusting it with new capital.

### 2.3 Walk-Forward Testing
Split history into rolling train/test windows (e.g., optimize strike-selection parameters on months 1-6, validate unseen on month 7, roll forward). Purpose: detect overfitting to a specific historical period — directly relevant given the current live "74% win rate" is drawn from 2 days that could just as easily be a lucky window as a validated edge.

### 2.4 Monte Carlo Simulation
Resample the empirical trade-outcome distribution (once a large-enough sample exists, per `02_RISK_MANAGEMENT_ENGINE.md` §6) with replacement, thousands of paths, to produce a *distribution* of possible equity curves rather than one historical path. Directly feeds Risk of Ruin and Expected Drawdown as ranges, not point estimates — per `02`'s explicit warning against false-precision single numbers.

### 2.5 Stress Testing — Named Historical Regimes
Explicit test windows, tagged by regime, run against every strategy before it's trusted with real capital sizing changes:

| Regime | Window (illustrative — confirm exact dates against actual NSE calendar) | What it stresses |
|---|---|---|
| COVID Crash | Feb-Apr 2020 | Extreme gap risk, VIX spike beyond any current guard's assumptions, liquidity evaporation |
| 2022 Rate-Hike Bear | Jan-Jun 2022 | Sustained directional bear market — tests whether "sideways market" gates correctly avoid signals rather than false-triggering |
| Union Budget days | Early Feb, annually | Single-day gap/volatility events — tests the DTE/gamma guards specifically |
| RBI Policy days | Bi-monthly MPC dates | Same category, higher frequency, smaller average magnitude |
| Election results | e.g. May 2024 (national), various state election dates | Extreme single-day gap risk — arguably the single best stress test for "is the max-loss-per-trade cap (`02` §3.3) actually sufficient" |
| High VIX grind (not a crash — sustained elevated IV) | Identify via India VIX historical series >20 for extended periods | Tests whether IV Rank filtering (`03` §2.1) correctly identifies genuinely elevated conditions vs. the current flat 25% cap's blind spot |
| Low VIX grind | India VIX <12 sustained periods | Tests theta-yield assumptions when premium is thin — does the engine correctly go quiet rather than force marginal trades |

### 2.6 Performance Metrics — compute correctly, not just completely

- **Sharpe Ratio**: `(mean_return - risk_free_rate) / std_dev_return`, annualized correctly for the actual trade frequency (daily vs. per-trade basis matters — a common retail-backtest mistake is annualizing per-trade Sharpe as if it were daily, inflating the number substantially).
- **Sortino Ratio**: same as Sharpe but downside-deviation only — more informative than Sharpe for a strategy (short options) with deliberately asymmetric return distributions (many small wins, occasional larger losses) where Sharpe's symmetric-variance assumption understates the real risk.
- **Calmar Ratio**: CAGR / Max Drawdown — good complementary metric precisely because it's drawdown-anchored rather than variance-anchored, which matters more to a capital-preservation-focused user than Sharpe does.
- **Win Rate, Expected Value, Avg Win/Loss**: already computable from `SignalHistory.metadata['final_pnl']` per this engagement's own live-data pull — the framework for computing these correctly already exists (see the earlier "Statistical Analysis" section of this engagement's chat history for the exact query pattern); formalize it into `backtesting/metrics.py` rather than an ad hoc shell query.
- **Maximum Drawdown**: peak-to-trough on the simulated equity curve, both in absolute rupees and %, reported alongside *drawdown duration* (time to recover) — duration is frequently omitted and is often more behaviorally important than depth (a 15% drawdown that recovers in 2 weeks is a very different experience than one that takes 8 months).

## 3. Statistical Significance — the gate before any metric above is trusted

Explicit minimum-sample-size and regime-coverage requirements before a backtest result changes real position sizing (ties directly to `02` §3.2's Kelly-sizing gate):
- Minimum 100 closed trades per strategy before any win-rate-derived sizing decision.
- Coverage of at least one high-VIX and one low-VIX window (§2.5).
- Walk-forward validation (§2.3) showing the edge persists out-of-sample, not just in-sample.

If these aren't met, the backtest report should say so explicitly, in the report itself — "insufficient sample, do not use for sizing decisions" as a literal, visible line, not a footnote. This is the single behavior that would have caught the previous review's core finding (a 27-trade, 2-day "74% win rate") before it was ever presented as evidence of anything.
