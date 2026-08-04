"""
Fetch GIFT Nifty proxy data from TrueData and upsert into GlobalMarket model.

Ticker used:
  - GIFT Nifty proxy → NIFTY 50 index (TrueData symbol "NIFTY 50", NSE segment)

Dow Jones / Nasdaq were previously sourced from Yahoo Finance but were dropped —
TrueData (an Indian-markets-only broker) has no US index coverage, and the
project no longer uses Yahoo Finance anywhere.
"""

import logging
from datetime import date
from decimal import Decimal

from django.db import transaction

from global_market.models import GlobalMarket
from stocks.services.truedata_service import get_truedata_instance

logger = logging.getLogger(__name__)

NIFTY_50_TOKEN = "NIFTY 50"


def _fetch_gift_nifty() -> tuple[Decimal | None, Decimal | None]:
    """
    Fetch the latest LTP and daily % change for the Nifty 50 index via TrueData.
    Returns (ltp, pct_change) — either can be None on failure.
    """
    try:
        svc = get_truedata_instance()
        if not svc:
            logger.warning("TrueData service unavailable for GIFT Nifty fetch")
            return None, None

        quote = svc.get_live_price_by_token(NIFTY_50_TOKEN, exchange="NSE")
        if not quote or not quote.get("ltp", 0):
            logger.warning("No GIFT Nifty (TrueData Nifty 50) quote returned")
            return None, None

        ltp = Decimal(str(round(float(quote["ltp"]), 2)))
        change = quote.get("change_percent")
        pct_change = Decimal(str(round(float(change), 4))) if change is not None else None

        return ltp, pct_change

    except Exception as exc:
        logger.warning("Failed to fetch GIFT Nifty via TrueData: %s", exc)
        return None, None


def fetch_global_market_data(target_date: date) -> GlobalMarket:
    """
    Fetch GIFT Nifty data from TrueData and upsert a GlobalMarket row for
    *target_date*. Returns the GlobalMarket instance (created or updated).
    """
    logger.info("Fetching GIFT Nifty data from TrueData for %s...", target_date)

    gift_ltp, gift_change = _fetch_gift_nifty()

    logger.info("TrueData GIFT Nifty data: %s (%.2f%%)", gift_ltp, gift_change or 0)

    if gift_ltp is None:
        last_gm = GlobalMarket.objects.exclude(date=target_date).order_by('-date').first()
        if last_gm:
            gift_ltp = last_gm.gift_nifty_ltp
            gift_change = last_gm.gift_nifty_change
            logger.info("Used fallback data from previous day due to missing TrueData data.")

    # Upsert: one record per date
    with transaction.atomic():
        gm, created = GlobalMarket.objects.update_or_create(
            date=target_date,
            defaults={
                'gift_nifty_ltp': gift_ltp,
                'gift_nifty_change': gift_change,
            },
        )

    action = 'Created' if created else 'Updated'
    logger.info("%s GlobalMarket row for %s (id=%d)", action, target_date, gm.pk)
    return gm
