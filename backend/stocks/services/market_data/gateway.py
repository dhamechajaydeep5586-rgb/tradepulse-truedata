"""
Gateway functions for historical candles and bulk quotes.

Each function here forwards, argument-for-argument, to the existing implementation
in `truedata_service.py` / `candle_store.py`. No new rate limiting, caching,
circuit-breaking, or retry logic is introduced — all of that already lives where
it always has, and stays there. Callers pass `svc` explicitly (matching the
existing `intraday_service.py` convention: obtain it once via `get_service()`,
check it's not None, then use it) rather than this module hiding that check —
the whole point of Phase 1 is zero new behavior, including zero new null-handling
conventions.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from stocks.services import candle_store
from stocks.services.truedata_service import TrueDataService, get_truedata_instance


def get_service() -> TrueDataService | None:
    """The one TrueData client instance. Straight re-export of
    truedata_service.get_truedata_instance() — see that function for the
    singleton/auth logic. Returns None if not yet authenticated."""
    return get_truedata_instance()


def request_candles(
    svc: TrueDataService,
    symbol: str,
    token: str,
    interval: str,
    lookback_days: int,
    exchange: str = "NSE",
    allow_fetch: bool = True,
) -> pd.DataFrame:
    """Cache-first historical candles for one symbol.

    Forwards to candle_store.get_candles() — the existing delta-fetch cache used
    today by intraday_service.py — so callers migrated onto this function get
    "don't refetch what we already have" for free, with no behavior change versus
    calling candle_store directly.
    """
    return candle_store.get_candles(
        svc, symbol, token, interval, lookback_days,
        exchange=exchange, allow_fetch=allow_fetch,
    )


def request_bulk_quote(
    svc: TrueDataService,
    exchange_token_map: dict[str, list[str]],
    mode: str = "FULL",
) -> dict[str, Any]:
    """Forwards to TrueDataService.get_bulk_quotes() unchanged."""
    return svc.get_bulk_quotes(exchange_token_map, mode=mode)
