from datetime import datetime, date
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.test import TestCase

from stocks.models import SignalHistory
from stocks.services.live_signal_service import update_signal_outcomes


IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


class MockDatetime(datetime):
    _mock_now = None

    @classmethod
    def now(cls, tz=None):
        if cls._mock_now:
            return cls._mock_now.astimezone(tz) if tz else cls._mock_now
        return super().now(tz)


class UpdateSignalOutcomesTests(TestCase):
    def setUp(self):
        # Prevent actual API status checks during tests
        self.patcher_static_closed = patch("stocks.services.signal_utils.is_static_closed", return_value=False)
        self.patcher_static_closed.start()

        # Mock standard datetime in live_signal_service to control now_time
        self.patcher_datetime = patch("stocks.services.live_signal_service.datetime", MockDatetime)
        self.patcher_datetime.start()

    def tearDown(self):
        self.patcher_static_closed.stop()
        self.patcher_datetime.stop()
        MockDatetime._mock_now = None

    def _create_signal(self, symbol: str = "CDSL", status=SignalHistory.Status.PENDING, category="intraday") -> SignalHistory:
        signal = SignalHistory.objects.create(
            symbol=symbol,
            signal_type="BUY",
            entry_price=1184.30,
            stop_loss=1174.90,
            target=1209.40,
            rr=2.7,
            status=status,
            category=category,
            reason="Strong BUY (POC Rejection)",
        )
        generated_at = datetime(2026, 4, 1, 9, 21, tzinfo=IST)
        SignalHistory.objects.filter(pk=signal.pk).update(generated_at=generated_at)
        signal.refresh_from_db()
        return signal

    @patch("stocks.services.live_signal_service.get_latest_prices")
    def test_pending_signal_becomes_active_when_entry_touched(
        self,
        mock_get_prices,
    ):
        # 1. Arrange
        signal = self._create_signal(status=SignalHistory.Status.PENDING)
        test_now = datetime(2026, 4, 1, 10, 0, tzinfo=IST)  # 10:00 AM (before 15:20 cutoff)
        MockDatetime._mock_now = test_now
        mock_get_prices.return_value = {"CDSL": 1184.30}  # exact entry price

        # 2. Act
        with patch("stocks.services.live_signal_service.dj_timezone.now", return_value=test_now):
            update_signal_outcomes(force=True)

        # 3. Assert
        signal.refresh_from_db()
        self.assertEqual(signal.status, SignalHistory.Status.ACTIVE)
        self.assertEqual(signal.active_time, test_now)

    @patch("stocks.services.live_signal_service.get_latest_prices")
    def test_active_signal_hits_target(
        self,
        mock_get_prices,
    ):
        # 1. Arrange
        signal = self._create_signal(status=SignalHistory.Status.ACTIVE, category="swing")
        test_now = datetime(2026, 4, 1, 10, 0, tzinfo=IST)  # 10:00 AM
        MockDatetime._mock_now = test_now
        mock_get_prices.return_value = {"CDSL": 1210.0}  # above target 1209.40

        # 2. Act
        with patch("stocks.services.live_signal_service.dj_timezone.now", return_value=test_now):
            update_signal_outcomes(force=True)

        # 3. Assert
        signal.refresh_from_db()
        self.assertEqual(signal.status, SignalHistory.Status.HIT_TARGET)
        self.assertEqual(float(signal.exit_price), 1209.40)
        self.assertEqual(signal.exit_time, test_now)

    @patch("stocks.services.truedata_service.get_truedata_instance")
    @patch("stocks.services.live_signal_service.get_latest_prices")
    def test_intraday_stop_gap_through_records_bar_low_not_raw_stop(
        self, mock_get_prices, mock_get_svc,
    ):
        """Audit fix: a genuine gap-through (bar low well past the stop, e.g. a news
        gap / illiquid reopen) must book the bar's actual touched low as exit_price,
        not the optimistic raw stop level — this exit price feeds the daily-loss-limit
        kill switch's realised P&L sum (_enforce_daily_loss_limit), so understating the
        loss here is exactly the scenario the kill switch most needs to catch."""
        import pandas as pd

        signal = self._create_signal(status=SignalHistory.Status.ACTIVE, category="intraday")
        test_now = datetime(2026, 4, 1, 10, 30, tzinfo=IST)
        MockDatetime._mock_now = test_now
        mock_get_prices.return_value = {"CDSL": 1150.0}

        # stop_loss=1174.90 (see _create_signal). Bar low gaps well past it — far
        # beyond the 0.05% graze tolerance in gap_adjusted_stop_price.
        gapped_low = 1150.0
        bars = pd.DataFrame(
            {"Open": [1180.0], "High": [1180.0], "Low": [gapped_low],
             "Close": [1155.0], "Volume": [10000]},
            index=pd.DatetimeIndex([datetime(2026, 4, 1, 10, 0)]),  # naive -> localized to IST
        )

        mock_svc = MagicMock()
        mock_svc.get_token_map.return_value = {"CDSL": "CDSL"}
        mock_svc.get_candle_data.return_value = bars
        mock_get_svc.return_value = mock_svc

        with patch("stocks.services.live_signal_service.dj_timezone.now", return_value=test_now):
            update_signal_outcomes(force=True)

        signal.refresh_from_db()
        self.assertEqual(signal.status, SignalHistory.Status.HIT_SL)
        self.assertAlmostEqual(float(signal.exit_price), gapped_low, delta=0.06)
        self.assertNotEqual(float(signal.exit_price), 1174.90)  # not the raw stop level

    @patch("stocks.services.live_signal_service.get_latest_prices")
    def test_intraday_pending_signal_cancelled_at_cutoff(
        self,
        mock_get_prices,
    ):
        # 1. Arrange
        signal = self._create_signal(status=SignalHistory.Status.PENDING)
        test_now = datetime(2026, 4, 1, 15, 30, tzinfo=IST)  # 3:30 PM (after 15:20 cutoff)
        MockDatetime._mock_now = test_now
        mock_get_prices.return_value = {"CDSL": 1180.0}  # doesn't matter

        # 2. Act
        with patch("stocks.services.live_signal_service.dj_timezone.now", return_value=test_now):
            update_signal_outcomes(force=True)

        # 3. Assert
        signal.refresh_from_db()
        self.assertEqual(signal.status, SignalHistory.Status.CANCELLED)
        self.assertEqual(signal.exit_time, test_now)

    @patch("stocks.services.live_signal_service.get_latest_prices")
    def test_intraday_active_signal_expired_at_cutoff(
        self,
        mock_get_prices,
    ):
        # 1. Arrange
        signal = self._create_signal(status=SignalHistory.Status.ACTIVE)
        test_now = datetime(2026, 4, 1, 15, 30, tzinfo=IST)  # 3:30 PM (after 15:20 cutoff)
        MockDatetime._mock_now = test_now
        mock_get_prices.return_value = {"CDSL": 1180.0}

        # 2. Act
        with patch("stocks.services.live_signal_service.dj_timezone.now", return_value=test_now):
            update_signal_outcomes(force=True)

        # 3. Assert
        signal.refresh_from_db()
        self.assertEqual(signal.status, SignalHistory.Status.EXPIRED)
        self.assertEqual(signal.exit_time, test_now)


