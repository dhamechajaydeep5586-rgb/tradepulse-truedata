# Short-Term Swing Engine V2 — Architecture Proposal

**Status: PROPOSED — awaiting approval. No code to be written until this is signed off.**

Source of truth: `doc/short_term_stock.md`, `doc/INSTITUTIONAL_AUDIT_PLATFORM.md`,
`doc/INSTITUTIONAL_AUDIT_INTRADAY.md`, and the current implementation.

Mandate: transform the existing swing engine into an institutional-grade production
engine **by reusing the intraday engine's institutional components**, preserving the
existing Django/APScheduler/Angel One architecture, and duplicating nothing.

---

## 0. THE BLOCKING PRECONDITION

**`ShortTermSignal` contains zero rows. The engine has never produced a signal.**

Every design below is unverifiable until that is explained. If the engine is failing at
token resolution, at the candle guard (`len(df) < 201` against a 365-day window that
yields ~250 bars — thin margin), at the `IntegrityError`, or simply never reaching
persistence, then V2 will hit the identical wall and we will not know it, because V2's
correct behaviour also includes "emit nothing on a bad day."

**Phase 0 is mandatory and gates everything else.** Run the existing scanner with
instrumentation at each of the 13 rejection points and record the funnel. Until we know
where 500 symbols become 0 signals, we are rebuilding blind.

This is not a caveat. It is the first work item.

---

## 1. NEW ARCHITECTURE

### 1.1 Principle

Three engines, one set of services, one place where every threshold lives.

Today the intraday engine owns institutional logic that the swing engine needs. Copying
it would create the third and fourth duplicate of ADX. Importing it as-is would impose
5-minute bars and a NIFTY200 universe on a 15–90 day strategy. The only correct move is
to **parameterise the shared services by engine profile** and have all three engines
consume the same code paths with different profiles.

### 1.2 Target module layout

```
backend/stocks/services/
├── shared/                          ← promoted, timeframe-agnostic
│   ├── profiles.py          [NEW]   EngineProfile — the ONLY place thresholds live
│   ├── universe.py          [MOVED] was universe_service.py, + profile param
│   ├── regime.py            [MOVED] was regime_service.py, + horizon param
│   ├── ranking.py           [MOVED] was ranking_service.py, + weight profiles
│   ├── portfolio_risk.py    [MOVED] + profile param, + cross-engine aggregation
│   ├── risk_engine.py       [NEW]   position_size extracted from intraday_service
│   ├── cost_model.py        [NEW]   friction constants extracted, per holding period
│   ├── sector.py            [NEW]   _sector_strength extracted + sector-index upgrade
│   ├── expected_value.py    [NEW]   base rates → EV, gated on sample size
│   ├── calendar_service.py  [NEW]   earnings blackout + corporate actions
│   └── outcome_store.py     [NEW]   trade outcome ledger feeding EV and Kelly
├── intraday_service.py      [MODIFIED] imports from shared/, keeps its triggers
├── swing_service.py         [NEW]      replaces trade_engine.py's scanner+lifecycle
├── swing_signals.py         [NEW]      swing setup detection only
├── trade_engine.py          [DELETED after migration]
└── pro_system_service.py    [REDUCED to get_market_direction + NIFTY500_FALLBACK]
```

Files move with **re-export shims** at the old paths for one release, so the intraday
engine keeps working unchanged during the refactor. `tests_intraday_v2.py` is the
regression net: Phase 1 is behaviour-preserving and must leave that suite green.

### 1.3 EngineProfile — the single source of thresholds

One frozen dataclass, three instances. Every hardcoded constant in the codebase moves
here. Nothing downstream reads `settings` or a literal.

