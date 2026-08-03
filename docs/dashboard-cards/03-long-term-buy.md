# Long-Term Buy

## What it is
Multi-week/month "sector leader" picks — the slowest-moving category, no same-session exit pressure.

## Where the data comes from
- **Scan engine:** `backend/stocks/services/pro_system_service.py` → `scan_long_term_stocks()` / `_fetch_long_term_quality()`
- **Universe:** Nifty 50
- **Trigger logic:** intended as fundamentals screening (ROE, growth, debt) to find the strongest stock per sector. **Known limitation:** as of this build, the ROE value used is a hardcoded placeholder (`22.5`), not a live-fetched fundamental — flagged deliberately so this isn't mistaken for real analysis. The "Top Sector Leader" selection logic itself runs, but the underlying quality number isn't real yet.

## Where it's stored
`SignalHistory`, `category="long_term"`. Standard 6-state status (`PENDING/ACTIVE/HIT_TARGET/HIT_SL/CANCELLED/EXPIRED`) — no target2/target3/AI-score fields like Short-Term has.

## The bug this card fixed
This data was **already being generated and saved** before this change — `scan_long_term_stocks()` ran, `SignalHistory` rows got created — but no API endpoint ever surfaced it. `ProSystemView`'s backing function, `get_dashboard_data()`, only ever queried the *separate* `ShortTermSignal` model. Long-term picks existed in the database, invisible to every page. This build added a parallel `long_term` block to `get_dashboard_data()`'s response and a Short-Term/Long-Term toggle on the frontend to expose it for the first time.

## Lifecycle
- No same-day auto-square-off, by design — unlike every other category here, a long-term signal just sits `PENDING`/`ACTIVE` until it naturally hits target, hits stop-loss, or is manually cancelled.

## What decides which ones show on the Dashboard card
```python
SignalHistory.objects.filter(
    category='long_term',
    status__in=[ACTIVE, PENDING],
).order_by('-generated_at')[:3]
```
Same "only currently open" rule as Intraday. **This is why the preview card can say "No active long-term signals" while the full `/pro-system?view=long_term` page shows 5 real signals** — all 5 stored signals (ASIANPAINT, BEL, COALINDIA, TCS, BAJAJ-AUTO) are `HIT_TARGET`, i.e. already resolved, so none qualify as "currently open" for the compact preview. The full page has no status filter — it shows every tab, including closed ones, which is where those 5 are actually visible.

## API endpoints
- `GET /api/stocks/pro-system/` → now also returns a top-level `long_term: { tabs, analytics }` block (additive change — the pre-existing `tabs`/`analytics` keys for short-term are untouched)
- `GET /api/stocks/dashboard-summary/` → `long_term` key — top-3 preview

## Frontend
- **Preview card** (Dashboard): symbol, reason (e.g. "Top Sector Leader (Technology)"), status pill
- **Full view:** `/pro-system?view=long_term` → same `ProSystem.jsx` page, new toggle switches to a simplified table (Stock / Reason / Status / Entry / SL-Target / Generated) with its own tab set: Pending / Active / Hit Target / Hit SL / Expired / Cancelled

## Current status (as tested)
5 historical signals, all `HIT_TARGET`: ASIANPAINT, BEL, COALINDIA, TCS, BAJAJ-AUTO — 100% win rate shown, all previously invisible before this fix.
