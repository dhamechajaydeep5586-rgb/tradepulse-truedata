# 10 — Master Implementation Roadmap

This document sequences documents 01-09 into dated, gated phases. Each phase lists what changes, what it depends on, how risky it is, and — critically — how to know it's actually done, not just started. Per the "no code yet" instruction governing this entire roadmap, no phase below has been implemented; this is the plan for implementing them.

---

## Phase 1 — Critical Cleanup (no new features, pure debt removal)

**Objective**: remove the specific confusions documented in `01_PROJECT_ARCHITECTURE.md` §2 before building anything new on top of them.

**Files affected**: `pro_system_service.py` (delete ~460 dead lines, §2.1), `models.py` (delete `IntradaySignal`, §2.7/`08` §7), `CLAUDE.md` (correct the fictional Option Selling Sniper section, §1.6), `short_strangle_scanner.py` (decide: delete, document as manual-override, or promote — do not leave in limbo), management commands (add a README documenting which are cron-scheduled vs. manual-only, §2.3).

**Database changes**: one migration deleting `IntradaySignal`. Nothing else.

**API changes**: none.

**Frontend changes**: none.

**Testing checklist**:
- [ ] Confirm `pro_system_service.get_pro_performance_report` (the one live function in that file) still works after dead code removal — it must not accidentally import something being deleted.
- [ ] Full regression pass on `/pro-system/`, `/delta-hedge/`, `/live-signals/`, `/commodity-signals/` — nothing here should change behavior, so any observed behavior change is a regression.
- [ ] Confirm `IntradaySignal` table has zero rows before dropping it (belt-and-suspenders, even though code shows zero writes).

**Acceptance criteria**: codebase has zero unreachable trading-logic code paths; `CLAUDE.md` describes only features that actually exist; every management command's purpose and trigger mechanism is documented.

**Estimated complexity**: Low (deletion + documentation, no new logic).

**Dependencies**: none — this can start immediately.

**Risk level**: Low, but not zero — deleting code that *looks* related always carries a chance of an unnoticed import. Mitigate with the regression checklist above.

**Rollback strategy**: standard git revert; since no live behavior should change, a rollback is simply "undo the commit," no data migration concerns beyond the one model deletion (which itself should be trivially reversible if caught before the next migration is applied on top of it).

**Definition of Done**: a fresh engineer (or AI session) reading `CLAUDE.md` and the codebase side by side finds zero discrepancies of the kind found in this engagement's own audit.

---

## Phase 2 — Risk Engine (`02_RISK_MANAGEMENT_ENGINE.md`)

**Objective**: make risk management real. This is the highest-priority *new* work — it directly addresses the only finding from the original review with an actual realized loss attached to it.

**Files affected**: new `core/risk_engine/` package; `delta_hedge_service.py` (route signal creation through the new engine instead of inline sizing); `pro_system_service.py`/`trade_engine.py` (same); `ProSystem.jsx` (remove client-side capital fiction).

**Database changes**: `Account`, `SectorMapping`, `RiskEvent` (per `02` §7 / `08` §2).

**API changes**: `/api/risk/dashboard/`, `/api/risk/account/` (per `08` §3.2).

**Frontend changes**: Risk Dashboard tile on Dashboard home (`09` §3.1), rewired Pro System risk panel (`09` §3.2).

**Testing checklist**:
- [ ] Integration test: signal creation is rejected when portfolio heat exceeds the configured max (the single acceptance criterion `02` §9 calls out as the one that matters most).
- [ ] Integration test: signal creation is rejected when sector cap is reached.
- [ ] Integration test: circuit breaker halts new signal generation after simulated daily-loss-limit breach, and does NOT force-close existing positions (verify the "halt new, don't panic-close existing" distinction from `02` §4.2/§5).
- [ ] Backtest the CRUDEOIL scenario from the original review against the new rupee-ceiling stop-loss — confirm the simulated loss is capped, not −₹10,910.
- [ ] Verify `RiskEvent` audit log captures every block/allow decision during a full trading day dry run.

**Acceptance criteria**: every number on the new Risk Dashboard is traceable to a value the engine actually used in a real allow/block decision (not a parallel display calculation).

**Estimated complexity**: Medium-High — this is genuinely new architecture (`core/risk_engine`), not a refactor, and it becomes a required dependency for every subsequent engine change.

**Dependencies**: Phase 1 (cleaner codebase to integrate against, especially for `pro_system_service.py`/`trade_engine.py` — know which one is live before wiring risk checks into it).

