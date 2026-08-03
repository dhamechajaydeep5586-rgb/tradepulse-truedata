# Option Selling

## What it is
Market-neutral premium-collection strategy — short strangles (sell a Call + a Put) on stocks/commodities expected to stay range-bound, profiting from time decay (theta).

## Where the data comes from
- **Scan engine:** `backend/stocks/services/delta_hedge_service.py` → `get_hedge_panel_data()` / `_background_scan()`
- **Universe:** 2 MCX commodities (`SPECIALISTS` — Crude Oil, Natural Gas) + Nifty-50 stocks
- **Trigger logic:**
  1. Qualify underlyings that are **sideways / inside their Value Area** (`get_symbol_market_state()` — sideways-market + VWAP/VA check)
  2. Pick strikes far OTM via `find_strike_by_delta()`, targeting `SHORT_DELTA = 0.25` (low delta = high theta, low assignment risk)
  3. Sell a Call leg and a Put leg simultaneously (the "strangle")
  4. Positions **rebalance** when CE/PE delta imbalance grows too large (`rebalance_delta_neutral_strangle()`), adjusting legs to stay roughly delta-neutral

## Where it's stored
`SignalHistory`, `category="specialist"`. Unlike other categories, **one row = one full strangle position** (not one row per leg) — both legs (strike, premium, delta, theta, entry price, current price) live inside the `metadata['legs']` JSON field as a list of 2 dicts.

## Lifecycle
- Self-contained — **not** covered by the shared `update_signal_outcomes()` (that function explicitly excludes `category='specialist'`). Delta-hedge audits and rebalances its own positions inside `get_hedge_panel_data()`.

## What decides which ones show on the Dashboard card
```python
SignalHistory.objects.filter(
    category='specialist',
    status__in=[PENDING, ACTIVE],
).order_by('-generated_at')[:3]
```
Same "currently open" rule again — closed/expired strangles drop out. One row = one full strangle (both legs), so "3" here means 3 underlyings, not 3 option contracts. The `9` count badge is the total number of open strangle positions across both sectors (commodity + equity). Note the `entry_premium` shown in the preview is the **combined CE+PE premium at entry** (summed from `metadata['legs']`), not a live number — the full `/option-selling` page shows live current premium and P&L per leg, refreshed every 3 seconds.

## API endpoints
- `GET /api/stocks/delta-hedge/` — full nested view (exchange → sector group → position → legs), 2-second cache, live P&L per leg
- `GET /api/stocks/dashboard-summary/` → `option_selling` key, powered by a **new, narrow helper** `get_hedge_panel_summary()` — deliberately does *not* call the full `get_hedge_panel_data()` (which does live scanning), just reads the already-stored `metadata['legs']` for a cheap top-3 preview with zero Angel One REST calls.

## Frontend
- **Preview card** (Dashboard): symbol, exchange, leg count, entry premium, status
- **Full view:** `/option-selling` → `OptionSellingFull.jsx` → wraps the existing `DeltaHedgePanel.jsx` (3-second live poll, full leg-by-leg P&L, rebalance controls)

## Current status (as tested)
9 total live positions — EICHERMOT, DIVISLAB, CIPLA all `ACTIVE` shown in preview; full view showed 11 sector positions including CRUDEOIL, ADANIPORTS, APOLLOHOSP, ASIANPAINT, BAJFINANCE, BRITANNIA, ADANIENT, BAJAJ-AUTO with real live P&L.
