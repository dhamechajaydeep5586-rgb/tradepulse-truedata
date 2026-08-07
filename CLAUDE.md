# TradePulse AI — Project Context for AI Assistants

> **READ THIS FILE FIRST** before making any changes to this codebase.
> This file captures the intended design, exact signal flows, and constraints agreed with the product owner.
> Violating any rule here will break the live application.

---

## Stack Overview

| Layer     | Technology |
|-----------|-----------|
| Backend   | Django 4.2 + Django REST Framework, Python 3.13 |
| Frontend  | React + Vite, Vanilla CSS + TailwindCSS |
| Market data | TrueData (WebSocket for live prices, REST for historical bars/quotes/option chain), NSE API (market status) |
| DB        | PostgreSQL (via Django ORM) |
| Cache     | Django cache (in-memory / Redis) |

> **Migrated from Angel One SmartAPI to TrueData** — see `doc/TRUEDATA_MIGRATION_PLAN.md`
> for the full rationale and phase-by-phase record. `angel_one_service.py` /
> `angel_one_streamer.py` no longer exist in this codebase; the equivalent files are
> `stocks/services/truedata_service.py` / `truedata_streamer.py`. The biggest behavioral
> difference: TrueData addresses everything by symbol NAME (e.g. `"RELIANCE"`,
> `"NIFTY 50"`, `"CRUDEOIL-I"`), not a broker-assigned numeric token — every `token`
> field flowing through this codebase (candle_store, market_data/gateway, universe
> liquidity stats, WS subscriptions) now holds that symbol string, not a numeric ID.

---

## Market Status — Single Source of Truth

**NEVER use the broker (TrueData) WebSocket pulse as the market-open gate.**

The authoritative market status comes from the **NSE API**:
```
GET https://www.nseindia.com/api/marketStatus
```
- Requires cookie bootstrap (GET `nseindia.com` homepage first, then the API).
- Cached for 60 seconds in `_NSE_STATUS_CACHE`.
- Fallback: static calendar + time window check if NSE API is unreachable.
- Weekend check (Sat/Sun) is always applied before calling the API.

The function `get_market_status(segment)` in `signal_utils.py` implements 3 layers:
1. Weekend guard (always)
2. NSE Live API (primary)
3. Static calendar fallback (secondary)

**Do NOT hardcode public holidays in `NSE_HOLIDAYS` as the primary gate — the NSE API is the truth.**

---

## Signal Lifecycle

All three engines use the same `SignalHistory` model with these status values:

```
PENDING  → ACTIVE → HIT_TARGET
                  → HIT_SL
       → CANCELLED  (never triggered — pending at cutoff)
       → EXPIRED    (was active but closed at auto-square-off)
```

### Auto Square-Off Rules (enforced in `live_signal_service.py → update_signal_outcomes`)
| Category       | Cutoff Time | PENDING action | ACTIVE action |
|---------------|-------------|----------------|---------------|
| intraday       | 3:20 PM IST | CANCELLED (no P&L) | HIT_TARGET/HIT_SL on a target/SL touch, detected by scanning **1-min bar high/low** (not an LTP snapshot); hard ±₹3,000 P&L cap on the position (`INTRADAY_HARD_PNL_CAP`) auto-closes as HIT_TARGET/HIT_SL regardless of how far price is from the level; anything still ACTIVE at cutoff force-closes as EXPIRED |
| commodity      | 11:15 PM IST | CANCELLED | HIT_TARGET/HIT_SL auto-closed on cross; else EXPIRED at cutoff |
| option_selling | 3:15 PM IST | CANCELLED | HIT_TARGET/HIT_SL auto-closed on cross; else EXPIRED at cutoff |