| Field | intraday | **swing** | long_term |
|---|---|---|---|
| `index` | NIFTY200 | **NIFTY500** | NIFTY500 |
| `bar_interval` | FIVE_MINUTE | **ONE_DAY** | ONE_DAY |
| `min_adv_inr` | ₹50 cr | **₹10 cr** | ₹5 cr |
| `min_price` | ₹100 | **₹50** | ₹50 |
| `max_spread_bps` | 5 | **25** | 50 |
| `universe_ttl` | 6 h | **24 h** | 7 d |
| `regime_horizon` | intraday | **swing** | structural |
| `regime_ttl` | 5 min | **12 h** | 7 d |
| `min_rank_score` | 65 | **60** | 55 |
| `max_positions` | 5 | **12** | 20 |
| `max_per_sector` | 2 | **3** | 4 |
| `max_per_cluster` | 2 | **2** | 2 |
| `corr_lookback_days` | 30 | **90** | 250 |
| `corr_threshold` | 0.70 | **0.65** | 0.60 |
| `risk_per_trade_pct` | 0.30 | **0.75** | vol-target |
| `max_gross_pct` | 300 | **100** | 100 |
| `equity_share_pct` | 30 | **40** | 30 |
| `round_trip_cost_pct` | 0.14 | **≈0.40 (delivery)** | ≈0.40 |
| `min_target_cost_multiple` | 3.0 | **6.0** | 15.0 |
| `earnings_blackout` | n/a | **(−2, +1) sessions** | none |
| `daily_loss_limit_pct` | 2.0 | **n/a** | n/a |
| `strategy_drawdown_limit_pct` | n/a | **8.0** | 15.0 |

Two entries need explicit derivation before build, not assumption:

- **`round_trip_cost_pct` for delivery.** The dominant term is STT at 0.1% on *both*
  sides = 0.20%, plus exchange charges, stamp duty, GST, DP charges (~₹15.5/sell), and
  slippage. My working estimate is 0.35–0.45%, but this must get the same itemised
  treatment `INSTITUTIONAL_AUDIT_INTRADAY.md §0.1` gave intraday. **Do not ship an
  invented number.**
- **`equity_share_pct`.** Three engines currently each assume they own the whole account
  (intraday hardcodes ₹5,00,000). Capital must be allocated once, centrally.

**Consequence worth stating up front:** at ~0.40% cost and a 6× multiple, the swing cost
gate requires a ≥2.4% target. A 15–90 day hold clears that trivially. The gate will
almost never bind for swing — which is the correct outcome, and confirms the intraday
cost problem was a *timeframe* problem, not a strategy problem.

### 1.4 Swing pipeline (V2)

```
                 16:00 IST, Mon–Fri, after close      ← CHANGED from 10:00
                            │
              ┌─────────────▼─────────────┐
              │ 0  HALT GATE              │  strategy drawdown / kill switch (Redis)
              └─────────────┬─────────────┘
              ┌─────────────▼─────────────┐
              │ 1  UNIVERSE  (shared)     │  profile=swing → NIFTY500, ADV≥₹10cr,
              │                           │  price≥₹50, spread≤25bps, +sector map
              └─────────────┬─────────────┘
              ┌─────────────▼─────────────┐
              │ 2  REGIME  (shared)       │  horizon=swing, daily bars, 2-axis,
              │                           │  hysteresis, breadth, VIX
              │                           │  → allow_momentum / allow_mean_reversion
              │                           │  → size_multiplier
              └─────────────┬─────────────┘
              ┌─────────────▼─────────────┐
              │ 3  CORPORATE ACTIONS      │  adjust candles for splits/bonus/dividends
              │    (shared calendar)      │  BEFORE any indicator runs
              └─────────────┬─────────────┘
              ┌─────────────▼─────────────┐
              │ 4  SETUP DETECTION        │  CLOSED daily bars only — no repaint
              │    swing_signals.py       │  momentum + pullback families
              └─────────────┬─────────────┘
              ┌─────────────▼─────────────┐
              │ 5  REGIME GATE  (shared)  │  strategy_allowed(family, regime)
              └─────────────┬─────────────┘
              ┌─────────────▼─────────────┐
              │ 6  EARNINGS BLACKOUT      │  drop if results within (−2, +1) sessions
              └─────────────┬─────────────┘
              ┌─────────────▼─────────────┐
              │ 7  COST + EV GATE         │  target ≥ 6× cost; EV ≥ 0 when base
              │    (shared cost + EV)     │  rates exist, else cost gate only
              └─────────────┬─────────────┘
              ┌─────────────▼─────────────┐
              │ 8  CROSS-SECTIONAL RANK   │  percentile-rank within the day's own
              │    (shared ranking)       │  candidate set → 0–100. May return ZERO.
              └─────────────┬─────────────┘
              ┌─────────────▼─────────────┐
              │ 9  PORTFOLIO CONSTRAINTS  │  sector cap, correlation cluster cap,
              │    (shared portfolio_risk)│  max positions, cross-engine gross
              └─────────────┬─────────────┘
              ┌─────────────▼─────────────┐
              │ 10 RISK-PARITY SIZING     │  qty = (alloc_equity × risk%) / (entry−stop)
              │    (shared risk_engine)   │  × regime.size_multiplier × vol_scalar
              └─────────────┬─────────────┘
              ┌─────────────▼─────────────┐
              │ 11 PERSIST as PENDING     │  entry armed for NEXT session
              └─────────────┬─────────────┘
                            ▼
        Next session 09:20–15:20, every 15 min:  ACTIVATION (bar-confirmed)
        Every session 15:25:                     EXIT AUDIT (bar-confirmed, ordered)
        Saturday 06:00:                          expiry / review / rebalance
```

