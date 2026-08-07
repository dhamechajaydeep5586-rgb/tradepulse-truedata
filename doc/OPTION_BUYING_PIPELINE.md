# Option Buying Pipeline — Full System Review

> Engine: `backend/stocks/services/option_buying_service.py`
> Model: `SignalHistory` with `category="option_buying"` (`backend/stocks/models.py`)
> Scheduler: `backend/stocks/updater.py` → `run_periodic_scanners` (every 15 min, 9:00 AM–3:15 PM IST, Mon–Fri)
> Telegram channel: `TELEGRAM_INTRADAY_CHAT_ID` (shared with intraday equity signals — **not** the specialist/strangle channel)
> Position sizing convention: target/SL are computed **per 2 lots** directly (not 1-lot-then-doubled like the strangle pipeline)

This document traces the option-buying system from candidate scan through strike selection, entry, exit, and Telegram notification — as the code actually behaves today. This is a **signal-generation and paper-tracked P&L system** — no broker order placement exists anywhere in the pipeline.

---

## 1. Strategy Overview

Buys near-ATM **CE or PE** on a confirmed intraday breakout — the opposite trade direction from the deleted `option_sniper_service.py` (which used to *sell* CE/PE near VAH/VAL for theta decay; that file no longer exists). This is a directional, long-premium strategy: profit when the underlying keeps moving in the breakout direction, loss (accelerated by theta) if it stalls or reverses.

**Eligible instruments**: F&O-eligible stocks that are also in the NIFTY50 universe (`get_fo_stocks() ∩ get_universe_symbols()`) — narrowed from all-F&O to NIFTY50-only on 2026-07-28 at the account owner's request. Indices (NIFTY/BANKNIFTY/FINNIFTY) are not scanned.

Unlike equity intraday signals, an option-buying signal has **no PENDING wait state** — entry is immediate at the live premium already fetched during the scan (`status: ACTIVE` from creation).

---

## 2. Timing Windows

| Boundary | Time (IST) | Behavior |
|---|---|---|
| `OPTION_BUYING_GENERATION_START` | 10:00 AM | Before this, generation is skipped entirely — ADX>20 trend confirmation is unreliable on the still-unsettled opening range. 45 min later than intraday's own opening-range skip. |
| `OPTION_BUYING_GENERATION_CUTOFF` | 1:45 PM | After this, no *new* signals — a position needs real runway to reach its target before the hard time-stop force-closes it regardless of P&L. Existing ACTIVE positions are untouched. |
| `OPTION_BUYING_TIME_STOP` | 2:30 PM | Every remaining ACTIVE position is force-closed at current premium, win or loss — every extra minute open past this is pure theta cost. |

Generation is attempted on **every** 15-min `run_periodic_scanners` tick inside the 10:00 AM–1:45 PM window, retrying indefinitely until a signal actually lands (fixed 2026-07-28 — used to give up after 4 attempts regardless of outcome).

---

## 3. Signal Generation (`get_option_buying_signals`)

Order of checks, each an early-return:

1. **Static + live market-open check** (NSE API, same source-of-truth as every other engine).
2. **Daily loss limit halt** — if `OPTION_BUYING_DAILY_LOSS_LIMIT_PCT` (default 2% of `INTRADAY_ACCOUNT_EQUITY`) has already been breached today, refuse all new generation for the rest of the session.
3. **Generation cutoff / start window** (§2).
4. **Action router**: `None` auto-resolves to `"generate"` the first time today's `option_buying` row doesn't exist yet, else `"update"` (read-only, no scan).
5. **Stale signal guard** — cancels PENDING/ACTIVE rows from previous days.
6. **Scan-rate guard** — 5-min cooldown (`option_buying_last_full_scan` cache key), skipped if there's nothing live yet.
7. **Candidate universe**: NIFTY50 ∩ F&O-eligible, minus symbols already holding an ACTIVE/PENDING option_buying signal, capped at **40** candidates.
8. **Strict/relaxed fallback**: if the day's last signal was >90 min ago (or, on a signal-free day, it's past 12:00 PM), thresholds relax — see §4.
9. Per candidate: load today's 5-min bars **from the local tick-aggregator table only** (`candle_store.load_bars`, no REST call — can't trip the TrueData circuit breaker), skip if fewer than 10 bars exist yet.
10. Breakout check (§4) → strike selection (§5) → target/SL (§6) → persist, capped at **3 signals per scan** (`MAX_OPTION_BUY_SIGNALS_PER_SCAN`).