Every intraday exit records `metadata.exit_reason`: `LEVEL_HIT` (bar-confirmed),
`LEVEL_HIT_LTP` (degraded fallback when bar data is unavailable), `PNL_CAP_HIT`
(replaced the old 8-bar/40-min time stop 2026-08-07, account owner's request — a
position now rides toward target/SL with no time limit, but force-exits the instant
its rupee P&L hits ±₹3,000), `SQUARE_OFF_CUTOFF`, or `DAILY_LOSS_LIMIT`.

### Intraday Risk Controls (`intraday_service.py`)
Positions are sized by **risk, not notional**: `qty = (equity × risk%) / (entry − stop)`.
All limits are overridable via Django settings — see the constants block at the top of
`intraday_service.py`. **`INTRADAY_ACCOUNT_EQUITY` defaults to ₹5,00,000 and must be set
to the real account size**, since every position size derives from it.

A **daily loss limit** (−2% of equity, realised + unrealised) flattens all intraday
positions and blocks further generation for the session via the
`intraday_daily_loss_halt` cache key.

A **cost gate** rejects any signal whose target is under `3 × round-trip cost` (0.42% by
default). At 2:1 RR, a 0.16% stop against ~0.14% friction needs a ~62% win rate to break
even versus 33% gross — those trades lose on arithmetic regardless of signal quality.
Expect this to substantially reduce signal count; that is intended.

**Rationale for all of the above: `doc/INSTITUTIONAL_AUDIT_INTRADAY.md`.**

### Stale Signal Guard
- At the **start of every scan**, cancel any PENDING/ACTIVE signals from **previous trading days**.
- This prevents old signals accumulating across sessions.
- Commodity guard is in `commodity_service.py → get_commodity_signals()`.
- Intraday guard is in `intraday_service.py → get_live_signals()`.

### Production Scheduler — What Actually Triggers Generation
Every engine's `get_*_signals(action=None)` function auto-resolves `action` to `"generate"`
the first time today's signal for that category doesn't exist yet, else `"update"` — but
something still has to *call* that function on a schedule for signals to appear without a user
sitting on the page. That's `stocks/updater.py`'s APScheduler jobs, not the frontend:

| Job (`updater.py`) | Schedule | Calls | Generates |
|---|---|---|---|
| `live_signal_update_hourly` + `live_signal_update_final_hour` (both run `run_periodic_scanners`) | Every 15 min, **9:00 AM – 3:15 PM IST**, Mon–Fri | `intraday_service.get_live_signals()` **and** `option_buying_service.get_option_buying_signals()` together | Intraday equity BUY/SELL, option_buying CE/PE |
| `strangle_signal_10am` (`run_10am_strangle_scan`) | **10:45 AM IST**, Mon–Fri | `generate_strangle_signals` management command → `delta_hedge_service` | `specialist` category (strangle selling) only |

The 9:00/9:15 AM slots fire but produce nothing — intraday needs closed 5-min bars and the
9:15 slot is explicitly skipped for that reason — so **the first real signal-generation
opportunity is ~9:30 AM**, not 9:00 and *not* 11:00 AM. An earlier version of this schedule
did start at 11:00 AM; it was moved earlier specifically to stop missing the 9:15–11:00
window, historically the highest-volume part of the session. If you find a reference to an
11:00 AM start anywhere (docs, old comments, chat history), it's stale — trust the
`CronTrigger` in `updater.py`, not the description next to it.

Frontend polling (`LiveSignalsTable.jsx`, `OptionBuyingTable.jsx`, etc.) only ever calls its
API with `action="update"` (or no `force` param) — it reads current DB state, it does **not**
generate. The one exception is intraday's explicit **Force Scan** button (`?force=true`),
which bypasses the 5-min scan-rate cooldown but still can't generate outside market hours or
past the category's cutoff.

---

## 1. Intraday / Next-Day Signals

### Market Hours
- **9:15 AM – 3:30 PM IST**, Monday–Friday, NSE trading days only.
- Signal generation cutoff: **3:20 PM** (no new signals after this).