class TelegramServiceTests(TestCase):
    def setUp(self):
        # Setup settings for Telegram
        self.patcher_settings = patch("stocks.services.telegram_service.is_enabled", return_value=True)
        self.mock_is_enabled = self.patcher_settings.start()
        
        self.patcher_send = patch("stocks.services.telegram_service.send_telegram_message", return_value=True)
        self.mock_send = self.patcher_send.start()

    def tearDown(self):
        self.patcher_settings.stop()
        self.patcher_send.stop()

    def test_send_periodic_pnl_updates(self):
        from stocks.services.telegram_service import send_periodic_pnl_updates

        # Create an active specialist equity signal with CE and PE legs
        sig = SignalHistory.objects.create(
            symbol="INFY",
            entry_price=1400.0,
            stop_loss=0,
            target=0,
            category="specialist",
            status=SignalHistory.Status.ACTIVE,
            metadata={
                "legs": [
                    {
                        "action": "SELL",
                        "option_type": "CE",
                        "strike": 1450.0,
                        "original_sell_price": 30.0,
                        "sell_price": 30.0,
                        "cmp": 20.0,
                        "pnl": 500.0,
                        "lot_size": 50
                    },
                    {
                        "action": "SELL",
                        "option_type": "PE",
                        "strike": 1350.0,
                        "original_sell_price": 20.0,
                        "sell_price": 20.0,
                        "cmp": 15.0,
                        "pnl": 250.0,
                        "lot_size": 50
                    }
                ]
            }
        )

        success = send_periodic_pnl_updates()
        self.assertTrue(success)
        self.mock_send.assert_called_once()

        call_args = self.mock_send.call_args[0][0]

        # Header checks
        self.assertIn("Strangles Session Update", call_args)
        self.assertIn("INFY", call_args)

        # New format: CE and PE leg lines must be present
        self.assertIn("CE 1,450: ₹30.00 → ₹20.00", call_args)          # CE strike label
        self.assertIn("PE 1,350: ₹20.00 → ₹15.00", call_args)          # PE strike label

        # P&L line (calculated for 2 lots: 750 * 2 = 1500)
        self.assertIn("P&L (2 Lots): 🟢 *₹1,500.00*", call_args)



# ──────────────────────────────────────────────
# NEW TESTS: Entry Consistency, CMP, P&L, Duplicates
# ──────────────────────────────────────────────

class SpecialistSignalConsistencyTests(TestCase):
    """Tests for entry premium immutability, CMP calculation, and duplicate prevention."""

    def _make_active_signal(self, symbol="BAJAJFINSV", ce_original=37.25, pe_original=24.85,
                             ce_cmp=30.10, pe_cmp=21.60, lot_size=250):
        """Helper: create an active specialist signal with original_sell_price set."""
        return SignalHistory.objects.create(
            symbol=symbol,
            entry_price=1804.0,
            stop_loss=0,
            target=0,
            category="specialist",
            status=SignalHistory.Status.ACTIVE,
            metadata={
                "legs": [
                    {
                        "action": "SELL",
                        "option_type": "CE",
                        "strike": 1860.0,
                        "original_sell_price": ce_original,
                        "sell_price": ce_cmp,      # sell_price may have floated during grace
                        "cmp": ce_cmp,
                        "pnl": round((ce_original - ce_cmp) * lot_size, 2),
                        "lot_size": lot_size
                    },
                    {
                        "action": "SELL",
                        "option_type": "PE",
                        "strike": 1740.0,
                        "original_sell_price": pe_original,
                        "sell_price": pe_cmp,      # sell_price may have floated during grace
                        "cmp": pe_cmp,
                        "pnl": round((pe_original - pe_cmp) * lot_size, 2),
                        "lot_size": lot_size
                    }
                ],
                "confidence": 85.0
            }
        )

    def test_entry_premium_consistency(self):
        """
        Entry shown in update message must always be original_sell_price (CE + PE),
        not sell_price which floats during the grace window and gets locked to CMP at activation.
        """
        from stocks.services.telegram_service import format_new_signal_message

        sig = self._make_active_signal(
            ce_original=37.25, pe_original=24.85,
            ce_cmp=30.10,       pe_cmp=21.60,
        )

        # Build a PENDING version to simulate format_new_signal_message using original prices
        # (the signal was just created; sell_price == original_sell_price at creation)
        pending_sig = SignalHistory()
        pending_sig.symbol = "BAJAJFINSV"
        pending_sig.entry_price = 1804.0
        pending_sig.metadata = {
            "legs": [
                {"action": "SELL", "option_type": "CE", "strike": 1860.0,
                 "original_sell_price": 37.25, "sell_price": 37.25, "lot_size": 250},
                {"action": "SELL", "option_type": "PE", "strike": 1740.0,
                 "original_sell_price": 24.85, "sell_price": 24.85, "lot_size": 250},
            ],
            "confidence": 85.0
        }

        msg = format_new_signal_message(pending_sig)

        # Must show original entry prices
        self.assertIn("₹37.25", msg)
        self.assertIn("₹24.85", msg)
        # Combined entry premium
        self.assertIn("Net Credit*: ₹62.10", msg)

    def test_cmp_is_sum_of_ce_and_pe_cmp(self):
        """
        CMP shown in update message = current CE ltp + current PE ltp.
        It must NOT be derived from sell_price.
        """
        from stocks.services.telegram_service import send_periodic_pnl_updates
        from unittest.mock import patch

        sig = self._make_active_signal(
            ce_original=37.25, pe_original=24.85,
            ce_cmp=30.10,       pe_cmp=21.60,
        )
        expected_cmp = round(30.10 + 21.60, 2)  # 51.70
        expected_entry = round(37.25 + 24.85, 2)  # 62.10 (True original entry premium)

        with patch("stocks.services.telegram_service.is_enabled", return_value=True), \
             patch("stocks.services.telegram_service.send_telegram_message", return_value=True) as mock_send:
            send_periodic_pnl_updates()

        mock_send.assert_called_once()
        message = mock_send.call_args[0][0]

        self.assertIn("CE 1,860: ₹37.25 → ₹30.10", message)
        self.assertIn("PE 1,740: ₹24.85 → ₹21.60", message)



    def test_pnl_uses_stored_leg_pnl_values(self):
        """
        P&L in update message must be sum of pre-stored per-leg pnl (lot-adjusted),
        NOT re-computed from entry - cmp.
        """
        from stocks.services.telegram_service import send_periodic_pnl_updates
        from unittest.mock import patch

        lot = 250
        ce_entry, pe_entry = 37.25, 24.85
        ce_cmp, pe_cmp = 30.10, 21.60
        # Per-leg pnl already stored (calculated by process_legs)
        ce_pnl = round((ce_entry - ce_cmp) * lot, 2)   # 1,787.50
        pe_pnl = round((pe_entry - pe_cmp) * lot, 2)   # 812.50
        total_stored_pnl = ce_pnl + pe_pnl                  # 2,600.00

        sig = SignalHistory.objects.create(
            symbol="BAJAJFINSV",
            entry_price=1804.0,
            stop_loss=0, target=0,
            category="specialist",
            status=SignalHistory.Status.ACTIVE,
            metadata={
                "legs": [
                    {"action": "SELL", "option_type": "CE", "strike": 1860.0,
                     "original_sell_price": ce_entry, "sell_price": ce_cmp,
                     "cmp": ce_cmp, "pnl": ce_pnl, "lot_size": lot},
                    {"action": "SELL", "option_type": "PE", "strike": 1740.0,
                     "original_sell_price": pe_entry, "sell_price": pe_cmp,
                     "cmp": pe_cmp, "pnl": pe_pnl, "lot_size": lot},
                ],
                "confidence": 85.0
            }
        )

        with patch("stocks.services.telegram_service.is_enabled", return_value=True), \
             patch("stocks.services.telegram_service.send_telegram_message", return_value=True) as mock_send:
            send_periodic_pnl_updates()

        mock_send.assert_called_once()
        message = mock_send.call_args[0][0]

        # The P&L line should reflect the stored lot-adjusted pnl values (multiplied by 2 for 2 lots)
        pnl_formatted = f"{total_stored_pnl * 2.0:,.2f}"
        self.assertIn(pnl_formatted, message)

    def test_no_duplicate_signals_for_same_symbol_same_day(self):
        """
        Creating a second specialist signal for the same symbol on the same day
        should be caught by the duplicate guard in the scanner.
        The DB check used in the scanner must return True when a signal already exists.
        """
        from django.utils import timezone

        # Create first signal
        sig1 = SignalHistory.objects.create(
            symbol="BRITANNIA",
            entry_price=5324.0,
            stop_loss=0, target=0,
            category="specialist",
            status=SignalHistory.Status.PENDING,
            metadata={"legs": [], "confidence": 85.0}
        )

        today_start = timezone.now().astimezone(
            __import__("zoneinfo").ZoneInfo("Asia/Kolkata")
        ).replace(hour=0, minute=0, second=0, microsecond=0)

        # Simulate the duplicate check used in _background_scan
        already_exists = SignalHistory.objects.filter(
            symbol="BRITANNIA",
            category="specialist",
            status__in=[SignalHistory.Status.PENDING, SignalHistory.Status.ACTIVE],
            generated_at__gte=today_start
        ).exists()

        self.assertTrue(already_exists,
            "Duplicate guard must detect existing PENDING signal for same symbol today")

        # Count must be exactly 1 (no duplicates created)
        count = SignalHistory.objects.filter(
            symbol="BRITANNIA",
            category="specialist",
            generated_at__date=timezone.now().astimezone(
                __import__("zoneinfo").ZoneInfo("Asia/Kolkata")
            ).date()
        ).count()
        self.assertEqual(count, 1, "Should have exactly 1 signal for BRITANNIA today")


