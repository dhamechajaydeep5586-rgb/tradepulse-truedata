# TradePulse AI — Short-Term (Swing) Stock System

**Reverse-engineered from source. Code is the only source of truth.**

Everything below is derived directly from the files listed in "Source Files". Where the
implementation does not contain something the reader might expect (fundamentals, VIX,
breadth, sector rotation, position sizing on the server, etc.), this document says so
explicitly rather than inventing it.

---

## SCOPE OF THIS DOCUMENT

TradePulse AI runs **six** independent signal categories. This document covers exactly one:

| Category | Model / `category` value | Holding period | Documented in |
|---|---|---|---|
| **Short-term swing (this doc)** | `ShortTermSignal` (own table) | 15–90 days (ATR-derived) | **this file** |
| Long-term | `SignalHistory(category="long_term")` | 1–2 years | `doc/long_term_stock.md` |
| Intraday | `SignalHistory(category="intraday")` | same day | `doc/INTRADAY_BUY_SELL_LOGIC.md` + Appendix A here |
| Option selling / specialist | `SignalHistory(category="specialist")` | intraday–weekly | `delta_hedge_service.py` (not covered) |
| Option buying | `SignalHistory(category="option_buying")` | intraday | `option_buying_service.py` (not covered) |
| Option sniper | `SignalHistory(category="option_selling")` | intraday | not covered |

The short-term swing system is **long-only**. There is no SELL/short path anywhere in
`trade_engine.py`. `get_dashboard_data()` hardcodes `'signal': 'BUY'` for every row
(`trade_engine.py:1370`).

### Source Files

| File | Role |
|---|---|
| `backend/stocks/services/trade_engine.py` | **The engine.** Scanner, AI scoring, lifecycle, exits, Telegram, dashboard payload |
| `backend/stocks/config/strategy_config.json` | All tunable thresholds (loaded at import time) |
| `backend/stocks/services/pro_system_service.py` | `get_market_direction()`, `NIFTY500_FALLBACK`, legacy parallel scanner, performance report |
| `backend/stocks/services/angel_one_service.py` | Broker/data integration (auth, quotes, candles, WebSocket, rate limits, circuit breaker) |
| `backend/stocks/services/angel_one_streamer.py` | WebSocket tick cache |
| `backend/stocks/services/market_data_orchestrator.py` | Unified price lookup layer (`get_price`, `get_prices_bulk`) |
| `backend/stocks/services/bhavcopy_service.py` | `_fetch_nifty500_symbols()`, `_get_session()` — universe source |
| `backend/stocks/services/telegram_service.py` | Alert delivery + async DB queue |
| `backend/stocks/updater.py` | APScheduler cron registration |
| `backend/stocks/models.py` | `ShortTermSignal`, `TradeHistory`, `TelegramLog` |
| `backend/stocks/views.py` | `ProSystemView`, `ProPerformanceReportView`, `DashboardSummaryView`, `CronScannerTriggerView` |
| `frontend/src/pages/ProSystem.jsx` | UI (short-term view is the default) |
| `frontend/src/pages/PerformanceReports.jsx` | Historical report UI |
| `frontend/src/pages/Dashboard.jsx` | Preview card |

---

# SECTION 1 — Complete System Architecture

## 1.1 Frontend

React 18 + Vite, TailwindCSS, `react-router-dom`, axios wrapper at `frontend/src/api/axios.js`.

| Page | Route | Endpoint consumed | Refresh |
|---|---|---|---|
| `pages/ProSystem.jsx` | `/pro-system` (default view = `short_term`; `?view=long_term` switches) | `GET /api/stocks/pro-system/` | Manual only — `useEffect(() => { fetchData(); }, [fetchData])` with `fetchData` memoised on `[]`, so **it fires once per mount. There is no `setInterval` on this page.** |
| `pages/PerformanceReports.jsx` | `/reports` | `GET /api/stocks/performance-report/` + `GET /api/stocks/pro-performance-report/` | `setInterval(fetchReport, 60000)` — 60 s (`PerformanceReports.jsx:188`) |
| `pages/Dashboard.jsx` | `/` | `GET /api/stocks/dashboard-summary/` | Once on mount (`Dashboard.jsx:33-34`) |

UI state vocabulary is defined in `ProSystem.jsx:52-60` (`TAB_CONFIG`): `pending`, `active`,
`target1`, `target2`, `review_required`, `archived`, `expired`. Note the backend also emits
`cancelled`, `hit_target`, `hit_sl`, `closed` (legacy tabs) which the short-term `TAB_CONFIG`
does not render.

## 1.2 Backend

Django 4.2 + Django REST Framework, Python 3.10 (`backend/venv/lib/python3.10`).
JWT auth via `rest_framework_simplejwt`; every stock endpoint is
`permission_classes = (IsAuthenticated,)` except `CronScannerTriggerView` which is
`AllowAny` + shared-secret token.

## 1.3 Database

PostgreSQL via `dj_database_url` (`config/settings.py:104-109`, `conn_max_age=600`).

**Models actually used by this system:**

| Model | Table | Written by |
|---|---|---|
| `ShortTermSignal` | `short_term_signals` | `trade_engine._run_daily_scanner_impl`, `check_pending_activations`, `run_eod_evaluation`, `_exit_signal`, `run_expiry_cleanup`, `run_intraday_check`, `pro_system_service.get_pro_system_data` |
| `TradeHistory` | `trade_history` | every lifecycle transition in `trade_engine.py` |
| `TelegramLog` | `telegram_logs` | `queue_telegram_message`, `_send_telegram_event_alert`, `_exit_signal`, `check_pending_activations` |

**Models that exist but are never written by any live code path** (verified by grepping for
`.objects.` usage across the whole non-venv tree): `TradeScanner`, `Trade`, `StockDailyData`,
`SignalChangeLog`. `Stock` and `IntradaySignal` are **read** by `insights/services/ai_insight_service.py`
but nothing in the current codebase writes them (`bhavcopy_service.fetch_and_store_bhavcopy`
explicitly logs *"Bypassed DB upsert … (Local Saving Disabled)"* at line 282). The docstring at
the top of `trade_engine.py:16-17` claiming "All data flows through … `StockDailyData → TradeScanner
→ Trade → TelegramLog`" is **stale** — the real flow is `ShortTermSignal → TradeHistory → TelegramLog`.

## 1.4 Cache

`django.core.cache.backends.filebased.FileBasedCache`, directory `DJANGO_CACHE_DIR` or
`BASE_DIR/django_cache` (`settings.py:24-29`). **Not Redis.** This matters: the cache is
per-process-filesystem, and every distributed lock in this system (`trade_engine_scanner_running`,
`run_periodic_scanners_running`) relies on it.

## 1.5 Scheduler

APScheduler `BackgroundScheduler`, timezone `Asia/Kolkata`, started from
`stocks/apps.py::StocksConfig.ready()` → `stocks/updater.py::start()`.

Job defaults (`updater.py:220-224`): `misfire_grace_time=300`, `max_instances=1`,
`replace_existing=True`.

Start guard (`updater.py:205-210`): runs if `RENDER=true`, or `RUN_MAIN=true`
(Django autoreload child), or `--noreload` present in `sys.argv`.

Full job table → **Section 17**.

## 1.6 Broker

**Angel One SmartAPI**, hand-rolled over `requests` (no SmartAPI SDK).
Singleton: `angel_one_service.get_angel_one_instance()` → `initialize_angel_one()`.

- Auth: `POST /rest/auth/angelbroking/user/v1/loginByPassword` with `clientcode`,
  `password`, `totp` (generated via `pyotp.TOTP(totp_secret).now()`).
- Public IP is detected at login from `api.ipify.org` → `ident.me` → `ifconfig.me/ip`
  and sent as `X-ClientPublicIP` (Angel One IP-binds sessions).
- Session reused for 18 hours (`initialize_angel_one:1078`), then forced disconnect + re-auth.
- Failed auth backs off `AUTH_RETRY_COOLDOWN = 60.0` s.
- Credentials from `settings.ANGEL_ONE` (env: `ANGEL_ONE_CLIENT_ID`, `_PASSWORD`, `_API_KEY`,
  `_TOTP_SECRET`, `_BASE_URL`).

## 1.7 Data Providers

| Need | Provider | Call site |
|---|---|---|
| Nifty 500 universe | `IndexConstituent` DB table first, then NSE archive CSV `ind_nifty500list.csv` | `bhavcopy_service._fetch_nifty500_symbols()` |
| Universe fallback | Hardcoded `NIFTY500_FALLBACK` list (≈180 symbols) | `pro_system_service.py:32-77` |
| Symbol → token | Angel One instrument master JSON (`OpenAPIScripMaster.json`), re-indexed every 6 h | `angel_one_service._refresh_instrument_master()` |
| Snapshot quotes (LTP/OHLC/volume/%chg) | Angel One `POST /rest/secure/angelbroking/market/v1/quote/` mode `FULL` | `get_bulk_quotes()` |
| Live tick | Angel One WebSocket (`AngelOneStreamer`) → `_STREAM_CACHE` | `get_stream_price()` |
| Daily candles (365 d) | Angel One `POST /rest/secure/angelbroking/historical/v1/getCandleData`, interval `ONE_DAY` | `get_candle_data()` |
| Nifty 50 index | Angel One token `99926000` | `pro_system_service.NIFTY_50_TOKEN` |
| Market open/holiday | NSE `api/marketStatus` + `api/holiday-master?type=trading` + `MarketHoliday` DB + static `NSE_HOLIDAYS` set | `signal_utils.get_market_status()` |

**No yfinance, no Groww, no other vendor** anywhere in this pipeline.

## 1.8 APIs

| Method + Path | View | Purpose |
|---|---|---|
| `GET /api/stocks/pro-system/?force=` | `ProSystemView` | Full dashboard payload; 30 s cache key `trade_engine_dashboard_30s` |
| `GET /api/stocks/pro-performance-report/?date=YYYY-MM-DD` | `ProPerformanceReportView` | Read-only historical aggregate |
| `GET /api/stocks/dashboard-summary/` | `DashboardSummaryView` | Top-3 preview; 20 s cache, DB-reads only |
| `GET /api/stocks/live-price-updates/?symbols=A,B` | `LivePriceUpdateView` | Bulk LTP via orchestrator |
| `GET /api/stocks/cron-trigger/?token=…&action=…` | `CronScannerTriggerView` | External manual triggers |

`cron-trigger` actions relevant here: `trade_scan` (`run_daily_scanner`, `relaxed` from
`?relaxed=1` or `?force=1`), `trade_intraday` (`trade_engine.run_intraday_check`),
`trade_eod` (`run_eod_evaluation`), `short_term_scan` (routes into the **legacy**
`pro_system_service.get_pro_system_data(trigger_scan=True)`), `ping`, `test_telegram`.
Secret: `CRON_SECRET_TOKEN` env, default `"trade_pulse_secure_cron_trigger_2026"`.
Guarded by `is_market_open_today()` unless `&force=1`.

## 1.9 Signal Engine

`trade_engine._compute_ai_score(df, nifty_20d_ret, relaxed)` — six sequential hard filters
then a five-component 0–100 score. Full spec → **Sections 5, 6, 8**.

## 1.10 Trading Engine

Five scheduled entry points, all in `trade_engine.py`:

1. `run_premarket_update()` — 9:05 AM
2. `run_daily_scanner()` → `_run_daily_scanner_impl()` — 10:00 AM
3. `check_pending_activations()` — every 30 min, 10:15–15:45
4. `run_eod_evaluation()` — 3:25 PM
5. `run_expiry_cleanup()` — Saturday 6:00 AM

Plus `run_intraday_check()` — **defined but NOT registered in `updater.py`**. The scheduler's
`updater.run_intraday_check()` wrapper (line 67-74) calls `trade_engine.check_pending_activations()`,
not `trade_engine.run_intraday_check()`. The latter is reachable **only** via
`GET /api/stocks/cron-trigger/?action=trade_intraday` (`views.py:591-600`).

## 1.11 Notification System

**Telegram only.** Two delivery modes:

- **Synchronous** — `telegram_service.send_telegram_message(text, parse_mode="HTML", chat_id)`,
  direct `POST https://api.telegram.org/bot{token}/sendMessage`, 10 s timeout.
- **Asynchronous (the mode the swing engine uses)** — `queue_telegram_message()` writes a
  `TelegramLog` row with `status='PENDING'`; `process_telegram_queue()` (APScheduler, every
  1 minute) picks up ≤20 oldest `PENDING` rows with `retry_count < 3`, sends with
  `select_for_update()`, marks `SENT` or increments `retry_count`, and marks `FAILED` at 3.

Channel: `get_short_term_chat_id()` → `settings.TELEGRAM_ALERTS["SHORT_TERM_CHAT_ID"]` or
`TELEGRAM_SHORT_TERM_CHAT_ID` env. `process_telegram_queue()` **routes every queued message to
the short-term chat id regardless of origin** (`telegram_service.py:533-536`).

Idempotency: `queue_telegram_message()` and `_send_telegram_event_alert()` both check
`TelegramLog.objects.filter(short_term_signal=…, event_type=…).exists()` before writing.

**In-app notifications:** the `Notification` model and `NotificationView` exist, but
`Notification.objects.create(...)` appears **nowhere** in the codebase. Nothing populates it.

**WhatsApp:** `whatsapp_service.py` and `settings.WHATSAPP_ALERTS` exist; the short-term engine
never calls them.

## 1.12 AI Components

Two distinct things are called "AI" in this codebase; neither is an LLM inside the trading loop.

1. **`_compute_ai_score()`** (`trade_engine.py:170-314`) — a deterministic weighted-sum of five
   technical sub-scores. No model, no training, no inference. The name is branding.
2. **`insights/services/ai_insight_service.py`** — a genuine LLM call
   (`anthropic.Anthropic(api_key=settings.CLAUDE_API_KEY)`, model `claude-sonnet-4-20250514`,
   `_call_claude_api`) that writes a daily narrative `Insight` row. It reads the `Stock` /
   `IntradaySignal` tables (which nothing currently writes) and has **zero influence on
   short-term signal generation, entry, exit, or sizing.**

## 1.13 Portfolio Engine

There is **no server-side portfolio engine.** No capital, quantity, or position-size field
exists on `ShortTermSignal`. What exists:

- `get_dashboard_data()['analytics']` — counts, `win_rate`, `realized_pnl` and `unrealized_pnl`
  computed as **sums of percentages**, not currency (`trade_engine.py:1422-1430`).
- `ProSystem.jsx` client-side sizing: user types `capital` (default ₹5,00,000),
  `maxLossRs = round(capital * 1.5 / 100)`, `posSizeShares = floor(maxLossRs / (entry - SL))`
  (`ProSystem.jsx:106, 367-369`). This is display-only; it is never posted back.
- `pro_system_service.get_pro_performance_report()` uses a *different*, hardcoded
  `max_risk_inr = 5000` for its qty column (`pro_system_service.py:809-811`).