### 1.5 The five decisions that define V2

**D1 — Scan after the close, not at 10:00.** The current engine reads `Close.iloc[-1]`
from an in-progress daily bar at 10:00, so "yesterday's close" is actually today's
10 a.m. price. Every indicator repaints and live can never agree with a backtest. V2
scans at 16:00 on **closed** bars and arms entries for the next session. This is the same
fix `_volume_profile_logic` already applies intraday via `df = df.iloc[:-1]`.

**D2 — Delete the relaxed fallback outright.** No replacement, no softer version. The
shared ranking threshold is allowed to return zero candidates; `ranking_service`'s own
docstring already states that producing nothing on a poor day is a feature. The relaxed
pass currently *skips the BEARISH market gate*, drops relative strength entirely, and
shifts `adx_limit` so scores are not comparable across modes. It is the worst single
defect in the engine.

**D3 — Ranking replaces the bespoke `ai_score`.** The 5-component 0–100 score in
`_compute_ai_score` is absolute-threshold based and does not adapt to the day's
opportunity set. It is replaced by the shared cross-sectional percentile ranker. The
column is renamed `rank_score` to make the break explicit and prevent silent comparison
of old and new values.

**D4 — Hard filters become factors, except where they are structural.** Currently six
absolute gates each independently reject. In V2 only three remain hard — trend structure
(`close > EMA50 > EMA200`), liquidity floor, and the cost/EV gate — because those are
binary tradability facts. ADX, relative strength, volume expansion and 52-week proximity
become **ranked factors**, so a stock with ADX 24 and exceptional relative strength is
no longer discarded for missing an arbitrary 25.

**D5 — Exits become bar-confirmed and correctly ordered.** Current EOD ordering puts the
20-EMA trail *before* every target check, so a gap through T3 books as `TRAILING_EXIT` at
the close. V2 reuses the intraday `_scan_bars_for_exit` pattern on daily bars: pessimistic
when one bar contains both stop and target, ordered SL → T3 → time-stop → T2 → T1, and
records `exit_reason` on every close.

---

## 2. FILE-BY-FILE IMPLEMENTATION PLAN

### 2.1 New shared files

