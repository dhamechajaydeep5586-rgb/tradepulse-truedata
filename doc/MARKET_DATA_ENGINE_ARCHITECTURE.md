# Market Data Engine — Architecture Proposal

**Status: PROPOSED — awaiting approval. No code to be written until this is signed off.**

Source of truth: `CLAUDE.md`, `doc/INSTITUTIONAL_AUDIT_PLATFORM.md`,
`doc/SHORT_TERM_ENGINE_V2_ARCHITECTURE.md`, `doc/V2_BUILD_LOG.md`, and the current
implementation as of 2026-07-26.

Mandate: one centralized Market Data Engine that owns all Angel One SmartAPI access.
No engine (intraday, swing, long-term, option buying, option selling, specialist) may
call SmartAPI directly. Rate-limit safety is the highest priority; speed is explicitly
not a goal — a 5-minute scan is fine, a rate-limit ban is not.

---

## 0. Reuse-not-rebuild — what already exists

This is the most important section. A ground-up redesign would duplicate infrastructure
that already exists and that the platform's own audit rates as the strongest layer in
the codebase. Building this wrong — as a parallel system next to the current one — would
leave two competing paths to Angel One, which is the opposite of the goal.

**Already built, already good, to be *wrapped and formalized*, not replaced:**

| Capability | Where it lives today | Verdict |
|---|---|---|
| REST rate limiting (candle: 1.05s/call, bulk quote: 0.5s/call) | `angel_one_service.py` (`_candle_api_lock`, `_bulk_quote_api_lock`) | Keep as-is |
| Per-category circuit breaker (403/WAF → 5-min blackout, candle/quote isolated) | `angel_one_service.py` (`_REST_CIRCUIT_BREAKER_UNTIL`) | Keep as-is |
| Session-expiry auto-recovery (AG8001 → re-auth → single retry) | `angel_one_service.py` (3 call sites) | Keep as-is |
| WebSocket singleton + self-healing restart | `angel_one_service.py` + `angel_one_streamer.py` | Keep as-is |
| WS subscription batching queue (2s drain, chunks of 50) | `angel_one_service.py` (`_PENDING_SUBSCRIPTIONS`, `_batch_subscriber_worker`) | Keep as-is |
| Persistent candle cache with delta-fetch ("don't refetch what we have") | `candle_store.py` + `CandleBar` model | Keep, **adopt everywhere** (§ gap below) |
| Shared, profile-parameterized indicator/portfolio layer | `shared/` (`universe.py`, `regime.py`, `ranking.py`, `risk_engine.py`, `portfolio_risk.py`, `sector.py`, `cost_model` in `trading_engine/`) | Keep as-is |
| DB-backed queue pattern (write pending rows, poll-and-drain job) | `telegram_service.py` + `TelegramLog` + `telegram_queue_dispatcher` cron | Reuse this *pattern* for new queues |

**Genuinely missing — this proposal's actual scope:**

