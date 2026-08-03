# Short-Term & Long-Term Buy Engine — Institutional Upgrade Plan

> Companion to the CIO-style review conducted on 2026-07-26. This document turns that
> review's findings into an execution plan, phased by dependency and risk — not by
> calendar time. Do not start a phase before the one before it is done; several later
> phases assume earlier bugs are already fixed.
>
> Scope: `backend/stocks/services/pro_system_service.py`, `backend/stocks/services/trade_engine.py`,
> `backend/stocks/models.py`, `backend/stocks/services/live_signal_service.py`,
> `backend/stocks/views.py`, `frontend/src/pages/ProSystem.jsx`.

---

## How to read this plan

Each phase has:
- **Goal** — what changes about the system's behavior once the phase ships.
- **Tasks** — concrete edits, with file/function references from the review.
- **Depends on** — phases that must be complete first, and why.
- **Breaking change?** — whether it changes stored data shape, API response shape, or live signal behavior.
- **Definition of Done** — how to verify the phase actually worked, not just that code was written.

Phases 0-1 fix things that are actively wrong today (duplicate engines, fabricated data,
unenforced risk). Phases 2-4 add capability that doesn't exist yet (real fundamentals,
sector analysis, portfolio risk controls). Phase 5 is presentation. **Doing 2-5 before 0-1
would mean building new analysis on top of a foundation that's still corrupting itself.**

---

## Phase 0 — Stop the Bleeding

**Goal:** Remove active bugs and fabricated data. No new capability yet — this phase only
removes things that are currently wrong or dangerous.

**Depends on:** nothing. Start here.

**Breaking change?** No — removes dead code and fixes silent bugs; no consumer relies on
the fabricated fields or the dead function on purpose.

### Tasks

1. **Remove the wrong-market ticker.**
   `pro_system_service.py:73` — delete `"AAPL"` from `NIFTY500_FALLBACK`. Spot-check the
   rest of the list for stale/merged symbols (e.g. `HDFC`, merged into `HDFCBANK` in 2023).

2. **Delete dead, previously-buggy code.**
   `pro_system_service.update_pro_system_outcomes()` — confirmed zero callers. This
   function was already pulled out of the periodic-scanner path once because it caused
   duplicate Telegram alerts (see the removal comment in `live_signal_service.py:202-208`).
   Leaving it in the codebase is a landmine for a future refactor to silently re-wire it.
   Delete the function entirely.

3. **Stop fabricating fundamentals.**
   `pro_system_service._fetch_long_term_quality` (:388-401) and `get_pro_system_data`
   (:566-577) — remove the hardcoded `roe`, `debt_to_equity`, `profit_growth`, and
   `"sector": "Large Cap"` literals. Return `null`/omit these fields rather than a fake
   number. Rename `revenue_growth` to what it actually is (`price_performance_100d`) until
   Phase 2 wires in real revenue data.

4. **Fix the mislabeled market-cap field.**
   `_fetch_long_term_quality:391` — `"mcap": int(close * vol_20d)` is price × volume, not
   market cap. Rename to `liquidity_proxy` immediately (real market cap comes in Phase 2).

