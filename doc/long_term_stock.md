# TradePulse AI — Long-Term Stock System

**Reverse-engineered from source. Code is the only source of truth.**

Everything below is derived directly from the files listed in "Source Files". Where the
implementation does not contain something the reader might expect (fundamentals, sector
classification, position sizing, exit automation), this document says so explicitly rather
than inventing it.

---

## SCOPE OF THIS DOCUMENT

This document covers exactly one of TradePulse AI's six signal categories:

| Category | Storage | Holding period | Documented in |
|---|---|---|---|
| **Long-term (this doc)** | `SignalHistory` rows with `category="long_term"` | **1–2 years** (stated hold rule) | **this file** |
| Short-term swing | `ShortTermSignal` (own table) | 15–90 days | `doc/short_term_stock.md` |
| Intraday | `SignalHistory(category="intraday")` | same day | `doc/INTRADAY_BUY_SELL_LOGIC.md` |
| Option selling / specialist | `SignalHistory(category="specialist")` | intraday–weekly | `delta_hedge_service.py` (not covered) |
| Option buying | `SignalHistory(category="option_buying")` | intraday | `option_buying_service.py` (not covered) |
| Option sniper | `SignalHistory(category="option_selling")` | intraday | not covered |

### Read this first — three defining facts

1. **The long-term system has no scheduler job of its own.** It executes as a *side effect
   of formatting the short-term scanner's Telegram message*
   (`trade_engine._send_telegram_scanner_summary` → `_scan_new_long_term_setups` →
   `pro_system_service.scan_long_term_stocks`). See Section 17.
2. **Long-term positions are never closed automatically.** The one function that could close
   them (`pro_system_service.update_pro_system_outcomes`) has **zero callers** in the current
   codebase, and `live_signal_service.update_signal_outcomes` explicitly excludes
   `category='long_term'`. Once a row is `ACTIVE` it stays `ACTIVE` indefinitely. See Sections
   11, 12, 15.
3. **There is no fundamental analysis, despite the code returning ROE, debt-to-equity and
   growth figures.** Those are hardcoded literals labelled *"Quality proxy defaults"*. See
   Section 7.

### Source Files

| File | Role |
|---|---|
| `backend/stocks/services/pro_system_service.py` | **The engine.** `scan_long_term_stocks()`, `_fetch_long_term_quality()`, `get_market_direction()`, `update_pro_system_outcomes()` (dead), `get_pro_performance_report()` |
| `backend/stocks/services/trade_engine.py` | `_scan_new_long_term_setups()` (the live persistence path), `_get_active_long_term_holdings_lines()`, `get_dashboard_data()['long_term']` |
| `backend/stocks/services/bhavcopy_service.py` | `_fetch_nifty500_symbols()`, `_get_session()` — universe |
| `backend/stocks/services/angel_one_service.py` | Broker/data integration, rate limits, circuit breaker |
| `backend/stocks/services/live_signal_service.py` | `get_latest_prices()`, `get_performance_report()`, and the **exclusion** of `long_term` from the outcome auditor |
| `backend/stocks/services/telegram_service.py` | Alert delivery |
| `backend/stocks/models.py` | `SignalHistory` |
| `backend/stocks/views.py` | `ProSystemView`, `ProPerformanceReportView`, `DashboardSummaryView`, `PerformanceReportView` |
| `frontend/src/pages/ProSystem.jsx` | UI (`?view=long_term`) |

---

# SECTION 1 — Complete System Architecture

## 1.1 Frontend

React 18 + Vite + TailwindCSS.

| Surface | Route / component | Endpoint | Refresh |
|---|---|---|---|
| Long-term tab view | `pages/ProSystem.jsx` with `?view=long_term` | `GET /api/stocks/pro-system/` | **Once per mount** — `fetchData` is memoised on `[]` and there is no `setInterval` on this page |
| Dashboard preview card | `pages/Dashboard.jsx` | `GET /api/stocks/dashboard-summary/` | Once on mount |
| Performance report | `pages/PerformanceReports.jsx` | `GET /api/stocks/pro-performance-report/` and `GET /api/stocks/performance-report/` | `setInterval(fetchReport, 60000)` |

Long-term tab vocabulary (`ProSystem.jsx:64-71`, `LT_TAB_CONFIG`) is deliberately separate from
the short-term one because `SignalHistory` has a 6-state status enum vs `ShortTermSignal`'s 14:
`pending, active, hit_target, hit_sl, expired, cancelled`.

Long-term table columns (`ProSystem.jsx:293-346`): Stock, Reason, Status, Entry, Current, P&L,
SL / Target, Generated. Note there is **no AI score, no setup label, no peak/drawdown, no
holding-days column** — those fields do not exist on `SignalHistory`.

## 1.2 Backend

Django 4.2 + DRF, Python 3.10. All long-term endpoints require JWT (`IsAuthenticated`).

## 1.3 Database

PostgreSQL. **One model: `SignalHistory`** (table `signal_history`), filtered on
`category="long_term"`.

Fields actually populated for long-term rows:

| Field | Value written |
|---|---|
| `symbol` | e.g. `"RELIANCE"` |
| `category` | `"long_term"` |
| `signal_type` | `"BUY PIP"` — a fixed string (comment in code: `# SIP Strategy`) |
| `entry_price` | last daily close at scan time |
| `stop_loss` | **differs by path** — see Section 11 |
| `target` | **differs by path** — see Section 12 |
| `reason` | `f"Top Sector Leader ({sector})"` where `sector` is always the literal `"Large Cap"` |
| `status` | `ACTIVE` (immediately — never `PENDING`) |
| `generated_at` | `auto_now_add` |

Fields left null/default for long-term rows: `rr`, `strike_price`, `option_type`,
`premium_cmp`, `option_expiry`, `active_time`, `exit_price`, `exit_time`, `metadata` (`{}`),
and all six `whatsapp_*` / `telegram_*` boolean flags.

**Unique constraint** (`models.py:46-52`):
```python
UniqueConstraint(fields=['symbol','category','status'],
                 condition=Q(status__in=['PENDING','ACTIVE']),
                 name='unique_live_signal')
```
This permits **at most one live long-term row per symbol**.

## 1.4 Cache

`FileBasedCache` at `DJANGO_CACHE_DIR` or `BASE_DIR/django_cache` (`settings.py:24-29`).
Relevant keys: `trade_engine_dashboard_30s` (30 s), `dashboard_summary_20s` (20 s),
`candle_cache_*` (240 s), `orchestrator_price_*` (5 s).

## 1.5 Scheduler

APScheduler, `Asia/Kolkata`, started from `stocks/apps.py::ready()` → `stocks/updater.py::start()`.

**No job in `updater.py` mentions long-term.** Full analysis in Section 17.

## 1.6 Broker

Angel One SmartAPI via `AngelOneService` singleton (`get_angel_one_instance()`).
Auth = client code + password + TOTP (`pyotp`), public IP detected at login and sent as
`X-ClientPublicIP`, session reused 18 h. Identical to the short-term system — see
`doc/short_term_stock.md` §1.6.

## 1.7 Data Providers

| Need | Provider | Call |
|---|---|---|
| Universe | `IndexConstituent(NIFTY500)` → NSE archive CSV → `NIFTY500_FALLBACK` | `_fetch_nifty500_symbols()` |
| Symbol → token | Angel One instrument master (6 h TTL) | `get_token_map()` |
| Liquidity prefilter | Angel One `/market/v1/quote/` mode `FULL`, chunks of 50 | `get_bulk_quotes()` |
| Daily candles | Angel One `/historical/v1/getCandleData`, `ONE_DAY`, **200 calendar days** | `get_candle_data()` |
| Live price for P&L | `MarketDataOrchestrator.get_price()` / `live_signal_service.get_latest_prices()` (WebSocket-first, REST fallback) | — |
| Market direction | Angel One token `99926000` (Nifty 50), 120 d daily | `get_market_direction()` |

**No fundamental data provider of any kind.** No screener API, no Tijori/Screener.in/
Trendlyne integration, no XBRL, no NSE corporate-filings feed.

## 1.8 APIs

| Method + Path | View | Long-term content |
|---|---|---|
| `GET /api/stocks/pro-system/` | `ProSystemView` | `data['long_term']['tabs']` (6 tabs) + `data['long_term']['analytics']` |
| `GET /api/stocks/dashboard-summary/` | `DashboardSummaryView` | `data['long_term']` = count + top 3 rows, DB-read only |
| `GET /api/stocks/pro-performance-report/?date=` | `ProPerformanceReportView` | `data['long_term']` aggregate (`_aggregate_lt`) |
| `GET /api/stocks/performance-report/?date=` | `PerformanceReportView` | `long_term_history` — rows generated on that date |
| `GET /api/stocks/cron-trigger/?action=short_term_scan` | `CronScannerTriggerView` | Triggers the **legacy** persistence path (Section 17.3) |

## 1.9 Signal Engine

`pro_system_service._fetch_long_term_quality(symbol, svc)` — a **single** hard filter
(`close > EMA50 > EMA200`) plus derived levels. There is no scoring function, no gate stack,
no threshold set. Section 5.

## 1.10 Trading Engine

There is no long-term trading engine. There is:
- a **scan** (`scan_long_term_stocks`),
- **two different persistence writers** (Section 17.3),
- **no activation logic** (rows are born `ACTIVE`),
- **no live exit logic** (the only implementation is dead code).

## 1.11 Notification System

Telegram only, and only as **sections embedded in the short-term scanner's daily message** —
long-term never sends a message of its own.

Two blocks, both built in `trade_engine.py`:

1. `_get_active_long_term_holdings_lines()` → `🏦 ACTIVE LONG-TERM HOLDINGS`
   — per row: symbol, entry, live CMP, P&L as `+X.XX% (+₹N)`.
2. The `📈 NEW LONG-TERM SETUPS` block built inline in `_send_telegram_scanner_summary`
   (lines 1243-1254) — per pick: index, symbol, `(sector)`, price, `entry_plan`, `hold_rule`.
   If empty, emits `📈 LONG-TERM\nNo new long-term setups found today.`

Both are appended to `DAILY_SCANNER_SUMMARY` or `SCANNER_NO_SETUPS`, delivered via the
`TelegramLog` queue to the short-term chat id.

**No long-term entry alert, no exit alert, no target/SL alert exists** — because no entry or
exit event ever fires.

## 1.12 AI Components

**None applied to long-term signal generation.** The long-term picker uses no scoring model at
all — not even the deterministic `_compute_ai_score` used by the short-term engine.
`SignalHistory` has no `ai_score` field. The Claude integration
(`insights/services/ai_insight_service.py`, model `claude-sonnet-4-20250514`) writes a daily
narrative and has no connection to this pipeline.

## 1.13 Portfolio Engine

No server-side portfolio engine. Two independent, disagreeing sizing conventions exist for
display:

| Where | Rule |
|---|---|
| `trade_engine.LONG_TERM_ASSUMED_CAPITAL_PER_POSITION = 100_000` (`trade_engine.py:107`) | `qty = ₹1,00,000 // entry_price` per position — used **only** for the ₹ figure in the Telegram holdings block |
| `ProSystem.jsx:109-110` | `ltCapitalPerStock = capital / active_count`; `ltQty = floor(ltCapitalPerStock / entry)` — equal-weight split of user-entered capital (default ₹5,00,000) across active LT holdings |
| `pro_system_service._aggregate_lt` (`:854-859`) | `qty = max(1, 5000 // |entry − stop_loss|)`, or a flat `100` if `stop_loss is None` |

Three surfaces, three different quantities for the same position.

---

# SECTION 2 — Daily Workflow

All times IST, Mon–Fri.

## Market Closed

Nothing long-term-specific runs. APScheduler stays up for the 1-minute Telegram dispatcher and
user cleanup. On a holiday boot, `apps.py::ready()` calls `is_static_closed("NSE")` and skips
Angel One initialisation entirely.

## Pre-Market — 09:05

`run_premarket_update()` → `get_market_direction()`. **Logged only, not persisted, and the
long-term scan does not read it.** The BEARISH gate belongs to the short-term scanner
(`_run_daily_scanner_impl`), not to `scan_long_term_stocks()`.

## 09:15 — Market Open

Nothing.

## First Scan — 10:00, as a side effect

There is no long-term scan job. The chain is:

```
10:00  updater.run_short_term_scan()
         └─ trade_engine.run_daily_scanner()
              └─ _run_daily_scanner_impl(...)                    [short-term work]
                   └─ Step 8: _send_telegram_scanner_summary()   [formatting the message]
                        └─ _scan_new_long_term_setups()          ◄── THE LONG-TERM SCAN
                             └─ pro_system_service.scan_long_term_stocks()
```

`_send_telegram_scanner_summary` is only called when `send_telegram=True`. Recall
`run_daily_scanner`'s two-pass design (`trade_engine.py:349-360`):

```python
result = _run_daily_scanner_impl(relaxed=False, send_telegram=False)   # strict, NO telegram
if result:
    return result                                                       # ← long-term never runs
logger.info("Strict scan found no setups — falling back to relaxed mode")
return _run_daily_scanner_impl(relaxed=True)                            # relaxed, telegram=True
```

