# 03 — Option Selling Engine: Redesign & Multi-Structure Expansion

## 1. What exists today (verified, not assumed)

### 1.1 Greeks engine — `option_greeks_service.py`

Standard Black-Scholes, three functions: `calculate_greeks` (delta, gamma, theta only — **no vega, no rho as returned values**), `estimate_iv` (Newton-Raphson solve, computes vega internally as a solver step but discards it rather than returning it), `calculate_theoretical_premium`. Risk-free rate hardcoded `r=0.07`. No dividend yield term (irrelevant for index/near-term equity options, but worth noting if covered calls are added — ex-dividend dates matter for assignment risk there).

**Gap**: vega and rho are computed as intermediate values and thrown away. A portfolio-level vega exposure figure already exists downstream (`delta_hedge_service.py:2197`, `total_vega` — recomputes its own vega inline via a duplicate Black-Scholes formula rather than calling the shared `option_greeks_service`). This is a second, silent implementation of the same math that could drift from the first.

### 1.2 Strike selection — `delta_hedge_service.py`

- Target delta by time-of-day regime (`INTRADAY_DELTA_MORNING=0.25` → `INTRADAY_DELTA_AFTERNOON=0.32`, capped `INTRADAY_MAX_DELTA=0.40`), adjusted by IV regime (low IV → more conservative delta; panic IV → capped; high IV → slight bump).
- Expected-move-capped strike floor (`EXPECTED_MOVE_FLOOR_MULT=1.0`), scanning 3%-10% OTM in 0.5% steps for an equidistant CE/PE premium-balanced pair (`find_equal_premium_pair`).
- **IV rejection guard**: reject if live IV > 25% (`delta_hedge_service.py:733`).
- **Intraday range guard**: reject if today's high-low range already exceeds 1.5% (`:752`) — sound instinct (don't sell into an already-trending day), correctly implemented.
- **Gamma guard**: reject new entries at DTE≤1 (`FORCE_EXIT_DTE=1`), force-close existing positions at the same threshold.

### 1.3 What's missing entirely (net-new, not "improve")

- **POP (probability of profit)** — not computed anywhere. Delta is used as a rough POP proxy implicitly (this is a common simplification, but POP for a *strangle* — two legs — is not the same as either leg's delta; it needs the joint probability the underlying stays between both breakevens, not one leg's touch probability).
- **IV Rank / IV Percentile** — the engine only ever looks at *current* IV against a flat 25% threshold. It has no concept of whether today's IV is high or low *relative to that stock's own recent history*. Selling at 24% IV when a stock's 1-year IV range is 15-20% is a very different trade than selling at 24% IV when its range is 22-45%. This is arguably the single highest-value addition in this document.
- **Open Interest / OI-based liquidity screening** — strike selection currently filters on premium floor and bid-ask spread (`MAX_BID_ASK_SPREAD_PCT=0.05`) but never checks OI, meaning a technically-tradeable but thin contract can be selected.
- **Assignment risk modeling** — irrelevant today (no CSP/Covered Call exist), becomes mandatory the moment §3-4 below ship.

---

## 2. Redesigned Core Algorithm

### 2.1 Strike Selection — add IV Rank/Percentile as a pre-filter, ahead of the existing distance scan

```
iv_rank = (current_iv - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100
iv_percentile = % of trading days in the last year where IV was below current_iv
```

Requires a new time-series of daily ATM IV per symbol (doesn't exist today — nearest proxy is per-scan `live_iv` estimates that are computed and discarded, never persisted). **New requirement**: persist daily ATM IV snapshots (cheap: one row per symbol per day) so IV Rank/Percentile become computable. Use IV Rank as a *pre-filter before* the existing 25% flat cap, not a replacement for it — keep the flat cap as a hard safety ceiling (it exists because of a real incident — ASIANPAINT, per the code comment — don't remove a scar-tissue guard, layer on top of it) while adding "only sell when IV Rank > 50" as the actual edge-seeking filter.

### 2.2 POP — proper joint-probability calculation for two-legged structures

For a short strangle with call strike `Kc` and put strike `Kp`:

```
POP = 1 - N(d2_call) - N(-d2_put)     # probability underlying finishes between Kp and Kc at expiry
```

This is a real, if simplified (assumes lognormal, ignores early-exit dynamics), improvement over "delta as POP proxy" and should be surfaced on every candidate the scanner produces, not just computed for later reporting.

### 2.3 Liquidity/OI screening — add as a hard filter alongside the existing spread check

`min_oi_threshold` per contract, sourced from Angel One's instrument/quote data (already fetched for premium — OI is very likely already present in the same API response and simply unused; verify field name before assuming a new API call is needed).

### 2.4 Theta Yield & Margin Efficiency — already computed (`delta_hedge_service.py:1132`), extend

`daily_theta_yield = total_theta_sold * lot_size / margin_required` exists but uses the flawed flat 10% margin estimate (per `02_RISK_MANAGEMENT_ENGINE.md` §4.7 — fix there fixes this automatically once margin estimation is corrected). No change needed to the yield formula itself, only its input.

---

## 3. Multi-Structure Support — Design

Every structure below shares the same underlying primitives already built (`get_nse_option_strikes`, `find_strike_by_delta`, `calculate_greeks`, `estimate_iv`) — this is a strength worth preserving, not a rewrite. The redesign is a **strategy selection layer** on top of the existing strike-selection primitives, per structure:

### 3.1 Short Strangle (exists — `delta_hedge_service.py`)
No structural change; gets IV Rank filter (§2.1) and POP (§2.2) added, gets routed through the new `core/risk_engine` (per `02_RISK_MANAGEMENT_ENGINE.md`) instead of its own inline sizing.

### 3.2 Iron Condor (net new)
Short strangle + a long OTM wing on each side (buy further-OTM CE above the short CE, further-OTM PE below the short PE). **Directly addresses the previous review's undefined-risk finding** — caps max loss to `(wing_width - net_credit) * lot_size`, a known number at entry, instead of theoretically unlimited. Implementation reuses `find_strike_by_distance` (already exists, currently used only for a fallback OTM-distance mode) to select the long-wing strikes at a further distance than the short strikes.