5. **Fix or remove the dead Risk-Reward check.**
   Both `_analyze_short_term` (`pro_system_service.py:229-234`) and `_compute_ai_score`
   (`trade_engine.py:247-253`): `target` is derived from the same multiplier used to build
   `sl_points`, so `rr_ratio` always equals that multiplier and the `< min_risk_reward_ratio`
   check can never reject anything. Either delete the check (it does nothing, don't pretend
   it's a gate) or decouple target-setting from the SL multiplier so it can actually filter.

6. **Fix the long-term stop/target override bug.**
   `trade_engine._scan_new_long_term_setups` (:1199-1211) overwrites the ATR-based
   `stop_loss`/`target` already computed by `scan_long_term_stocks()` with a flat
   `price * 0.5` / `price * 2.0` — a 50%-drawdown-before-stop is not a risk control for any
   holding period. Use the values already present in the `p` dict instead of recomputing.

### Definition of Done
- `AAPL` no longer appears anywhere in the scanned universe (grep confirms it).
- `update_pro_system_outcomes` no longer exists in the codebase.
- API responses for long-term picks contain no hardcoded `roe`/`debt_to_equity`/`profit_growth`/`sector` values — either real data (later phase) or `null`.
- `mcap` is gone/renamed; nothing downstream still reads a field called `mcap` expecting market cap.
- The RR check either does real filtering or has been removed — no dead conditionals left pretending to be safety gates.
- `_scan_new_long_term_setups` produces the same stop/target as `_fetch_long_term_quality` computed for that stock (no silent override).

---

## Phase 1 — Architectural Consolidation

**Goal:** One engine per signal category. No symbol can have two independently-managed
positions with conflicting entry/SL/target. No position is ever silently unmonitored.

**Depends on:** Phase 0 (no point consolidating around code that's about to be deleted).

**Breaking change?** Yes — changes which code path is authoritative for live short-term
signal creation, and changes long-term signal lifecycle behavior. Needs your sign-off on
which engine wins before starting (see recommendation below).

### Tasks

1. **Pick one short-term engine and retire the other's signal-creation path.**
   Today, `pro_system_service.scan_short_term_stocks()` (invoked via the
   `short_term_scan` cron action in `live_signal_service.py:184-187`) and
   `trade_engine.run_daily_scanner()` (the 10:00 AM APScheduler job) both create
   `ShortTermSignal` rows for the same universe, with different entry philosophy
   (immediate ACTIVE-at-close vs. scored PENDING-with-pullback-confirmation) and
   different dedup checks (one checks only `ACTIVE`, the other checks
   `PENDING/ACTIVE/TARGET1/TARGET2/REVIEW_REQUIRED`).

   **Recommendation:** keep `trade_engine.run_daily_scanner` / `_compute_ai_score` as the
   sole signal-creation path — it already has tiered targets (T1/T2/T3), an audit trail via
   `TradeHistory`, and config-driven thresholds. Change the `short_term_scan` cron action in
   `live_signal_service.py` to either call `trade_engine.run_daily_scanner()` instead, or be
   removed if the external cron-job.org trigger is redundant with the 10:00 AM scheduled job.
   Keep `pro_system_service.py`'s indicator helpers (`_get_ema`, `_compute_atr`, etc.) and
   `get_market_direction()` — those are shared utilities, not competing engines.

2. **Close the long-term monitoring gap.**
   `live_signal_service.update_signal_outcomes` explicitly excludes `category='long_term'`
   from any exit check (comment at :47-48: "must never be [auto-exited]"). Today that means
   a long-term position's stored `stop_loss`/`target` is decorative — nothing ever compares
   it to price. Add a periodic check (daily EOD is sufficient for a 1-2 year hold) that:
   - Compares current price to `stop_loss`/`target` and marks `HIT_TARGET`/`HIT_SL`, **or**
   - If you want true buy-and-hold with no hard stop, drop the `stop_loss`/`target` fields
     for this category entirely and instead check the stated qualitative `hold_rule`
     (e.g. "exit if trend breaks below 200 EMA") on the same cadence.

   Either is acceptable — what's not acceptable is storing a number that implies a risk
   plan while enforcing neither it nor the qualitative alternative.

3. **Deduplicate indicator implementations.**
   Three parallel Wilder-indicator implementations exist: `trade_engine._ema/_atr/_adx/_rsi`,
   `pro_system_service._get_ema/_compute_atr/_compute_adx`, and the equivalents in
   `signal_utils.py` used by the other engines. Consolidate into one shared module (e.g.
   `services/indicators.py`) imported by all three. This is mechanical but matters: a future
   bug fix to ADX smoothing in one copy will otherwise silently diverge from the other two.

4. **Delete the now-orphaned PENDING→pullback state machine**, if Task 1 removes its only
   caller. If instead the pullback-confirmation philosophy (wait for a bullish candle before
   activating) is the one you want to keep, make it the *only* entry path for short-term
   signals rather than a code path only some signals go through.

### Definition of Done
- Exactly one function creates `ShortTermSignal` rows in normal operation; confirmed by
  removing/redirecting the second caller and checking no new signals appear via the old path.
- A long-term `SignalHistory` row can transition to `HIT_TARGET`/`HIT_SL` (or a documented
  qualitative exit), verified against at least one manually-forced price scenario.
- Only one copy of EMA/ATR/ADX/RSI computation exists in the codebase; all three engines import it.
- No unreachable state-machine branches remain for signal statuses that are never set.

---

## Phase 2 — Fundamental & Institutional Data Foundation

**Goal:** The Long-Term engine's "quality" claim becomes true — backed by real financial
data, not placeholders. This is the highest-effort phase because it requires a data source
you don't currently have, but it's the prerequisite for everything the CIO review flagged
as "zero real fundamental analysis."

**Depends on:** Phase 0 (fabricated fields already removed, so there's a clean slot to fill)
and Phase 1 (one engine to wire the data into, not two).

**Breaking change?** Additive — new fields, no removal of existing behavior. Existing
consumers of the API keep working; new fields appear alongside.

### Tasks

1. **Choose and integrate a fundamentals data source.**
   You need, at minimum: revenue (quarterly + annual), EPS/PAT, ROE, ROCE, Debt-to-Equity,
   free cash flow, shares outstanding. Options: a paid fundamentals API (e.g. a
   screener.in/Tickertape-style aggregator with an API), or direct NSE/BSE corporate filing
   ingestion (XBRL) if you want to avoid a paid dependency. This is a build-vs-buy decision —
   flag it for a separate discussion before committing engineering time, since it drives the
   cost/complexity of every task below.

2. **Add fundamental fields to the schema.**
   Extend `Stock`/`StockDailyData` or add a new `StockFundamentals` model (recommended, to
   avoid overloading the daily-price table with quarterly-cadence data):
   `revenue_growth_yoy`, `revenue_growth_qoq`, `eps_growth_yoy`, `pat_growth_yoy`, `roe`,
   `roce`, `debt_to_equity`, `interest_coverage`, `free_cash_flow`, `shares_outstanding`,
   `market_cap`, `promoter_holding_pct`, `promoter_pledge_pct`, `institutional_holding_pct`,
   `pe_ratio`, `pb_ratio`, `sector`, `industry`, `last_results_date`, `next_results_date`.

3. **Wire real market cap.**
   Replace the `liquidity_proxy` placeholder from Phase 0 with `price × shares_outstanding`.
   Add a cap-tier bucket (large/mid/small) — this feeds position-sizing in Phase 4.

4. **Add sector/industry classification.**
   Map every scanned symbol to a real sector (NSE sector indices or GICS-equivalent).
   Compute sector-level relative strength (20/60-day sector return vs Nifty) separately
   from individual-stock relative strength — this is the piece currently missing entirely
   (today's "sector_score" is just stock RS, mislabeled).

5. **Extend institutional-activity capture.**
   You already fetch FII/DII flow data (`fii_dii_service.py`) but don't use it in scoring.
   Add per-stock shareholding-pattern ingestion (promoter %, institutional %, pledge %,
   quarter-over-quarter change) and a bulk/block-deal feed (NSE publishes this in the same
   bhavcopy family `bhavcopy_service.py` already pulls from).

6. **Add a results-calendar flag.**
   Ingest the NSE corporate-action/results calendar so the scanner can flag "results due in
   N days" — used in Phase 4 as a gap-risk guard.

### Definition of Done
- A `StockFundamentals` (or equivalent) record exists for every actively-scanned symbol,
  refreshed at least quarterly, with a visible "as of" timestamp.
- `revenue_growth`, `roe`, `debt_to_equity`, `sector` in the API response are real,
  traceable to a source, and differ across stocks (the current bug was that they were
  identical for every stock — verify they no longer are).
- FII/DII and promoter-holding data is queryable per symbol, even before Phase 3 wires it
  into scoring.

---

## Phase 3 — Scoring & Selection Algorithm Upgrade

**Goal:** Move from pure price/volume momentum to a composite institutional filter —
technical entry gated by fundamental quality, with sharper relative-strength and
base-formation logic.

**Depends on:** Phase 2 (there's no fundamental gate to add without fundamental data) and
Phase 1 (one scoring function to upgrade, not two).

**Breaking change?** Yes — changes which stocks qualify as signals. Expect the signal count
to drop initially (that's the intended effect — quality over quantity was the explicit brief).

### Tasks

1. **Add a fundamental gate before the technical filter runs.**
   A stock failing minimum revenue/EPS growth thresholds should never qualify, regardless of
   chart pattern — especially for the Long-Term engine, where "quality" is the entire premise.

2. **Replace binary RS with percentile ranking.**
   Instead of "did the stock beat Nifty over 20 days" (`trade_engine.py:209`,
   `pro_system_service.py:339-341`), rank each candidate's 20/60/120-day return against the
   full scanned universe and require a minimum percentile (IBD-style RS Rating, e.g. ≥70).

3. **Add base-formation / VCP quality scoring.**
   Beyond "near 52-week high or 20-day breakout," measure the number and depth of recent
   price contractions and volume dry-up before the breakout (Minervini VCP). This
   distinguishes a healthy consolidation from a random spike — both currently score
   identically under the "20d Breakout" label.

4. **Replace ₹-flat liquidity floor with turnover-based floor.**
   Replace the flat 100,000-share threshold with a minimum ₹-value average daily turnover
   (price × volume), so the filter scales correctly across price levels.

5. **Add valuation and gap-risk guards.**
   Reject candidates trading at extreme P/E vs. sector/historical median (Phase 2 data), and
   defer entry for candidates with quarterly results due within N days (Phase 2's
   results-calendar flag) — both currently absent.

6. **Rank-and-cap selection instead of first-N-that-pass.**
   Score the full qualifying pool, then select the top N by composite score subject to a
   sector-concentration cap (e.g. no more than 2 picks from one sector per scan) — prevents
   the current failure mode where all picks can coincidentally cluster in one industry.

### Definition of Done
- A candidate can be rejected on fundamental grounds alone, verified with a test case
  (strong chart, weak revenue growth → rejected).
- RS percentile is computed and logged for every candidate, not just a pass/fail boolean.
- Selected signals are never more than the configured per-sector cap from the same sector
  in a single scan.
- Signal volume is measured before/after — expect fewer, higher-conviction signals.

---

## Phase 4 — Portfolio-Level Risk Management

**Goal:** Move risk control from "implied by a per-trade stop-loss" to genuine
portfolio-level capital preservation — the review's top-line priority.

**Depends on:** Phase 1 (single engine — portfolio limits are meaningless if two engines
can each independently open positions) and Phase 2 (cap-tier data needed for tiered sizing).

**Breaking change?** Yes — can block signal creation that today would go through
unconditionally (by design: that's the point of a concentration/heat cap).

### Tasks

1. **Position sizing as % of a defined capital base**, replacing the fixed ₹5,000
   risk-per-trade assumption currently used only for retroactive P&L display
   (`get_pro_performance_report`, both `_aggregate_st`/`_aggregate_lt`). Enforce it at
   signal-creation time, not just at report-display time.

2. **Portfolio heat cap.** Sum of (entry-to-stop % × position weight) across all open
   positions, capped at a configured ceiling (e.g. 6-8% of capital at risk at any time).
   New signals that would breach the cap are queued/rejected, not silently created.

3. **Max concurrent positions and max-per-sector caps**, enforced in the scanner's
   signal-creation step (ties into Phase 3's rank-and-cap selection).

4. **Corporate-action awareness.** Verify Angel One's split/bonus adjustment convention on
   historical candles. If unadjusted, detect corporate actions on open positions and
   re-baseline stored `entry_price`/`stop_loss`/`target` accordingly — otherwise a split
   during a holding period can silently trigger a false stop-loss exit.

5. **Correlation check (stretch within this phase).** Before opening a new position, check
   its recent price correlation to existing open positions; flag/reject highly-correlated
   additions even if they're in nominally different sectors.

### Definition of Done
- A simulated scenario with N positions at the heat cap correctly blocks position N+1.
- No single sector can exceed its configured share of concurrent open positions.
- A test corporate-action event (real or simulated) does not produce a false stop-loss trigger.

---

## Phase 5 — Explainability & Presentation

**Goal:** Every signal — pass or fail — comes with a narrative a human can act on, not just
a composite number.

**Depends on:** Phases 2-3 (there's nothing meaningful to explain until the sub-scores and
fundamental gates from those phases exist).

**Breaking change?** No — purely additive to the API response and frontend.

### Tasks

1. **Surface existing sub-scores.** `_compute_ai_score` already computes
   `trend_score/momentum_score/volume_score/sector_score/risk_score` — `get_dashboard_data._fmt`
   (`trade_engine.py:1358-1394`) currently discards all of them except the composite
   `ai_score`. Include them in the API response.

2. **Expose `expected_holding_days`.** Already stored on `ShortTermSignal`, never returned
   by `_fmt` — add it.

3. **Add a templated thesis string.** Generate a one-line "why this passed" from the actual
   factors that fired (e.g. "Breakout above 52w high, RS rank 82nd percentile, revenue growth
   18% YoY, sector: IT (leading)") rather than free text.

4. **Add explicit R:R display.** Compute and show the real risk:reward ratio directly
   (entry/SL/target are already stored — this is a display calculation, not new data).

5. **Add confidence framing.** Translate the composite score into a calibrated confidence
   band (e.g. High/Medium/Low) with a stated basis, rather than a bare 0-100 number with no
   context for what "70" means.

6. **Frontend (`ProSystem.jsx`).** Display the above: sub-score breakdown, thesis line,
   confidence band, expected holding period, explicit R:R — replacing the current bare
   `AI: {score}` badge and empty `reason` column for short-term signals.

### Definition of Done
- Every active signal in the UI shows: composite score + sub-score breakdown, a one-line
  thesis, confidence band, expected holding period, and explicit R:R — verified by opening
  the Pro System page and checking a live signal.

---

## Sequencing Summary

| Phase | Focus | Can start after | Changes live signal behavior? |
|---|---|---|---|
| 0 | Remove active bugs & fabricated data | — | No |
| 1 | One engine per category, close monitoring gap | 0 | Yes |
| 2 | Real fundamental/sector/institutional data | 0, 1 | No (additive) |
| 3 | Composite scoring, sharper technical filters | 1, 2 | Yes (fewer, better signals) |
| 4 | Portfolio-level risk controls | 1, 2 | Yes (can block new signals) |
| 5 | Explainability in API + UI | 2, 3 | No |

Phase 0 should happen regardless of what you decide for everything after it — it's pure
bug removal. Phases 2-4 are the ones that require a real investment of time (data sourcing)
and a decision on build-vs-buy for the fundamentals feed; flag that decision before
scheduling those phases.