**Therefore: on any day the strict short-term pass finds at least one setup, the long-term
scan does not run at all.** The long-term engine only executes on days the strict short-term
scan comes up empty — including days it aborts early because the market is BEARISH.

## Second Scan

None.

## Signal Generation

Inside `_scan_new_long_term_setups()`. Detail in Sections 5 and 9.

## Trade Monitoring

**None.** `check_pending_activations()` queries `ShortTermSignal` only. `run_eod_evaluation()`
queries `ShortTermSignal` only. `run_periodic_scanners()` → `update_signal_outcomes()`
explicitly excludes long-term:

```python
active_signals = SignalHistory.objects.filter(
    status__in=[ACTIVE, PENDING]
).exclude(category__in=['specialist', 'long_term', 'option_buying'])
```
with the comment: *"long_term is a 1-2 year buy-and-hold with no target/SL — it must never be
auto-closed by this same-day price check"* (`live_signal_service.py:47-50`).

Long-term prices are refreshed **only when a UI request or a Telegram formatter asks for them**:
- `get_dashboard_data()` → `live_signal_service.get_latest_prices(active_lt_symbols)` (bulk)
- `_get_active_long_term_holdings_lines()` → `orch.get_price(sym)` per symbol
- `get_performance_report()` → `get_latest_prices` for ACTIVE rows

None of these writes anything back to the DB. `SignalHistory` has no `current_price` column.

## Target Monitoring / Stop Monitoring

**Neither runs.** The only code that compares a long-term row's live price against its
`target`/`stop_loss` is `pro_system_service.update_pro_system_outcomes()` (lines 612-648),
which has no callers. Section 11.6 gives the full evidence.

## Market Close — 15:30

Nothing.

## Square Off

Not applicable — a 1–2 year hold has no square-off. There is also no EOD flatten, no
expiry, and no maximum holding enforcement.

## End of Day — 15:25 / 15:35 / 16:30

- 15:25 `run_eod_evaluation()` — short-term only.
- 15:35 `send_short_term_status_update()` — short-term only; it does **not** include the
  long-term block (that block exists only in `_send_telegram_scanner_summary`).
- 16:30 `run_daily_market_update` — global market data + market bias + Claude insight +
  option-chain snapshots. Unrelated.

**Net effect: on a normal trading day, a long-term position generates zero scheduled activity.**

---

# SECTION 3 — Stock Universe

## 3.1 Where stocks come from

`scan_long_term_stocks()` (`pro_system_service.py:420-427`):

```python
try:
    session = _get_session()
    symbols = list(_fetch_nifty500_symbols(session))
except Exception:
    symbols = []
if not symbols:
    symbols = NIFTY500_FALLBACK
```

Identical resolution order to the short-term engine:
1. **`IndexConstituent` DB table** — `filter(index_name='NIFTY500', is_active=True)`. Returned
   immediately if non-empty. **Primary source.**
2. **NSE archive CSV** — `https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv`,
   `Symbol` column only, 15 s timeout, cookie-bootstrapped session with a Chrome UA.
3. **`NIFTY500_FALLBACK`** — hardcoded ~180-symbol list at `pro_system_service.py:32-77`.
   Static snapshot; contains stale/invalid symbols (`HDFC` — merged; `AAPL`; `RCOM`; `MTNL`;
   `TVSMOTORS`). Unresolvable symbols are silently dropped by `get_token_map()`.

> The docstring on `scan_long_term_stocks` says *"Scan Nifty 500 for Long Term quality stocks"*,
> which is correct. The docstring on `_scan_new_long_term_setups` in `trade_engine.py:1185` says
> *"Scan **Nifty 50** for long-term quality setups"* — **that is wrong**; it calls
> `scan_long_term_stocks()`, which scans Nifty 500.

## 3.2 NIFTY100 / NIFTY200 / NIFTY500 / Custom / Live NSE

| Tier | Used by long-term? |
|---|---|
| NIFTY50 | No (used by the strangle scanner and WebSocket bootstrap) |
| NIFTY100 | No (intraday engine) |
| NIFTY200 | Not implemented anywhere in the codebase |
| **NIFTY500** | **Yes** |
| Custom list | `NIFTY500_FALLBACK` only, as a fallback |
| Database | `IndexConstituent` (primary) |
| Live NSE | Archive CSV (secondary) |

## 3.3 Sector Lists

**None exist.** This is important because every long-term row is stored with
`reason = "Top Sector Leader (Large Cap)"` and every pick carries `"sector": "Large Cap"`
(`pro_system_service.py:389`). There is **no sector classification, no sector index, and no
"leader within sector" comparison anywhere in the code.** The string is a hardcoded literal.

`get_pro_system_data`'s UI formatter then parses it back out
(`l.reason.replace("Top Sector Leader (", "").replace(")", "")`, line 568), so the UI displays
"Large Cap" as if it were a resolved sector.

## 3.4 Liquidity Filters

One, at the prefilter stage (`pro_system_service.py:450`):
```python
if vol > 50000 and change_pct > -5.0:
```
- `vol` = `trade_volume` from the `FULL` bulk quote.
- The `change_pct > -5.0` clause is described in-code as excluding stocks *"in an acute
  single-day crash"*.

There is **no** minimum-volume floor at the candle stage (unlike the short-term engine's
100 k `df['Volume'].iloc[-1]` check), no bid-ask spread check, no impact cost, no delivery
percentage.

## 3.5 Average Volume Filters

**None as a filter.** `vol_20d = df['Volume'].iloc[-20:].mean()` is computed only to build the
`mcap` proxy (Section 3.7). Nothing is rejected on volume expansion or contraction.

## 3.6 Price Filters

- Prefilter: `change_percent > -5.0`.
- Structural: `close > EMA50 > EMA200`.

No absolute price floor or ceiling.

## 3.7 Market Cap Filters

**No real market-cap data.** `_fetch_long_term_quality` returns:
```python
"mcap": int(close * vol_20d),   # Proxy market weight metric
```
This is price × average daily volume — a **turnover** figure, not market capitalisation
(shares outstanding is never fetched). It is returned in the dict, is **never filtered on**,
is never persisted, and `get_pro_system_data`'s UI formatter overwrites it with `0`
(`pro_system_service.py:569`, comment: *"MCAP query avoided for speed"*).

## 3.8 Blacklist

No explicit blacklist. Two effective exclusions:
1. `_scan_new_long_term_setups` filters out symbols already having a `long_term` row with
   `status=ACTIVE`.
2. `get_token_map()` silently drops symbols with no Angel One instrument-master match.

## 3.9 Whitelist

None. `NIFTY500_FALLBACK` is a fallback.

## 3.10 Candidate cap

Two hard truncations:
```python
candidates.sort(key=lambda x: x["volume"], reverse=True)
top_candidates = [c["symbol"] for c in candidates[:75]]     # 75 sent to the EMA check
...
results.sort(key=lambda x: x['revenue_growth'], reverse=True)
return results[:5]                                          # 5 returned
```
The 75-candidate cap exists because each `_fetch_long_term_quality` call issues **two** Angel
One requests (a per-symbol `get_token_map`, then a 200-day candle fetch), and candles are
globally rate-limited to ~1/s.

**Maximum 5 long-term picks per scan.** Combined with the ACTIVE-symbol filter, that is the
hard ceiling on new positions per run.

---

# SECTION 4 — Market Filters

## 4.1 Market regime

**The long-term scan applies no market filter at all.**

`scan_long_term_stocks()` does not call `get_market_direction()`. It does not check
BULLISH/BEARISH, does not check `is_market_open()`, and does not check the holiday calendar.
It runs whenever its caller runs.

The BEARISH abort at `_run_daily_scanner_impl:385-389` belongs to the **short-term** scan. And
because that abort returns `[]`, it *triggers* the relaxed pass, which *does* send Telegram —
which *does* run the long-term scan. So a BEARISH market makes the long-term scan **more**
likely to run, not less.

## 4.2 NIFTY Trend

Not consulted by the long-term scan. (`get_market_direction()` is called by
`get_pro_system_data()` and `get_dashboard_data()` purely to populate the
`market_direction` block for the UI header.)

## 4.3 Sector Trend

**Not implemented.** See Section 3.3.

## 4.4 Breadth

**Not implemented.** No advance/decline, no % above 200-DMA.

## 4.5 VIX

**Not implemented.** `INDIAVIX` appears nowhere in the codebase.

## 4.6 Gap Up / Gap Down

**Not implemented.**

## 4.7 Bull / Bear / Sideways

Not evaluated for long-term.

## 4.8 Volatility

Only ATR(14), and only to set the stop distance (Section 11.1). No volatility rejection.

## 4.9 Holiday Detection

The infrastructure exists in `signal_utils.py` — weekend guard, NSE `holiday-master` API sync
into `MarketHoliday`, static `NSE_HOLIDAYS` (2025–2027), 60 s result cache — but the long-term
scan never calls it. Its indirect protection is that its caller
(`updater.run_short_term_scan`) is registered with `day_of_week="mon-fri"`. **A weekday NSE
holiday will still trigger the whole chain, including the long-term scan.**

## 4.10 Market Open Detection

Same: `get_market_status("NSE")` / `is_market_open()` are never called on this path. The only
holiday gate is in `CronScannerTriggerView` (manual triggers) and `apps.py::ready()`.

---

# SECTION 5 — Stock Selection Pipeline

Two functions in sequence. Every rejection is listed.

## Part A — `trade_engine._scan_new_long_term_setups()` (the caller)

### Step A0 — Wrapper
Entire body is inside `try/except Exception` → returns `[]` on any failure, logging
`"[TRADE_ENGINE] Long-term scan for scanner report failed: %s"`.

### Step A1 — Load already-tracked symbols
```python
already_active = set(SignalHistory.objects
                       .filter(category="long_term", status=ACTIVE)
                       .values_list("symbol", flat=True))
```

### Step A2 — Run the scan
`picks = scan_long_term_stocks()` → Part B.

### Step A3 — Deduplicate
```python
new_picks = [p for p in picks if p["symbol"] not in already_active]
```
❌ **REJECT**: symbol already has an ACTIVE long-term row.

### Step A4 — Persist
For each `p` in `new_picks`:
```python
SignalHistory.objects.update_or_create(
    symbol=p["symbol"],
    category="long_term",
    defaults={
        "signal_type": "BUY PIP",
        "entry_price": p["price"],
        "stop_loss":   float(p["price"]) * 0.5,     # ← 50 % below entry
        "target":      float(p["price"]) * 2.0,     # ← 100 % above entry
        "reason":      f"Top Sector Leader ({p['sector']})",
        "status":      SignalHistory.Status.ACTIVE,
    },
)
```

⚠️ **The persisted SL/target are NOT the ATR-derived values computed by
`_fetch_long_term_quality`.** That function returns `stop_loss` (3×ATR, 15 % floor) and
`target` (2.5R) in its dict, and this writer **ignores both** in favour of the flat ×0.5 / ×2.0
multipliers. The ATR levels survive only in the other, non-scheduled persistence path
(Section 17.3).

⚠️ **`update_or_create(symbol=…, category=…)` is not status-scoped.** If a symbol accumulates
more than one `long_term` row over time (e.g. one `HIT_TARGET` from an old run plus a new one),
the lookup matches multiple rows and Django's `update_or_create` raises
`MultipleObjectsReturned`. The enclosing `try/except` swallows it and the whole long-term block
returns `[]`. *(Observation from reading the code — not verified against a live database.)*

## Part B — `pro_system_service.scan_long_term_stocks()`

### Step B1 — Broker session
`svc = get_angel_one_instance()`.
❌ **REJECT ALL**: `svc is None` → log error, `return []`.

### Step B2 — Universe
Section 3.1.

### Step B3 — Token resolution
`token_map = svc.get_token_map(symbols, exchange="NSE")`.
❌ **REJECT (symbol)**: no instrument-master match for `symbol` or `f"{symbol}-EQ"`.
❌ **REJECT ALL**: `token_map` empty → `return []`.

### Step B4 — Bulk quote sweep
Tokens in chunks of 50, `svc.get_bulk_quotes({"NSE": chunk}, mode="FULL")`. Chunk exceptions
logged and skipped. Circuit breaker may return partial results.

### Step B5 — Liquidity prefilter
❌ **REJECT**: no quote at `f"NSE:{tok}"`.
❌ **REJECT**: `trade_volume <= 50_000`.
❌ **REJECT**: `change_percent <= -5.0`.

### Step B6 — Rank and truncate
`candidates.sort(key=lambda x: x["volume"], reverse=True)` → `[:75]`.
❌ **REJECT**: rank > 75 **by raw daily volume**.

> This is the single most consequential selection rule in the long-term engine. The 75
> survivors are simply the 75 highest-traded-share-count names of the day. High-priced
> large caps (which trade fewer *shares* for the same rupee turnover) are systematically
> disadvantaged versus low-priced high-volume names.

### Step B7 — Per-candidate quality check
`time.sleep(0.35)` then `_fetch_long_term_quality(sym, svc)`:

