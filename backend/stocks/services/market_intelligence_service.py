import logging
import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta
from django.core.cache import cache
from .signal_utils import (
    IST, compute_vwap, compute_session_vwap, compute_atr, compute_volume_profile,
)

logger = logging.getLogger(__name__)

MARKET_STATE_CACHE_KEY = "unified_market_state_1m"

def is_sideways_market(df: pd.DataFrame, vwap_threshold: float = 0.0015, range_threshold: float = 0.01) -> dict:
    """
    JD Sideways Detection Logic (Institutional Standard):
    1. Distance from VWAP: Price must be within 0.15% of VWAP.
    2. Range Contraction: Day's high-low range must be within 1%.
    3. Returns: {'is_sideways': bool, 'market_state': str, 'confidence': float}
    """
    if df.empty or len(df) < 20:
        return {"is_sideways": False, "market_state": "UNKNOWN", "confidence": 65}

    current_price = float(df["Close"].iloc[-1])
    vwap = compute_vwap(df).iloc[-1]
    
    # 1. VWAP Distance
    vwap_dist = abs(current_price - vwap) / vwap
    is_near_vwap = vwap_dist <= vwap_threshold
    
    # 2. Daily Range Check
    today = df.index.max().date()
    today_df = df[df.index.date == today]
    if today_df.empty:
        # Fallback to last 20 candles if today is empty (market just opened)
        day_high = df["High"].iloc[-20:].max()
        day_low = df["Low"].iloc[-20:].min()
    else:
        day_high = today_df["High"].max()
        day_low = today_df["Low"].min()
        
    day_range = (day_high - day_low) / day_low
    
    # 3. Simple logic: If day range < threshold AND near VWAP -> Sideways
    is_sideways = is_near_vwap and (day_range <= range_threshold)
    
    state = "SIDEWAYS" if is_sideways else "TRENDING"
    
    # --- Graduated Confidence Scoring ---
    # We want a smoother drop-off rather than 0% for being 0.01% away from threshold
    vwap_score = max(0, 100 * (1 - vwap_dist / (vwap_threshold * 2))) # 100% at VWAP, 50% at threshold
    range_score = max(0, 100 * (1 - day_range / (range_threshold * 1.5)))
    
    confidence = (vwap_score * 0.7) + (range_score * 0.3)
    
    # No fixed floor — let the graduated score fully differentiate stocks.
    # A stock 0.01% from VWAP scores higher than one 0.14% from VWAP.
    # Previously all sideways stocks were floored at 85, making them indistinguishable.
    
    return {
        "is_sideways": is_sideways,
        "market_state": state,
        "confidence": round(max(5, min(98, confidence)), 2),
        "metrics": {
            "vwap_dist_pct": round(vwap_dist * 100, 4),
            "day_range_pct": round(day_range * 100, 4)
        }
    }

def is_trading_window_active() -> bool:
    """Check if time is between 9:30 AM and 3:00 PM IST (as requested)."""
    from django.utils import timezone as dj_timezone
    now_ist = dj_timezone.now().astimezone(IST).time()
    start = time(9, 30)
    end = time(15, 0)
    return start <= now_ist <= end

def _derive_directional_bias(df: pd.DataFrame, poc: float, is_sideways: bool) -> str:
    """Directional read on the index: BULLISH / BEARISH / SIDEWAYS.

    Separate from `market_state` (SIDEWAYS/TRENDING), which answers "is there a trend?"
    but not "which way?". Downstream mean-reversion gates need direction: a value-area
    low bounce is a materially worse bet while the index itself is selling off.

    Requires agreement between two independent references before committing to a
    direction — the session-anchored VWAP (execution benchmark) and the volume-profile
    POC (fair-value reference). Disagreement resolves to SIDEWAYS, so the gate only
    blocks trades when the read is unambiguous.
    """
    try:
        if is_sideways:
            return "SIDEWAYS"
        price = float(df["Close"].iloc[-1])
        vwap = float(compute_session_vwap(df).iloc[-1])
        if price > vwap and price > poc:
            return "BULLISH"
        if price < vwap and price < poc:
            return "BEARISH"
        return "SIDEWAYS"
    except Exception:
        return "SIDEWAYS"


