"""
Market Data Gateway.

Phase 1 of doc/MARKET_DATA_ENGINE_ARCHITECTURE.md: the single module every engine
should import instead of calling truedata_service / candle_store directly. Nothing
here is a new behavior — every function is a thin forward to logic that already
exists and is already in production. Nothing in the codebase imports this package
yet (that migration is Phase 2); it exists first so Phase 2 has a stable, already
-verified boundary to move callers onto.
"""
from .gateway import get_service, request_candles, request_bulk_quote
from .ws_read import get_ltp

__all__ = ["get_service", "request_candles", "request_bulk_quote", "get_ltp"]