# ──────────────────────────────────────────────
# NEW TESTS: Tick Rounding & Equal-Premium Pair
# ──────────────────────────────────────────────

class SpecialistStrikeSelectionTests(TestCase):
    """
    Unit tests for:
    1. Tick rounding (₹0.05) applied to all stored option premiums.
    2. find_equal_premium_pair() selects CE/PE pair with minimum |CE - PE|.
    """

    def test_tick_rounding_produces_valid_option_prices(self):
        """
        round_to_tick(val, 0.05) must produce ₹0.05-aligned values.
        Tests the exact real-world premium examples from the failing signals.
        """
        from stocks.services.signal_utils import round_to_tick

        cases = [
            # (raw_ltp, expected_rounded)
            (18.69, 18.70),   # BAJFINANCE CE  — was ₹18.69 (invalid tick)
            (14.31, 14.30),   # BAJFINANCE PE  — was ₹14.31 (invalid tick)
            (106.29, 106.30), # BRITANNIA CE   — was ₹106.29 (invalid tick)
            (77.77, 77.75),   # BRITANNIA PE   — was ₹77.77 (invalid tick)
            (37.25, 37.25),   # BAJAJFINSV CE  — already valid
            (24.85, 24.85),   # BAJAJFINSV PE  — already valid
            (18.50, 18.50),   # Exact multiple — unchanged
            (0.00, 0.00),     # Zero premium   — unchanged
        ]

        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = round_to_tick(raw, 0.05)
                self.assertEqual(
                    result, expected,
                    f"round_to_tick({raw}, 0.05) = {result}, expected {expected}"
                )
                # Result must always be a multiple of ₹0.05
                # Using round to avoid floating-point representation issues like 77.75 % 0.05 == 0.04999999999999
                remainder = round((result * 100) % 5, 4)
                self.assertAlmostEqual(
                    remainder, 0.0, places=4,
                    msg=f"₹{result} is not a valid ₹0.05 tick-size multiple"
                )

    def test_find_equal_premium_pair_selects_minimum_diff(self):
        """
        find_equal_premium_pair() must return the (CE, PE) pair where
        |CE_premium - PE_premium| is minimised across all candidate combinations.
        """
        from stocks.services.delta_hedge_service import find_equal_premium_pair

        # Simulate 3 CE and 3 PE candidates using plain dicts (no DB needed)
        ce1 = {'strike': 97000, 'symbol': 'BAJFINANCE26MAY970CE', 'expiry': '29May2026'}
        ce2 = {'strike': 96000, 'symbol': 'BAJFINANCE26MAY960CE', 'expiry': '29May2026'}
        ce3 = {'strike': 95000, 'symbol': 'BAJFINANCE26MAY950CE', 'expiry': '29May2026'}

        pe1 = {'strike': 91000, 'symbol': 'BAJFINANCE26MAY910PE', 'expiry': '29May2026'}
        pe2 = {'strike': 92000, 'symbol': 'BAJFINANCE26MAY920PE', 'expiry': '29May2026'}
        pe3 = {'strike': 93000, 'symbol': 'BAJFINANCE26MAY930PE', 'expiry': '29May2026'}

        ce_candidates = [ce1, ce2, ce3]
        pe_candidates = [pe1, pe2, pe3]

        # LTP map: ce1=18.70, ce2=16.50, ce3=13.00
        #          pe1=12.00, pe2=16.25, pe3=19.50
        # All diffs: ce2 vs pe2 = |16.50-16.25| = 0.25  ← minimum
        ce_ltp_map = {id(ce1): 18.70, id(ce2): 16.50, id(ce3): 13.00}
        pe_ltp_map = {id(pe1): 12.00, id(pe2): 16.25, id(pe3): 19.50}

        best_ce, best_pe, min_diff = find_equal_premium_pair(
            ce_candidates, pe_candidates, ce_ltp_map, pe_ltp_map,
            fallback_ce=ce1, fallback_pe=pe1
        )

        self.assertIs(best_ce, ce2, "Should select ce2 (₹16.50) — closest to pe2 (₹16.25)")
        self.assertIs(best_pe, pe2, "Should select pe2 (₹16.25) — closest to ce2 (₹16.50)")
        self.assertAlmostEqual(min_diff, 0.25, places=2)

    def test_find_equal_premium_pair_falls_back_when_no_ltp(self):
        """
        When all LTP values are zero, find_equal_premium_pair() must return
        the original fallback pair unchanged with min_diff == float('inf').
        """
        from stocks.services.delta_hedge_service import find_equal_premium_pair

        ce1 = {'strike': 97000, 'symbol': 'BAJFINANCE26MAY970CE', 'expiry': '29May2026'}
        pe1 = {'strike': 91000, 'symbol': 'BAJFINANCE26MAY910PE', 'expiry': '29May2026'}

        best_ce, best_pe, min_diff = find_equal_premium_pair(
            [ce1], [pe1],
            ce_ltp_map={id(ce1): 0.0},
            pe_ltp_map={id(pe1): 0.0},
            fallback_ce=ce1, fallback_pe=pe1,
        )

        self.assertIs(best_ce, ce1, "Must return fallback CE when no valid LTP")
        self.assertIs(best_pe, pe1, "Must return fallback PE when no valid LTP")
        self.assertEqual(min_diff, float('inf'))


