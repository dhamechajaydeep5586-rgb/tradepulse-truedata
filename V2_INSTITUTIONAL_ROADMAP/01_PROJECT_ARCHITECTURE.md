# 01 — Project Architecture: Current State Audit & Target Design

**Status:** Research document. No code changed. Every claim below was verified by reading the actual file/line in this repository as of this audit — not inferred from `CLAUDE.md`, which this audit found to be materially out of date in at least one entire subsystem (see §2.6).

---

## 0. How to read this document

Section 1 is the honest, unflattering inventory of what exists today — including the parts that are dead, duplicated, or undocumented. Section 2 pulls out the specific technical debt items with file:line evidence. Section 3 is the target (V2) architecture. Section 4 is how to get from one to the other without a rewrite. If you only read one section before greenlighting Phase 1 of `10_IMPLEMENTATION_ROADMAP.md`, read Section 2 — it's the reason Phase 1 exists at all.

---

## 1. Current Architecture — As-Built

### 1.1 Stack

| Layer | Technology | Version evidence |
|---|---|---|
| Backend | Django 4.2 + DRF | `backend/requirements.txt` |
| Language | Python | 3.10 (venv), `runtime.txt` may say 3.13 — **mismatch, verify before Phase 1** |
| Frontend | React 19 + Vite 7 | `frontend/package.json` |
| Styling | TailwindCSS 4 + vanilla CSS | `frontend/package.json` |
| DB | PostgreSQL via Supabase (pooled connection) | `DATABASE_URL` env, `aws-*.pooler.supabase.com` host observed live |
| Cache | Django `LocMemCache` (default — **no `CACHES` block in `settings.py`**) | confirmed by absence, not by config |
| Scheduler | APScheduler (in-process, `CronTrigger`) | `backend/stocks/updater.py` |
| Broker/Data | Angel One SmartAPI (WebSocket + REST) | `angel_one_service.py`, `angel_one_streamer.py` |
| Hosting | Render (backend, free tier), Vercel (frontend) | confirmed live this session |
| Auth | JWT via `rest_framework_simplejwt`, custom `User` model | `users/` app |

### 1.2 Actual folder structure (backend)

```
backend/
├── config/                    # Django settings, wsgi, urls root
├── users/                     # Custom auth app — register/login/profile
├── stocks/                    # THE monolith — everything trading-related lives here
│   ├── models.py              # 11 models, 2 unrelated model families (see §1.4)
│   ├── views.py                # 13 API views, all in one file, ~500+ lines
│   ├── urls.py                 # 13 routes
│   ├── groww_preview_views.py  # kept separate deliberately (research feature)
│   ├── updater.py              # APScheduler job definitions — the de facto orchestrator
│   ├── serializers.py
│   ├── management/commands/    # 9 commands, several overlapping in purpose (§2.3)
│   └── services/                # 25+ files, no sub-package structure, flat namespace
│       ├── delta_hedge_service.py       # 2,913 lines — the strangle engine, LIVE
│       ├── trade_engine.py               # the REAL swing engine, LIVE (see §2.1)
│       ├── pro_system_service.py         # ~60% dead code (see §2.1)
│       ├── intraday_service.py           # intraday VP engine, LIVE
│       ├── commodity_service.py          # 154 lines, MCX engine, LIVE
│       ├── short_strangle_scanner.py     # standalone, management-command-only (§2.2)
│       ├── groww_free_service.py         # this session's addition, LIVE, isolated
│       ├── signal_utils.py               # market status, holidays, indicators — shared kernel
│       ├── angel_one_service.py          # broker integration
│       ├── angel_one_streamer.py         # WebSocket price stream
│       ├── market_data_orchestrator.py   # price-fetch abstraction over Angel One
│       ├── option_greeks_service.py      # Black-Scholes greeks, IV estimation
│       ├── option_chain_service.py
│       ├── risk_state_engine.py          # position risk classification
│       ├── market_intelligence_service.py # VWAP/VA/EMA "intel" per symbol
│       ├── config_vol.py                 # tunable strategy constants
│       ├── live_signal_service.py        # outcome auditor / auto square-off
│       ├── vol_telegram_formatter.py
│       ├── telegram_service.py / whatsapp_service.py
│       ├── fii_dii_service.py / bhavcopy_service.py / groww_service.py
│       └── trading_engine/               # a SEPARATE, smaller, cleaner sub-package
│           ├── state_engine.py           # is_signal_active — shared by live AND backtest
│           ├── backtest.py               # real (if minimal) backtest replay, LIVE via SignalBacktestView
│           ├── config.py, data.py, levels.py
```

