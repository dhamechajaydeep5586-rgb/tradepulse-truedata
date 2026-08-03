"""
Compute market bias from GIFT Nifty's daily % change (the only global
indicator tracked — Dow Jones/Nasdaq were dropped along with Yahoo Finance).

Score > 0.25  → BULLISH
Score < -0.25 → BEARISH
Otherwise     → NEUTRAL
"""

import logging
from datetime import date

from django.conf import settings as django_settings

from global_market.models import GlobalMarket

logger = logging.getLogger(__name__)


def calculate_market_bias_for_date(target_date: date) -> str | None:
    """
    Look up the GlobalMarket row for *target_date*, compute the bias from
    gift_nifty_change, and store the market_bias label.

    Returns the bias string ('BULLISH', 'BEARISH', or 'NEUTRAL'),
    or None if no GlobalMarket row exists for this date.
    """
    try:
        gm = GlobalMarket.objects.get(date=target_date)
    except GlobalMarket.DoesNotExist:
        logger.warning("No GlobalMarket row for %s — cannot compute bias", target_date)
        return None

    score = float(gm.gift_nifty_change) if gm.gift_nifty_change is not None else 0.0

    bullish_threshold = getattr(django_settings, 'MARKET_BIAS_BULLISH_THRESHOLD', 0.25)
    bearish_threshold = getattr(django_settings, 'MARKET_BIAS_BEARISH_THRESHOLD', -0.25)

    if score > bullish_threshold:
        bias = GlobalMarket.MarketBias.BULLISH
    elif score < bearish_threshold:
        bias = GlobalMarket.MarketBias.BEARISH
    else:
        bias = GlobalMarket.MarketBias.NEUTRAL

    gm.market_bias = bias
    gm.save(update_fields=['market_bias'])

    logger.info("Market bias for %s: %s (score=%.4f)", target_date, bias, score)
    return bias