The two sizing rules disagree unless the user's capital is exactly ₹3,33,333.

---

# SECTION 2 — Daily Workflow

All times IST, Monday–Friday. Every job is registered with `day_of_week="mon-fri"`.

## Market Closed (overnight / weekend)

- APScheduler stays up. `run_user_cleanup` and `run_telegram_queue` fire every 1 minute
  **all days** (interval triggers, no day filter).
- `stocks/apps.py::ready()` calls `is_static_closed("NSE")`; if closed it **skips Angel One
  initialization entirely** — so on a holiday boot there is no broker session at all.
- Saturday 06:00 — `run_expiry_cleanup` (Section 15).

## Pre-Market — 09:05

`updater.run_premarket_update()` → `trade_engine.run_premarket_update()` →
`pro_system_service.get_market_direction()`.

- Fetches 120 calendar days of `ONE_DAY` candles for token `99926000` (Nifty 50).
- Requires `len(df) >= 51`.
- `trend = "BULLISH" if close > ema20 and close > ema50 else "BEARISH"`.
- On any failure returns `{"trend": "NEUTRAL", "close": 0, …}`.

**The result is logged and returned but not persisted anywhere.** The 10:00 scanner calls
`get_market_direction()` again independently. This 9:05 job is effectively a warm-up/log.

## 09:15 — Market Open

**Nothing in the short-term system runs at 9:15.** There is no opening-range logic, no
gap handler, no pre-open auction read. The first swing-relevant job is at 10:00.

## First Scan — 10:00 (the only signal-generating scan)

`updater.run_short_term_scan()` → `trade_engine.run_daily_scanner()`.

Overlap lock: `cache.add("trade_engine_scanner_running", True, timeout=600)`. If the key
already exists the run is skipped with a warning — this exists because two concurrent scans
share one `AngelOneService` singleton and one `requests.Session`, which was observed to corrupt
the TLS connection (`SSL "decryption failed or bad record mac"`).

Two-pass strategy (`run_daily_scanner:349-362`):
1. `_run_daily_scanner_impl(relaxed=False, send_telegram=False)` — strict.
2. If that returns an empty list (including the BEARISH short-circuit), run
   `_run_daily_scanner_impl(relaxed=True)` — relaxed, and *this* pass sends Telegram.

Consequence: on a strict-pass success **no Telegram is sent from that pass** because
`send_telegram=False` was passed. The daily report only goes out when the relaxed pass runs.

Detailed steps → **Section 5**.

## 10:05 — Status Update

`updater.send_short_term_status_update()`. Lists all `ACTIVE` holdings with live CMP/P&L
(via `MarketDataOrchestrator.get_price` per symbol) and all `PENDING` setups.
Queued with `event_type='SWING_STATUS_UPDATE'`. Returns early if both lists are empty.

## Second Scan — there is none

The swing scanner runs **once per day**. The only recurring intraday touch is the activation
checker below. `run_daily_scanner` cannot re-run the same day unless triggered manually via
`cron-trigger?action=trade_scan`.

## Signal Generation

Occurs only inside `_run_daily_scanner_impl` Step 7 — see Section 9.
Created rows are `status = PENDING`, `activated_at = NULL`.

## Trade Monitoring — 10:15 → 15:45, every 30 minutes

Cron: `hour="10-15", minute="15,45"` (`updater.py:254-264`). That is **12 firings**:
10:15, 10:45, 11:15, 11:45, 12:15, 12:45, 13:15, 13:45, 14:15, 14:45, 15:15, 15:45.

> The job id is `trade_engine_activation_checker` and the comment says "10:15 AM - 3:15 PM …
> every 30 minutes", but `hour="10-15"` includes hour 15 minute 45, i.e. **15:45, after the
> 15:30 close**. The docstring on `trade_engine.run_intraday_check` also says "Every 10 min" —
> no 10-minute job is registered.

Each firing runs `trade_engine.check_pending_activations()`:
1. Load all `status=PENDING` `ShortTermSignal` rows. Return early if none.
2. `svc.get_token_map(symbols, "NSE")`.
3. `svc.get_bulk_quotes({"NSE": chunk}, mode="FULL")` in chunks of 50.
4. For each: `trade.current_price = ltp; trade.save(update_fields=['current_price'])`.
5. **Activation condition: `ltp <= float(trade.entry_price)`.**
6. Inside `transaction.atomic()` with `select_for_update()`, re-check `status == PENDING`,
   then set `status=ACTIVE`, `activated_at=timezone.now()`, write `TradeHistory`
   (`PENDING→ACTIVE`, reason `'Entry Trigger Hit'`), queue `BUY_ACTIVATED` Telegram.

**No SL or target check happens in this loop.** During the trading day a swing position can
run through its stop-loss and its target without the system reacting; both are only evaluated
at 3:25 PM EOD.

## Target Monitoring

Only inside `run_eod_evaluation()` (3:25 PM) and inside the manually-triggered
`trade_engine.run_intraday_check()`. See Section 12.

## Stop Monitoring

Same: only at 3:25 PM EOD (and the manual `run_intraday_check`). See Section 11.

## Market Close — 15:30

No job fires at 15:30 for this system.

## Square Off — none

**The short-term swing system has no square-off.** Positions are multi-day by design; there is
no intraday cutoff, no forced EOD flatten. (Square-off exists only in the intraday engine —
Appendix A.)

## End of Day — 15:25, 15:35, 16:30

**15:25 — `run_eod_evaluation()`** (note: fires *before* the 15:30 close, so "latest close" is
actually the last completed daily candle available at 15:25):

For every signal in `status ∈ {ACTIVE, TARGET1, TARGET2}`:
1. Fetch 120 days of `ONE_DAY` candles. Skip if `len(df) < 20`.
2. `current_price = latest_close`.
3. `holding_days` = days since `activated_at`, else `holding_days + 1`.
4. `highest_profit = max(highest_profit, (latest_high − entry)/entry × 100)`.
5. `max_drawdown  = min(max_drawdown,  (latest_low  − entry)/entry × 100)`.
6. `pnl_pct = (latest_close − entry)/entry × 100`.
7. Ordered exit checks (first match wins, `continue`s out of the loop):
   1. **Hard SL** — `latest_low <= stop_loss` → `HIT_SL`
   2. **Trailing** — `latest_close < EMA20(daily)` → `TRAILING_EXIT`
   3. **Target 3** — `latest_high >= target3` → `HIT_TARGET`
   4. **Time-stop** — `elapsed_days >= expected_holding_days` (requires `activated_at`) → `TIME_STOP`
   5. **Target 2** — `latest_high >= target2` and status ∈ {ACTIVE, TARGET1} → status `TARGET2`, **SL trailed to entry**
   6. **Target 1** — `elif latest_high >= target1` and status == ACTIVE → status `TARGET1`, **SL locked to entry**
8. `sig.save()`.
9. After the loop: `_send_telegram_eod_status()` — portfolio average P&L across ACTIVE rows.

Ordering consequence: **check 2 (trailing 20-EMA) is evaluated before any target check.** A
stock that gapped to target-3 but closed below its 20 EMA exits as `TRAILING_EXIT` at
`latest_close`, not as `HIT_TARGET` at `target3`.

**15:35** — `send_short_term_status_update()` again (same function as 10:05).

**16:30** — `run_daily_pipeline()` → `manage.py run_daily_market_update --skip-ai=False`:
global market data → market bias → Claude AI insight → NIFTY/BANKNIFTY option-chain snapshots.
**Independent of the swing system.**

---

# SECTION 3 — Stock Universe

## 3.1 Where stocks come from

`_run_daily_scanner_impl` Step 3 (`trade_engine.py:397-405`):

```python
session = _get_session()                       # requests.Session with NSE cookies
symbols = list(_fetch_nifty500_symbols(session))
if not symbols:
    symbols = NIFTY500_FALLBACK
```

`bhavcopy_service._fetch_nifty500_symbols()` resolution order:
1. **`IndexConstituent` DB table** — `filter(index_name='NIFTY500', is_active=True)`. If
   non-empty, returned immediately. **This is the primary source.**
2. **NSE archive CSV** — `https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv`,
   parsed for the `Symbol` column. 15 s timeout. No caching in this function.
3. Empty `set()` on exception.

`_get_session()` sets a Chrome UA + `Referer: https://www.nseindia.com/` and GETs
`https://www.nseindia.com` first to obtain cookies (NSE 403s otherwise; the codebase notes
cloud IPs are frequently blocked).

3. **`NIFTY500_FALLBACK`** (`pro_system_service.py:32-77`) — a hardcoded list of ~180 symbols
   used when steps 1–2 both yield nothing. It is a static snapshot and contains symbols that
   are stale or wrong for NSE (`HDFC` — merged into HDFCBANK; `AAPL`; `RCOM`; `MTNL`;
   `INOXLEISURE`; `TVSMOTORS` vs the actual `TVSMOTOR`). Unresolvable symbols are silently
   dropped by `get_token_map()`.

## 3.2 NIFTY100 / NIFTY200 / NIFTY500 / Custom / Live NSE

| Tier | Used by short-term? |
|---|---|
| NIFTY50 | No (used by the strangle scanner and the WebSocket bootstrap only) |
| NIFTY100 | No — that is the **intraday** engine's universe (`signal_utils.INTRADAY_UNIVERSE = "NIFTY100"`) |
| NIFTY200 | Not implemented anywhere. `signal_utils._NIFTY_INDEX_CSV` maps only NIFTY50/100/500 |
| **NIFTY500** | **Yes — the short-term universe** |
| Custom list | Only `NIFTY500_FALLBACK` (emergency fallback, not user-editable at runtime) |
| Database | `IndexConstituent` table (primary) |
| Live NSE | Archive CSV (secondary) |

Refreshing `IndexConstituent`: `manage.py refresh_nifty500`
(`backend/stocks/management/commands/refresh_nifty500.py`). Not on any schedule.

## 3.3 Sector Lists

**None.** No sector taxonomy, no sector index, no sector rotation exists in the short-term
system. The NSE CSV contains an `Industry` column but `_fetch_nifty500_symbols()` reads only
`Symbol`. `TradeScanner.sector_score` exists as a DB column on a dead model, and
`_compute_ai_score` computes a variable *named* `sector_score` that is actually pure relative
strength vs Nifty (Section 6.9).

## 3.4 Liquidity Filters

Two, both on today's Angel One quote / candle:

| Filter | Strict | Relaxed | Where |
|---|---|---|---|
| Pre-filter volume | `trade_volume > 50_000` | `> 10_000` | `_run_daily_scanner_impl:430-431` |
| Liquidity floor | `df['Volume'].iloc[-1] >= 100_000` | same (not relaxed) | `_compute_ai_score` Filter 6 |

`minimum_liquidity_floor = 100000` in `strategy_config.json`. Note `nifty_500_prefilter_volume: 50000`
in the config file is **not read** — `_run_daily_scanner_impl` hardcodes `min_vol = 10000 if relaxed else 50000`.

## 3.5 Average Volume Filters

`_compute_ai_score` Filter 5: `vol_5d >= vol_20d × multiplier`, where
`vol_5d = df['Volume'].iloc[-5:].mean()`, `vol_20d = df['Volume'].iloc[-20:].mean()`.
Multiplier 1.5 strict / 1.0 relaxed. Rejects if `vol_20d == 0`.

## 3.6 Price Filters

- Pre-filter: `change_percent > 0.5` strict, `> -2.0` relaxed. (`nifty_500_prefilter_pct: 0.5`
  in the config is likewise **not read**; the value is hardcoded.)
- Trend filter: `close > ema50 > ema200`.
- Proximity: within 5 % (strict) / 10 % (relaxed) of 52-week high **OR** ≥ previous 20-day high.

There is **no absolute price floor/ceiling** (no "skip stocks under ₹50", no penny-stock filter
beyond volume).

## 3.7 Market Cap Filters

**None.** Market cap is never fetched. `_fetch_long_term_quality` (long-term engine) computes a
`mcap` proxy as `close × vol_20d`; the short-term engine has no equivalent and never uses it.

## 3.8 Blacklist

No explicit blacklist. Three effective exclusions:
1. **Duplicate lifecycle** — symbol already in {PENDING, ACTIVE, TARGET1, TARGET2, REVIEW_REQUIRED}.
2. **Cooldown lock** — an `ARCHIVED` row with `cooldown_until > now()` (28 calendar days).
3. **Unresolvable token** — dropped silently by `get_token_map()`.

## 3.9 Whitelist

**None.** `NIFTY500_FALLBACK` is a fallback, not a whitelist — it is only consulted when the
primary sources return nothing.

## 3.10 Candidate cap

After pre-filtering, candidates are sorted by `change_pct` descending and truncated to
**`candidates[:50]`** — hardcoded at `trade_engine.py:439`. (`top_candidates_count: 50` in the
config file is, again, not read.)

---

# SECTION 4 — Market Filters

Everything the short-term system checks about the market, exhaustively.

## 4.1 NIFTY Trend — the only market regime filter

`pro_system_service.get_market_direction()`:

```
df   = 120 calendar days of ONE_DAY candles, token 99926000
guard: len(df) >= 51
ema20 = Close.ewm(span=20, adjust=False).mean().iloc[-1]
ema50 = Close.ewm(span=50, adjust=False).mean().iloc[-1]
trend = "BULLISH" if (close > ema20 and close > ema50) else "BEARISH"
```

Failure / insufficient data → `{"trend": "NEUTRAL", "close": 0, "ema20": 0, "ema50": 0}`.

**Gate** (`_run_daily_scanner_impl:384-389`):
```python
if direction.get('trend') == 'BEARISH' and not relaxed:
    if send_telegram: _send_telegram_scanner_summary([], direction)
    return []
```
- `BEARISH` + strict → abort, return `[]`. That empty return triggers the relaxed pass, which
  does **not** re-apply this gate, so the scan proceeds anyway with relaxed thresholds.
- `NEUTRAL` is **not** blocked — it passes the `== 'BEARISH'` test and scanning continues.

## 4.2 Relative Strength vs NIFTY (stock-level)

`nifty_20d_ret = (nifty.Close[-1] − nifty.Close[-20]) / nifty.Close[-20]`, computed from 365
days of Nifty daily candles (`_run_daily_scanner_impl:443-449`). Requires `len(nifty_df) > 20`,
else `0.0`.

Used twice: as a hard filter (Filter 3, strict only) and as a score component (Section 6.9).

## 4.3 Sector Trend

**Not implemented.**

## 4.4 Market Breadth

**Not implemented.** No advance/decline, no % above 200-DMA, no new-highs count.

## 4.5 VIX

**Not implemented.** `INDIAVIX` appears nowhere in the codebase.

## 4.6 Market Regime