| File | Contents | Notes |
|---|---|---|
| `shared/profiles.py` | `EngineProfile` frozen dataclass; `INTRADAY`, `SWING`, `LONG_TERM` instances; `get_profile(name)` | Every literal from §1.3. Settings-overridable per field, same pattern as today. |
| `shared/risk_engine.py` | `position_size(entry, stop, profile, equity)`, `apply_regime_scalar`, `gross_exposure_check`, `allocated_equity(profile)` | Lifted verbatim from `intraday_service.position_size`, generalised. Intraday behaviour must be bit-identical. |
| `shared/cost_model.py` | `round_trip_cost_pct(profile)`, `min_target_pct(profile)`, `passes_cost_gate(entry, target, profile)` | Extracts `ROUND_TRIP_COST_PCT` / `MIN_TARGET_COST_MULTIPLE`. Delivery costs derived, itemised, commented. |
| `shared/sector.py` | `sector_strength_from_candidates()` (extracted), `sector_strength_from_indices()` (new), `sector_rotation_rank()` | Intraday keeps the cheap candidate-derived version; swing uses real sector indices — 11 daily candle calls once a day is affordable, intraday's per-scan budget was not. |
| `shared/outcome_store.py` | Append-only ledger of closed trades: engine, symbol, setup, regime snapshot, R-multiple, holding period, exit_reason | **Prerequisite for EV and Kelly.** Nothing exists today. |
| `shared/expected_value.py` | `base_rates(engine, setup, regime, min_n)`, `expected_value(...)`, `ev_gate(...)` | Returns `None` below `min_n` (300). Callers must treat `None` as "cost gate only" — never as EV 0. |
| `shared/calendar_service.py` | `next_earnings_date(symbol)`, `in_earnings_blackout(symbol, profile)`, `corporate_actions(symbol, since)`, `adjust_ohlcv(df, actions)` | **Requires a new data source.** See §2.4. |

### 2.2 Modified shared files

| File | Change | Risk |
|---|---|---|
| `shared/universe.py` | Add `profile` param → per-profile index, thresholds, cache key, TTL. Add spread filter (currently declared as `MAX_SPREAD_BPS` but **not applied** in `get_trading_universe`). | Low. Add the spread gate for intraday too — it is a latent bug. |
| `shared/regime.py` | Add `horizon` param selecting bar interval, lookback, TTL, cache key, and **strategy-family map**. The two-axis composite, z-scoring and hysteresis are reused unchanged. | Medium. The family map is currently a module-level set naming intraday triggers; it becomes per-horizon. |
| `shared/ranking.py` | `FACTOR_WEIGHTS` becomes per-profile. Factor computation becomes a registry so engines contribute their own factors without touching the ranker. | Low. Mechanically clean. |
| `shared/portfolio_risk.py` | Add `profile` param (lookback, thresholds, caps). Add `cross_engine_gross_exposure()` reading open positions across all three engines. Add **`max_per_promoter_group`**. | Medium. Cross-engine aggregation is genuinely new. |

**Promoter-group cap:** sector caps alone would not have prevented the current long-term
book (ADANIPORTS is Infrastructure, ADANIENT is Diversified — two different sectors, one
group). India-specific and mandatory. Needs a group mapping table; the initial version can
be a curated static map of the ~20 major promoter groups, refined later from shareholding
data.

### 2.3 Swing engine files

**`swing_signals.py` (new)** — setup detection only, no I/O, no persistence. Pure
functions over an adjusted daily OHLCV frame, returning candidate dicts. Two families:

- **Momentum family** — 20-day-high breakout, and 52-week-high proximity breakout.
  Requires `regime.allow_momentum`.
- **Pullback family** — retracement to the 20-EMA within an intact `close > EMA50 > EMA200`
  structure. Requires `regime.allow_mean_reversion`.

The current engine has one implicit setup (breakout selected by filters, entered on a
pullback) — a contradiction: it *selects* breakouts then *enters* only if price falls
back. V2 separates them into two families with their own triggers and their own regime
permissions.

Stops and targets stay ATR-based (`entry − 2×ATR`, 10% floor, 2R/3R/4R) — that logic is
sound and needs no change.

**`swing_service.py` (new)** — orchestration only. Implements §1.4 by calling shared
services. Contains no indicator maths whatsoever. Replaces `trade_engine.py`'s scanner,
lifecycle, EOD evaluator, and dashboard payload.

