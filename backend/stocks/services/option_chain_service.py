"""
option_chain_service.py

Fetches NSE option chain data for NIFTY / BANKNIFTY (or any index symbol).
Uses a requests.Session with the proper NSE browser headers and cookie
bootstrapping (first hit the homepage, then the API endpoint).

Returns a structured dict with:
  - expiry_dates
  - strikes with CE/PE OI, LTP, change-in-OI
  - PCR (Put-Call Ratio)
  - max_pain strike
  - top 5 CE buildup strikes
  - top 5 PE buildup strikes
"""

from __future__ import annotations

import logging
import random
from typing import Any
from django.utils import timezone
import requests

logger = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com"
NSE_OPTION_CHAIN_URL = f"{NSE_BASE}/api/option-chain-indices"

import time

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

REQUEST_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

def _build_session(max_retries: int = 3) -> requests.Session:
    """Return a Session that has visited the NSE homepage and option chain page to acquire necessary cookies."""
    session = requests.Session()
    
    for attempt in range(max_retries):
        try:
            headers = NSE_HEADERS.copy()
            headers["User-Agent"] = random.choice(USER_AGENTS)
            session.headers.update(headers)
            
            # 1. Bootstrap on homepage
            session.get(NSE_BASE, timeout=REQUEST_TIMEOUT)
            time.sleep(1)
            
            # 2. Bootstrap explicitly on the option-chain frontend page to get specific routing cookies
            session.headers.update({"Referer": NSE_BASE + "/"})
            session.get(f"{NSE_BASE}/option-chain", timeout=REQUEST_TIMEOUT)
            
            # If we succeed without throwing, break out of retry loop
            return session
        except Exception as exc:
            logger.warning("NSE session bootstrap attempt %d failed: %s", attempt + 1, exc)
            time.sleep(2)
            
    return session

# ---------------------------------------------------------------------------
# Mock / fallback data  (realistic NIFTY option chain)
# ---------------------------------------------------------------------------

def _generate_mock_chain(symbol: str = "NIFTY") -> dict[str, Any]:
    """Return realistic mock option chain data for NIFTY/BANKNIFTY."""
    if symbol == "BANKNIFTY":
        spot = 52450.0
        strikes_base = list(range(51000, 54000, 100))
    else:
        spot = 23300.0
        strikes_base = list(range(22500, 24100, 50))

    chain = []
    rng = random.Random(42)  # deterministic mock
    for s in strikes_base:
        diff = abs(s - spot)
        ce_oi_base = max(500000, int(2000000 * (1 - diff / 2000))) if s >= spot else int(800000 * (1 - diff / 3000))
        pe_oi_base = int(800000 * (1 - diff / 3000)) if s >= spot else max(500000, int(2000000 * (1 - diff / 2000)))
        ce_oi = max(0, ce_oi_base + rng.randint(-200000, 200000))
        pe_oi = max(0, pe_oi_base + rng.randint(-200000, 200000))
        ce_change = rng.randint(-50000, 80000)
        pe_change = rng.randint(-50000, 80000)
        ce_ltp = max(0.5, round((max(0, spot - s) + rng.uniform(5, 50)) if s < spot else rng.uniform(1, max(2, 200 - diff / 10)), 2))
        pe_ltp = max(0.5, round((max(0, s - spot) + rng.uniform(5, 50)) if s > spot else rng.uniform(1, max(2, 200 - diff / 10)), 2))

        chain.append({
            "strike": s,
            "ce_oi": ce_oi,
            "ce_oi_change": ce_change,
            "ce_ltp": ce_ltp,
            "pe_oi": pe_oi,
            "pe_oi_change": pe_change,
            "pe_ltp": pe_ltp,
        })

    total_ce = sum(r["ce_oi"] for r in chain)
    total_pe = sum(r["pe_oi"] for r in chain)
    pcr = round(total_pe / total_ce, 4) if total_ce > 0 else 0.0

    # Max pain
    max_pain_strike = None
    min_pain = None
    for k_row in chain:
        k = k_row["strike"]
        pain = sum(r["ce_oi"] * max(r["strike"] - k, 0) + r["pe_oi"] * max(k - r["strike"], 0) for r in chain)
        if min_pain is None or pain < min_pain:
            min_pain = pain
            max_pain_strike = k

    return {
        "symbol": symbol,
        "spot_price": spot,
        "expiry": "27-Mar-2026",
        "expiry_dates": ["27-Mar-2026", "03-Apr-2026", "10-Apr-2026"],
        "chain": chain,
        "pcr": pcr,
        "max_pain": max_pain_strike,
        "total_ce_oi": total_ce,
        "total_pe_oi": total_pe,
        "ce_buildup": sorted(
            [r for r in chain if r["ce_oi_change"] > 0],
            key=lambda x: x["ce_oi_change"], reverse=True
        )[:5],
        "pe_buildup": sorted(
            [r for r in chain if r["pe_oi_change"] > 0],
            key=lambda x: x["pe_oi_change"], reverse=True
        )[:5],
    }


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _extract_records(raw_data: list[dict]) -> list[dict[str, Any]]:
    """Flatten NSE option-chain rows into a list of strike-level dicts."""
    records: list[dict[str, Any]] = []
    for row in raw_data:
        strike = row.get("strikePrice")
        ce = row.get("CE", {})
        pe = row.get("PE", {})
        records.append(
            {
                "strike": strike,
                "ce_oi": ce.get("openInterest", 0),
                "ce_oi_change": ce.get("changeinOpenInterest", 0),
                "ce_ltp": ce.get("lastPrice", 0),
                "ce_volume": ce.get("totalTradedVolume", 0),
                "pe_oi": pe.get("openInterest", 0),
                "pe_oi_change": pe.get("changeinOpenInterest", 0),
                "pe_ltp": pe.get("lastPrice", 0),
                "pe_volume": pe.get("totalTradedVolume", 0),
            }
        )
    return records


