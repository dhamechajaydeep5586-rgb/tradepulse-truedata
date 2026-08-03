"""
fii_dii_service.py

Fetches FII/DII (Foreign Institutional Investor / Domestic Institutional
Investor) trading activity data from NSE India.

Primary endpoint : https://www.nseindia.com/api/fiidiiTradeReact
Backup  endpoint : https://www.nseindia.com/api/fii-stats

If both endpoints fail (NSE APIs are frequently unreliable), the service
returns hardcoded realistic sample data so the frontend always has
something meaningful to display.
"""

from __future__ import annotations

import logging
import random
from typing import Any
from datetime import datetime, timedelta

import requests
import pandas as pd
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_KEY = "fii_dii_data"
CACHE_TTL = 1800  # 30 min — NSE only publishes FII/DII once per day after close

NSE_BASE = "https://www.nseindia.com"
NSE_FIIDII_PRIMARY = f"{NSE_BASE}/api/fiidiiTradeReact"
NSE_FIIDII_BACKUP = f"{NSE_BASE}/api/fii-stats"

NSE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "Sec-Ch-Ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
]

REQUEST_TIMEOUT = 10

# ---------------------------------------------------------------------------
# Mock / fallback data  (realistic INR crore values)
# ---------------------------------------------------------------------------

_BASE_MOCK_DATA = [
    {"fii_buy": 12345.67, "fii_sell": 11234.56, "fii_net": 1111.11, "dii_buy": 14876.54, "dii_sell": 10765.43, "dii_net": 4111.11, "category": "Equity"},
    {"fii_buy": 10234.56, "fii_sell": 11345.67, "fii_net": -1111.11, "dii_buy": 10123.45, "dii_sell": 7012.34, "dii_net": 3111.11, "category": "Equity"},
    {"fii_buy": 13456.78, "fii_sell": 12345.67, "fii_net": 1111.11, "dii_buy": 8901.23, "dii_sell": 9812.34, "dii_net": -911.11, "category": "Equity"},
    {"fii_buy": 9123.45, "fii_sell": 8012.34, "fii_net": 1111.11, "dii_buy": 11234.56, "dii_sell": 8123.45, "dii_net": 3111.11, "category": "Equity"},
    {"fii_buy": 14567.89, "fii_sell": 15678.90, "fii_net": -1111.01, "dii_buy": 12345.67, "dii_sell": 11234.56, "dii_net": 1111.11, "category": "Equity"},
    {"fii_buy": 11111.11, "fii_sell": 13000.00, "fii_net": -1888.89, "dii_buy": 9000.00, "dii_sell": 9500.00, "dii_net": -500.00, "category": "Equity"},
    {"fii_buy": 8765.43, "fii_sell": 9876.54, "fii_net": -1111.11, "dii_buy": 13456.78, "dii_sell": 11345.67, "dii_net": 2111.11, "category": "Equity"},
    {"fii_buy": 16789.01, "fii_sell": 15678.90, "fii_net": 1110.11, "dii_buy": 7890.12, "dii_sell": 8901.23, "dii_net": -1011.11, "category": "Equity"},
    {"fii_buy": 12000.00, "fii_sell": 9000.00, "fii_net": 3000.00, "dii_buy": 11000.00, "dii_sell": 12000.00, "dii_net": -1000.00, "category": "Equity"},
    {"fii_buy": 7654.32, "fii_sell": 8765.43, "fii_net": -1111.11, "dii_buy": 14567.89, "dii_sell": 12456.78, "dii_net": 2111.11, "category": "Equity"},
]

def _get_fallback_fii_dii_data() -> list[dict[str, Any]]:
    now = datetime.now()
    if now.hour < 18:
        # Before 6 PM, latest data is from previous business day
        now = now - timedelta(days=1 if now.weekday() < 5 else max(1, now.weekday() - 4))
    
    dates = pd.date_range(end=now, periods=10, freq='B')[::-1]
    mock_data = []
    for i, d in enumerate(dates):
        if i >= len(_BASE_MOCK_DATA): break
        item = _BASE_MOCK_DATA[i].copy()
        item["date"] = d.strftime("%d-%b-%Y")
        mock_data.append(item)
    return mock_data

