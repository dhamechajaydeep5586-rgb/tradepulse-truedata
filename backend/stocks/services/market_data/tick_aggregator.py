"""
Tick-to-candle aggregator: builds FIVE_MINUTE OHLCV bars for the local CandleBar
store directly from live WebSocket ticks, instead of REST-fetching them via Angel
One's getCandleData endpoint.

Added 2026-07-27 so intraday_service.py and option_buying_service.py's breakout
detection can keep working even when getCandleData is blocked (as it was that
day) — the WebSocket stream is a completely separate Angel One channel from the
REST historical-candle endpoint, unaffected by that specific restriction.

roll_up_universe() only ever reads truedata_streamer's already-in-memory tick
cache (get_stream_price()) — zero network calls, zero rate-limit exposure. Safe
to call for the entire scan universe, frequently, unlike the REST-backed paths
elsewhere in this codebase that all have to be paced/serialized against Angel
One's own limits.

Two bottlenecks found and fixed the same day, both from treating the scan
universe (~200-400 symbols) one-at-a-time instead of as a batch:

1. Cache I/O: settings.py's CACHES backend is FileBasedCache (one file per key).
   The original design did one cache get + one cache set per symbol per tick —
   400-800 file operations every 15s. Fixed by keeping all symbols' in-progress
   bar state under ONE cache key, read once and written once per call.

2. DB writes: 5-minute windows are calendar-aligned, not per-symbol, so the
   entire universe's window closes in the same tick. Calling candle_store.
   store_bars() once per symbol meant a burst of ~200-400 sequential SELECT+
   INSERT round-trips every 5 minutes — confirmed in production to be slow
   enough that the aggregator's own scheduled runs missed several ticks in a
   row, every 5 minutes, indefinitely. Fixed by candle_store.store_completed_bars()
   — one batched insert for every symbol that finalized this tick, regardless
   of how many that is.

Real limitation, by construction, not a bug: this can only build up TODAY's bars
from whenever it starts running — there is no way to backfill "yesterday's" bars
from a live tick stream. REST (or bhavcopy, for daily bars) is still the only
source of genuine history. A symbol needs enough of today's session elapsed
before callers' own bar-count gates (e.g. option_buying_service's `len(df) < 10`)
let a signal through — same cold-start wait every session opens with now, not a
one-time bootstrap cost like candle_store's delta-fetch cache.
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.core.cache import cache

from stocks.services import candle_store
from stocks.services.truedata_streamer import get_stream_price
from stocks.services.signal_utils import IST

logger = logging.getLogger(__name__)

BAR_MINUTES = 5
_STATE_CACHE_KEY = "tick_agg_state_all"
_STATE_TTL = 20 * 3600  # outlives a trading day; a new day's first tick just starts fresh windows


def _window_start(now_ist: datetime) -> datetime:
    """Floor `now` to the start of its 5-minute bucket."""
    minute = (now_ist.minute // BAR_MINUTES) * BAR_MINUTES
    return now_ist.replace(minute=minute, second=0, microsecond=0)


def roll_up_universe(token_map: dict[str, str], exchange: str = "NSE") -> int:
    """Roll up already-arrived ticks for every symbol in `token_map` (symbol ->
    token) in one pass: one cache read at the start, one cache write at the end,
    and one batched DB insert for however many symbols' windows just closed.

    Returns how many symbols had a live tick to process this call.
    """
    now_ist = datetime.now(tz=IST)
    win_start = _window_start(now_ist)
    win_start_iso = win_start.isoformat()

    all_state: dict[str, dict] = cache.get(_STATE_CACHE_KEY) or {}
    processed = 0
    completed_rows: list[dict] = []

    for symbol, token in token_map.items():
        tick = get_stream_price(token)
        if not tick or tick.get("ltp", 0) <= 0:
            continue
        processed += 1

        ltp = float(tick["ltp"])
        cum_volume = float(tick.get("trade_volume", 0) or 0)
        key = f"{exchange}:{token}"
        state = all_state.get(key)

        if state is None or state["window_start"] != win_start_iso:
            if state is not None:
                completed_rows.append({
                    "symbol": symbol, "exchange": exchange, "interval": "FIVE_MINUTE",
                    "ts": datetime.fromisoformat(state["window_start"]),
                    "open": state["open"], "high": state["high"],
                    "low": state["low"], "close": state["close"],
                    "volume": max(0.0, state["vol_last"] - state["vol_start"]),
                })
            state = {
                "window_start": win_start_iso,
                "open": ltp, "high": ltp, "low": ltp, "close": ltp,
                "vol_start": cum_volume, "vol_last": cum_volume,
            }
        else:
            state["high"] = max(state["high"], ltp)
            state["low"] = min(state["low"], ltp)
            state["close"] = ltp
            state["vol_last"] = cum_volume

        all_state[key] = state

    cache.set(_STATE_CACHE_KEY, all_state, timeout=_STATE_TTL)

    if completed_rows:
        try:
            written = candle_store.store_completed_bars(completed_rows)
            logger.debug("[TICK_AGG] finalized %d/%d bars @ %s", written, len(completed_rows), win_start)
        except Exception as e:
            logger.warning("[TICK_AGG] batch finalize failed for %d bars: %s", len(completed_rows), e)

    return processed
