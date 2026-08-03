# TradePulse AI — Signal Engines: How They Work Today (updated 2026-07-31)

**Purpose of this file:** a plain-English, example-driven walkthrough of all five
live stock/option signal engines. Originally written 2026-07-27 after a candle-data
debugging session; updated 2026-07-31 to add the specialist/strangle engine (missing
from the original pass) and reflect the NIFTY50 → NIFTY100 universe switch made this
session. Treat it as a snapshot of the code at time of writing, not a design doc —
if it disagrees with the code later, trust the code.

---

## 0. The one thing to understand first

**Storing a candle and generating a signal are two separate steps**, always. A
scheduled job reads whatever's in the `CandleBar` table (or fetches fresh via REST)
and *only then* runs the trigger logic that decides BUY/SELL. Nothing about *how* a
candle got stored (WebSocket tick vs REST fetch) changes what counts as a signal.

| # | Category | Live engine | Schedule | Candle source | Storage model |
|---|---|---|---|---|---|
| 1 | Intraday equity BUY/SELL | `intraday_service.py` | Every 15 min, 9:00 AM–3:15 PM IST | **WS ticks only** (local `CandleBar`, zero REST) | `SignalHistory(category="intraday")` |
| 2 | Option buying (CE/PE) | `option_buying_service.py` | Every 15 min, 9:00 AM–2:30 PM IST (audit-only after) | **WS ticks only** (same as above) | `SignalHistory(category="option_buying")` |
| 3 | Specialist (strangle selling) | `delta_hedge_service.py` | Heavy scan once/day at 10:45 AM; cheap "update" the rest of the day | Mix of `CandleBar` (VWAP/value-area) + live option-leg quotes | `SignalHistory(category="specialist")` |
| 4 | Short-term stocks | `trade_engine.py` (primary) + `pro_system_service.py` (secondary) | 10:00 AM daily, + external cron-job.org trigger | REST, **cache-first via `candle_store`** | `ShortTermSignal` |
| 5 | Long-term stocks | `pro_system_service.py::scan_long_term_stocks()` | Side effect of #4's runs (no cron of its own) | REST, **cache-first via `candle_store`** | `SignalHistory(category="long_term")` |

There is also a **V2 replacement** for #4/#5 (`swing_service.py`, profiles `SWING` /
`LONG_TERM`) — it also uses `candle_store`, but it is **shadow-only**: it runs at
4:05 PM, logs what it would have done, and persists nothing. `LONG_TERM` isn't
scheduled at all yet. Not live — described in §6 for completeness.

**Universe, as of 2026-07-31:** all 5 engines now scan the same live-fetched
**NIFTY100** list (100 symbols, confirmed by direct fetch — see §8). Before this
session every engine independently fetched its own copy of a NIFTY50 list (4 separate
implementations); they now all delegate to one function,
`signal_utils.fetch_nifty_symbols_live("NIFTY100")`, so an NSE index reconstitution
(a symbol swap) reaches every engine within 24h automatically instead of needing 4
separate code changes.

---

## 1. Intraday stock BUY/SELL

**Data layer:** `tick_aggregator.roll_up_universe()` (`market_data/tick_aggregator.py`)
runs every 30s, reading live WebSocket ticks already sitting in memory
(`angel_one_streamer`) — zero network calls. Every time a 5-minute window rolls over,
it writes the just-closed bar to `CandleBar`. The still-forming bar is never persisted.

**Scan (`intraday_service.get_live_signals()`, every 15 min):**