```python
token_map = svc.get_token_map([symbol], exchange="NSE")   # second lookup, per symbol
token = token_map.get(symbol)
if not token: return None                                  # ❌ REJECT

from_date = now − 200 days ;  to_date = now
df = svc.get_candle_data(token, "NSE", "ONE_DAY", from_date, to_date)
if df.empty or len(df) < 100: return None                  # ❌ REJECT

close  = df['Close'].iloc[-1]
ema50  = Close.ewm(span=50,  adjust=False).mean().iloc[-1]
ema200 = Close.ewm(span=200, adjust=False).mean().iloc[-1]

if not (close > ema50 > ema200): return None               # ❌ REJECT — the ONLY filter
```

Everything is wrapped in `except Exception: return None` — any error is an unlogged silent
rejection.

> **Data-sufficiency note:** a 200-*calendar*-day window yields roughly 135–140 trading days.
> The guard only requires `len(df) >= 100`. A 200-period EMA computed on ~137 bars with
> `adjust=False` is seeded from the first bar and has not converged; it is materially
> different from a true EMA200. The short-term engine, computing the same EMA200, requires
> `len(df) >= 201` from a 365-day window. **The long-term engine's 200 EMA is the least
> reliable indicator in the codebase.**

### Step B8 — Derived values (no further filtering)
```python
perf_100d = ((close − df['Close'].iloc[-100]) / df['Close'].iloc[-100]) * 100
vol_20d   = df['Volume'].iloc[-20:].mean()
atr14     = _compute_atr(df, period=14).iloc[-1]

stop_loss    = close − (3 * atr14)
max_sl_floor = close * 0.85
if stop_loss < max_sl_floor: stop_loss = max_sl_floor
sl_points = close − stop_loss
target    = close + (2.5 * sl_points)
```
No sanity check that `sl_points > 0`, no R:R minimum, no rejection here at all.

### Step B9 — Rank and cap
```python
results.sort(key=lambda x: x['revenue_growth'], reverse=True)
return results[:5]
```
`revenue_growth` is `round(perf_100d, 1)` — **100-day price performance**, not revenue.
Section 7.

❌ **REJECT**: rank > 5.

## Rejection summary

| # | Stage | Condition | Logged? |
|---|---|---|---|
| 1 | Broker | `svc is None` | Yes (error) |
| 2 | Universe | empty after all 3 sources | Yes (warning) |
| 3 | Token | no instrument-master match | No |
| 4 | Prefilter | no quote returned | No |
| 5 | Prefilter | `trade_volume <= 50,000` | No |
| 6 | Prefilter | `change_percent <= −5.0` | No |
| 7 | Cap | not in the top 75 by share volume | Count logged |
| 8 | Quality | per-symbol token lookup fails | No |
| 9 | Quality | `df.empty or len(df) < 100` | No |
| 10 | Quality | `not (close > ema50 > ema200)` | No |
| 11 | Quality | any exception | **No — silently swallowed** |
| 12 | Cap | not in the top 5 by 100-day performance | Count logged in caller |
| 13 | Dedup | already has an ACTIVE long-term row | Count logged in caller |

**Nine of thirteen rejection reasons produce no log line.** There is no rejected-candidates
table and no audit trail; the only way to learn why a stock was not picked is to re-run the scan.

---

# SECTION 6 — Technical Analysis

The long-term engine uses **three** indicators. Two are filters/levels; one is a ranking key.

## 6.1 EMA 50 / EMA 200 — `_get_ema(series, window)`

```python
series.ewm(span=window, adjust=False).mean()
```
- **Purpose:** the sole structural-uptrend gate.
- **Parameters:** spans 50 and 200, on 200 calendar days of `ONE_DAY` closes.
- **Threshold:** `close > ema50 > ema200`. Binary.
- **BUY influence:** absolute — failing it is the only technical rejection.
- **SELL influence:** the `hold_rule` string says *"Exit early if trend breaks below 200 EMA"*.
  **That rule is not implemented in code.** No function ever re-checks the EMA200 for a
  long-term holding. It is documentation shipped inside a data payload.
- **Weight:** no score exists; as a gate its weight is total.
- **Caveat:** see the data-sufficiency note in Section 5, Step B7.

## 6.2 ATR(14) — `_compute_atr(df, period=14)`

```python
tr  = max(High−Low, |High−PrevClose|, |Low−PrevClose|)
atr = tr.rolling(window=14).mean()      # simple rolling mean, NOT Wilder's
```
- **Purpose:** stop distance only.
- **Threshold:** none.
- **BUY influence:** none (never rejects).
- **Level influence:** `stop_loss = close − 3×ATR`, floored at `close × 0.85`;
  `target = close + 2.5 × sl_points`.
