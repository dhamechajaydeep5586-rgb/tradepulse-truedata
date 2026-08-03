from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Insight
from .serializers import InsightSerializer


class DailyInsightView(generics.GenericAPIView):
    """
    GET /api/insights/daily/?date=YYYY-MM-DD
    Returns the insight for a specific date, or the most recent one.
    """
    serializer_class = InsightSerializer
    permission_classes = (IsAuthenticated,)

    def get(self, request, *args, **kwargs):
        date_str = request.query_params.get('date')

        if date_str:
            try:
                insight = Insight.objects.get(date=date_str)
            except (Insight.DoesNotExist, ValueError):
                return Response(
                    {'detail': 'No insight for the requested date.'},
                    status=404,
                )
        else:
            from django.utils import timezone
            today = timezone.localtime().date()
            insight = Insight.objects.order_by('-date').first()

            if insight is None or insight.date < today:
                try:
                    from insights.services.ai_insight_service import generate_daily_insight
                    new_insight = generate_daily_insight(today)
                    if new_insight:
                        insight = new_insight
                except Exception as e:
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.warning("Failed to auto-generate insight for %s: %s", today, e)

            if insight is None:
                # Fallback to absolute latest regardless of date if today's is missing
                insight = Insight.objects.order_by('-date').first()

            if insight is None:
                return Response(
                    {'detail': 'Market is closed or no data available yet.', 'ai_summary': 'No AI insight available for today.'},
                    status=200,
                )

        return Response(self.get_serializer(insight).data)
