"""Earnings and corporate-action ingestion.

Both `EarningsEvent` and `CorporateAction` (see models.py) were added by the V2 build but
left empty — this module is what actually populates them. `event_filter_service.py`
already fetches the live earnings calendar for the blackout filter, but only into a
12h cache; nothing gave it durability or history. This module persists both feeds.

── WHAT THIS DOES NOT DO ──────────────────────────────────────────────────────────
It does not adjust any price series. `doc/LONG_TERM_ENGINE_V2_ARCHITECTURE.md` §0
explicitly flags that whether the broker's candle series already returns
split/bonus-adjusted data must be VERIFIED before building an adjuster — building one
on top of already-adjusted data double-adjusts and is worse than doing nothing. TrueData's
own docs claim data is "fully adjusted for corporate actions (splits, bonuses, dividends,
rights)" — `verify_truedata_adjustment()` below is the check that confirms that claim
against real data rather than trusting it blind.

Splits and bonuses are ingested and classified because that classification is needed
regardless of the answer. Dividends and other cash actions never require a price
adjustment and are recorded for completeness only.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import requests
from django.core.cache import cache
from django.utils import timezone as dj_timezone

from ..signal_utils import IST

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

_NSE_ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities"

_SYNC_LOCK_KEY = "calendar_sync_running"

# Ratio patterns for stock splits: "FROM Rs.X TO Rs.Y", i.e. a face-value subdivision.
# The price adjustment factor is old_face_value / new_face_value.
_SPLIT_RE = re.compile(
    r"SUB[- ]?DIVISION.*?RS\.?\s*([\d.]+).*?RS\.?\s*([\d.]+)", re.IGNORECASE
)
# Bonus ratio "X:Y" meaning Y held shares become X+Y shares. Price factor = Y/(X+Y).
_BONUS_RE = re.compile(r"BONUS\s*(?:ISSUE)?\s*(\d+)\s*:\s*(\d+)", re.IGNORECASE)
_DIVIDEND_RE = re.compile(r"DIVIDEND.*?RS\.?\s*([\d.]+)", re.IGNORECASE)


def _classify(subject: str) -> tuple[str, float | None, float | None]:
    """(action_type, ratio, amount) parsed from NSE's free-text `subject` field.

    NSE does not expose a structured action-type field — everything is prose like
    "Sub-Division - Rs 10/- to Rs 2/-" or "Bonus Issue 1:1" — so this is a best-effort
    classifier. Anything unmatched is filed as DIVIDEND (the catch-all "cash action, no
    price adjustment needed" bucket) rather than silently dropped, so ingestion coverage
    stays visible in the raw_description field even when parsing fails.
    """
    s = subject.upper()

    m = _SPLIT_RE.search(s)
    if m:
        old_fv, new_fv = float(m.group(1)), float(m.group(2))
        ratio = round(new_fv / old_fv, 6) if old_fv > 0 else None
        return "SPLIT", ratio, None

    m = _BONUS_RE.search(s)
    if m:
        new_shares, held_shares = float(m.group(1)), float(m.group(2))
        total = new_shares + held_shares
        ratio = round(held_shares / total, 6) if total > 0 else None
        return "BONUS", ratio, None

    if "RIGHTS" in s:
        return "RIGHTS", None, None
    if "DEMERGER" in s or "SCHEME OF ARRANGEMENT" in s:
        return "DEMERGER", None, None

    m = _DIVIDEND_RE.search(s)
    amount = float(m.group(1)) if m else None
    return "DIVIDEND", None, amount


def sync_corporate_actions(lookback_days: int = 400) -> dict:
    """Fetch NSE's corporate-actions feed and upsert into `CorporateAction`.

    One page (`index=equities`) covers upcoming and recent actions; NSE does not offer a
    date-ranged history endpoint on this API, so `lookback_days` only bounds how far
    back an already-passed ex-date is still accepted, not how the fetch itself is scoped.
    """
    from stocks.models import CorporateAction

    result = {"fetched": 0, "created": 0, "updated": 0, "skipped": 0, "error": None}
    try:
        session = requests.Session()
        session.headers.update(_HEADERS)
        session.get("https://www.nseindia.com", timeout=10)
        resp = session.get(_NSE_ACTIONS_URL, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        result["error"] = str(exc)
        logger.warning("[CALENDAR] Corporate-actions fetch failed: %s", exc)
        return result

    cutoff = (datetime.now(tz=IST) - timedelta(days=lookback_days)).date()
    result["fetched"] = len(rows)

    for row in rows:
        try:
            sym = str(row.get("symbol", "")).strip().upper()
            ex_date_str = str(row.get("exDate", "")).strip()
            subject = str(row.get("subject", "")).strip()
            if not sym or not ex_date_str or ex_date_str == "-":
                result["skipped"] += 1
                continue

            ex_date = datetime.strptime(ex_date_str, "%d-%b-%Y").date()
            if ex_date < cutoff:
                result["skipped"] += 1
                continue

            action_type, ratio, amount = _classify(subject)

            _, created = CorporateAction.objects.update_or_create(
                symbol=sym, ex_date=ex_date, action_type=action_type,
                defaults={
                    "ratio": ratio, "amount": amount,
                    "raw_description": subject, "source": "nse",
                },
            )
            result["created" if created else "updated"] += 1
        except Exception as exc:
            result["skipped"] += 1
            logger.debug("[CALENDAR] Skipped corporate-action row %s: %s", row, exc)

    logger.info("[CALENDAR] Corporate actions synced: %s", result)
    return result


def sync_earnings_calendar() -> dict:
    """Persist the live earnings calendar into `EarningsEvent`.

    Reuses `event_filter_service.get_earnings_calendar()` rather than re-fetching, so
    this and the live blackout filter never disagree about what NSE returned this cycle.
    """
    from stocks.models import EarningsEvent
    from stocks.services.event_filter_service import get_earnings_calendar

    result = {"fetched": 0, "created": 0, "updated": 0, "skipped": 0}
    calendar = get_earnings_calendar()
    result["fetched"] = len(calendar)

    for sym, iso in calendar.items():
        try:
            event_date = datetime.fromisoformat(iso).date()
            _, created = EarningsEvent.objects.update_or_create(
                symbol=sym, event_date=event_date,
                defaults={"confirmed": True, "purpose": "Results", "source": "nse"},
            )
            result["created" if created else "updated"] += 1
        except Exception as exc:
            result["skipped"] += 1
            logger.debug("[CALENDAR] Skipped earnings row %s/%s: %s", sym, iso, exc)

    logger.info("[CALENDAR] Earnings calendar synced: %s", result)
    return result


def sync_all() -> dict:
    """Both feeds, guarded against overlapping runs."""
    if not cache.add(_SYNC_LOCK_KEY, True, timeout=600):
        logger.warning("[CALENDAR] Sync already running — skipping overlapping trigger.")
        return {"skipped": "lock_held"}
    try:
        return {
            "corporate_actions": sync_corporate_actions(),
            "earnings": sync_earnings_calendar(),
            "synced_at": dj_timezone.now().isoformat(),
        }
    finally:
        cache.delete(_SYNC_LOCK_KEY)


def verify_truedata_adjustment(svc, symbol: str, split_ex_date, expected_ratio: float,
                                tolerance: float = 0.03) -> dict:
    """Is TrueData's candle series already split-adjusted around a known ex-date?

    This is the check `LONG_TERM_ENGINE_V2_ARCHITECTURE.md` §0 says must happen BEFORE
    any adjuster is built — an adjuster applied to an already-adjusted series
    double-adjusts, which is a worse defect than doing nothing.

    Method: compare the close on the day before the ex-date to the close on the ex-date
    itself. An UNADJUSTED raw series shows a jump of ~`expected_ratio` on that boundary
    (e.g. a 1:5 split shows an ~80% single-bar drop). An ADJUSTED series shows continuity
    — no jump beyond ordinary daily volatility.

    Returns a dict with a `verdict` of "ADJUSTED", "UNADJUSTED", or "INCONCLUSIVE" (not
    enough data spanning the boundary) — never a silent guess.
    """
    from datetime import date as _date, timedelta as _td

    if isinstance(split_ex_date, str):
        split_ex_date = _date.fromisoformat(split_ex_date)

    token_map = svc.get_token_map([symbol], exchange="NSE") or {}
    token = token_map.get(symbol)
    if not token:
        return {"verdict": "INCONCLUSIVE", "reason": "token_not_found"}

    frm = (split_ex_date - _td(days=10)).strftime("%Y-%m-%d 09:15")
    to = (split_ex_date + _td(days=10)).strftime("%Y-%m-%d 15:30")
    df = svc.get_candle_data(token, "NSE", "ONE_DAY", frm, to)
    if df is None or df.empty:
        return {"verdict": "INCONCLUSIVE", "reason": "no_candle_data"}

    before = df[df.index.date < split_ex_date]
    on_after = df[df.index.date >= split_ex_date]
    if before.empty or on_after.empty:
        return {"verdict": "INCONCLUSIVE", "reason": "boundary_not_spanned"}

    pre_close = float(before["Close"].iloc[-1])
    post_close = float(on_after["Close"].iloc[0])
    observed_ratio = post_close / pre_close if pre_close > 0 else None

    if observed_ratio is None:
        return {"verdict": "INCONCLUSIVE", "reason": "zero_pre_close"}

    if abs(observed_ratio - expected_ratio) <= tolerance:
        verdict = "UNADJUSTED"
    elif abs(observed_ratio - 1.0) <= 0.15:
        verdict = "ADJUSTED"
    else:
        verdict = "INCONCLUSIVE"

    return {
        "verdict": verdict,
        "symbol": symbol,
        "ex_date": split_ex_date.isoformat(),
        "pre_close": pre_close,
        "post_close": post_close,
        "observed_ratio": round(observed_ratio, 4),
        "expected_ratio": expected_ratio,
        "reason": None if verdict != "INCONCLUSIVE" else "ambiguous_ratio",
    }
