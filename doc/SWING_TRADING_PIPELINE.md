# Swing Trading Pipeline — Full System Review

> Engine: `backend/stocks/services/trade_engine.py`
> Model: `ShortTermSignal` (`backend/stocks/models.py`)
> Scheduler: `backend/stocks/updater.py` → `start()` → "TRADE ENGINE PIPELINE JOBS"
> Telegram channel: `TELEGRAM_SHORT_TERM_CHAT_ID` (intended — see **Known Issues**, not always honored)

This document traces the swing trading system from stock universe selection through signal generation, activation, exit, and Telegram notification — as the code actually behaves today, including known bugs.

---

## 1. Universe & Market Direction

**Universe**: Nifty 500, fetched live via `_fetch_nifty500_symbols()` (`bhavcopy_service.py`). Falls back to a hardcoded `NIFTY500_FALLBACK` list (~180 symbols, `pro_system_service.py:33-78`) if the live fetch fails.

**Market direction** — `get_market_direction()` (`pro_system_service.py:134-164`):
- Pulls Nifty 50 index daily candles (Angel One, token `99926000`), 120-day lookback, needs ≥51 rows.
- `BULLISH` if `close > EMA20 AND close > EMA50`, else `BEARISH`. (No `SIDEWAYS` state here — falls back to `NEUTRAL` only if data is unavailable.)

**Gate**: `run_daily_scanner()` skips the entire scan if the market is `BEARISH` and the run isn't `relaxed` — it still sends a "no setups" Telegram message in that case.

---

## 2. AI Scoring (`_compute_ai_score`, `trade_engine.py:165-309`)

Every candidate must pass **all** of these hard filters or it's dropped (relaxed mode loosens some thresholds):

| # | Filter | Normal threshold | Relaxed threshold |
|---|---|---|---|
| 1 | Data sufficiency | ≥201 daily bars | same |
| 2 | Trend structure | `close > EMA50 > EMA200` | same |
| 3 | ADX(14) | ≥ 25.0 | ≥ 15.0 |
| 4 | Relative strength vs Nifty (20d return) | stock ≥ Nifty | *skipped* |
| 5 | Proximity to 52w high | within 5% or new 20d high | within 10% |
| 6 | Volume | 5d avg ≥ 1.5× 20d avg | ≥ 1.0× |
| 7 | Liquidity floor | today's volume ≥ 100,000 | same |
| 8 | Risk/Reward | Target1 R:R ≥ 2.0 | same |
| 9 | Composite AI score | ≥ 25.0 | same |

**Trade levels**: `entry = close`. `stop_loss = entry − 2.0×ATR(14)`, floored to max 10% away from entry. `target1/2/3 = entry + (2.0 / 3.0 / 4.0) × SL-distance`.

**AI score composition** (0–100, weighted sum of 5 components — Trend 25, Momentum 25, Volume 20, Sector/RS 15, Risk 15). A 6th component, "Fundamental Score," is computed as a fixed placeholder value of 10 and shown in the payload/Telegram text, but is **not** added into the actual `ai_score` total — it's cosmetic only.

---

## 3. Daily Scanner — `run_daily_scanner()` — 10:00 AM