**Risk level**: Medium. A bug here that's too permissive silently reintroduces the exact problem this phase exists to fix; a bug that's too restrictive blocks legitimate trading. Mitigate with the integration tests above, run against historical data before going live.

**Rollback strategy**: feature-flag the risk engine's *enforcement* (allow it to run in shadow/log-only mode first — compute and log what it would have blocked, without actually blocking, for a trial period) before making it authoritative. This is the safest rollout pattern for exactly this kind of change.

**Definition of Done**: shadow mode has run for at least 2 weeks of live market days with zero unexplained discrepancies between what the engine would have blocked and what a human reviewer agrees should have been blocked, before flipping to enforcing mode.

---

## Phase 3 — Portfolio Engine (`05_PORTFOLIO_MANAGEMENT.md`)

**Objective**: build the missing "what do I actually own" layer.

**Files affected**: new `core/portfolio/` package; new Portfolio page + components.

**Database changes**: `Holding`, `CashLedger` (`05` §2 / `08` §2).

**API changes**: `/api/portfolio/holdings/`, `/api/portfolio/allocation/` (`08` §3.2).

**Frontend changes**: new Portfolio page (`09` §3.4).

**Testing checklist**:
- [ ] Manual reconciliation: for a sample of recent closed signals, confirm a `Holding` record would have been created/closed at the correct times had this been live.
- [ ] Sleeve-drift detection test: simulate an options-income-heavy month, confirm the rebalance flag triggers at the configured threshold (`05` §4).
- [ ] Tax-lot aging: confirm a holding approaching 365 days is flagged before, not after, the LTCG boundary passes.

**Acceptance criteria**: at any point, `/api/portfolio/holdings/` matches the user's actual broker-side positions (validated by manual spot-check against the Angel One account, since this system doesn't yet have an automated broker-position-reconciliation feed — flag that as a real limitation, not a solved problem, at this phase).

**Estimated complexity**: Medium.

**Dependencies**: Phase 2 (sleeve allocation limits are enforced by the risk engine).

**Risk level**: Low-Medium — this is additive (new models, new page), doesn't modify existing signal-generation logic.

**Rollback strategy**: standard — new models/endpoints can be disabled via feature flag without affecting existing engines.

**Definition of Done**: a user can answer "what do I own, in which sleeve, and how is my portfolio allocated right now" from the UI, where today the honest answer is "the system doesn't know."

---

## Phase 4 — Option Selling Engine Redesign (`03_OPTION_SELLING_ENGINE.md`)

**Objective**: IV Rank/Percentile, POP, OI screening, and — the largest scope item — Iron Condor, CSP, Covered Call, Credit Spread as real, shippable structures.

