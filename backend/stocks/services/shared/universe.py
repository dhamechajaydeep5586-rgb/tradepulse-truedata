"""
Liquidity- and cost-filtered trading universe.

See doc/INSTITUTIONAL_AUDIT_INTRADAY.md §1. Index membership is a proxy for market cap,
not for tradability. The variable that decides whether an intraday strategy is runnable
is spread relative to target: if the spread is 30bps and the target is 32bps, the
strategy is unrunnable at any signal quality. That is why NIFTY500 was rejected — its
tail carries 50-150bps effective spreads — and why the universe is gated on liquidity
rather than simply widened.

Widening NIFTY100 -> NIFTY200 raises selectivity at constant trade count: picking 5 from
~150 survivors is a deeper cut than picking 5 from 100, which is free edge provided the
ranking model has any signal at all.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta

import pandas as pd
import requests
from django.conf import settings
from django.core.cache import cache

from .. import market_data
from ..signal_utils import IST, load_cached_index_symbols

logger = logging.getLogger(__name__)

UNIVERSE_CACHE_KEY = "intraday_filtered_universe_v1"
SECTOR_MAP_CACHE_KEY = "nse_sector_map_v1"
UNIVERSE_TTL = 60 * 60 * 6      # 6h — constituent/liquidity drift is slow
SECTOR_TTL = 60 * 60 * 24

INDEX_CSV = {
    "NIFTY50": "ind_nifty50list.csv",
    "NIFTY100": "ind_nifty100list.csv",
    "NIFTY200": "ind_nifty200list.csv",
    "NIFTY500": "ind_nifty500list.csv",
}

# Gates. Deliberately conservative: every one of these removes trades that are
# arithmetically poor rather than merely unattractive.
MIN_ADV_INR = float(getattr(settings, "INTRADAY_MIN_ADV_INR", 50_00_00_000))   # ₹50 cr
MAX_SPREAD_BPS = float(getattr(settings, "INTRADAY_MAX_SPREAD_BPS", 5.0))
MIN_PRICE_INR = float(getattr(settings, "INTRADAY_MIN_PRICE", 100.0))
TARGET_INDEX = getattr(settings, "INTRADAY_UNIVERSE_INDEX", "NIFTY200")


def _fetch_index_csv(index_name: str) -> pd.DataFrame | None:
    csv_name = INDEX_CSV.get(index_name, INDEX_CSV["NIFTY200"])
    url = f"https://nsearchives.nseindia.com/content/indices/{csv_name}"
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Referer": "https://www.nseindia.com/",
        })
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        df.columns = df.columns.str.strip()
        return df
    except Exception as exc:
        logger.error("[UNIVERSE] NSE fetch failed for %s: %s", index_name, exc)
        return None


def get_sector_map(index_name: str = TARGET_INDEX) -> dict[str, str]:
    """{symbol: industry}. Sourced from the NSE constituent CSV's Industry column.

    Needed for the sector-concentration cap — without it, five "independent" signals can
    all be the same sector bet (NIFTY100 is ~35% financials).
    """
    cached = cache.get(SECTOR_MAP_CACHE_KEY)
    if cached:
        return cached

    df = _fetch_index_csv(index_name)
    if df is None or "Symbol" not in df.columns:
        return {}

    industry_col = next((c for c in df.columns if c.lower() == "industry"), None)
    mapping = {
        str(r["Symbol"]).strip(): (str(r[industry_col]).strip() if industry_col else "UNKNOWN")
        for _, r in df.iterrows()
        if str(r.get("Symbol", "")).strip()
    }
    if mapping:
        cache.set(SECTOR_MAP_CACHE_KEY, mapping, timeout=SECTOR_TTL)
    return mapping


def _spread_bps(svc, token_map: dict[str, str]) -> dict[str, float]:
    """Live bid-ask half... full spread, in bps of mid, from FULL-mode bulk quotes.

    Previously the spread gate was declared (`MAX_SPREAD_BPS`) but never fed any data,
    so `spread_bps is not None` was always false and it could never reject a candidate.
    This is what actually populates it. Batched in chunks of 50, matching every other
    bulk-quote call site in the codebase, rather than one request per symbol.

    Falls back to Angel One's own synthetic bid/ask (`ltp*0.998`/`ltp*1.002`, from
    `_parse_quote_item`) when live depth is thin — that fallback prices spread at exactly
    40bps, so a real 5bps/25bps gate still discriminates against genuinely illiquid names.
    """
    tokens = list(token_map.values())
    quotes: dict[str, dict] = {}
    for i in range(0, len(tokens), 50):
        try:
            quotes.update(svc.get_bulk_quotes({"NSE": tokens[i:i + 50]}, mode="FULL"))
        except Exception as exc:
            logger.debug("[UNIVERSE] Spread quote chunk failed: %s", exc)

    out: dict[str, float] = {}
    for sym, token in token_map.items():
        q = quotes.get(f"NSE:{token}")
        if not q:
            continue
        bid, ask = float(q.get("bid") or 0), float(q.get("ask") or 0)
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2.0
        if mid > 0:
            out[sym] = round((ask - bid) / mid * 10_000.0, 2)
    return out


def _liquidity_stats(svc, symbols: list[str]) -> dict[str, dict]:
    """20-day ADV (₹), daily vol %, and live spread (bps) per symbol.

    Candles are one call per symbol at ~1/sec, so this is the expensive part of the
    pipeline. Cached for 6 hours precisely because ADV/vol do not need to be fresh —
    liquidity is a slow-moving property. Spread is fetched fresh each call (it is not
    slow-moving) but the whole result, spread included, still lives under the same
    6h TTL — acceptable because the spread gate only needs to catch structurally wide
    names, not react to intraday spread widening.
    """
    stats: dict[str, dict] = {}
    token_map = svc.get_token_map(symbols, exchange="NSE") or {}

    for sym, token in token_map.items():
        try:
            # allow_fetch=False (added 2026-07-28): ADV/vol is a slow-moving property —
            # the docstring above already tolerates a 6h-stale result, so there is no
            # reason this loop needs to trigger its own REST fetch for up to 200 symbols
            # every time the universe cache expires. Read-only against whatever
            # candle_trickle_warmer has already backfilled into CandleBar; a symbol with
            # too little stored history just doesn't pass the 10-bar minimum below yet,
            # rather than this function adding its own REST burst on top of the
            # trickle warmer's already-throttled one. Confirmed 2026-07-28: this loop's
            # own fetch attempts were a repeat contributor to the WAF circuit breaker
            # tripping, on top of the trickle warmer's.
            df = market_data.request_candles(
                svc, sym, token, "ONE_DAY", lookback_days=40, exchange="NSE", allow_fetch=False
            )
            if df is None or df.empty or len(df) < 10:
                continue
            tail = df.tail(20)
            adv = float((tail["Close"] * tail["Volume"]).median())
            price = float(df["Close"].iloc[-1])
            rets = (df["Close"].pct_change().dropna()).tail(20)
            vol_pct = float(rets.std() * 100.0) if len(rets) > 2 else 1.5
            stats[sym] = {"adv_inr": adv, "price": price, "daily_vol_pct": vol_pct}
        except Exception:
            continue

    spreads = _spread_bps(svc, token_map)
    for sym, bps in spreads.items():
        if sym in stats:
            stats[sym]["spread_bps"] = bps
    return stats


def get_trading_universe(svc=None, force: bool = False, profile=None) -> dict:
    """Filtered universe plus the liquidity metadata downstream stages need.

    Returns {"symbols": [...], "stats": {sym: {...}}, "sectors": {sym: industry}}.

    `profile` selects index, liquidity thresholds, cache key and TTL. Each engine gets
    its own cache entry — a swing scan must never be served intraday's Rs.50cr-ADV
    universe, which would silently drop most of the names a 15-90 day strategy can
    legitimately trade.
    """
    if profile is None:
        from .profiles import INTRADAY
        profile = INTRADAY

    index_name = profile.index
    cache_key = f"{UNIVERSE_CACHE_KEY}_{profile.name}"

    if not force:
        cached = cache.get(cache_key)
        if cached:
            return cached

    if svc is None:
        from ..truedata_service import get_truedata_instance
        svc = get_truedata_instance()

    df = _fetch_index_csv(index_name)
    if df is not None and "Symbol" in df.columns:
        base = [str(s).strip() for s in df["Symbol"].tolist() if str(s).strip()]
    else:
        base = load_cached_index_symbols(index_name) or load_cached_index_symbols("NIFTY100")
        logger.warning("[UNIVERSE] Falling back to cached constituents (%d).", len(base))

    sectors = get_sector_map(index_name)

    if svc is None:
        # No broker: return unfiltered so the engine still runs, but say so loudly —
        # an unfiltered universe silently reintroduces untradeable names.
        logger.warning("[UNIVERSE] No broker service; skipping liquidity filter.")
        return {"symbols": base, "stats": {}, "sectors": sectors, "filtered": False}

    stats = _liquidity_stats(svc, base)

    survivors, rejected = [], {"price": 0, "adv": 0, "spread": 0, "no_stats": 0}
    for sym in base:
        st = stats.get(sym)
        if not st:
            rejected["no_stats"] += 1
            continue
        if st["price"] < profile.min_price_inr:
            rejected["price"] += 1   # tick quantisation: Rs.0.05 on a Rs.60 stock is 8bps
            continue
        if st["adv_inr"] < profile.min_adv_inr:
            rejected["adv"] += 1
            continue
        # Spread gate. MAX_SPREAD_BPS was declared but never applied — a latent hole,
        # since a name whose spread exceeds the target is unrunnable regardless of
        # signal quality. Only enforced when the stat is actually available, so a
        # missing quote does not silently empty the universe.
        spread_bps = st.get("spread_bps")
        if spread_bps is not None and spread_bps > profile.max_spread_bps:
            rejected["spread"] += 1
            continue
        survivors.append(sym)

    if not survivors:
        logger.warning("[UNIVERSE] Liquidity filter removed everything; using base list.")
        survivors = base

    result = {
        "symbols": survivors,
        "stats": stats,
        "sectors": sectors,
        "filtered": True,
        "base_count": len(base),
        "rejected": rejected,
        "profile": profile.name,
    }

    # A rate-limit burst mid-_liquidity_stats() leaves most symbols with no candle
    # data at all (no_stats), not filtered out on merit. Caching that degraded result
    # for the full multi-hour TTL would strand the scan on ~1 symbol until the TTL
    # expires, long after Angel One's 5-min circuit breaker has cleared. Use a short
    # retry TTL instead so the next scan re-attempts once REST access is back.
    no_stats_ratio = rejected["no_stats"] / len(base) if base else 0.0
    degraded = no_stats_ratio > 0.5
    ttl = 300 if degraded else profile.universe_ttl_secs
    cache.set(cache_key, result, timeout=ttl)
    if degraded:
        logger.warning(
            "[UNIVERSE][%s] Degraded result (%.0f%% no_stats, likely mid-burst rate limit) "
            "— caching for %ds instead of %ds so the next scan retries.",
            profile.name, no_stats_ratio * 100, ttl, profile.universe_ttl_secs,
        )
    logger.info(
        "[UNIVERSE][%s] %s: %d -> %d (ADV>=Rs.%.0fcr, price>=Rs.%.0f, spread<=%.0fbps) "
        "rejected: %s",
        profile.name, index_name, len(base), len(survivors),
        profile.min_adv_inr / 1e7, profile.min_price_inr, profile.max_spread_bps, rejected,
    )
    return result
