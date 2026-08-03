"""
Generate a daily market insight.

- If CLAUDE_API_KEY is configured → uses Claude API for rich insight.
- Otherwise → generates a rule-based local insight from signal data.

Collects structured data (global market, bias, top bullish/bearish stocks,
top delivery, top volume spike) and produces an actionable summary.
"""

import json
import logging
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from global_market.models import GlobalMarket
from insights.models import Insight
from stocks.models import IntradaySignal, Stock

logger = logging.getLogger(__name__)


# ── Data collection ──────────────────────────────────────────

def _collect_structured_data(target_date: date) -> dict:
    """Gather all pipeline outputs into a single dict."""

    # Global market
    try:
        gm = GlobalMarket.objects.get(date=target_date)
        global_data = {
            'gift_nifty_change': str(gm.gift_nifty_change),
            'dow_jones_change': str(gm.dow_jones_change),
            'nasdaq_change': str(gm.nasdaq_change),
            'market_bias': gm.market_bias,
        }
    except GlobalMarket.DoesNotExist:
        global_data = None

    # Find the latest available date for signals and stocks if target_date has none
    latest_signal_date = IntradaySignal.objects.filter(date__lte=target_date).order_by('-date').values_list('date', flat=True).first() or target_date
    latest_stock_date = Stock.objects.filter(date__lte=target_date).order_by('-date').values_list('date', flat=True).first() or target_date

    # Top bullish signals (BUY, top 5 by confidence)
    bullish = list(
        IntradaySignal.objects.filter(date=latest_signal_date, signal_type='BUY')
        .select_related('stock')
        .order_by('-confidence_score')[:5]
        .values_list('stock__symbol', 'confidence_score', 'reasoning_summary')
    )

    # Top bearish signals (SELL, top 5 by confidence)
    bearish = list(
        IntradaySignal.objects.filter(date=latest_signal_date, signal_type='SELL')
        .select_related('stock')
        .order_by('-confidence_score')[:5]
        .values_list('stock__symbol', 'confidence_score', 'reasoning_summary')
    )

    # Top delivery % stocks
    top_delivery = list(
        Stock.objects.filter(date=latest_stock_date, delivery_percentage__isnull=False)
        .order_by('-delivery_percentage')[:5]
        .values_list('symbol', 'delivery_percentage', 'price_change_percent', 'close')
    )

    # Top volume spike stocks
    top_vol_spike = list(
        Stock.objects.filter(date=latest_stock_date, volume_spike_ratio__isnull=False)
        .order_by('-volume_spike_ratio')[:5]
        .values_list('symbol', 'volume_spike_ratio', 'price_change_percent', 'close')
    )

    return {
        'date': str(target_date),
        'global_market': global_data,
        'top_bullish_stocks': [
            {'symbol': s, 'confidence': str(c), 'reasoning': r}
            for s, c, r in bullish
        ],
        'top_bearish_stocks': [
            {'symbol': s, 'confidence': str(c), 'reasoning': r}
            for s, c, r in bearish
        ],
        'top_delivery_stocks': [
            {'symbol': s, 'delivery_pct': str(d), 'change_pct': str(c or 0), 'close': str(p)}
            for s, d, c, p in top_delivery
        ],
        'top_volume_spike_stocks': [
            {'symbol': s, 'spike_ratio': str(v), 'change_pct': str(c or 0), 'close': str(p)}
            for s, v, c, p in top_vol_spike
        ],
        'total_buy_signals': IntradaySignal.objects.filter(
            date=latest_signal_date, signal_type='BUY'
        ).count(),
        'total_sell_signals': IntradaySignal.objects.filter(
            date=latest_signal_date, signal_type='SELL'
        ).count(),
        'total_hold_signals': IntradaySignal.objects.filter(
            date=latest_signal_date, signal_type='HOLD'
        ).count(),
    }


# ── Local insight generator (no API key needed) ─────────────