### Stock Universe
- **Nifty 50** (`INTRADAY.index` / `INTRADAY_UNIVERSE_INDEX` in `shared/profiles.py`,
  consumed by `shared/universe.py → get_trading_universe()`), fetched live from the NSE
  archive CSV and cached 6h, then liquidity-filtered (ADV, price, spread — see
  `shared/universe.py`). `signal_utils.py`'s `INTRADAY_UNIVERSE` constant and
  `load_symbols_from_stock_table()` are only the fallback path if `get_trading_universe()`
  returns no symbols; both are kept in sync with the same Nifty 50 tier. Narrowed
  NIFTY500 → NIFTY50 → NIFTY200 → NIFTY100 → **NIFTY50** (2026-08-05, account owner's
  request) — every engine (intraday, option buying, specialist/strangle, short-term,
  long-term) shares this one universe, fetched via `bhavcopy_service._fetch_nifty500_symbols()`
  and `market_data/download_queue.py`'s candle trickle-warmer, both also pinned to NIFTY50
  so backfilled candle data matches what's actually scanned. Falls back to the
  `IndexConstituent` table if NSE is unreachable.
- Only stocks NOT already having an ACTIVE/PENDING signal today are re-scanned.

### Scan Logic (`intraday_service.py → get_live_signals`)
1. Market open check (NSE API).
2. **Stale signal guard** — cancel previous-day signals.
3. **Scan rate guard** — if a scan ran in the last 5 minutes AND live signals exist → return DB payload immediately (no re-scan). Cache key: `intraday_last_full_scan`.
4. Compute NIFTY 50 trend (BULLISH / BEARISH / SIDEWAYS) via 15-min candles.
5. Cache sentiment in `intraday_nifty_sentiment` for cooldown returns.
6. Scan each symbol: fetch 5-min candles (2-day lookback), apply Volume Profile (VP) logic.
   **The still-forming last bar is discarded** — triggers run on closed bars only, so
   signals cannot repaint and live agrees with the backtest.
7. **Rank all candidates by score, then cap at 5** (`MAX_SIGNALS_PER_SCAN = 5`). The scan
   does not stop early: stopping at the first 5 meant emitting the first 5 symbols
   alphabetically rather than the 5 best.
8. Apply the gross-exposure cap, then persist via `engine_persist_live_signal_history()`.

### Signal Strategy
Three triggers, evaluated in priority order on **closed** bars (first match wins):
- **POC Flip** (score 4.5): price crosses the Point of Control with `vol_ratio > 1.2`
- **Value Area Breakout** (4.0): price breaks VAH/VAL with `vol_ratio > 1.5`
- **Value Area Rejection** (3.5): bounce off VAL / rejection at VAH, `vol_ratio > 1.1`.
  This is the only *mean-reversion* trigger, so it is the only one gated by the index
  `directional_bias` (BULLISH/BEARISH/SIDEWAYS from `get_standard_market_state`).

Risk: `SL = min(VAL, entry − 0.8×ATR)` for BUY (mirrored for SELL), `target = entry ± 2R`.
Candidates below 1.5 RR, or failing the cost gate, are discarded.

**Historical bug, fixed 2026-07-26:** `compute_session_vwap()` (`signal_utils.py`)
divides by cumulative volume, and the NIFTY 50 index reports **zero volume on every
candle** (only its constituents are traded, not the index itself). That made every
call on NIFTY data return `NaN`, which propagates through `np.sign()`/`np.clip()` in
`get_regime()`'s trend-score math (`shared/regime.py`) and makes every threshold
comparison evaluate `False` — silently collapsing `directional_bias` to `SIDEWAYS`
regardless of the real market. Fixed with an unweighted-average fallback when volume is
zero. Verified against real NIFTY history: previously 100% SIDEWAYS across 3,325 bars
tested, now a believable ~42% SIDEWAYS / 29% BEARISH / 29% BULLISH split. If you see
`directional_bias` behaving suspiciously flat again, check this function first.