The only regime classifier is BULLISH / BEARISH / NEUTRAL above. There is a separate
`market_intelligence_service.get_standard_market_state()` returning
`SIDEWAYS`/`TRENDING`/`UNKNOWN` from VWAP distance and intraday range on 5-minute Nifty
candles — that is consumed by the **intraday** engine only, never by `trade_engine.py`.

## 4.7 Gap Up / Gap Down

**Not implemented.** No previous-close vs today-open comparison anywhere in the short-term path.

## 4.8 Bull Market / Bear Market / Sideways

Bull / Bear = the 4.1 classifier. Sideways is not a short-term state.

## 4.9 Volatility

Volatility enters only via ATR:
- ATR(14) sets the stop distance (Section 11).
- `atr_pct = atr/close × 100` sets `expected_holding_days` (Section 12.7).

There is **no volatility guard** that rejects a candidate for being too volatile or too quiet.
(`compute_adx` and `_volatility_guard` exist in other engines, not here.)

## 4.10 Holiday Detection

Three layers, in `signal_utils.py`:

1. **Weekend guard** — `now_ist.weekday() >= 5` → CLOSED. Always applied first.
2. **`sync_nse_holidays_from_api()`** — GET `https://www.nseindia.com/api/holiday-master?type=trading`,
   parses `CM` and `FO` segments, upserts `MarketHoliday` rows, deactivates rows no longer
   returned for those years. Success cached 24 h (`nse_holidays_sync_success`); failure backs
   off 6 h (`nse_holidays_sync_failed_cooldown`) because NSE blocks Render's IP.
3. **`MarketHoliday` DB lookup**, then **static `NSE_HOLIDAYS`** set (2025–2027 dates hardcoded
   at `signal_utils.py:72-89`).

`is_market_open_today()` caches its result for 60 s in `_MARKET_OPEN_CACHE`.

## 4.11 Market Open Detection

`get_market_status("NSE")`:
1. Weekend → CLOSED.
2. `_fetch_nse_market_status()` — cookie-bootstrapped GET of `https://www.nseindia.com/api/marketStatus`,
   cached 60 s in `_NSE_STATUS_CACHE`. Matches segment `market ∈ {"Capital Market","Equity","NSE"}`.
   - API says Open → OPEN.
   - API says Closed **but** `09:15 ≤ now ≤ 15:30` and not a holiday → **override to OPEN**
     ("Resilient Override", `signal_utils.py:323-326`).
   - Otherwise CLOSED.
3. API unreachable → holiday check + time-window check.

`is_static_closed(segment)` = `get_market_status(segment, static_only=True) == "CLOSED"` —
calendar + time window only, no network.

**The short-term scheduled jobs do NOT call any of this.** `run_daily_scanner`,
`check_pending_activations`, `run_eod_evaluation`, and `run_expiry_cleanup` contain **no market-open
check at all**. They rely entirely on APScheduler's `day_of_week="mon-fri"` trigger. On an NSE
holiday that falls on a weekday, all four run and hit Angel One. The only holiday gate on this
path is in `CronScannerTriggerView` (manual triggers) and in `apps.py::ready()` (skips broker init).

---

# SECTION 5 — Stock Selection Pipeline

Function: `trade_engine._run_daily_scanner_impl(relaxed, send_telegram)`.
Every rejection is listed with its exact condition.

## Step 0 — Overlap lock
`cache.add("trade_engine_scanner_running", True, timeout=600)`.
❌ **REJECT ALL**: key already present → log warning, `return []`.

## Step 1 — Market Direction
`direction = get_market_direction()`.
❌ **REJECT ALL**: `trend == 'BEARISH'` and `relaxed is False` → send empty Telegram summary
(only if `send_telegram`), `return []`.

## Step 2 — Broker session
`svc = get_angel_one_instance()`.
❌ **REJECT ALL**: `svc` is None → log error, `return []`.

## Step 3 — Universe
`symbols = _fetch_nifty500_symbols(session)` → `NIFTY500_FALLBACK` on empty.

## Step 4a — Token resolution
`token_map = svc.get_token_map(symbols, exchange="NSE")`. Looks up `symbol` then `f"{symbol}-EQ"`
in the indexed equity master.
❌ **REJECT (symbol)**: no match in `_INSTRUMENT_MASTER_CACHE["equity"]` → dropped silently.
❌ **REJECT ALL**: `token_map` empty → `return []`.

## Step 4b — Bulk quote sweep
Tokens chunked by 50; `svc.get_bulk_quotes({"NSE": chunk}, mode="FULL")` per chunk.
Chunk-level exceptions are caught and logged; the sweep continues.
Circuit breaker: if `_REST_CIRCUIT_BREAKER_UNTIL["quote"] > now`, `get_bulk_quotes` returns
only whatever is already fresh in the WebSocket cache.

## Step 4c — Pre-filter
For each `(sym, tok)`:
❌ **REJECT**: no quote at key `f"NSE:{tok}"`.
❌ **REJECT**: `change_percent <= min_change` (strict `0.5`, relaxed `-2.0`).
❌ **REJECT**: `trade_volume <= min_vol` (strict `50000`, relaxed `10000`).

Survivors sorted by `change_pct` desc → **`top_candidates = candidates[:50]`**.
❌ **REJECT**: rank > 50.

## Step 5 — Nifty baseline
365-day Nifty daily candles → `nifty_20d_ret` (0.0 if `len <= 20`).

## Step 6 — Per-candidate AI scoring
`time.sleep(0.35)` before each call (Angel One historical API is ~3 req/s; `get_candle_data`
additionally enforces a global ≥1.05 s gap under `_candle_api_lock`, so the real pace is ~1/s).

`df = svc.get_candle_data(token, "NSE", "ONE_DAY", from_date, to_date)` — 365 calendar days.
❌ **REJECT**: `df.empty or len(df) < 100`.
❌ **REJECT**: exception during fetch/score → logged, skipped.

Then `_compute_ai_score(df, nifty_20d_ret, relaxed)`:

| # | Filter | Reject condition |
|---|---|---|
| 0 | Length | `len(df) < max(201, ema_trend_long + 1)` = `< 201` |
| 1 | Trend stack | `not (close > ema50 > ema200)` |
| 2 | ADX | `ADX14 < 25.0` (strict) / `< 15.0` (relaxed) |
| 3 | Relative strength | `stock_20d_ret < nifty_20d_ret` — **strict only**, bypassed when relaxed |
| 4 | Breakout | `not (close >= high_52w × 0.95  OR  close >= high_20d)`; relaxed uses `× 0.90` |
| 5 | Volume expansion | `vol_20d == 0  or  vol_5d < vol_20d × 1.5` (relaxed `× 1.0`) |
| 6 | Liquidity floor | `df['Volume'].iloc[-1] < 100_000` |
| 7 | Stop sanity | `sl_points <= 0` after ATR stop + 10 % floor |
| 8 | Risk/Reward | `rr_ratio < 2.0` — always exactly 2.0 by construction, so this never rejects |
| 9 | Minimum score | `ai_score < 25.0` |

Survivors get `result['symbol']` and `result['current_ltp']` attached.

## Step 6b — Ranking
`scored_results.sort(key=lambda x: x['ai_score'], reverse=True)`.
`top_picks = scored_results` — **no truncation.** Every stock that passes all filters becomes
a signal. The cap of 10 applies only to the Telegram message.

## Step 7 — Persistence
Per pick:
❌ **REJECT**: an existing `ShortTermSignal` for that symbol with
`status ∈ {PENDING, ACTIVE, TARGET1, TARGET2, REVIEW_REQUIRED}` → *"already tracked in active lifecycle"*.
❌ **REJECT**: an `ARCHIVED` row with `cooldown_until > timezone.now()` → *"cooldown lock active"*.
❌ **REJECT**: DB exception → logged, skipped.

Otherwise, inside `transaction.atomic()`:
- `ShortTermSignal.objects.create(symbol, entry_price, stop_loss, target=target1, target2,
  target3, current_price=current_ltp, vol_ratio, setup, status=PENDING,
  expected_holding_days=holding_days, ai_score)`
- `TradeHistory.objects.create(old_status='NONE', new_status='PENDING',
  price=entry_price, reason='Scanner Discovery', triggered_by='SYSTEM')`

## Step 8 — Telegram
`top_alerts = new_trades[:10]` (`max_telegram_alerts`, this one **is** read from config).
`_send_telegram_scanner_summary(top_alerts, direction)` — **always called when
`send_telegram=True`, even with zero picks**, because the message also carries the ACTIVE
holdings block and the long-term block.

⚠️ **Cross-system coupling:** `_send_telegram_scanner_summary()` calls
`_scan_new_long_term_setups()` (`trade_engine.py:1234`), which runs the entire long-term Nifty 500
scan and **persists long-term `SignalHistory` rows**. The long-term engine therefore executes as a
side effect of formatting the short-term Telegram report. See `doc/long_term_stock.md` §17.

## Step 9 — Release lock
`finally: cache.delete("trade_engine_scanner_running")`.

---

# SECTION 6 — Technical Analysis

Every indicator used by the short-term engine. All are computed in `trade_engine.py` on
**daily (`ONE_DAY`) candles** unless stated.

## 6.1 EMA — `_ema(series, span)`

```python
series.ewm(span=span, adjust=False).mean()
```
- **Periods:** 20 (`ema_trailing`), 50 (`ema_trend_short`), 200 (`ema_trend_long`).
- **Purpose:** trend structure (50/200) and trailing exit (20).
- **BUY influence:** `close > ema50 > ema200` is a hard gate. `(close − ema200)/ema200 × 100 × 1.5`,
  capped at 25, is the Trend Score.
- **SELL influence:** none (long-only). `close < ema20` at EOD is the trailing-exit trigger.
- **Weight:** 25/100 in the score; **infinite as a filter** (rejection is absolute).

## 6.2 ATR(14) — `_atr(df, period=14)`

```python
tr  = max(High−Low, |High−PrevClose|, |Low−PrevClose|)
atr = tr.rolling(window=14).mean()       # simple rolling mean, NOT Wilder's
```
- **Purpose:** stop distance and expected holding period.
- **BUY influence:** `stop_loss = entry − 2.0 × ATR`; `holding_days = clamp(60/atr_pct, 15, 90)`.
- **Weight in score:** **zero** — ATR is not a scoring component.

## 6.3 ADX(14) — `_adx(df, period=14)`

```python
tr, +DM, −DM  computed conventionally
smoothing: .ewm(alpha=1/14, adjust=False).mean()      # Wilder-equivalent
+DI = 100 × +DM_s / TR_s ;  −DI = 100 × −DM_s / TR_s
DX  = 100 × |+DI − −DI| / (+DI + −DI)
ADX = DX.ewm(alpha=1/14, adjust=False).mean().fillna(0.0)
```
- **Threshold:** `min_adx_threshold = 25.0`; `adx_relaxed_threshold = 15.0`.
- **BUY influence:** hard gate, plus `min(12.5, (adx − adx_limit) × 0.5)` in Momentum Score.
- **Weight:** 12.5/100.
- **Note:** `trade_engine._adx` uses EWM (Wilder) smoothing; `pro_system_service._compute_adx`
  is identical; `signal_utils.compute_adx` uses **simple rolling** smoothing and belongs to the
  option-buying engine. Three implementations exist; the swing engine uses `trade_engine._adx`.

## 6.4 RSI(14) — `_rsi(series, period=14)`

```python
avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
RSI = (100 − 100/(1 + avg_gain/avg_loss)).fillna(50.0)
```
- **Threshold:** none — RSI is **never a filter**.
- **BUY influence:** `min(12.5, max(0, (rsi − 40) × 0.25))` in Momentum Score. Saturates at
  RSI 90. RSI ≤ 40 contributes 0. **Overbought is never penalised.**
- **Weight:** 12.5/100.

## 6.5 52-Week High

```python
high_52w = df['High'].rolling(252, min_periods=100).max().iloc[-1]
```
- **Purpose:** breakout proximity gate + Risk Score.
- **Threshold:** `close >= high_52w × 0.95` (strict) / `× 0.90` (relaxed).
- **Weight:** 15/100 via Risk Score.

## 6.6 20-Day High

```python
high_20d = df['High'].rolling(20).max().iloc[-2]   # -2 = excludes today's bar
```
- **Threshold:** `close >= high_20d`. Satisfying this alone passes Filter 4.
- **Sets `setup` label:** `'20d Breakout'` if `is_20d_break` else `'52w High Breakout'`.
  Note the label check is `'20d Breakout' if is_20d_break else …`, so when both are true the
  row is labelled `20d Breakout`.

## 6.7 Volume Ratio

```python
vol_5d  = df['Volume'].iloc[-5:].mean()
vol_20d = df['Volume'].iloc[-20:].mean()
vol_ratio = vol_5d / vol_20d
```
- **Threshold:** `≥ 1.5` strict / `≥ 1.0` relaxed.
- **BUY influence:** `min(20, max(0, (vol_ratio − 1.0) × 10))` — saturates at ratio 3.0.
- **Weight:** 20/100 — the single largest score component.

## 6.8 Absolute Volume (liquidity floor)

`df['Volume'].iloc[-1] >= 100_000`. Pure gate, no score weight.

## 6.9 Relative Strength (20-day, vs Nifty 50)

```python
stock_20d_ret = (Close[-1] − Close[-20]) / Close[-20]
rs_spread     = (stock_20d_ret − nifty_20d_ret) × 100
```
- **Gate:** `stock_20d_ret >= nifty_20d_ret` — strict only.
- **Score:** `min(15, max(0, rs_spread × 3.0))` — saturates at 5 pp outperformance.
- **Weight:** 15/100. Called `sector_score` in the code; it has nothing to do with sectors.

## 6.10 Indicators that are computed but never used for a decision

`ema20` is returned in the score dict and used only at EOD for trailing. `rsi` and `adx` are
returned for display. `fundamental_score = 10.0` is a hardcoded constant returned in the dict
and **excluded from the `ai_score` sum** (Section 8.2).

## 6.11 Indicators NOT present in the short-term engine

MACD, Bollinger Bands, Stochastic, OBV, CCI, Supertrend, Ichimoku, Fibonacci, pivot points,
VWAP, Volume Profile (POC/VAH/VAL), candlestick pattern recognition, moving-average crossovers
as events, and any multi-timeframe confirmation. (VWAP and Volume Profile exist in
`signal_utils.py` but are used only by the intraday / option engines.)

## 6.12 Summary weight table

| Indicator | Gate? | Max score | % of 100 |
|---|---|---|---|
| EMA 50/200 stack | Hard | — | — |
| EMA 200 distance | — | 25.0 | 25 % |
| ADX(14) | Hard (25/15) | 12.5 | 12.5 % |
| RSI(14) | No | 12.5 | 12.5 % |
| Volume ratio 5d/20d | Hard (1.5/1.0) | 20.0 | 20 % |
| RS vs Nifty 20d | Hard (strict only) | 15.0 | 15 % |
| 52-week high proximity | Hard (5 %/10 %, OR 20d) | 15.0 | 15 % |
| Absolute volume | Hard (100 k) | 0 | 0 % |
| ATR(14) | Indirect (SL sanity) | 0 | 0 % |
| Fundamentals | No | 10.0 (**unused**) | 0 % |
| **Total scored** | | **100.0** | **100 %** |