### 3.3 Cash Secured Put (net new — was listed as a core strategy, has zero code today)
Sell a single OTM put, strike selected at a level the user is willing to own the stock at (this is a *portfolio-construction* decision, not a pure premium-max decision — strike selection should weight toward the swing/long-term engine's fundamental scores, per `04_SWING_TRADING_ENGINE.md`, rather than pure delta-targeting). Requires: (a) cash-secured margin check (100% of strike × lot_size available as cash, not just SPAN margin — a materially different and stricter capital requirement than the naked-selling margin model currently assumed everywhere else in the codebase), (b) assignment-handling logic (on assignment, the position becomes a long equity holding — needs to hand off to whatever holds long-term positions, currently `SignalHistory(category='long_term')`, another point where `01_PROJECT_ARCHITECTURE.md`'s unified-lifecycle recommendation pays off, since a CSP assignment is a *lifecycle transition between engines*, which today's two-separate-model-family design has no clean way to express).

### 3.4 Covered Call (net new)
Sell a call against an existing long equity holding. Requires knowing what's already held — today there is **no portfolio/holdings model at all** (confirmed: no `Position`/`Holding` model in `models.py`). This is a prerequisite, not just a nice-to-have — Covered Call cannot be built safely without first knowing the system's own record of what's owned, otherwise it risks suggesting/tracking a "covered" call against a position that was actually sold outside the system. See `05_PORTFOLIO_MANAGEMENT.md` §3 for the Holdings model this depends on.

### 3.5 Credit Spread (net new)
Single-sided version of the Iron Condor (§3.2) — a bull put spread or bear call spread. Same wing-selection primitive, one side only. Natural entry point for a *directional* (not pure premium-collection) options view, which nothing in the current system supports — everything today assumes a neutral/sideways thesis (`is_sideways_ok or is_within_va_ok` gates in `delta_hedge_service.py`). A credit spread engine should be allowed to trigger off `market_intelligence_service.py`'s directional signals instead of only the sideways-market gate.

### 3.6 Calendar Spread (net new)
Sell near-dated, buy longer-dated, same strike. This is the one structure that genuinely needs new infrastructure beyond what exists: it requires comparing IV *term structure* across two expiries, which the codebase has no concept of today (every existing calculation is single-expiry). Lower priority than 3.2-3.5 — sequence last in `10_IMPLEMENTATION_ROADMAP.md`.

### 3.7 Ratio Spread (net new)
Sell more contracts than bought (e.g., 1 long + 2 short at a further strike). Materially different risk profile (undefined risk beyond the second short leg) — given the previous review's core finding was *too much undefined risk already*, recommend building this **last**, and only once the Risk Engine (`02`) has proven itself on the defined-risk structures first. Flagging explicitly: this is the one structure in the requested list that arguably conflicts with the user's own stated objective ("NOT maximum profit... controlled drawdowns") — worth a direct conversation before building it, not just an assumption that everything on the requested list should ship.

### 3.8 Protective Hedge (net new — portfolio-level, not per-trade)
Buy index puts (NIFTY/BANKNIFTY) sized to offset portfolio-level delta/vega exposure computed by the Risk Engine's portfolio heat calculation. This is the direct implementation of "Black Swan Protection" from `02_RISK_MANAGEMENT_ENGINE.md` §5 — a standing hedge rather than (or in addition to) the VIX-triggered circuit breaker.

### 3.9 Dynamic Adjustment / Rolling (net new — applies across all structures)
Currently, when a short strangle is threatened (delta breach, e.g. `SHORT_DELTA_DANGER` referenced in imports), the only documented outcome is exit (`AUTO_EXIT_ON_DELTA_BREACH`). No rolling logic exists — rolling (closing a threatened leg and re-opening further OTM and/or further out in time) is standard professional practice for exactly this scenario and is currently entirely absent. Build as a decision layer that runs *before* the exit logic: "can this be rolled to a strike/expiry that restores acceptable delta and POP at a net credit (or acceptable net debit)? If yes, roll. If no, exit." This single addition would likely have changed the outcome of the CRUDEOIL loss from the previous review (a rolled position, if roll criteria were met, may have avoided the full SL hit) — worth prioritizing.

---

## 4. Professional Entry & Exit Rules (consolidated, across all structures)

**Entry** (all must pass, additive to existing guards):
1. IV Rank > threshold (structure-dependent — premium-selling structures want high IV Rank; Calendar spreads want the opposite: low IV Rank, since they benefit from IV *expansion*)
2. POP within acceptable band for account risk tier (see `02`, §3)
3. Liquidity/OI above threshold
4. Sector/correlation limits satisfied (see `02`, §4.4-4.5)
5. Margin/capital check passes against real `Account.available_margin`, not the flat 10% estimate
6. Not within `FORCE_EXIT_DTE` of expiry
7. Intraday range guard (existing, keep)
8. VIX circuit breaker not tripped (see `02`, §5)

**Exit** (evaluated continuously, not just at EOD — extend `live_signal_service.py`'s existing outcome-auditor pattern):
1. Target profit capture (existing `PROFIT_CAPTURE_PCT`/`INTRADAY_PROFIT_CAPTURE_PCT`, keep)
2. Stop-loss — **rupee-ceiling version from `02_RISK_MANAGEMENT_ENGINE.md` §3.3**, not pure percentage-of-premium
3. Roll evaluation (§3.9) before hard exit, where applicable
4. Gamma/DTE force-close (existing, keep)
5. Delta breach beyond dynamic-adjustment tolerance
6. Circuit breaker trip (portfolio-level, forces exit evaluation across all positions, not new entries only, if the breach is severe — see `02` §5 distinction between "halt new" and "close existing")

---

## 5. What NOT to change

The equidistant premium-balancing logic (`find_equal_premium_pair`) and the time-of-day adaptive delta targeting are both more sophisticated than typical retail tooling and showed no evidence of being the source of the CRUDEOIL loss or any other finding — preserve both as-is. The redesign in this document is additive (IV Rank, POP, OI, multi-structure, rolling, risk-engine integration), not a replacement of the core selection math, which is sound.