def get_standard_market_state(svc=None) -> dict:
    """
    Unified Market State Service (Single Source of Truth).
    Fetches Nifty data and returns the standardized market sentiment.

    Returns both `market_state` (SIDEWAYS/TRENDING — trend presence) and
    `directional_bias` (BULLISH/BEARISH/SIDEWAYS — trend direction). Consumers should
    read `directional_bias` for directional gating; `market_state` is kept unchanged
    because delta_hedge_service depends on its existing value set.
    """
    cached = cache.get(MARKET_STATE_CACHE_KEY)
    if cached:
        return cached

    if not svc:
        from .truedata_service import get_truedata_instance
        svc = get_truedata_instance()

    if not svc:
        return {"is_sideways": True, "market_state": "SIDEWAYS", "confidence": 50,
                "directional_bias": "SIDEWAYS", "error": "no_service"}

    try:
        now_ist = datetime.now(tz=IST)
        to_date = now_ist.strftime("%Y-%m-%d %H:%M")
        from_date = (now_ist - timedelta(days=2)).strftime("%Y-%m-%d 09:15")

        # NIFTY 50 Index — TrueData symbol name, see truedata_service.py module docstring
        nifty_df = svc.get_candle_data("NIFTY 50", "NSE", "FIVE_MINUTE", from_date, to_date)
        if nifty_df.empty:
            return {"is_sideways": True, "market_state": "SIDEWAYS", "confidence": 50,
                    "directional_bias": "SIDEWAYS"}

        intel = is_sideways_market(nifty_df)

        # --- Structural Analysis (Volume Profile) ---
        poc, vah, val = compute_volume_profile(nifty_df, bins=40)
        current_price = float(nifty_df["Close"].iloc[-1])
        is_within_va = val <= current_price <= vah

        intel.update({
            "poc": round(poc, 2),
            "vah": round(vah, 2),
            "val": round(val, 2),
            "is_within_va": is_within_va,
            "current_price": round(current_price, 2),
            "directional_bias": _derive_directional_bias(nifty_df, poc, intel["is_sideways"]),
        })

        cache.set(MARKET_STATE_CACHE_KEY, intel, timeout=60) # 1 minute cache
        return intel
    except Exception as e:
        logger.error(f"Error fetching unified market state: {e}")
        return {"is_sideways": True, "market_state": "SIDEWAYS", "confidence": 50,
                "directional_bias": "SIDEWAYS"}

def get_symbol_market_state(symbol: str, exchange: str = 'NSE', svc=None) -> dict:
    """
    Generalized Market State for specific symbols (Stocks/Indices).
    Used by Hedge Engine to check for range-bound setups.
    """
    cache_key = f"market_state_{symbol}_{exchange}"
    cached = cache.get(cache_key)
    if cached: return cached

    if not svc:
        from .truedata_service import get_truedata_instance
        svc = get_truedata_instance()
    
    if not svc: return {"is_within_va": True, "is_sideways": True}

    try:
        # TrueData is symbol-addressed — the symbol IS the token (see
        # truedata_service.py module docstring), no master lookup needed.
        token = symbol if exchange == 'NSE' else None

        if not token: return {"is_within_va": True, "confidence": 60}

        now_ist = datetime.now(tz=IST)
        to_date = now_ist.strftime("%Y-%m-%d %H:%M")
        from_date = (now_ist - timedelta(days=2)).strftime("%Y-%m-%d 09:15")
        
        df = svc.get_candle_data(token, exchange, "FIVE_MINUTE", from_date, to_date)
        if df.empty: return {"is_within_va": True, "confidence": 75.0}

        intel = is_sideways_market(df)
        poc, vah, val = compute_volume_profile(df, bins=40)
        current_price = float(df["Close"].iloc[-1])
        is_within_va = val <= current_price <= vah
        
        # Boost confidence if both sideways and within Value Area
        if is_within_va:
            intel["confidence"] = max(intel.get("confidence", 0), 82.0)
        
        intel.update({
            "is_within_va": is_within_va,
            "current_price": round(current_price, 2),
            "vah": round(vah, 2), "val": round(val, 2)
        })
        
        cache.set(cache_key, intel, timeout=300) # 5 min cache for stocks
        return intel
    except Exception as e:
        logger.error(f"Error getting state for {symbol}: {e}")
        return {"is_within_va": True, "confidence": 70.0}