### Frontend (`LiveSignalsTable.jsx`)
| Timer | Interval | What it does |
|-------|----------|-------------|
| Signal refresh | **5 min** | Fetches full signal list from `/api/stocks/live-signals/` |
| Price ticker | **1 sec** | Polls `/api/stocks/live-price-updates/?symbols=...` for ACTIVE/PENDING signals only (≤5 symbols) |

**Rules:**
- Price ticker ONLY polls `status IN [ACTIVE, PENDING]` — not CLOSED/EXPIRED signals.
- Signals remain visible until status = HIT_TARGET / HIT_SL / CANCELLED / EXPIRED.
- Initial page load fetches current DB state (does NOT force a re-scan).
- Force Scan button triggers `?force=true` which bypasses the 5-min cooldown.

---

## 2. Option Buying

> **Superseded `option_sniper_service.py` (deleted).** That file's `get_option_sell_signals()`
> (SELL CE/PE near VAH/VAL for theta decay) no longer exists in the codebase at all — not
> renamed, not merged, just gone. The current option-buying engine (`option_buying_service.py`)
> is a different strategy: it **buys** near-ATM CE/PE on a confirmed breakout, the opposite
> trade direction from what this section used to describe. If old context/docs/chats reference
> "option selling sniper," "SELL CE," or `option_sniper_service`, treat that as stale.
>
> There is a genuinely separate, still-active **strangle-selling** engine
> (`delta_hedge_service.py`, category `specialist`) — that one does sell option premium, is
> generated once daily by `run_10am_strangle_scan` at **10:45 AM IST**, and is rendered by
> `DeltaHedgePanel` on `OptionSellingFull.jsx` ("Option Selling (Strangle)" page). It is not
> documented in detail here — this section covers option **buying** only.

### Market Hours
- Follows the same NSE market-open gate as intraday (9:15 AM – 3:30 PM IST window), but signal
  *generation* stops earlier — see the hard time-stop below.
- **Hard time-stop: 2:30 PM IST** (`OPTION_BUYING_TIME_STOP` in `signal_utils.py`). Past this
  time, `get_option_buying_signals()` skips generation entirely and only audits/exits existing
  positions — a decaying option opened this late has no time left to work before end-of-day risk
  management would need to force it closed anyway.

### Stock Universe
- F&O-eligible stocks (`svc.get_fo_stocks()`), capped at 40 candidates per scan
  (`candidate_limit`, matches `MARKET_RULES`).
- Filters out symbols already holding an ACTIVE/PENDING `option_buying` signal.

### Scan Logic (`option_buying_service.py → get_option_buying_signals`)
1. Static + live NSE market-open check.
2. **Hard time-stop check** (2:30 PM) — past this, audit-only (see Market Hours above).
3. `action` router: `None` auto-resolves to `"generate"` the first time today's `option_buying`
   signal doesn't exist yet, else `"update"` — same pattern as `intraday_service.get_live_signals()`.
4. **Stale signal guard** — cancels PENDING/ACTIVE `option_buying` rows from previous days.
5. **Scan rate guard** — 5-min cooldown, cache key `option_buying_last_full_scan`.
6. Batch-resolve tokens then batch-fetch quotes **once** for the whole scan via
   `get_bulk_quotes()` (chunks of 50) — not one REST call per symbol.
7. Per symbol: fetch `FIVE_MINUTE` candles (2-day lookback — same interval/lookback as
   `intraday_service`, so `get_candle_data()`'s cache transparently dedupes the REST call
   for any symbol both scanners touch in the same cycle).