### 2.4 New data dependencies — must be procured

| Need | Used by | Options | Blocking? |
|---|---|---|---|
| **Corporate actions** (split, bonus, dividend, ex-dates) | All three engines | NSE/BSE corporate-action feeds (free, scrapeable); or vendor | **Yes for correctness.** Unadjusted series manufacture false stop-outs. First verify whether Angel One's `getCandleData` already returns adjusted series — **this must be tested, not assumed.** |
| **Earnings calendar** | Swing blackout, LT quarterly review | NSE results calendar (free); Tickertape/Trendlyne | Yes for the blackout feature |

If Angel One candles turn out to be split-adjusted already, the corporate-action work
shrinks to dividend handling and an assertion test. **Determining this is a Phase 0 task.**

### 2.5 Deletions

| Delete | Reason |
|---|---|
| `trade_engine._compute_ai_score` | Replaced by shared ranking |
| `trade_engine._run_daily_scanner_impl` relaxed pass | D2 |
| `trade_engine._ema/_atr/_adx/_rsi` | Duplicates of `signal_utils` |
| `pro_system_service._get_ema/_compute_atr/_compute_adx` | Third duplicate set |
| `pro_system_service.scan_short_term_stocks` | Dead parallel scanner |
| `pro_system_service.update_pro_system_outcomes` | Dead; LT exits move to the LT engine |
| `pro_system_service.get_pro_system_data` | Dead path; also the `IntegrityError` source |
| `trade_engine.run_intraday_check` | Dead + `_exit_signal` arity TypeError |
| `TradeScanner`, `Trade`, `StockDailyData`, `SignalChangeLog` models | Never written |
| `strategy_config.json` | Superseded by `profiles.py`; its keys were never read anyway |
| `ShortTermSignal.Status` `CLOSED`, `CANCELLED`, `COOLDOWN` | Never assigned |

**Three ADX implementations collapse to one** (`signal_utils.compute_adx`, Wilder
smoothing). Note the current `signal_utils` version uses simple rolling smoothing while
`trade_engine`/`pro_system_service` use Wilder — the canonical one must be Wilder, and
intraday's ADX values will shift slightly. That is a deliberate, documented change and
`tests_intraday_v2.py` expectations may need updating.

---

## 3. DATABASE CHANGES

### 3.1 `ShortTermSignal` — extend, do not replace

Keeping the model preserves the existing architecture as mandated. Added fields:

| Field | Type | Purpose |
|---|---|---|
| `qty` | int | Risk-parity size — sizing becomes server-side |
| `rupee_risk` | decimal | Rupee risk at entry |
| `rank_score` | decimal | Shared ranker output (replaces `ai_score` semantically) |
| `rank_factors` | JSON | Per-factor breakdown, for attribution |
| `regime_snapshot` | JSON | `RegimeState` at generation — required to measure regime-conditional EV |
| `setup_family` | varchar | `MOMENTUM` / `PULLBACK` |
| `exit_reason` | varchar | `LEVEL_HIT` / `TIME_STOP` / `TRAILING` / `TARGET3` / `EARNINGS` / `DRAWDOWN_HALT` / `EXPIRED` |
| `cost_pct` | decimal | Cost assumption at generation |
| `target_pct` | decimal | For EV attribution |
| `entry_valid_until` | date | Next-session arming window |

`ai_score` retained, nullable, deprecated — never written by V2. Removed in a later release.

### 3.2 New models

- **`TradeOutcome`** — append-only ledger. `engine`, `symbol`, `setup_family`,
  `regime_snapshot`, `entry`, `exit`, `r_multiple`, `holding_days`, `exit_reason`,
  `cost_pct`, `closed_at`. Feeds `expected_value` and `kelly_fraction`. Written on every
  terminal transition by all three engines.
- **`CorporateAction`** — `symbol`, `ex_date`, `action_type`, `ratio`, `amount`, `source`.
- **`EarningsEvent`** — `symbol`, `event_date`, `confirmed`, `source`.
- **`PromoterGroup`** — `symbol`, `group_name`. Seeded from a curated map.