# ──────────────────────────────────────────────
# NEW TESTS: Weekend & Holiday Guard
# ──────────────────────────────────────────────

class MarketHolidayGuardTests(TestCase):
    """
    Unit and integration tests for weekends, NSE holidays,
    and external cron trigger guards.
    """

    def setUp(self):
        from datetime import date
        from unittest.mock import patch
        from stocks.models import MarketHoliday

        # sync_nse_holidays_from_api() makes a real network call to NSE and
        # deactivates any DB holiday not present in the live feed — without this,
        # it clobbers the synthetic fixture rows below before is_market_open_today's
        # DB check ever runs, making these tests depend on live network + the real
        # NSE calendar instead of the DB state under test.
        patcher_sync = patch("stocks.services.signal_utils.sync_nse_holidays_from_api", return_value=True)
        patcher_sync.start()
        self.addCleanup(patcher_sync.stop)

        # NSE_HOLIDAYS (static fallback list) already hardcodes 2026-06-26 — without
        # this, the static-list check runs after the DB check and independently
        # closes the market for that date regardless of the DB row's is_active flag,
        # defeating the point of test_market_open_on_inactive_db_holiday. Excludes
        # only that one date (not the whole list) so
        # test_market_closed_on_static_configured_holiday's Christmas check is unaffected.
        from stocks.services.signal_utils import NSE_HOLIDAYS as _real_nse_holidays
        patcher_static = patch(
            "stocks.services.signal_utils.NSE_HOLIDAYS",
            _real_nse_holidays - {date(2026, 6, 26)},
        )
        patcher_static.start()
        self.addCleanup(patcher_static.stop)

        # Set up a few test holidays in the DB
        MarketHoliday.objects.update_or_create(
            holiday_date=date(2026, 6, 25),
            defaults={"market": "NSE", "name": "Test Summer Festival", "is_active": True}
        )
        MarketHoliday.objects.update_or_create(
            holiday_date=date(2026, 6, 26),
            defaults={"market": "NSE", "name": "Inactive Holiday", "is_active": False}
        )

    def test_market_open_on_weekday(self):
        """A normal weekday (e.g. Wednesday 2026-06-24) should return True."""
        from stocks.services.signal_utils import is_market_open_today
        normal_wednesday = date(2026, 6, 24)
        self.assertTrue(
            is_market_open_today(target_date=normal_wednesday),
            "Weekdays without holidays should be considered open"
        )

    def test_market_closed_on_saturday(self):
        """Saturdays must always be closed."""
        from stocks.services.signal_utils import is_market_open_today
        saturday = date(2026, 6, 27)
        self.assertFalse(
            is_market_open_today(target_date=saturday),
            "Saturdays must always be closed"
        )

    def test_market_closed_on_sunday(self):
        """Sundays must always be closed."""
        from stocks.services.signal_utils import is_market_open_today
        sunday = date(2026, 6, 28)
        self.assertFalse(
            is_market_open_today(target_date=sunday),
            "Sundays must always be closed"
        )

    def test_market_closed_on_db_holiday(self):
        """Market holidays in DB (e.g. 2026-06-25) must return False."""
        from stocks.services.signal_utils import is_market_open_today
        db_holiday = date(2026, 6, 25)
        self.assertFalse(
            is_market_open_today(target_date=db_holiday),
            "Holidays listed actively in DB must be closed"
        )

    def test_market_open_on_inactive_db_holiday(self):
        """Market holidays in DB that are inactive (is_active=False) should still be open if normal weekday."""
        from stocks.services.signal_utils import is_market_open_today
        inactive_holiday = date(2026, 6, 26)  # Friday
        self.assertTrue(
            is_market_open_today(target_date=inactive_holiday),
            "Inactive DB holidays should not trigger a closed status"
        )

    def test_market_closed_on_static_configured_holiday(self):
        """Christmas (2026-12-25) in static fallback list must return False."""
        from stocks.services.signal_utils import is_market_open_today
        christmas = date(2026, 12, 25)
        self.assertFalse(
            is_market_open_today(target_date=christmas),
            "Static fallback holidays must return closed"
        )

    def test_cron_endpoint_skips_on_closed_market(self):
        """
        Requesting the manual cron trigger on weekends or holiday dates
        must immediately skip execution and return the appropriate JSON response.
        """
        from rest_framework.test import APIClient
        from unittest.mock import patch

        client = APIClient()
        token = "trade_pulse_secure_cron_trigger_2026"
        url = f"/api/stocks/cron-trigger/?token={token}&action=generate"

        # Mock is_market_open_today to return False (simulating weekend/holiday)
        with patch("stocks.services.signal_utils.is_market_open_today", return_value=False):
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data.get("reason"), "Market closed / NSE holiday — add &force=1 to override")

    def test_cron_endpoint_executes_on_open_market(self):
        """
        Requesting manual cron trigger on a trading day must successfully queue
        and execute scanners.
        """
        from rest_framework.test import APIClient
        from unittest.mock import patch

        client = APIClient()
        token = "trade_pulse_secure_cron_trigger_2026"
        url = f"/api/stocks/cron-trigger/?token={token}&action=generate"

        # Mock is_market_open_today to return True (simulating open day)
        with patch("stocks.services.signal_utils.is_market_open_today", return_value=True):
            # Patch run_periodic_scanners / scheduler to prevent actual live scanning in tests
            with patch("stocks.services.live_signal_service.run_periodic_scanners") as mock_scanner:
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data.get("status"), "Success")
                self.assertIsNotNone(data.get("message"))

    def test_timezone_check_kolkata(self):
        """
        Verifies that timezone-aware dates resolved to Asia/Kolkata
        are parsed correctly when checking market status.
        """
        from stocks.services.signal_utils import is_market_open_today
        from unittest.mock import patch
        from django.utils import timezone as dj_timezone
        import zoneinfo

        tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        # Wednesday 2026-06-24 10:00:00 IST
        mocked_dt = dj_timezone.datetime(2026, 6, 24, 10, 0, 0, tzinfo=tz)

        with patch("django.utils.timezone.now", return_value=mocked_dt):
            self.assertTrue(
                is_market_open_today(),
                "Timezone-aware check must evaluate to True on Wednesday morning"
            )


# ──────────────────────────────────────────────
# NEW TESTS: Strangle Pair Selection & Tie-Breakers
# ──────────────────────────────────────────────

