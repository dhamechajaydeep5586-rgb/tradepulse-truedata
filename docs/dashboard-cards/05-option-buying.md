# Option Buying

## What it is
**Brand new engine, built in this project** — directional option buying (buy a Call or a Put outright) on high-conviction breakouts. This is the opposite philosophy to Option Selling: buyers need the underlying to make a *real move* to overcome the premium paid and time decay working against them, whereas sellers profit from the underlying staying still.

## Where the data comes from
- **Scan engine:** `backend/stocks/services/option_buying_service.py` (new file) → `get_option_buying_signals()`
- **Universe:** F&O-eligible Nifty stocks (`svc.get_fo_stocks()`)
- **Trigger logic** (`_option_breakout_logic()`) — deliberately **stricter** than Intraday Buy's breakout trigger, since a losing options bet costs more than a losing stock bet:
  1. Same Value-Area-Breakout condition as Intraday (`vol_ratio > 1.5`, price crossing VAH/VAL)
  2. **+ VWAP alignment required** (price above VWAP for a Call, below for a Put) — not enforced by the base breakout condition
  3. **+ ADX(14) > 20 required** — a brand-new trend-strength utility (`compute_adx()` in `signal_utils.py`, this project's first ADX implementation) to filter out choppy, directionless breakouts
- **Strike selection** (`select_option_buying_strike()`): resolves the nearest real tradable strike (`get_nearest_strike()`), fetches its live premium, estimates implied volatility from that premium, then computes Black-Scholes delta — only accepts strikes with **delta between 0.40–0.60** (near-the-money, behaves like the underlying) — the opposite of Option Selling's deep-OTM 0.25-delta target.
- **Target/SL are in premium space, not spot price:** Target = entry premium × 1.6–2.0 (scales up with ADX strength), SL = entry premium × 0.625.
- **Hard time-stop at 2:30 PM** — force-exits regardless of P&L. This exists nowhere else in the codebase: sellers want time to pass (theta helps them), buyers are hurt by every extra minute a position sits open.

## Where it's stored
`SignalHistory`, `category="option_buying"`. `entry_price` = the option premium (not the underlying's spot price). `strike_price`, `option_type` (CE/PE), `premium_cmp` (live-tracked current premium) are populated.

## Lifecycle
- Self-contained, same precedent as Option Selling: `update_option_buying_outcomes()` runs its own audit loop, re-fetching premiums and applying target/SL/time-stop — not part of the shared `update_signal_outcomes()`.
- Own stale-signal guard, cancelling any leftover PENDING/ACTIVE rows from a previous day at scan start.
- Capped at 3 new signals per scan cycle.

## Where it sits in the scan order
Runs in `run_periodic_scanners()` **after** the commodity and option-selling scans (preserving today's rate-limit-safe ordering) but **before** the heavy 500-symbol Intraday scan — because its 2:30 PM cutoff is earlier than Intraday's 3:20 PM, so it needs to run while there's still time on its own clock.

## What decides which ones show on the Dashboard card
```python
SignalHistory.objects.filter(
    category='option_buying',
    status__in=[ACTIVE, PENDING],
).order_by('-generated_at')[:3]
```
Same rule as every other category — only currently-open positions qualify, most recent first, capped at 3 for the preview. Right now this is empty simply because the engine hasn't had a live market session to scan yet since being built, not because of the filter.

## API endpoints
- `GET /api/stocks/option-buying/` — full list, DB-only read
- `GET /api/stocks/dashboard-summary/` → `option_buying` key — top-3 preview

## Frontend
- **Preview card** (Dashboard): symbol + CE/PE, strike, entry premium, current premium, status
- **Full view:** `/option-buying` → `OptionBuying.jsx` → new `OptionBuyingTable.jsx` (symbol, type, strike, entry/current premium, target, SL, status columns)

## Current status (as tested)
No active signals yet — the scanner hasn't run against live market hours since being built (development happened after market close). Page renders correctly with the empty state; behavior will only be confirmed live once it runs during actual market hours.