**Observation worth sitting with:** `trading_engine/` (the sub-package) is the best-organized part of this codebase — small files, single responsibility, and its `state_engine.is_signal_active` is deliberately shared between live signal tracking and backtest replay so the two can never silently diverge. It is also the *only* part of the codebase organized as a sub-package rather than a flat file dropped into `services/`. That it was clearly a deliberate, careful design and the rest of `services/` wasn't is itself a finding: whoever built `trading_engine/` knew the right pattern; it just wasn't applied consistently.

### 1.3 Actual folder structure (frontend)

```
frontend/src/
├── pages/          # 6 pages: Login, Register, Dashboard, PerformanceReports, ProSystem, GrowwPreview
├── components/      # 17 components, flat namespace, no sub-folders by domain
├── context/          # AuthContext.jsx — the only context provider
├── api/               # axios.js — single shared client with interceptors
```

No component sub-folders (e.g. no `components/intraday/`, `components/strangle/`), no shared hooks directory, no design-system/primitives layer (buttons, cards, badges are re-implemented per-component with inline Tailwind classes — confirmed by comparing `Badge` in `GrowwPreview.jsx` against similar status-pill markup duplicated in `LiveSignalsTable.jsx`/`CommoditySignalsTable.jsx`).

### 1.4 Database — two unrelated model families in one app

This is the single most important architectural fact about this codebase, and it isn't documented anywhere:

**Family A — the "categorized" engine** (`SignalHistory`, generic, `category` field distinguishes `intraday`/`commodity`/`specialist`/`long_term`):
- Used by: `intraday_service.py`, `commodity_service.py`, `delta_hedge_service.py`
- One unique constraint prevents duplicate live signals: `(symbol, category, status IN [PENDING, ACTIVE])`

**Family B — the "typed" engine** (`Stock`, `StockDailyData`, `TradeScanner`, `Trade`, `ShortTermSignal`, `IndexConstituent`, `TelegramLog`, `TradeHistory`):
- Used by: `trade_engine.py` (the real swing engine), partially by `pro_system_service.py`
- `IntradaySignal` model exists but **has zero code references outside `models.py`/migrations — confirmed dead.**

These two families do not share a base class, a common status enum, or a common persistence helper. `SignalHistory.Status` and `ShortTermSignal.Status` independently define overlapping-but-different lifecycles (`HIT_TARGET`/`HIT_SL`/`EXPIRED` in both, but `ShortTermSignal` additionally has `TARGET1`/`TARGET2`/`TRAILING_EXIT`/`TIME_STOP`/`REVIEW_REQUIRED`/`CLOSED`/`ARCHIVED`/`COOLDOWN` — a materially richer state machine that Family A never adopted). A new engine (option selling, CSPs, covered calls — see `03_OPTION_SELLING_ENGINE.md`) will have to choose one family or invent a third; right now there's no documented rule for which.

### 1.5 Caching & background execution

- **Cache backend: Django `LocMemCache`** (the implicit default — no `CACHES` block in `settings.py`). This is per-process, in-memory, and is wiped on every deploy and every Render free-tier cold-start spin-down (confirmed live this session: ~23s cold start, cache empty after).
- **Scheduler: APScheduler, in-process**, defined in `updater.py`, registered against `CronTrigger`s at IST-converted hours. This runs *inside the same gunicorn worker process* as the web server — there is no separate worker process, no task queue (Celery/RQ/Dramatiq), no message broker. A long-running scan job and an incoming HTTP request compete for the same process's CPU/GIL.
- **No Redis anywhere** in `requirements.txt`, despite `CLAUDE.md`'s stack table listing "Django cache (in-memory / Redis)" as if Redis were in use.

### 1.6 Documentation drift — CLAUDE.md describes a feature that does not exist