1. Redis. Cache is `FileBasedCache` today; already flagged in `SHORT_TERM_ENGINE_V2_ARCHITECTURE.md` §8 as deployment-order item #1 and **not done**. The kill switch, scan-cooldown locks, and every new queue in this design need a durable, atomic store. This is the one hard prerequisite everything else depends on.
2. Universal `candle_store` adoption. Confirmed gap: `swing_service.py` and all of `shared/` (`regime.py`, `universe.py`, `portfolio_risk.py`, `sector.py`) call `svc.get_candle_data()` **directly**, bypassing the cache. Only `intraday_service.py` goes through `candle_store.get_candles()`. This is free rate-limit reduction sitting unclaimed.
3. A master scan queue / worker tier. Confirmed absent — `INSTITUTIONAL_AUDIT_PLATFORM.md` scores the scheduler 38/100 for exactly this: "APScheduler in-process... no queue, no worker tier." Every cron job independently triggers its own Angel One calls; the only coordination is the shared rate limiter.
4. A single **Market Data Gateway** boundary — a module every engine imports instead of `angel_one_service` directly, so "no engine calls SmartAPI directly" becomes enforceable (today it's a convention, not a boundary — `swing_service.py` already violates it).
5. Concurrency safety across cron jobs — `INSTITUTIONAL_AUDIT_PLATFORM.md` §2 notes `run_daily_scanner` and `run_periodic_scanners` use different lock keys, so two scans can run concurrently against the one shared `requests.Session`. A real queue fixes this by construction (one drain loop, one job at a time).
6. Corporate-action price adjustment — ingestion exists (`shared/calendar_service.py`), the adjustment math does not. Out of scope for this proposal (it's a data-correctness problem, not a rate-limit/queue problem) but noted since it touches the same candle pipeline.

**Constraint this design must not violate:** the app runs `gunicorn --workers 1 --worker-class gthread --threads 4` specifically because Angel One login + APScheduler must be single-process (`start.sh`). SmartAPI also invalidates a session's WebSocket/REST tokens when a second login happens elsewhere — so there can only ever be **one authenticated Angel One client in existence**, by the broker's own design, not just this app's choice. Every option below is evaluated against that constraint.

---

## 1. Architecture Diagram

```
                          ┌─────────────────────────────────────┐
                          │      SCHEDULER (APScheduler)         │
                          │  master_scan_cycle() — one cron      │
                          └───────────────┬───────────────────┘
                                          │ enqueues work
                                          ▼
                     ┌────────────────────────────────────────┐
                     │         MARKET DATA GATEWAY              │
                     │  (the ONLY module that imports           │
                     │   angel_one_service / SmartAPI)           │
                     │                                          │
                     │  ┌────────────┐  ┌──────────────────┐   │
                     │  │ WS Manager │  │ Rate Limiter /     │   │
                     │  │ (singleton)│  │ Circuit Breaker    │   │
                     │  └─────┬──────┘  │ (per-category,     │   │
                     │        │         │  existing logic)   │   │
                     │        │         └─────────┬──────────┘   │
                     │        │                   │              │
                     │  ┌─────▼───────────────────▼───────────┐ │
                     │  │     Download Queue Consumer          │ │
                     │  │  (drains DownloadRequest table,       │ │
                     │  │   1 request in flight at a time,      │ │
                     │  │   respects existing 1.05s/0.5s locks) │ │
                     │  └─────────────────┬─────────────────────┘ │
                     └────────────────────┼───────────────────────┘
                                          │ writes
                                          ▼
              ┌───────────────────────────────────────────────────┐
              │                  CACHE / STORE TIER                 │
              │  Redis: LTP, OI, locks, kill-switch, job state       │
              │  Postgres CandleBar: historical + intraday bars      │
              │  IndicatorCache (Redis, TTL'd): EMA/RSI/MACD/VWAP/    │
              │    ATR/ADX/VolumeProfile/RelativeStrength, per        │
              │    (symbol, interval, indicator) — computed ONCE      │
              └──────────────────────────┬────────────────────────┘
                                          │ reads (never writes, never calls SmartAPI)
              ┌───────────────────────────┼───────────────────────────┐
              ▼                          ▼                           ▼
      ┌───────────────┐         ┌───────────────┐          ┌───────────────────┐
      │ Intraday Engine│         │  Swing Engine  │          │ Option Buy / Sell  │
      └───────────────┘         └───────────────┘          └───────────────────┘
              ▼                          ▼                           ▼
      ┌───────────────────────────────────────────────────────────────────────┐
      │                          SIGNAL QUEUE                                  │
      │        (candidate signals → ranking/portfolio-constraint pass →       │
      │                  persisted SignalHistory / TradeOutcome)              │
      └──────────────────────────────┬──────────────────────────────────────┘
                                     ▼
      ┌───────────────────────────────────────────────────────────────────────┐
      │   TELEGRAM QUEUE (exists)  │  DASHBOARD QUEUE  │  PORTFOLIO QUEUE      │
      └───────────────────────────────────────────────────────────────────────┘
```

Key property: everything **below** the Cache/Store Tier line never touches SmartAPI.
Every engine becomes a pure function of (cached candles, cached indicators, cached LTP)
→ signals. This is what makes "no duplicate API calls" enforceable rather than aspirational.

---

## 2. Queue Architecture

All queues use the **same proven pattern already in production** for Telegram: a
Postgres table of rows with a `status` column (`PENDING → IN_PROGRESS → DONE/FAILED`),
drained by a single poller job, with `select_for_update(skip_locked=True)` to make it
safe even though only one process ever runs. Redis is used for **hot, ephemeral** state
(locks, cooldowns, kill switch, indicator cache) — not as a message broker. This avoids
introducing Celery/RQ (a new worker-tier dependency) while still getting durable,
resumable, ordered queues, which is all seven of the requested queues actually need at
this app's scale (hundreds, not millions, of items per cycle).

| Queue | Backing | Producer | Consumer | Purpose |
|---|---|---|---|---|
| **Download Queue** | Postgres (`DownloadRequest`) | Scheduler (`master_scan_cycle`), any engine needing an off-cycle refresh | Gateway's single drain loop | "Fetch candles for symbol X, interval Y, since Z" — deduped, ordered, rate-limiter-aware |
| **Indicator Queue** | Postgres (`IndicatorRequest`) or inline (see below) | Download Queue consumer, on each symbol's data landing | Indicator worker (in-process function call, not a separate service — see §9 rationale) | "Compute EMA/RSI/MACD/VWAP/ATR/ADX/VP for symbol X now that new bars exist" |
| **Signal Queue** | Postgres (`SignalCandidate`) | Each engine's scan step | Ranking + portfolio-constraint pass (`shared/ranking.py`, `shared/portfolio_risk.py`, both exist) | Decouples "engine found a candidate" from "candidate survives portfolio caps and gets persisted" |
| **Telegram Queue** | Postgres (`TelegramLog`) — **exists today, unchanged** | Signal persistence, status jobs | `telegram_queue_dispatcher` (exists) | Outbound notifications |
| **Dashboard Queue** | Redis pub/sub or a simple "dirty" flag + short TTL cache | Signal Queue consumer | Frontend polling endpoints (`LiveSignalsTable.jsx` etc., unchanged) | Invalidates/refreshes the small API-response caches so the dashboard reflects a completed scan without recomputing on every request |
| **Notification Queue** | Same table as Telegram Queue, different `channel` field | Risk breaches (daily loss halt), engine failures | Same dispatcher | Ops alerts (rate-limit breaker tripped, scan failed, session expired repeatedly) — piggybacks on the existing dispatcher rather than a new one |
| **Portfolio Queue** | Not a queue — a **read-through service** (`shared/portfolio_risk.py::cross_engine_gross_exposure`, exists) | n/a | Every engine's sizing step, before persisting a signal | Already exists; keep as a synchronous call, not a queue — it must block signal creation, not happen asynchronously |

Note on Indicator Queue: it's listed because the user's spec calls for one, but the
correct implementation is **not** a separate async hop — it's a function call
immediately after `candle_store.get_candles()` returns fresh data, writing into the
Redis `IndicatorCache` before the Download Queue consumer moves to the next symbol.
Making it a genuinely separate async queue would add latency and a new failure mode
for no benefit at this data volume (≤2000 symbols). "One calculation, shared by every
engine" is achieved by the cache being keyed by `(symbol, interval)` and read by all
three engines — not by the calculation being decoupled into its own worker.

---

## 3. Scheduler Architecture

Replace N independent cron jobs each hitting Angel One with **one master cycle** that
owns the entire data-refresh path, followed by engine-specific cron jobs that only
*read* from cache. This directly fixes the confirmed concurrency bug (`INSTITUTIONAL_AUDIT_PLATFORM.md`
§2: two scanners can run concurrently against one shared session today).

```
09:00  Pre-open: WS Manager connects, subscribes index + F&O ban list refresh
                 (event_filter_service.py — unchanged, NSE-direct, not Angel One)
09:10  Universe Refresh   — shared/universe.py, per profile (intraday/swing/long_term),
                             already cached 24h; only re-fetched if stale
09:15  MARKET OPEN
09:15–15:20   Master Scan Cycle, every 5 min (existing intraday cadence):
   ├─ 1. Download Queue drain — fetch only symbols whose cached bar is stale
   │      (candle_store.latest_stored_ts comparison — exists, just needs to gate
   │       the request instead of being bypassed)
   ├─ 2. Indicator Cache refresh — inline, per symbol, as data lands
   ├─ 3. Intraday Engine scan — reads cache only, ranks, emits Signal Queue rows
   ├─ 4. Option Selling (delta_hedge) scan — reads same cache, own cadence gate
   └─ 5. Signal Queue drain — portfolio constraints, persist, notify
15:25  EOD evaluation (exists: run_eod_evaluation)
16:05  Swing Engine — daily-bar profile, reads EOD-settled candles from cache
16:30  Daily pipeline / corporate-action + earnings sync (exists)
Weekly Sat 06:00  Expiry cleanup (exists, unchanged)
```

This is *not* a new set of wall-clock times to invent — it is the **existing schedule**
(`updater.py`'s current cron table, confirmed by direct inspection, not docstrings) with
one structural change: step 1 (data fetch) is extracted out of each engine and run once,
first, shared. Today `intraday_service`, `swing_service`, and `delta_hedge_service` each
independently decide what to fetch; after this change, they each declare *what they need*
(symbol list + interval) and the Download Queue consumer is the only thing that talks to
Angel One to satisfy it.

Scan-rate guards already in `intraday_service.py` (5-min cooldown cache key) and the
option engine become guards on **enqueueing into the Download Queue**, not on calling
Angel One directly — same effect, one enforcement point instead of three.

---

## 4. WebSocket Architecture

No redesign — formalize what exists. `angel_one_service.py`'s `AngelOneService.streamer`
(backed by `angel_one_streamer.py`'s `AngelOneStreamer`, itself already using an internal
`queue.Queue` to decouple socket-frame receipt from tick processing) **is** the one
websocket manager. The only change: engines currently reaching into
`get_angel_one_instance()` and pulling `.streamer` state directly should instead read
LTP through the Gateway's read API (which internally reads the same in-memory tick
store or a thin Redis mirror of it), so that "one websocket, many consumers" is enforced
by import boundaries, not convention. Concretely:

- Subscription requests from any engine go through the existing `_PENDING_SUBSCRIPTIONS`
  batching queue (unchanged — it already does exactly this).
- LTP reads for the 1-second dashboard price ticker go through the Gateway's
  `get_ltp(symbols)` read function, which is a thin wrapper — no new polling of Angel
  One's REST quote endpoint is introduced (CLAUDE.md already mandates this: "always
  prefer WebSocket over REST for LTP").
- Self-healing restart logic (`_STREAMER_RESTART_LOCK`) is unchanged.

---

## 5. Cache Architecture

Three tiers, each with a distinct durability/latency profile:

| Tier | Backing | Contents | TTL / durability |
|---|---|---|---|
| **Hot** | Redis (new) | LTP snapshot, open interest, scan cooldown locks, daily-loss kill switch, circuit-breaker state, IndicatorCache | Seconds–minutes; must survive process restart (today's `FileBasedCache` mostly does, but is explicitly flagged as not safe under any future multi-instance deploy) |
| **Warm** | Postgres `CandleBar` (exists) | Historical + today's closed bars, per (symbol, exchange, interval) | Permanent; append-only, unique-constrained, this is the system of record for candles |
| **Cold** | Postgres (existing tables) | `SignalHistory`, `TradeOutcome`, `CorporateAction`, `EarningsEvent`, `PromoterGroup` | Permanent |

Rule enforced by the Gateway, not by convention: **every read of historical candles
goes through `candle_store.get_candles()`**, which itself decides warm-cache-hit vs.
delta-fetch vs. full-fetch. Today three of five consumers bypass this; closing that gap
(§0 item 2) is pure win with zero architecture change — `candle_store.py`'s API
(`get_candles`, `load_bars`, `backfill_symbol`) already supports every current call site's
needs, it's an adoption task, not a design task.

---

## 6. API Calling Sequence

For one 5-minute intraday cycle across the Nifty 100 universe (worst case: no cache hits,
first scan of the day):

1. Scheduler fires `master_scan_cycle()`.
2. Universe check: cache hit (24h TTL) → no NSE call.
3. For each of ~100 symbols: `candle_store.get_candles()` checks `latest_stored_ts`.
   - If within one bar's duration of "now" → **zero API calls**, return cached frame.
   - Else → enqueue a `DownloadRequest(symbol, interval, since=last_stored_ts)`.
4. Download Queue consumer drains sequentially, respecting the existing 1.05s/call lock
   and per-category circuit breaker. Worst case (no prior cache): 100 calls × 1.05s ≈
   105s. With the cache warm after the first scan of the day, subsequent 5-min cycles
   only fetch symbols whose last bar aged out — typically a handful, not the full 100.
5. Each response is written to `CandleBar` (warm tier) via `candle_store.store_bars()`,
   trimming the still-forming last bar (existing behavior).
6. Indicator Cache updated inline per symbol as its bars land (no separate round trip).
7. Once all requested symbols have landed (or the queue empties), Intraday Engine reads
   from cache only, ranks, applies cost gate, emits candidates to the Signal Queue.
8. Signal Queue consumer applies portfolio constraints (`shared/portfolio_risk.py`,
   exists), persists via `engine_persist_live_signal_history` (exists, unchanged),
   enqueues Telegram notification (exists, unchanged).
9. Swing Engine, Option Selling, and (future) Long-Term engine repeat step 7–8 against
   the *same* cached candles/indicators produced in steps 3–6 for symbols they also
   cover — no re-fetch, this is the "download once, distribute to every strategy"
   requirement satisfied directly.

Bulk quotes (`get_bulk_quotes`, 0.5s/call throttle) follow the identical enqueue/drain
pattern for engines needing a quote snapshot rather than a full candle history (e.g. the
1-second dashboard ticker reads LTP from the WS tick store instead, per §4 — bulk REST
quotes are reserved for symbols not currently subscribed on the socket).

---

## 7. Retry Strategy

Formalize the existing per-call-site retry logic (`angel_one_service.py`'s AG8001
handling) into one policy applied uniformly by the Download Queue consumer:

- **Transient (timeout, connection error, 5xx):** exponential backoff with jitter,
  base 2s, cap 60s, max 5 attempts per `DownloadRequest` row before marking `FAILED`.
- **429 / rate-limit signal:** do not retry the individual request — instead trip the
  existing category circuit breaker (already does a 5-min blackout) and re-queue the
  row for after the blackout window; this matches current 403/WAF handling exactly,
  extended to cover an explicit 429 if Angel One ever returns one distinctly from the
  WAF-HTML 403 it returns today.
- **AG8001 / invalid session:** unchanged — forced re-auth, one immediate retry, per
  existing logic. If re-auth itself fails, halt the Download Queue consumer entirely
  (do not keep retrying against a dead session) and fire a Notification Queue alert.
- **Row-level checkpointing:** each `DownloadRequest` carries its own status and
  attempt count. If the process restarts mid-cycle (deploy, crash), the consumer resumes
  by querying for `PENDING`/`IN_PROGRESS`-but-stale rows — "resume from last completed
  symbol" falls out of the table design for free, no separate checkpoint file needed.
- **Never duplicate:** a request is only enqueued if no `PENDING`/`IN_PROGRESS` row
  already exists for the same `(symbol, interval, exchange)` — a unique constraint on
  those columns (while status is non-terminal) makes double-enqueueing a DB-level
  impossibility, not a runtime check that can race.

---

## 8. Rate-Limit Strategy

The single-writer constraint (§0) is actually an advantage here: because exactly one
process ever holds the Angel One session, rate limiting does **not** need to be a
distributed token bucket (Redis-coordinated across workers) — the existing in-process
locks (`_candle_api_lock`, `_bulk_quote_api_lock`) are already correct and sufficient, and
should be kept exactly as-is. What changes is *who* is subject to them: today, any code
path that imports `angel_one_service` gets the throttle; after this change, **only the
Download Queue consumer** ever calls those functions, so the throttle governs a single
serialized drain loop instead of N independently-scheduled cron jobs racing each other
(closing the confirmed concurrent-scan bug).

Priorities (per the user's requested ordering), implemented as queue priority, not
separate rate budgets:
1. WebSocket maintenance (connect/reconnect/subscribe) — always processed first, never
   queued behind candle downloads.
2. Live LTP reads — served from the WS tick store / hot cache, not rate-limited at all
   since they don't call REST.
3. Historical candle downloads — the Download Queue, throttled at 1.05s/call.
4. Indicator computation — inline, effectively free (CPU, not API).
5. Scanners — read cache only, never wait on rate limits directly; they wait on the
   Download Queue finishing, which is a queue-depth wait, not a rate-limit wait.

---

## 9. Database Architecture

New tables (Postgres, via Django migration), modeled directly on the existing
`TelegramLog` pattern:

**`DownloadRequest`**
```
symbol, exchange, interval        — what to fetch
since_ts                          — delta-fetch starting point (candle_store already computes this)
status                            — PENDING / IN_PROGRESS / DONE / FAILED
attempts, last_error
requested_by                      — which engine/profile asked (for observability)
created_at, updated_at
unique_together: (symbol, exchange, interval) WHERE status NOT IN (DONE, FAILED)
index: (status, created_at)  — for the drain query
```

**`SignalCandidate`** (Signal Queue)
```
engine, symbol, direction, entry, stop, target, rank_score, rank_factors (JSON)
regime_snapshot (JSON)
status  — PENDING / ACCEPTED / REJECTED
reject_reason  — sector cap / cluster cap / promoter-group cap / gross exposure / cost gate
created_at
```
This gives the portfolio-constraint pass (already implemented logic in
`shared/portfolio_risk.py`) an audit trail of *rejected* candidates too, which doesn't
exist today — currently a rejected candidate simply never becomes a `SignalHistory` row
and the reason is only in logs.

Existing tables, unchanged, reused as-is: `CandleBar`, `SignalHistory`, `ShortTermSignal`,
`TradeOutcome`, `PromoterGroup`, `CorporateAction`, `EarningsEvent`, `TelegramLog`.

Redis (new dependency): key namespaces `ltp:{symbol}`, `oi:{symbol}`, `indicator:{symbol}:{interval}:{name}`,
`lock:scan_cooldown:{engine}`, `breaker:{category}`, `kill_switch:daily_loss`. This is a
straight lift of what `FileBasedCache` holds today, onto a backend that's actually safe
under process restarts and (if ever needed) multiple instances — matching the deployment
order already specified in `SHORT_TERM_ENGINE_V2_ARCHITECTURE.md` §8.

---

## 10. Folder Structure

```
backend/stocks/services/
├── market_data/                        [NEW package — the Gateway]
│   ├── __init__.py                     Public read API every engine imports
│   ├── gateway.py                      Wraps angel_one_service; the ONLY module
│   │                                    outside angel_one_service.py/streamer.py
│   │                                    allowed to import them
│   ├── download_queue.py               DownloadRequest enqueue/drain, checkpointing
│   ├── indicator_cache.py              Redis-backed indicator read/write, wraps
│   │                                    existing signal_utils.py compute_* functions
│   │                                    (no reimplementation — they already exist)
│   ├── ws_read.py                       get_ltp()/get_oi() thin read wrapper over the
│   │                                    existing streamer's tick store
│   └── models.py                        DownloadRequest, SignalCandidate
├── angel_one_service.py                 UNCHANGED — wrapped, not modified
├── angel_one_streamer.py                UNCHANGED
├── candle_store.py                      UNCHANGED — becomes the sole implementation
│                                         backing market_data/download_queue.py's writes
├── shared/                              UNCHANGED — already the correct target for
│                                         indicator/portfolio logic; now consumes
│                                         market_data/ instead of angel_one_service
│                                         directly (regime.py, universe.py,
│                                         portfolio_risk.py, sector.py each get their
│                                         3-6 direct svc.get_candle_data() call sites
│                                         swapped for candle_store.get_candles())
├── intraday_service.py                  MODIFIED — declares data needs, no longer
│                                         calls candle_store directly mid-scan
├── swing_service.py                     MODIFIED — same
├── delta_hedge_service.py               MODIFIED — same
└── trading_engine/                      UNCHANGED — consumes candle_store already via
                                          replay.py/backtest.py, no change needed
```

---

## 11. File-by-File Implementation Plan

1. **`market_data/models.py`** — `DownloadRequest`, `SignalCandidate` models + migration.
2. **`market_data/gateway.py`** — thin functions `request_candles(symbol, interval, lookback)`,
   `request_bulk_quote(symbols)`, delegating to existing `angel_one_service` functions.
   No new rate-limit logic here — it calls the existing throttled functions.
3. **`market_data/download_queue.py`** — `enqueue(symbol, interval, exchange)` (dedup via
   unique constraint), `drain_once()` (called by the scheduler, processes all `PENDING`
   rows respecting existing locks, writes via `candle_store.store_bars`).
4. **`market_data/indicator_cache.py`** — `get_or_compute(symbol, interval, name, compute_fn)`
   wrapping `signal_utils.py`'s existing `compute_atr`/`compute_adx`/etc.; Redis-backed
   with TTL slightly longer than the bar interval.
5. **`market_data/ws_read.py`** — `get_ltp(symbols) -> dict`, reading the existing
   streamer's in-memory tick store (no new subscription logic).
6. **Update `shared/regime.py`, `shared/universe.py`, `shared/portfolio_risk.py`,
   `shared/sector.py`** — replace each direct `svc.get_candle_data(...)` call with
   `candle_store.get_candles(...)` / route through `market_data.gateway`. This is the
   single highest-leverage change in this whole plan: it's the gap identified in §0
   item 2, requires no new abstraction, and immediately cuts redundant Angel One calls
   from four of the five `shared/` modules that currently bypass the cache.
7. **Update `intraday_service.py`, `swing_service.py`, `delta_hedge_service.py`** —
   replace ad hoc "fetch what I need" logic with "declare what I need" (call
   `download_queue.enqueue` for anything not already fresh, then read from cache after
   the scheduler's drain step completes).
8. **Update `updater.py`** — insert the Download Queue drain as an explicit first step
   in the existing cron jobs (or a new preceding job at the same cadence), per §3.
   No change to existing job *times* — only to what happens before each one calls its
   engine's scan function.
9. **Redis provisioning** — settings change (`CACHES` backend), no application code
   change beyond the key namespaces above; this is infra, not new modules.

---

## 12. Migration Plan

Phased so each step is independently shippable and reversible, and none of it
contradicts or races the swing-V2 shadow rollout already in progress
(`doc/V2_BUILD_LOG.md` — currently Phase 4 of that plan, shadow-only). This proposal
sits *underneath* that rollout, not beside it — the swing-V2 cutover criteria (~20 shadow
sessions) are unaffected by any phase here.

| Phase | Work | Risk | Reversible? |
|---|---|---|---|
| 0 | Provision Redis; point `CACHES` at it; keep `FileBasedCache` code path available behind a setting for one deploy cycle | Low — pure infra swap, existing keys/TTLs unchanged | Yes, flip setting back |
| 1 | Add `market_data/` package as a thin wrapper with zero behavior change (gateway functions just call existing `angel_one_service` functions 1:1) | Very low — no logic changes | Yes, delete package |
| 2 | Migrate the 4 `shared/` modules' direct Angel One calls to `candle_store`/`market_data.gateway` (§11 item 6) | Low — `candle_store`'s output shape already matches what these callers expect (a DataFrame); this is the same migration `intraday_service.py` already did successfully | Yes, revert per-file |
| 3 | Add `DownloadRequest` queue + drain step; wire into `updater.py` ahead of each engine's existing scan step, **without** removing engines' own direct-call fallback yet (dual-path, compare outputs) | Medium — first structural change to the scheduler | Yes, remove the drain step, engines still work standalone |
| 4 | Remove engines' direct-call fallback once dual-path comparison shows parity for ~1-2 weeks | Medium — this is the "one-way door" step, same caution level as `SHORT_TERM_ENGINE_V2_ARCHITECTURE.md`'s Phase 5 | Requires re-adding fallback code, not just a flag |
| 5 | `SignalCandidate` audit table for rejected candidates (observability improvement, no behavior change to what gets persisted) | Low | Yes |

---

## 13. Testing Strategy

Follow the pattern already established by `tests_intraday_v2.py`/`tests_intraday_v3.py`/
`tests_swing_v2.py` (unit tests over pure functions, no live broker calls):

- **Download Queue**: unit-test `enqueue`/`drain_once` against a fake gateway that
  returns canned responses, canned 429s, canned AG8001s — verify dedup, backoff timing,
  checkpoint/resume after a simulated mid-drain crash (kill the process, restart, assert
  no duplicate Angel One calls and no lost rows).
- **Circuit breaker parity**: existing per-category breaker logic is unit-tested today
  (if not, add tests) before wrapping it — a regression here reintroduces the exact
  403-blast-radius bug `CLAUDE.md`'s bug history already fixed once.
- **`shared/` migration (Phase 2)**: for each of the 4 modules being switched to
  `candle_store`, a before/after test that feeds the same symbol/date range through
  both the old direct call and the new cached path and asserts identical output —
  this is the same discipline `swing_signals.py` was built with ("what makes the
  backtest and the live path provably identical").
- **Dual-path comparison (Phase 3-4)**: log both the old direct-fetch result and the
  new queue-mediated result for every symbol during the parallel-run window; alert on
  divergence rather than trusting it silently.
- **Load/scale test**: synthetic run against 500 → 1000 → 2000 symbols (mocked Angel One
  responses) measuring queue drain wall-clock time, to validate §14's estimates before
  they're needed for real.
- **Chaos test**: simulate Angel One returning 403 mid-drain, verify the breaker trips,
  the queue backs off, and — critically — resumes correctly, with no requests silently
  dropped and no duplicate calls once the breaker window closes.

---

## 14. Performance Expectations

Explicitly **not** optimizing for speed, per the mandate — these are sanity bounds, not
targets:

| Universe size | Cold cache (worst case) | Warm cache (typical 5-min cycle) |
|---|---|---|
| 100 (current Nifty 100 intraday) | ~105s (100 × 1.05s) | Seconds — only aged-out symbols re-fetch |
| 500 | ~525s (~8.75 min) | Tens of seconds |
| 1000 | ~1050s (~17.5 min) | ~1 min |
| 2000 | ~2100s (~35 min) | ~2 min |

This is why the candle cache (§0 item 2, §5) is not optional at 1000-2000 scale — a
cold-cache-every-cycle design would make a 5-min scan interval structurally impossible
above ~250 symbols even before considering rate-limit safety margin. The architecture
scales by *avoiding* re-fetches, not by parallelizing calls (parallelizing would violate
the single-writer/rate-limit constraint that is the highest priority here). Scaling from
500 to 2000 symbols requires zero redesign under this proposal — it's the same queue,
same drain loop, same cache, just more rows and a longer first-cold-scan-of-the-day.

---

## 15. Risk Analysis

| Risk | Mitigation |
|---|---|
| The one Angel-One-owning process crashes mid-drain | Row-level checkpointing (§7) means restart resumes cleanly; supervisor (Render's own process restart) brings it back; no queue state is lost because it's in Postgres, not memory |
| Redis becomes a new single point of failure | Redis is a well-understood, restartable dependency; unlike the Angel One session it does not require re-authentication — a Redis restart loses only ephemeral hot-tier data (LTP cache, locks), all of which is cheaply rebuilt from the WS stream / next scan cycle. Warm/cold tiers (Postgres) are unaffected |
| Queue backs up (Download Queue depth grows faster than it drains) | Bounded by universe size (≤2000) and cache-hit rate; monitor queue depth, alert via Notification Queue if depth exceeds a threshold at cycle end — this is a symptom of a rate-limit or breaker problem upstream, not a queue design flaw |
| Migrating `shared/` modules to `candle_store` introduces subtle output differences (Phase 2) | Before/after parity tests (§13) required before cutover, not after |
| Someone adds a new engine later and imports `angel_one_service` directly, bypassing the Gateway | Enforce via code review / a lint rule (no direct `angel_one_service` imports outside `market_data/`) rather than runtime — this is the same discipline `shared/__init__.py`'s existing docstring already states ("no module here imports from an engine module") |
| Two-process temptation (splitting Gateway into a separate worker dyon for "real" isolation) | Explicitly rejected in this design — SmartAPI's own single-session-per-login behavior means a second process would just fight the first for the token, not add capacity. If ever revisited, it requires Angel One's explicit multi-session support (not currently used), not just infra effort |
| Corporate-action price gaps still unadjusted in cached candles | Out of scope here — flagged, not silently absorbed. Should be resolved before `CandleBar` is trusted as the sole source for swing/long-term backtests spanning a split |

---

## Summary

The user's request is not "no infrastructure exists, design one" — it's "close the gap
between infrastructure that already exists and the 'no engine calls SmartAPI directly'
guarantee that hasn't been enforced yet." Concretely: provision Redis, wrap the existing
(already good) `angel_one_service.py` behind one `market_data/` package boundary, migrate
the four `shared/` modules that still bypass `candle_store` onto it, and add one Download
Queue table with the same shape as the Telegram queue that already works in production.
Nothing here proposes Celery, a second worker process, or a rewrite of the broker layer —
all three would violate either the single-Angel-One-session constraint or the "don't
rebuild what's already rated well" principle from §0.