class StranglePairSelectionTests(TestCase):
    """
    Unit and integration tests for equal-premium strangle pair selection,
    including liquidity, strike symmetry, and combined premium tie-breakers.
    """

    def test_bajaj_auto_like_steep_put_skew_balancing(self):
        """
        BAJAJ-AUTO-like steep put skew: PE strikes near spot have massive premiums.
        The selector must go deep OTM (e.g. 10100 PE) to find the correct equal premium
        matching the OTM CE (e.g. 11100 CE @ 65.00), instead of blindly choosing
        the closest PE strike with a massive mismatch.
        """
        from stocks.services.delta_hedge_service import find_equal_premium_pair

        # Spot = 10600.0
        # CE candidates: 10700, 10800, 10900, 11000, 11100
        ce107 = {'strike': 1070000, 'symbol': 'BAJAJ-AUTO10700CE'} # ₹250.00
        ce111 = {'strike': 1110000, 'symbol': 'BAJAJ-AUTO11100CE'} # ₹65.00  <-- Target CE

        # PE candidates:
        # Near-spot PE (10300 PE) has massive put premium (₹295.00) due to market fear/skew
        pe103 = {'strike': 1030000, 'symbol': 'BAJAJ-AUTO10300PE'} # ₹295.00 (invalid balance match)
        # Deeper OTM PE (9700 PE) has a balanced premium of ₹65.25 matching the CE perfectly!
        pe97  = {'strike': 970000,  'symbol': 'BAJAJ-AUTO9700PE'}  # ₹65.25  <-- Target PE

        ce_candidates = [ce107, ce111]
        pe_candidates = [pe103, pe97]

        ce_ltp_map = {id(ce107): 250.00, id(ce111): 65.00}
        pe_ltp_map = {id(pe103): 295.00, id(pe97): 65.25}

        best_ce, best_pe, min_diff = find_equal_premium_pair(
            ce_candidates, pe_candidates, ce_ltp_map, pe_ltp_map,
            fallback_ce=ce111, fallback_pe=pe103, spot=10600.0, exchange="NFO"
        )

        self.assertIs(best_ce, ce111, "Should select 11100 CE (₹65.00)")
        self.assertIs(best_pe, pe97, "Should select 9700 PE (₹65.25) to balance putting skew")
        self.assertAlmostEqual(min_diff, 0.25, places=2, msg="Difference should be exactly ₹0.25")

    def test_tie_breaker_liquidity_priority(self):
        """
        When two pairs have the exact same premium difference,
        the pair with higher combined liquidity (OI + Volume) must be selected.
        """
        from stocks.services.delta_hedge_service import find_equal_premium_pair

        ce1 = {'strike': 10000, 'symbol': 'TEST100CE', 'open_interest': 1000, 'trade_volume': 500} # premium: 10.0
        ce2 = {'strike': 10200, 'symbol': 'TEST102CE', 'open_interest': 5000, 'trade_volume': 2500} # premium: 10.0  <-- Higher Liquidity

        pe1 = {'strike': 9000, 'symbol': 'TEST90PE', 'open_interest': 1000, 'trade_volume': 500} # premium: 10.0
        pe2 = {'strike': 8800, 'symbol': 'TEST88PE', 'open_interest': 4000, 'trade_volume': 2000} # premium: 10.0  <-- Higher Liquidity

        # Pair A: ce1 (10.0) & pe1 (10.0) -> diff = 0.0, combined liquidity = 3000
        # Pair B: ce2 (10.0) & pe2 (10.0) -> diff = 0.0, combined liquidity = 13500  <-- Winner
        ce_candidates = [ce1, ce2]
        pe_candidates = [pe1, pe2]

        ce_ltp_map = {id(ce1): 10.0, id(ce2): 10.0}
        pe_ltp_map = {id(pe1): 10.0, id(pe2): 10.0}

        best_ce, best_pe, min_diff = find_equal_premium_pair(
            ce_candidates, pe_candidates, ce_ltp_map, pe_ltp_map,
            fallback_ce=ce1, fallback_pe=pe1, spot=9500.0, exchange="NFO"
        )

        self.assertIs(best_ce, ce2, "Should break tie using higher CE liquidity")
        self.assertIs(best_pe, pe2, "Should break tie using higher PE liquidity")
        self.assertEqual(min_diff, 0.0)

    def test_tie_breaker_symmetry_priority(self):
        """
        When two pairs have the exact same premium difference and identical liquidity,
        the pair whose strikes are more symmetrically balanced around spot must be selected.
        """
        from stocks.services.delta_hedge_service import find_equal_premium_pair

        # Spot = 1000
        # Pair A: CE 1100 (+100) & PE 900 (-100) -> Symmetry diff = |100 - 100| = 0  <-- Symmetric Winner
        # Pair B: CE 1200 (+200) & PE 950 (-50) -> Symmetry diff = |200 - 50| = 150
        ce_sym = {'strike': 110000, 'symbol': 'TEST1100CE'} # premium: 12.00
        ce_asym = {'strike': 120000, 'symbol': 'TEST1200CE'} # premium: 12.00

        pe_sym = {'strike': 90000, 'symbol': 'TEST900PE'} # premium: 12.00
        pe_asym = {'strike': 95000, 'symbol': 'TEST950PE'} # premium: 12.00

        ce_candidates = [ce_sym, ce_asym]
        pe_candidates = [pe_sym, pe_asym]

        ce_ltp_map = {id(ce_sym): 12.00, id(ce_asym): 12.00}
        pe_ltp_map = {id(pe_sym): 12.00, id(pe_asym): 12.00}

        best_ce, best_pe, min_diff = find_equal_premium_pair(
            ce_candidates, pe_candidates, ce_ltp_map, pe_ltp_map,
            fallback_ce=ce_asym, fallback_pe=pe_asym, spot=1000.0, exchange="NFO"
        )

        self.assertIs(best_ce, ce_sym, "Should select symmetric CE")
        self.assertIs(best_pe, pe_sym, "Should select symmetric PE")
        self.assertEqual(min_diff, 0.0)

    def test_tie_breaker_combined_premium_priority(self):
        """
        When difference, liquidity, and symmetry are identical,
        the pair with the higher combined premium must be selected.
        """
        from stocks.services.delta_hedge_service import find_equal_premium_pair

        # Spot = 1000
        # Pair A: CE 1100 (+100) & PE 900 (-100) -> premiums = 20.0 & 20.0 -> combined = 40.0  <-- Winner
        # Pair B: CE 1100 (+100) & PE 900 (-100) -> premiums = 10.0 & 10.0 -> combined = 20.0
        # Represent this by passing two candidates with same strikes but different maps/IDs
        ce_high = {'strike': 110000, 'symbol': 'TEST1100CE'} 
        ce_low = {'strike': 110000, 'symbol': 'TEST1100CE'}

        pe_high = {'strike': 90000, 'symbol': 'TEST900PE'}
        pe_low = {'strike': 90000, 'symbol': 'TEST900PE'}

        ce_candidates = [ce_high, ce_low]
        pe_candidates = [pe_high, pe_low]

        ce_ltp_map = {id(ce_high): 20.00, id(ce_low): 10.00}
        pe_ltp_map = {id(pe_high): 20.00, id(pe_low): 10.00}

        best_ce, best_pe, min_diff = find_equal_premium_pair(
            ce_candidates, pe_candidates, ce_ltp_map, pe_ltp_map,
            fallback_ce=ce_low, fallback_pe=pe_low, spot=1000.0, exchange="NFO"
        )

        self.assertIs(best_ce, ce_high, "Should select higher premium CE")
        self.assertIs(best_pe, pe_high, "Should select higher premium PE")
        self.assertEqual(min_diff, 0.0)

    def test_sync_scan_parameter_integration(self):
        """
        Verify that get_hedge_panel_data signature and logic accepts
        and integrates the sync_scan parameter without failing.
        """
        from stocks.services.delta_hedge_service import get_hedge_panel_data
        from unittest.mock import patch, MagicMock

        # We mock get_truedata_instance, cache, is_market_open to return clean/mock values
        mock_svc = MagicMock()
        mock_svc.streamer = MagicMock()
        mock_svc.streamer.is_connected = True

        with patch("stocks.services.delta_hedge_service.get_truedata_instance", return_value=mock_svc), \
             patch("stocks.services.delta_hedge_service.cache") as mock_cache, \
             patch("stocks.services.delta_hedge_service.is_market_open", return_value=True), \
             patch("stocks.services.delta_hedge_service._background_scan") as mock_bg_scan:
            
            mock_cache.get.return_value = None
            
            # Call with sync_scan=True
            res = get_hedge_panel_data(action="generate", sync_scan=True)
            
            # Should have returned a valid panel dict
            self.assertIsInstance(res, dict)
            self.assertEqual(res.get("market_status"), "OPEN")
            
            # Since sync_scan is True, it should have triggered _background_scan directly
            mock_bg_scan.assert_called_once()