### 3.3 `TradeHistory` — make it generic

Currently `trade` is an FK to `ShortTermSignal`, so **long-term and intraday signals
cannot be audited at all**. Change to a nullable FK pair (`short_term_signal`,
`signal_history`) or a `GenericForeignKey`. Recommend the explicit nullable-pair form —
simpler queries, no content-type join, matches how `TelegramLog` already does it.

### 3.4 Index additions

`ShortTermSignal(status, generated_at)`, `TradeOutcome(engine, setup_family, closed_at)`,
`CorporateAction(symbol, ex_date)`, `EarningsEvent(symbol, event_date)`.

---

## 4. API CHANGES

| Endpoint | Change | Compatibility |
|---|---|---|
| `GET /api/stocks/pro-system/` | Add `rank_score`, `rank_factors`, `qty`, `rupee_risk`, `regime_snapshot`, `setup_family`, `exit_reason`. Keep `ai_score` mirroring `rank_score` for one release. Bound the query — currently unbounded `.objects.all()`. | Additive |
| `GET /api/stocks/pro-system/` | Add `regime` block and `portfolio` block (`gross_exposure`, `n_eff`, `sector_exposure`, `cluster_exposure`, `promoter_group_exposure`) | Additive |
| `GET /api/stocks/pro-performance-report/` | Fix win-rate to read `exit_reason`, not `status`. Add R-multiple, expectancy, MAE/MFE, regime-conditional breakdown. | **Breaking** — win rate goes from a structural 0.0% to a real number |
| `GET /api/stocks/swing-rejections/` | **New.** Today's rejected candidates with reason. Makes the funnel observable — directly addresses B1. | New |
| `GET /api/stocks/cron-trigger/?action=trade_scan` | Drop `relaxed` parameter | **Breaking**, intentional |
| `GET /api/stocks/dashboard-summary/` | Add `qty`, `rank_score` to swing items | Additive |

`ProSystem.jsx` must move its client-side sizing to read server `qty`, and stop
displaying "Max Risk (1.5%)" as if it drove anything.

---

## 5. MIGRATION PLAN

Seven phases. Each is independently shippable and revertible.

| Phase | Work | Gate to proceed |
|---|---|---|
| **0. Diagnose** | Instrument the existing scanner's 13 rejection points; run one full scan; publish the funnel. Verify whether Angel One candles are corporate-action adjusted. | Root cause of B1 identified and written down |
| **1. Promote** | Move 4 services into `shared/`, add `EngineProfile`, extract `risk_engine` + `cost_model` + `sector`. **Zero behaviour change.** Re-export shims at old paths. | `tests_intraday_v2.py` green; intraday output byte-identical on a replayed day |
| **2. Parameterise** | Add `profile`/`horizon` params. Intraday continues passing `INTRADAY`. Apply the missing spread filter. | Intraday unchanged; swing profile resolves correctly in isolation |
| **3. Data** | `CorporateAction`, `EarningsEvent`, `PromoterGroup`, `TradeOutcome` models + ingestion + backfill | Adjusted series reconcile against a known split (e.g. a recent 1:5) |
| **4. Build swing V2** | `swing_signals.py`, `swing_service.py`, DB fields, run **shadow** alongside the old engine — generate and persist to a shadow table, execute nothing | ≥20 trading days of shadow output; funnel sane; ≥1 signal on days the market offers setups |
| **5. Cut over** | Switch scheduler to V2, retire `trade_engine.py`, delete dead code | Shadow output reviewed and accepted |
| **6. Backfill + measure** | Populate `TradeOutcome` from history; enable EV gate once n ≥ 300 per setup family | EV gate stays disabled until the sample exists |

**Phase 4 is the one that cannot be rushed.** An engine that has produced zero signals in
its lifetime must prove it produces signals *before* it is trusted to produce good ones.

---