If signals were created, one consolidated Telegram message fires. If the scan found nothing **and this was the day's first attempt**, a one-shot "no qualifying setup today" notice fires instead (later empty retries stay silent — no repeat spam).

---

## 4. Breakout Logic (`_option_breakout_logic`)

Deliberately a separate, **stricter** implementation from intraday equity's `_volume_profile_logic` — a decaying option needs more conviction than a plain stock entry. Evaluated on the **last closed 5-min bar only** (never the still-forming bar, so it can't repaint).

| Condition | Strict | Relaxed (fallback) |
|---|---|---|
| Volume ratio (vs 10-bar SMA) | > 1.5 | > 1.2 |
| ADX(14) | > 20 | > 15 |

- **BUY_CE**: previous close ≤ VAH, current close > VAH, volume/ADX pass, price above session VWAP.
- **BUY_PE**: previous close ≥ VAL, current close < VAL, volume/ADX pass, price below session VWAP.

Value Area (POC/VAH/VAL) computed the same way as intraday's volume-profile logic, 40 bins.

---

## 3a. Rejection Funnel Logging

Added 2026-08-07 (account owner's request) — a bare "no signal today" gave no way to tell whether the bottleneck was thin candle data, the breakout filter, strike/delta selection, or bad target/SL math. Every scan cycle now logs a 4-gate funnel, in the order candidates are actually evaluated:

| Gate | Counter | Per-symbol detail logged? |
|---|---|---|
| Bars loaded (§3 step 9) | `no_bars` | No — not enough today's bars yet, nothing more to say |
| Breakout (§4) | `no_breakout` | Only for a **near-miss**: crossed VAH/VAL but failed volume/ADX/VWAP. A symbol that never crossed at all stays silent (the common case, not actionable) |
| Strike/quote/delta (§5) | `no_strike` | Yes — no tradable strike, no live quote, or quote premium ≤ 0 |
| Target/SL viability (§6) | `bad_target_sl` | Yes — logged with the computed entry/target/SL that failed the viability check |

Near-miss and strike-stage rejections log under `[OPTION_BUYING][REJECT]`, e.g.:
```
[OPTION_BUYING][REJECT] ICICIBANK: crossed BUY_CE but failed vol_ratio=1.18(<1.5)
[OPTION_BUYING][REJECT] SBIN: no option quote for strike=590 type=CE
```
A confirmed breakout that clears every check also logs under `[OPTION_BUYING][SCREEN]` before moving to strike selection.

Every scan ends with one summary line regardless of outcome, mirroring intraday's existing `[INTRADAY][SCAN_DONE]` convention:
```
[OPTION_BUYING][SCAN_DONE] scanned=40 persisted=1 | no_bars=3 no_breakout=34 no_strike=2 bad_target_sl=0 errors=0
```
Grepping `OPTION_BUYING` in the Render logs for a full trading day now answers "market conditions vs. data quality vs. logic bug" without touching the strategy itself.

---

## 5. Strike Selection (`select_option_buying_strike`)

Opposite target from the strangle-selling pipeline (which hunts deep-OTM/high-theta strikes) — option buying wants the contract to actually track the underlying:

1. Nearest tradable strike to spot (`get_nearest_strike`).
2. Live quote must exist with a positive LTP.
3. **Reject same-day (0-DTE) expiry** outright — extreme gamma/theta risk (fixed 2026-08-xx: was silently floored to "1 day left" by a naive non-IST `datetime.now()` comparison).
4. Estimate IV (Newton-Raphson) off the fetched premium, compute Greeks.
5. **Reject unless delta is between 0.40 and 0.60** — this is the core filter that makes this a directional trade rather than a lottery ticket; a candidate that passed the breakout check can still be rejected here.

---

## 6. Target / Stop-Loss (`_compute_target_sl`)

**Fixed 2-lot rupee amounts**, not the ADX-scaled percentage formula this pipeline used before 2026-07-31:

- Target: **+₹5,000** (2 lots)
- Stop-loss: **−₹2,500** (2 lots)
- Clean 1:2 reward:risk, converted to a premium-price delta via the symbol's real lot size (`OPTION_BUYING_PROFIT_RUPEES / (lot_size × 2)`), so the same ₹ outcome applies regardless of how large a premium move that requires for a given stock.

Replaced the old formula (target scaling 1.6×–2.0× entry premium with ADX strength, fixed 0.625× SL) after it let one real position (SUNPHARMA CE) run to +₹8,785 unrealized with no exit condition anywhere near that level before decaying back to a loss by the 2:30 PM time-stop.

A candidate is discarded if the resulting target/SL isn't viable (`target <= entry`, `sl >= entry`, or `sl <= 0`) — most commonly when `get_lot_size()` can't resolve a real lot size for the symbol.

---

## 7. Exit / Auditing (`update_option_buying_outcomes`)

**Self-contained** — deliberately excluded from the shared `update_signal_outcomes()` (same precedent as the strangle-selling pipeline): premium-space math and the hard time-stop don't fit that function's equity/commodity branches. Runs every `run_periodic_scanners` tick, **before** the intraday and specialist scans in that cycle (so a fresh premium is already in the DB by the time other jobs read it).

Per ACTIVE signal, in priority order:
1. Fetch live premium via `get_option_quote`.
2. **Pessimistic tie-break**: if a single audit tick's premium has crossed *both* target and stop, book the stop first.
3. Stop-loss touch → `HIT_SL`.
4. Target touch → `HIT_TARGET`.
5. Past 2:30 PM time-stop → force-close: `HIT_TARGET` if premium ≥ entry, else `HIT_SL`. This fires even if the quote fetch failed (falls back to last known premium) — a bad data tick must never let a position silently sit open past close.

Every close fires an instant Telegram exit alert (`send_instant_exit_alert`) — doesn't wait for the next periodic recap.

**Known gap** (documented in code, not yet fixed): this audits a single live LTP snapshot per ~15-min cycle, with no intrabar/bar-history re-scan — unlike the equity engine, which re-scans 1-min bar highs/lows since the last checkpoint. A target/SL touch that occurs and reverts between two polls is invisible here. Left unimplemented pending verification of TrueData's per-strike option historical-bar endpoint.

---

## 8. Daily Loss Limit Kill Switch

Mirrors intraday's equivalent (`_enforce_option_buying_daily_loss_limit`, checked after every outcome audit):

- Sums today's realised (closed) + unrealised (ACTIVE, at current `premium_cmp`) P&L across all `option_buying` rows, 2-lot convention.
- If total P&L breaches `-INTRADAY_ACCOUNT_EQUITY × OPTION_BUYING_DAILY_LOSS_LIMIT_PCT / 100` (default −2% of ₹5,00,000 = −₹10,000): flattens every remaining ACTIVE position at its current premium, tags `exit_reason: DAILY_LOSS_LIMIT`, and sets a cache halt through end-of-day blocking any further generation.
- A symbol whose lot size can't be resolved is excluded from the sum (not silently treated as ₹0) so the kill switch never understates a real loss.

---

## 9. Frontend

`OptionBuyingTable.jsx` / `pages/OptionBuying.jsx` poll `/api/stocks/option-buying/` every 5 min (open positions) or 30 min (closed) — **read-only**, always `action="update"`. It never triggers generation; only the backend scheduler does that (see CLAUDE.md's "Production Scheduler" section).

---