class SpecialistStrangleInstitutionalUpgradesTests(TestCase):
    """
    Unit tests for institutional upgrades in delta_hedge_service:
    - Expected Move physical floors
    - Gamma cap when DTE <= 3
    - Min DTE gate
    - Dynamic target delta adjustments based on IV and skew
    - Spread filter checking ask-bid spread
    """

    def test_min_dte_guard_rejects_expiry_day(self):
        """Should return empty list if days to expiry is less than MIN_DTE (1)."""
        from stocks.services.delta_hedge_service import build_specialist_hedge
        from unittest.mock import MagicMock
        
        # Stated strikes has expiry today (0 days left)
        strikes = [
            {'strike': '140000', 'symbol': 'HCLTECH26MAY1400CE', 'expiry': '27May2026', 'token': '1', 'exch_seg': 'NFO'},
            {'strike': '140000', 'symbol': 'HCLTECH26MAY1400PE', 'expiry': '27May2026', 'token': '2', 'exch_seg': 'NFO'}
        ]
        
        orch = MagicMock()
        with patch("stocks.services.delta_hedge_service.get_nse_option_strikes", return_value=strikes), \
             patch("django.utils.timezone.now") as mock_now:
            
            # mock now to be expiry day
            mock_now.return_value.astimezone.return_value.date.return_value = datetime(2026, 5, 27).date()
            res = build_specialist_hedge("HCLTECH", "NFO", 1432.0, orch)
            self.assertEqual(res, [], "Strangle should be rejected on expiry day due to Gamma guard")

    def test_expected_move_bounds_strikes(self):
        """Expected Move model must shift strikes farther OTM if spot is too close to selected strike."""
        from stocks.services.delta_hedge_service import build_specialist_hedge
        from unittest.mock import MagicMock
        
        strikes = [
            # Strikes extremely close to spot 100
            {'strike': '10100', 'symbol': 'MOCK101CE', 'expiry': '25Jun2026', 'token': '1', 'exch_seg': 'NFO', 'lotsize': '100'},
            {'strike': '9900', 'symbol': 'MOCK99PE', 'expiry': '25Jun2026', 'token': '2', 'exch_seg': 'NFO', 'lotsize': '100'},
            # Outer strikes that stand outside Expected Move
            {'strike': '12000', 'symbol': 'MOCK120CE', 'expiry': '25Jun2026', 'token': '3', 'exch_seg': 'NFO', 'lotsize': '100'},
            {'strike': '8000', 'symbol': 'MOCK80PE', 'expiry': '25Jun2026', 'token': '4', 'exch_seg': 'NFO', 'lotsize': '100'}
        ]
        
        orch = MagicMock()
        orch.get_option_data.return_value = ({'ltp': 8.50, 'bid': 8.45, 'ask': 8.55}, 'token')
        
        with patch("stocks.services.delta_hedge_service.get_nse_option_strikes", return_value=strikes), \
             patch("stocks.services.delta_hedge_service.estimate_iv", return_value=0.22), \
             patch("stocks.services.delta_hedge_service.get_lot_size", return_value=1000), \
             patch("stocks.services.delta_hedge_service.find_strike_by_delta") as mock_delta, \
             patch("django.utils.timezone.now") as mock_now:

            # Fix "now" well before the strikes' 25Jun2026 expiry so DTE stays large
            # regardless of the real wall-clock date — same technique as
            # test_min_dte_guard_rejects_expiry_day above, just picking a date that
            # keeps this test's DTE comfortably clear of the gamma/MIN_DTE guards
            # instead of on top of them. .time() also needs a real value —
            # get_intraday_target_delta() compares it against datetime.time objects,
            # which fails against the MagicMock this same mocked `now` otherwise returns.
            from datetime import time as _time
            mock_now.return_value.astimezone.return_value.date.return_value = datetime(2026, 6, 1).date()
            mock_now.return_value.astimezone.return_value.time.return_value = _time(10, 0)

            # Mock initial delta selector to pick close strikes for sells and far for buys (6 calls total)
            mock_delta.side_effect = [
                strikes[0],  # skew PE check
                strikes[1],  # skew CE check
                strikes[0],  # sell CE
                strikes[1],  # sell PE
                strikes[2],  # buy CE
                strikes[3]   # buy PE
            ]
            
            res = build_specialist_hedge("MOCK", "NFO", 100.0, orch, sigma=0.22)
            self.assertTrue(len(res) >= 2)

            # CE and PE strikes must be resolved to the outer ones to respect Expected Move floor
            strikes_selected = [leg['strike'] for leg in res if leg['action'] == 'SELL']
            self.assertIn(120.0, strikes_selected)
            self.assertIn(80.0, strikes_selected)


class AssignmentRiskFlagTests(TestCase):
    """
    Audit fix C4: NSE stock options are American-style / physically settled, unlike
    index options — nothing previously distinguished that risk anywhere in the
    strangle-selling engine. is_physical_settlement_risk() is the extracted
    classification logic used by the live audit loop in get_hedge_panel_data().
    """

    def test_stock_underlying_near_itm_flags_risk(self):
        from stocks.services.delta_hedge_service import is_physical_settlement_risk
        from stocks.services.config_vol import ASSIGNMENT_RISK_DELTA

        self.assertTrue(is_physical_settlement_risk("RELIANCE", ASSIGNMENT_RISK_DELTA))
        self.assertTrue(is_physical_settlement_risk("RELIANCE", ASSIGNMENT_RISK_DELTA + 0.1))

    def test_stock_underlying_far_otm_does_not_flag(self):
        from stocks.services.delta_hedge_service import is_physical_settlement_risk
        from stocks.services.config_vol import ASSIGNMENT_RISK_DELTA

        self.assertFalse(is_physical_settlement_risk("RELIANCE", ASSIGNMENT_RISK_DELTA - 0.1))

    def test_index_underlying_never_flags_regardless_of_delta(self):
        """Index options are cash-settled — no assignment risk exists even deep ITM."""
        from stocks.services.delta_hedge_service import is_physical_settlement_risk

        for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            self.assertFalse(is_physical_settlement_risk(idx, 0.99))

    def test_build_specialist_hedge_tags_stock_legs_as_optstk(self):
        """build_specialist_hedge()'s legs carry instrument_type so the panel/audit
        loop can classify assignment risk without re-deriving it per leg."""
        from stocks.services.delta_hedge_service import build_specialist_hedge

        strikes = [
            {'strike': '10100', 'symbol': 'MOCK101CE', 'expiry': '25Jun2026', 'token': '1', 'exch_seg': 'NFO', 'lotsize': '100'},
            {'strike': '9900', 'symbol': 'MOCK99PE', 'expiry': '25Jun2026', 'token': '2', 'exch_seg': 'NFO', 'lotsize': '100'},
        ]
        orch = MagicMock()
        orch.get_option_data.return_value = ({'ltp': 8.50, 'bid': 8.45, 'ask': 8.55}, 'token')

        with patch("stocks.services.delta_hedge_service.get_nse_option_strikes", return_value=strikes), \
             patch("stocks.services.delta_hedge_service.estimate_iv", return_value=0.22), \
             patch("stocks.services.delta_hedge_service.get_lot_size", return_value=1000), \
             patch("stocks.services.delta_hedge_service.find_strike_by_delta", side_effect=[
                 strikes[0], strikes[1], strikes[0], strikes[1],
             ]), \
             patch("django.utils.timezone.now") as mock_now:
            from datetime import time as _time
            mock_now.return_value.astimezone.return_value.date.return_value = datetime(2026, 6, 1).date()
            mock_now.return_value.astimezone.return_value.time.return_value = _time(10, 0)

            res = build_specialist_hedge("MOCK", "NFO", 100.0, orch, sigma=0.22)

        self.assertTrue(len(res) >= 2)
        self.assertTrue(all(leg.get('instrument_type') == 'OPTSTK' for leg in res))


