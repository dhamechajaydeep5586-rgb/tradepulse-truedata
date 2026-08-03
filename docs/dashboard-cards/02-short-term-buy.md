# Short-Term Buy

## What it is
Multi-day swing setups on trending Nifty 500 stocks — positions are expected to be held for days, not closed same-day.

## Where the data comes from
- **Scan engine:** `backend/stocks/services/pro_system_service.py` → `scan_short_term_stocks()`
- **Universe:** Nifty 500
- **Trigger logic:** `_analyze_short_term()` — a trend-following filter, not a breakout:
  - **Price > 50-EMA > 200-EMA** (an established uptrend already in place)
  - Volume confirmation (`min_vol_ratio`, default 1.5×)
  - Proximity to 52-week high
- This is philosophically different from Intraday Buy: it looks for strength *already confirmed*, not a fresh breakout moment.

## Where it's stored
Its own dedicated Django model, `ShortTermSignal` — **not** `SignalHistory**. It has a richer status set than any other category because it supports multi-target exits:
`PENDING → ACTIVE → TARGET1 → TARGET2 → HIT_TARGET / HIT_SL / TRAILING_EXIT / TIME_STOP → EXPIRED / CANCELLED / REVIEW_REQUIRED / CLOSED / ARCHIVED / COOLDOWN`

Extra fields not present on other categories: `target2`, `target3`, `highest_profit`, `max_drawdown`, `ai_score`, `expected_holding_days`, `review_required`, `cooldown_until`.

## Lifecycle
- Scanned once daily at **10:00 AM** (`trade_engine_scanner_10am` job) via `run_short_term_scan` / `run_daily_scanner` in `trade_engine.py`.
- Entry activation checked every 30 min (10:15 AM–3:15 PM).
- EOD evaluation at 3:25 PM trails stops and checks target/SL, but does **not** force-close positions same-day (unlike Intraday) — these are meant to carry over days, up to `expected_holding_days`.

## What decides which ones show on the Dashboard card
```python
ShortTermSignal.objects.exclude(
    status__in=[HIT_TARGET, HIT_SL, CLOSED, CANCELLED, EXPIRED, ARCHIVED],
).order_by('-generated_at')[:3]
```
This is an **exclude**, not an include-list, because Short-Term has more "still open" states than the other categories (PENDING, ACTIVE, TARGET1 — partial target hit, TARGET2, REVIEW_REQUIRED all count as live). Anything that's fully resolved (hit final target/SL, closed, expired, cancelled, or archived) is left out. The 3 most recently generated of what remains are shown; the `11` badge is the total live count, not the visible row count. The full `/pro-system` page has no such filter — every status, including fully closed ones, gets its own tab.

## API endpoints
- `GET /api/stocks/pro-system/` → `ProSystemView` → `get_dashboard_data()` in `trade_engine.py` — full tab-grouped view (Pending/Active/Target1/Target2/Review/Archived/Expired), 30s server-side cache
- `GET /api/stocks/dashboard-summary/` → `short_term` key — top-3 preview (excludes fully-closed statuses)

## Frontend
- **Preview card** (Dashboard): symbol, setup name, entry price, status pill
- **Full view:** `/pro-system` (default tab) → `ProSystem.jsx` — status tabs, AI score badges, risk calculator sidebar, AI pipeline timeline

## Current status (as tested)
11 total signals — FEDERALBNK (ACTIVE, entry ₹349), LTF (PENDING, ₹296.91), WELCORP (PENDING, ₹1532.3) shown in preview. Real data, actively tracked.