def _generate_local_insight(data: dict) -> str:
    """
    Build a rule-based market insight from structured data.
    Produces actionable text without needing an external API.
    """
    lines = []
    target_date = data['date']

    # Global cues
    gm = data.get('global_market')
    if gm:
        bias = gm['market_bias']
        
        def _safe_float(val):
            return 0.0 if not val or val == 'None' else float(val)

        gift = _safe_float(gm.get('gift_nifty_change'))
        dow = _safe_float(gm.get('dow_jones_change'))
        nasdaq = _safe_float(gm.get('nasdaq_change'))

        if bias == 'BEARISH':
            lines.append(
                f"Global cues are negative. GIFT Nifty ({gift:+.2f}%), "
                f"Dow Jones ({dow:+.2f}%), and Nasdaq ({nasdaq:+.2f}%) "
                f"indicate a BEARISH sentiment for the next trading session."
            )
        elif bias == 'BULLISH':
            lines.append(
                f"Global cues are positive. GIFT Nifty ({gift:+.2f}%), "
                f"Dow Jones ({dow:+.2f}%), and Nasdaq ({nasdaq:+.2f}%) "
                f"suggest a BULLISH outlook for the next session."
            )
        else:
            lines.append(
                f"Global cues are mixed. GIFT Nifty ({gift:+.2f}%), "
                f"Dow Jones ({dow:+.2f}%), and Nasdaq ({nasdaq:+.2f}%) "
                f"point to a NEUTRAL market stance."
            )

    # Signal summary
    buy = data['total_buy_signals']
    sell = data['total_sell_signals']
    hold = data['total_hold_signals']
    total = buy + sell + hold

    if total > 0:
        lines.append(
            f"\nOut of {total} Nifty 200 stocks analyzed: "
            f"{buy} BUY, {sell} SELL, {hold} HOLD signals."
        )

        if sell > buy * 3:
            lines.append(
                "The market shows strong selling pressure across most stocks. "
                "Caution is advised — consider reducing long exposure."
            )
        elif sell > buy * 1.5:
            lines.append(
                "Selling pressure outweighs buying interest. "
                "Selective stock picking is recommended over broad exposure."
            )
        elif buy > sell * 1.5:
            lines.append(
                "Buying interest is dominant. "
                "Look for quality stocks with strong delivery for next-day momentum."
            )
        else:
            lines.append(
                "The market is fairly balanced between buyers and sellers. "
                "Focus on stocks with strong conviction signals."
            )

    # Top bullish picks
    bullish = data.get('top_bullish_stocks', [])
    if bullish:
        symbols = ', '.join(s['symbol'] for s in bullish)
        lines.append(
            f"\nTop BUY candidates for next session: {symbols}. "
            f"These show positive price momentum, strong delivery, "
            f"and favorable volume patterns."
        )
        # Detail the best pick
        best = bullish[0]
        lines.append(
            f"Strongest conviction: {best['symbol']} "
            f"(confidence: {float(best['confidence']):.1f}%)."
        )

    # Top bearish picks
    bearish = data.get('top_bearish_stocks', [])
    if bearish:
        symbols = ', '.join(s['symbol'] for s in bearish[:3])
        lines.append(
            f"\nStocks to AVOID or consider shorting: {symbols}. "
            f"These show negative price action with rising volume/OI."
        )

    # High delivery stocks (institutional interest)
    delivery = data.get('top_delivery_stocks', [])
    if delivery:
        symbols = ', '.join(s['symbol'] for s in delivery[:3])
        lines.append(
            f"\nHigh delivery % (institutional interest): {symbols}. "
            f"High delivery often signals strong hands accumulating — "
            f"watch these for next-day continuation."
        )

    # Volume spike stocks
    vol_spike = data.get('top_volume_spike_stocks', [])
    if vol_spike:
        symbols = ', '.join(s['symbol'] for s in vol_spike[:3])
        lines.append(
            f"\nVolume spike alert: {symbols}. "
            f"Unusual volume activity detected — these could see "
            f"continuation or reversal in the next session."
        )

    # Actionable outlook
    if gm and gm['market_bias'] == 'BEARISH':
        lines.append(
            "\nOutlook: Given bearish global cues, traders should maintain "
            "tight stop-losses and consider hedging positions. "
            "Look for oversold bounces in quality names with high delivery %."
        )
    elif gm and gm['market_bias'] == 'BULLISH':
        lines.append(
            "\nOutlook: Positive global sentiment supports an up-move. "
            "Focus on BUY signals with high confidence scores and "
            "strong delivery percentage for swing trades."
        )
    else:
        lines.append(
            "\nOutlook: In a range-bound market, focus on stock-specific "
            "setups. Prefer stocks with volume spike + high delivery "
            "for short-term momentum plays."
        )

    return '\n'.join(lines)


# ── Claude API insight generator ─────────────────────────────

_SYSTEM_PROMPT = (
    "You are a professional Indian stock market analyst. "
    "Given today's structured market data, write a concise pre-market "
    "insight summary (150-250 words). Cover: global cues, overall market bias, "
    "key bullish and bearish stocks to watch for next session, "
    "high delivery % stocks showing institutional interest, "
    "and a brief actionable outlook for traders. "
    "Use clear language. Do not use markdown headers."
)


def _call_claude_api(structured_data: dict) -> str:
    """Send structured data to Claude API and return the AI summary text."""
    import anthropic

    client = anthropic.Anthropic(api_key=settings.CLAUDE_API_KEY)

    user_message = (
        f"Here is today's market data:\n\n"
        f"{json.dumps(structured_data, indent=2)}\n\n"
        f"Please provide your pre-market insight summary for the next "
        f"trading session. Suggest which stocks look best for buying "
        f"and which to avoid."
    )

    logger.info("Calling Claude API for AI insight...")

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": user_message},
        ],
    )

    ai_text = message.content[0].text
    logger.info("Claude API response received (%d chars)", len(ai_text))
    return ai_text


# ── Main entry point ─────────────────────────────────────────

def generate_daily_insight(target_date: date) -> Insight | None:
    """
    Generate a daily market insight for *target_date*.

    - If CLAUDE_API_KEY is set → uses Claude API.
    - Otherwise → generates a local rule-based insight.

    Upserts Insight model (one per date, idempotent).
    Returns the Insight instance, or None if no data is available.
    """
    structured_data = _collect_structured_data(target_date)

    # Skip if there's no meaningful data to summarize
    if (
        structured_data['global_market'] is None
        and structured_data['total_buy_signals'] == 0
        and structured_data['total_sell_signals'] == 0
    ):
        logger.warning("No data available for %s — skipping insight", target_date)
        return None

    # Choose AI or local generation
    api_key = getattr(settings, 'CLAUDE_API_KEY', '')
    if api_key:
        logger.info("CLAUDE_API_KEY is set — using Claude API")
        ai_summary = _call_claude_api(structured_data)
    else:
        logger.info("No CLAUDE_API_KEY — generating local insight")
        ai_summary = _generate_local_insight(structured_data)

    # Upsert insight (one record per date)
    with transaction.atomic():
        insight, created = Insight.objects.update_or_create(
            date=target_date,
            defaults={
                'ai_summary': ai_summary,
                'structured_data_json': structured_data,
            },
        )

    action = 'Created' if created else 'Updated'
    logger.info("%s Insight for %s (id=%d)", action, target_date, insight.pk)
    return insight
