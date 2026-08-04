from datetime import date

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import GlobalMarket


class LatestGlobalMarketSmokeTests(TestCase):
    """
    Audit fix M21: this app's test file was the unmodified framework stub — zero
    tests on a live trading UI's market-status-wrapping request path. Minimal smoke
    coverage: authenticated access works, unauthenticated is rejected, and a
    requested historical date that doesn't exist 404s cleanly instead of crashing.
    """

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="gm_test_user", password="x")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_requires_authentication(self):
        anon_client = APIClient()
        response = anon_client.get("/api/global-market/latest/")
        self.assertEqual(response.status_code, 401)

    def test_returns_stored_row_without_hitting_yahoo_finance(self):
        GlobalMarket.objects.create(
            date=date.today(), gift_nifty_ltp=24500.0, gift_nifty_change=0.5,
            market_bias=GlobalMarket.MarketBias.BULLISH,
        )
        # Stub the refresh path so a fresh-enough row never triggers a real network
        # call to Yahoo Finance during tests.
        with patch("global_market.views.LatestGlobalMarketView._refresh_global_data") as mock_refresh:
            response = self.client.get("/api/global-market/latest/")
            mock_refresh.assert_not_called()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["market_bias"], "BULLISH")

    def test_unknown_historical_date_returns_404_not_crash(self):
        response = self.client.get("/api/global-market/latest/", {"date": "2020-01-01"})
        self.assertEqual(response.status_code, 404)

    def test_no_data_available_returns_404_not_crash(self):
        """Empty table, refresh fails too (simulates Yahoo Finance being down) —
        must degrade to a clean 404, not an unhandled exception."""
        with patch("global_market.views.LatestGlobalMarketView._refresh_global_data", return_value=None):
            response = self.client.get("/api/global-market/latest/")

        self.assertEqual(response.status_code, 404)
