"""
Live LTP reads, sourced from the existing WebSocket tick store.

get_ltp() forwards to TrueDataService.get_live_price_by_token() per symbol, which
already does WS-first-then-REST-fallback, staleness checking, and self-healing
stream reconnects (see truedata_service.py). No new subscription logic and no new
polling of the REST quote endpoint is introduced here — this is a read-only
convenience over what already exists, matching CLAUDE.md's "always prefer
WebSocket over REST for LTP" rule by construction rather than by convention.
"""
from __future__ import annotations

from typing import Any

from stocks.services.truedata_service import TrueDataService


def get_ltp(svc: TrueDataService, symbols: list[str], exchange: str = "NSE") -> dict[str, dict[str, Any]]:
    """Resolve symbols to tokens, then read live price per token.

    Returns a dict keyed by symbol (not token) so callers don't need to carry
    their own token map around just to read a price.
    """
    if not symbols:
        return {}

    token_map = svc.get_token_map(symbols, exchange=exchange) or {}
    out: dict[str, dict[str, Any]] = {}
    for symbol, token in token_map.items():
        tick = svc.get_live_price_by_token(token, exchange=exchange)
        if tick:
            out[symbol] = tick
    return out