## 6. CODE CHANGES — summary of surface area

| Category | Files | Est. LOC |
|---|---|---|
| New shared services | 7 | ~900 |
| Modified shared services | 4 | ~350 changed |
| New swing engine | 2 | ~600 |
| Deleted | `trade_engine.py` (1,652) + `pro_system_service` reduction (~640) | **−2,300** |
| Models + migrations | 5 models, 4 migrations | ~250 |
| API/serializers | 3 files | ~200 |
| Frontend | `ProSystem.jsx`, `PerformanceReports.jsx` | ~150 |
| Tests | new suites | ~800 |

**Net: roughly −200 lines of production code for a materially more capable engine**,
because deleting `trade_engine.py` and the `pro_system_service` duplicates removes more
than the shared layer adds. That is the return on not duplicating.

---

## 7. TESTING PLAN

**Unit** — `EngineProfile` resolution and settings override; `position_size` parity with
the current intraday implementation (must be bit-identical); cost gate arithmetic;
corporate-action adjustment against a known split; earnings-blackout boundary conditions;
`_pct_rank` with degenerate input; cluster construction; promoter-group cap.

**Property-based** — ranking is invariant to candidate ordering; portfolio constraints
never exceed any cap; sizing never exceeds gross budget; adjusted series preserve return
continuity across an ex-date.

**Golden-path regression** — Phase 1 must reproduce intraday output byte-for-byte on a
replayed session. This is the single most important test in the plan; it is what makes
the refactor safe.

**Backtest** — replay swing V2 over 3 years of daily bars. Report expectancy in R, not
win rate. Report MAE/MFE. Report regime-conditional performance. **Verify live-vs-backtest
agreement** — only possible because of D1 (closed bars).

**Shadow** — Phase 4, ≥20 sessions, no execution.

**Failure-mode** — broker unavailable, circuit breaker tripped, empty universe, zero
candidates (must be a normal outcome, not an error), Redis down.

**The test that matters most:** an assertion that the engine produces a non-zero number of
signals over a 20-day shadow window. B1 existed because nothing ever checked.

---

## 8. DEPLOYMENT ORDER

1. **Redis** — before anything else. Kill switches and locks in `FileBasedCache` are not
   durable, and V2 adds more state.
2. Phase 0 diagnosis — no deploy, findings only.
3. Phase 1 promotion — deploy, verify intraday unchanged for 3 sessions.
4. Phase 2 parameterisation — deploy, verify intraday unchanged for 3 sessions.
5. Phase 3 data models + ingestion — deploy, backfill, reconcile.
6. Phase 4 shadow mode — deploy, 20 sessions, review.
7. Phase 5 cutover — single scheduler switch, `trade_engine.py` deleted, old job removed.
8. Phase 6 — outcome backfill; EV gate remains off until n ≥ 300.

**Rollback:** phases 1–3 are behaviour-preserving and revert cleanly. Phase 5 is the only
one-way door; keep the old scheduler entry commented for one release.

---

## 9. WHAT THIS DOES NOT FIX

Stated so it is not discovered later:

- **No execution layer.** V2 still emits advice. Slippage remains an assumption.
- **No fundamentals.** Swing stays a technical strategy. That is defensible for a 15–90
  day horizon; it is not defensible for long-term (see the companion document).
- **EV is inert until n ≥ 300.** At an expected 5–15 swing trades per month, that is
  2–4 years. Until then the cost gate is the only EV proxy. **Kelly sizing is therefore
  unavailable to this engine for years** — the existing `n_trades < 300` gate is correct
  and must not be weakened to make the feature appear to work.
- **Single-process scheduler** remains a single point of failure.
- **Corporate-action coverage** will be imperfect at first.

---

*Companion: `doc/LONG_TERM_ENGINE_V2_ARCHITECTURE.md`. As-built reference:
`doc/short_term_stock.md`. Findings this document closes:
`doc/INSTITUTIONAL_AUDIT_PLATFORM.md` §5 (B1, B7–B11, B15–B25).*
