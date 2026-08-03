"""
Event blackout: F&O ban period and earnings-window exclusions.

See doc/INSTITUTIONAL_AUDIT_INTRADAY.md §1 and §3.1f. Both events inject jump risk the
strategy does not model:

  * F&O ban period — a stock crossing 95% of market-wide position limit is restricted to
    position-reduction only. Liquidity thins, spreads widen sharply, and price behaviour
    becomes driven by unwinding rather than by the volume-profile structure the signal
    logic assumes.
  * Earnings — a gap through the stop is not a 1R loss, it is an unbounded one. A stop
    is a price level, not a guarantee of fill, and a results announcement is the single
    most reliable way to discover that distinction.

Both feeds degrade gracefully: if NSE is unreachable the scan continues unfiltered
rather than failing, but the fallback is logged so a silent loss of protection is
visible.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta

import pandas as pd
import requests
from django.conf import settings
from django.core.cache import cache

from .signal_utils import IST

logger = logging.getLogger(__name__)

FO_BAN_CACHE_KEY = "nse_fo_ban_list"
EARNINGS_CACHE_KEY = "nse_earnings_calendar"
FO_BAN_TTL = 60 * 60 * 2
EARNINGS_TTL = 60 * 60 * 12

# Days either side of a results date to stay out. T-1 covers the pre-announcement drift
# and positioning; T+1 covers the post-gap repricing.
EARNINGS_BLACKOUT_DAYS = int(getattr(settings, "INTRADAY_EARNINGS_BLACKOUT_DAYS", 1))

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}


def get_fo_ban_list() -> set[str]:
    """Symbols currently in F&O ban period, from NSE's daily securities-ban CSV."""
    cached = cache.get(FO_BAN_CACHE_KEY)
    if cached is not None:
        return set(cached)

    url = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=12)
        resp.raise_for_status()
        text = resp.text.strip()

        banned: set[str] = set()
        for line in text.splitlines():
            token = line.split(",")[-1].strip().upper()
            # Skip the header/preamble lines NSE prefixes the file with.
            if token and token.isalnum() and "SECURIT" not in token and token != "NAME":
                banned.add(token)

        cache.set(FO_BAN_CACHE_KEY, list(banned), timeout=FO_BAN_TTL)
        logger.info("[EVENTS] F&O ban list: %d symbol(s).", len(banned))
        return banned
    except Exception as exc:
        logger.warning("[EVENTS] F&O ban list unavailable (%s) — proceeding UNFILTERED.", exc)
        cache.set(FO_BAN_CACHE_KEY, [], timeout=300)
        return set()


def get_earnings_calendar() -> dict[str, str]:
    """{symbol: ISO date} of upcoming results, from NSE's event calendar."""
    cached = cache.get(EARNINGS_CACHE_KEY)
    if cached is not None:
        return cached

    calendar: dict[str, str] = {}
    try:
        session = requests.Session()
        session.headers.update(_HEADERS)
        # NSE requires a cookie bootstrap before its JSON endpoints will answer.
        session.get("https://www.nseindia.com", timeout=10)
        resp = session.get("https://www.nseindia.com/api/event-calendar", timeout=12)
        resp.raise_for_status()

        for row in resp.json():
            sym = str(row.get("symbol", "")).strip().upper()
            purpose = str(row.get("purpose", "")).lower()
            date_str = str(row.get("date", "")).strip()
            if not sym or not date_str:
                continue
            if "result" not in purpose and "earning" not in purpose:
                continue
            for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    calendar[sym] = datetime.strptime(date_str, fmt).date().isoformat()
                    break
                except ValueError:
                    continue

        cache.set(EARNINGS_CACHE_KEY, calendar, timeout=EARNINGS_TTL)
        logger.info("[EVENTS] Earnings calendar: %d symbol(s).", len(calendar))
    except Exception as exc:
        logger.warning("[EVENTS] Earnings calendar unavailable (%s) — proceeding UNFILTERED.", exc)
        cache.set(EARNINGS_CACHE_KEY, {}, timeout=600)

    return calendar


def get_blacklist() -> dict[str, str]:
    """{symbol: reason} for every symbol that must not be traded today."""
    today = datetime.now(tz=IST).date()
    blocked: dict[str, str] = {sym: "FO_BAN" for sym in get_fo_ban_list()}

    for sym, iso in get_earnings_calendar().items():
        try:
            d = datetime.fromisoformat(iso).date()
        except (ValueError, TypeError):
            continue
        if abs((d - today).days) <= EARNINGS_BLACKOUT_DAYS:
            blocked.setdefault(sym, f"EARNINGS:{iso}")

    return blocked


def filter_symbols(symbols: list[str]) -> tuple[list[str], dict[str, str]]:
    """Drop blacklisted symbols. Returns (allowed, {excluded: reason})."""
    blacklist = get_blacklist()
    if not blacklist:
        return symbols, {}

    allowed, excluded = [], {}
    for s in symbols:
        reason = blacklist.get(s.upper())
        if reason:
            excluded[s] = reason
        else:
            allowed.append(s)

    if excluded:
        logger.info("[EVENTS] Excluded %d symbol(s): %s",
                    len(excluded), ", ".join(f"{k}({v})" for k, v in list(excluded.items())[:8]))
    return allowed, excluded
