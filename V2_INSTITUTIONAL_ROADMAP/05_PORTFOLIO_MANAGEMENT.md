# 05 — Portfolio Management System

## 1. The foundational gap: there is no portfolio

Confirmed by full read of `stocks/models.py` (11 models): none of them represent "what the user currently owns." `SignalHistory` and `ShortTermSignal` represent *signals* (recommendations with a lifecycle), not confirmed holdings. There is no `Position` or `Holding` model, no cash-balance tracking, no realized-vs-unrealized P&L ledger independent of a specific signal's own `pnl` field. This means:

- Every "portfolio" view in the current system (`PerformanceReports.jsx`, the Pro System dashboard) is really a **signal-outcome report**, not a portfolio view. It can tell you how past recommendations performed; it cannot tell you what you own right now, your actual cash position, or your actual realized-vs-unrealized split.
- Covered Calls (`03_OPTION_SELLING_ENGINE.md` §3.4) cannot be built without this.
- Diversification, sector allocation, beta, and correlation (all requested here) are all *portfolio-level* concepts that require knowing actual current holdings, not just historical signal outcomes.

This document is therefore mostly **net-new design**, not a redesign of something broken — the honest framing is "this doesn't exist yet," not "this exists and is wrong."

## 2. Data Model (new)

```python
class Holding(models.Model):
    """A confirmed position — distinct from a Signal, which is a recommendation."""
    symbol = models.CharField(max_length=20, db_index=True)
    asset_type = models.CharField(max_length=20)  # EQUITY, OPTION, FUTURE
    quantity = models.IntegerField()
    avg_entry_price = models.DecimalField(max_digits=12, decimal_places=2)
    current_price = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    source_signal_ref = models.CharField(max_length=100, null=True, blank=True)  # links back to originating SignalHistory/ShortTermSignal if applicable
    sleeve = models.CharField(max_length=20)  # LONG_TERM, SWING, OPTIONS_INCOME — see §6
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

class CashLedger(models.Model):
    """Simple running cash balance — capital in/out, not a full accounting system."""
    entry_type = models.CharField(max_length=20)  # DEPOSIT, WITHDRAWAL, REALIZED_PNL, FEES
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
```

`Holding.source_signal_ref` is deliberately a string, not a ForeignKey — because (per `01_PROJECT_ARCHITECTURE.md` §1.4) a holding might originate from either `SignalHistory` or `ShortTermSignal`, two unrelated model families. Forcing a single FK type here would require picking one, which is exactly the ambiguity that document flags. This is a pragmatic bridge until the unified lifecycle service exists — revisit once it does.

## 3. Portfolio Allocation Targets

The user's stated strategies map to distinct "sleeves," each with its own capital budget, not one undifferentiated pool:

| Sleeve | Source | Suggested capital band (starting point, tune per user risk tolerance) |
|---|---|---|
| Long-Term Investing | `04_SWING_TRADING_ENGINE.md`'s fundamental scanner, high-conviction only | 40-50% |
| Swing Trading | `04`'s technical/momentum engine | 15-20% |
| Options Income (strangle, IC, CSP, covered call) | `03_OPTION_SELLING_ENGINE.md` | 20-30% |
| Cash reserve | — | 10-15%, hard floor, never fully deployed |

The cash reserve floor is not a suggestion — it's the direct fix for a gap `02_RISK_MANAGEMENT_ENGINE.md` identifies: without one, a margin call during a black-swan event has no buffer. Enforce it as a `RiskEngine` check: no new signal (any sleeve) may be approved if it would push total deployed capital past `total_capital * (1 - min_cash_reserve_pct)`.

## 4. Rebalancing

Trigger-based, not calendar-based, given the mixed time horizons across sleeves: rebalance a sleeve when its actual allocation drifts more than a threshold (e.g., 5 percentage points) from its target — e.g., a strong options-income month that pushes that sleeve from 25% to 33% of capital should trigger either a deliberate reduction in new options signals or a deliberate increase in long-term/swing deployment, rather than silently letting one sleeve's success concentrate the portfolio's risk profile. This is a genuinely institutional discipline (most retail systems let a winning strategy silently overgrow its allocation) and directly prevents the concentration failure mode implied by the CRUDEOIL-loss finding at the portfolio level, not just the per-trade level.

## 5. Diversification, Sector Allocation, Beta, Correlation

- **Sector allocation**: aggregate `Holding.symbol → SectorMapping.sector` (new table, per `02_RISK_MANAGEMENT_ENGINE.md` §7) across *all* sleeves combined, not per-sleeve — a long-term HDFC Bank holding and a short strangle on ICICI Bank both count against "Financials" exposure.
- **Beta**: compute portfolio beta as the weighted average of each holding's beta vs. Nifty (60-day rolling regression — cheap, standard). Surface as a single portfolio-level number on the risk dashboard (`02` §8).
- **Correlation**: reuse the same rolling correlation matrix proposed in `02` §4.5 — one shared utility, not a portfolio-specific reimplementation.

## 6. Expected CAGR & Drawdown Analysis

**Explicit warning, consistent with the previous review's Statistical Analysis section**: do not compute a headline "Expected CAGR" number from the current ~2-day, 27-trade dataset and present it as a portfolio-level projection. Once `07_BACKTESTING_FRAMEWORK.md`'s historical simulation exists, CAGR and max drawdown should be *simulated ranges* (e.g., 5th/50th/95th percentile across Monte Carlo paths), not a single point estimate — a single "expected CAGR: 24%" number is exactly the kind of false precision this entire engagement exists to push back on.

## 7. Tax Considerations

India-specific: STCG (equity <1yr, options are always short-term for tax purposes) taxed differently from LTCG (equity >1yr). The Long-Term sleeve's exit logic should be tax-aware — flag positions approaching the 1-year mark where holding a few more days converts STCG to LTCG, since that's a real, quantifiable, zero-risk optimization currently entirely absent from any exit logic in the codebase (`trade_engine.py`'s EOD evaluation has no holding-period-aware tax check). This is a genuinely free improvement — no new data source needed, just a date comparison already available on every `Holding.opened_at`.

## 8. Portfolio Review Output (ties to `06_AI_RESEARCH_ENGINE.md`)

A scheduled (weekly) portfolio review job producing: current sleeve allocation vs. target, sector/correlation/beta snapshot, tax-lot aging summary (§7), and a plain-language summary generated by the AI research layer — but note per `06`, the AI layer must be **read-only** against this data, never able to directly rebalance or close a `Holding`.
