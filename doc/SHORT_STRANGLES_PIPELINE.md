# Short Strangles Pipeline — Full System Review

> Engine: `backend/stocks/services/delta_hedge_service.py` (a.k.a. "Specialist" / "Delta Hedge")
> Model: `SignalHistory` with `category="specialist"` (`backend/stocks/models.py`)
> Scheduler: `backend/stocks/updater.py` → `start()`
> Telegram channel: `TELEGRAM_CHAT_ID` (default channel — this pipeline does **not** use the short-term channel)
> Position sizing convention: figures below are **1 lot**; Telegram/EOD reporting multiplies by **2 lots** — see §3.

This document traces the strangle system from candidate selection through strike selection, delta monitoring/rebalancing, exit, and Telegram notification — as the code actually behaves today, including known bugs. This is a **signal-generation and paper-tracked P&L system** — no broker order placement exists anywhere in the pipeline.

---

## 1. Strategy Overview

A short strangle = selling one OTM Call (CE) + one OTM Put (PE) on the same underlying and expiry, collecting combined premium, no long legs in the live position. (A "buy far-OTM protection" wing is calculated in code but never actually appended to the live position — effectively dead code; the live strategy is a naked 2-leg short strangle, not an iron-condor-style hedged strangle.)

**Eligible instruments**:
- **Equities**: a hardcoded list of 49 Nifty 50 stocks (`NIFTY_50_STOCKS`), *not* the Nifty 500 pool used by the swing pipeline. NIFTY/BANKNIFTY/FINNIFTY/NATURALGAS index/commodity tickers are explicitly excluded from the equity scan.
- **Commodities**: hardcoded to exactly two — `CRUDEOIL` and `NATURALGAS` (MCX).

---

## 2. Signal Generation

Entry point: `get_hedge_panel_data(action="generate", sync_scan=True)` → `_background_scan()` → `build_specialist_hedge()` per candidate.

### Candidate selection
- Time window: equities 10:45 AM–3:30 PM; commodities up to 10:30 PM.
- **Commodities**: accepted if sideways, or currently trading inside its Value Area, or no VA data yet.
- **Equities**: two-pass relative ranking across the day's universe —
  1. Collect raw metrics per stock: VWAP distance %, Value-Area width %, POC distance, intraday range %.
  2. Rank-normalize each metric 0–1 and compute a weighted composite confidence score: **VWAP proximity 50%, VA tightness 25%, POC centrality 15%, intraday range 10%**.
  3. Hard filter: must be sideways or inside its Value Area — trending stocks are skipped entirely.
- Final pick: sorted by confidence, capped at **≤2 commodities** and **≤10 equities**, bounded overall by `HEDGE_MAX_SIGNALS` (default 12 — "1 NG + 1 Crude + 10 Stocks").

### Strike selection (`build_specialist_hedge`) — multi-stage funnel
1. Reject if ≤1 day to expiry (gamma guard).
2. Estimate IV off the ATM call (Newton-Raphson).
3. Reject if live IV > 25%.
4. Reject if today's high-low range > 1.5% of LTP (choppy/volatile day guard).
5. Pick a **target delta** that adapts through the day: Morning (before 11:00) 0.25 → Midday (11:00–1:00) 0.28 → Afternoon (after 1:00) 0.32, hard-capped at 0.40. Adjusted down in low-IV or panic-IV regimes; capped at 0.20 if ≤3 days to expiry. MCX always uses the flat morning value (0.25).
6. Compute Expected Move (`spot × IV × √(1/365)`).
7. Search for a strike pair between 2.0–2.5% and 5.0–6.0% OTM (0.5% steps) meeting: minimum per-leg premium (₹5–10 depending on days-to-expiry), minimum combined premium (₹8/₹10/₹12 by session), and ≤15% premium imbalance between legs.
8. Multiple fallback/retry passes if premium is too thin — first for minimum leg premium, then for a minimum **lot-weighted net credit of ₹2,500**.
9. Reject strikes beyond 1.5× Expected Move (2.0× if IV > 35%).
10. Reject if daily theta yield (`theta × lot_size / margin`) falls below a minimum threshold.
11. A second, independent "equal-premium pair search" re-scans up to 25 OTM candidates per side and can override the first pick, minimizing premium difference / spread / maximizing liquidity and symmetry.
12. Final rebalance pass if legs are still >10% / >₹0.50 apart, then a final ₹-floor and per-leg viability re-check (min premium ₹2 equities / ₹0.50 MCX, min notional ₹500 / ₹600 MCX).

**Entry premium** = live LTP at signal-creation time per leg (falls back to theoretical Black-Scholes price if the live API returns 0). Stored as both a floating `sell_price` and an immutable `original_sell_price`.

---

## 3. Position Sizing — "2 Lots"