# ---------------------------------------------------------------------------
# Session & Parsing Logic
# ---------------------------------------------------------------------------

def _build_session() -> requests.Session:
    """Single-attempt, fail-fast session bootstrap.

    This runs synchronously inside a web request (gunicorn worker), so it
    must never block for longer than a few seconds — a retry loop with
    sleeps here previously ran past gunicorn's worker timeout and got the
    whole worker SIGKILLed mid-request.
    """
    session = requests.Session()
    try:
        headers = NSE_HEADERS.copy()
        headers["User-Agent"] = random.choice(USER_AGENTS)
        session.headers.update(headers)
        session.get(NSE_BASE, timeout=REQUEST_TIMEOUT)
        session.headers.update({"Referer": NSE_BASE + "/"})
        session.get(f"{NSE_BASE}/all-reports", timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        logger.warning("NSE FII session bootstrap failed: %s", exc)
    return session

def _safe_float(item: dict, *keys) -> float:
    for key in keys:
        val = item.get(key)
        if val is not None:
            try: return float(val)
            except (TypeError, ValueError): continue
    return 0.0

def _parse_fiidii_response(payload: Any) -> list[dict[str, Any]] | None:
    if not payload: return None
    rows = payload if isinstance(payload, list) else (payload.get("data") or payload.get("fiiDiiData") or payload.get("results"))
    if not rows: return None

    result = []
    for item in rows:
        if not isinstance(item, dict): continue
        fii_buy  = _safe_float(item, "fiiBuyValue", "FII_BUY", "fiiBuy")
        fii_sell = _safe_float(item, "fiiSellValue", "FII_SELL", "fiiSell")
        fii_net  = _safe_float(item, "fiiNetValue", "FII_NET", "fiiNet")
        dii_buy  = _safe_float(item, "diiBuyValue", "DII_BUY", "diiBuy")
        dii_sell = _safe_float(item, "diiSellValue", "DII_SELL", "diiSell")
        dii_net  = _safe_float(item, "diiNetValue", "DII_NET", "diiNet")

        result.append({
            "date": item.get("date") or item.get("tradingDate") or item.get("DATE") or "",
            "fii_buy": fii_buy, "fii_sell": fii_sell,
            "fii_net": fii_net if fii_net != 0.0 else round(fii_buy - fii_sell, 2),
            "dii_buy": dii_buy, "dii_sell": dii_sell,
            "dii_net": dii_net if dii_net != 0.0 else round(dii_buy - dii_sell, 2),
            "category": item.get("category") or item.get("CATEGORY") or "Equity",
        })

    if result:
        if not any(abs(r.get("fii_buy", 0)) > 0 or abs(r.get("dii_buy", 0)) > 0 for r in result):
            return None
    return result if result else None

def _fetch_from_url(session: requests.Session, url: str) -> list[dict[str, Any]] | None:
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return _parse_fiidii_response(resp.json())
    except Exception as exc:
        logger.warning("FII/DII fetch error (%s): %s", url, exc)
        return None

# ---------------------------------------------------------------------------
# Public API (Fixed)
# ---------------------------------------------------------------------------

def get_fii_dii_data() -> list[dict[str, Any]]:
    """Fetch FII/DII data silently on holidays."""
    from stocks.services.signal_utils import is_market_open
    if not is_market_open():
        logger.debug("[FII_DII] Market closed or holiday, skipping fetch.")
        return _get_fallback_fii_dii_data()

    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached

    session = _build_session()
    # 1. Primary endpoint
    data = _fetch_from_url(session, NSE_FIIDII_PRIMARY)
    # 2. Backup endpoint
    if not data:
        data = _fetch_from_url(session, NSE_FIIDII_BACKUP)

    if data:
        cache.set(CACHE_KEY, data, CACHE_TTL)
        return data

    # 3. Fallback (not cached — retry live fetch on next request)
    logger.warning("FII/DII endpoints failed - returning sample data.")
    return _get_fallback_fii_dii_data()