8. Breakout logic (`_option_breakout_logic`, in `option_buying_service.py` — deliberately
   **not** shared with `intraday_service._volume_profile_logic`, since a direction signal for
   options needs stricter confirmation than an equity entry does):
   - **BUY_CE**: price crosses above VAH, `vol_ratio > 1.5`, price above VWAP, ADX > 20
   - **BUY_PE**: price crosses below VAL, `vol_ratio > 1.5`, price below VWAP, ADX > 20
   - `relaxed=True` fallback (triggered if nothing has qualified in the last 90 min, or by
     noon on a quiet day) lowers these to `vol_ratio > 1.2`, ADX > 15.
9. Strike selection (`select_option_buying_strike`): nearest ATM strike, then requires the
   resulting **delta between 0.40–0.60** — buyers need the option to actually track the
   underlying, unlike strangle-selling's deep-OTM/high-theta target band. Rejected if outside
   that range even if the breakout itself qualified.
10. Target/SL (`_compute_target_sl`): **fixed 2-lot rupee amounts** — target +₹5,000, SL
    −₹2,500 (clean 1:2 reward:risk), converted to a premium-price delta via the symbol's
    real lot size. Replaced the old ADX-scaled 1.6×–2.0×-entry target / fixed 0.625×-entry
    SL formula 2026-07-31 at the account owner's request, after that formula let one real
    position (SUNPHARMA CE) run to +₹8,785 unrealized with no exit condition anywhere near
    that level before decaying back to a loss by the 2:30 PM time-stop. See
    `doc/OPTION_BUYING_PIPELINE.md` for the full pipeline writeup.
11. Cap: **3 signals max** per scan (`MAX_OPTION_BUY_SIGNALS_PER_SCAN`).
12. Entry is immediate at the live premium already fetched — unlike equity signals, there's no
    PENDING state to wait through; a persisted option-buying signal starts ACTIVE.

### Exit / Auditing (`update_option_buying_outcomes`)
- **Self-contained**, deliberately excluded from the shared `update_signal_outcomes()` (same
  precedent as `delta_hedge_service`) — premium-space math and the hard time-stop don't fit
  that function's equity/commodity-oriented branches.
- Per active signal: fetch live premium via `svc.get_option_quote()`, close as `HIT_TARGET` /
  `HIT_SL` on a cross, or **force-close at 2:30 PM** regardless of P&L (`HIT_TARGET` if premium
  ≥ entry, else `HIT_SL`) — every extra minute open past the time-stop is pure theta cost.

### Frontend (`OptionBuyingTable.jsx`, `pages/OptionBuying.jsx`)
| Timer | Interval | What it does |
|-------|----------|-------------|
| Signal refresh | **5 min** (open) / 30 min (closed) | Polls `/api/stocks/option-buying/` |

`OptionBuyingView` always calls `get_option_buying_signals(action="update")` — this endpoint
is **read-only**, same as intraday's frontend. It never triggers generation; only the backend
scheduler (see "Production Scheduler" below) does that.

---

## TrueData API — Critical Rules

Full endpoint reference: `TrueDataAPIDocument/` (Market Data API v2.6 PDF is the primary
spec; TD Postman collections have exact request shapes). `doc/TRUEDATA_MIGRATION_PLAN.md`
has the phase-by-phase migration record and the exact Angel One → TrueData mapping.

### Symbol addressing (the load-bearing design decision of this migration)
TrueData addresses every symbol by **name**, not a broker-assigned numeric token — there is
no separate token-resolution step. `truedata_service.get_token_map()` is therefore an
**identity map** (`{symbol: symbol}`), and every `token` field elsewhere in this codebase
(candle_store, `market_data/gateway.py`, WS subscriptions, `shared/universe.py`) holds that
symbol string. Nothing in this codebase ever parses `token` as an int — verified before
making this the design — so this required zero call-site logic changes, only import swaps.
Indices use their plain NSE name too: `"NIFTY 50"`, `"NIFTY BANK"`, `"INDIA VIX"` (the last
is a naming-convention guess, not confirmed against a live symbol master — check
`getAllSymbols?segment=in` if it returns no data).