---

# SECTION 7 — Fundamental Analysis

## **There is NO fundamental analysis in the short-term system.**

Stated explicitly, as requested.

| Requested factor | Status in code |
|---|---|
| Revenue | Not fetched, not stored, not used |
| Profit | Not fetched |
| EPS | Not fetched |
| ROE | Not fetched |
| ROCE | Not fetched |
| Debt / Debt-to-Equity | Not fetched |
| Cash Flow | Not fetched |
| Promoter Holding | Not fetched |
| FII (per stock) | Not fetched |
| DII (per stock) | Not fetched |
| Valuation (P/E, P/B, EV/EBITDA) | Not fetched |
| Growth | Not fetched |
| Quality | Not fetched |
| Momentum | **Present — but this is technical momentum** (ADX + RSI), not fundamental |

Evidence:

1. `_compute_ai_score` sets `fundamental_score = 10.0` with the comment
   `# placeholder (would need external fundamental data)` (`trade_engine.py:277-278`).
2. That value is returned in the result dict but is **not added** to `ai_score`:
   `ai_score = trend_score + momentum_score + volume_score + sector_score + risk_score`
   (`trade_engine.py:280`). It is a dead field.
3. `TradeScanner.fundamental_score` is a DB column on a model nothing writes.
4. The only "fundamental-looking" numbers in the whole codebase are hardcoded literals in
   `pro_system_service._fetch_long_term_quality` (`roe: 22.5`, `debt_to_equity: 0.2`,
   `profit_growth: 15.0`) — those belong to the long-term engine, are explicitly labelled
   *"Quality proxy defaults"*, and are not real data. See `doc/long_term_stock.md` §7.

**There is also no FII/DII input to signal selection.** `fii_dii_service.py` and
`GET /api/stocks/fii-dii/` exist and feed a dashboard card only.

---

# SECTION 8 — Scoring Engine

## 8.1 The five components

Computed in `_compute_ai_score` (`trade_engine.py:255-280`), all on the latest daily bar.

### Trend Score — 0 to 25
```python
ema_spread  = ((close − ema200) / ema200) × 100
trend_score = min(25.0, max(0.0, ema_spread × 1.5))
```
Saturates when price is 16.67 % above the 200 EMA.

### Momentum Score — 0 to 25
```python
adx_contribution = min(12.5, (adx_val − adx_limit) × 0.5) if adx_val > adx_limit else 0.0
rsi_contribution = min(12.5, max(0.0, (rsi_val − 40) × 0.25))
momentum_score   = adx_contribution + rsi_contribution
```
`adx_limit` is 25.0 strict / 15.0 relaxed. ADX saturates at `adx_limit + 25`.
RSI saturates at 90.

> Note: `adx_limit` differs between modes, so the same ADX value scores **5 points higher in
> relaxed mode** (e.g. ADX 40 → strict `(40−25)×0.5 = 7.5`, relaxed `(40−15)×0.5 = 12.5`).
> Relaxed-mode scores are therefore not comparable to strict-mode scores.

### Volume Score — 0 to 20
```python
vol_ratio    = vol_5d / vol_20d
volume_score = min(20.0, max(0.0, (vol_ratio − 1.0) × 10.0))
```
Saturates at ratio 3.0.

### Sector Score (actually Relative Strength) — 0 to 15
```python
rs_spread    = (stock_20d_ret − nifty_20d_ret) × 100
sector_score = min(15.0, max(0.0, rs_spread × 3.0))
```
Saturates at 5 percentage points of 20-day outperformance.

### Risk Score — 0 to 15
```python
pct_from_52w = ((high_52w − close) / high_52w) × 100
risk_score   = min(15.0, max(0.0, 15.0 − pct_from_52w × 3.0))
```
15 at the 52-week high, 0 at ≥5 % below it. Despite the name it measures **only** proximity
to the 52-week high; the docstring's "+ tight SL" is not implemented.

### Composite
```python
ai_score = trend_score + momentum_score + volume_score + sector_score + risk_score
```
Range 0–100. `fundamental_score` is excluded.

**Cutoff:** `if ai_score < 25.0: return None` (`min_ai_score`).

## 8.2 How stocks are ranked

Two ranking passes:

1. **Pre-filter rank** — `candidates.sort(key=lambda x: x["change_pct"], reverse=True)`, then
   `[:50]`. A stock with a great setup but a small daily move can be cut here before its
   indicators are ever computed. This is the biggest selection bias in the pipeline.
2. **Final rank** — `scored_results.sort(key=lambda x: x['ai_score'], reverse=True)`.

## 8.3 How ties are broken

**They are not.** `list.sort()` in Python is stable, so equal `ai_score` values retain their
pre-filter order, i.e. higher `change_pct` first. This is incidental, not designed. There is
no explicit tiebreaker.

## 8.4 How confidence is calculated

There is no confidence metric for short-term signals. `ai_score` is surfaced directly in the
UI as `AI: {score}`.

(`SignalHistorySerializer.get_confidence_score` computes `clamp(50 + rr×20, 50, 99)` — that
serializer only handles `SignalHistory`, i.e. intraday/long-term/options, never `ShortTermSignal`.)

## 8.5 Final ranking → action

`top_picks = scored_results` — **the entire scored list is persisted**, in descending AI-score
order. There is no "take top N". Only the Telegram message is capped at
`max_telegram_alerts = 10`. Practically, the number of signals per day is bounded by how many
of the 50 pre-filtered candidates clear all nine gates.

---

# SECTION 9 — Signal Generation

The short-term engine emits **one signal type: BUY**.

## BUY

Emitted when, on the 10:00 AM scan, **all** of the following hold:

1. `get_market_direction()['trend'] != 'BEARISH'` (or the run is the relaxed second pass)
2. Symbol resolves to an Angel One NSE token
3. `change_percent > 0.5` (relaxed `> −2.0`)
4. `trade_volume > 50_000` (relaxed `> 10_000`)
5. Ranked in the top 50 by `change_pct`
6. `len(daily_df) >= 201`
7. `close > EMA50 > EMA200`
8. `ADX14 >= 25.0` (relaxed `>= 15.0`)
9. `stock_20d_ret >= nifty_20d_ret` (strict only)
10. `close >= high_52w × 0.95` **OR** `close >= high_20d` (relaxed `× 0.90`)
11. `vol_5d >= vol_20d × 1.5` (relaxed `× 1.0`)
12. `today_volume >= 100_000`
13. `entry − stop_loss > 0`
14. `rr_ratio >= 2.0`
15. `ai_score >= 25.0`
16. No existing PENDING/ACTIVE/TARGET1/TARGET2/REVIEW_REQUIRED row for the symbol
17. No ARCHIVED row with `cooldown_until > now()`

Persisted as `ShortTermSignal(status=PENDING)`.

## SELL

**Not implemented.** The engine is long-only. There is no short-side scan, no bearish setup,
no put-equivalent. `get_dashboard_data` hardcodes `'signal': 'BUY'`.

## WATCHLIST

**No separate WATCHLIST state.** The closest analogue is `status=PENDING`: a discovered setup
whose entry trigger has not yet been touched. The `ProSystem.jsx` "Pending ⏳" tab is the
watchlist in practice.

## HOLD

**No HOLD signal is emitted.** Holding is implicit: a row sits in `ACTIVE`, `TARGET1`, or
`TARGET2` until an exit condition fires. (`IntradaySignal.signal_type` has a `HOLD` choice —
that is a dead model.)

## REJECT

Rejections are **not persisted**. A stock that fails any gate is silently skipped; only a log
line is emitted for exceptions and for the duplicate/cooldown skips. There is no rejected-
candidates table and no rejection-reason audit trail. Rebuilding *why* a stock was not picked
requires re-running the scan.

---

# SECTION 10 — Entry Logic

## 10.1 Exact entry price

```python
entry_price = close      # last daily close in the fetched candle series
```
(`_compute_ai_score:234`, rounded to 2 dp at line 298.)

Since the scan runs at 10:00 AM, the "last daily close" returned by Angel One's `ONE_DAY`
endpoint for an in-progress session is the **current running value of today's daily bar** at
scan time — i.e. approximately the 10:00 AM price. Nothing in the code pins this to the
*previous* session's close.

## 10.2 Order type

**No orders are placed.** There is no broker order API call anywhere in the codebase —
no `placeOrder`, no `/rest/secure/angelbroking/order/`. The system is advisory: it produces
signals, tracks them against market data, and sends Telegram messages. Execution is manual.

Consequently "Limit Order" and "Market Order" are **not applicable**. The Telegram copy says
`⚡ Action: Buy at current market price` (`trade_engine.py:1286`) and
`✅ Execute at current market price before market close` — instructions to a human.

## 10.3 Activation / confirmation

A `PENDING` row becomes `ACTIVE` in `check_pending_activations()` when:

```python
if ltp <= float(trade.entry_price):
```

Because `entry_price` was set to the ~10:00 AM price, this is a **pullback trigger**: the
signal activates when price comes back down to or below the scan-time price. It does **not**
activate on a breakout above it.

Race protection: `transaction.atomic()` + `select_for_update()` + re-check `status == PENDING`.

⚠️ **Divergence:** the legacy `pro_system_service.update_pro_system_outcomes()` implements a
stricter activation — `low <= entry_price` **AND** a bullish daily candle
(`close > open` and `close >= (high+low)/2`). That function has **no callers** (verified by
grep) and is dead code. The live rule is the simple `ltp <= entry` above.

## 10.4 Volume confirmation

Volume is confirmed **at scan time only** (`vol_5d ≥ 1.5 × vol_20d`, `today_volume ≥ 100k`).
There is **no volume check at activation.**

## 10.5 Breakout

The breakout is a *selection* criterion (`close >= high_20d` or within 5 % of the 52-week
high), not an *entry* criterion. The system never waits for a breakout to occur before entering.

## 10.6 Retest

The `ltp <= entry_price` activation rule is effectively a retest/pullback entry, and
`pro_system_service`'s formatter labels it that way: `f"Near ₹{entry} (Pullback)"`
(`pro_system_service.py:587`). This is the only retest concept implemented.

## 10.7 Entry timing window

Activation checks run only at the 12 cron firings listed in Section 2. A pullback that
touches entry and reverses **between** two checks is missed — the check uses live `ltp`, not
the interval's low. (`run_intraday_check`, the manual-only function, does read `low`, but only
to test the stop-loss on already-ACTIVE rows.)

## 10.8 Entry expiry

A `PENDING` row that never activates is expired by `run_expiry_cleanup` after
`pending_expiry_trading_days = 30` → `int(30 × 7/5) = 42` calendar days. See Section 15.

---

# SECTION 11 — Stop Loss Logic

## 11.1 ATR stop (initial)

`_compute_ai_score:238-241`:
```python
stop_loss    = entry_price − (atr_stop_loss_multiplier × ATR14)   # multiplier = 2.0
max_sl_floor = entry_price × ((100 − max_stop_loss_floor_pct)/100) # = entry × 0.90
if stop_loss < max_sl_floor:
    stop_loss = max_sl_floor
```

So the stop is `entry − 2×ATR`, **but never worse than −10 %**. If 2×ATR exceeds 10 % of
price, the stop is tightened to exactly −10 % (which also tightens the implied risk and thus
pulls the targets in, since targets are R-multiples of `sl_points`).

`sl_points = entry_price − stop_loss`; reject if `<= 0`.

## 11.2 Swing Low

**Not implemented.** No swing-low, pivot-low, or structural-low stop exists.

## 11.3 Swing High

Not applicable — long-only.

## 11.4 Percentage stop

Only as the −10 % floor above. There is no fixed-percentage stop mode.

## 11.5 Support

**Not implemented.** No support/resistance detection in the short-term engine.

## 11.6 Resistance

**Not implemented.**

## 11.7 Trailing Stop — two mechanisms

**(a) Level-based trail to break-even** (`run_eod_evaluation`):
- On `TARGET1` (`latest_high >= target1`): `sig.stop_loss = sig.entry_price` — *"locked to
  entry to secure break-even"*.
- On `TARGET2` (`latest_high >= target2`): `sig.stop_loss = sig.entry_price` again.

The stop is **never trailed above entry.** After T2 the position still risks giving back the
entire gain down to break-even.

**(b) Daily 20-EMA closing trail** (`run_eod_evaluation` Check 2):
```python
ema20_val = _ema(df['Close'], 20).iloc[-1]
if latest_close < ema20_val:
    _exit_signal(sig, latest_close, TRAILING_EXIT, "Trailing Exit", …)
```
Evaluated **before** every target check, so it dominates. Exit price = `latest_close` (market),
not the EMA level.

## 11.8 Time Stop

`run_eod_evaluation` Check 3.5:
```python
if sig.expected_holding_days and sig.activated_at:
    elapsed_days = (today − sig.activated_at.date()).days
    if elapsed_days >= sig.expected_holding_days:
        _exit_signal(sig, latest_close, TIME_STOP, "Time-Stop Exit", …)
```
`expected_holding_days` is set at scan time (Section 12.7), range 15–90.
Requires `activated_at` — a signal that somehow reached ACTIVE without it is never time-stopped.

## 11.9 Emergency Exit

There is **no** emergency/kill-switch exit: no circuit-limit detection, no news halt, no
gap-down override, no manual close endpoint. The only paths out of a position are the six
EOD checks and `run_expiry_cleanup`'s `REVIEW_REQUIRED` flag (which does **not** close the
position — it only relabels it).

## 11.10 Stop-loss evaluation frequency

| Path | Frequency | Data used |
|---|---|---|
| `run_eod_evaluation` | Once daily, 3:25 PM | `latest_low <= stop_loss` from the daily candle |
| `run_intraday_check` (manual only) | On demand via cron-trigger | `low <= stop_loss` from the FULL quote |
| `check_pending_activations` | 12×/day | **Does not check SL at all** |

**Practical consequence:** an ACTIVE swing position's stop is checked once per day. Intraday
stop violations that recover before the close are not acted on; intraday violations that
persist are exited at the *stop-loss price* (not the actual worse close), because
`_exit_signal(sig, sl_val, …)` books the exit at `sl_val`. Reported P&L therefore assumes a
fill at the stop, with no slippage or gap modelling.

---

# SECTION 12 — Target Logic

## 12.1 Risk / Reward

`_compute_ai_score:247-253`:
```python
target1 = entry_price + (2.0 × sl_points)   # target1_risk_reward
target2 = entry_price + (3.0 × sl_points)   # target2_risk_reward
target3 = entry_price + (4.0 × sl_points)   # target3_risk_reward
rr_ratio = (target1 − entry_price) / sl_points          # always exactly 2.0
if rr_ratio < min_risk_reward_ratio (2.0): return None  # unreachable
```