`CLAUDE.md` §3 ("Option Selling Sniper") documents, in full implementation detail — market hours (9:15 AM–3:15 PM), a `option_sniper_service.py → get_option_sell_signals` scan pipeline, NIFTY bias checks, day-type classification, a 6-signal cap, a `frontend/src/components/OptionSellingCard.jsx` UI, and even a "Common Bugs" history for it.

**None of this exists.** Verified: no `option_sniper_service.py` file anywhere in the repo, no `OptionSellingCard.jsx`, no `/option-sell` or `/option-sniper` URL route, no reference to "SELL CE"/"SELL PE" text anywhere except inside `vol_telegram_formatter.py` (a formatter, not a scanner). This is either a feature that was fully speced and never built, or was built and later deleted with the documentation never updated. Either way: **treat every claim in `CLAUDE.md` as a hypothesis to verify against code, not a fact**, until this doc is corrected (Phase 1 action item).

### 1.7 API surface (as of this audit — 13 routes, `stocks/urls.py`)

| Route | View | Backing service | Status |
|---|---|---|---|
| `live-signals/` | `LiveSignalView` | `intraday_service.py` | Live |
| `commodity-signals/` | `CommoditySignalView` | `commodity_service.py` | Live |
| `option-chain/` | `OptionChainView` | `option_chain_service.py` | Live |
| `fii-dii/` | `FIIDIIView` | `fii_dii_service.py` | Live |
| `live-price-updates/` | `LivePriceUpdateView` | orchestrator | Live |
| `performance-report/` | `PerformanceReportView` | — | Live |
| `signal-backtest/` | `SignalBacktestView` | `trading_engine/backtest.py` | Live, minimal |
| `pro-system/` | `ProSystemView` | **`trade_engine.get_dashboard_data`** (not `pro_system_service`!) | Live |
| `pro-performance-report/` | `ProPerformanceReportView` | `pro_system_service.get_pro_performance_report` | Live, reads both `ShortTermSignal` and `SignalHistory(category='long_term')` |
| `delta-hedge/` | `DeltaHedgeView` | `delta_hedge_service.py` | Live |
| `notifications/` | `NotificationView` | `Notification` model | Live |
| `cron-trigger/` | `CronScannerTriggerView` | — | Live, external cron entrypoint |
| `groww-preview/` | `GrowwFreePreviewView` | `groww_free_service.py` | Live, isolated research feature |

Note the `pro-system/` finding: the page named "Pro System" in the frontend is **not powered by `pro_system_service.py`** for its main data — it's powered by `trade_engine.py`. `pro_system_service.py` only contributes the performance-report aggregation. This is exactly the kind of naming trap that causes engineers (and AI assistants) to "fix" the wrong file when debugging a Pro System issue — confirmed to have nearly happened in this session's own initial architecture review before the trace-through in §2.1 was done.

---

## 2. Technical Debt Inventory (concrete, file:line, no speculation)

### 2.1 Duplicate/dead swing-trading implementations

- `pro_system_service.py` lines 80-541 (`_get_ema`, `_compute_atr`, `_compute_adx`, `get_market_direction`, `_analyze_short_term`, `scan_short_term_stocks`, `_fetch_long_term_quality`, `scan_long_term_stocks`, `get_pro_system_data`) — **zero call sites** in `views.py`, `urls.py`, or `updater.py`. This is ~460 of the file's 763 lines, or roughly 60%, that never executes in production.
- The actually-live swing engine is `trade_engine.py`, using an entirely different model family (`Trade`, `TradeScanner`, `Stock`, `StockDailyData`) plus direct `ShortTermSignal` creation (line 470).
- **Risk of this debt**: any future engineer (or AI assistant) asked to "improve the swing scanner" has a 50/50 chance of editing the dead code path and shipping a change that has zero effect on production, then reporting success because the code "compiled and looked right." This already nearly happened during this session's research.

### 2.2 Three separate strangle-signal code paths

1. `delta_hedge_service.py` — the live, scheduled, VWAP/VA-ranked engine (confirmed live via Render logs pattern this session).
2. `short_strangle_scanner.py` — a **standalone** scanner, invoked only via `python manage.py scan_strangles`, **not scheduled anywhere** in `updater.py`. Reuses `delta_hedge_service.NIFTY_50_STOCKS` and `get_nse_option_strikes` but reimplements its own selection logic.
3. `generate_strangle_signals` management command — this one *is* the real 10:00 AM cron entrypoint (per its own docstring), and appears to be a thin CLI wrapper that triggers `delta_hedge_service.py`'s scan synchronously plus a Telegram summary. This is closer to legitimate infrastructure than duplication, but its existence alongside `short_strangle_scanner.py` — a *different* file, also strangle-focused, also management-command-only — is exactly the kind of pair an engineer with less context would assume are the same thing.

