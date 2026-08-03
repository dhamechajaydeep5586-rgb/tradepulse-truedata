# Intraday Buy/Sell Signal Logic

This document explains, in plain terms, exactly how the **Intraday Buy/Sell** signals on the
dashboard are generated — where the stock list comes from, which platform supplies the data,
the precise math behind a BUY vs a SELL, and what happens after a signal is created. It is
written directly off the current code (not just the design notes), so it reflects what actually
runs today.

**Source of truth (code):**
- `backend/stocks/services/intraday_service.py` — the scan engine (`get_live_signals`)
- `backend/stocks/services/signal_utils.py` — indicators, market-hours check, stock universe
- `backend/stocks/services/market_intelligence_service.py` — NIFTY trend/state
- `backend/stocks/services/angel_one_service.py` — broker/data platform integration
- `backend/stocks/services/trading_engine/state_engine.py` — signal persistence
- `backend/stocks/services/live_signal_service.py` — outcome auditing / auto square-off

---

## 1. Platform

**Broker/data platform: Angel One SmartAPI** — this is the only market-data source used for
intraday signals.

| Data need | Source |
|---|---|
| Market open/closed check | NSE public API (`marketStatus`), with a static calendar fallback |
| Live price (LTP) | Angel One **WebSocket** stream (preferred), REST quote as fallback |
| Historical candles (for indicators) | Angel One **Historical Candle REST API** |
| Stock list (universe) | NSE archive CSV (`nsearchives.nseindia.com`), cached 24h; DB table as fallback |

Nothing in this pipeline uses yfinance, Groww, or any other data source — Angel One end to end.

---

## 2. Where the stock list comes from

```
load_symbols_from_stock_table()  →  fetch_nifty_symbols_live(INTRADAY_UNIVERSE)
```

- `INTRADAY_UNIVERSE = "NIFTY100"` (in `signal_utils.py`). The scanner pulls the **current NIFTY
  100 constituent list** directly from NSE's archive CSV (`ind_nifty100list.csv`), cached for 24
  hours. If NSE is unreachable, it falls back to the `IndexConstituent` DB table.
- ⚠️ Note: this is **NIFTY 100**, not NIFTY 500. It was intentionally narrowed from 500 → 100 in a
  recent change to cut Angel One REST call volume and avoid 403 rate-limit errors (see git log:
  "Reduce Angel One REST call volume..."). `CLAUDE.md` still says "Nifty 500" in one place — that
  line is stale and should be updated to match.
- From that universe, any symbol that **already has an ACTIVE or PENDING intraday signal today**
  is skipped — only fresh symbols get re-scanned.

---

## 3. Scan pipeline, step by step

Each time `get_live_signals()` runs (see cadence below):

1. **Market-open check** — via the NSE API (never via Angel One's WebSocket pulse; that's
   explicitly banned as a gate because it can silently go `None`).
2. **Idempotency router** — if a signal was already generated today, treat this call as
   `"update"` (just return current DB state, no new scan). Otherwise, `"generate"`.
3. **Stale signal guard** — any PENDING/ACTIVE intraday signal from a **previous** trading day is
   force-cancelled before scanning (prevents old signals bleeding into a new session).
4. **Scan-rate guard** — a full scan is expensive (network calls per stock), so it only actually
   runs once per **5 minutes** (cache key `intraday_last_full_scan`). Within that window, repeat
   calls just return the current DB payload.
5. **NIFTY 50 trend read** — `get_standard_market_state()` pulls 2 days of 5-min NIFTY 50 candles
   and classifies the day as `"SIDEWAYS"` or `"TRENDING"` (see caveat in §6 below — this is
   **not** `BULLISH`/`BEARISH` despite the naming used downstream).
6. **Bulk data fetch** — token IDs for all scan symbols are resolved once, then quotes are pulled
   in batches of 50 (`get_bulk_quotes`) instead of one REST call per stock, to stay under Angel
   One's rate limit.
7. **Per-symbol scan** — for each candidate symbol:
   - Fetch 5-minute candles (2-day lookback).
   - Skip if fewer than 10 candles came back.
   - Run the **Volume Profile logic** (§4 below).
   - If a signal fires, persist it (§5) and count it against the cap.
8. **Hard cap: 5 signals per scan cycle.** Scanning stops early once 5 are persisted in one
   cycle.

---

## 4. The actual BUY/SELL condition (Volume Profile logic)

Function: `_volume_profile_logic()` in `intraday_service.py`.

This is **not** the simple "price above/below VWAP and VAH/VAL" rule from the old design notes —
the live code runs three separate triggers, evaluated in priority order per symbol (first match
wins):

First, three levels are computed from a 40-bin Volume Profile of the recent candles:
- **POC** — Point of Control (the price level with the most traded volume)
- **VAH** — Value Area High (top of the 70%-volume zone)
- **VAL** — Value Area Low (bottom of the 70%-volume zone)

And a volume filter: `vol_ratio = current candle volume ÷ 10-period average volume`.

### Trigger 1 — POC Flip (score 4.5, highest priority)
| Direction | Condition |
|---|---|
| **BUY** | Previous candle closed *below* POC, current price is *above* POC, and `vol_ratio > 1.2` |
| **SELL** | Previous candle closed *above* POC, current price is *below* POC, and `vol_ratio > 1.2` |

### Trigger 2 — Value Area Breakout (score 4.0)
| Direction | Condition |
|---|---|
| **BUY** | Price breaks *above* VAH (was at/below it last candle) with `vol_ratio > 1.5` |
| **SELL** | Price breaks *below* VAL (was at/above it last candle) with `vol_ratio > 1.5` |

