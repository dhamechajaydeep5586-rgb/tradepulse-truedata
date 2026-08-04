from datetime import date

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Insight


class DailyInsightSmokeTests(TestCase):
    """
    Audit fix M21: this app's test file was the unmodified framework stub — zero
    tests on the AI-insight generation path in a live trading UI's request path.
    Minimal smoke coverage: authenticated access works, unauthenticated is
    rejected, a requested historical date that doesn't exist 404s cleanly, and a
    fully-empty table with a failing generator degrades to 200 with a placeholder
    instead of crashing (matches the view's own documented fallback contract).
    """

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="insights_test_user", password="x")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_requires_authentication(self):
        anon_client = APIClient()
        response = anon_client.get("/api/insights/daily/")
        self.assertEqual(response.status_code, 401)

    def test_returns_todays_stored_insight_without_regenerating(self):
        Insight.objects.create(date=date.today(), ai_summary="Today's market summary.")

        with patch("insights.services.ai_insight_service.generate_daily_insight") as mock_gen:
            response = self.client.get("/api/insights/daily/")
            mock_gen.assert_not_called()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["ai_summary"], "Today's market summary.")

    def test_unknown_historical_date_returns_404_not_crash(self):
        response = self.client.get("/api/insights/daily/", {"date": "2020-01-01"})
        self.assertEqual(response.status_code, 404)

    def test_no_data_and_generation_failure_degrades_to_placeholder_not_crash(self):
        with patch(
            "insights.services.ai_insight_service.generate_daily_insight",
            side_effect=Exception("AI service down"),
        ):
            response = self.client.get("/api/insights/daily/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("No AI insight available", response.data.get("ai_summary", ""))
