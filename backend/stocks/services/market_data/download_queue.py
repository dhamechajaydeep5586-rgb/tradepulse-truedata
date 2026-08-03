"""
Download Queue: durable "fetch candles for symbol X, interval Y" requests.

Phase 3 of doc/MARKET_DATA_ENGINE_ARCHITECTURE.md (§2, §9, §12). Same
PENDING -> IN_PROGRESS -> DONE/FAILED pattern already proven in production for
`TelegramLog` — a Postgres table drained by a single poller, not a new
Celery/RQ worker tier, matching this app's actual scale (hundreds, not
millions, of rows per cycle). Redis is deliberately NOT introduced here (still
deferred platform-wide) — there is no hot/ephemeral state in this module that
needs it; the queue's durable state lives in `DownloadRequest` itself.

This pass is purely additive and low-risk by construction: `candle_store.get_candles()`
(reached via `market_data.gateway.request_candles`) already has its own
self-sufficient cold-fetch fallback, so this queue existing, sitting empty, or
never being drained changes nothing for any existing engine. Nothing calls
`enqueue()` yet in this pass — that producer wiring (something that enqueues
the day's universe) is intentionally left for later. `drain_once()` is wired
into the scheduler as a no-op-if-empty cache warmer so the mechanics are
proven end-to-end before anything depends on it.

Only imports `stocks.models` and `stocks.services.market_data.gateway` — per
the architecture rule, only `market_data/` may reach `truedata_service`, and
even then only through `gateway`/`get_service`.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone as dj_timezone

from stocks.models import DownloadRequest
from stocks.services.market_data import gateway

logger = logging.getLogger(__name__)

_NON_TERMINAL = [DownloadRequest.Status.PENDING, DownloadRequest.Status.IN_PROGRESS]
_DEFAULT_LOOKBACK_DAYS = 5
_MAX_REQUEUE_ATTEMPTS = 4  # rows past this many attempts are left permanently FAILED

_TRICKLE_CURSOR_KEY = "candle_queue_trickle_cursor_v2"
# _v2: renamed 2026-07-28 when the universe narrowed NIFTY200 -> NIFTY50, so this
# doesn't silently keep serving the old 200-symbol list out of stale state.


def get_universe_symbols() -> list[str]:
    """Raw NIFTY100 symbol list — delegates to signal_utils.fetch_nifty_symbols_live,
    the one shared live-fetch+cache used by every engine, so an NSE reconstitution
    (a symbol added/removed from the index) lands here the same as everywhere else.
    Deliberately NOT shared.universe.get_trading_universe(), which itself needs
    liquidity stats (i.e. candle data) to filter down to it — using that here would be
    circular: the whole point of the trickle warmer is to build up the candle data
    that filter depends on.
    """
    from stocks.services.signal_utils import fetch_nifty_symbols_live

    return sorted(set(fetch_nifty_symbols_live("NIFTY100")))


def enqueue_next_trickle_symbol(interval: str = "ONE_DAY") -> DownloadRequest | None:
    """This is the producer this module's own docstring said was "intentionally left
    for later" — called once per scheduler tick (updater.py's candle_trickle_warmer
    job) to enqueue exactly one symbol, rotating through the universe.

    Why one at a time: the 2026-07-27 rate-limit incident showed shared/universe.py's
    _liquidity_stats() doing ~150-200 candle calls back-to-back at scan time — paced at
    1.5s/call (respecting Angel One's own documented per-second limit) but still all
    crammed into the first few minutes of every 15-min cycle, i.e. a burst. Spreading
    the exact same work across one tick per symbol (see updater.py for the interval)
    means the local CandleBar store fills in gradually instead. Once warm,
    _liquidity_stats()'s 6h cache mostly reads local data instead of triggering its own
    fetch burst.

    Does NOT fix an already-blocked account — a single isolated call fails identically
    to a burst (confirmed 2026-07-27 with a standalone script making exactly one
    request). Only reduces how often bursts happen once Angel One access is normal.
    """
    universe = get_universe_symbols()
    if not universe:
        return None

    from django.core.cache import cache

    cursor = cache.get(_TRICKLE_CURSOR_KEY, 0) % len(universe)
    symbol = universe[cursor]
    cache.set(_TRICKLE_CURSOR_KEY, (cursor + 1) % len(universe), timeout=None)

    return enqueue(symbol, "NSE", interval, requested_by="candle_trickle_warmer")


def enqueue(symbol: str, exchange: str, interval: str, since_ts=None, requested_by: str = "") -> DownloadRequest:
    """Get-or-create a PENDING row for (symbol, exchange, interval).

    Respects the partial unique constraint on DownloadRequest (only one
    non-terminal row per key) — calling this twice while a row is still
    PENDING/IN_PROGRESS returns the existing row instead of raising or
    creating a duplicate.
    """
    existing = (
        DownloadRequest.objects
        .filter(symbol=symbol, exchange=exchange, interval=interval, status__in=_NON_TERMINAL)
        .order_by("-created_at")
        .first()
    )
    if existing:
        return existing

    try:
        return DownloadRequest.objects.create(
            symbol=symbol, exchange=exchange, interval=interval,
            since_ts=since_ts, requested_by=requested_by,
        )
    except IntegrityError:
        # Lost a race against a concurrent enqueue() for the same key — reuse
        # whichever row won instead of raising.
        existing = (
            DownloadRequest.objects
            .filter(symbol=symbol, exchange=exchange, interval=interval, status__in=_NON_TERMINAL)
            .order_by("-created_at")
            .first()
        )
        if existing:
            return existing
        raise


def _requeue_failed_rows(batch_limit: int) -> int:
    """Give FAILED rows another shot by resetting them back to PENDING, using the
    existing `attempts` counter (already incremented on every failure) as the backoff
    signal. Rows that have already failed `_MAX_REQUEUE_ATTEMPTS` times are left FAILED
    permanently instead of retried forever against a systematically bad symbol/token —
    previously drain_once() never revisited a FAILED row at all.
    """
    stale_ids = list(
        DownloadRequest.objects
        .filter(status=DownloadRequest.Status.FAILED, attempts__lt=_MAX_REQUEUE_ATTEMPTS)
        .order_by("updated_at")
        .values_list("id", flat=True)[:batch_limit]
    )
    if not stale_ids:
        return 0
    DownloadRequest.objects.filter(id__in=stale_ids).update(
        status=DownloadRequest.Status.PENDING, updated_at=dj_timezone.now()
    )
    return len(stale_ids)


def drain_once(svc, batch_limit: int = 200) -> dict:
    """Process up to `batch_limit` PENDING rows, oldest first.

    Resolves tokens with one `svc.get_token_map()` call per unique exchange
    present in the batch — not one call per symbol — mirroring the existing
    batching pattern in `shared/universe.py::_liquidity_stats`. Each row is
    processed in its own try/except so one bad row can't abort the batch.
    Rows beyond `batch_limit` are left PENDING and picked up on the next call,
    so a large backlog can never block a single drain indefinitely.

    Marks DONE on success — `request_candles` already persists via
    `candle_store.store_bars()` internally, so there is no separate store step
    here. Marks FAILED with `last_error` set and `attempts` incremented on any
    exception; never raises out of this function.

    Before picking up PENDING rows, also requeues previously-FAILED rows back to
    PENDING (up to `_MAX_REQUEUE_ATTEMPTS` attempts each) via `_requeue_failed_rows()`
    — otherwise a transient failure (token hiccup, one-off REST error) parked a row in
    FAILED forever with no other path back to PENDING.
    """
    summary = {"picked": 0, "done": 0, "failed": 0, "skipped_no_token": 0, "requeued": 0}

    if svc is None:
        logger.info("[DOWNLOAD_QUEUE] No Angel One service instance available; skipping drain.")
        return summary

    summary["requeued"] = _requeue_failed_rows(batch_limit)

    # select_for_update(skip_locked=True) + the claiming UPDATE happen inside the
    # same atomic transaction so the SELECT and the claim are a single round-trip
    # from Postgres's point of view: FOR UPDATE SKIP LOCKED means a concurrent
    # drain (run_download_queue_drain every 5 min vs. run_candle_trickle_warmer
    # every 2 min, both in stocks/updater.py) simply skips rows this call already
    # has locked, instead of both selecting the same PENDING rows before either
    # claims them.
    with transaction.atomic():
        rows = list(
            DownloadRequest.objects
            .select_for_update(skip_locked=True)
            .filter(status=DownloadRequest.Status.PENDING)
            .order_by("created_at")[:batch_limit]
        )
        if not rows:
            return summary

        row_ids = [r.id for r in rows]
        DownloadRequest.objects.filter(id__in=row_ids).update(
            status=DownloadRequest.Status.IN_PROGRESS, updated_at=dj_timezone.now()
        )
    summary["picked"] = len(rows)

    # One get_token_map() call per unique exchange in this batch.
    by_exchange: dict[str, list[str]] = {}
    for r in rows:
        by_exchange.setdefault(r.exchange, []).append(r.symbol)

    token_maps: dict[str, dict[str, str]] = {}
    for exch, symbols in by_exchange.items():
        try:
            token_maps[exch] = svc.get_token_map(list(set(symbols)), exchange=exch) or {}
        except Exception as e:
            logger.warning("[DOWNLOAD_QUEUE] get_token_map failed for exchange=%s: %s", exch, e)
            token_maps[exch] = {}

    for row in rows:
        try:
            token = token_maps.get(row.exchange, {}).get(row.symbol)
            if not token:
                row.attempts += 1
                row.status = DownloadRequest.Status.FAILED
                row.last_error = f"No token resolved for {row.symbol} on {row.exchange}"
                row.save(update_fields=["status", "attempts", "last_error", "updated_at"])
                summary["skipped_no_token"] += 1
                summary["failed"] += 1
                continue

            lookback_days = _DEFAULT_LOOKBACK_DAYS
            if row.since_ts is not None:
                delta_days = (dj_timezone.now() - row.since_ts).days + 1
                lookback_days = max(1, delta_days)

            # request_candles() routes through candle_store, which persists
            # via store_bars() internally — no separate store step needed here.
            gateway.request_candles(
                svc, row.symbol, token, row.interval,
                lookback_days=lookback_days, exchange=row.exchange,
            )

            row.status = DownloadRequest.Status.DONE
            row.save(update_fields=["status", "updated_at"])
            summary["done"] += 1
        except Exception as e:
            logger.warning(
                "[DOWNLOAD_QUEUE] drain failed for %s/%s/%s: %s",
                row.symbol, row.exchange, row.interval, e,
            )
            try:
                row.attempts += 1
                row.status = DownloadRequest.Status.FAILED
                row.last_error = str(e)[:2000]
                row.save(update_fields=["status", "attempts", "last_error", "updated_at"])
            except Exception:
                logger.exception("[DOWNLOAD_QUEUE] failed to record failure for row id=%s", row.id)
            summary["failed"] += 1

    logger.info("[DOWNLOAD_QUEUE] drain_once summary: %s", summary)
    return summary