Every per-leg P&L calculation in the core engine (`calculate_pnl()`) is computed for **1 lot**. The **"2 lots" figure only appears at the reporting layer**:
- The periodic Telegram P&L update explicitly multiplies by 2.0 and labels lines "P&L (2 Lots)".
- The EOD square-off job's log line also multiplies by 2.0 for display.
- **But** the EOD job's `final_pnl` value *saved to the database* is the raw **1-lot** figure — only the log/Telegram output is doubled. Anyone reading `SignalHistory.metadata['final_pnl']` directly (API, DB query) sees half of what Telegram showed. See Known Issues.

There is no actual lot-count field on the model driving order sizing — "2 lots" is a fixed assumption baked into reporting math only.

---

## 4. Expiry Resolution

- **Equities**: filters the NFO instrument master for unexpired option contracts, sorts expiries chronologically, and uses the nearest one with 0 or more trading days remaining (rolls to the next expiry once the current one has 0 days left). Logged as "Locking to Sector Expiry."
- **MCX**: same logic, "Locking to Commodity Expiry."
- No fixed weekly-vs-monthly designation — it's simply whatever the nearest live contract is (typically weekly for equities/index, monthly for MCX), subject to the 1-day gamma-guard cutoff (§2 step 1).

---

## 5. Delta Monitoring & Rebalancing (Roll Mechanism)

- Both legs' deltas are recomputed live every panel refresh via Black-Scholes.
- **Trigger**: `|CE delta − PE delta| ≥ 0.15` (hardcoded), checked only while the signal is `ACTIVE`.
- **Roll logic**: the leg with the *lower* delta gets rolled toward the *higher*-delta leg's strike (e.g. if CE delta > PE delta, the PE is rolled up to match CE's delta). New strike candidates are pulled from the live option chain on the correct side of spot, and the one with delta closest to the target is chosen.
- The **old leg is not deleted** — it's marked `EXPIRED` with reason "Rolled to {new strike} (Delta Neutral Adjustment)" and frozen; a **new leg is appended** with a fresh entry price = current premium, target = 70% of new premium, SL = 115% of new premium.
- A synchronous Telegram alert ("🔄 TradePulse Greeks Rebalance") is sent immediately to the **default** channel — this is the message you saw in your original logs ("REBALANCE_TRIGGER... Triggering roll" → "Successfully executed leg roll").

---

## 6. P&L Calculation

**Per leg (1 lot)**:
```
premium_diff = sell_price − current_price     (profit as premium decays, since it's a short position)
pnl = premium_diff × lot_size
pnl_pct = premium_diff / sell_price × 100
```

**Per signal, periodic Telegram update (2 lots)**:
```
for each leg (CE and PE):
    leg_pnl = (original_sell_price − current_price) × lot_size
    running_pnl += leg_pnl
    total_entry_value += original_sell_price × lot_size
running_pnl ×= 2        # 2 lots
pnl_pct = running_pnl / total_entry_value × 100
```
For already-closed signals, it instead uses the persisted `final_pnl × 2` directly rather than recomputing.

---

## 7. Full Status Lifecycle

Statuses: `PENDING, ACTIVE, CANCELLED, HIT_TARGET, HIT_SL, EXPIRED`. At most one PENDING or ACTIVE signal per (symbol, category) is allowed at a time (DB constraint).

```
[created] ──────────────────────────────────────────► PENDING

PENDING ──(120s grace window elapses, entry locks to live CMP)──► ACTIVE
PENDING ──(any leg's live premium decays below ₹0.30)──────────► CANCELLED
PENDING ──(duplicate symbol in same scan cycle)─────────────────► CANCELLED
PENDING ──(⚠️ intended: EOD force-close if never activated)────► CANCELLED   [not automated — see Known Issues]

ACTIVE ──(combined premium expands beyond entry×1.20 intraday / ×1.30 monthly)──► HIT_SL
ACTIVE ──(a leg's delta ≥ 0.55, auto-exit-on-breach enabled)───────────────────► HIT_SL
ACTIVE ──(combined premium decays to ≤ entry×0.75 intraday / ×0.20 monthly)───► HIT_TARGET
ACTIVE ──(≤1 day to expiry — gamma guard)──────────────────────────────────────► EXPIRED
ACTIVE ──(⚠️ intended: EOD auto square-off at market close)───────────────────► EXPIRED   [not automated — see Known Issues]

[any PENDING/ACTIVE from a prior calendar day] ──(stale sweep, ~hourly)───────► EXPIRED

ACTIVE ──(delta imbalance ≥ 0.15)──► one leg rolled to a new strike, status stays ACTIVE (§5)
```

Individual legs can independently reach `HIT_TARGET`/`HIT_SL` and freeze ("let winners run") — but this per-leg freeze is only meaningfully evaluated for **commodities**; for equities the signal exits as a whole. For commodities, the overall signal only closes once **both** legs have finished.

**A previously-planned intraday time-based cutoff (e.g. 3:15 PM) exists in the code but is commented out** ("disabled per user request") — see Known Issues.

