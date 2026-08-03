# 02 — Institutional Risk Management Engine

**Premise**: this document exists because the previous review found real money already lost to a risk-sizing gap (a −₹10,910 CRUDEOIL loss against a −₹620 average equity loss, one trade erasing 74% of two weeks' equity profit), and found the "Risk Management" UI panel on Pro System is disconnected from the backend entirely. Everything here is designed to make those two failure modes structurally impossible, not just less likely.

---

## 1. Current State (baseline, verified this session)

| Control | Where it lives today | What's actually wrong |
|---|---|---|
| "Total Capital" input | `frontend/src/pages/ProSystem.jsx:67` — `useState(500000)` | Never sent to any API. Purely client-side, resets on refresh. |
| "Max Risk (1.5%)" | `ProSystem.jsx:83` — `capital * 1.5 / 100` | Computed client-side, drives only the on-screen `qty` display, never the backend. |
| Backend position sizing (swing) | `pro_system_service.py:730,774` — `max_risk_inr = 5000` | Hardcoded flat rupee amount. Doesn't scale with account size at all. |
| Portfolio heat | `delta_hedge_service.py:2200` — `total_notional / 10_000_000 * 100` | Hardcoded ₹1 crore denominator. If real capital is ₹5,00,000, every heat % shown is understated ~20x. |
| Stop-loss (strangle) | `config_vol.py:8` — `COMBINED_SL_MULTIPLIER = 1.30` | Percentage-of-premium only. No absolute rupee ceiling. Lot-size asymmetry (CRUDEOIL=100, NATURALGAS=1250, equities vary) means realized rupee loss at SL is wildly different per instrument for the "same" 30% trigger. |
| Margin check | `delta_hedge_service.py:1126` — `margin_required = 0.10 * spot * lot_size` | A flat 10%-of-notional estimate used only to compute a theta-yield ratio. Never validated against real broker margin or account margin availability. Never gates trade count. |
| Sector/correlation limits | none found | Nifty-50 ranking (`delta_hedge_service.py:1604-1739`) has no sector or correlation constraint. All 10 concurrent equity slots could legally be one sector. |
| Max daily/weekly/monthly loss | none found | No circuit breaker anywhere in the codebase that halts new signal generation based on realized P&L over any time window. |
| Emergency stop | none found | The closest thing is `DeltaHedgeView.post()` — a manual "Reset Strategy" that cancels today's signals on demand. Not automatic, not loss-triggered. |

**Bottom line**: every number labeled "risk" in this system today is either decorative, hardcoded against the wrong base, or absent. This document specifies the replacement.

---

## 2. Design Principles

1. **One source of truth for capital.** A single `Account` record (new, minimal model — see §7) holds `total_capital`, updated explicitly by the user, read by every risk calculation. No component computes its own capital assumption.
2. **Risk checks happen before a signal is persisted, not displayed after.** The `RiskEngine` is a gate in the signal-creation path (`core/risk_engine`, per `01_PROJECT_ARCHITECTURE.md` §3.2), not a dashboard widget.
3. **Every strategy speaks the same risk currency: rupees at risk, as % of capital.** Instrument-specific quirks (lot size, premium level) get normalized *before* comparison, never after.
4. **Fail closed.** If the risk engine cannot compute a confident number (missing margin data, stale price), it blocks the signal rather than allowing it through with a default.
5. **Layered limits, not one number.** Per-trade, per-day, per-week, per-month, per-sector, and portfolio-wide — a single "portfolio heat %" is necessary but not sufficient, per the CRUDEOIL evidence (portfolio heat can look fine in aggregate while one position is sized 17x larger than typical).

---

## 3. Position Sizing

### 3.1 Fixed-Fractional (baseline, replaces the current hardcoded ₹5,000)

```
risk_per_trade_rupees = account.total_capital * risk_pct_per_trade
qty = floor(risk_per_trade_rupees / (entry_price - stop_loss_price))
```

`risk_pct_per_trade` defaults to 1.0-1.5% (matches the UI's existing 1.5% assumption — validate this is the number the user actually wants as policy, don't just inherit it because it was already in the UI). This alone fixes the ₹5,000-flat-amount bug (`pro_system_service.py:730`).

### 3.2 Kelly Criterion — why it's requested and why it should NOT be used at full value

Kelly sizing (`f* = (bp - q) / b`, where `b`=win/loss ratio, `p`=win probability, `q`=1-p) requires a *statistically reliable* `p` and `b`. Per the previous review's Statistical Analysis section: **the strangle engine has 27 trades over 2 days, and the swing engine has zero closed trades ever.** Feeding Kelly a win-rate estimate from n=27, single-regime data produces a position size that is confidently wrong — full Kelly is known to be extremely sensitive to estimation error in `p`, and will oversize aggressively on a lucky streak.

**Recommendation**: implement Kelly as a *reference calculation only*, displayed alongside fixed-fractional sizing, and gate its use as the actual sizing method behind a minimum sample-size threshold (recommend: 100+ closed trades, spanning at least 2 distinct India VIX regimes — see `07_BACKTESTING_FRAMEWORK.md` for how "regime" is defined). Until that threshold is met, use **Half-Kelly at most, and only as an upper bound check** — i.e., fixed-fractional sizing, capped so it never exceeds half-Kelly's suggested size, never sized *up* to Kelly's suggestion.

### 3.3 Instrument normalization (the direct CRUDEOIL fix)

```
max_loss_rupees_per_trade = account.total_capital * max_risk_pct_per_trade   # one number, all instruments
sl_distance_points = max_loss_rupees_per_trade / lot_size                    # solved backward, per instrument
```

Instead of "30% combined premium expansion, whatever that means in rupees for this lot size," the SL is derived FROM the rupee cap, not the other way around. For CRUDEOIL (lot=100) this produces a materially tighter percentage SL than for a small-lot equity — which is exactly correct, because CRUDEOIL's larger lot size means the same percentage move is a larger rupee swing.

---

## 4. Portfolio-Level Limits

### 4.1 Portfolio Heat (corrected)

```
portfolio_heat_pct = total_notional_at_risk / account.total_capital * 100
```

Replace the hardcoded `10_000_000` at `delta_hedge_service.py:2200` with `account.total_capital`. Display a hard ceiling (recommend 60-80% max heat for a premium-selling book that also holds long-term equity — leaves room for margin calls and doesn't assume 100% of capital is deployable to short options simultaneously).

### 4.2 Max Daily / Weekly / Monthly Loss (circuit breakers — does not exist today, net new)

```
daily_realized_pnl = sum(final_pnl for signals closed today)
if daily_realized_pnl <= -1 * (account.total_capital * max_daily_loss_pct):
    halt_new_signal_generation(reason="daily loss limit hit")
    notify_user(severity="critical")
```

Same pattern at weekly and monthly granularity, with monotonically looser thresholds (e.g., daily 2%, weekly 5%, monthly 8% — these are starting points to tune, not prescriptions; the important part is the mechanism, which doesn't exist at all today). Once tripped, **new signal generation halts** (existing PENDING/ACTIVE positions still get their normal exit logic — a circuit breaker stops new risk, it doesn't panic-close existing risk, which would itself be a risk event).

### 4.3 Position Limits

- Max concurrent positions per instrument class (equity strangle, commodity strangle, swing) — commodity should have a *tighter* cap than equity given the lot-size-driven tail risk found in evidence.
- Max concurrent positions, portfolio-wide — a hard ceiling independent of the existing `HEDGE_MAX_SIGNALS`/`MAX_EQUITY_SIGNALS` constants, expressed as "no more than N% of capital deployed across all strategies simultaneously," so a swing-engine position and a strangle-engine position count against the *same* budget rather than each engine independently assuming it owns 100% of capital.

### 4.4 Sector Limits (net new — nothing exists today)

```
max_positions_per_sector = 3   # tunable
```

Requires a sector-mapping table (see §7) since today `sector` is only a cosmetic label in `pro_system_service.py`, never a real GICS/NSE-sector-classification lookup. Enforced at the same ranking step where `delta_hedge_service.py:1731` currently sorts candidates purely by VWAP/VA score — add a sector-cap filter into that same pass.

### 4.5 Correlation Limits

Sector caps catch the obvious case (don't sell strangles on 5 banks at once); correlation catches the non-obvious case (a "diversified" basket that's still 0.85+ correlated on risk-off days). Minimum viable version: maintain a rolling 60-day return correlation matrix for the Nifty-50 universe (cheap to compute, refresh weekly), and reject a new candidate if its correlation to any *already-open* position exceeds a threshold (e.g., 0.7). This is a v2.1 refinement — ship sector limits first (cheaper, catches most of the same risk), add correlation once sector limits are live and validated.

### 4.6 Volatility Limits

Already partially exists (`MAX_LIVE_IV = 0.25` in both `delta_hedge_service.py` and `groww_free_service.py`) — this is good and should be kept, but should become **regime-aware** rather than a flat constant: the correct max-IV threshold in a genuinely high-VIX market period is different from a low-VIX grind. Tie this to India VIX level (fetch via NSE, already have the NSE-API-access pattern in `signal_utils.py`) rather than a single hardcoded 25% for all conditions.

### 4.7 Margin Limits (does not exist today as a real check)

Replace the flat `0.10 * spot * lot_size` estimate with an actual SPAN+exposure margin call to Angel One's margin API (if available) or a more accurate estimate (NSE publishes indicative margin requirements per contract). Gate new signal creation on `margin_required <= account.available_margin`, where `available_margin` is a real, user-updatable field, not assumed.

---

## 5. Emergency Stop & Circuit Breakers

- **Manual emergency stop** (extend the existing `DeltaHedgeView.post()` "Reset Strategy" pattern — it already proves the mechanism works, generalize it to all engines behind one endpoint): cancel all PENDING signals, flag all ACTIVE signals for manual review, halt all scanners, across every engine, with one action.
- **Automatic circuit breakers** (§4.2) trip on realized loss thresholds.
- **Black Swan Protection**: a specific, named trigger — India VIX crossing an extreme threshold (e.g., >35, roughly where it sat during the 2020 COVID crash and other genuine dislocations) intraday halts *all* new naked-option-selling signal generation platform-wide, regardless of per-trade heat, because vol regime changes invalidate the IV/EM assumptions every strike-selection calculation in `delta_hedge_service.py` depends on. This is cheap to implement (one VIX check, one global flag) and addresses the single scariest tail-risk scenario for a short-premium book.

---

## 6. Risk of Ruin & Expected Drawdown

Both require the same input the Statistical Analysis in the previous review flagged as currently insufficient: a real win-rate/avg-win/avg-loss distribution from enough independent trades. **Do not compute these from the current 27-trade sample and present them as decision-grade numbers** — that would be the exact "guess dressed as statistics" this framework exists to prevent. Once `07_BACKTESTING_FRAMEWORK.md`'s historical + Monte Carlo infrastructure exists, Risk of Ruin should be computed via Monte Carlo simulation (resample the empirical trade distribution with replacement, thousands of runs, track % of runs that breach a ruin threshold e.g. −50% capital) rather than the classical closed-form Kelly-adjacent formula, because the closed-form version assumes a stationary win-rate that this system's own data (74% win rate driven mostly by one 2-day window) doesn't support assuming.

---

## 7. Data Model (new)

```python
class Account(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    total_capital = models.DecimalField(max_digits=14, decimal_places=2)
    available_margin = models.DecimalField(max_digits=14, decimal_places=2, null=True)
    max_risk_pct_per_trade = models.DecimalField(max_digits=5, decimal_places=2, default=1.5)
    max_daily_loss_pct = models.DecimalField(max_digits=5, decimal_places=2, default=2.0)
    max_weekly_loss_pct = models.DecimalField(max_digits=5, decimal_places=2, default=5.0)
    max_monthly_loss_pct = models.DecimalField(max_digits=5, decimal_places=2, default=8.0)
    max_portfolio_heat_pct = models.DecimalField(max_digits=5, decimal_places=2, default=70.0)
    circuit_breaker_tripped_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

class SectorMapping(models.Model):
    symbol = models.CharField(max_length=20, unique=True, db_index=True)
    sector = models.CharField(max_length=50, db_index=True)  # real NSE/GICS classification, not a cosmetic label

class RiskEvent(models.Model):
    """Audit trail — every time the risk engine blocks or allows a signal with a nonzero risk finding."""
    signal_ref = models.CharField(max_length=50)   # symbol+category+timestamp, not an FK — spans both model families per 01's finding
    event_type = models.CharField(max_length=30)   # BLOCKED_MARGIN, BLOCKED_HEAT, BLOCKED_SECTOR, CIRCUIT_BREAKER, ALLOWED_WITH_WARNING
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
```

`RiskEvent` matters as much as the enforcement itself — without an audit trail, "why did the system let a 6th financial-sector strangle through" is unanswerable after the fact. Every risk-engine decision, allow or block, gets logged.

---

## 8. Risk Dashboard (backend contract — UI detail in `09_UI_UX_DESIGN.md`)

New endpoint `GET /api/stocks/risk-dashboard/` returning:

```json
{
  "account": {"total_capital": 500000, "available_margin": 380000},
  "portfolio_heat_pct": 34.2,
  "daily_pnl": -2100, "daily_loss_limit_pct": 2.0, "daily_loss_used_pct": 0.42,
  "weekly_pnl": 8700, "monthly_pnl": 15200,
  "sector_exposure": {"Financials": 2, "IT": 1, "Energy": 1, "Pharma": 3},
  "circuit_breaker_status": "ARMED",
  "recent_risk_events": ["..."]
}
```

This single endpoint is what makes the fictional "Max Risk (1.5%)" display in today's `ProSystem.jsx` (§1) become real — the frontend renders *this* number instead of computing its own client-side guess.

---

## 9. Explicit anti-pattern to avoid: don't let this become another decorative layer

The reason this document exists is that the previous risk "system" was decorative — it looked like risk management in the UI without being connected to anything. The single acceptance criterion that matters most for this entire document: **every number the Risk Dashboard shows must be traceable to a value the `RiskEngine` actually used to allow or block a real signal**, verified by an integration test that asserts a signal creation call is rejected when a limit is exceeded (see `10_IMPLEMENTATION_ROADMAP.md` Phase 2 testing checklist). If that test doesn't exist, assume this document's intent has not been met, no matter how complete the dashboard UI looks.