def _compute_pcr(records: list[dict]) -> float:
    """Put-Call Ratio = total PE OI / total CE OI."""
    total_pe = sum(r["pe_oi"] for r in records)
    total_ce = sum(r["ce_oi"] for r in records)
    if total_ce == 0:
        return 0.0
    return round(total_pe / total_ce, 4)


def _compute_max_pain(records: list[dict]) -> float | None:
    """
    Max Pain = strike at which total option writers' pain is minimised.
    """
    strikes = [r["strike"] for r in records if r["strike"] is not None]
    if not strikes:
        return None

    min_pain: float | None = None
    max_pain_strike: float | None = None

    for k in strikes:
        pain = 0.0
        for r in records:
            s = r["strike"]
            if s is None:
                continue
            pain += r["ce_oi"] * max(s - k, 0)
            pain += r["pe_oi"] * max(k - s, 0)
        if min_pain is None or pain < min_pain:
            min_pain = pain
            max_pain_strike = k

    return max_pain_strike


def _top_buildup(records: list[dict], side: str, n: int = 5) -> list[dict]:
    """Top-n strikes with highest positive change in OI."""
    key_change = f"{side}_oi_change"
    key_oi = f"{side}_oi"
    key_ltp = f"{side}_ltp"

    sorted_records = sorted(
        [r for r in records if r[key_change] > 0],
        key=lambda x: x[key_change],
        reverse=True,
    )[:n]

    return [
        {
            "strike": r["strike"],
            "oi_change": r[key_change],
            "oi": r[key_oi],
            "ltp": r[key_ltp],
        }
        for r in sorted_records
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _save_option_chain_snapshot(data: dict[str, Any]):
    """Save a snapshot of the option chain to the database for fallback use."""
    try:
        from stocks.models import OptionChain
        
        # Don't save if it looks like mock data (symbol check is enough for now)
        if data.get("expiry") == "27-Mar-2026":
            return

        OptionChain.objects.create(
            symbol=data["symbol"],
            spot_price=data.get("spot_price") or 0,
            pcr=data.get("pcr") or 0,
            max_pain=data.get("max_pain") or 0,
            total_ce_oi=data.get("total_ce_oi") or 0,
            total_pe_oi=data.get("total_pe_oi") or 0,
            chain_data_json=data.get("chain") or []
        )
        
        # Keep only the last 50 snapshots per symbol to protect Render DB storage
        old_snapshots = OptionChain.objects.filter(symbol=data["symbol"]).order_by('-timestamp')[50:]
        if old_snapshots.exists():
            OptionChain.objects.filter(id__in=old_snapshots.values_list('id', flat=True)).delete()
    except Exception as e:
        logger.error("Failed to save option chain snapshot: %s", e)

def get_option_chain(symbol: str = "NIFTY") -> dict[str, Any]:
    """
    Fetch and parse the NSE option chain for *symbol*.
    Falls back to realistic mock data if NSE API fails.
    """
    session = _build_session()
    result = None
    
    try:
        resp = session.get(
            NSE_OPTION_CHAIN_URL,
            params={"symbol": symbol},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        
        filtered = payload.get("filtered", {})
        records_raw = filtered.get("data", [])
        expiry_dates = payload.get("records", {}).get("expiryDates", [])
        underlying_value = payload.get("records", {}).get("underlyingValue", None)

        records = _extract_records(records_raw)

        if not records:
            logger.warning("No option chain records for %s — using mock.", symbol)
            result = _generate_mock_chain(symbol)
        else:
            pcr = _compute_pcr(records)
            max_pain = _compute_max_pain(records)
            ce_buildup = _top_buildup(records, "ce")
            pe_buildup = _top_buildup(records, "pe")

            total_ce_oi = sum(r["ce_oi"] for r in records)
            total_pe_oi = sum(r["pe_oi"] for r in records)

            result = {
                "symbol": symbol,
                "spot_price": underlying_value,
                "expiry": expiry_dates[0] if expiry_dates else None,
                "expiry_dates": expiry_dates,
                "chain": records,
                "pcr": pcr,
                "max_pain": max_pain,
                "total_ce_oi": total_ce_oi,
                "total_pe_oi": total_pe_oi,
                "ce_buildup": ce_buildup,
                "pe_buildup": pe_buildup,
            }
            
            # Save valid real data to database
            _save_option_chain_snapshot(result)

    except Exception as exc:
        logger.error("NSE option-chain request or parse failed for %s: %s", symbol, exc)
        result = _generate_mock_chain(symbol)
        
    return result