1. Market direction gate (§1).
2. Pull Nifty 500 universe, resolve tokens, bulk-quote in chunks of 50.
3. Pre-filter: keep symbols with `change% > 0.5` (or `> -2.0` relaxed) **and** `volume > 50,000` (or `>10,000` relaxed); sort by change% descending, keep top 50.
4. Fetch Nifty's 20-day return as the relative-strength baseline.
5. For each of the ≤50 pre-filtered candidates: fetch 365-day daily candles (rate-limited ~0.35s apart — Angel One's 3 req/sec cap), run AI scoring (§2).
6. Every candidate that **passes** scoring becomes a new `PENDING` `ShortTermSignal` — there is **no cap** on how many PENDING signals get created in one run (unlike the strangle pipeline's hard cap). Duplicate symbols are skipped if they already have an open signal, or are in a post-exit cooldown (see §5).
7. Telegram: only the **top 10** (`max_telegram_alerts`) newly created picks are included in the "Daily Scanner Report" message.

---

## 4. Entry Activation — every 30 min, 10:15 AM–3:45 PM

Function: `check_pending_activations()`, scheduled as job `trade_engine_activation_checker` (confusingly wired to a wrapper *named* `run_intraday_check` in `updater.py` — not the same function as `trade_engine.run_intraday_check`, see §5).

- Bulk-quotes all `PENDING` signals.
- **Activates** (PENDING → ACTIVE) when live price pulls back to or through the recorded entry level: `LTP ≤ entry_price`.
- Uses row locking (`select_for_update`) to avoid double-activation races.
- Sends a "BUY ACTIVATED" Telegram alert on activation.

---

## 5. Exit Logic

**Two separate exit-checking code paths exist, but only one is actually scheduled:**

- `trade_engine.run_intraday_check()` — checks live quotes against target/SL for `ACTIVE` signals. **Not on any schedule.** Only reachable manually via `GET /cron-trigger/?action=trade_intraday&token=...`.
- `run_eod_evaluation()` — the real, scheduled exit path (job `trade_engine_eod_325pm`, 3:25 PM). Uses **daily candle high/low**, not live intraday quotes.

**`run_eod_evaluation()` decision order** (first match wins, per signal):
1. Hard stop-loss: `daily low ≤ stop_loss` → exit, status `HIT_SL`.
2. Trailing exit: `daily close < EMA20(daily closes)` → exit, status `TRAILING_EXIT`.
3. Final target (Target 3, or 2× target if T3 unset): `daily high ≥ target3` → exit, status `HIT_TARGET`.
4. Target 2 milestone (if currently ACTIVE or TARGET1): `daily high ≥ target2` → status becomes `TARGET2`, stop-loss ratcheted to breakeven (entry price). Position stays open.
5. Target 1 milestone (if currently ACTIVE): `daily high ≥ target1` → status becomes `TARGET1`, stop-loss ratcheted to breakeven. Position stays open.

**All automated exits funnel through `_exit_signal()`**, which:
- Always sets the *persisted* final status to `ARCHIVED` (the passed-in reason like `HIT_TARGET`/`HIT_SL`/`TRAILING_EXIT` is recorded as text/audit only, not as the stored `status` field).
- Locks a **cooldown** on that symbol: `cooldown_until = now + 20 trading days (~28 calendar days)`. The scanner (§3) won't re-pick the same symbol until this expires.
- Sends the corresponding exit Telegram alert ("FINAL TARGET HIT" / "TRAILING EXIT" / "STOP LOSS HIT").

---

## 6. Full Status Lifecycle

```
PENDING ──(price pulls back to entry)──────────────► ACTIVE
PENDING ──(unfilled > 30 trading / ~42 calendar days)─► EXPIRED

ACTIVE ──(EOD: high ≥ target1)──► TARGET1 (SL → breakeven)
TARGET1 ──(EOD: high ≥ target2)──► TARGET2 (SL → breakeven)

ACTIVE/TARGET1/TARGET2 ──(EOD: low ≤ SL)───────────► ARCHIVED  (Stop Loss)
ACTIVE/TARGET1/TARGET2 ──(EOD: close < EMA20)──────► ARCHIVED  (Trailing Exit)
ACTIVE/TARGET1/TARGET2 ──(EOD: high ≥ target3)─────► ARCHIVED  (Final Target Hit)
ACTIVE/TARGET1/TARGET2 ──(active > 90 calendar days)► REVIEW_REQUIRED  (dead end — no automatic path out)

ARCHIVED ──(cooldown_until elapses, ~28 days)───────► symbol eligible for scanner again
```

`CANCELLED`, `CLOSED`, and `COOLDOWN` exist as declared statuses in the model but are never produced by this pipeline (legacy/vestigial from the older `pro_system_service.py` system — see Known Issues).

---

## 7. Daily Schedule (IST, Mon–Fri unless noted)

| Time | Job | What happens |
|---|---|---|
| 9:05 AM | `trade_engine_premarket` | Logs Nifty trend (no persistence/gating by itself) |
| 10:00 AM | `trade_engine_scanner_10am` | Daily Scanner runs (§3) — creates PENDING signals, sends Telegram summary |
| 10:05 AM | `trade_engine_status_1005am` | Status ping to short-term channel |
| 10:15 AM – 3:45 PM, every 30 min | `trade_engine_activation_checker` | Entry activation check (§4) |
| 3:25 PM | `trade_engine_eod_325pm` | EOD evaluation — trailing stop, target/SL checks, portfolio Telegram (§5) |
| 3:35 PM | `trade_engine_status_335pm` | Final status ping |
| Sat 6:00 AM | `trade_engine_weekly_cleanup` | Weekly cleanup (§8) |
| every 1 min, all days | `telegram_queue_dispatcher` | Delivers queued Telegram messages (see §9) |

**Manual-only (not automated)**: `?action=trade_scan` (re-run scanner, optionally `relaxed`), `?action=trade_intraday` (live SL/Target check), `?action=trade_eod` (force EOD eval) — via `CronScannerTriggerView`.

---

## 8. Weekly Cleanup — `run_expiry_cleanup()` — Saturday 6:00 AM

1. **Expire stale PENDING**: any `PENDING` signal older than 42 calendar days (30 trading days) → `EXPIRED`, Telegram "TRADE SETUP EXPIRED" alert.
2. **Flag stale ACTIVE holdings**: any `ACTIVE`/`TARGET1`/`TARGET2` signal active for more than 90 calendar days → `REVIEW_REQUIRED`, Telegram "REVIEW REQUIRED" alert. (No P&L calc, no cooldown — just reclassification. As noted in §6, nothing currently moves a signal back out of `REVIEW_REQUIRED`.)

---

## 9. Telegram Notifications

Delivery: `queue_telegram_message()` writes a `TelegramLog(status='PENDING')` row; the 1-minute `telegram_queue_dispatcher` job picks up to 20 pending rows and sends them via `send_telegram_message()`, retrying up to 3 times before marking `FAILED`. All swing-pipeline messages go through this **async queue** (never sent inline from the scan thread).

| Message | Sent when | Intended channel | **Actual channel** |
|---|---|---|---|
| Daily Scanner Report | After `run_daily_scanner` (with picks, or "no setups") | Short-term | ⚠️ **Default/strangle channel** (bug, see Known Issues) |
| BUY ACTIVATED | Entry triggered | Short-term | Short-term ✅ |
| TARGET 1 / TARGET 2 HIT | EOD milestone hit | Short-term | Short-term ✅ |
| FINAL TARGET / TRAILING EXIT / STOP LOSS HIT | Position closed | Short-term | Short-term ✅ |
| TRADE SETUP EXPIRED | Weekly cleanup, stale PENDING | Short-term | Short-term ✅ |
| REVIEW REQUIRED | Weekly cleanup, stale ACTIVE | Short-term | Short-term ✅ |
| EOD PORTFOLIO STATUS | End of 3:25 PM eval, if any ACTIVE signals exist | Short-term | ⚠️ **Default/strangle channel** (bug) |
| Status update (10:05 AM / 3:35 PM) | Scheduled ping | Short-term | ⚠️ **Default/strangle channel** (bug) |

**Why the bug happens**: `process_telegram_queue()` (`telegram_service.py:635`) routes each queued message by checking `if db_log.short_term_signal else None` — i.e. only messages tied to *one specific signal row* get the short-term chat ID. Messages that summarize multiple signals or the whole portfolio aren't attached to a single `short_term_signal` FK, so they fall through to the default channel, regardless of what `chat_id` the caller originally intended.

---

## 10. Known Issues (in priority order)

1. **3 of 8 message types land in the wrong Telegram channel** (Daily Scanner Report, EOD Portfolio Status, the 10:05/3:35 status pings) — see §9. Fix requires either attaching a representative signal to these queue entries, or fixing `process_telegram_queue()` to honor the originally-computed chat ID instead of re-deriving it.
2. **No automated intraday exit detection.** The only automated SL/Target check happens once daily at 3:25 PM off daily candle highs/lows. A stock could blow through its stop-loss intraday and the system won't react until end of day. The live-quote intraday checker exists (`trade_engine.run_intraday_check`) but isn't scheduled.
3. **`REVIEW_REQUIRED` is a dead end.** Nothing in the current code moves a signal out of this status automatically.
4. **A second legacy pipeline (`pro_system_service.py`) still exists** with different rules (fixed 2.5x R:R, no AI scoring, pullback+bullish-candle activation) and writes to the same `ShortTermSignal` table. It's not on the scheduler, but if any endpoint still triggers `get_pro_system_data(trigger_scan=True)`, it can create signals that conflict with the main scanner's view of "does this symbol already have an open signal."
5. **No hard cap on PENDING signals per scan** — every AI-qualifying candidate becomes a PENDING row; only the Telegram alert is capped at 10. On a strongly trending day this could create many more open signals than the strangle pipeline's equivalent 10-signal cap.
6. Minor/cosmetic: `fundamental_score` doesn't feed into `ai_score`; several `STRATEGY_CONFIG` scanner/telegram keys are unused dead config (hardcoded literals used instead).