**Action for Phase 1**: determine definitively whether `short_strangle_scanner.py` is (a) an experiment that should be deleted, (b) a backup/manual-override path that should be documented as such, or (c) actually superior logic that should replace part of `delta_hedge_service.py`. Do not keep it in its current undocumented, unscheduled limbo state.

### 2.3 Management command sprawl without a naming or ownership convention

`stocks/management/commands/` has 9 commands: `backfill_option_selling_metadata.py` (backfilling metadata for a feature — Option Selling Sniper — that per §1.6 doesn't exist in the live codebase; this command may itself be dead or may be operating on stale data from when the feature *did* exist), `generate_strangle_signals.py`, `init_angel_one.py`, `recreate_missing_tables.py`, `refresh_nifty500.py`, `run_daily_market_update.py`, `scan_strangles.py`, `send_test_telegram.py`, `send_test_whatsapp.py`. No README, no doc mapping which are cron-scheduled vs. manual-only vs. one-off/historical.

### 2.4 No shared "signal lifecycle" abstraction despite two independent implementations of essentially the same idea

`SignalHistory.Status` and `ShortTermSignal.Status` both model PENDING→ACTIVE→(TARGET/SL)→terminal, but as two hand-written, unrelated Django model classes. Every future engine (option selling, CSP, covered call — all requested in `03_OPTION_SELLING_ENGINE.md`) faces a choice with no documented answer: extend `SignalHistory`, extend `ShortTermSignal`, or invent a third state machine.

### 2.5 No sub-package structure in `services/`

25+ files in one flat directory, mixing broker integration (`angel_one_service.py`), strategy logic (`delta_hedge_service.py`), formatting (`vol_telegram_formatter.py`), and notification delivery (`telegram_service.py`, `whatsapp_service.py`) at the same directory level with no grouping. `trading_engine/` proves the team knows how to do this well when it matters — it just wasn't applied project-wide.

### 2.6 `CLAUDE.md` documentation drift (see §1.6) — the Option Selling Sniper section is fiction relative to current code.

### 2.7 Frontend: no design-system layer

Status badges, buttons, and card containers are re-implemented per-component with inline Tailwind utility strings rather than shared primitives. Not a correctness bug, but it means a visual/behavior change (e.g. "make all status badges accessible with proper contrast") requires touching N components instead of one.

### 2.8 `runtime.txt` vs. actual venv Python version mismatch

`CLAUDE.md` states Python 3.13; the actual `backend/venv` observed this session is Python 3.10. Verify which is authoritative before any dependency upgrade work — mismatched runtime assumptions are a classic source of "works locally, breaks in prod" (and this project already has one Render-vs-local-path mismatch in `CLAUDE.md`'s own restart instructions, which reference a macOS path `/Users/indianic/tradepulse-ai` that doesn't exist on this Linux dev machine).

---

## 3. Target Architecture (V2)

### 3.1 Design principles (why each one, tied to a concrete problem found above)

1. **One signal lifecycle, one persistence layer.** Every engine — existing and new (option selling, CSP, covered call, credit spread) — writes through a single `SignalLifecycleService` with one `Status` enum. *Why*: §2.4 — two independently-evolving state machines already exist; a third would make the "which model do I extend" question worse, not better.
2. **No orphaned code reachable only by accident.** Every function in `services/` must have a traceable caller — a URL view, a scheduled job, or a management command documented as manual-only. *Why*: §2.1 — 60% of a file that looks live is not.
3. **Domain sub-packages, not a flat file dump.** Mirror `trading_engine/`'s pattern project-wide: `services/strangle/`, `services/swing/`, `services/broker/`, `services/notifications/`, `services/market_data/`. *Why*: §2.5 — the codebase already proves this pattern works when applied.
4. **Real capital-aware sizing as a first-class service, not a UI decoration.** A `RiskEngine` (full design in `02_RISK_MANAGEMENT_ENGINE.md`) that every signal-creation path calls *before* persisting a signal. *Why*: last session's review found position sizing and portfolio heat are either hardcoded or client-side-only and never reach the backend.
5. **Async execution separated from the web process.** Move APScheduler-triggered scans off the gunicorn request-serving process. *Why*: §1.5 — a scan currently competes with live HTTP traffic for the same process.
6. **Documentation as a build artifact, not prose that drifts.** `CLAUDE.md` (or its replacement) should be partially generated/checked against actual routes and files (e.g., a CI check that every documented service file exists) rather than hand-maintained prose that can describe a feature that was deleted. *Why*: §1.6/§2.6.

### 3.2 Target folder structure (backend)

```
backend/
├── config/
├── users/
├── core/                          # NEW — shared kernel, no domain logic
│   ├── signal_lifecycle/          # unified Status enum + SignalLifecycleService
│   ├── risk_engine/                # portfolio heat, position sizing, limits (see doc 02)
│   └── market_calendar/            # signal_utils.py's NSE/holiday logic, promoted
├── engines/                        # NEW — one sub-package per trading strategy
│   ├── strangle/                   # delta_hedge_service.py, split into strike_selection.py,
│   │                                #   entry_rules.py, exit_rules.py, scanner.py
│   ├── swing/                      # trade_engine.py logic, migrated off dead pro_system_service.py code
│   ├── intraday/                   # intraday_service.py, unchanged boundary, cleaned imports
│   ├── commodity/                  # commodity_service.py
│   ├── option_income/              # NEW — CSP, Covered Call, Credit Spread, Iron Condor (doc 03)
│   └── groww_preview/               # groww_free_service.py, already correctly isolated — model for the rest
├── broker/                         # angel_one_service.py, angel_one_streamer.py, market_data_orchestrator.py
├── notifications/                  # telegram_service.py, whatsapp_service.py, vol_telegram_formatter.py
├── backtesting/                    # promoted from trading_engine/, expanded per doc 07
├── ai/                              # NEW — doc 06
└── stocks/                          # becomes thin: views.py (or split per engine), urls.py, models.py (or split)
```

This is a **migration target**, not a rewrite mandate — §4 below specifies an incremental path that never requires a big-bang cutover.

### 3.3 Database design principles for V2

- New engines (option income structures) use the unified `SignalLifecycleService` (§3.1.1) — no new bespoke Django model per strategy.
- Existing `SignalHistory` and `ShortTermSignal` are **not merged retroactively** (too risky, no clear ROI) but are both wrapped behind the same lifecycle service interface going forward, so callers stop caring which table backs which category.
- `IndexConstituent`, `MarketHoliday` stay as-is — genuinely reference/lookup tables, correctly modeled.
- `IntradaySignal` (confirmed zero usage) — delete in Phase 1, after one more confirmation pass (a model deletion needs a migration and is worth a dedicated PR, not a drive-by).

### 3.4 Caching strategy

Move to **Redis**, for three concrete reasons tied to findings above:
1. `LocMemCache` is wiped on every Render cold-start spin-down (§1.5) — the 90s Groww-preview cache and the 2s delta-hedge-panel cache both silently reset far more often than their TTL suggests.
2. A future async worker (see §3.5) needs a cache shared *across processes*, which `LocMemCache` cannot do by definition.
3. Redis also gives you a real distributed lock for the "scan cooldown" pattern already used ad hoc via cache keys (`intraday_last_full_scan`, `option_sniper_last_scan` — note: that second key name is more drift-evidence per §1.6, since the service it names doesn't exist) — currently these are just cache reads/writes with no atomicity guarantee, which is fine for a single process but not once a worker is added.

### 3.5 Background workers

Introduce **Celery + Redis-as-broker** (or a lighter alternative — RQ or Django-Q2 — if the team wants to avoid Celery's operational overhead; recommendation: RQ, given the team's stated preference for "not paying anything" and RQ's lower infra footprint than a full Celery+beat setup). Move every `updater.py` `CronTrigger` job to a scheduled worker task. *Why this matters concretely*: `run_eod_evaluation`, the 3:25 PM EOD scan that walks every open swing position, currently runs inside the same process serving live dashboard requests — on a day with many open positions, this could visibly slow down or block a user's page load at exactly the time they're most likely to be checking the dashboard.

### 3.6 Message queue vs. task queue — do you need both?

No. A task queue (Celery/RQ) covers this project's actual need (scheduled + on-demand background jobs). A full message queue (Kafka/RabbitMQ as an event bus) would be over-engineering for a single-backend-instance retail trading tool — flag this explicitly because "message queues" was in the requested scope, and the honest architectural answer is "you don't need one, and adding one would be premature complexity for the current scale." Revisit only if/when there are multiple independent backend services that need to react to the same event stream (e.g., a separate notification service, a separate analytics service) — not before.

### 3.7 Dependency graph (target)

```
engines/*  ──depends on──>  core/signal_lifecycle, core/risk_engine, core/market_calendar, broker/*
core/risk_engine  ──depends on──>  (nothing else in this repo — pure functions over position/account state)
broker/*  ──depends on──>  (external: Angel One SDK only)
notifications/*  ──depends on──>  core/signal_lifecycle (reads signal state to format messages)
ai/*  ──depends on──>  engines/* (read-only, for review/analysis — never writes signals directly)
backtesting/*  ──depends on──>  core/signal_lifecycle, engines/* (replays the same rules, per existing trading_engine/state_engine.py pattern)
```

The one rule this graph is designed to enforce: **`core/risk_engine` has zero knowledge of any specific strategy.** Every engine calls into it, never the reverse. This is the direct fix for last session's finding that position sizing is either hardcoded per-engine or missing entirely — centralizing it structurally prevents the "someone added a new engine and forgot to wire risk checks" failure mode.

### 3.8 Scalability

At current scale (one user, retail account, single Nifty-50/F&O universe), the honest scalability ceiling isn't compute — it's **Angel One's rate limits** (documented extensively in `CLAUDE.md`, and the actual binding constraint behind most of the scan-cooldown/caching patterns already in the code). Any V2 scalability plan should optimize for *fewer, better-batched broker calls* (the existing `get_bulk_quotes`/`get_prices_bulk` patterns in `delta_hedge_service.py` and `pro_system_service.py` already do this correctly — extend the pattern, don't replace it) rather than for horizontal request-handling capacity, which isn't the bottleneck here.

### 3.9 Future expansion hooks

- `engines/option_income/` designed from day one to support CSP and Covered Call as first-class strategies (not retrofitted), since both were named in the user's stated trading business but have zero code today.
- `ai/` designed to consume `core/signal_lifecycle` read-only, so AI-generated trade reviews can never accidentally create or modify a live position — a hard boundary, not a convention.
- `backtesting/` designed to replay against the exact same `core/signal_lifecycle` state transitions used live (extending the existing, correct `trading_engine/state_engine.is_signal_active` pattern) so backtest results can never silently diverge from live behavior — this is the single most important property a backtest engine can have, and this codebase already has the right instinct for it in one corner (`trading_engine/`); V2 generalizes it everywhere.

---

## 4. Migration principles — how to get from Section 1 to Section 3 without a rewrite

1. **Never touch two engines in the same PR.** Each engine (strangle, swing, intraday, commodity) migrates independently into its `engines/<name>/` sub-package. This repo's own commit history this session (each logical change as its own commit) is the right instinct — apply it at the architecture-migration scale too.
2. **Delete dead code before refactoring live code.** Removing the ~460 dead lines in `pro_system_service.py` (§2.1) is a zero-risk, high-clarity first move — it can happen before any risk-engine or lifecycle-service work even starts, and it immediately removes the single biggest source of "which file is actually live" confusion in the codebase.
3. **New capability (option income structures) is the forcing function for the new abstractions**, not a separate cleanup project. Build `core/signal_lifecycle` and `core/risk_engine` *as part of* building the CSP/Covered Call engine (`03_OPTION_SELLING_ENGINE.md`) — retrofitting them onto the existing strangle engine afterward, once they're proven against a real new use case.
4. **`CLAUDE.md` gets corrected in Phase 1**, not left for later — every day it stays wrong is a day it actively misleads the next engineer (or AI session) that reads it first, exactly as instructed at the top of that file ("READ THIS FILE FIRST").

See `10_IMPLEMENTATION_ROADMAP.md` for this translated into dated, acceptance-criteria'd phases.