Example — RELIANCE, 9:35 AM:
1. Reads the last 3 days of 5-min bars for RELIANCE from `CandleBar` — purely local, no REST (deliberate, so this scan can't trip Angel One's `getCandleData` rate limiter).
2. Drops the last row as a safety margin, then computes Volume Profile on what's left: POC = ₹2,840, VAH = ₹2,850, VAL = ₹2,825, ATR = ₹9.
3. Latest usable bar: close ₹2,847, volume 1.8× the 20-bar average. Price has crossed above POC with `vol_ratio 1.8 > 1.2` → **POC Flip** trigger fires, BUY, score 4.5.
4. SL = `min(VAL, entry − 0.8×ATR)` = `min(2825, 2847 − 7.2)` = **2,839.8**. Target = `entry + 2R` = **2,861.4**. RR = 2.0 → passes the 1.5 minimum. Cost gate passes (0.5% move clears 3× the 0.14% round-trip friction).
5. Candidate is ranked against everything else that fired this cycle; top 5 survive and get written as `SignalHistory` rows with `status=PENDING`.

**Outcome tracking (separate job, `update_signal_outcomes`):** watches PENDING/ACTIVE
rows. Price touching 2,847 flips it ACTIVE. From there it scans 1-min bar highs/lows
(not raw LTP) for a touch of 2,861.4 (HIT_TARGET) or 2,839.8 (HIT_SL). No touch within
8 bars (40 min) → EXPIRED (time stop). Still open at 3:20 PM → force-closed EXPIRED.

---

## 2. Option buying (CE/PE)

Same `CandleBar` source as intraday (same tick aggregator, same 5-min bars), but its
own stricter breakout check (`_option_breakout_logic`) and its own risk math in
premium space.

Example — INFY, spot ₹1,850, 10:15 AM:
1. Price crosses above VAH, `vol_ratio 1.6 > 1.5`, price above VWAP, ADX 24 (> 20) → **BUY_CE** fires.
2. Strike selection: nearest ATM strike (1,850), then checks delta — needs 0.40–0.60. The 1,850 CE's delta is 0.48 → qualifies. (A breakout with a 0.25-delta strike would be rejected even though the breakout itself was valid — buyers need the option to actually track the underlying.)
3. Entry = live premium fetched now, ₹22.50. No PENDING wait — options start ACTIVE immediately.
4. Target = `1.6×–2.0× entry`, scaled by ADX strength; ADX 24 → ≈1.8× → target ≈ **₹40.50**. SL = fixed `0.625× entry` ≈ **₹14.06** (doesn't scale with trend strength — premium/theta risk is what it is regardless of how strong the breakout looked).
5. Capped at 3 signals per scan.

**Exit (`update_option_buying_outcomes`, self-contained):** closes on a premium
cross of target/SL, or **force-closes at 2:30 PM regardless of P&L** — every extra
minute past that is pure theta decay with no upside. Generation itself also stops at
2:30 PM (`OPTION_BUYING_TIME_STOP`); the engine still audits open positions after that.

---

## 3. Specialist (strangle selling)

The only engine that **sells** option premium instead of buying/going long — a market-
neutral short strangle (sell 1 OTM CE + 1 OTM PE on the same underlying), collecting
theta decay rather than betting on direction. Lives entirely in `delta_hedge_service.py`,
separate from `option_buying_service.py`'s CE/PE-buying logic (opposite trade direction,
different file, different risk model).