- **Weight:** 0 in ranking.
- **Multiplier rationale, quoted from `pro_system_service.py:376-379`:**
  *"wider than the short-term ATR stop since this is a position trade… 3xATR stop, capped at
  15 % max risk (vs short-term's 10 %) to give large-cap swings room; target at 2.5x the SL
  distance, same R:R convention as the short-term engine."*
- **⚠️ These levels are discarded by the live persistence path** (Section 5, Step A4).

## 6.3 100-Day Price Performance

```python
perf_100d = ((close − df['Close'].iloc[-100]) / df['Close'].iloc[-100]) * 100
```
- **Purpose:** the **only** ranking key, and the only differentiator between the 5 picks and
  everything else that passed the EMA gate.
- **Threshold:** none — it never rejects, only orders.
- **Stored as:** `result['revenue_growth']` — a field name that has nothing to do with revenue.
- **Weight:** 100 % of the ranking.

## 6.4 20-Day Average Volume

```python
vol_20d = df['Volume'].iloc[-20:].mean()
```
Used only inside the `mcap` proxy (`int(close × vol_20d)`), which is never filtered on and
never persisted. Effectively unused.

## 6.5 Indicators NOT present in the long-term engine

ADX, RSI, MACD, Bollinger Bands, Stochastic, OBV, CCI, Supertrend, Ichimoku, VWAP, Volume
Profile, 52-week-high proximity, 20-day breakout, volume-expansion ratio, relative strength
vs Nifty, EMA20, candlestick patterns, support/resistance, pivot points, multi-timeframe
confirmation.

For contrast, the short-term engine uses EMA20/50/200, ADX(14), RSI(14), ATR(14), 52w high,
20d high, volume ratio, absolute volume, and RS-vs-Nifty — nine inputs across six gates.
The long-term engine uses **one gate**.

## 6.6 Summary table

| Indicator | Formula | Purpose | Threshold | BUY | SELL | Weight |
|---|---|---|---|---|---|---|
| EMA50 / EMA200 | `ewm(span, adjust=False)` | Structural uptrend | `close > ema50 > ema200` | Hard gate | Documented in `hold_rule`, **not coded** | Total (as gate) |
| ATR(14) | `TR.rolling(14).mean()` | Stop distance | none | none | none | 0 |
| 100-day performance | `(C[-1]−C[-100])/C[-100]×100` | Ranking | none | Orders the top 5 | none | 100 % of rank |
| 20-day avg volume | `Volume[-20:].mean()` | `mcap` proxy | none | none | none | 0 |

---

# SECTION 7 — Fundamental Analysis

## **There is NO fundamental analysis in the long-term system.**

This is the most important thing in this document, because the long-term engine **returns
fundamental-looking numbers that are hardcoded constants**, and those numbers reach the UI.

### The literal code (`pro_system_service.py:388-401`)

```python
return {
    "symbol": symbol,
    "sector": "Large Cap",                          # hardcoded string
    "mcap": int(close * vol_20d),                   # price × avg volume — a turnover proxy
    "roe": 22.5,                                    # Quality proxy defaults
    "debt_to_equity": 0.2,                          # Quality proxy defaults
    "revenue_growth": round(float(perf_100d), 1),   # ← 100-DAY PRICE RETURN
    "profit_growth": 15.0,                          # Quality proxy defaults
    "price": round(float(close), 2),
    "stop_loss": round(float(stop_loss), 2),
    "target": round(float(target), 2),
    "entry_plan": "Buy 30% Now, 30% on 10% dip, 40% on major correction",
    "hold_rule": "Hold for 1-2 years (max 2 years). Exit early if trend breaks below 200 EMA."
}
```

The comment `# Quality proxy defaults` is in the source at line 392.

### And a second, different set of constants in the UI formatter

`get_pro_system_data()` re-derives the display payload from the DB and substitutes **different
hardcoded values** (`pro_system_service.py:566-577`):

```python
formatted_lt.append({
    "symbol": l.symbol,
    "sector": l.reason.replace("Top Sector Leader (", "").replace(")", ""),
    "mcap": 0,                    # MCAP query avoided for speed
    "roe": 25.0,                  # ← 22.5 in the scanner, 25.0 here
    "debt_to_equity": 0.1,        # ← 0.2 in the scanner, 0.1 here
    "revenue_growth": 15.0,       # ← real perf_100d in the scanner, constant 15.0 here
    "profit_growth": 12.0,        # ← 15.0 in the scanner, 12.0 here
    ...
    "hold_rule": "Hold for 1-2 years (max 2 years). Exit early if ROE drops below 10% or consistent losses."
})
```

Note the second `hold_rule` cites an **ROE < 10 %** exit condition. ROE is never fetched, so
that condition can never be evaluated. Neither `hold_rule` string is machine-readable — both
are display copy.

### Factor-by-factor

| Requested factor | Status |
|---|---|
| Revenue | **Not fetched.** `revenue_growth` is 100-day price return (scanner) or the constant `15.0` (UI) |
| Profit | **Not fetched.** `profit_growth` is the constant `15.0` / `12.0` |
| EPS | Not fetched, not referenced |
| ROE | **Not fetched.** Constant `22.5` / `25.0` |
| ROCE | Not fetched, not referenced |
| Debt / D-E | **Not fetched.** Constant `0.2` / `0.1` |
| Cash Flow | Not fetched, not referenced |
| Promoter Holding | Not fetched, not referenced |
| FII (per stock) | Not fetched. `fii_dii_service.py` provides aggregate market flows to a dashboard card only |
| DII (per stock) | Same |
| Valuation (P/E, P/B, EV/EBITDA) | Not fetched, not referenced |
| Growth | Only price momentum (100-day return), labelled `revenue_growth` |
| Quality | Only the EMA50/EMA200 stack, described in-code as *"Quality Rule: Must be in structural uptrend"* |
| Momentum | Present — 100-day price return, used as the ranking key |
| Market Cap | Not fetched. `mcap` is `close × vol_20d` (turnover) and is zeroed in the UI |
| Sector | Not classified. Literal `"Large Cap"` |

### Where else this leaks

- The Telegram `📈 NEW LONG-TERM SETUPS` block prints `(p['sector'])` — always `(Large Cap)`.
- `ProSystem.jsx`'s long-term table shows the `reason` column — always
  `Top Sector Leader (Large Cap)`.
- No endpoint exposes `roe`/`debt_to_equity`/`profit_growth` today
  (`get_pro_system_data` is not wired to a URL — `ProSystemView` calls
  `trade_engine.get_dashboard_data()` instead), but the constants remain in the code and would
  surface immediately if that formatter were ever re-attached.

---

# SECTION 8 — Scoring Engine

## 8.1 How scores are calculated

**There is no scoring engine.** No composite score, no weighted factors, no sub-scores.
`SignalHistory` has no `ai_score`, `confidence`, or `rank` column, and `_fetch_long_term_quality`
returns no score field.

This is a deliberate structural difference from the short-term engine, which computes a
five-component 0–100 `ai_score`.

## 8.2 How stocks are ranked

Two ordinal passes, each a plain sort:

| Pass | Key | Direction | Cut |
|---|---|---|---|
| 1 — prefilter | `trade_volume` (today's share count) | descending | top **75** |
| 2 — final | `revenue_growth` = `perf_100d` (100-day price return) | descending | top **5** |

Nothing else influences ordering. A stock that barely clears `close > ema50 > ema200` with a
huge 100-day run outranks a stock deep in a strong uptrend with a modest run.

## 8.3 How ties are broken

**No explicit tiebreaker.** `list.sort()` is stable, so equal `perf_100d` values (which are
rounded to 1 dp, making ties plausible) retain their **volume rank** order from pass 1. This
is incidental, not designed.

## 8.4 How confidence is calculated

**No confidence value is computed or stored for long-term signals.**

`SignalHistorySerializer.get_confidence_score` computes `clamp(50 + rr×20, 50, 99)` — but
long-term rows are written with `rr = NULL`, so that expression would yield the floor value
`50.0`. In practice the serializer is never applied to long-term rows: it is used by
`_live_intraday_payload()` (intraday only). The long-term UI is fed by
`get_dashboard_data()['long_term']`, which builds its dicts by hand (`_fmt_lt`, line 1451)
and emits no confidence field at all.

## 8.5 Final ranking

`return results[:5]` in `scan_long_term_stocks`, then `_scan_new_long_term_setups` removes any
symbol already ACTIVE. So a run persists between 0 and 5 new positions. There is no re-ranking
of existing holdings, no periodic review, no rebalancing.

---

# SECTION 9 — Signal Generation

The long-term engine emits **one signal type**, and it emits it directly into the `ACTIVE` state.

## BUY (`signal_type = "BUY PIP"`)

Emitted when all of the following hold:

1. The chain in Section 2 reached `_send_telegram_scanner_summary` (i.e. the strict short-term
   pass found nothing)
2. Angel One session available
3. Symbol resolves to an NSE token
4. `trade_volume > 50,000`
5. `change_percent > −5.0`
6. Ranked in the **top 75 by share volume**
7. Per-symbol token lookup succeeds
8. `len(daily_df) >= 100` (from a 200-calendar-day window)
9. **`close > EMA50 > EMA200`**
10. Ranked in the **top 5 by 100-day price performance** among all survivors
11. No existing `SignalHistory(category="long_term", status=ACTIVE)` row for the symbol

The string `"BUY PIP"` is written verbatim into `signal_type`. The in-code comment is
`# SIP Strategy` — the intent is a staged Systematic-Investment-Plan-style accumulation, which
matches `entry_plan = "Buy 30% Now, 30% on 10% dip, 40% on major correction"`. **The tranching
is not implemented** (Section 10.4).

## SELL

**Not implemented.** Long-only. No short path, no reduce path, no trim path.

## WATCHLIST

**Not implemented.** There is no `PENDING` stage — rows are created with
`status = SignalHistory.Status.ACTIVE` in both persistence paths. `get_dashboard_data`
therefore renders a `pending` tab that is always empty, and
`DashboardSummaryView`'s long-term query filters on `status__in=[ACTIVE, PENDING]` where only
ACTIVE ever matches.

## HOLD

**No HOLD signal is emitted.** Holding is the default and only steady state: a row sits at
`ACTIVE` forever unless a human intervenes. The `hold_rule` string is the human-facing
instruction.

## REJECT

Not persisted. Nine of the thirteen rejection paths are not even logged (Section 5).

---

# SECTION 10 — Entry Logic

## 10.1 Exact entry price

```python
close = df['Close'].iloc[-1]          # last daily close in the 200-day series
"price": round(float(close), 2)
```
Persisted as `SignalHistory.entry_price`.

Because the chain runs at ~10:00–10:04 AM, Angel One's `ONE_DAY` endpoint returns the
**in-progress daily bar**, so `close` is approximately the 10:00 AM price. Nothing pins this
to the previous session's close.

## 10.2 Order type

**No orders are placed.** There is no broker order API call anywhere in the codebase — no
`placeOrder`, no `/rest/secure/angelbroking/order/`. The system is advisory. "Limit Order" and
"Market Order" are **not applicable**.

## 10.3 Confirmation

**There is none.** Unlike the short-term engine (which creates `PENDING` rows and waits for
`ltp <= entry_price`), the long-term writer sets `status = ACTIVE` at creation time. There is
no activation check, no pullback wait, no trigger, and no `active_time` timestamp
(the field stays NULL).

## 10.4 Staged entry / tranching

`entry_plan = "Buy 30% Now, 30% on 10% dip, 40% on major correction"` is a **string in a
dictionary**. No code:
- tracks tranches,
- records a filled fraction,
- detects a "10 % dip" or a "major correction",
- adjusts the average entry price.

`SignalHistory` has no quantity, tranche, or average-price field. The plan is human copy
printed in Telegram.

## 10.5 Volume Confirmation

At scan time only: `trade_volume > 50,000`. No volume check at or after entry.

## 10.6 Breakout

**Not used.** No 52-week-high proximity, no 20-day-high breakout — those belong to the
short-term engine. The long-term entry is trend-following on the EMA stack, not breakout-based.

## 10.7 Retest

Not implemented (see 10.3).

## 10.8 Entry expiry

Not applicable — there is no PENDING state to expire.

---

# SECTION 11 — Stop Loss Logic

## 11.1 ATR stop (computed)

`_fetch_long_term_quality` (`pro_system_service.py:380-386`):
```python
atr14        = _compute_atr(df, period=14).iloc[-1]
stop_loss    = close − (3 * atr14)
max_sl_floor = close * 0.85               # max 15 % risk
if stop_loss < max_sl_floor:
    stop_loss = max_sl_floor
sl_points = close − stop_loss
```
So: `entry − 3×ATR`, never worse than **−15 %**. No `sl_points > 0` sanity check.

## 11.2 The stop that is actually stored

**Depends on which writer ran.** This is the single largest divergence in the long-term system.

| Writer | Reachable how | `stop_loss` written | Implied risk |
|---|---|---|---|
| `trade_engine._scan_new_long_term_setups` (**the live path**) | 10:00 chain, whenever the strict short-term pass is empty | `float(price) * 0.5` | **−50 %** |
| `pro_system_service.get_pro_system_data(trigger_scan=True)` (legacy) | Only `cron-trigger?action=short_term_scan` | `l.get("stop_loss")` — the ATR value | −15 % max |

Both write `status=ACTIVE`. So the same symbol picked on two different days by two different
paths gets a stop 35 percentage points apart.

## 11.3 Percentage stop

Only as the two floors above: −15 % (computed) and −50 % (as persisted by the live path).

## 11.4 Swing Low / Swing High

**Not implemented.**

## 11.5 Support / Resistance

**Not implemented.** No support/resistance detection anywhere in the long-term engine.

## 11.6 Trailing Stop

**Not implemented.** No trailing logic exists for long-term positions. The `hold_rule`'s
*"Exit early if trend breaks below 200 EMA"* would be a trailing rule if it were coded; it is not.

## 11.7 Time Stop

**Not implemented.** The `hold_rule` says *"max 2 years"*. Nothing enforces it:
- `SignalHistory` has no `expected_holding_days` field (that is on `ShortTermSignal`).
- `run_expiry_cleanup` queries `ShortTermSignal` only.
- No job scans `SignalHistory(category='long_term')` for age.

A 2019-vintage long-term row would still read `ACTIVE` today.

## 11.8 Emergency Exit

**Not implemented.** No circuit-limit detection, no news halt, no gap-down override, no manual
close endpoint or admin action.

## 11.9 Is the stop ever evaluated? — No.

This is the critical finding. Three candidate auditors exist; none reaches long-term rows.

**(a) `live_signal_service.update_signal_outcomes()`** — runs every 15 min via
`run_periodic_scanners`. Explicitly excludes long-term (`live_signal_service.py:45-50`):
```python
active_signals = SignalHistory.objects.filter(
    status__in=[ACTIVE, PENDING]
).exclude(category__in=['specialist', 'long_term', 'option_buying'])
```
Comment: *"long_term is a 1-2 year buy-and-hold with no target/SL — it must never be
auto-closed by this same-day price check (see pro_system_service.py hold_rule)."*

**(b) `trade_engine.run_eod_evaluation()`** — 15:25 daily. Queries `ShortTermSignal` only.

**(c) `pro_system_service.update_pro_system_outcomes()`** — the **only** implementation that
checks long-term targets and stops:
```python
active_lt_sigs = SignalHistory.objects.filter(status=ACTIVE, category='long_term')
...
if high >= sig.target:      sig.status = HIT_TARGET; sig.exit_price = sig.target;    sig.exit_time = now
elif low <= sig.stop_loss:  sig.status = HIT_SL;     sig.exit_price = sig.stop_loss; sig.exit_time = now
```
It has **zero callers.** Verified by grep across the entire non-venv tree — the only three hits
are the definition itself and two comments explaining why it was removed:
- `live_signal_service.py:169-175` — removed from `run_periodic_scanners` because it duplicated
  `trade_engine.py`'s exit logic on `ShortTermSignal` rows and double-sent Telegram alerts.
- `pro_system_service.py:779-784` — removed from `get_pro_performance_report()` because a GET
  on the Reports page could silently fire duplicate Telegram alerts.

Both removals were made to fix **short-term** duplicate-alert bugs. Neither comment notes that
the same function was the **only** long-term exit path, so long-term auto-close was removed as
collateral damage.

`pro_system_service.py:536-541` still documents the intended behaviour:
> *"3xATR stop (capped at 15% max risk) / 2.5x R:R target — see `_fetch_long_term_quality`.
> Primary exit is still the hold_rule (trend break below 200 EMA); these levels are a hard
> backstop so `update_pro_system_outcomes` can auto-close on a target/SL hit."*

That backstop is currently inert.

**Net: a long-term position has no automated exit of any kind.** `stop_loss` and `target` are
display values in the UI and the reports; they trigger nothing.

---

# SECTION 12 — Target Logic

## 12.1 Risk / Reward (computed)

```python
target = close + (2.5 * sl_points)
```
With the ATR stop, `sl_points = min(3×ATR, 0.15×close)`, so the computed target is at most
`close × 1.375` (+37.5 %) and typically `close + 7.5×ATR`.

No minimum-R:R check exists (unlike the short-term engine's `rr_ratio >= 2.0`).

## 12.2 The target that is actually stored

Same split as the stop:

| Writer | `target` written | Implied gain |
|---|---|---|
| `_scan_new_long_term_setups` (**live**) | `float(price) * 2.0` | **+100 %** |
| `get_pro_system_data` (legacy, manual only) | ATR-derived `l.get("target")` | ≤ +37.5 % |

The live path's ×2.0 / ×0.5 pair gives a nominal R:R of exactly **2:1** (100 % up vs 50 %
down) — arithmetically consistent, but the levels are so wide they are effectively "never".

## 12.3 ATR target

Indirect only, via `sl_points` (Section 12.1), and only on the legacy path.

## 12.4 Fixed %

The live path's ×2.0 target *is* a fixed-percentage target (+100 %).

## 12.5 Resistance-based targets

**Not implemented.**

## 12.6 Multiple targets

**No.** `SignalHistory` has a single `target` column. There is no T1/T2/T3
(`ShortTermSignal.target2`/`target3` are on the other model). `get_dashboard_data`'s `_fmt_lt`
emits one `target` field; `ProSystem.jsx`'s long-term table renders one `T:` row.

## 12.7 Scaling Out / Partial Exit

**Not implemented.** No quantity is tracked, so no partial exit is representable. The
`entry_plan` describes staged *entry*, and nothing describes staged exit.

## 12.8 Final Exit

**Never fires automatically** (Section 11.9). The two statuses that would represent an exit —
`HIT_TARGET` and `HIT_SL` — are written **only** by the dead
`update_pro_system_outcomes()`. `EXPIRED` and `CANCELLED` are never written to a long-term row
by any code path.

Consequences that are visible in production surfaces:

- `get_dashboard_data()['long_term']['tabs']` — `hit_target`, `hit_sl`, `expired`, `cancelled`,
  and `pending` are all permanently empty; every row is in `active`.
- `long_term.analytics.win_rate` = `round(hit_target/(hit_target+hit_sl)×100, 1) if closed > 0
  else 0.0` → **always 0.0**.
- `pro_system_service._aggregate_lt` computes `pnl_amt`/`pnl_pct` only when
  `status ∈ {HIT_TARGET, HIT_SL, EXPIRED}` **and** `exit_price` is set → **always 0** in the
  performance report.
- The only place a real long-term P&L number appears is the live unrealised calculation in
  `get_dashboard_data._fmt_lt` and in the Telegram holdings block.

## 12.9 The documented exit rule

`hold_rule = "Hold for 1-2 years (max 2 years). Exit early if trend breaks below 200 EMA."`
(and, in the UI formatter, a variant citing ROE < 10 %).

**This is the operative exit policy, and it is executed by a human reading Telegram.**
Nothing in the codebase evaluates it.

---

# SECTION 13 — Position Sizing

## 13.1 Server-side

**None.** `SignalHistory` has no quantity, capital, allocation, or weight field.

## 13.2 Capital Allocation

Three unconnected display-only assumptions:

| Surface | Rule | Source |
|---|---|---|
| Telegram holdings block | `qty = 100_000 // entry_price` | `trade_engine.LONG_TERM_ASSUMED_CAPITAL_PER_POSITION = 100_000`, with the in-code note: *"Long-term BUY signals are scan-only picks with no recorded quantity/capital, so an equal notional per position is assumed… (agreed basis: ₹1,00,000 per position)."* |
| `ProSystem.jsx` long-term table | `ltCapitalPerStock = capital / active_count`; `ltQty = floor(ltCapitalPerStock / entry)` | `ProSystem.jsx:109-111`, with the note: *"Long-term has no SL to size risk against (buy-and-hold), so position size … is an equal-weight split of Total Capital across active LT positions."* |
| Performance report | `qty = max(1, 5000 // |entry − stop_loss|)`, or flat `100` if `stop_loss is None` | `pro_system_service.py:854-859` |

The third assumes a risk-based sizing that contradicts the "no SL to size against" premise of
the second.

## 13.3 Risk %

No risk-per-trade parameter exists for long-term. The `ProSystem.jsx` "Max Risk (1.5 %)"
sidebar tile is computed from `capital` and is used only by the **short-term** table's Qty
column; the long-term table ignores it entirely.

## 13.4 Maximum Positions

**No cap.** The structural limits are:
- ≤5 new picks per scan (`results[:5]`),
- one live row per symbol (the ACTIVE filter plus the `unique_live_signal` DB constraint),
- and — because positions never close — the ACTIVE set is **monotonically increasing**.

Over time this matters: `ltCapitalPerStock = capital / ltActiveCount` shrinks with every
addition, so the UI's displayed ₹ P&L for every existing holding silently changes whenever a
new pick is added.

## 13.5 Sector Exposure

**Not implemented.** No sector data exists (Section 3.3), so concentration is invisible.

## 13.6 Portfolio Exposure

**Not implemented.** `long_term.analytics` contains only `total`, `active_count`,
`pending_count`, `win_rate`. No exposure, no beta, no correlation, no aggregate P&L.

## 13.7 Maximum Loss

Per-position: nominally −50 % (the stored stop), but since the stop is never evaluated, the
practical maximum loss is **unbounded**. Portfolio-level max loss: not modelled.

---

# SECTION 14 — Risk Management

## 14.1 Duplicate Prevention

Three layers:

1. **Application filter** — `_scan_new_long_term_setups` excludes symbols with an existing
   `long_term` ACTIVE row.
2. **`update_or_create`** — matches on `(symbol, category)`, so a re-pick updates in place
   rather than inserting.
3. **DB constraint** — `UniqueConstraint(['symbol','category','status'],
   condition=Q(status__in=['PENDING','ACTIVE']))`. Enforced by Postgres as a partial unique
   index; this is the only hard guarantee.

⚠️ Layer 2's lookup is not status-scoped, so it can match multiple historical rows for a symbol
and raise `MultipleObjectsReturned` (Section 5, Step A4). The caller's blanket
`except Exception` would swallow it and abandon the entire long-term block for that run.
*(Code-reading observation; not runtime-verified.)*

## 14.2 Concurrency / Overlap Locks

The long-term scan has **no lock of its own**. It inherits protection from its caller's
`trade_engine_scanner_running` lock (`cache.add(..., timeout=600)`), because the whole chain
runs inside `run_daily_scanner`.

**The manual path is unprotected:** `cron-trigger?action=short_term_scan` →
`run_periodic_scanners(action="short_term_scan")` → `get_pro_system_data(trigger_scan=True)` →
`scan_long_term_stocks()`. That path takes only `run_periodic_scanners_running`, which is a
*different* key from `trade_engine_scanner_running`. **A manual `short_term_scan` firing while
the 10:00 scanner is running will run two long-term scans concurrently against the same
`requests.Session`** — precisely the failure mode
(`SSL "decryption failed or bad record mac"`) the short-term lock was added to prevent.

## 14.3 Circuit Filters

**Not implemented.** The `change_percent > −5.0` prefilter is the closest thing: it skips
stocks in an acute single-day fall, but it is not a circuit-limit check (Angel One's
circuit-limit fields are never read).

## 14.4 Liquidity Filters

Section 3.4: `trade_volume > 50,000` only, at prefilter. No spread, no impact cost, no
delivery percentage, no candle-stage volume floor.

## 14.5 Cooldown

**Not implemented for long-term.** The 28-calendar-day cooldown
(`archived_cooldown_trading_days = 20`) is `ShortTermSignal.cooldown_until`;
`SignalHistory` has no such field. Because long-term rows never leave `ACTIVE`, the ACTIVE
filter functions as a permanent cooldown by accident.

## 14.6 Maximum Daily Trades

Effectively ≤5 new long-term positions per scan run, and the scan runs at most once a day
(and only on days the strict short-term pass is empty).

## 14.7 Maximum Daily Loss

**Not implemented.**

## 14.8 Volatility Filters

**Not implemented.** ATR only sizes the (unenforced) stop.

## 14.9 Broker-level protections (shared infrastructure)

Inherited from `angel_one_service.py`; identical to the short-term system:

| Protection | Detail |
|---|---|
| Per-category circuit breaker | `_REST_CIRCUIT_BREAKER_UNTIL = {"candle", "quote"}`; HTTP 403/429 or an HTML (WAF) body disables that category for **300 s** |
| Candle rate limit | `_candle_api_lock`, ≥ **1.05 s** between all candle calls process-wide |
| Bulk-quote pacing | `_bulk_quote_api_lock`, ≥ **0.5 s** |
| Scan sleep | `time.sleep(0.35)` per candidate in `scan_long_term_stocks` |
| Auth throttle | 60 s cooldown after a failed login; session reused 18 h |
| AG8001 recovery | Force re-auth + one retry |
| Candle cache | 240 s, keyed `(exchange, token, interval, lookback_days)` |
| WebSocket self-heal / bootstrap guard | `_STREAMER_RESTART_LOCK`, `_BOOTSTRAP_RUNNING` |

## 14.10 Data-integrity guards

- `if df.empty or len(df) < 100: return None`
- `if not token: return None`
- `if not token_map: return []`
- `if svc is None: return []`
- Blanket `except Exception: return None` around the whole per-symbol quality check
- Blanket `except Exception` around the whole `_scan_new_long_term_setups` body
- `if sig.target is None or sig.stop_loss is None: continue` in
  `update_pro_system_outcomes` — a guard for legacy rows persisted before SL/target were added
  (in dead code, but it documents that such rows exist)

## 14.11 Risk controls that are documented but not implemented

| Documented in | Rule | Implemented? |
|---|---|---|
| `hold_rule` | Exit if trend breaks below 200 EMA | **No** |
| `hold_rule` (UI variant) | Exit if ROE < 10 % or consistent losses | **No** — ROE is never fetched |
| `hold_rule` | Max 2 years | **No** |
| `entry_plan` | 30 % / 30 % / 40 % tranching | **No** |
| `pro_system_service.py:536-541` | ATR SL/target as a "hard backstop" for auto-close | **No** — the auto-closer is dead code, and the live writer overwrites the ATR levels anyway |

---

# SECTION 15 — Trade Lifecycle

## 15.1 State machine (as implemented)

`SignalHistory.Status` has six values: `PENDING, ACTIVE, CANCELLED, HIT_TARGET, HIT_SL, EXPIRED`.

For `category="long_term"`, **exactly one is ever written by live code**:

```
   10:00 chain, on days the strict short-term pass finds nothing
                            │
                            ▼
              scan_long_term_stocks() → top 5
                            │
                  not already ACTIVE?
                            │
                            ▼
                     ┌────────────┐
                     │   ACTIVE   │ ◄── created here, status hardcoded
                     └─────┬──────┘
                           │
                           │  (no auditor reaches this row)
                           │  live price fetched on demand for display only
                           │
                           ▼
                     ┌────────────┐
                     │   ACTIVE   │  … indefinitely
                     └────────────┘

   Unreachable in the current build:
     PENDING     — never written (rows are born ACTIVE)
     HIT_TARGET  — only update_pro_system_outcomes(), which has no callers
     HIT_SL      — only update_pro_system_outcomes(), which has no callers
     EXPIRED     — no long-term code path writes it
     CANCELLED   — no long-term code path writes it
```

## 15.2 Signal Created

`_scan_new_long_term_setups` → `SignalHistory.objects.update_or_create(...)` with
`status=ACTIVE`. `generated_at` is `auto_now_add`.

**No audit row.** Unlike the short-term engine (which writes a `TradeHistory` row for every
transition), `TradeHistory.trade` is a FK to `ShortTermSignal` and cannot reference a
`SignalHistory` row. There is **no audit trail for long-term signals at all**.

## 15.3 Pending

**Never occurs.** The `pending` tab in `get_dashboard_data()['long_term']['tabs']` and the
`PENDING` inclusion in `DashboardSummaryView`'s long-term query are structurally dead.

## 15.4 Active

The only state. While ACTIVE, the row is:
- included in `_get_active_long_term_holdings_lines()` (Telegram, when the block runs),
- included in `get_dashboard_data()['long_term']['tabs']['active']` with a live `current_price`
  and `pnl_pct` from `live_signal_service.get_latest_prices()`,
- included in `DashboardSummaryView`'s top-3 preview,
- included in `get_performance_report()`'s `long_term_history` **only for its generation date**
  (that function filters `generated_at__date == target_date`), where it receives a
  `current_price` and a `hold_rule` string,
- included unconditionally in `get_pro_performance_report()`'s `_aggregate_lt` (last 50 rows).

Nothing writes to the row.

## 15.5 Target Hit

Would set `status=HIT_TARGET`, `exit_price=target`, `exit_time=datetime.now()`.
**Dead code.** Note it uses naive `datetime.now()` rather than `django.utils.timezone.now()`,
which under `USE_TZ` would produce a naive-datetime warning.

## 15.6 Stop Hit

Would set `status=HIT_SL`, `exit_price=stop_loss`, `exit_time=datetime.now()`. **Dead code.**

## 15.7 Cancelled

Never written for long-term. (`DeltaHedgeView.post` bulk-cancels `category='specialist'` rows
only.)

## 15.8 Expired

Never written for long-term. The intraday engine's stale-signal guard filters
`category="intraday"`; `run_expiry_cleanup` queries `ShortTermSignal`.

## 15.9 Closed

Not a `SignalHistory` status. (`CLOSED` exists on `ShortTermSignal` and is itself never
assigned.)

## 15.10 Archived

Not a `SignalHistory` status. The archive-with-cooldown pattern is short-term-only.

## 15.11 Practical consequence

The long-term book is **append-only**. Every pick ever made is still ACTIVE, still shown in the
dashboard's active tab, still included in the Telegram holdings block, and still counted in
`active_count` (which the UI divides capital by). There is no code path that removes a position
from the book. Closing one requires a manual DB or Django-admin edit.

---

# SECTION 16 — Data Flow

## 16.1 End-to-end

```
                     [ trigger: 10:00 short-term scanner, strict pass empty ]
                                             │
                    trade_engine._send_telegram_scanner_summary()
                                             │
                          _scan_new_long_term_setups()
                                             │
     SignalHistory(long_term, ACTIVE) ◄──────┤  already_active set
                                             ▼
                    pro_system_service.scan_long_term_stocks()
                                             │
IndexConstituent(NIFTY500) ─┐                │
NSE archive CSV            ─┼─► symbols ─────┤
NIFTY500_FALLBACK          ─┘                │
                                             ▼
        Angel One instrument master (6h TTL) → get_token_map()
                                             ▼
        Angel One /market/v1/quote FULL, chunks of 50, ≥0.5s apart
                                             │  vol > 50k AND change > −5%
                                             │  sort by volume desc → top 75
                                             ▼
        per symbol: sleep(0.35) → _fetch_long_term_quality()
              ├─ get_token_map([symbol])              (2nd lookup)
              └─ get_candle_data(ONE_DAY, 200d)       (≥1.05s lock, 240s cache)
                                             │  close > EMA50 > EMA200 ?
                                             ▼
              compute perf_100d, ATR14, stop_loss(3×ATR/15%), target(2.5R)
                                             │  sort by perf_100d desc → top 5
                                             ▼
                        list[dict] returned to the caller
                                             │
                        filter out already-ACTIVE symbols
                                             ▼
        SignalHistory.update_or_create(symbol, category="long_term", defaults={
            signal_type="BUY PIP", entry_price=price,
            stop_loss=price×0.5,  target=price×2.0,     ← ATR values DISCARDED
            reason="Top Sector Leader (Large Cap)", status=ACTIVE })
                                             │
                    ┌────────────────────────┴────────────────────────┐
                    ▼                                                 ▼
   📈 NEW LONG-TERM SETUPS block                      🏦 ACTIVE LONG-TERM HOLDINGS block
   (appended to the short-term                        (_get_active_long_term_holdings_lines,
    scanner's Telegram message)                        orch.get_price per symbol,
                    │                                  qty = 100_000 // entry)
                    └────────────────────────┬────────────────────────┘
                                             ▼
                                 TelegramLog (PENDING)
                                             │ 1-minute dispatcher
                                             ▼
                                     Telegram Bot API

   ── read path (independent) ──
   GET /api/stocks/pro-system/  → get_dashboard_data()
        └─ SignalHistory.filter(category="long_term").order_by('-generated_at')
        └─ get_latest_prices([active symbols])   ← WebSocket-first bulk
        └─ _fmt_lt() → tabs{pending,active,hit_target,hit_sl,cancelled,expired} + analytics
        └─ 30s cache → ProSystem.jsx (?view=long_term)
```

## 16.2 Market Data

**Prefilter path** — `get_bulk_quotes(..., mode="FULL")`:
1. Serve from `_STREAM_CACHE` when fresh (`source=="websocket"` or `age < 30 s`, **and**
   `high > 0 and low > 0` in FULL mode).
2. Circuit-breaker check.
3. Warm-subscribe every token about to be REST-fetched.
4. Pace ≥0.5 s, POST, parse via `_parse_quote_item`, write results back into `_STREAM_CACHE`
   as `source="rest_fallback"`.

**Candle path** — `get_candle_data(token, "NSE", "ONE_DAY", now−200d, now)`, global ≥1.05 s
lock, 240 s response cache keyed on rounded lookback days.

**Live-price path (display only)** — two different mechanisms:
- `live_signal_service.get_latest_prices(symbols)` → `MarketDataOrchestrator.get_prices_bulk()`
  — checks the 5 s `orchestrator_price_*` cache, then one bulk Angel One call, then a
  per-symbol `get_price()` fallback for anything still missing.
- `_get_active_long_term_holdings_lines()` → `orch.get_price(sym)` **one symbol at a time in a
  loop**, each with its own 5 s cache lookup and potential REST call. For N holdings this is up
  to N sequential REST quotes, unlike the bulk path used by the dashboard.

## 16.3 Indicators

Computed in-process on pandas DataFrames; **nothing is persisted.** `SignalHistory.metadata`
(a JSONField that the intraday engine uses to store `vol_ratio`, `vwap`, `atr`, `poc`, `vah`,
`val`, `score`) is left as `{}` for long-term rows — `_scan_new_long_term_setups` writes no
metadata. EMA50, EMA200, ATR14, `perf_100d`, `vol_20d` and `mcap` are all discarded after the
scan.

## 16.4 Signal Engine → Database

Single write: `SignalHistory.objects.update_or_create(...)`. No transaction wrapper, no audit row.

## 16.5 Database → Frontend

`get_dashboard_data()` (`trade_engine.py:1436-1500`):
```python
lt_signals = SignalHistory.objects.filter(category="long_term").order_by('-generated_at')
active_lt_symbols = [s.symbol for s in lt_signals if s.status == ACTIVE]
lt_price_map = get_latest_prices(active_lt_symbols)     # wrapped in try/except
```
`_fmt_lt(sig)` emits: `id, symbol, entry_price, current_price, pnl_pct, stop_loss, target,
status, reason, exit_price, exit_time, generated_at`.
`current_price` and `pnl_pct` are populated **only** for ACTIVE rows with a resolved live price;
otherwise `None`.

The in-code note explains why long-term is kept parallel rather than merged into the
short-term tabs: *"SignalHistory has a 6-state Status vs ShortTermSignal's 14-state Status, and
no target2/ai_score/highest_profit/etc. fields."*

## 16.6 Notifications

Section 1.11. There is **no long-term-specific `event_type`** — the two long-term blocks ride
inside `DAILY_SCANNER_SUMMARY` or `SCANNER_NO_SETUPS`.

Telegram format (`trade_engine.py:1243-1252`):
```
📈 NEW LONG-TERM SETUPS

  1. RELIANCE (Large Cap)
  ▸ Price: ₹2845.6
  ▸ Plan: Buy 30% Now, 30% on 10% dip, 40% on major correction
  ▸ Hold for 1-2 years (max 2 years). Exit early if trend breaks below 200 EMA.
```
Holdings format (`trade_engine.py:1175-1179`):
```
🏦 ACTIVE LONG-TERM HOLDINGS

  RELIANCE 📈
  ▸ Entry: ₹2845.60 | CMP: ₹2991.20
  ▸ P&L: +5.12% (+₹5,088)
```
When the live price is unavailable, P&L renders as `Fetching...` and CMP as `—`.

## 16.7 Charts

**None.** `ProSystem.jsx`'s long-term view is a table only. No price chart, no EMA overlay,
no equity curve, no allocation pie exists anywhere in the app for long-term positions.

---

# SECTION 17 — Scheduler

## 17.1 Long-term jobs registered in `updater.py`

**Zero.** Searching `updater.py` for `long_term`, `long-term`, or `scan_long_term` returns
nothing. There is no cron entry, no interval job, and no management command
(`backend/stocks/management/commands/` contains `scan_strangles`, `generate_strangle_signals`,
`run_daily_market_update`, `refresh_nifty500`, `backfill_option_selling_metadata`,
`recreate_missing_tables`, `init_angel_one`, `send_test_telegram`, `send_test_whatsapp`,
`create_owner` — none of them long-term).

## 17.2 How it actually executes — call-chain analysis

**Path 1 — the live path (scheduled, indirect):**
```
APScheduler  cron mon-fri 10:00:00   id="trade_engine_scanner_10am"
  └─ updater.run_short_term_scan()
       └─ trade_engine.run_daily_scanner()
            ├─ LOCK cache.add("trade_engine_scanner_running", 600s)
            ├─ pass 1: _run_daily_scanner_impl(relaxed=False, send_telegram=False)
            │     └─ if it returns a non-empty list  ──►  RETURN. LONG-TERM NEVER RUNS.
            └─ pass 2: _run_daily_scanner_impl(relaxed=True)      [send_telegram defaults True]
                 └─ Step 8: _send_telegram_scanner_summary(top_alerts, direction)
                      ├─ _get_active_holdings_lines()             [short-term holdings]
                      ├─ _get_active_long_term_holdings_lines()   [LT holdings + live prices]
                      └─ _scan_new_long_term_setups()             ◄── THE LONG-TERM SCAN
                           └─ pro_system_service.scan_long_term_stocks()
```

**Frequency in practice:** at most once per trading day, and **only on days the strict
short-term pass produced zero new picks** — which includes every day
`get_market_direction()` returns `BEARISH` (that path returns `[]` from
`_run_daily_scanner_impl`, triggering the relaxed pass).

**Path 2 — the legacy path (manual only):**
```
GET /api/stocks/cron-trigger/?token=<secret>&action=short_term_scan
  └─ CronScannerTriggerView.get()
       ├─ guard: is_market_open_today() unless &force=1
       └─ APScheduler one-off job (or a raw thread) → run_periodic_scanners(action="short_term_scan")
            ├─ LOCK cache.add("run_periodic_scanners_running", 600s)
            └─ pro_system_service.get_pro_system_data(trigger_scan=True)
                 ├─ scan_short_term_stocks()      [legacy short-term scanner]
                 └─ scan_long_term_stocks()       ◄── THE LONG-TERM SCAN, DIFFERENT WRITER
```

## 17.3 The two writers compared

This is the most important operational difference in the long-term system.

| | **Path 1 — `_scan_new_long_term_setups`** | **Path 2 — `get_pro_system_data`** |
|---|---|---|
| File:line | `trade_engine.py:1199-1211` | `pro_system_service.py:529-546` |
| Trigger | 10:00 scanner, strict pass empty | `cron-trigger?action=short_term_scan` only |
| Dedup | Excludes symbols already ACTIVE | None — relies on `update_or_create` |
| Lookup keys | `symbol`, `category` | `symbol`, `category`, **`generated_at__date=dt_today`** |
| `stop_loss` | `price × 0.5` (**−50 %**) | `l.get("stop_loss")` — ATR 3× / 15 % floor |
| `target` | `price × 2.0` (**+100 %**) | `l.get("target")` — 2.5 × sl_points |
| `signal_type` | `"BUY PIP"` | `"BUY PIP"` |
| `status` | `ACTIVE` | `ACTIVE` |
| Lock held | `trade_engine_scanner_running` | `run_periodic_scanners_running` (a *different* key) |

⚠️ Path 2's `update_or_create` passes `generated_at__date=dt_today` as a **lookup keyword**
against an `auto_now_add` field. Django's `_extract_model_params`
(`django/db/models/query.py:989`) builds create-params with
`{k: v for k, v in kwargs.items() if LOOKUP_SEP not in k}` — keys containing `__` are
**silently dropped** before field validation, so this raises no `FieldError` and the create
succeeds with `symbol` + `category` + `defaults`. Verified against the installed Django source
and against live DB rows written by this path.

The real defect is one step later. The `.get()` lookup **is** date-scoped, so on any subsequent
day the existing row does not match, and Django proceeds to `create()` a second row with
`status=ACTIVE`. That collides with the partial unique index
`unique_live_signal (symbol, category, status) WHERE status IN ('PENDING','ACTIVE')` →
**`IntegrityError`, uncaught** (`get_pro_system_data` has no try/except around this block; only
the outer `run_periodic_scanners` does). Net effect: **this path can persist a given symbol
exactly once, then raises on every later run for that symbol.**

**Live DB evidence (queried 2026-07-26):** the only two long-term rows in the database are
`ADANIPORTS` (entry 1841.90, SL 1565.62, target 2532.61) and `ADANIENT` (entry 3187.50,
SL 2709.38, target 4382.81), both `ACTIVE`, both generated 2026-07-22. Both sets of levels
are `entry × 0.85` and `entry + 2.5 × sl_points` — i.e. the **ATR/15 %-floor levels of
Path 2**, not Path 1's `×0.5` / `×2.0`. So in this deployment it is the *legacy manual* path
that has written the book, and **Path 1 has never persisted a long-term row.**

## 17.4 Frequency, order, dependencies

| Aspect | Value |
|---|---|
| Frequency | ≤1×/trading day, conditional on the strict short-term pass being empty |
| Position in the daily order | After all short-term scanner work, during Telegram message assembly (~10:02–10:04) |
| Runtime | ~75 candidates × (0.35 s sleep + ≥1.05 s candle lock) ≈ **80–110 s**, plus ~10 bulk-quote chunks at 0.5 s, plus the per-symbol `get_token_map` calls (in-memory, cheap) |
| Hard dependency | Angel One authenticated singleton → else `return []` |
| Hard dependency | Nifty 500 universe → falls back to `NIFTY500_FALLBACK` |
| Hard dependency | Instrument master indexed → else `return []` |
| Hard dependency | ≥100 daily candles per symbol → else that symbol is skipped |
| Soft dependency | `_get_active_long_term_holdings_lines()` runs *before* the scan in the same function, so the holdings block reflects the state **before** today's new picks |
| Failure mode | Any exception → `_scan_new_long_term_setups` logs and returns `[]`; the Telegram message then prints *"No new long-term setups found today."* — indistinguishable from a genuinely empty scan |

## 17.5 Contention

The long-term scan adds ~80–110 s of rate-limited Angel One traffic **inside** the short-term
scanner's critical path, while:
- the `trade_engine_activation_checker` job may fire at 10:15 (a different lock, so it overlaps
  and competes for the same session), and
- `run_periodic_scanners` starts its own intraday/specialist/option-buying sweep at 11:00.

All of it shares one `requests.Session`, one 1.05 s candle lock, and one 300 s circuit breaker.

---

# SECTION 18 — Performance

## 18.1 Caching

| Key | TTL | Relevance to long-term |
|---|---|---|
| `candle_cache_{exch}_{token}_{interval}_{lookback_days}` | 240 s | 200-day lookback → its own key; no collision with the short-term 365-day or the EOD 120-day requests |
| `orchestrator_price_{symbol}_NSE` | 5 s | Live P&L lookups |
| `trade_engine_dashboard_30s` | 30 s | Full `/pro-system/` payload incl. the long-term block |
| `dashboard_summary_20s` | 20 s | Top-3 preview |
| `run_periodic_scanners_running` | 600 s | Guards path 2 |
| `trade_engine_scanner_running` | 600 s | Guards path 1 |

In-memory: `_INSTRUMENT_MASTER_CACHE` (6 h), `_STREAM_CACHE` (WebSocket ticks; REST-sourced
entries expire after 30 s), `_NSE_STATUS_CACHE` (60 s), `_MARKET_OPEN_CACHE` (60 s).

**No caching of the scan result itself.** Every run re-fetches all 75 candle series (subject to
the 240 s candle cache, which is far shorter than the once-a-day cadence, so effectively every
run is a cold fetch).

## 18.2 Parallel Processing

**None.** The candidate loop is strictly sequential with an explicit `time.sleep(0.35)`.
`pro_system_service.py:14` imports `ThreadPoolExecutor` and **never uses it**.

Sequential is intentional at the infrastructure level: the docstring on
`trade_engine.run_daily_scanner` records that concurrent scans corrupted the shared TLS session.

## 18.3 Batch Processing

- Quotes: chunks of **50** tokens (`pro_system_service.py:436-441`).
- Candles: **not batchable** — one token per Angel One historical call. This is the entire
  bottleneck.
- Telegram: batches of 20 per dispatcher run.

**Known inefficiency:** `_fetch_long_term_quality` calls
`svc.get_token_map([symbol], exchange="NSE")` **per symbol** (line 355), even though
`scan_long_term_stocks` already built a full `token_map` at line 429 and could pass the token
in. `get_token_map` is an in-memory dict lookup after `_refresh_instrument_master()`, so the
cost is a TTL check and a dict access rather than a network call — but the token is being
resolved twice for every one of the 75 candidates.

## 18.4 Retry Logic

| Layer | Retry |
|---|---|
| Angel One AG8001 (invalid session) | Force re-auth + retry once (in `get_bulk_quotes`, `get_candle_data`, `get_live_price_by_token`) |
| Angel One 403/429/WAF | **No retry** — trip the 300 s breaker, return empty |
| Per-symbol quality check | **No retry** — `except Exception: return None`, unlogged |
| Whole long-term block | **No retry** — `except Exception: return []`, logged once |
| Telegram | 3 attempts via `TelegramLog.retry_count`, 1/minute |
| Universe fetch | No retry; falls through DB → CSV → hardcoded list |

There is no strict/relaxed fallback for the long-term scan (unlike the short-term scanner's
two-pass design). If the EMA gate produces zero survivors, the day yields zero long-term picks.

## 18.5 Error Handling

- `scan_long_term_stocks`: `try/except` around the universe fetch and around each quote chunk;
  no wrapper around the candidate loop itself, but each `_fetch_long_term_quality` call is
  internally total-wrapped.
- `_fetch_long_term_quality`: one bare `except Exception: return None` around the entire body —
  **no logging**. A systematic failure (e.g. a schema change in the candle response) would
  manifest as "no long-term setups found today", silently, indefinitely.
- `_scan_new_long_term_setups`: `except Exception` → logs
  `"Long-term scan for scanner report failed: %s"` → `return []`.
- `get_dashboard_data`'s price fetch: `except Exception` → logs a warning, leaves `lt_price_map`
  empty, and every row renders `current_price = None`.
- `ProSystemView` has no try/except — an exception surfaces as a DRF 500.

## 18.6 Rate Limits

| Endpoint | Pacing | Breaker |
|---|---|---|
| Historical candles (~3/s documented) | `_candle_api_lock`, ≥ **1.05 s** globally | `_REST_CIRCUIT_BREAKER_UNTIL["candle"]`, 300 s |
| Bulk quote | `_bulk_quote_api_lock`, ≥ **0.5 s** | `_REST_CIRCUIT_BREAKER_UNTIL["quote"]`, 300 s |
| Single quote fallback | shares the 1.05 s candle lock | quote breaker |

Trip conditions: HTTP `403`, HTTP `429`, or a body containing `<html` (Angel One's WAF block page).

The `time.sleep(0.35)` in the candidate loop is **redundant with** the 1.05 s candle lock — the
lock dominates, so the effective pace is ~1 candidate/second regardless. The in-code comment
says *"Rate limit safety: 3 req/sec limit on Angel One historical API"*.

## 18.7 Cost profile of one long-term run

| Stage | Calls | Wall time |
|---|---|---|
| Universe (DB hit) | 1 query | ~ms |
| `get_token_map` (bulk) | 0 network (in-memory after master refresh) | ~ms |
| Instrument master refresh (if stale) | 1 HTTP (large JSON) | 5–20 s |
| Bulk quotes | ~10 POSTs at ≥0.5 s | ~5 s |
| Candles | **75 POSTs at ≥1.05 s** | **~80 s** |
| Persistence | ≤5 upserts | ~ms |
| **Total** | | **≈ 85–105 s** |

Plus `_get_active_long_term_holdings_lines()`, which runs immediately before it and issues up
to N sequential `orch.get_price()` calls (one per active holding, each potentially a REST quote
under the 1.05 s lock). As the book grows monotonically (Section 15.11), **this block gets
slower every time a position is added and never gets faster**, because positions never close.

---

# SECTION 19 — Daily Example

A concrete walk-through with real arithmetic. Input values are illustrative; every formula and
constant is quoted from the code.

**Stock: `RELIANCE`. Date: Wednesday.**

### Precondition — why the long-term scan runs at all

09:05 `get_market_direction()`:
```
Nifty close = 24,310.00 ; ema20 = 24,455.60 ; ema50 = 24,520.10
24310 > 24455.60 ?  No   →  trend = "BEARISH"
```

10:00 `run_daily_scanner()`:
```
LOCK trade_engine_scanner_running       → acquired
pass 1: _run_daily_scanner_impl(relaxed=False, send_telegram=False)
        trend == 'BEARISH' and not relaxed  →  return []      (no Telegram, send_telegram=False)
result is empty  →  log "Strict scan found no setups — falling back to relaxed mode"
pass 2: _run_daily_scanner_impl(relaxed=True)                  (send_telegram defaults True)
        BEARISH gate skipped (relaxed=True)
        ... short-term relaxed scan runs ...
        Step 8 → _send_telegram_scanner_summary(top_alerts, direction)
                   → _get_active_long_term_holdings_lines()
                   → _scan_new_long_term_setups()      ◄── LONG-TERM SCAN STARTS
```

### Step 1 — Already-tracked symbols
```sql
SELECT symbol FROM signal_history WHERE category='long_term' AND status='ACTIVE';
-- → {'TCS', 'HDFCBANK'}
```

### Step 2 — Universe
`_fetch_nifty500_symbols()` returns 500 symbols from `IndexConstituent`.
`get_token_map` resolves 487 of them (13 unmatched, silently dropped).
`RELIANCE → "2885"`.

### Step 3 — Bulk quote prefilter
`NSE:2885` from the FULL bulk quote:
```
ltp = 2,845.60 ; change_percent = −0.42 ; trade_volume = 8,940,000

trade_volume 8,940,000 > 50,000    ✓
change_pct   −0.42     > −5.0      ✓
```
Sorted by `volume` descending, RELIANCE lands at rank 9 → inside `[:75]` ✓

*(Note: a −0.42 % day passes here. The short-term engine would have rejected it at
`change_pct > 0.5`. This is the clearest behavioural difference between the two prefilters.)*

### Step 4 — Quality check
```
time.sleep(0.35)
get_token_map(["RELIANCE"]) → "2885"          (second lookup, in-memory)
get_candle_data("2885", "NSE", "ONE_DAY", now−200d, now)
  → 138 rows   (200 calendar days ≈ 138 trading sessions)

138 >= 100  ✓
```

Indicator values from the 138-row series:
```
close  = 2,845.60
ema50  = 2,772.30      # ewm(span=50,  adjust=False).mean().iloc[-1]
ema200 = 2,681.95      # ewm(span=200, adjust=False).mean().iloc[-1]   ← seeded on 138 bars
```

**The one filter:**
```
close 2845.60 > ema50 2772.30 > ema200 2681.95   ✓  PASS
```

### Step 5 — Derived values
```
Close[-100]  = 2,530.00
perf_100d    = (2845.60 − 2530.00)/2530.00 × 100 = 12.474  →  12.5
vol_20d      = 7,120,000
mcap (proxy) = int(2845.60 × 7,120,000) = 20,260,672,000     # never used

atr14        = 46.20                       # TR.rolling(14).mean().iloc[-1]
stop_loss    = 2845.60 − (3 × 46.20) = 2845.60 − 138.60 = 2,707.00
max_sl_floor = 2845.60 × 0.85 = 2,418.76
2707.00 > 2418.76  →  floor not applied
sl_points    = 2845.60 − 2707.00 = 138.60          (4.87 % risk)
target       = 2845.60 + (2.5 × 138.60) = 3,192.10 (+12.18 %)
```

Returned dict:
```python
{
  "symbol": "RELIANCE",
  "sector": "Large Cap",                 # hardcoded
  "mcap": 20260672000,                   # price × avg volume, never used
  "roe": 22.5,                           # hardcoded constant
  "debt_to_equity": 0.2,                 # hardcoded constant
  "revenue_growth": 12.5,                # ← actually 100-day PRICE return
  "profit_growth": 15.0,                 # hardcoded constant
  "price": 2845.6,
  "stop_loss": 2707.0,                   # ← will be DISCARDED by the live writer
  "target": 3192.1,                      # ← will be DISCARDED by the live writer
  "entry_plan": "Buy 30% Now, 30% on 10% dip, 40% on major correction",
  "hold_rule": "Hold for 1-2 years (max 2 years). Exit early if trend breaks below 200 EMA."
}
```

### Step 6 — Ranking
Of 75 candidates, 23 passed the EMA gate. Sorted by `revenue_growth` (= `perf_100d`) descending:
```
1. TRENT        +31.2
2. BEL          +24.8
3. TCS          +18.1    ← already ACTIVE, will be filtered in step 7
4. RELIANCE     +12.5
5. HINDUNILVR   +11.9
────────────── results[:5] cut ──────────────
6. ITC          +11.4    ✗ dropped
...
```

### Step 7 — Deduplicate
```
already_active = {'TCS', 'HDFCBANK'}
new_picks = [TRENT, BEL, RELIANCE, HINDUNILVR]      # TCS removed
```

### Step 8 — Persist (the live writer)
```python
SignalHistory.objects.update_or_create(
    symbol="RELIANCE",
    category="long_term",
    defaults={
        "signal_type": "BUY PIP",
        "entry_price": 2845.6,
        "stop_loss":   2845.6 * 0.5 = 1422.80,     # ← NOT the 2707.00 computed above
        "target":      2845.6 * 2.0 = 5691.20,     # ← NOT the 3192.10 computed above
        "reason":      "Top Sector Leader (Large Cap)",
        "status":      "ACTIVE",
    },
)
```

Resulting row:
```
id           = 1043
symbol       = 'RELIANCE'
category     = 'long_term'
signal_type  = 'BUY PIP'
entry_price  = 2845.60
stop_loss    = 1422.80        (−50.00 %)
target       = 5691.20        (+100.00 %)
rr           = NULL
status       = 'ACTIVE'
reason       = 'Top Sector Leader (Large Cap)'
metadata     = {}
generated_at = <today 10:03>
active_time  = NULL
exit_price   = NULL
exit_time    = NULL
```

**No `TradeHistory` row is written** — `TradeHistory.trade` is a FK to `ShortTermSignal` and
cannot reference this row. There is no audit entry for this signal's creation.

### Step 9 — Telegram
Appended to the relaxed pass's scanner message:
```
🏦 ACTIVE LONG-TERM HOLDINGS

  TCS 📈
  ▸ Entry: ₹4120.00 | CMP: ₹4331.50
  ▸ P&L: +5.13% (+₹4,858)

  HDFCBANK 📉
  ▸ Entry: ₹1712.40 | CMP: ₹1689.20
  ▸ P&L: -1.35% (-₹1,322)

📈 NEW LONG-TERM SETUPS

  1. TRENT (Large Cap)
  ▸ Price: ₹6210.5
  ▸ Plan: Buy 30% Now, 30% on 10% dip, 40% on major correction
  ▸ Hold for 1-2 years (max 2 years). Exit early if trend breaks below 200 EMA.

  2. BEL (Large Cap)
  ...

  3. RELIANCE (Large Cap)
  ▸ Price: ₹2845.6
  ▸ Plan: Buy 30% Now, 30% on 10% dip, 40% on major correction
  ▸ Hold for 1-2 years (max 2 years). Exit early if trend breaks below 200 EMA.

  4. HINDUNILVR (Large Cap)
  ...
```
Note the TCS holdings line reflects state **before** today's scan, because
`_get_active_long_term_holdings_lines()` runs before `_scan_new_long_term_setups()` inside
`_send_telegram_scanner_summary`. The TCS ₹ figure uses `qty = 100_000 // 4120 = 24` shares:
`(4331.50 − 4120.00) × 24 = ₹5,076` — the ₹4,858 above corresponds to `qty = 23`, i.e.
`100_000 // 4120 = 24`… *(the exact integer depends on the entry price; the formula is
`int(LONG_TERM_ASSUMED_CAPITAL_PER_POSITION // entry)`).*

### Step 10 — Monitoring (there is none)

| Time | Job | Touches RELIANCE's long-term row? |
|---|---|---|
| 10:15–15:45 | `check_pending_activations` | No — `ShortTermSignal` only |
| 11:00–15:15 (every 15 min) | `run_periodic_scanners` → `update_signal_outcomes` | **No** — `.exclude(category__in=[…,'long_term',…])` |
| 15:25 | `run_eod_evaluation` | No — `ShortTermSignal` only |
| 15:28 | `run_periodic_scanners(action="update")` | No — same exclusion |
| 15:35 | `send_short_term_status_update` | No — `ShortTermSignal` only |
| 16:30 | `run_daily_market_update` | No |
| Sat 06:00 | `run_expiry_cleanup` | No — `ShortTermSignal` only |

### Day 40 — Price rises to ₹3,210
`3210 >= target 5691.20`? No. Nothing happens. `status` stays `ACTIVE`.
(Had the ATR target of ₹3,192.10 been persisted, this would have been a target hit — but no
auditor would have detected it anyway, because `update_pro_system_outcomes` has no callers.)

### Day 120 — Price falls to ₹2,455, and closes below its 200 EMA
`hold_rule` says *"Exit early if trend breaks below 200 EMA."*
No code evaluates it. `2455 <= stop_loss 1422.80`? No. `status` stays `ACTIVE`.

### Day 400 — Price at ₹3,480
Dashboard (`ProSystem.jsx?view=long_term`, "Active 📈" tab), with `capital = 500000` and 6
active long-term holdings:
```
entry_price       = 2845.60
current_price     = 3480.00                        (live, from get_latest_prices)
pnl_pct           = (3480 − 2845.60)/2845.60 × 100 = +22.29 %
ltCapitalPerStock = 500000 / 6 = 83,333.33
ltQty             = floor(83333.33 / 2845.60) = 29
ltPnlRupees       = (22.29/100) × 2845.60 × 29 = +₹18,394
```
Rendered: `RELIANCE | Top Sector Leader (Large Cap) | Active 📈 | ₹2845.60 | ₹3480.00 |
+22.29% / +₹18,394 | SL: ₹1422.80  T: ₹5691.20 | <gen date>`

`long_term.analytics`: `total = 6, active_count = 6, pending_count = 0, win_rate = 0.0`.
`win_rate` is 0.0 because `hit_target` and `hit_sl` are both empty — as they always will be.

### Day 730+ — "max 2 years" elapses
Nothing. No time-stop exists. The row remains `ACTIVE`, is still counted in `active_count`,
still divides the UI's capital, and still appears in every Telegram holdings block.

---

# SECTION 20 — Complete Decision Tree

```
                ┌──────────────────────────────────────────────────────┐
                │  APScheduler  cron mon–fri 10:00:00                  │
                │  id = "trade_engine_scanner_10am"                    │
                └───────────────────────┬──────────────────────────────┘
                                        ▼
                          trade_engine.run_daily_scanner()
                                        │
                     LOCK trade_engine_scanner_running (600 s)
                            not acquired ─┴─► SKIP ENTIRE RUN
                                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ SHORT-TERM STRICT PASS  (_run_daily_scanner_impl,             │
        │                          relaxed=False, send_telegram=False)  │
        └───────────────────────────────┬───────────────────────────────┘
                        found ≥1 pick ──┴──► RETURN
                                        │      ⚠ LONG-TERM NEVER RUNS TODAY
                          found nothing │      (incl. the BEARISH early-return)
                                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ SHORT-TERM RELAXED PASS (relaxed=True, send_telegram=True)    │
        │   → Step 8: _send_telegram_scanner_summary()                  │
        │        ├─ _get_active_holdings_lines()          [short-term]  │
        │        ├─ _get_active_long_term_holdings_lines() [pre-scan]   │
        │        └─ _scan_new_long_term_setups()  ◄── ENTRY POINT       │
        └───────────────────────────────┬───────────────────────────────┘
                                        ▼
                ┌───────────────────────────────────────────┐
                │ MARKET FILTER                             │
                │   NONE. No trend check, no open check,    │
                │   no holiday check.                       │
                └───────────────────────┬───────────────────┘
                                        ▼
                ┌───────────────────────────────────────────┐
                │ UNIVERSE                                  │
                │  IndexConstituent(NIFTY500)               │
                │   → NSE archive CSV                       │
                │   → NIFTY500_FALLBACK (~180 hardcoded)    │
                └───────────────────────┬───────────────────┘
                              empty ────┴──► return []
                                        ▼
                ┌───────────────────────────────────────────┐
                │ TOKEN RESOLUTION                          │
                │  instrument master: sym → sym-EQ → token  │
                └───────────────────────┬───────────────────┘
                        unresolved ─────┴──► DROP (unlogged)
                                        ▼
                ┌───────────────────────────────────────────┐
                │ STOCK FILTER  (bulk quote, chunks of 50)  │
                │   trade_volume   >  50,000                │
                │   change_percent >  −5.0 %                │
                └───────────────────────┬───────────────────┘
                              fail ─────┴──► REJECT (unlogged)
                                        ▼
                ┌───────────────────────────────────────────┐
                │ RANK 1:  sort by trade_volume DESC        │
                │          take top 75                      │
                └───────────────────────┬───────────────────┘
                          rank > 75 ────┴──► REJECT
                                        ▼
                ┌───────────────────────────────────────────┐
                │ INDICATOR FILTER  (200 d ONE_DAY candles) │
                │   sleep(0.35) + ≥1.05 s global lock       │
                │   len(df) ≥ 100                           │
                │                                           │
                │   ►►  close > EMA50 > EMA200  ◄◄          │
                │       (the ONLY technical gate)           │
                └───────────────────────┬───────────────────┘
                              fail ─────┴──► REJECT (unlogged)
                              error ────┴──► REJECT (silently swallowed)
                                        ▼
                ┌───────────────────────────────────────────┐
                │ LEVELS  (computed — then discarded)       │
                │  perf_100d = (C[-1]−C[-100])/C[-100]×100  │
                │  SL     = max(close − 3×ATR, close×0.85)  │
                │  target = close + 2.5 × sl_points         │
                │  no sl_points>0 check, no R:R minimum     │
                └───────────────────────┬───────────────────┘
                                        ▼
                ┌───────────────────────────────────────────┐
                │ SCORE                                     │
                │   NONE. No scoring engine exists.         │
                └───────────────────────┬───────────────────┘
                                        ▼
                ┌───────────────────────────────────────────┐
                │ RANK 2:  sort by perf_100d DESC           │
                │          take top 5                       │
                └───────────────────────┬───────────────────┘
                           rank > 5 ────┴──► REJECT
                                        ▼
                ┌───────────────────────────────────────────┐
                │ RISK CHECK                                │
                │  symbol already long_term + ACTIVE ?      │
                │                        → SKIP             │
                │  (no cooldown, no max-positions,          │
                │   no sector cap, no exposure check)       │
                └───────────────────────┬───────────────────┘
                                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ SIGNAL  SignalHistory.update_or_create(symbol, category)      │
        │   signal_type = "BUY PIP"                                     │
        │   entry_price = close                                         │
        │   stop_loss   = close × 0.5      ⚠ ATR value DISCARDED        │
        │   target      = close × 2.0      ⚠ ATR value DISCARDED        │
        │   reason      = "Top Sector Leader (Large Cap)"               │
        │   status      = ACTIVE           ⚠ no PENDING stage           │
        │   (no TradeHistory audit row — FK points at ShortTermSignal)  │
        └───────────────────────────────┬───────────────────────────────┘
                                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ ENTRY                                                         │
        │   Row is born ACTIVE. No trigger, no confirmation,            │
        │   no activation check, no tranching. entry_plan is a string.  │
        └───────────────────────────────┬───────────────────────────────┘
                                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ MONITOR                                                       │
        │   update_signal_outcomes()  → .exclude('long_term')   ✗       │
        │   run_eod_evaluation()      → ShortTermSignal only    ✗       │
        │   check_pending_activations→ ShortTermSignal only    ✗        │
        │   run_expiry_cleanup()      → ShortTermSignal only    ✗       │
        │   update_pro_system_outcomes() → THE ONLY AUDITOR,            │
        │                                  ZERO CALLERS        ✗        │
        │                                                               │
        │   ►► NO AUTOMATED MONITORING OF ANY KIND ◄◄                   │
        │   Live price is fetched on demand, for display only.          │
        └───────────────────────────────┬───────────────────────────────┘
                                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ TARGET / STOP                                                 │
        │   target (close×2.0) and stop_loss (close×0.5) are stored     │
        │   and displayed. Nothing ever compares price against them.    │
        └───────────────────────────────┬───────────────────────────────┘
                                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ EXIT                                                          │
        │   No automated exit path exists.                              │
        │                                                               │
        │   Documented (string only, never executed):                   │
        │     • "Hold for 1-2 years (max 2 years)"                      │
        │     • "Exit early if trend breaks below 200 EMA"              │
        │     • "Exit early if ROE drops below 10%"  (ROE never fetched)│
        │                                                               │
        │   Actual mechanism: a human reads the Telegram holdings       │
        │   block and acts in their broker terminal. Closing the        │
        │   record requires a manual DB / Django-admin edit.            │
        └───────────────────────────────┬───────────────────────────────┘
                                        ▼
                            status = ACTIVE, indefinitely
```

---

# APPENDIX A — Short-Term vs Long-Term, side by side

| Dimension | Short-term swing | Long-term |
|---|---|---|
| Model | `ShortTermSignal` (own table, 14 statuses) | `SignalHistory(category='long_term')` (6 statuses) |
| Scheduled job | Yes — `trade_engine_scanner_10am` @ 10:00 | **No** — runs inside the short-term Telegram formatter |
| Runs when? | Every trading weekday | Only when the strict short-term pass is empty |
| Universe | Nifty 500 | Nifty 500 |
| Prefilter | `change_pct > 0.5` AND `vol > 50k`, top **50 by change_pct** | `vol > 50k` AND `change_pct > −5.0`, top **75 by volume** |
| Candle window | 365 d, `len ≥ 100` then `len ≥ 201` | 200 d, `len ≥ 100` |
| Gates | **6** (trend, ADX, RS, breakout, volume, liquidity) | **1** (`close > EMA50 > EMA200`) |
| Indicators | EMA20/50/200, ADX14, RSI14, ATR14, 52wH, 20dH, vol ratio, RS | EMA50/200, ATR14, 100-day return |
| Scoring | 5-component 0–100 `ai_score`, min 25 | **None** |
| Ranking | `ai_score` desc, no cap on picks | `perf_100d` desc, **top 5** |
| Initial status | `PENDING` | `ACTIVE` |
| Entry trigger | `ltp <= entry_price` (pullback), checked 12×/day | **None** |
| Stop loss | `entry − 2×ATR`, 10 % floor | Computed `entry − 3×ATR`/15 % floor, **stored as `entry × 0.5`** |
| Targets | T1/T2/T3 at 2R/3R/4R | One, computed 2.5R, **stored as `entry × 2.0`** |
| Trailing stop | SL→entry at T1/T2; daily 20-EMA close exit | **None** |
| Time stop | ATR-derived 15–90 days | **None** (2-year rule is a string) |
| Exit monitoring | Once daily @ 15:25 | **Never** |
| Terminal state | `ARCHIVED` + 28-day cooldown | **None — stays ACTIVE forever** |
| Audit trail | `TradeHistory` on every transition | **None** |
| Telegram events | 10 distinct `event_type`s | 0 — two blocks inside the short-term message |
| Fundamentals | None (`fundamental_score = 10.0`, unused) | None (`roe`, `debt_to_equity`, `profit_growth` are hardcoded constants) |
| Sectors | None | None (literal `"Large Cap"`) |
| Position sizing | Client-side, 1.5 % of capital | Three conflicting conventions (₹1,00,000 / capital÷N / ₹5,000 risk) |

---

# APPENDIX B — Dead / Divergent Code (factual inventory)

Every item verified by grepping the whole non-venv tree for call sites.

| Item | File:line | Status |
|---|---|---|
| `pro_system_service.update_pro_system_outcomes()` | `:604` | **No callers.** The only implementation of long-term target/SL auto-close. Removed from `run_periodic_scanners` and from `get_pro_performance_report` to fix *short-term* duplicate-Telegram bugs; long-term exit automation was lost as collateral damage. Also uses naive `datetime.now()` instead of `timezone.now()`. |
| `pro_system_service.get_pro_system_data()` | `:470` | Reachable only via `cron-trigger?action=short_term_scan` — but it is the writer that produced **every long-term row currently in the database**. Its date-scoped `update_or_create` lookup makes it re-attempt a `create()` on every later run for an already-ACTIVE symbol, hitting the `unique_live_signal` partial index with an uncaught `IntegrityError` — see §17.3. It is also the **only** writer that persists the ATR-derived SL/target. |
| `pro_system_service.scan_short_term_stocks()` | `:256` | Legacy parallel short-term scanner; same manual-only reachability. |
| ATR-derived `stop_loss` / `target` in `_fetch_long_term_quality` | `:380-386` | Computed on every run, returned in the dict, then **discarded** by the live writer (`trade_engine.py:1206-1207`) in favour of `×0.5` / `×2.0`. |
| `"sector": "Large Cap"` | `:389` | Hardcoded literal. Propagates to `reason`, the Telegram block, and the UI's Reason column. |
| `"roe": 22.5`, `"debt_to_equity": 0.2`, `"profit_growth": 15.0` | `:391-393` | Hardcoded, comment `# Quality proxy defaults`. |
| `"roe": 25.0`, `"debt_to_equity": 0.1`, `"revenue_growth": 15.0`, `"profit_growth": 12.0` | `:570-573` | A **second, different** set of hardcoded constants in the UI formatter. |
| `"revenue_growth": round(perf_100d, 1)` | `:394` | Field name has nothing to do with revenue. It is the ranking key. |
| `"mcap": int(close * vol_20d)` | `:391` | Turnover proxy, never filtered on, never persisted, zeroed in the UI formatter. |
| `hold_rule` / `entry_plan` strings | `:399-400`, `:575-576` | Display copy. Neither the 200-EMA exit, the ROE<10 % exit, the 2-year cap, nor the 30/30/40 tranching is implemented. |
| `_scan_new_long_term_setups` docstring | `trade_engine.py:1185` | Says *"Scan Nifty 50"*; it scans Nifty 500. |
| `ThreadPoolExecutor` import | `pro_system_service.py:14` | Imported, never used. |
| Per-symbol `get_token_map([symbol])` | `pro_system_service.py:355` | Re-resolves a token the caller already has (in-memory, but redundant 75×). |
| `SignalHistory.metadata` | — | Left `{}` for long-term rows; no computed indicator survives the scan. |
| `long_term.analytics.win_rate` | `trade_engine.py:1498` | Structurally always `0.0` — `hit_target` and `hit_sl` are never populated. |
| `long_term.tabs.pending` | `trade_engine.py:1479` | Structurally always empty — rows are born `ACTIVE`. |
| `_aggregate_lt` P&L | `pro_system_service.py:861-867` | Computes `pnl_amt`/`pnl_pct` only for `HIT_TARGET`/`HIT_SL`/`EXPIRED` with an `exit_price` → always `0`. |
| `DashboardSummaryView` long-term query | `views.py:450-453` | Filters `status__in=[ACTIVE, PENDING]`; only ACTIVE ever matches. |
| `TradeHistory` for long-term | `models.py:370` | `trade` is an FK to `ShortTermSignal` — long-term rows cannot be audited by it. |
| `Notification.objects.create` | — | Appears nowhere in the codebase. The in-app bell is always empty. |

---

*Generated by reading the code at commit `bb7c962` plus the uncommitted working-tree changes.
Every claim above is traceable to a file and line. The two items explicitly marked as
code-reading observations (`MultipleObjectsReturned` in §5/§14.1 and `FieldError` in §17.3)
were not executed against a live database; everything else is a direct reading of control flow.
Where the implementation is silent, this document says so rather than guessing.*
