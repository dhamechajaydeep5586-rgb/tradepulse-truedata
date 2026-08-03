import logging
from typing import Any, Dict, Optional
from .truedata_service import get_truedata_instance
from django.core.cache import cache

logger = logging.getLogger(__name__)

class MarketDataOrchestrator:
    """
    JD Market Data Orchestrator:
    100% Angel One — no external fallback providers.
    """
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MarketDataOrchestrator, cls).__new__(cls)
        return cls._instance

    def get_option_data(self, symbol: str, strike: float, option_type: str, expiry: str, exchange: str = "NSE") -> Dict[str, Any] | tuple[Dict[str, Any], str | None]:
        """
        Unified Option Data Layer — Angel One only.
        """
        cache_key = f"orch_opt_{symbol}_{strike}_{option_type}_{expiry}"
        cached = cache.get(cache_key)
        if cached: return cached

        res = {"ltp": 0, "delta": 0, "theta": 0, "provider": "NONE", "status": "PENDING"}
        token = None
        
        # 1. Primary & Only Source: Angel One
        try:
            svc = get_truedata_instance()
            if svc:
                from .delta_hedge_service import get_nse_option_quote
                q_res = get_nse_option_quote(symbol, strike, option_type, expiry)

                quote = q_res[0] if isinstance(q_res, tuple) else q_res
                token = q_res[1] if isinstance(q_res, tuple) else None

                if quote and quote.get("ltp", 0) > 0:
                    res.update(quote)
                    res["provider"] = "Angel One"
                    res["status"] = "LIVE"
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Angel One failed for {symbol}: {e}")

        # Note: We don't cache the token in the 2s cache as it's meant for LTP, 
        # but the caller can handle the tuple.
        if token:
            return res, token
        
        cache.set(cache_key, res, 2) # Low TTL for real-time sync
        return res

    def get_prices_bulk(self, symbols: list[str], exchange: str = "NSE") -> Dict[str, float]:
        """Fetch multiple prices in one call via Angel One Bulk API."""
        results = {}
        if not symbols: return results

        # 1. Check Cache First
        remaining_symbols = []
        for s in symbols:
            ck = f"orchestrator_price_{s}_{exchange}"
            cached = cache.get(ck)
            if cached and cached.get("ltp", 0) > 0:
                results[s] = cached["ltp"]
            else:
                remaining_symbols.append(s)
        
        if not remaining_symbols:
            return results

        # 2. Resolve Tokens & Fetch Bulk from Angel One
        try:
            svc = get_truedata_instance()
            if svc:
                token_map = svc.get_token_map(remaining_symbols, exchange)
                if token_map:
                    # Angel One token_map is {symbol: token}
                    # get_bulk_quotes needs {exchange: [tokens]}
                    bulk_arg = {exchange: list(token_map.values())}
                    bulk_res = svc.get_bulk_quotes(bulk_arg)
                    
                    # Map back to symbols and cache
                    for s, token in token_map.items():
                        key = f"{exchange}:{token}"
                        if key in bulk_res:
                            quote = bulk_res[key]
                            ltp = quote.get("ltp", 0)
                            if ltp > 0:
                                results[s] = ltp
                                cache.set(f"orchestrator_price_{s}_{exchange}", {
                                    "symbol": s,
                                    "ltp": ltp,
                                    "high": quote.get("high", 0),
                                    "low": quote.get("low", 0),
                                    "provider": "angel_one_bulk",
                                    "status": "LIVE"
                                }, 5)

        except Exception as e:
            logger.warning("[ORCHESTRATOR] Bulk fetch error: %s", e)

        # 3. Fallback for any symbols still missing (single REST via get_price)
        for s in remaining_symbols:
            if s not in results:
                try:
                    res = self.get_price(s, exchange)
                    if res.get("ltp", 0) > 0:
                        results[s] = res["ltp"]
                except:
                    continue

        return results

    def get_price(self, symbol: str, exchange: str = "NSE") -> Dict[str, Any]:
        """Unified Spot Price Layer."""
        # Layer 1: Check Cache
        cache_key = f"orchestrator_price_{symbol}_{exchange}"
        cached_res = cache.get(cache_key)
        if cached_res:
            return cached_res

        # Layer 2: Angel One Primary
        try:
            svc = get_truedata_instance()
            if svc:
                if exchange == "NSE":
                    quote = svc.get_stock_quote(symbol)
                else:
                    quote = None

                if quote and quote.get("ltp", 0) > 0:
                    res = {
                        "symbol": symbol,
                        "ltp": quote["ltp"],
                        "high": quote.get("high", 0),
                        "low": quote.get("low", 0),
                        "provider": "angel_one",
                        "status": "LIVE",
                        "change_pct": quote.get("percentChange", 0)
                    }
                    cache.set(cache_key, res, 5)
                    return res
        except Exception as e:
            logger.warning("[ORCHESTRATOR] Angel One failed for %s: %s", symbol, e)

        return {
            "symbol": symbol,
            "ltp": 0,
            "provider": "NONE",
            "status": "ERROR",
            "change_pct": 0
        }

def get_orchestrator():
    return MarketDataOrchestrator()