### Trigger 3 — Value Area Rejection / Bounce (score 3.5, lowest priority)
| Direction | Condition |
|---|---|
| **BUY** | Price is at/above VAL, previous candle's low touched VAL, current candle closed green (close > open), NIFTY isn't BEARISH*, `vol_ratio > 1.1` |
| **SELL** | Price is at/below VAH, previous candle's high touched VAH, current candle closed red (close < open), NIFTY isn't BULLISH*, `vol_ratio > 1.1` |

\* **Caveat:** as noted in §3, `get_standard_market_state()` only ever returns `"SIDEWAYS"` or
`"TRENDING"` — never the literal strings `"BULLISH"`/`"BEARISH"` that this trigger checks
against. In practice this means the NIFTY-bias gate on Trigger 3 never blocks a trade; it always
evaluates as "not BEARISH" / "not BULLISH" = true. This looks like a leftover from an earlier
version of the market-state function. Flagging it here — happy to fix it if you want Trigger 3 to
actually respect NIFTY bias.

**Relaxed/fallback mode:** if no signal has fired in the last 2 hours (or it's past 11:30 AM and
nothing has fired today), the volume thresholds above are lowered (e.g. POC Flip needs
`vol_ratio > 1.0` instead of `1.2`) so the scanner doesn't come up completely empty on a quiet
day. This is the `relaxed=True` path.

---

## 5. Entry / Target / Stop-Loss math

Once a direction is picked:

```
BUY:
  entry = current price
  stop_loss = min(VAL, entry - 0.8 × ATR14)
  target   = entry + 2 × |entry - stop_loss|

SELL:
  entry = current price
  stop_loss = max(VAH, entry + 0.8 × ATR14)
  target   = entry - 2 × |stop_loss - entry|
```

- `ATR14` = 14-period Average True Range.
- The stop-loss is always anchored to the *structural* level (VAL for BUY, VAH for SELL) unless
  the ATR-based distance is wider — whichever is more conservative.
- Target is always **2× the risk distance** → every signal has **Reward:Risk = 2.0**. If for some
  reason RR computes below 1.5 after price rounding, the candidate is discarded entirely
  (`_build_intraday_candidate`).
- Prices are rounded to the nearest ₹0.05 tick.

If multiple triggers fire for the same symbol in the same cycle (rare, since they're `elif`
chained), the one with the highest `score` wins.

---

## 6. Persistence & de-duplication

`engine_persist_live_signal_history()` (`trading_engine/state_engine.py`):
- Won't create a duplicate — if the symbol already has an ACTIVE/PENDING intraday signal, the
  existing row is returned as-is.
- Stores `vol_ratio`, `vwap`, `poc`, `vah`, `val`, `score`, `strategy_key` in `metadata` so the UI
  can show the "Reason" column.
- Triggers a WhatsApp/Telegram notification if configured.

**Trigger mode:** `price_cross` — a PENDING signal becomes ACTIVE once the live price comes
within **0.2%** of the entry price.

---

## 7. Signal lifecycle & auto square-off

```
PENDING → ACTIVE → HIT_TARGET
                 → HIT_SL
      → CANCELLED   (never triggered, still pending at cutoff)
      → EXPIRED      (was active, force-closed at cutoff without hitting target/SL)
```

Enforced in `live_signal_service.py → update_signal_outcomes()`, which runs on the periodic
scanner cycle:

- **Immediate auto square-off (recently added):** the instant the live price crosses Target or
  Stop Loss, the signal closes right then as `HIT_TARGET` / `HIT_SL` with `exit_price` locked in —
  same behavior as every other signal category (option selling, commodity, etc.). This used to be
  bypassed for intraday (it only tracked a running P&L number and never closed the trade), but
  that bypass was removed so intraday now behaves consistently with the rest of the app.
- **Hard cutoff — 3:20 PM IST:** anything still open at this time is force-closed regardless of
  where price is — PENDING → `CANCELLED` (no P&L), ACTIVE → `EXPIRED` (P&L marked at whatever the
  price is at that moment).
- **Stale guard:** at the start of every new day's first scan, any leftover PENDING/ACTIVE
  intraday rows from a previous day are cancelled — they never carry across sessions.

---

## 8. Frontend refresh cadence

| Element | Interval | Behavior |
|---|---|---|
| Full signal list | 5 min | Re-fetches from `/api/stocks/live-signals/`; re-scans only if the 5-min cooldown has elapsed |
| Price ticker | 1 sec | Polls `/api/stocks/live-price-updates/` for ACTIVE/PENDING symbols only (≤5 at a time) — never for closed signals |
| Force Scan button | on demand | `?force=true` bypasses the 5-min cooldown for an immediate re-scan |

---

## 9. Known doc/code drift worth resolving

Two things this investigation surfaced, flagged for visibility (not yet changed):

1. **Universe size** — `CLAUDE.md` says "Nifty 500 stocks... via `IndexConstituent` table"; the
   running code actually scans **NIFTY 100** via a live NSE CSV fetch. The doc is stale.
2. **Dead NIFTY-bias gate** — Trigger 3 (VA Rejection) checks `nifty_trend != "BEARISH"` /
   `!= "BULLISH"`, but `get_standard_market_state()` never returns those strings (only
   `SIDEWAYS`/`TRENDING`/`UNKNOWN`). The gate is effectively always-true. Let me know if you'd
   like this wired up properly (e.g. deriving BULLISH/BEARISH from price vs POC/VWAP) or left as
   is intentionally.
