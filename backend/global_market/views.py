import logging
from datetime import date, timedelta

from django.utils import timezone
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import GlobalMarket
from .serializers import GlobalMarketSerializer

logger = logging.getLogger(__name__)

REFRESH_INTERVAL = timedelta(minutes=5)

# Auto-refresh enabled: fetches latest global market data if missing for today,
# or if the stored row is older than REFRESH_INTERVAL.


class LatestGlobalMarketView(generics.GenericAPIView):
    """
    GET /api/global-market/latest/?date=YYYY-MM-DD

    - Without ?date → fetches fresh data from Yahoo Finance (cached 5 min)
    - With ?date    → returns stored data for that date from DB
    """
    serializer_class = GlobalMarketSerializer
    permission_classes = (IsAuthenticated,)

    def get(self, request, *args, **kwargs):
        date_str = request.query_params.get('date')

        if date_str:
            # Historical date — serve from DB only
            try:
                gm = GlobalMarket.objects.get(date=date_str)
            except (GlobalMarket.DoesNotExist, ValueError):
                return Response(
                    {'detail': 'No data for the requested date.'},
                    status=404,
                )
            return Response(self.get_serializer(gm).data)

        # Latest — fetch fresh data if we don't have today's row yet, OR it's
        # more than REFRESH_INTERVAL old. Previously this only checked "does a
        # row for today exist," so once fetched it silently froze for the rest
        # of the day instead of actually refreshing every 5 min as documented
        # above — GIFT Nifty in particular moves throughout the day.
        today = timezone.localtime().date()
        gm = GlobalMarket.objects.order_by('-date').first()
        is_stale = gm is not None and gm.date == today and (timezone.now() - gm.updated_at) > REFRESH_INTERVAL

        if gm is None or gm.date < today or is_stale:
            gm = self._refresh_global_data(today, gm)

        if gm is None:
            # Absolute fallback to latest in DB regardless of date
            gm = GlobalMarket.objects.order_by('-date').first()

        if gm is None:
            return Response(
                {'detail': 'No global market data available.'},
                status=404,
            )

        return Response(self.get_serializer(gm).data)

    @staticmethod
    def _refresh_global_data(target_date, existing_gm=None):
        """Fetch fresh data from Yahoo Finance and recalculate bias for the target date."""
        try:
            from global_market.services import (
                fetch_global_market_data,
                calculate_market_bias_for_date,
            )

            gm = fetch_global_market_data(target_date)
            calculate_market_bias_for_date(target_date)
            gm.refresh_from_db()
            logger.info("Auto-refreshed global market data for %s", target_date)
            return gm
        except Exception as exc:
            logger.warning("Auto-refresh failed: %s", exc)
            return existing_gm