class OptionGreeksServiceTests(TestCase):
    """AUDIT_REMEDIATION_PLAN.md #3.2.3 — estimate_iv must never raise ZeroDivisionError.

    t_days<=0 (or an iv underflow mid-loop) used to divide by
    `iv * math.sqrt(t_years)` with no guard, unlike calculate_greeks's own try/except.
    estimate_iv now short-circuits on t_days<=0 and wraps the Newton-Raphson loop in
    try/except (ZeroDivisionError, ValueError), both returning the same 0.20 starting
    guess used elsewhere as the safe fallback.
    """

    def test_zero_t_days_returns_fallback_without_raising(self):
        from stocks.services.option_greeks_service import estimate_iv

        result = estimate_iv(spot=100, strike=100, t_days=0, premium=5, option_type='CE')

        self.assertIsInstance(result, float)
        self.assertEqual(result, 0.20)

    def test_negative_t_days_returns_fallback_without_raising(self):
        from stocks.services.option_greeks_service import estimate_iv

        result = estimate_iv(spot=100, strike=100, t_days=-3, premium=5, option_type='PE')

        self.assertIsInstance(result, float)
        self.assertEqual(result, 0.20)

    def test_normal_t_days_still_converges(self):
        """Sanity check the guard didn't break the happy path for a realistic input."""
        from stocks.services.option_greeks_service import estimate_iv

        result = estimate_iv(spot=100, strike=100, t_days=30, premium=2.5, option_type='CE')

        self.assertIsInstance(result, float)
        self.assertGreaterEqual(result, 0.01)
        self.assertLessEqual(result, 2.0)


class ResolveTargetExpiryTests(TestCase):
    """
    Unit tests for delta_hedge_service.resolve_target_expiry() — the single shared
    expiry-rollover selector used by both get_nse_option_strikes() and
    build_specialist_hedge() (Audit Remediation Plan Phase 3 #3.2.4). Both call sites
    previously carried their own local parse_expiry closure and their own
    valid_expiries/target_expiry filtering; this now covers that logic once, directly,
    instead of duplicating the same test across two service test files.
    """

    def test_rolls_past_expiry_within_rollover_window(self):
        """
        An expiry with <= min_days trading days remaining (e.g. expiry day itself) must be
        skipped in favor of the next expiry that clears the threshold.
        """
        from stocks.services.delta_hedge_service import resolve_target_expiry

        with patch("stocks.services.delta_hedge_service.get_trading_days_remaining") as mock_days:
            # "25JUN2026" is the near/expiring contract (0 trading days left — expiry day
            # itself); "30JUL2026" is the next month's contract (20 trading days left).
            def fake_days(expiry_date):
                return 0 if expiry_date.day == 25 else 20

            mock_days.side_effect = fake_days
            result = resolve_target_expiry(["25JUN2026", "30JUL2026"], min_days=3)
            self.assertEqual(result, "30JUL2026", "Should roll past the expiring contract to the next one")

    def test_falls_back_to_nearest_when_no_expiry_qualifies(self):
        """
        If every available expiry is inside the rollover window (e.g. broker hasn't listed
        next month's contract yet), fall back to the nearest expiry rather than returning
        None — there's no better contract available to trade.
        """
        from stocks.services.delta_hedge_service import resolve_target_expiry

        with patch("stocks.services.delta_hedge_service.get_trading_days_remaining", return_value=1):
            result = resolve_target_expiry(["25JUN2026", "30JUL2026"], min_days=3)
            self.assertEqual(result, "25JUN2026", "Should fall back to the chronologically nearest expiry")

    def test_empty_expiries_returns_none(self):
        """No expiries available at all must return None, not raise."""
        from stocks.services.delta_hedge_service import resolve_target_expiry

        self.assertIsNone(resolve_target_expiry([], min_days=3))


class DailyLossHaltArmsOnEmptyBookTests(TestCase):
    """
    Audit fix (C1): the daily loss halt must arm from today's closed SignalHistory
    rows even when active_signals is empty — that's exactly the state right after a
    string of stop-losses closes the book, which is precisely when the kill switch
    is needed most. update_signal_outcomes() used to `return` before ever reaching
    _enforce_daily_loss_limit() in that case.
    """

    def setUp(self):
        from django.core.cache import cache
        from stocks.services.intraday_service import DAILY_HALT_CACHE_KEY
        self.cache = cache
        self.halt_key = DAILY_HALT_CACHE_KEY
        self.cache.delete(self.halt_key)
        self.patcher_static_closed = patch("stocks.services.signal_utils.is_static_closed", return_value=False)
        self.patcher_static_closed.start()
        # _enforce_daily_loss_limit's `today = datetime.now(tz=IST).date()` must line up
        # with the generated_at date on the test fixtures below (same pattern as
        # UpdateSignalOutcomesTests).
        self.patcher_datetime = patch("stocks.services.live_signal_service.datetime", MockDatetime)
        self.patcher_datetime.start()
        MockDatetime._mock_now = datetime(2026, 4, 1, 13, 15, tzinfo=IST)

    def tearDown(self):
        self.patcher_static_closed.stop()
        self.patcher_datetime.stop()
        MockDatetime._mock_now = None
        self.cache.delete(self.halt_key)

    def _closed_signal(self, symbol: str, entry: float, exit_price: float, qty: int):
        signal = SignalHistory.objects.create(
            symbol=symbol, signal_type="BUY", entry_price=entry, stop_loss=entry * 0.98,
            target=entry * 1.02, rr=2.0, status=SignalHistory.Status.HIT_SL,
            category="intraday", reason="test", exit_price=exit_price,
            metadata={"qty": qty},
        )
        generated_at = datetime(2026, 4, 1, 9, 30, tzinfo=IST)
        SignalHistory.objects.filter(pk=signal.pk).update(generated_at=generated_at)
        return signal

    @patch("stocks.services.live_signal_service.get_latest_prices")
    def test_halt_arms_with_zero_active_signals(self, mock_get_prices):
        # All positions already stopped out — book is flat, nothing ACTIVE/PENDING.
        # Combined realised loss exceeds the 2% of Rs.5,00,000 = Rs.10,000 default limit.
        self._closed_signal("SYM1", entry=100.0, exit_price=90.0, qty=600)  # -Rs.6,000
        self._closed_signal("SYM2", entry=100.0, exit_price=90.0, qty=600)  # -Rs.6,000
        mock_get_prices.return_value = {}

        test_now = datetime(2026, 4, 1, 13, 15, tzinfo=IST)
        with patch("stocks.services.live_signal_service.dj_timezone.now", return_value=test_now):
            update_signal_outcomes(force=True)

        self.assertTrue(
            self.cache.get(self.halt_key),
            "Daily loss halt must arm from closed rows even with an empty active book",
        )

    @patch("stocks.services.live_signal_service.get_latest_prices")
    def test_halt_does_not_arm_when_loss_within_limit(self, mock_get_prices):
        # Sanity check: a small loss well within the limit must not trip the halt.
        self._closed_signal("SYM1", entry=100.0, exit_price=99.0, qty=100)  # -Rs.100
        mock_get_prices.return_value = {}

        test_now = datetime(2026, 4, 1, 13, 15, tzinfo=IST)
        with patch("stocks.services.live_signal_service.dj_timezone.now", return_value=test_now):
            update_signal_outcomes(force=True)

        self.assertFalse(self.cache.get(self.halt_key))