---

## 8. Daily Schedule (IST, Mon–Fri unless noted)

| Time | Job | What happens |
|---|---|---|
| **10:45 AM** | `strangle_signal_10am` | Clears scan throttle → full `generate` scan → new strangle signals created (§2) → consolidated "Daily Specialist Strangles" Telegram message |
| **11:00 AM – 2:45 PM, every 15 min** | `live_signal_update_hourly` | Re-scans/updates positions, checks delta imbalance and rolls legs if needed (§5), sends the periodic P&L Telegram update |
| **3:00 PM, 3:15 PM** | `live_signal_update_final_hour` | Same as above |
| **3:28 PM** | `final_eod_update_328` | Final periodic P&L update for the day ("no auto square-off" — by design, this job does *not* close positions) |
| every 1 min, all days | `telegram_queue_dispatcher` | Not used by this pipeline — see §9 |

**⚠️ Not found scheduled anywhere**: `run_option_square_off()` — the function that's supposed to force-close ACTIVE strangles at end of day and cancel unfilled PENDING ones. It's fully written but not wired to any `CronTrigger`, view, or management command. See Known Issues — this is the most important gap in this pipeline.

---

## 9. Telegram Notifications

Unlike the swing pipeline, **every active message here is sent synchronously** (direct `requests.post`, not the async `TelegramLog` queue) — and **every one goes to the default channel** (`TELEGRAM_CHAT_ID`), never the short-term channel.

| Message | Trigger | Status |
|---|---|---|
| Consolidated new signals ("Daily Specialist Strangles") | End of a `generate` scan, if any new signals were created | ✅ Active |
| Periodic P&L update ("Strangles Session Update") | Every `action="update"` cycle (15-min jobs + 3:28 PM final), suppressed before 10:10 AM | ✅ Active |
| Delta-neutral rebalance/roll alert | Every leg roll (imbalance ≥ 0.15) | ✅ Active |
| Activation alert (PENDING → ACTIVE) | Position activates | ❌ Hardcoded disabled ("per user request") |
| Exit alert (target/SL hit) | Position closes | ❌ Hardcoded disabled ("per user request") |
| Daily picks summary | End of scan (separate from consolidated new signals) | ❌ Hardcoded disabled ("per user request") |

This matches what you saw in your original logs: `Skipping activation alert for BHARTIARTL (disabled per user request)`.

---

## 10. Throttle / Cooldown Mechanisms

| Cache key | TTL | Purpose |
|---|---|---|
| `delta_hedge_scanner_throttle_5m` | 120s (name says 5m, actual TTL is 2 min) | Stops re-triggering a full scan on every panel poll |
| `delta_hedge_panel_live_5s` | 5s | Caches the whole panel response for rapid repeat polling |
| `delta_hedge_panel_2s` | 2s | Extra outer-layer cache in the view itself |
| `stale_signal_cleanup_done` | 1 hour | Limits the stale-signal sweep to once/hour instead of every refresh |
| `specialist_baseline_{symbol}_{strike}_{type}_{id}` | 24 hours | Locks each leg's entry-price baseline so it doesn't drift on repeated reads |

---

## 11. Known Issues (in priority order)

1. **No automated end-of-day close for strangle positions.** `run_option_square_off()` exists and is fully implemented (force-close ACTIVE positions, cancel unfilled PENDING ones) but is never invoked by the scheduler, a view, or a management command. Combined with the commented-out intraday time cutoff (§7), a strangle that's still ACTIVE at 3:30 PM has **no mechanism to close it that day** — it just carries over and gets caught by the once-hourly "stale signal" sweep the *next* day instead, at whatever price that sweep finds. **This is the highest-priority fix** — recommend either wiring `run_option_square_off()` to a ~3:25–3:29 PM cron job, or re-enabling the commented-out intraday cutoff.
2. **DB `final_pnl` is 1-lot; Telegram always shows 2-lot.** Anyone reading the database or an API response directly (dashboard, reports) will see half the P&L figure shown in Telegram, with no field indicating the multiplier. Recommend either storing the 2-lot value consistently, or adding an explicit `lots` field so consumers can compute it themselves.
3. **3 of 6 Telegram message types are dead code paths** (hardcoded `return False`) — activation alerts, exit alerts, and the (separate) daily picks summary never fire, regardless of config. If this is intentional (you mentioned disabling activation alerts on purpose), fine — but exit alerts (target/SL hit) being silently disabled means you only find out a position closed via the next periodic P&L update, not in real time.
4. The "buy far-OTM protection" leg is computed but never added to the live position — the strategy is actually a naked 2-leg short strangle, not a hedged one, despite the code's docstring describing hedged protection.
5. Minor: a `MIN_DTE` constant is defined but unused (superseded by `FORCE_EXIT_DTE`); a code comment mislabels the 10:45 AM job as "10:00 AM."