### Auth
- `POST https://auth.truedata.in/token` (OAuth2 password grant: `username`, `password`,
  `grant_type=password`) → bearer token, `expires_in` ≈ 13961s (~3.9h). TrueData also
  renews the underlying session daily near 4am regardless — `truedata_service.py` re-auths
  proactively 5 minutes before either boundary (`_ensure_fresh_token()`), not on a fixed
  wall-clock guess like Angel One's old 18h reuse window.
- WebSocket auth is separate and simpler: query-string `user`/`password` directly on the
  `wss://push.truedata.in:<port>` URL. Sandbox port **8086**, production port **8084**
  (`TRUEDATA_WS_PORT` in settings).
- Only **one WebSocket session per login** — a second connection attempt gets
  `"User Already Connected"`. There's a documented force-logout REST endpoint
  (`GET https://api.truedata.in/logoutRequest?user=...&password=...&port=...`) if a dirty
  disconnect leaves a stale session; not currently wired into this codebase.

### Rate limits (documented directly, not forum-sourced like Angel One's were)
| Endpoint class | Documented limit |
|---|---|
| Tick history (`getticks`, `getlastnticks`) | 5 req/sec, 300/min, 18000/hour |
| Bar history (`getbars`, `getlastnbars`, `getAllBars*`) | 10 req/sec, 600/min, 18000/hour |

A rate-limit breach comes back as **plain text in an HTTP 200 body** ("API calls quota
exceeded! maximum admitted 1 per Second."), not a distinct HTTP status code — see
`truedata_service._is_quota_exceeded()`, checked alongside 403/429 in every REST call site.
As with the old Angel One integration, this codebase paces well under the documented
ceiling (~1 req/sec globally via `_REST_CALL_LOCK`) rather than targeting it.

### REST call serialization
Every TrueData REST call goes through one choke point —
`TrueDataService._rest_request(method, url, **kwargs)` — behind a module-level
`_REST_CALL_LOCK` wrapping pacing *and* the request itself (not just the sleep), with one
automatic retry on `SSLError`/`ConnectionError`. This is a direct carryover of the pattern
Angel One's integration converged on after two separate production incidents (a same-socket
race condition, then a stale-pooled-connection issue neither the lock alone nor the retry
alone fully fixed — see git history / `doc/TRUEDATA_MIGRATION_PLAN.md` if you need the full
incident writeup). Applied here from day one rather than waiting to rediscover it.

### Login concurrency
Same `_AUTH_LOCK` pattern as the old Angel One integration: `initialize_truedata()`
serializes its whole check-then-login sequence under the lock, and re-auth triggered by a
near-expiry token happens through the same guarded path — never call `.authenticate()`
directly from a request-thread code path outside that lock.

### Historical candle API
- Interval names differ from Angel One's (`ONE_MINUTE` etc. still work as the *argument*
  callers pass — `truedata_service._INTERVAL_MAP` translates to TrueData's own names
  (`1min`, `5min`, ..., `eod`) at the boundary, so no call site needed to change).
- `get_candle_data()` returns TrueData's CSV bar response reshaped into the same
  `Open/High/Low/Close/Volume`-indexed-by-`Datetime` DataFrame shape the rest of the
  codebase already expects.