**Files affected**: `delta_hedge_service.py` (extend, don't rewrite — §5 of `03` is explicit about what NOT to change), new `engines/option_income/` structure-specific modules.

**Database changes**: `IVSnapshot`, `FundamentalSnapshot` isn't needed here but is listed for Phase 5 — `IVSnapshot` only, here (`08` §2).

**API changes**: `/api/option-income/candidates/` (`08` §3.2).

**Frontend changes**: new Option Income page with per-structure tabs (`09` §3.3).

**Testing checklist**:
- [ ] Iron Condor: verify max loss is a known, capped number at entry (the core claim of this structure) via a unit test constructing a worst-case underlying move.
- [ ] CSP: verify cash-secured margin check (100% of strike × lot, not SPAN) is enforced distinctly from naked-selling margin.
- [ ] Covered Call: **blocked on Phase 3** — cannot ship until `Holding` model exists to know what's covered. Do not build this structure before Phase 3 lands.
- [ ] IV Rank: verify against at least 45 days of collected `IVSnapshot` data before trusting it (per `08` §7's backfill note) — and confirm the UI is honest about limited-history rank when applicable.
- [ ] Regression: existing strangle engine's win rate/behavior on a replayed historical week is unchanged by the IV Rank *addition* (it's a pre-filter, existing logic below it should be untouched).

**Acceptance criteria**: at least 3 of the 5 highest-priority structures (Strangle-existing, Iron Condor, CSP) are live; Covered Call gated correctly behind Phase 3; Calendar/Ratio spreads explicitly deferred per `03` §3.6-3.7's sequencing recommendation.

**Estimated complexity**: High — this is the largest single scope item across all 10 phases.

**Dependencies**: Phase 2 (risk engine gates every new structure's entry), Phase 3 (for Covered Call specifically).

**Risk level**: Medium-High for Iron Condor/CSP (new capital-at-risk logic); explicitly deferred/High risk for Ratio Spread — per `03` §3.7, revisit whether to build it at all given it conflicts with the user's stated capital-preservation objective.

**Rollback strategy**: each structure ships behind its own feature flag / tab — a problem with Iron Condor doesn't require rolling back CSP.

**Definition of Done**: for each shipped structure, a backtest (Phase 7, or the minimal single-signal replay if Phase 7 isn't done yet) exists showing it behaves as designed against at least one historical stress window from `07` §2.5.

---

## Phase 5 — Swing Trading Engine Redesign (`04_SWING_TRADING_ENGINE.md`)

**Objective**: real fundamental scoring (or explicit removal of the fake placeholder), delivery-%-conviction scoring, sector rotation, split breakout/pullback detectors.

**Files affected**: consolidate `trade_engine.py` (live) with salvaged logic from dead `pro_system_service.py` code (§8 of `04`) into `engines/swing/`.

**Database changes**: `FundamentalSnapshot` (`04` §3 / `08` §2).

**API changes**: `/api/swing/candidates/`.

**Frontend changes**: updated scoring display — surface sub-scores (technical/fundamental/institutional/etc.), not just one composite number, so the fundamental-score fix is visible/verifiable to the user, not just internal.

**Testing checklist**:
- [ ] Confirm `fundamental_score` is either real (sourced from `FundamentalSnapshot`) or explicitly weighted to 0 — a hardcoded 10.0 must not survive this phase under any composite weighting.
- [ ] Confirm delivery-% is read and contributes to scoring (currently fetched, unused — verify the fix actually wires it in, not just adds a TODO).
- [ ] Side-by-side comparison of `trade_engine.py`'s live funnel vs. the salvaged `pro_system_service.py` funnel on the same historical day's data — pick whichever produces better-justified candidates, document why.

**Acceptance criteria**: every input the composite score claims to use is traceable to real data, per the table in `04` §2.1 — no silent placeholders.

**Estimated complexity**: Medium.

**Dependencies**: Phase 1 (must know definitively which engine is live before consolidating).

**Risk level**: Medium — changing scoring weights changes which stocks get surfaced; validate against Phase 7 backtesting before fully replacing the current composite.

**Rollback strategy**: ship the new composite alongside the old one (both computed, old one authoritative) for a trial period, compare outputs, cut over only once validated.

**Definition of Done**: a stock's displayed score can be decomposed, on demand, into its real component inputs and their source data timestamps — full traceability, zero magic numbers.

---

## Phase 6 — AI Research Engine (`06_AI_RESEARCH_ENGINE.md`)

**Objective**: extend the existing, working `ai_insight_service.py` pattern to trade review, portfolio review, and (if news source is built) sentiment.

**Files affected**: `insights/services/ai_insight_service.py` (extend), new prompt template store (`06` §4).

**Database changes**: none required beyond what Phases 2-5 already added (AI layer reads existing tables).

**API changes**: `/api/ai/trade-review/<signal_ref>/`, `/api/ai/portfolio-review/`.

**Frontend changes**: trade review surfaced in relevant signal detail views; portfolio review on the new Portfolio page; visual "AI analysis" tagging (`09` §5) to maintain the read-only-boundary distinction visibly.

**Testing checklist**:
- [ ] Verify (by code review, not just testing) that the AI service layer has zero import of any write-path function on `SignalHistory`, `ShortTermSignal`, or `Holding` — the read-only boundary from `06` §2 should be enforceable by inspection, not just convention.
- [ ] Verify graceful degradation (existing `_generate_local_insight` fallback pattern) is replicated for the new trade-review/portfolio-review features, not just the original daily insight.
- [ ] Confirm the Claude model string is current (not deprecated) before this phase's launch.

**Acceptance criteria**: AI-generated content is clearly, visually distinguishable from system-computed numbers everywhere it appears (§9 above / `09` §5).

**Estimated complexity**: Medium — mostly extending a proven pattern, not building new infrastructure.

**Dependencies**: Phases 2-5 (needs real risk/portfolio/scoring data to analyze — an AI review of fictional risk numbers would just be well-written nonsense).

**Risk level**: Low (read-only by design) — the main risk is cost (API usage) and prompt-quality drift, not capital risk.

**Rollback strategy**: trivial — disable the feature flag, existing engines are entirely unaffected since nothing depends on the AI layer's output for execution.

**Definition of Done**: a trade review, run against a real closed signal, is judged by the user as genuinely useful (not generic) — same bar this engagement's own reviews were held to.

---

## Phase 7 — Backtesting Framework (`07_BACKTESTING_FRAMEWORK.md`)

**Objective**: portfolio-level simulation, walk-forward, Monte Carlo, named stress windows.

**Files affected**: extend `trading_engine/backtest.py` → `backtesting/` package (per `01` §3.2's promotion plan).

**Database changes**: `BacktestRun` (`07` §2 / `08` §2).

**API changes**: `/api/backtest/run/`, `/api/backtest/results/<id>/`.

**Frontend changes**: new Backtest page (`09` §3.5), with the mandatory statistical-significance warning treatment.

**Testing checklist**:
- [ ] Confirm portfolio-level backtest correctly enforces Phase 2's risk engine limits *during* simulation, not just live.
- [ ] Run all 7 named stress windows (`07` §2.5) against the current strangle engine, document results even if unflattering — that's the point.
- [ ] Confirm Sharpe/Sortino annualization is computed correctly (verify against a known reference calculation, not just "the code runs").
- [ ] Confirm a backtest run with <100 trades or <2 VIX regimes visibly displays the "insufficient sample" warning — this is a hard acceptance gate, not optional polish.

**Acceptance criteria**: the framework can answer, with an honest confidence caveat, "how would each current strategy have performed during COVID, the 2022 bear market, and the last 3 Union Budget days" — the exact question this whole 10-document roadmap exists to eventually let the system answer credibly.

**Estimated complexity**: High — Monte Carlo and walk-forward are genuinely new quant infrastructure, not extensions of simple logic.

**Dependencies**: Phase 2 (risk-engine-aware simulation), ideally Phase 4/5 data (more strategies to validate) though can start with just the existing strangle engine.

**Risk level**: Low to the live system (backtesting doesn't touch live capital) but High in terms of "getting the math wrong produces confidently false conclusions" — peer-review the metric implementations (§2.6) carefully, this is the one phase where a subtle bug does the most epistemic damage.

**Rollback strategy**: N/A in the live-capital sense — a backtesting bug is fixed and results are simply recomputed, no position/capital impact.

**Definition of Done**: every number in `02_RISK_MANAGEMENT_ENGINE.md` and the original review that was flagged as "cannot be computed responsibly from current data" (Sharpe, Sortino, Risk of Ruin, Expected Drawdown) can now be computed, with an honestly-reported confidence level.

---

## Phase 8 — Production Hardening

**Objective**: the operational reliability work — Redis, queue, deploy hygiene — that makes Phases 2-7 actually robust in production, not just correct in a test environment.

**Files affected**: `settings.py` (Redis `CACHES`), `updater.py` (jobs enqueue to RQ instead of running inline), deployment config.

**Database changes**: none (infrastructure, not schema).

**API changes**: none functionally — same endpoints, more reliable backing.

**Frontend changes**: none required, though async job endpoints (backtest run) should show real progress/status rather than a blocking spinner, once queue-backed.

**Testing checklist**:
- [ ] Load test: confirm a scheduled scan job no longer visibly slows dashboard response time (the exact problem named in `01` §3.5).
- [ ] Confirm cache survives a deploy/cold-start cycle (the opposite of today's `LocMemCache` behavior).
- [ ] Confirm queue jobs retry correctly on transient Angel One API failures (403 rate-limit handling, already a documented pain point in `CLAUDE.md`) rather than silently dropping.

**Acceptance criteria**: no user-facing request ever blocks on a background job; cache state survives routine deploys.

**Estimated complexity**: Medium — mostly integration/config work, not new business logic.

**Dependencies**: Phases 2-7 (there needs to be real background work worth hardening — sequencing this after the feature phases means the hardening work is sized to the actual final job set, not a guess).

**Risk level**: Medium — infrastructure changes can introduce subtle new failure modes (queue job ordering, cache invalidation timing) that don't show up until production load.

**Rollback strategy**: keep the inline-execution code path available behind a flag during the transition, so a queue-infrastructure problem can fall back to "slower but working" rather than "broken."

**Definition of Done**: a full trading day runs with zero manual intervention, verified via monitoring (not just "nothing broke that we noticed").

---

## Phase 9 — Performance Optimization

**Objective**: reduce Angel One API call volume (the actual scalability bottleneck per `01` §3.8, not compute).

**Files affected**: broker integration layer — extend existing bulk-fetch patterns (`get_bulk_quotes`, `get_prices_bulk`) everywhere a per-symbol loop still exists.

**Database changes**: none.

**API changes**: none.

**Frontend changes**: none.

**Testing checklist**:
- [ ] Audit every remaining per-symbol Angel One call across all engines (Phases 2-7 will have added new ones — CSP/Covered Call candidate scanning, fundamental data fetches) and batch where the API supports it.
- [ ] Confirm no regression in the existing, already-correct bulk patterns in `delta_hedge_service.py`/`pro_system_service.py`.

**Acceptance criteria**: measurable reduction in Angel One API calls per scan cycle vs. pre-Phase-9 baseline, with zero increase in 403 rate-limit incidents.

**Estimated complexity**: Low-Medium — pattern-matching and applying an existing, proven technique, not inventing a new one.

**Dependencies**: Phases 2-7 (optimize the real, final call pattern, not a moving target).

**Risk level**: Low.

**Rollback strategy**: trivial, per-call-site reverts.

**Definition of Done**: a documented API-call budget per scan cycle, with headroom under Angel One's documented rate limits, for every engine combined.

---

## Phase 10 — Institutional Release

**Objective**: the "would this survive a professional investment committee's scrutiny" bar from the original review, applied to the finished V2 system as a whole.

**Files affected**: all — this is a review phase, not a build phase.

**Database changes**: none new — a final audit that every table from Phases 2-7 is actually in use (apply Phase 1's "no orphaned code" standard to the new work, not just the old).

**API changes**: none new — full API documentation pass (OpenAPI/Swagger schema generation from DRF, if not already present).

**Frontend changes**: full accessibility and cross-browser pass on the new pages (Risk Dashboard, Portfolio, Option Income, Backtest) — everything shipped fast across Phases 2-9 gets a design-polish pass here.

**Testing checklist**:
- [ ] Re-run this entire roadmap's originating review (the 10-section institutional-format critique) against the *finished* V2 system, scored the same way the original was — the explicit success bar the user set is "a 10/10 review."
- [ ] Confirm every finding from the original review (fictional risk metrics, undefined-risk-only strategies, 2-day sample size, no sector limits, dead code) has a corresponding, verifiable fix.
- [ ] Full regression suite across every engine, every endpoint.
- [ ] Confirm `CLAUDE.md` (or its Phase-1-corrected successor) accurately describes the final V2 system with zero drift.

**Acceptance criteria**: the re-review in the first checklist item scores materially higher than the original 41/100 and 43/100 — with the gap between them and 100 explainable by genuinely remaining limitations (e.g., "no automated broker-position reconciliation yet," flagged honestly in Phase 3) rather than by unresolved findings from this roadmap.

**Estimated complexity**: Medium (review/polish, not new architecture).

**Dependencies**: all previous phases.

**Risk level**: Low (review phase).

**Rollback strategy**: N/A.

**Definition of Done**: an institutional-grade review of the finished system finds no finding of the type "the dashboard shows a risk number the backend doesn't actually use."

---

## Sequencing Summary

```
Phase 1 (cleanup, no deps)
   │
   ▼
Phase 2 (Risk Engine) ──required by──> Phases 3, 4, 5 (every new engine gates through it)
   │
   ├──> Phase 3 (Portfolio) ──required by──> Phase 4's Covered Call structure specifically
   ├──> Phase 4 (Option Income)
   └──> Phase 5 (Swing)
           │
           ▼
        Phase 6 (AI) — depends on 2-5 having real data to analyze
           │
           ▼
        Phase 7 (Backtesting) — depends on 2 (risk-aware simulation), benefits from 4/5 (more to test)
           │
           ▼
        Phase 8 (Hardening) — sized to the final job set from 2-7
           │
           ▼
        Phase 9 (Performance) — optimizes the final call pattern from 2-8
           │
           ▼
        Phase 10 (Institutional Release) — reviews everything
```

Phases 3, 4, 5 can run in parallel once Phase 2 lands (they don't depend on each other, only on the risk engine) — the one hard ordering constraint inside that group is Covered Call (part of Phase 4) waiting on Phase 3's `Holding` model specifically, not all of Phase 3's scope.