Example — HDFCBANK, 10:45 AM:
1. Universe: same live NIFTY100 list as every other engine (`NIFTY_50_STOCKS()` — name
   kept from before this session's NIFTY100 switch to avoid touching call sites).
2. For each candidate not already tracked: pulls `CandleBar` state for VWAP/value-area
   positioning, then probes up to 25 OTM CE + 25 OTM PE strikes to find a pair with
   balanced premium (`find_equal_premium_pair`) — deep-OTM/high-theta by design, the
   opposite selection criterion from option-buying's 0.40–0.60 delta band.
3. Expected-move floor pushes strikes further out if spot sits too close to the
   selected strike; a gamma guard rejects the trade outright if days-to-expiry ≤ 1
   (unmanaged final-day gamma risk).
4. Entry is immediate at live combined premium (both legs), same as option buying —
   no PENDING wait.
5. Runs the heavy full scan once daily at 10:45 AM (`run_10am_strangle_scan`); the rest
   of the day only cheap "update"-mode P&L/exit checks run, not a fresh symbol scan.

---

## 4. Short-term stocks

Two live producers exist side by side, both using `candle_store` instead of raw
`svc.get_candle_data()`:

**(a) `trade_engine.py::run_daily_scanner()` — the primary path, 10:00 AM daily cron
(`trade_engine_scanner_10am`).**

Example — 10:00 AM scan:
1. Pulls live quotes for the NIFTY100 universe, pre-filters to `change_pct > 0.5%` and `volume > 50,000` → say 45 candidates survive, sorted by change%, top 50 kept (the cap rarely binds now that the universe itself is only 100 names).
2. Fetches NIFTY's own 365-day daily history once (`candle_store.get_candles(..., lookback_days=365)`) — cache-first: first run of the day does a real REST fetch, every scan after that (this one and tomorrow's) only pulls the missing tail.
3. For each surviving candidate, fetches its own 365-day daily bars the same cache-first way, then runs `_compute_ai_score(df, nifty_20d_ret)` — an EMA/RSI/trend-based score, not literally an ML model despite the name.
4. Candidates are ranked by AI score; for each one not already holding a PENDING/ACTIVE `ShortTermSignal`, a new PENDING row is created with entry/target/SL sized by the strategy's rules (15–90 day holding window).
5. A trailing-stop check later (`update_pro_system_outcomes`'s sibling logic) exits ACTIVE positions if the daily close drops below the 20-EMA — but note, this function has **zero callers** today (dead code) — so short-term ACTIVE positions are **not actually auto-exited on a trail stop** right now, only on hitting target/SL directly, or (if they were created as PENDING and never trigger) an expiry rule.

**(b) `pro_system_service.py::scan_short_term_stocks()` — a second, largely
parallel implementation**, triggered externally: cron-job.org hits
`/api/stocks/cron-trigger/?action=short_term_scan` on a schedule configured outside
this codebase (not in `updater.py`). Same shape (NIFTY100 → pre-filter by quotes →
daily bars → EMA/ADX/volume rules, top 40 kept), also cache-first via `candle_store`.
Its main side effect in production today is actually **triggering the long-term scan**
— see §5.

---

## 5. Long-term stocks

**There is no scheduled job for this at all.** `pro_system_service.scan_long_term_stocks()`
only runs as a side effect of formatting a Telegram message from one of the two
short-term paths above (`trade_engine._scan_new_long_term_setups()` after the 10 AM
scan, or `pro_system_service.get_pro_system_data(trigger_scan=True)` from the external
cron-job.org hit).

Example, when it does run:
1. Same NIFTY100 → quote pre-filter → top-75-by-volume shortlist (a cap that no longer
   binds much now the universe itself is only 100 names — see §8 change note).
2. For each candidate: `candle_store.get_candles(..., lookback_days=200)` (cache-first, was raw REST before this session's earlier candle_store migration). Requires `close > 50-EMA > 200-EMA` (structural uptrend) to qualify at all.
3. Momentum score from 100-day performance; SL = `3×ATR` (capped at 15% risk, wider than short-term's 10% — this is a position trade, not a swing trade), target = `2.5×` the SL distance.
4. "Quality" figures shown alongside (ROE, debt-to-equity, growth) are **hardcoded placeholder literals**, not real fundamentals data — flagged directly in `doc/long_term_stock.md`, not something introduced recently.
5. Persisted as `SignalHistory(category="long_term")`, ACTIVE once entered.

**Known gap:** once a long-term position is ACTIVE, nothing closes it.
`update_pro_system_outcomes()` — the only function that could — has zero callers, and
`live_signal_service.update_signal_outcomes()` explicitly excludes `category='long_term'`.
A long-term BUY signal today stays ACTIVE indefinitely unless someone builds/wires an
exit path. Documented pre-existing behavior (`doc/long_term_stock.md`).

---

## 6. The V2 replacement (shadow-only, not live)

`swing_service.py` is a from-scratch rewrite meant to eventually replace both
`trade_engine.py` and `pro_system_service.py`'s short/long-term logic, parameterized by
an `EngineProfile` (`SWING` = 15–90 day hold, `LONG_TERM` = 1–2 year hold, both defined
in `shared/profiles.py`). It's also been switched to `candle_store` today. Status:

- `SWING` profile: runs at **4:05 PM daily** (`swing_v2_shadow_1605`), `dry_run=True` —
  computes and logs candidates, persists nothing. Needs ~20 shadow sessions of proven
  output before cutover (per `doc/SHORT_TERM_ENGINE_V2_ARCHITECTURE.md`).
- `LONG_TERM` profile: not scheduled anywhere. Code path exists and is exercised by
  tests, but nothing calls it in production.

---

## 7. What changed 2026-07-27 (candle-store migration)

| File | What changed |
|---|---|
| `stocks/services/swing_service.py` | 3 call sites: `svc.get_candle_data()` → `candle_store.get_candles()` (cache-first). Covers both `SWING` and `LONG_TERM` V2 profiles (shadow-only, see §6). |
| `stocks/services/trade_engine.py` | 3 call sites converted the same way (live 10 AM short-term scanner + its EOD holding check). Removed a redundant `time.sleep(0.35)` per candidate — rate-limit safety is already enforced globally by `angel_one_service`'s `_REST_CALL_LOCK` (1.5s pacing), so this was extra latency with no added protection, and became pure waste on cache-hit days. |
| `stocks/services/pro_system_service.py` | 5 call sites converted — this file turned out to be the actual live long-term engine (`scan_long_term_stocks`) plus a second, largely-parallel short-term scanner (`scan_short_term_stocks`), neither obvious from the short-term/long-term docs alone until traced through `long_term_stock.md`'s call-chain notes. |
| `stocks/updater.py` | `run_candle_trickle_warmer()`'s per-symbol log bumped from `DEBUG` to `INFO`, so Render logs show which symbol each ~20s REST trickle cycle actually touched. |

Net effect: every REST-backed daily-bar fetch across short-term and long-term now
reuses the same local `CandleBar` store instead of re-pulling full history from Angel
One on every run. First run after a cold cache still pays the full REST cost; every
run after that only fetches bars newer than what's already stored.

---

## 8. What changed 2026-07-31 (this session)

Started from a Render log showing repeated `[ANGEL_ONE] WAF/Rate limit detected during
Candle fetch` errors. Root cause traced to `run_candle_trickle_warmer()` (a 2-min cache
warmer, unrelated to any of the 5 signal engines above) re-hitting Angel One on the
first tick after its own 5-min circuit-breaker cooldown expired, immediately re-tripping
it — a recurring ~6-minute cycle, not a one-off.

| Change | Files | Why |
|---|---|---|
| One shared NIFTY50→NIFTY100 fetch | `signal_utils.py` (`fetch_nifty_symbols_live`, pre-existing), `delta_hedge_service.py`, `market_data/download_queue.py`, `bhavcopy_service.py`, `shared/profiles.py` | Every engine independently re-implemented "fetch NIFTY50 from NSE" (4 separate copies, one of them a fully static hardcoded list that could never see an index reconstitution). Consolidated to one function first, then flipped its index parameter to NIFTY100 platform-wide — this is what made the 50→100 switch a 4-line diff instead of a file-by-file hunt. |
| CI test-collection landmine fixed | `stocks/tests_*.py` → `stocks/selfcheck_*.py` (10 files renamed), `.github/workflows/backend-tests.yml` | These are plain assert-and-`sys.exit(1)` scripts, not Django `TestCase`s, but their old `tests_*.py` names matched Django's test-discovery glob — meaning every `manage.py test stocks` run (local and CI) silently imported and executed all 10 as a side effect, and a `sys.exit()` mid-collection (a `BaseException`, not caught by unittest's loader) could abort the entire 52-test Django suite before it ran. Renamed off the glob; CI now runs them as an explicit, separately-attributed step instead. |

Universe count as of this session, confirmed by live fetch: **NIFTY100 = 100 symbols**
(`signal_utils.fetch_nifty_symbols_live("NIFTY100")`).