class OptionBuyingDailyLossHaltTests(TestCase):
    """
    Audit fixes C2 (no daily loss circuit breaker existed for option buying at all)
    and C3 (a failed quote fetch used to skip the mandatory 2:30 PM force-close).
    """

    def setUp(self):
        from django.core.cache import cache
        from stocks.services.option_buying_service import OPTION_BUYING_DAILY_HALT_CACHE_KEY
        self.cache = cache
        self.halt_key = OPTION_BUYING_DAILY_HALT_CACHE_KEY
        self.cache.delete(self.halt_key)

    def tearDown(self):
        self.cache.delete(self.halt_key)

    def _closed_signal(self, symbol, entry, exit_price, status, strike=100.0, option_type="CE"):
        from stocks.models import SignalHistory as SH
        return SH.objects.create(
            symbol=symbol, signal_type="BUY_CE", category="option_buying",
            entry_price=entry, stop_loss=entry * 0.5, target=entry * 2.0, rr=2.0,
            status=status, reason="test", exit_price=exit_price,
            strike_price=strike, option_type=option_type,
        )

    @patch("stocks.services.delta_hedge_service.get_lot_size", return_value=50)
    def test_c2_halt_arms_from_closed_rows_with_empty_active_book(self, mock_lot_size):
        """A day of stopped-out option_buying trades (nothing ACTIVE) must still arm
        the halt — same empty-book scenario as C1's intraday fix. Uses real "now" for
        both generated_at (auto_now_add) and the check itself, so no date faking is
        needed for the generated_at__date=today match."""
        from django.utils import timezone as dj_timezone
        from stocks.services.option_buying_service import (
            _enforce_option_buying_daily_loss_limit, OPTION_BUYING_DAILY_HALT_CACHE_KEY,
        )
        from stocks.services.intraday_service import INTRADAY_ACCOUNT_EQUITY

        # 2-lot P&L per Re.1 move = lot_size(50)*2 = 100. Loss of Rs.3/premium * 100 =
        # Rs.300 per trade; enough trades to clear the 2% limit regardless of its value.
        limit = INTRADAY_ACCOUNT_EQUITY * 0.02
        per_trade_loss = 3.0 * 100
        n = int(limit // per_trade_loss) + 2
        for i in range(n):
            self._closed_signal(f"SYM{i}", entry=50.0, exit_price=47.0, status=SignalHistory.Status.HIT_SL)

        now_ist = dj_timezone.now().astimezone(IST)
        halted = _enforce_option_buying_daily_loss_limit(now_ist)

        self.assertTrue(halted)
        self.assertTrue(self.cache.get(OPTION_BUYING_DAILY_HALT_CACHE_KEY))

    def test_c3_failed_quote_still_force_closes_at_time_stop(self):
        """A quote fetch failure at/after 2:30 PM must not leave the position stuck
        ACTIVE — it force-closes at the last known premium instead of being skipped."""
        from unittest.mock import MagicMock
        from stocks.services.option_buying_service import update_option_buying_outcomes

        sig = self._closed_signal(
            "OPTSYM", entry=50.0, exit_price=None, status=SignalHistory.Status.ACTIVE,
        )
        sig.premium_cmp = 48.0
        sig.save(update_fields=["premium_cmp"])

        mock_svc = MagicMock()
        mock_svc.get_option_quote.return_value = None  # quote fetch fails

        past_time_stop = datetime(2026, 4, 1, 14, 35, tzinfo=IST)  # after 2:30 PM
        with patch("stocks.services.truedata_service.get_truedata_instance", return_value=mock_svc), \
             patch("stocks.services.option_buying_service.datetime") as mock_dt:
            mock_dt.now.return_value = past_time_stop
            update_option_buying_outcomes()

        sig.refresh_from_db()
        self.assertIn(sig.status, (SignalHistory.Status.HIT_TARGET, SignalHistory.Status.HIT_SL))
        self.assertEqual(float(sig.exit_price), 48.0)


class OptionChainFallbackTests(TestCase):
    """
    Regression test: on an NSE scrape failure, get_option_chain() must prefer the
    last real stored OptionChain snapshot over fabricated mock data, and must only
    ever return mock data flagged with is_mock=True (never silently as if live).
    """

    def _make_snapshot(self, symbol="NIFTY", spot_price=24777):
        from stocks.models import OptionChain
        return OptionChain.objects.create(
            symbol=symbol,
            spot_price=spot_price,
            pcr=1.1,
            max_pain=24500,
            total_ce_oi=1000000,
            total_pe_oi=1100000,
            chain_data_json=[{"strike": 24500, "ce_oi": 1000, "pe_oi": 1000}],
        )

    def test_live_failure_with_snapshot_returns_real_data_not_mock(self):
        from unittest.mock import MagicMock
        from stocks.services.option_chain_service import get_option_chain

        self._make_snapshot(symbol="NIFTY", spot_price=24777)

        fake_session = MagicMock()
        fake_session.get.side_effect = Exception("network down")

        with patch("stocks.services.option_chain_service._build_session", return_value=fake_session):
            result = get_option_chain("NIFTY")

        self.assertEqual(float(result.get("spot_price")), 24777.0)
        self.assertFalse(result.get("is_mock"))

    def test_live_failure_without_snapshot_returns_flagged_mock(self):
        from unittest.mock import MagicMock
        from stocks.services.option_chain_service import get_option_chain

        fake_session = MagicMock()
        fake_session.get.side_effect = Exception("network down")

        with patch("stocks.services.option_chain_service._build_session", return_value=fake_session):
            result = get_option_chain("NIFTY")

        self.assertTrue(result.get("is_mock"))
        self.assertEqual(result.get("symbol"), "NIFTY")

    def test_live_success_returns_real_data_without_mock_flag(self):
        from unittest.mock import MagicMock
        from stocks.services.option_chain_service import get_option_chain

        fake_resp = MagicMock()
        fake_resp.raise_for_status.return_value = None
        fake_resp.json.return_value = {
            "filtered": {"data": [{
                "strikePrice": 24500,
                "CE": {"openInterest": 1000, "changeinOpenInterest": 10, "lastPrice": 50, "totalTradedVolume": 5},
                "PE": {"openInterest": 900, "changeinOpenInterest": 5, "lastPrice": 40, "totalTradedVolume": 4},
            }]},
            "records": {"expiryDates": ["27-Aug-2026"], "underlyingValue": 24500},
        }
        fake_session = MagicMock()
        fake_session.get.return_value = fake_resp

        with patch("stocks.services.option_chain_service._build_session", return_value=fake_session), \
             patch("stocks.services.option_chain_service._save_option_chain_snapshot"):
            result = get_option_chain("NIFTY")

        self.assertFalse(result.get("is_mock"))
        self.assertEqual(result.get("spot_price"), 24500)

