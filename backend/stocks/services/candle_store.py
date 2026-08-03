"""
Local OHLCV persistence with delta fetching.

See doc/INSTITUTIONAL_AUDIT_INTRADAY.md §9.3. The Angel One candle endpoint enforces a
~1 call/sec global lock and trips a 5-minute circuit breaker on 403. Re-pulling full
history for every symbol on every scan cycle is what caps the tradable universe and
makes bulk historical download — the precondition for any walk-forward validation —
impractical.

Storing bars locally changes the access pattern from "fetch N days per symbol per cycle"
to "fetch only bars newer than the last stored one", which is normally a handful of bars
or, on a repeat call within the same bar, nothing at all.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd
from django.db import transaction
from django.utils import timezone as dj_timezone

from stocks.models import CandleBar
from .signal_utils import IST

logger = logging.getLogger(__name__)

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _to_dataframe(rows) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)
    df = pd.DataFrame(
        [(r.ts, float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
         for r in rows],
        columns=["Datetime"] + _COLUMNS,
    ).set_index("Datetime")
    df.index = pd.DatetimeIndex(df.index).tz_convert(IST)
    return df


def latest_stored_ts(symbol: str, interval: str, exchange: str = "NSE"):
    row = (CandleBar.objects
           .filter(symbol=symbol, exchange=exchange, interval=interval)
           .order_by("-ts").values_list("ts", flat=True).first())
    return row


# Symbols never persisted to CandleBar, at the account owner's explicit request
# (2026-07-28). NIFTY50_INDEX still gets fetched and read in-memory by every caller
# that needs it (get_market_direction(), short-term's relative-strength benchmark,
# intraday's NIFTY comparison) — it just never lands in the table, so every one of
# those reads falls through to a fresh REST/live fetch instead of serving from cache.
_NEVER_PERSIST_SYMBOLS = {"NIFTY50_INDEX"}


def store_bars(symbol: str, interval: str, df: pd.DataFrame, exchange: str = "NSE") -> int:
    """Upsert bars. Returns the number of new rows written.

    The final bar of a live fetch is still forming, so it is deliberately NOT persisted —
    storing it would bake a partial bar into history and reintroduce the repaint problem
    at the data layer, where it would be far harder to notice.
    """
    if symbol in _NEVER_PERSIST_SYMBOLS:
        return 0

    if df is None or df.empty:
        return 0

    df = df.iloc[:-1] if len(df) > 1 else df
    if df.empty:
        return 0

    existing = set(
        CandleBar.objects.filter(
            symbol=symbol, exchange=exchange, interval=interval,
            ts__gte=df.index.min(), ts__lte=df.index.max(),
        ).values_list("ts", flat=True)
    )

    objs = []
    for ts, row in df.iterrows():
        ts_aware = ts.to_pydatetime()
        if dj_timezone.is_naive(ts_aware):
            ts_aware = ts_aware.replace(tzinfo=IST)
        if ts_aware in existing:
            continue
        objs.append(CandleBar(
            symbol=symbol, exchange=exchange, interval=interval, ts=ts_aware,
            open=round(float(row["Open"]), 2), high=round(float(row["High"]), 2),
            low=round(float(row["Low"]), 2), close=round(float(row["Close"]), 2),
            volume=int(row.get("Volume", 0) or 0),
        ))

    if not objs:
        return 0
    with transaction.atomic():
        CandleBar.objects.bulk_create(objs, ignore_conflicts=True, batch_size=1000)
    return len(objs)


def store_completed_bars(rows: list[dict]) -> int:
    """Batch upsert already-complete bars — one bulk_create for the whole list instead
    of store_bars()'s one-existing-check-query-plus-one-insert-per-symbol pattern.

    Each row: {"symbol", "exchange", "interval", "ts", "open", "high", "low", "close",
    "volume"}. No existing-check query first (unlike store_bars()) — relies entirely
    on the DB's own unique constraint via ignore_conflicts=True, since the caller here
    (market_data/tick_aggregator.py) only ever passes genuinely-new completed bars.

    Added 2026-07-27: 5-minute bar windows are calendar-aligned, not per-symbol, so
    the entire scan universe's window closes in the same tick — tick_aggregator.py's
    original per-symbol store_bars() calls meant a burst of ~200-400 sequential SELECT
    + INSERT round-trips every 5 minutes. Confirmed in production: that burst was long
    enough to make the aggregator's own scheduled tick miss its next few runs, every
    5 minutes, indefinitely. One batched insert fixes it regardless of burst size.
    """
    if not rows:
        return 0
    rows = [r for r in rows if r["symbol"] not in _NEVER_PERSIST_SYMBOLS]
    if not rows:
        return 0
    objs = [
        CandleBar(
            symbol=r["symbol"], exchange=r["exchange"], interval=r["interval"], ts=r["ts"],
            open=round(float(r["open"]), 2), high=round(float(r["high"]), 2),
            low=round(float(r["low"]), 2), close=round(float(r["close"]), 2),
            volume=int(r["volume"] or 0),
        )
        for r in rows
    ]
    with transaction.atomic():
        CandleBar.objects.bulk_create(objs, ignore_conflicts=True, batch_size=1000)
    return len(objs)


def load_bars(symbol: str, interval: str, start, end=None, exchange: str = "NSE") -> pd.DataFrame:
    """Read stored bars in [start, end]."""
    qs = CandleBar.objects.filter(
        symbol=symbol, exchange=exchange, interval=interval, ts__gte=start
    )
    if end is not None:
        qs = qs.filter(ts__lte=end)
    return _to_dataframe(list(qs.order_by("ts")))


def get_candles(
    svc, symbol: str, token: str, interval: str, lookback_days: int,
    exchange: str = "NSE", allow_fetch: bool = True,
) -> pd.DataFrame:
    """Cache-first candle access: serve from the store, fetch only the missing tail.

    Falls back to a direct broker fetch when nothing is stored yet (cold start).
    """
    now_ist = datetime.now(tz=IST)
    start = now_ist - timedelta(days=lookback_days)

    stored = load_bars(symbol, interval, start, exchange=exchange)
    last_ts = stored.index.max() if not stored.empty else None

    if not allow_fetch:
        return stored

    # Nothing newer to ask for if the most recent stored bar is still the current one.
    bar_minutes = {"ONE_MINUTE": 1, "FIVE_MINUTE": 5, "FIFTEEN_MINUTE": 15,
                   "ONE_DAY": 1440}.get(interval, 5)
    if last_ts is not None and (now_ist - last_ts) < timedelta(minutes=bar_minutes):
        return stored

    fetch_from = (last_ts - timedelta(minutes=bar_minutes)) if last_ts is not None else start
    fmt = "%Y-%m-%d %H:%M"   # required for all intervals — see backfill_symbol()

    try:
        fresh = svc.get_candle_data(
            token, exchange, interval, fetch_from.strftime(fmt), now_ist.strftime(fmt)
        )
    except Exception as exc:
        logger.warning("[CANDLE_STORE] fetch failed for %s: %s", symbol, exc)
        return stored

    if fresh is None or fresh.empty:
        return stored

    if fresh.index.tz is None:
        fresh.index = fresh.index.tz_localize(IST)
    else:
        fresh.index = fresh.index.tz_convert(IST)

    written = store_bars(symbol, interval, fresh, exchange=exchange)
    if written:
        logger.debug("[CANDLE_STORE] %s %s: +%d bars", symbol, interval, written)

    if stored.empty:
        return fresh
    combined = pd.concat([stored, fresh])
    return combined[~combined.index.duplicated(keep="last")].sort_index()


# Angel One caps the date range a single candle request may return, per interval.
# Exceeding it returns an error rather than truncating, so history must be fetched in
# chunks. Values are set below the documented maxima to leave headroom.
_MAX_DAYS_PER_REQUEST = {
    "ONE_MINUTE": 25,
    "THREE_MINUTE": 60,
    "FIVE_MINUTE": 90,
    "TEN_MINUTE": 90,
    "FIFTEEN_MINUTE": 180,
    "THIRTY_MINUTE": 180,
    "SIXTY_MINUTE": 365,
    "ONE_DAY": 1825,
}


def backfill_symbol(svc, symbol: str, token: str, interval: str, days: int,
                    exchange: str = "NSE", skip_existing: bool = True) -> dict:
    """Download `days` of history for one symbol, in API-sized chunks.

    Walks oldest -> newest so an interrupted run leaves a contiguous block rather than
    islands. Chunks that already contain stored bars are skipped, which makes the whole
    job resumable: re-running after a failure costs only the missing tail.
    """
    chunk_days = _MAX_DAYS_PER_REQUEST.get(interval, 90)
    # Angel One requires "YYYY-MM-DD HH:MM" for EVERY interval, daily included. Sending
    # a bare date returns HTTP 400, not a truncated result — verified against the live
    # endpoint: '2026-03-28' -> 0 rows, '2026-03-28 11:56' -> 79 rows.
    fmt = "%Y-%m-%d %H:%M"
    now_ist = datetime.now(tz=IST)

    written = requests_made = skipped = 0
    cursor = now_ist - timedelta(days=days)

    while cursor < now_ist:
        chunk_end = min(cursor + timedelta(days=chunk_days), now_ist)

        if skip_existing:
            already = CandleBar.objects.filter(
                symbol=symbol, exchange=exchange, interval=interval,
                ts__gte=cursor, ts__lt=chunk_end,
            ).exists()
            if already:
                skipped += 1
                cursor = chunk_end
                continue

        try:
            df = svc.get_candle_data(
                token, exchange, interval,
                cursor.strftime(fmt), chunk_end.strftime(fmt),
            )
            requests_made += 1
            if df is not None and not df.empty:
                if df.index.tz is None:
                    df.index = df.index.tz_localize(IST)
                else:
                    df.index = df.index.tz_convert(IST)
                # A historical chunk that ends in the past has no forming bar, so keep
                # every row; store_bars() drops the last one, which is only correct for
                # a live fetch. Append a sentinel so nothing real is discarded.
                written += _store_all(symbol, interval, df, exchange)
        except Exception as exc:
            logger.warning("[BACKFILL] %s %s..%s failed: %s",
                           symbol, cursor.date(), chunk_end.date(), exc)

        cursor = chunk_end

    return {"symbol": symbol, "written": written,
            "requests": requests_made, "skipped_chunks": skipped}


def _store_all(symbol: str, interval: str, df: pd.DataFrame, exchange: str) -> int:
    """store_bars() without the forming-bar trim — for completed historical chunks."""
    if df is None or df.empty:
        return 0
    padded = pd.concat([df, df.tail(1)])   # sentinel row absorbs the trim
    return store_bars(symbol, interval, padded, exchange=exchange)


def backfill(svc, symbols: list[str], interval: str = "FIVE_MINUTE",
             days: int = 60, exchange: str = "NSE") -> dict:
    """Bulk historical download for validation runs.

    Intended to be run offline (management command / one-off), not inline in a scan:
    the broker's 1 call/sec lock means a few hundred symbols takes minutes to hours.
    This is the job that unblocks walk-forward and Monte Carlo analysis.
    """
    token_map = svc.get_token_map(symbols, exchange=exchange) or {}
    total_bars = 0
    failed: list[str] = []

    for i, (sym, token) in enumerate(token_map.items(), 1):
        try:
            df = get_candles(svc, sym, token, interval, days, exchange=exchange)
            total_bars += len(df)
            if i % 25 == 0:
                logger.info("[CANDLE_STORE] backfill %d/%d symbols", i, len(token_map))
        except Exception as exc:
            logger.warning("[CANDLE_STORE] backfill failed for %s: %s", sym, exc)
            failed.append(sym)

    return {"symbols": len(token_map), "bars": total_bars, "failed": failed}