## 12.2 ATR target

Indirect: `sl_points = 2 × ATR` (unless floored at 10 %), so
`target1 = entry + 4×ATR`, `target2 = entry + 6×ATR`, `target3 = entry + 8×ATR`.
There is no separate ATR-multiple target rule.

## 12.3 Fixed %

Not used as a target rule. The only percentage constant is the −10 % stop floor, which
indirectly caps the targets at +20 % / +30 % / +40 % for high-ATR names.

## 12.4 Resistance-based targets

**Not implemented.**

## 12.5 Multiple targets

Three, stored as `target` (= T1), `target2`, `target3` on `ShortTermSignal`.

EOD defaults when null (`run_eod_evaluation:705-707`):
```python
t3_val = float(sig.target3) if sig.target3 else float(sig.target) × 2.0
t2_val = float(sig.target2) if sig.target2 else float(sig.target) × 1.5
t1_val = float(sig.target)
```
These fallbacks are multiples of the **target price**, not of `sl_points`, so they differ from
the scan-time values. They only apply to legacy rows created without T2/T3.

## 12.6 Scaling Out / Partial Exit

**Not implemented.** No quantity is tracked, so no partial exit is possible. Hitting T1 or T2
does **not** book any profit — it only:
- transitions the status (`ACTIVE → TARGET1 → TARGET2`),
- moves the stop-loss to entry,
- fires a Telegram alert.

The position remains fully open.

## 12.7 Expected holding period

```python
atr_pct      = (atr_val / close) × 100
holding_days = max(15, min(90, int(60 / max(atr_pct, 0.5))))
expiry_days  = holding_days + 10          # returned but never persisted or used
```
Stored as `ShortTermSignal.expected_holding_days`; drives the time-stop.
`expiry_days` is a dead field.

## 12.8 Final Exit

The position closes only via `_exit_signal()`, called for exactly four statuses:

| Status | Exit price booked | Trigger |
|---|---|---|
| `HIT_SL` | `stop_loss` | `latest_low <= stop_loss` |
| `TRAILING_EXIT` | `latest_close` | `latest_close < EMA20` |
| `HIT_TARGET` | `target3` | `latest_high >= target3` |
| `TIME_STOP` | `latest_close` | `elapsed_days >= expected_holding_days` |

**Only Target 3 closes a trade.** T1 and T2 are milestones.

`_exit_signal` always sets the row's final `status = ARCHIVED` (the exit reason is preserved
in `exit_reason` and in two `TradeHistory` rows). See Section 15.

---

# SECTION 13 — Position Sizing

## 13.1 Server-side

**None.** `ShortTermSignal` has no `quantity`, `capital`, `allocation`, or `position_value`
field. No backend code computes a share count for short-term signals.

## 13.2 Capital Allocation

Not modelled. The engine emits signals without reference to available capital, and creates as
many as pass the filters (Section 8.5).

## 13.3 Risk %

Two unconnected, hardcoded conventions:

| Where | Rule | Value |
|---|---|---|
| `ProSystem.jsx:106` (UI, live) | `maxLossRs = capital × 1.5%` | 1.5 % of a user-entered capital, default ₹5,00,000 → ₹7,500 |
| `pro_system_service.py:809` (Reports API) | `max_risk_inr = 5000` fixed | ₹5,000 |

Both then do `qty = floor(max_risk / |entry − stop_loss|)` (the reports version uses
`max(1, …)` and falls back to `qty = 100` when `sl_points == 0`).

For the same signal the two surfaces will display different quantities and therefore
different ₹ P&L.

## 13.4 Maximum Positions

**No cap.** Nothing limits how many `PENDING` or `ACTIVE` short-term signals can exist
simultaneously. The only structural limits are:
- one live row per symbol (duplicate-lifecycle check),
- at most 50 candidates scored per day,
- a 28-calendar-day cooldown per symbol after archiving.

(For contrast, the intraday engine has `MAX_SIGNALS_PER_SCAN = 5` and the option engines have
`max_signals` in `trading_engine/config.py`. The swing engine has no equivalent.)

## 13.5 Sector Exposure

**Not implemented.** No sector data exists (Section 3.3).

## 13.6 Portfolio Exposure

**Not implemented.** `get_dashboard_data()['analytics']['unrealized_pnl']` is
`sum(t['pnl_pct'] for t in active_legacy)` — a sum of percentages across positions, which is
not a portfolio return under any weighting scheme. `_send_telegram_eod_status()` reports
`avg_pnl = total_pnl / len(active)`, i.e. an **equal-weight** average, implicitly assuming
identical position sizes.

## 13.7 Maximum Loss

Per-trade max loss is bounded by the −10 % stop floor. Portfolio-level max loss, daily loss
limits, and drawdown circuit-breakers **do not exist**.

---

# SECTION 14 — Risk Management

Every protection actually implemented.

## 14.1 Duplicate Prevention

Four distinct layers:

1. **Scanner lifecycle check** — skip if a `ShortTermSignal` exists for the symbol with
   `status ∈ {PENDING, ACTIVE, TARGET1, TARGET2, REVIEW_REQUIRED}` (`trade_engine.py:479-490`).
2. **Cooldown check** — skip if an `ARCHIVED` row has `cooldown_until > now()`.
3. **Activation race guard** — `select_for_update()` + status re-check inside
   `transaction.atomic()` in `check_pending_activations` and in every `run_expiry_cleanup`
   transition.
4. **Telegram idempotency** — `TelegramLog.objects.filter(short_term_signal=…, event_type=…).exists()`
   before every queue write.

> Note: `ShortTermSignal` has **no** DB unique constraint. Its `Meta` declares only
> `db_table` and `ordering`. Duplicate prevention is entirely application-level.
> (`SignalHistory`, used by the long-term/intraday engines, *does* have
> `UniqueConstraint(['symbol','category','status'], condition=Q(status__in=['PENDING','ACTIVE']))`.)

## 14.2 Concurrency / Overlap Locks

| Lock key | Guards | Timeout |
|---|---|---|
| `trade_engine_scanner_running` | `run_daily_scanner` | 600 s |
| `run_periodic_scanners_running` | `run_periodic_scanners` (intraday/options cycle) | 600 s |

Both use `cache.add()` (atomic set-if-absent) and `cache.delete()` in `finally`.
Rationale recorded in the code: concurrent scans corrupt the shared `requests.Session` TLS
state (`SSL "decryption failed or bad record mac"`) and trip Angel One's rate limiter.

Because the cache backend is **file-based**, these locks are per-filesystem. On a
multi-instance deploy sharing no filesystem they would not hold.

## 14.3 Circuit Filters

**Not implemented.** No upper/lower-circuit detection, no `circuitLimit` field is read from
Angel One quotes.

## 14.4 Liquidity Filters

Section 3.4: pre-filter `trade_volume > 50k` (strict) and hard floor `today_volume >= 100k`.
No bid-ask spread check, no impact-cost check, no delivery-percentage check for short-term.

## 14.5 Cooldown

`_exit_signal` (`trade_engine.py:1021-1023`):
```python
cooldown_days     = archived_cooldown_trading_days   # 20
calendar_cooldown = int(20 × 7 / 5)                  # 28
sig.cooldown_until = timezone.now() + timedelta(days=28)
```
Enforced only at scan time (`status=ARCHIVED and cooldown_until > now()`).

## 14.6 Maximum Daily Trades

**None.** See Section 13.4.

## 14.7 Maximum Daily Loss

**None.** No daily-loss circuit breaker exists.

## 14.8 Volatility Filters

No volatility rejection. ATR only sizes the stop and the holding horizon.

## 14.9 Broker-side protections (shared infrastructure)

These live in `angel_one_service.py` and protect the whole platform:

| Protection | Detail |
|---|---|
| **Per-category circuit breaker** | `_REST_CIRCUIT_BREAKER_UNTIL = {"candle": 0.0, "quote": 0.0}`. On HTTP 403/429 or an HTML (WAF) body → that category is disabled for **300 s**. Split per category so a candle rate-limit doesn't blackout quote fetches. |
| **Candle rate limit** | `_candle_api_lock` enforces ≥ **1.05 s** between *all* candle calls process-wide. |
| **Quote-fallback rate limit** | `_rest_quote_fallback` shares the same 1.05 s lock. |
| **Bulk-quote pacing** | `_bulk_quote_api_lock` enforces ≥ **0.5 s** between bulk-quote calls. |
| **Scanner sleep** | `time.sleep(0.35)` per candidate in the scan loop. |
| **Auth throttle** | `AUTH_RETRY_COOLDOWN = 60 s` after a failed login. |
| **Session refresh** | Forced re-auth after 18 h. |
| **AG8001 recovery** | On "invalid token", force re-auth and retry the request once. |
| **WebSocket self-heal** | Disconnected streamer restarted under `_STREAMER_RESTART_LOCK` (non-blocking `acquire`) so only one thread rebuilds the socket. |
| **Bootstrap guard** | `_BOOTSTRAP_RUNNING` `threading.Event` prevents duplicate Nifty-500 subscription threads. |
| **Candle response cache** | `get_candle_data` caches by `(exchange, token, interval, lookback_days)` for **240 s**, so engines running in the same cycle share one REST call. |

## 14.10 Data-integrity guards in the engine

- `if df.empty or len(df) < 100: continue` (scanner)
- `if df.empty or len(df) < 20: continue` (EOD)
- `if len(df) < max(201, 201): return None` (scoring)
- `if ltp <= 0: continue` (activation)
- `if sl_points <= 0: return None`
- `if vol_20d == 0: return None`
- Blanket `try/except` around every per-symbol iteration so one bad symbol never aborts a scan.

---

# SECTION 15 — Trade Lifecycle

## 15.1 State machine

`ShortTermSignal.Status` declares 14 values (`models.py:194-208`):
`PENDING, ACTIVE, TARGET1, TARGET2, HIT_TARGET, HIT_SL, TRAILING_EXIT, TIME_STOP, EXPIRED,
CANCELLED, REVIEW_REQUIRED, CLOSED, ARCHIVED, COOLDOWN`.

**Only 8 are ever assigned by live code:**
`PENDING, ACTIVE, TARGET1, TARGET2, EXPIRED, REVIEW_REQUIRED, ARCHIVED`, plus `HIT_TARGET`/`HIT_SL`
written by the dead `pro_system_service.update_pro_system_outcomes()` and the manual-only
`trade_engine.run_intraday_check()`.

**Never assigned by any code path:** `CLOSED`, `CANCELLED`, `COOLDOWN`.
(`TRAILING_EXIT`, `TIME_STOP`, `HIT_TARGET`, `HIT_SL` are passed to `_exit_signal` as the
`status` argument and recorded in `TradeHistory`, but the row's own `status` is then
overwritten with `ARCHIVED` — so they never persist on `ShortTermSignal.status`.)

```
                      10:00 scanner
                            │
                            ▼
                       ┌─────────┐
                       │ PENDING │◄─── created by _run_daily_scanner_impl
                       └────┬────┘
              ltp<=entry    │              42 calendar days
        (check_pending_     │              (run_expiry_cleanup)
          activations)      │                      │
                            ▼                      ▼
                       ┌─────────┐            ┌─────────┐
                       │ ACTIVE  │            │ EXPIRED │ (terminal)
                       └────┬────┘            └─────────┘
                            │
       ┌────────────────────┼──────────────────────────┐
       │ high>=T1           │ high>=T2                 │ exit conditions
       ▼                    ▼                          ▼
  ┌─────────┐         ┌─────────┐              _exit_signal(...)
  │ TARGET1 │────────►│ TARGET2 │──────────────────────┤
  └─────────┘ high>=T2└────┬────┘                      │
   SL:=entry            SL:=entry                      ▼
       │                    │                    ┌──────────┐
       └────────────────────┴───────────────────►│ ARCHIVED │ (terminal)
                            │                    │ +28d     │
        90 calendar days    │                    │ cooldown │
       (run_expiry_cleanup) │                    └──────────┘
                            ▼
                  ┌──────────────────┐
                  │ REVIEW_REQUIRED  │  (still open, needs human action)
                  └──────────────────┘
```

## 15.2 Signal Created

`_run_daily_scanner_impl` Step 7. `status=PENDING`, `generated_at=auto_now_add`,
`activated_at=NULL`, `current_price=current_ltp`, `ai_score`, `expected_holding_days`, `setup`,
`vol_ratio`, `entry_price`, `stop_loss`, `target`(=T1), `target2`, `target3`.
Audit: `TradeHistory('NONE' → 'PENDING', reason='Scanner Discovery')`.

## 15.3 Pending

- Price refreshed every 30 min by `check_pending_activations` (`current_price` only).
- Included in the 10:05 / 15:35 Telegram status under "⏳ PENDING PULLBACKS".
- Expires after **42 calendar days** (`int(30 × 7/5)`) measured from `generated_at`, in
  `run_expiry_cleanup`. Transition is done under `select_for_update()` with a status re-check.
  Audit `TradeHistory('PENDING' → 'EXPIRED')`, Telegram `SETUP_EXPIRED`.

## 15.4 Active

- Entered when `ltp <= entry_price`. `activated_at = timezone.now()`.
- Audit `TradeHistory('PENDING' → 'ACTIVE', reason='Entry Trigger Hit')`.
- Telegram `BUY_ACTIVATED` with entry trigger, current price, SL, T1/T2/T3, activation time.
- Evaluated once daily at 3:25 PM.

## 15.5 Target 1 / Target 2

Not exits. On T1: `status=TARGET1`, `stop_loss = entry_price`, `TradeHistory`, Telegram
`TARGET1_HIT`. On T2 (from ACTIVE or TARGET1): `status=TARGET2`, `stop_loss = entry_price`,
`TradeHistory`, Telegram `TARGET2_HIT`.

Both remain in the EOD evaluation set (`ACTIVE, TARGET1, TARGET2`).

## 15.6 Target Hit (final)

`latest_high >= target3` → `_exit_signal(sig, t3_val, HIT_TARGET, "Final Target Hit", …)`.

## 15.7 Stop Hit

`latest_low <= stop_loss` → `_exit_signal(sig, sl_val, HIT_SL, "Stop Loss Triggered", …)`.
Checked **first**, before the trailing and target checks.

## 15.8 Cancelled

`ShortTermSignal.Status.CANCELLED` exists but **nothing sets it.**
`get_dashboard_data` builds a `cancelled` tab and a `closed_legacy = expired_list + cancelled_list`
group that will always be missing the cancelled half.

## 15.9 Expired

Only from `PENDING`, only in `run_expiry_cleanup`, at 42 calendar days. Terminal — no cooldown
is applied, so an expired symbol can be re-picked by the very next scan.

## 15.10 Closed

`ShortTermSignal.Status.CLOSED` is **never assigned.** It is referenced in
`get_dashboard_data`'s realized-P&L query and in `pro_system_service`'s exclude-lists, where it
matches nothing.