### WebSocket message shapes (see truedata_streamer.py)
- Plain JSON over the wire — no binary tick format to decode (unlike Angel One's SDK).
- Subscribe via `{"method": "addsymbol", "symbols": [...]}` — symbol names directly, no
  exchange-type code needed (TrueData symbols are already segment-qualified, e.g.
  `"CRUDEOIL-I"` vs `"RELIANCE"`).
- Incoming ticks (`"trade"` messages) are keyed by TrueData's own numeric Symbol ID, which
  is only revealed in the touchline/addsymbol response — `truedata_streamer.py` keeps a
  `symbolid -> symbol_name` map built from that response to translate ticks back onto the
  symbol-string keys the rest of the app expects.

### Price Units
- **NSE stocks**: prices in Rupees (already normalized by the service layer).
- MCX/commodity functionality was removed platform-wide before this migration (see the
  "Common Bugs" doc-hygiene entry below) — there is no live MCX code path to have unit
  conventions for. If MCX trading is ever reintroduced, confirm TrueData's rupee-vs-paise
  convention for that segment before trusting any price from it; don't assume it matches
  Angel One's.

---

## Signal Persistence Layer

`engine_persist_live_signal_history(result, category, rules)` in `trading_engine/state_engine.py`:
- Checks for duplicate signals (unique constraint: symbol + category + PENDING/ACTIVE).
- Enriches with RR ratio, entry tolerance, etc.
- Notifies via WhatsApp if configured.

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `backend/stocks/services/signal_utils.py` | Market status (NSE API), holiday calendar, technical indicators |
| `backend/stocks/services/intraday_service.py` | Intraday scan engine |
| `backend/stocks/services/option_buying_service.py` | Option buying scan engine (BUY CE/PE on breakout) |
| `backend/stocks/services/delta_hedge_service.py` | Strangle-selling (`specialist` category) scan engine |
| `backend/stocks/services/live_signal_service.py` | Signal outcome auditor, auto square-off |
| `backend/stocks/services/truedata_service.py` | TrueData REST integration (candles, quotes, option chain) |
| `backend/stocks/services/truedata_streamer.py` | TrueData WebSocket integration (live ticks) |
| `backend/stocks/serializers.py` | API response serialization (adds live price, signal metadata) |
| `backend/stocks/views.py` | REST API endpoints |
| `frontend/src/components/LiveSignalsTable.jsx` | Intraday signals UI |
| `frontend/src/components/OptionBuyingTable.jsx` | Option buying UI |
| `frontend/src/components/DeltaHedgePanel.jsx` | Strangle-selling (specialist) UI |

---

## Common Bugs & Their Fixes (History)

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| Market always showing CLOSED | Angel One WebSocket pulse returning None → used as hard gate | Replaced with NSE API as truth source |
| Wrong holiday (April 6 blocked) | Hardcoded `date(2026, 4, 6)` in NSE_HOLIDAYS | Removed; NSE API is the truth |
| Crude Oil showing ₹10,38,400 | Stale signal from April 3 (previous paise-unit bug era) persisted into new session | Added stale signal guard to cancel previous-day signals at scan start |
| 121 signals shown | No signal cap + no cross-day cleanup | Added MAX_SIGNALS_PER_SCAN=5, stale signal guard |
| Option selling always empty | `from_date = now - 2 days` → hits weekend → only 4 candles | Changed to 7-day lookback |
| 403 rate limit errors | Live price poller hitting 500+ symbols every 1 second | Poller now only polls ACTIVE/PENDING signals (≤5 symbols) |
| `UnboundLocalError: now_ist` | `now_ist` defined after code that used it | Moved definition to top of `get_live_signals()` |
| Recurring 403 on Angel One login + intraday scan degraded to ~2 symbols | No lock around login (`initialize_angel_one` / AG8001 retries) — concurrent gthread/APScheduler threads raced into simultaneous login calls, tripping Angel One's own rate limit, which cascaded into the candle circuit breaker and starved the universe filter's liquidity stats | Added `_AUTH_LOCK` + `_reauthenticate_locked()` double-checked-lock helper (see "Login Concurrency" above) |
| (N/A — doc hygiene) Stale "Commodity Signals" docs referencing a deleted feature | MCX/commodity functionality (`commodity_service.py`, `CommoditySignalsTable.jsx`, Crude Oil/Natural Gas signals) was removed platform-wide in commit `04746d2` | Deleted the "Commodity Signals" section from this file; if you're looking for MCX/commodity logic, it no longer exists anywhere in the codebase |

