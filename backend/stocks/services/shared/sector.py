"""Sector strength and rotation, shared by all engines.

Extracted from `intraday_service._sector_strength`, which derived sector strength from
the scan's own candidate set. That approach is correct for intraday — one REST round trip
per sector index against a shared rate limiter is unaffordable inside a 5-minute cycle —
but it has a structural weakness: it can only rank sectors that happen to have candidates
today, and it measures the strength of *this scan's picks* rather than of the sector.

Swing and long-term run once a day or once a month, so they can afford the 11 index calls
and should use real sector indices instead. Both implementations live here so the
trade-off is visible in one place rather than being an accident of which engine you read.
"""
from __future__ import annotations

import logging

from django.core.cache import cache

from .. import market_data

logger = logging.getLogger(__name__)

SECTOR_INDEX_CACHE_KEY = "shared_sector_index_strength_v1"

# TrueData addresses every symbol (including indices) by its plain NSE name — there is
# no separate numeric token to resolve first (see truedata_service.py's module docstring:
# "token" is the TrueData symbol string everywhere in this codebase). "NIFTY BANK" and
# "NIFTY 50" are confirmed directly in TrueData's own docs (Market Data API v2.6 touchline
# sample; getIndexComponents' indexName option list). The rest follow the same "NIFTY
# <SECTOR>" convention but were not individually confirmed against a live symbol-master
# call — verify with `getAllSymbols?segment=in` before relying on one that returns no data,
# the same silent-wrong-instrument failure mode the old Angel One token map had.
SECTOR_INDEX_TOKENS = {
    "NIFTY BANK": "NIFTY BANK",
    "NIFTY IT": "NIFTY IT",
    "NIFTY FMCG": "NIFTY FMCG",
    "NIFTY AUTO": "NIFTY AUTO",
    "NIFTY PHARMA": "NIFTY PHARMA",
    "NIFTY METAL": "NIFTY METAL",
    "NIFTY REALTY": "NIFTY REALTY",
    "NIFTY ENERGY": "NIFTY ENERGY",
    "NIFTY INFRA": "NIFTY INFRA",
    "NIFTY MEDIA": "NIFTY MEDIA",
    "NIFTY PSU BANK": "NIFTY PSU BANK",
}

# Maps the `Industry` column of the NSE constituent CSV onto a sector index.
# Anything unmapped falls through to the candidate-derived method.
INDUSTRY_TO_INDEX = {
    "Financial Services": "NIFTY BANK",
    "Information Technology": "NIFTY IT",
    "Fast Moving Consumer Goods": "NIFTY FMCG",
    "Automobile and Auto Components": "NIFTY AUTO",
    "Healthcare": "NIFTY PHARMA",
    "Metals & Mining": "NIFTY METAL",
    "Realty": "NIFTY REALTY",
    "Oil Gas & Consumable Fuels": "NIFTY ENERGY",
    "Power": "NIFTY ENERGY",
    "Construction": "NIFTY INFRA",
    "Media Entertainment & Publication": "NIFTY MEDIA",
}


def _normalise(values: dict[str, float]) -> dict[str, float]:
    """Min-max to 0..1. Flat input maps to 0.5 rather than dividing by zero."""
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi - lo < 1e-9:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def sector_strength_from_candidates(candidates: list[dict]) -> dict[str, float]:
    """Rank sectors 0..1 by the mean relative strength of their candidates.

    Zero extra API calls — this is why intraday uses it. Only ranks sectors present in
    the candidate set, so a sector with no candidates today is simply absent (callers
    should default a missing sector to 0.5, not to 0.0).
    """
    by_sector: dict[str, list[float]] = {}
    for c in candidates:
        by_sector.setdefault(c.get("sector", "UNKNOWN"), []).append(
            c.get("relative_strength", 0.0)
        )
    if not by_sector:
        return {}
    return _normalise({k: sum(v) / len(v) for k, v in by_sector.items()})


def sector_strength_from_indices(
    svc,
    lookback_days: int = 60,
    ttl_secs: int = 12 * 3600,
) -> dict[str, float]:
    """Rank sector INDICES 0..1 by trailing return. 11 candle calls, cached.

    Measures the sector itself rather than the scan's picks, and covers every sector
    whether or not it produced a candidate today. Affordable only at daily-or-slower
    cadence — see the module docstring.
    """
    cached = cache.get(SECTOR_INDEX_CACHE_KEY)
    if cached:
        return cached

    if svc is None:
        return {}

    returns: dict[str, float] = {}
    for name, token in SECTOR_INDEX_TOKENS.items():
        try:
            # +20 buffer preserved from the direct-call version (weekend/holiday slack).
            # `name` (e.g. "NIFTY_BANK") doubles as the candle_store symbol — stable and
            # unique per sector index, same convention as regime.py's index pseudo-symbols.
            df = market_data.request_candles(
                svc, name, token, "ONE_DAY", lookback_days=lookback_days + 20, exchange="NSE"
            )
            if df is None or df.empty or len(df) < 20:
                continue
            window = df.tail(min(lookback_days, len(df)))
            first, last = float(window["Close"].iloc[0]), float(window["Close"].iloc[-1])
            if first > 0:
                returns[name] = (last / first - 1.0) * 100.0
        except Exception as exc:
            logger.debug("[SECTOR] index fetch failed for %s: %s", name, exc)

    if not returns:
        logger.warning("[SECTOR] No sector index data available; callers will fall back.")
        return {}

    scores = _normalise(returns)
    cache.set(SECTOR_INDEX_CACHE_KEY, scores, timeout=ttl_secs)
    logger.info(
        "[SECTOR] %d sector indices ranked over %dd. Strongest=%s weakest=%s",
        len(scores), lookback_days,
        max(returns, key=returns.get), min(returns, key=returns.get),
    )
    return scores


def strength_for_symbol(
    symbol: str,
    sector_map: dict[str, str],
    index_scores: dict[str, float],
    candidate_scores: dict[str, float],
) -> float:
    """Resolve one symbol's sector strength, preferring index-derived over
    candidate-derived, and defaulting to neutral 0.5 rather than 0.0.

    Defaulting to 0.0 would silently penalise every stock whose sector could not be
    resolved, which is a data-coverage artefact masquerading as a signal.
    """
    industry = sector_map.get(symbol, "UNKNOWN")
    idx_name = INDUSTRY_TO_INDEX.get(industry)
    if idx_name and idx_name in index_scores:
        return index_scores[idx_name]
    if industry in candidate_scores:
        return candidate_scores[industry]
    return 0.5