## 15.11 Archived

The real terminal state. `_exit_signal` sets, in one `save()`:
```python
sig.exit_price   = round(exit_price, 2)
sig.exited_at    = timezone.now()
sig.exit_reason  = exit_reason              # human string, e.g. "Trailing Exit"
sig.pnl_pct      = (exit_price − entry)/entry × 100
sig.status       = ARCHIVED
sig.cooldown_until = now + 28 calendar days
```
Then **two** `TradeHistory` rows: `old_status → <real exit status>` and
`<real exit status> → ARCHIVED (reason='Position Archived, Cooldown Active')`.
Then one Telegram `EXIT_{status}` with emoji/title per exit type.

The true exit reason is recoverable from `exit_reason` and `TradeHistory`, **not** from
`status` — every closed trade reads `ARCHIVED`.

> This is why `get_dashboard_data`'s win-rate is structurally broken:
> `total_wins = filter(status=HIT_TARGET).count()` and
> `total_losses = filter(status=HIT_SL).count()` both count rows that the live engine never
> produces, so `win_rate` reads 0.0 %. The legacy tabs work around it with string matching on
> `exit_reason` (`'Target' in exit_reason` → win; `'Stop'`/`'Trailing'` → loss) — note
> `TIME_STOP`'s reason `"Time-Stop Exit"` contains neither, so time-stopped trades appear in
> neither legacy bucket.

## 15.12 Review Required

`run_expiry_cleanup`: any row in `{ACTIVE, TARGET1, TARGET2}` with
`activated_at < now − 90 calendar days` (`active_review_calendar_days`) →
`status=REVIEW_REQUIRED`, `review_required=True`, `TradeHistory`, Telegram `REVIEW_REQUIRED`.

**This does not close the position and does not remove it from the scanner's duplicate block.**
It also removes the row from the EOD evaluation set (which queries only
`ACTIVE/TARGET1/TARGET2`), so a `REVIEW_REQUIRED` position stops being monitored entirely and
can only be resolved manually in the DB/admin.

## 15.13 Audit trail

`TradeHistory` (table `trade_history`) captures every transition:
`trade` (FK), `old_status`, `new_status`, `price`, `reason`, `triggered_by` (always `'SYSTEM'`),
`timestamp`. There is no UI or API that exposes it.

---

# SECTION 16 — Data Flow

## 16.1 End-to-end

```
NSE archive CSV ─┐
IndexConstituent ┼─► symbols (Nifty 500)
NIFTY500_FALLBACK┘          │
                            ▼
        Angel One instrument master (OpenAPIScripMaster.json, 6h TTL)
                            │  get_token_map()
                            ▼
        Angel One /market/v1/quote  (FULL, chunks of 50, ≥0.5s apart)
                            │  change_pct > 0.5, volume > 50k, top 50 by change
                            ▼
        Angel One /historical/v1/getCandleData  (ONE_DAY, 365d, ≥1.05s apart, 240s cache)
                            │
                            ▼
              _compute_ai_score()  ── 9 gates ──► ai_score, entry, SL, T1/T2/T3, holding_days
                            │  sort desc
                            ▼
              ShortTermSignal (PENDING)  +  TradeHistory
                            │
        ┌───────────────────┼────────────────────────┐
        ▼                   ▼                        ▼
  TelegramLog        check_pending_activations   run_eod_evaluation
  (PENDING)          (30 min, quotes)            (3:25 PM, daily candles)
        │                   │                        │
        │ 1-min queue       ▼                        ▼
        ▼             ACTIVE + TradeHistory    TARGET1/2 | ARCHIVED
  Telegram Bot API          │                        │
                            └────────────┬───────────┘
                                         ▼
                         GET /api/stocks/pro-system/  (30s cache)
                                         ▼
                              ProSystem.jsx tabs + analytics
```

## 16.2 Market Data

Two independent paths, both inside `AngelOneService`:

**Snapshot (REST)** — `get_bulk_quotes(exchange_token_map, mode="FULL")`:
1. For each token, check `_STREAM_CACHE` via `get_stream_price(token)`.
   Fresh = `source == "websocket"` **or** `age < STREAM_CACHE_TTL (30 s)`; and if
   `mode == "FULL"`, additionally requires `high > 0 and low > 0`.
2. Only stale/missing tokens go to REST.
3. Circuit-breaker check → return partial results if tripped.
4. Warm the WebSocket by subscribing every token about to be REST-fetched.
5. Pace ≥0.5 s, POST, parse via `_parse_quote_item`, and **write the results back into
   `_STREAM_CACHE`** with `source="rest_fallback"` so subsequent lookups hit the cache.

`_parse_quote_item` normalises: `ltp` (tries `lastTradedPrice, ltp, lastPrice, currentPrice,
price, last_price`), `bid`/`ask` from `depth.buy[0]`/`depth.sell[0]` with fallbacks to
`buyModel`/`bestBuyPrice`/`bp`/`bp1`, and **synthesises `bid = ltp × 0.998`, `ask = ltp × 1.002`
when both are zero**, plus `high, low, close, change, change_percent, trade_volume, open_interest`.

**Streaming (WebSocket)** — `AngelOneStreamer` writes ticks into the module-global
`_STREAM_CACHE` under `_STREAM_LOCK`. Bootstrap subscribes indices `99926000` (NIFTY),
`99926009` (BANKNIFTY), `99926037` (FINNIFTY) plus up to 500 Nifty-500 equity tokens
(exchange type `1` = NSE, `2` = NFO, `5` = MCX). Deferred subscriptions are batched by
`_batch_subscriber_worker` every 2 s in chunks of 50.

**Historical** — `get_candle_data(token, exchange, interval, from_date, to_date)`; the short-term
engine always uses `interval="ONE_DAY"` with a 365-day (scanner) or 120-day (EOD) window.

## 16.3 Indicators

Computed **in-process on pandas DataFrames**. Nothing is persisted:
`StockDailyData` has `ema20/ema50/ema200/rsi14/adx14/atr14/high_52week/...` columns and is
never written. Indicator values that survive are only those copied into the
`ShortTermSignal` row: `vol_ratio`, `setup`, `ai_score`, and the derived price levels.
`adx`, `rsi`, `ema20/50/200`, `atr`, `high_52w` are returned by `_compute_ai_score` and used
only for the Telegram text and immediate logic — they are **not stored**.

## 16.4 Signal Engine → Database

Single write point: `ShortTermSignal.objects.create(...)` inside `transaction.atomic()`,
paired with a `TradeHistory` row.

## 16.5 Database → Frontend

`GET /api/stocks/pro-system/` → `trade_engine.get_dashboard_data()`:
- `ShortTermSignal.objects.all().order_by('-generated_at')` → `_fmt()` per row.
- Grouped into 11 tabs (7 current + 4 legacy).
- `analytics` block.
- `market_direction` — **calls `get_market_direction()` on every uncached request**, i.e. a
  365-day Angel One candle fetch on a page load. Mitigated by the 30 s response cache.
- `long_term` block — a parallel structure sourced from `SignalHistory(category="long_term")`,
  with live prices via `live_signal_service.get_latest_prices()`.

## 16.6 Notifications

Section 1.11. Message inventory for the short-term system:

| `event_type` | Fired by | Trigger |
|---|---|---|
| `DAILY_SCANNER_SUMMARY` | `_send_telegram_scanner_summary` | Scan produced ≥1 new pick |
| `SCANNER_NO_SETUPS` | `_send_telegram_scanner_summary` | Scan produced 0 picks |
| `SWING_STATUS_UPDATE` | `updater.send_short_term_status_update` | 10:05 and 15:35 |
| `BUY_ACTIVATED` | `check_pending_activations` | PENDING → ACTIVE |
| `TARGET1_HIT` | `run_eod_evaluation` | ACTIVE → TARGET1 |
| `TARGET2_HIT` | `run_eod_evaluation` | → TARGET2 |
| `EXIT_HIT_TARGET` / `EXIT_HIT_SL` / `EXIT_TRAILING_EXIT` / `EXIT_TIME_STOP` | `_exit_signal` | Position archived |
| `SETUP_EXPIRED` | `run_expiry_cleanup` | PENDING → EXPIRED |
| `REVIEW_REQUIRED` | `run_expiry_cleanup` | 90-day holding |
| `EOD_PORTFOLIO_STATUS` | `_send_telegram_eod_status` | End of `run_eod_evaluation` |

All HTML `parse_mode`, all routed to the short-term chat id.

## 16.7 Charts

**No charting for the short-term system.** `frontend/src/components/` contains
`DeliveryChart.jsx`, `VolumeSpikeChart.jsx`, `OIChangeChart.jsx` — all belong to other
dashboard cards. `ProSystem.jsx` renders tables and stat tiles only. No candlestick chart,
no equity curve, no indicator overlay exists anywhere in the app.

---

# SECTION 17 — Scheduler

`backend/stocks/updater.py::start()`. Timezone `Asia/Kolkata`. Defaults:
`misfire_grace_time=300`, `max_instances=1`, `replace_existing=True`.

## 17.1 Every registered job

| # | Job id | Trigger | Function | Belongs to |
|---|---|---|---|---|
| 1 | `trade_engine_premarket` | cron mon–fri **09:05:00** | `run_premarket_update` → `pro_system_service.get_market_direction` | short-term |
| 2 | `trade_engine_scanner_10am` | cron mon–fri **10:00:00** | `run_short_term_scan` → `trade_engine.run_daily_scanner` | **short-term (signal generation)** |
| 3 | `trade_engine_status_1005am` | cron mon–fri **10:05:00** | `send_short_term_status_update` | short-term |
| 4 | `trade_engine_activation_checker` | cron mon–fri `hour="10-15", minute="15,45"` → **12 firings** 10:15…15:45 | `run_intraday_check` (wrapper) → `trade_engine.check_pending_activations` | short-term |
| 5 | `trade_engine_eod_325pm` | cron mon–fri **15:25:00** | `run_eod_evaluation` → `trade_engine.run_eod_evaluation` | short-term |
| 6 | `trade_engine_status_335pm` | cron mon–fri **15:35:00** | `send_short_term_status_update` | short-term |
| 7 | `trade_engine_weekly_cleanup` | cron **sat 06:00:00** | `run_expiry_cleanup` → `trade_engine.run_expiry_cleanup` | short-term |
| 8 | `strangle_signal_10am` | cron mon–fri **10:45** | `run_10am_strangle_scan` → `manage.py generate_strangle_signals --telegram-delay 30` | option selling |
| 9 | `live_signal_update_hourly` | cron mon–fri `hour="11-14", minute="0,15,30,45"` | `run_periodic_scanners()` | intraday + options |
| 10 | `live_signal_update_final_hour` | cron mon–fri `hour=15, minute="0,15"` | `run_periodic_scanners()` | intraday + options |
| 11 | `final_eod_update_328` | cron mon–fri **15:28:00** | `run_periodic_scanners(action="update")` | intraday + options |
| 12 | `daily_market_update` | cron mon–fri **16:30** | `run_daily_pipeline` → `manage.py run_daily_market_update` | analytics/AI |
| 13 | `user_account_cleanup` | interval **1 min, all days** | `run_user_cleanup` | platform |
| 14 | `telegram_queue_dispatcher` | interval **1 min, all days** | `run_telegram_queue` → `process_telegram_queue` | platform |

Job ids 5 and 6 are misleadingly named (`_325pm` fires at 15:25; `_335pm` at 15:35 — that one
is correct). Job 4's id and comment say "10:15 AM - 3:15 PM every 30 minutes" but the trigger
produces a 15:45 firing as well.

## 17.2 Frequency summary (short-term only)

| Cadence | Jobs |
|---|---|
| Once daily | premarket (9:05), scanner (10:00), EOD (15:25) |
| Twice daily | status update (10:05, 15:35) |
| 12× daily | activation checker |
| Weekly | Saturday cleanup |
| Every minute | Telegram queue dispatcher (shared) |

## 17.3 Execution Order

**Within a trading day:**
`09:05 premarket → 10:00 scanner → 10:05 status → 10:15…15:45 activation ×12 → 15:25 EOD → 15:35 status`

**Within the 10:00 scanner:**
`lock → market direction → broker → universe → tokens → bulk quotes → prefilter → nifty baseline
→ per-candidate candles+score (~0.35–1 s each) → sort → persist → Telegram (which triggers the
long-term scan) → unlock`

**Within `run_eod_evaluation`, per signal:** the six ordered checks of Section 2.

## 17.4 Dependencies

| Job | Hard dependency | Behaviour if unmet |
|---|---|---|
| All | Angel One authenticated singleton | `svc is None` → log error, return |
| Scanner | Nifty 500 universe | Falls back to `NIFTY500_FALLBACK` |
| Scanner | Instrument master indexed | `get_token_map` returns `{}` → return `[]` |
| Scanner | Non-BEARISH direction | Strict pass aborts; relaxed pass proceeds |
| Activation checker | ≥1 PENDING row | Logs and returns |
| EOD | ≥1 ACTIVE/TARGET1/TARGET2 row | Logs and returns |
| EOD (per symbol) | ≥20 daily candles | `continue` |
| Telegram dispatcher | `TELEGRAM_ALERTS_ENABLED` + BOT_TOKEN + CHAT_ID | `send_telegram_message` returns False → `retry_count++`, `FAILED` at 3 |

**Ordering hazard:** the activation checker's 10:15 run fires while the 10:00 scanner may still
be running (the scan is ~50 sequential candle fetches at ~1 s each plus a full Nifty-500 bulk
quote sweep, so it commonly exceeds 15 minutes when the relaxed second pass also runs — the
relaxed pass repeats the entire sweep). The two jobs take different locks, so they overlap and
compete for the same rate-limited Angel One session.

`run_periodic_scanners` (jobs 9–11) additionally calls the intraday scan, the specialist scan,
and the option-buying scan against the **same** shared session — the `run_periodic_scanners_running`
lock protects those three from each other, but **not** from the short-term scanner.

## 17.5 External triggers

`GET /api/stocks/cron-trigger/?token=<CRON_SECRET_TOKEN>&action=<...>` — see Section 1.8.
Blocked on non-trading days unless `&force=1`.

---

# SECTION 18 — Performance

## 18.1 Caching

| Key | TTL | Contents |
|---|---|---|
| `trade_engine_dashboard_30s` | 30 s | Full `/pro-system/` payload |
| `dashboard_summary_20s` | 20 s | Top-3 preview for all 6 categories |
| `candle_cache_{exchange}_{token}_{interval}_{lookback_days}` | 240 s | A candle DataFrame; `.copy()` on read |
| `orchestrator_price_{symbol}_{exchange}` | 5 s | Single-symbol LTP dict |
| `trade_engine_scanner_running` | 600 s | Scanner lock |
| `run_periodic_scanners_running` | 600 s | Periodic-scanner lock |
| `nse_holidays_sync_success` | 86 400 s | Holiday sync success flag |
| `nse_holidays_sync_failed_cooldown` | 21 600 s | Holiday sync backoff |

In-memory (module-global, per process):
`_INSTRUMENT_MASTER_CACHE` (6 h), `_STREAM_CACHE` (WebSocket ticks, 30 s TTL for REST-sourced
entries), `_NSE_STATUS_CACHE` (60 s), `_MARKET_OPEN_CACHE` (60 s).

The candle cache is keyed on **rounded lookback days**, not on exact `from_date`/`to_date`, so
the scanner's 365-day request and the EOD's 120-day request never collide, but two engines
asking for the same window inside 4 minutes share one REST call.

## 18.2 Parallel Processing

**Essentially none in the short-term engine.** The scan loop is strictly sequential with an
explicit `time.sleep(0.35)`. `pro_system_service.py` imports `ThreadPoolExecutor` but never
uses it. The only threads in the system are:
- APScheduler's worker pool (jobs are `max_instances=1`),
- `AngelOneStreamer`'s WebSocket thread,
- `_batch_subscriber_worker` (daemon, 2 s loop),
- `_bootstrap_subscriptions` (one-shot daemon),
- ad-hoc `threading.Thread` spawned by `CronScannerTriggerView` for manual triggers.

Sequential-by-design: concurrency here previously corrupted the shared `requests.Session`
(documented in `run_daily_scanner`'s docstring).

## 18.3 Batch Processing

- Quotes: chunks of **50** tokens per REST call, throughout (`trade_engine.py:415, 583, 1566`;
  `pro_system_service.py:287`).
- WebSocket subscriptions: chunks of **50**.
- Telegram queue: batches of **20** per dispatcher run.
- Candles: **not batchable** — Angel One's historical endpoint is one token per call. This is
  the scan's bottleneck: 50 candidates × ~1 s = ~50 s minimum, doubled by the relaxed pass.

## 18.4 Retry Logic

| Layer | Retry |
|---|---|
| Angel One AG8001 (invalid session) | Force re-auth, retry the request **once** — implemented in `get_live_price_by_token`, `get_bulk_quotes`, `get_candle_data` |
| Angel One 403/429/WAF | **No retry.** Trip the 300 s circuit breaker and return empty |
| Bhavcopy download | Walk back up to 7 trading days on 404 (`_find_available_date`) — not used by the swing engine |
| Telegram | Up to **3** attempts via `TelegramLog.retry_count`, one per minute |
| Scanner strict → relaxed | One automatic fallback pass |
| Everything else | No retry |

## 18.5 Error Handling

- Every per-symbol iteration is wrapped in `try/except Exception` that logs and `continue`s.
- Every scheduler wrapper in `updater.py` wraps its call in `try/except` and logs.
- `run_periodic_scanners` wraps each sub-scan separately so one engine's failure doesn't
  block the others.
- `ProSystemView` has no try/except — an exception surfaces as a DRF 500.
  `OptionBuyingView` and `DeltaHedgeView` do catch and return a soft payload.
- `close_old_connections()` is called at the start of every scheduled job (and in the
  cron-trigger threads) to avoid stale Postgres connections after idle periods.

## 18.6 Rate Limits

Angel One documented/observed limits and the code's response:

| Endpoint | Enforced pacing | Breaker |
|---|---|---|
| Historical candles (~3/s) | Global `_candle_api_lock`, **≥1.05 s** between calls | `_REST_CIRCUIT_BREAKER_UNTIL["candle"]`, 300 s |
| Bulk quote | `_bulk_quote_api_lock`, **≥0.5 s** | `_REST_CIRCUIT_BREAKER_UNTIL["quote"]`, 300 s |
| Single quote fallback | Shares the 1.05 s candle lock | quote breaker |
| Login | 60 s cooldown after failure; session reused 18 h | — |

Trip conditions: HTTP `403`, HTTP `429`, or a response body containing `<html` (Angel One's
WAF returns an HTML block page).

Additional load reduction already in place: the intraday engine was moved from NIFTY500 to
**NIFTY100** (`signal_utils.INTRADAY_UNIVERSE`) and to batched bulk quotes specifically to
lower shared-session pressure. The short-term scanner still sweeps the full Nifty 500 for
quotes twice a day (once per pass).

## 18.7 Known performance characteristics

- **Scanner wall time:** dominated by 50 sequential candle fetches at ~1 s each (~50 s), plus
  ~10 bulk-quote chunks at 0.5 s (~5 s), plus the Nifty baseline fetch. A strict-then-relaxed
  double pass repeats all of it, and `_send_telegram_scanner_summary` then triggers the
  long-term scan (75 more sequential candle fetches at 0.35 s + 1.05 s lock ≈ 80 s).
  Total ≈ 3–4 minutes on a clean run.
- **`get_dashboard_data()` cost:** `ShortTermSignal.objects.all()` with **no limit** — the
  payload grows unbounded with history. Plus one `get_market_direction()` candle fetch and one
  bulk price fetch for active long-term symbols.
- **No DB index** exists on `ShortTermSignal.cooldown_until`? — it is declared
  `db_index=True` (`models.py:234`). `status`, `symbol`, `generated_at` are also indexed.

---

# SECTION 19 — Daily Example

A concrete walk-through with real arithmetic. Input values are illustrative; every formula and
constant is quoted from the code.

**Stock: `TITAN`. Date: Monday. Strict pass.**

### 09:05 — Pre-market
`get_market_direction()` fetches 120 days of Nifty daily candles (token `99926000`).
```
close = 24,850.00 ; ema20 = 24,610.30 ; ema50 = 24,180.75
24850 > 24610.30 and 24850 > 24180.75  →  trend = "BULLISH"
```

### 10:00 — Scanner starts
`cache.add("trade_engine_scanner_running")` succeeds. `trend != 'BEARISH'` → proceed.
`_fetch_nifty500_symbols()` returns 500 symbols from `IndexConstituent`.
`get_token_map` resolves `TITAN → "3506"`.

### 10:00 — Pre-filter
Bulk quote for `NSE:3506`:
```
ltp = 3,420.00 ; change_percent = 1.85 ; trade_volume = 1,240,000
1.85 > 0.5      ✓
1,240,000 > 50,000 ✓
```
Sorted by `change_pct`, TITAN lands at rank 12 → inside `[:50]` ✓

### 10:00 — Nifty baseline
```
nifty.Close[-1] = 24,850 ; nifty.Close[-20] = 24,100
nifty_20d_ret = (24850 − 24100)/24100 = 0.03112   (+3.11 %)
```

### 10:01 — Candle fetch
`time.sleep(0.35)`, then `get_candle_data("3506","NSE","ONE_DAY", now−365d, now)` →
280 daily rows. `280 >= 100` ✓ and `280 >= 201` ✓

### 10:01 — `_compute_ai_score`

**Inputs from the DataFrame**
```
close    = 3,420.00
ema20    = 3,358.40      # ewm(span=20)
ema50    = 3,281.60      # ewm(span=50)
ema200   = 3,102.15      # ewm(span=200)
ADX14    = 31.4
RSI14    = 64.2
ATR14    = 58.30
high_52w = 3,510.00      # rolling(252, min_periods=100).max()
high_20d = 3,398.00      # rolling(20).max().iloc[-2]
vol_5d   = 1,180,000
vol_20d  =   690,000
today_vol= 1,240,000
Close[-20] = 3,190.00
```

**Filter 0 — length:** `280 >= max(201, 201)` ✓

**Filter 1 — trend stack:** `3420 > 3281.60 > 3102.15` ✓

**Filter 2 — ADX:** `31.4 >= 25.0` ✓

**Filter 3 — relative strength:**
```
stock_20d_ret = (3420 − 3190)/3190 = 0.07210   (+7.21 %)
0.07210 >= 0.03112  ✓
```

**Filter 4 — breakout:**
```
is_near_52w  = 3420 >= 3510 × 0.95 = 3334.50  → True  ✓
is_20d_break = 3420 >= 3398                    → True
```
Passes. `setup = '20d Breakout'` (the `is_20d_break` branch wins when both are true).

**Filter 5 — volume expansion:**
```
1,180,000 >= 690,000 × 1.5 = 1,035,000  ✓
```

**Filter 6 — liquidity floor:** `1,240,000 >= 100,000` ✓

**Trade parameters**
```
entry_price  = 3,420.00
stop_loss    = 3420 − (2.0 × 58.30) = 3420 − 116.60 = 3,303.40
max_sl_floor = 3420 × 0.90 = 3,078.00
3303.40 > 3078.00  → floor not applied
sl_points    = 3420 − 3303.40 = 116.60      (3.41 % risk)

target1 = 3420 + 2.0 × 116.60 = 3,653.20    (+6.82 %)
target2 = 3420 + 3.0 × 116.60 = 3,769.80    (+10.23 %)
target3 = 3420 + 4.0 × 116.60 = 3,886.40    (+13.64 %)
rr_ratio = (3653.20 − 3420)/116.60 = 2.0    ✓ >= 2.0
```

**Score**
```
ema_spread   = (3420 − 3102.15)/3102.15 × 100 = 10.246
trend_score  = min(25, 10.246 × 1.5)          = 15.37

adx_contrib  = min(12.5, (31.4 − 25.0) × 0.5) = 3.20
rsi_contrib  = min(12.5, (64.2 − 40) × 0.25)  = 6.05
momentum     = 3.20 + 6.05                     = 9.25

vol_ratio    = 1,180,000 / 690,000             = 1.710
volume_score = min(20, (1.710 − 1.0) × 10)     = 7.10

rs_spread    = (0.07210 − 0.03112) × 100       = 4.098
sector_score = min(15, 4.098 × 3.0)            = 12.29

pct_from_52w = (3510 − 3420)/3510 × 100        = 2.564
risk_score   = min(15, 15 − 2.564 × 3.0)       = 7.31

ai_score = 15.37 + 9.25 + 7.10 + 12.29 + 7.31  = 51.32
51.32 >= 25.0  ✓
```

**Holding period**
```
atr_pct      = 58.30 / 3420 × 100 = 1.7047
holding_days = max(15, min(90, int(60 / 1.7047)))
             = max(15, min(90, int(35.196)))
             = 35
expiry_days  = 45      (returned, never persisted)
```

**Result dict** (rounded as in the code):
`ai_score 51.32 | trend 15.37 | momentum 9.25 | volume 7.10 | sector 12.29 | risk 7.31 |
fundamental 10.0 (unused) | entry 3420.0 | SL 3303.4 | T1 3653.2 | T2 3769.8 | T3 3886.4 |
holding_days 35 | vol_ratio 1.71 | adx 31.4 | rsi 64.2 | setup '20d Breakout'`

### 10:03 — Persistence
Sorted by `ai_score`, TITAN sits 4th of 7 survivors.
No existing `ShortTermSignal(TITAN)` in `{PENDING, ACTIVE, TARGET1, TARGET2, REVIEW_REQUIRED}` ✓
No `ARCHIVED` row with `cooldown_until > now` ✓
```sql
INSERT INTO short_term_signals
 (symbol, entry_price, stop_loss, target, target2, target3, current_price,
  vol_ratio, setup, status, expected_holding_days, ai_score, generated_at)
VALUES ('TITAN', 3420.00, 3303.40, 3653.20, 3769.80, 3886.40, 3420.00,
        1.71, '20d Breakout', 'PENDING', 35, 51.32, now());

INSERT INTO trade_history (trade_id, old_status, new_status, price, reason, triggered_by)
VALUES (<id>, 'NONE', 'PENDING', 3420.00, 'Scanner Discovery', 'SYSTEM');
```

### 10:04 — Telegram queued
```
🚀 DAILY SCANNER — NEW BUY TODAY SETUPS
🕒 <date>, 10:04 AM IST
Market: 🟢 BULLISH | Nifty: 24850.0
━━━━━━━━━━━━━━━━━━━━

4. TITAN (AI: 51/100)
   Buy: ₹3420.0 | SL: ₹3303.4
   T1: ₹3653.2 | T2: ₹3769.8
   Timeframe: ~35 days

⚡ Action: Buy at current market price
[ACTIVE HOLDINGS block][ACTIVE LONG-TERM block][NEW LONG-TERM SETUPS block]
```
Row written to `telegram_logs` as `PENDING`; the 1-minute dispatcher delivers it by ~10:05.

### 10:15 → 15:45 — Activation checks (12 firings, same day)
```
10:15  ltp 3,431.50  →  3431.50 <= 3420.00 ? No
10:45  ltp 3,444.00  →  No
11:15  ltp 3,428.20  →  No
...
14:15  ltp 3,417.85  →  YES
```
Inside `transaction.atomic()` + `select_for_update()`, status still `PENDING`:
```
status       = 'ACTIVE'
activated_at = <that timestamp>
TradeHistory: 'PENDING' → 'ACTIVE', price 3417.85, reason 'Entry Trigger Hit'
TelegramLog:  BUY_ACTIVATED
```
Note: `entry_price` stays **3420.00**. All future P&L is measured against 3420.00, not against
the 3417.85 at which activation was detected.

### 15:25 — EOD evaluation, Day 1
120-day candles for token 3506. `latest_close = 3,441.00`, `high = 3,452.00`, `low = 3,408.00`.
```
holding_days   = (today − activated_at.date()).days = 0
highest_profit = max(0.00, (3452 − 3420)/3420 × 100 = +0.94)   → 0.94
max_drawdown   = min(0.00, (3408 − 3420)/3420 × 100 = −0.35)   → −0.35
pnl_pct        = (3441 − 3420)/3420 × 100 = +0.61

Check 1  low 3408 <= SL 3303.40 ?              No
Check 2  close 3441 < EMA20 3,371.90 ?         No
Check 3  high 3452 >= T3 3886.40 ?             No
Check 3.5 elapsed 0 >= 35 ?                    No
Check 4  high 3452 >= T2 3769.80 ?             No
Check 5  high 3452 >= T1 3653.20 ?             No
→ sig.save() only
```

### Day 9 — Target 1
`latest_high = 3,661.00`, `latest_close = 3,655.20`, `EMA20 = 3,512.80`.
Checks 1–3.5 all fail; Check 4 (T2 3769.80) fails; **Check 5 passes**:
```
status    = 'TARGET1'
stop_loss = entry_price = 3,420.00          # risk removed
TradeHistory: 'ACTIVE' → 'TARGET1', price 3655.20,
              reason 'Target 1 reached (₹3653.20), stop loss locked to entry ₹3420.00'
TelegramLog:  TARGET1_HIT
```

### Day 16 — Target 2
`latest_high = 3,781.00`, `latest_close = 3,774.50`.
**Check 4 passes** (status is `TARGET1`, which is in the allowed set):
```
status    = 'TARGET2'
stop_loss = 3,420.00        # already at entry; re-assigned
TradeHistory: 'TARGET1' → 'TARGET2'
TelegramLog:  TARGET2_HIT
```

### Day 23 — Exit via trailing stop
`latest_high = 3,802.00`, `latest_low = 3,690.00`, `latest_close = 3,704.00`, `EMA20 = 3,718.60`.
```
Check 1  low 3690 <= SL 3420 ?          No
Check 2  close 3704 < EMA20 3718.60 ?   YES  →  exit
```
`_exit_signal(sig, 3704.00, TRAILING_EXIT, "Trailing Exit",
 "EOD close ₹3704.00 below daily 20 EMA (₹3718.60)")`:
```
exit_price     = 3704.00
exited_at      = now()
exit_reason    = 'Trailing Exit'
pnl_pct        = (3704 − 3420)/3420 × 100 = +8.30
status         = 'ARCHIVED'
cooldown_until = now + 28 days

TradeHistory: 'TARGET2' → 'TRAILING_EXIT', price 3704.00, reason '<details>'
TradeHistory: 'TRAILING_EXIT' → 'ARCHIVED',  price 3704.00, reason 'Position Archived, Cooldown Active'
TelegramLog:  EXIT_TRAILING_EXIT
```

Telegram body:
```
📉 TRAILING EXIT
━━━━━━━━━━━━━━━━━━━━
Stock: TITAN
▸ Entry Price: ₹3420.00
▸ Exit Price: ₹3704.00
▸ Final Profit: +8.30%
▸ Holding Duration: 23 Days
▸ Reason: EOD close ₹3704.00 below daily 20 EMA (₹3718.60)
━━━━━━━━━━━━━━━━━━━━
```

Note: target 3 (₹3886.40) was never reached, so this trade does **not** count as a
`HIT_TARGET`. In `get_dashboard_data`'s legacy buckets it is classified a **loss**, because
`hit_sl_legacy` matches `'Trailing' in exit_reason` — despite the +8.30 % gain. The
`archived` tab shows it correctly with its P&L.

### Days 24–51 — Cooldown
TITAN is skipped by every scan until `cooldown_until` passes (28 calendar days).

### Dashboard
`ProSystem.jsx`, "Archived 📦" tab, with `capital = 500000`:
```
maxLossRs     = round(500000 × 1.5/100) = 7,500
slAmt         = 3420.00 − 3420.00 = 0        ← SL was trailed to entry
posSizeShares = slAmt > 0 ? … : 0            = 0
pnlRupees     = null (posSizeShares == 0)
```
The ₹ P&L column renders blank for any trade whose stop was trailed to entry, because the
sizing formula divides by `entry − stop_loss`, which is exactly zero after T1.

---

# SECTION 20 — Complete Decision Tree

```
                    ┌────────────────────────────────────────┐
                    │  APScheduler, Asia/Kolkata, Mon–Fri    │
                    └────────────────────┬───────────────────┘
                                         │
                       09:05 ────────────┼──────── run_premarket_update()
                                         │         get_market_direction()  [logged only]
                                         │
                       10:00 ────────────▼──────── run_daily_scanner()
                                         │
                    ┌────────────────────▼────────────────────┐
                    │ LOCK  cache.add("trade_engine_scanner_  │
                    │       running", 600s)                   │
                    └────────────────────┬────────────────────┘
                                    held │ not held → SKIP RUN
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │ MARKET FILTER                           │
                    │ Nifty50 daily: close > EMA20 && > EMA50 │
                    └────────────────────┬────────────────────┘
                     BEARISH & strict ───┼─► return []  ──► relaxed pass re-enters here
                     BULLISH / NEUTRAL   │                   (gate NOT re-applied)
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │ UNIVERSE                                │
                    │ IndexConstituent(NIFTY500) → NSE CSV    │
                    │ → NIFTY500_FALLBACK                     │
                    └────────────────────┬────────────────────┘
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │ TOKEN RESOLUTION                        │
                    │ instrument master: sym → sym-EQ → token │
                    └────────────────────┬────────────────────┘
                              unresolved ─┴─► DROP
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │ STOCK FILTER (bulk quote, chunks of 50) │
                    │  change_pct  > 0.5    (relaxed −2.0)    │
                    │  trade_vol   > 50,000 (relaxed 10,000)  │
                    │  rank by change_pct → top 50            │
                    └────────────────────┬────────────────────┘
                                    fail ─┴─► REJECT
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │ INDICATOR FILTER (365d ONE_DAY candles) │
                    │  len(df) ≥ 201                          │
                    │  close > EMA50 > EMA200                 │
                    │  ADX14  ≥ 25.0        (relaxed 15.0)    │
                    │  stock_20d_ret ≥ nifty_20d_ret  [strict]│
                    │  close ≥ 52wH×0.95 OR close ≥ 20dH      │
                    │                       (relaxed ×0.90)   │
                    │  vol_5d ≥ 1.5 × vol_20d (relaxed 1.0)   │
                    │  today_volume ≥ 100,000                 │
                    └────────────────────┬────────────────────┘
                                    fail ─┴─► REJECT (not logged, not stored)
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │ LEVELS                                  │
                    │  entry = close                          │
                    │  SL    = max(entry − 2×ATR, entry×0.90) │
                    │  T1/T2/T3 = entry + {2,3,4} × sl_points │
                    │  RR = 2.0  (≥ 2.0 required)             │
                    └────────────────────┬────────────────────┘
                            sl_points≤0 ─┴─► REJECT
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │ SCORE  (0–100)                          │
                    │  trend    = min(25, spread200 × 1.5)    │
                    │  momentum = ADX part + RSI part (≤25)   │
                    │  volume   = min(20,(volRatio−1)×10)     │
                    │  RS       = min(15, rsSpread × 3)       │
                    │  risk     = min(15, 15 − from52w × 3)   │
                    │  fundamental 10.0 → NOT SUMMED          │
                    └────────────────────┬────────────────────┘
                          ai_score < 25 ─┴─► REJECT
                                         ▼
                              sort by ai_score DESC (no cap)
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │ RISK CHECK                              │
                    │  symbol already PENDING/ACTIVE/TARGET1/ │
                    │  TARGET2/REVIEW_REQUIRED ?  → SKIP      │
                    │  ARCHIVED & cooldown_until > now ? →SKIP│
                    └────────────────────┬────────────────────┘
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │ SIGNAL: ShortTermSignal(status=PENDING) │
                    │        + TradeHistory('NONE'→'PENDING') │
                    │ Telegram: top 10 → TelegramLog(PENDING) │
                    │  ⚠ also runs the LONG-TERM scan         │
                    └────────────────────┬────────────────────┘
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │ ENTRY  (10:15–15:45, every 30 min)      │
                    │  ltp <= entry_price ?                   │
                    └────────────────────┬────────────────────┘
                       no → stay PENDING │  ── 42 cal. days ──► EXPIRED (terminal)
                                     yes ▼
                    ┌─────────────────────────────────────────┐
                    │ ACTIVE   activated_at = now             │
                    │ TradeHistory + Telegram BUY_ACTIVATED   │
                    └────────────────────┬────────────────────┘
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │ MONITOR — once/day @ 15:25              │
                    │ (no intraday SL/target monitoring)      │
                    └────────────────────┬────────────────────┘
                                         ▼
        ┌───────────────────── ORDERED EXIT CHECKS ─────────────────────┐
        │ 1. low  ≤ SL              → HIT_SL        @ SL                │
        │ 2. close < EMA20(daily)   → TRAILING_EXIT @ close             │
        │ 3. high ≥ T3              → HIT_TARGET    @ T3                │
        │ 4. elapsed ≥ holding_days → TIME_STOP     @ close             │
        │ 5. high ≥ T2              → status TARGET2, SL := entry  ◄─┐  │
        │ 6. high ≥ T1 (elif)       → status TARGET1, SL := entry  ──┘  │
        │    none                   → save price/P&L, stay open         │
        └───────────────┬──────────────────────────────┬────────────────┘
                 1,2,3,4│                          5,6 │ (position stays open)
                        ▼                              └──► next day
        ┌─────────────────────────────────┐
        │ EXIT  _exit_signal()            │
        │  exit_price, exited_at,         │
        │  exit_reason, pnl_pct           │
        │  status = ARCHIVED              │
        │  cooldown_until = now + 28d     │
        │  2 × TradeHistory               │
        │  Telegram EXIT_<status>         │
        └─────────────────────────────────┘
                        │
                        ▼
        ┌─────────────────────────────────┐        ┌──────────────────────────┐
        │ Saturday 06:00 cleanup          │        │ 90 days ACTIVE/T1/T2     │
        │  PENDING > 42d → EXPIRED        │───────►│ → REVIEW_REQUIRED        │
        └─────────────────────────────────┘        │ (open, but no longer     │
                                                    │  monitored by EOD)       │
                                                    └──────────────────────────┘
```

---

# APPENDIX A — Boundary with the Intraday Engine

Included because Sections 2, 11, 12 and 20 ask about "square off", which exists only there.
Authoritative document: `doc/INTRADAY_BUY_SELL_LOGIC.md`. Code: `intraday_service.py`,
`live_signal_service.py`.

| Aspect | Short-term (this doc) | Intraday |
|---|---|---|
| Model | `ShortTermSignal` | `SignalHistory(category='intraday')` |
| Universe | Nifty 500 | **Nifty 100** (`INTRADAY_UNIVERSE`) |
| Timeframe | `ONE_DAY` candles, 365 d | `FIVE_MINUTE` candles, 2 d |
| Strategy | EMA stack + ADX + RS + breakout + volume | Volume Profile (POC flip, VA breakout, VA rejection) |
| Direction | BUY only | BUY and SELL |
| Entry | `ltp <= entry` (pullback) | `price_cross` on the scan-time price |
| SL | `entry − 2×ATR`, 10 % floor | `min(VAL, entry − 0.8×ATR)` for BUY |
| Target | 2R / 3R / 4R | `entry ± 2.0 × |entry − SL|` (single target) |
| Cap per scan | none | `MAX_SIGNALS_PER_SCAN = 5` |
| Signal cutoff | none | `INTRADAY_SIGNAL_CUTOFF = 15:20` |
| **Square-off** | **none** | **15:20 — PENDING → `CANCELLED`, ACTIVE → `EXPIRED`** (`live_signal_service.update_signal_outcomes:68-73`) |
| Monitoring | 1× daily (15:25) | Every 15 min via `run_periodic_scanners`; UI polls prices every 1 s |
| Stale guard | none needed (multi-day by design) | Cancels all previous-day PENDING/ACTIVE at scan start |
| Scan cooldown | 1 scan/day + 600 s lock | 5-minute cache lock `intraday_last_full_scan` |
| Frontend | `ProSystem.jsx`, manual refresh | `LiveSignalsTable.jsx`, 5 min signal refresh + 1 s price poll |

`live_signal_service.update_signal_outcomes()` explicitly excludes
`category__in=['specialist','long_term','option_buying']` and operates only on `SignalHistory`
— it **never touches `ShortTermSignal`**. The two systems share the broker session, the cache,
and the scheduler, and nothing else.

---

# APPENDIX B — Dead / Divergent Code (factual inventory)

Recorded so a new engineer does not mistake any of it for live behaviour. Each item was
verified by grepping the whole non-venv tree for call sites.

| Item | File:line | Status |
|---|---|---|
| `pro_system_service.update_pro_system_outcomes()` | `pro_system_service.py:604` | **No callers.** Was removed from `run_periodic_scanners` because it duplicated `trade_engine`'s exits and double-sent Telegram alerts (comment at `live_signal_service.py:169-175`). Contains the *only* implementation of the bullish-candle activation confirmation and the only code that writes `HIT_TARGET`/`HIT_SL` to `ShortTermSignal.status`. |
| `pro_system_service.scan_short_term_stocks()` | `pro_system_service.py:256` | Reachable only via `get_pro_system_data(trigger_scan=True)`, itself reachable only via `cron-trigger?action=short_term_scan`. A **parallel, older scanner**: top 40 candidates (not 50), `change_pct > 1.0` (not 0.5), single target at 2.5R (not 2/3/4R), returns `results[:10]`, and creates rows as **`ACTIVE` with `activated_at=now`** — bypassing the PENDING/pullback stage entirely. |
| `trade_engine.run_intraday_check()` | `trade_engine.py:550` | Not registered in `updater.py`. Only via `cron-trigger?action=trade_intraday`. It is the only code that checks SL/target intraday, and it writes `"HIT_TARGET"`/`"HIT_SL"` as **plain strings** into `_exit_signal(status=…)`. It also calls `_exit_signal` with **4 positional args**, but `_exit_signal` requires 5 (`sig, exit_price, status, exit_reason, details`) — `trade_engine.py:610` and `:615` pass `(sig, price, "HIT_TARGET", "Target hit intraday")`. This raises `TypeError: _exit_signal() missing 1 required positional argument: 'details'`. |
| `TradeScanner`, `Trade`, `StockDailyData`, `SignalChangeLog` models | `models.py` | Tables exist; **zero** `.objects.` usage anywhere. |
| `Stock`, `IntradaySignal` models | `models.py` | Read by `insights/services/ai_insight_service.py`; **never written** (bhavcopy upsert disabled at `bhavcopy_service.py:280-283`). |
| `Notification` model writes | — | `Notification.objects.create` appears nowhere. The bell is always empty. |
| `fundamental_score` | `trade_engine.py:277-278` | Constant `10.0`, returned, never summed into `ai_score`. |
| `expiry_days` | `trade_engine.py:304` | Computed, returned, never persisted. |
| `strategy_config.json` keys `nifty_500_prefilter_pct`, `nifty_500_prefilter_volume`, `top_candidates_count` | config | Defined but **not read** — the scanner hardcodes 0.5 / 50000 / 50. |
| `min_risk_reward_ratio` check | `trade_engine.py:252` | Unreachable rejection: `rr_ratio` is always exactly `target1_risk_reward` (2.0). |
| `ShortTermSignal.Status.CLOSED / CANCELLED / COOLDOWN` | `models.py` | Never assigned. |
| `ThreadPoolExecutor` import | `pro_system_service.py:14` | Imported, never used. |
| `trade_engine.py` module docstring | lines 9-17 | Describes a 10-minute intraday job and a `StockDailyData → TradeScanner → Trade` flow. Neither matches the running system. |
| `_send_telegram_scanner_summary` side effect | `trade_engine.py:1234` | A Telegram **formatter** runs the entire long-term Nifty 500 scan and persists `SignalHistory` rows. |
| `get_dashboard_data` win-rate | `trade_engine.py:1417-1420` | Counts `status=HIT_TARGET/HIT_SL`, which `_exit_signal` never leaves on the row → always 0.0 %. |
| Position sizing disagreement | `ProSystem.jsx:106` vs `pro_system_service.py:809` | 1.5 % of user capital vs a fixed ₹5,000. |

---

*Generated by reading the code at commit `bb7c962` plus the uncommitted working-tree changes.
Every claim above is traceable to a file and line. Where the implementation is silent, this
document says so rather than guessing.*
