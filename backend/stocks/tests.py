import os
from datetime import datetime, date, time
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

        Uses a fixed, safe mid-day IST moment rather than real wall-clock time — this
        test used to derive `today_start`/`today` by converting real "now" to IST, but
        Django's generated_at__date lookup evaluates in settings.TIME_ZONE (UTC), so
        the test was flaky for ~5.5h/day (IST 00:00-05:30, when the IST and UTC
        calendar dates disagree — e.g. IST 00:03 is still the previous UTC date).
        """
        # Fixed IST moment safely mid-day, so its IST date and UTC date agree.
        test_now = datetime(2026, 4, 1, 13, 0, tzinfo=IST)

        # Create first signal, with generated_at pinned to test_now instead of
        # auto_now_add's real "now".
        sig1 = SignalHistory.objects.create(
            symbol="BRITANNIA",
            entry_price=5324.0,
            stop_loss=0, target=0,
            category="specialist",
            status=SignalHistory.Status.PENDING,
            metadata={"legs": [], "confidence": 85.0}
        )
        SignalHistory.objects.filter(pk=sig1.pk).update(generated_at=test_now)

        today_start = test_now.replace(hour=0, minute=0, second=0, microsecond=0)

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
            generated_at__date=test_now.date()
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

        CRON_SECRET_TOKEN must be set for this endpoint to accept any token at all
        (audit fix: no hardcoded fallback) — this test's own token was the removed
        hardcoded default, so it stopped matching once that fix shipped; set it via
        the env var like a real deployment would, instead of relying on the old
        literal.
        """
        from rest_framework.test import APIClient
        from unittest.mock import patch

        client = APIClient()
        token = "test-cron-secret-token"
        url = f"/api/stocks/cron-trigger/?token={token}&action=generate"

        # Mock is_market_open_today to return False (simulating weekend/holiday)
        with patch.dict(os.environ, {"CRON_SECRET_TOKEN": token}), \
             patch("stocks.services.signal_utils.is_market_open_today", return_value=False):
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data.get("reason"), "Market closed / NSE holiday — add &force=1 to override")

    def test_cron_endpoint_executes_on_open_market(self):
        """
        Requesting manual cron trigger on a trading day must successfully queue
        and execute scanners. See test_cron_endpoint_skips_on_closed_market's
        docstring for why CRON_SECRET_TOKEN must be set explicitly here.
        """
        from rest_framework.test import APIClient
        from unittest.mock import patch

        client = APIClient()
        token = "test-cron-secret-token"
        url = f"/api/stocks/cron-trigger/?token={token}&action=generate"

        # Mock is_market_open_today to return True (simulating open day)
        with patch.dict(os.environ, {"CRON_SECRET_TOKEN": token}), \
             patch("stocks.services.signal_utils.is_market_open_today", return_value=True):
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

        Pins `timezone.now()` to a fixed mid-session IST moment — the sync_scan path
        only actually fires _background_scan when now_ist >= ENTRY_WINDOW_START
        (10:45 AM), so this test was flaky depending on real wall-clock time whenever
        the suite happened to run outside that window (e.g. late night IST).
        """
        from stocks.services.delta_hedge_service import get_hedge_panel_data
        from unittest.mock import patch, MagicMock

        # We mock get_truedata_instance, cache, is_market_open to return clean/mock values
        mock_svc = MagicMock()
        mock_svc.streamer = MagicMock()
        mock_svc.streamer.is_connected = True

        test_now = datetime(2026, 4, 1, 13, 0, tzinfo=IST)  # safely inside ENTRY_WINDOW

        with patch("stocks.services.delta_hedge_service.get_truedata_instance", return_value=mock_svc), \
             patch("stocks.services.delta_hedge_service.cache") as mock_cache, \
             patch("stocks.services.delta_hedge_service.is_market_open", return_value=True), \
             patch("stocks.services.delta_hedge_service.timezone") as mock_timezone, \
             patch("stocks.services.delta_hedge_service._background_scan") as mock_bg_scan:

            mock_cache.get.return_value = None
            mock_timezone.now.return_value = test_now

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


class SpecialistPortfolioConstraintTests(TestCase):
    """
    Audit fix H3: the strangle scanner had no correlation or sector concentration
    control — apply_portfolio_constraints()/build_correlation_clusters() were already
    proven in intraday_service.py/pro_system_service.py but never imported here. Now
    wired into _background_scan() via a dedicated concentration-cap profile
    (_specialist_portfolio_profile(), derived from INTRADAY's caps via
    with_overrides() rather than a whole new EngineProfile, since most of that
    dataclass's fields — factor weights, sizing mode, cost model — don't apply to a
    premium-selling scanner).
    """

    def test_specialist_profile_has_sane_concentration_caps(self):
        from stocks.services.delta_hedge_service import _specialist_portfolio_profile

        profile = _specialist_portfolio_profile()
        self.assertEqual(profile.name, "specialist")
        self.assertGreaterEqual(profile.max_per_sector, 1)
        self.assertGreaterEqual(profile.max_per_cluster, 1)
        self.assertGreater(profile.max_per_promoter_group_pct, 0)
        self.assertEqual(profile.corr_lookback_days, 30)

    def test_sector_cap_rejects_excess_same_sector_candidates(self):
        """The same apply_portfolio_constraints() machinery intraday_service.py
        already relies on must reject candidates once the specialist profile's
        sector cap is hit."""
        from stocks.services.delta_hedge_service import _specialist_portfolio_profile
        from stocks.services.shared.portfolio_risk import apply_portfolio_constraints

        profile = _specialist_portfolio_profile()  # max_per_sector=3 by default
        candidates = [
            {"symbol": f"BANK{i}", "confidence": 90 - i} for i in range(5)
        ]
        sectors = {c["symbol"]: "FINANCIAL SERVICES" for c in candidates}

        accepted, rejected = apply_portfolio_constraints(
            candidates, [], sectors, {}, max_positions=10, profile=profile,
        )

        self.assertEqual(len(accepted), profile.max_per_sector)
        self.assertEqual(len(rejected), len(candidates) - profile.max_per_sector)
        self.assertTrue(all(r["reject_reason"].startswith("SECTOR_CAP") for r in rejected))

    def test_open_positions_count_toward_sector_cap(self):
        """Already-open specialist positions in a sector must count against new
        candidates in the same sector, not just other candidates in this scan."""
        from stocks.services.delta_hedge_service import _specialist_portfolio_profile
        from stocks.services.shared.portfolio_risk import apply_portfolio_constraints

        profile = _specialist_portfolio_profile()  # max_per_sector=3
        open_positions = [{"symbol": f"OPEN{i}"} for i in range(profile.max_per_sector)]
        candidates = [{"symbol": "NEWCAND", "confidence": 80}]
        sectors = {**{c["symbol"]: "IT" for c in candidates},
                   **{p["symbol"]: "IT" for p in open_positions}}

        accepted, rejected = apply_portfolio_constraints(
            candidates, open_positions, sectors, {}, max_positions=10, profile=profile,
        )

        self.assertEqual(len(accepted), 0)
        self.assertEqual(rejected[0]["reject_reason"], "SECTOR_CAP:IT")

    def test_background_scan_calls_constraint_filter_before_creating_signals(self):
        """Integration check: _background_scan must run candidates through the
        concentration-cap filter before build_specialist_hedge is ever called for
        a rejected candidate."""
        from stocks.services.delta_hedge_service import _background_scan

        with patch("stocks.services.delta_hedge_service.get_orchestrator") as mock_get_orch, \
             patch("stocks.services.delta_hedge_service.get_truedata_instance") as mock_get_svc, \
             patch("stocks.services.delta_hedge_service.NIFTY_50_STOCKS", return_value=["SYMA", "SYMB"]), \
             patch("stocks.services.delta_hedge_service.ENTRY_WINDOW_START", time(0, 0)), \
             patch("stocks.services.delta_hedge_service.ENTRY_WINDOW_END", time(23, 59)), \
             patch("stocks.services.delta_hedge_service.get_symbol_market_state", return_value={
                 "is_within_va": True, "vah": 0, "val": 0, "confidence": 60,
                 "current_price": 500.0, "metrics": {},
             }), \
             patch("stocks.services.shared.portfolio_risk.apply_portfolio_constraints") as mock_apc, \
             patch("stocks.services.delta_hedge_service.build_specialist_hedge") as mock_build:
            mock_orch = MagicMock()
            mock_orch.get_price.return_value = {"ltp": 500.0, "high": 510.0, "low": 490.0}
            mock_orch.get_prices_bulk.return_value = {}
            mock_get_orch.return_value = mock_orch
            mock_get_svc.return_value = MagicMock()
            # Both candidates rejected by the concentration filter -> build_specialist_hedge
            # (and therefore signal creation) must never be reached for either.
            mock_apc.return_value = ([], [{"symbol": "SYMA"}, {"symbol": "SYMB"}])

            _background_scan(tracked=set())

            mock_apc.assert_called_once()
            mock_build.assert_not_called()


class StaleQuoteSuspectTests(TestCase):
    """
    Audit fix M6: process_legs() only checked "is the LTP non-zero" — on a
    circuit-frozen underlying, a stale last-print looks like a live, actionable
    price to the exit/rebalance logic. Now flags a leg `is_theoretical` (already
    respected by the exit-check logic elsewhere) once its LTP is frozen across 3
    consecutive polls while the underlying spot has moved meaningfully.
    """

    def _leg(self):
        return {
            'symbol': 'MOCK', 'option_type': 'CE', 'strike': 25000.0,
            'expiry': '25JUN2026', 'status': 'WAITING', 'exchange': 'NSE',
            'action': 'SELL', 'lots': -1, 'lot_size': 50, 'live_iv': 0.20,
            'sell_price': 12.0, 'cmp': 12.0, 'original_sell_price': 12.0,
        }

    def test_frozen_ltp_with_moving_spot_flags_theoretical_after_three_polls(self):
        from stocks.services.delta_hedge_service import process_legs

        leg = self._leg()
        section = {'underlying': 'MOCK', 'legs': [], 'section_pnl': 0}
        panel_data = {'total_pnl': 0, 'sections': []}

        frozen_quote = {'NSE:token1': {'ltp': 12.0}}
        leg['token'] = 'token1'

        # 4 polls -> 3 consecutive same-cmp comparisons (the 1st poll only
        # establishes the baseline, nothing to compare yet).
        spots = [24500.0, 24600.0, 24700.0, 24800.0]  # each move >0.1% vs the previous
        result = [leg]
        for i, spot in enumerate(spots):
            result = process_legs(
                section, result, orch=MagicMock(), panel_data=panel_data,
                persist_updates=False, bulk_quotes=frozen_quote, underlying_spot=spot,
            )
            section['legs'] = []  # reset the "already closed" accumulation path between calls

        self.assertTrue(result[0].get('is_theoretical'),
                         "A frozen LTP across 3 polls with a moving spot must be flagged suspect")

    def test_moving_ltp_never_flagged(self):
        """Sanity check: a genuinely live (moving) quote must not be flagged."""
        from stocks.services.delta_hedge_service import process_legs

        leg = self._leg()
        leg['token'] = 'token1'
        section = {'underlying': 'MOCK', 'legs': [], 'section_pnl': 0}
        panel_data = {'total_pnl': 0, 'sections': []}

        result = [leg]
        for i, (spot, ltp) in enumerate([(24500.0, 12.0), (24600.0, 12.5), (24700.0, 13.0)]):
            result = process_legs(
                section, result, orch=MagicMock(), panel_data=panel_data,
                persist_updates=False, bulk_quotes={'NSE:token1': {'ltp': ltp}},
                underlying_spot=spot,
            )
            section['legs'] = []

        self.assertFalse(result[0].get('is_theoretical', False))


class StrangleRollCapTests(TestCase):
    """
    Audit fix H2: rebalance_delta_neutral_strangle() had no counter/cooldown — the
    same challenged leg could roll 5-6 times in a trending/whipsawing session, each
    roll realizing a small loss chasing price, invisible in the headline SL%. Now
    capped at MAX_ROLLS_PER_DAY per leg, tracked via roll_count/last_roll_date
    carried on the leg dict itself (not sig.metadata, which gets overwritten by the
    caller right after this function returns).
    """

    def _legs(self, ce_delta_leg_extra=None):
        ce = {
            'option_type': 'CE', 'strike': 25000.0, 'expiry': '25JUN2026',
            'status': 'WAITING', 'exchange': 'NSE', 'action': 'SELL', 'lots': -1,
            'live_iv': 0.20, 'cmp': 15.0, 'symbol': 'MOCK', 'lot_size': 50,
        }
        if ce_delta_leg_extra:
            ce.update(ce_delta_leg_extra)
        pe = {
            'option_type': 'PE', 'strike': 24000.0, 'expiry': '25JUN2026',
            'status': 'WAITING', 'exchange': 'NSE', 'action': 'SELL', 'lots': -1,
            'live_iv': 0.20, 'cmp': 10.0, 'symbol': 'MOCK', 'lot_size': 50,
        }
        return ce, pe

    def test_roll_cap_blocks_further_rolls_same_day(self):
        from stocks.services.delta_hedge_service import rebalance_delta_neutral_strangle
        from stocks.services.config_vol import MAX_ROLLS_PER_DAY

        today = datetime(2026, 6, 1).date()
        ce, pe = self._legs({'roll_count': MAX_ROLLS_PER_DAY, 'last_roll_date': today.isoformat()})
        updated_legs = [ce, pe]
        sig = MagicMock(symbol="MOCK", id=1)

        # CE more challenged than PE -> CE would be the rolled leg, but the cap
        # must stop the roll before any strike/quote lookup happens at all.
        with patch("django.utils.timezone.now") as mock_now, \
             patch("stocks.services.option_greeks_service.calculate_greeks") as mock_greeks, \
             patch("stocks.services.delta_hedge_service.get_nse_option_strikes") as mock_strikes:
            mock_now.return_value.astimezone.return_value.date.return_value = today
            mock_greeks.side_effect = [{'delta': 0.40}, {'delta': 0.20}]  # CE, PE

            rebalance_delta_neutral_strangle(sig, updated_legs, 24500.0, "NSE", MagicMock())

            mock_strikes.assert_not_called()

        self.assertEqual(len(updated_legs), 2, "No new leg should have been appended")

    def test_first_roll_of_day_stamps_count_and_date(self):
        from stocks.services.delta_hedge_service import rebalance_delta_neutral_strangle

        today = datetime(2026, 6, 1).date()
        ce, pe = self._legs()  # no prior roll_count -> first roll of the day
        updated_legs = [ce, pe]
        sig = MagicMock(symbol="MOCK", id=1)

        strikes = [
            {'strike': 25000.0, 'expiry': '25JUN2026'},
            {'strike': 25500.0, 'expiry': '25JUN2026'},
        ]

        with patch("django.utils.timezone.now") as mock_now, \
             patch("stocks.services.option_greeks_service.calculate_greeks") as mock_greeks, \
             patch("stocks.services.delta_hedge_service.get_nse_option_strikes", return_value=strikes), \
             patch("stocks.services.delta_hedge_service.get_nse_option_quote", return_value={'ltp': 12.5}), \
             patch("stocks.services.delta_hedge_service.get_lot_size", return_value=50), \
             patch("stocks.services.telegram_service.send_telegram_message"):
            mock_now.return_value.astimezone.return_value.date.return_value = today
            # 2 calls to size the imbalance (CE, PE), then repeated calls per
            # candidate strike while picking the roll target.
            mock_greeks.side_effect = [
                {'delta': 0.40}, {'delta': 0.20},  # imbalance check: CE, PE (target_delta=0.20)
                {'delta': 0.35},  # candidate strike 25000 (== current strike; far from target)
                {'delta': 0.22},  # candidate strike 25500 (closer to target -> gets picked)
            ]

            rebalance_delta_neutral_strangle(sig, updated_legs, 24500.0, "NSE", MagicMock())

        self.assertEqual(len(updated_legs), 3, "A new rolled leg should have been appended")
        new_leg = updated_legs[-1]
        self.assertEqual(new_leg['roll_count'], 1)
        self.assertEqual(new_leg['last_roll_date'], today.isoformat())


class SingleWorkerGuardTests(TestCase):
    """
    Audit fix H1: the single-worker-process assumption (REST rate-limit lock,
    TrueData's one-session-per-login limit, in-process APScheduler dedup, every
    FileBasedCache-backed cooldown/halt key) was previously enforced only by a
    comment next to `--workers 1` in start.sh / the Oracle systemd unit — nothing
    failed loudly if that config drifted.
    """

    def test_real_render_invocation_passes(self):
        """start.sh's actual gunicorn invocation must not trip the guard."""
        from stocks.apps import _assert_single_gunicorn_worker

        argv = [
            "/opt/render/project/.venv/bin/python", "-m", "gunicorn",
            "config.wsgi:application", "--bind", "0.0.0.0:10000",
            "--workers", "1", "--worker-class", "gthread", "--threads", "4",
            "--timeout", "300",
        ]
        with patch("sys.argv", argv), patch.dict("os.environ", {}, clear=False):
            os.environ.pop("WEB_CONCURRENCY", None)
            _assert_single_gunicorn_worker()  # must not raise

    def test_real_oracle_invocation_passes(self):
        """The Oracle systemd unit's actual gunicorn invocation must not trip the guard."""
        from stocks.apps import _assert_single_gunicorn_worker

        argv = [
            "/home/ubuntu/tradepulse-truedata/backend/venv/bin/gunicorn",
            "config.wsgi:application", "--workers", "1",
            "--worker-class", "gthread", "--threads", "4", "--timeout", "300",
            "--bind", "unix:/run/tradepulse-backend.sock",
        ]
        with patch("sys.argv", argv), patch.dict("os.environ", {}, clear=False):
            os.environ.pop("WEB_CONCURRENCY", None)
            _assert_single_gunicorn_worker()  # must not raise

    def test_multiple_workers_flag_raises(self):
        from stocks.apps import _assert_single_gunicorn_worker

        argv = ["gunicorn", "config.wsgi:application", "--workers", "4"]
        with patch("sys.argv", argv), patch.dict("os.environ", {}, clear=False):
            os.environ.pop("WEB_CONCURRENCY", None)
            with self.assertRaises(RuntimeError):
                _assert_single_gunicorn_worker()

    def test_equals_syntax_multiple_workers_raises(self):
        from stocks.apps import _assert_single_gunicorn_worker

        argv = ["gunicorn", "config.wsgi:application", "--workers=3"]
        with patch("sys.argv", argv), patch.dict("os.environ", {}, clear=False):
            os.environ.pop("WEB_CONCURRENCY", None)
            with self.assertRaises(RuntimeError):
                _assert_single_gunicorn_worker()

    def test_web_concurrency_env_var_overrides_and_raises(self):
        """WEB_CONCURRENCY is gunicorn's own override, and takes priority — a
        `--workers 1` flag with WEB_CONCURRENCY=3 set must still be caught."""
        from stocks.apps import _assert_single_gunicorn_worker

        argv = ["gunicorn", "config.wsgi:application", "--workers", "1"]
        with patch("sys.argv", argv), patch.dict("os.environ", {"WEB_CONCURRENCY": "3"}):
            with self.assertRaises(RuntimeError):
                _assert_single_gunicorn_worker()

    def test_no_workers_flag_defaults_to_one_and_passes(self):
        """Absence of --workers must not be treated as unbounded/unknown — gunicorn's
        own default is 1, so this must not raise."""
        from stocks.apps import _assert_single_gunicorn_worker

        argv = ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
        with patch("sys.argv", argv), patch.dict("os.environ", {}, clear=False):
            os.environ.pop("WEB_CONCURRENCY", None)
            _assert_single_gunicorn_worker()  # must not raise


class RegimeGatingFailClosedTests(TestCase):
    """
    Audit fix H11: "Compression Breakout"/"Compression Breakdown" (intraday_service's
    Trigger 4) were never added to MOMENTUM_STRATEGIES/MEAN_REVERSION_STRATEGIES, and
    strategy_allowed()'s fallback was `return True` — so that whole trigger family
    bypassed regime gating entirely, firing regardless of market state. Now classified
    as momentum, and the fallback fails closed instead of open.
    """

    def test_compression_breakout_is_gated_as_momentum(self):
        from stocks.services.shared.regime import strategy_allowed, RegimeState

        allow = RegimeState(allow_momentum=True, allow_mean_reversion=False)
        block = RegimeState(allow_momentum=False, allow_mean_reversion=True)

        self.assertTrue(strategy_allowed("Compression Breakout", allow))
        self.assertFalse(strategy_allowed("Compression Breakout", block))
        self.assertTrue(strategy_allowed("Compression Breakdown", allow))
        self.assertFalse(strategy_allowed("Compression Breakdown", block))

    def test_unclassified_trigger_fails_closed_not_open(self):
        """The bug class itself: an unregistered trigger name must be blocked, not
        silently ungated, regardless of what the regime otherwise permits."""
        from stocks.services.shared.regime import strategy_allowed, RegimeState

        wide_open = RegimeState(allow_momentum=True, allow_mean_reversion=True)
        self.assertFalse(strategy_allowed("Some Future Trigger Nobody Classified", wide_open))

    def test_swing_unclassified_family_fails_closed_not_open(self):
        """Same fail-open trap existed in swing_signals.strategy_allowed too."""
        from stocks.services import swing_signals

        class WideOpenRegime:
            allow_momentum = True
            allow_mean_reversion = True

        self.assertFalse(swing_signals.strategy_allowed("SOME_UNKNOWN_FAMILY", WideOpenRegime()))


class ResetStrategyAtomicityTests(TestCase):
    """
    Audit fix H5: Reset Strategy used to cancel the existing specialist book, then
    separately try to rebuild it with no atomicity between the two steps — a scan
    failure partway through (or a total scan failure) left the account flat with its
    hedge already cancelled and nothing to replace it. Now the network-heavy pre-scan
    runs BEFORE anything is cancelled, and the cancel+recreate DB writes happen
    together in one transaction.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user(
            username="reset_test_user", password="x", is_temporary=False,
        )

    def _existing_specialist_signal(self, symbol="OLDSYM"):
        return SignalHistory.objects.create(
            symbol=symbol, signal_type="STRANGLE", entry_price=100.0,
            target=0, stop_loss=0, status=SignalHistory.Status.ACTIVE,
            category="specialist", metadata={"legs": []},
        )

    def test_zero_candidates_leaves_existing_signals_untouched(self):
        """Pre-scan finding nothing viable must NOT cancel the existing book."""
        from rest_framework.test import APIClient

        existing = self._existing_specialist_signal()

        client = APIClient()
        client.force_authenticate(user=self.user)
        # Patching NIFTY_50_STOCKS to an empty list makes the pre-scan produce zero
        # candidates without needing to mock the whole quote/leg-building pipeline.
        with patch("stocks.services.delta_hedge_service.NIFTY_50_STOCKS", return_value=[]), \
             patch("stocks.services.delta_hedge_service.ENTRY_WINDOW_START", time(0, 0)), \
             patch("stocks.services.delta_hedge_service.ENTRY_WINDOW_END", time(23, 59)):
            response = client.post("/api/stocks/delta-hedge/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data.get("cancelled"), 0)
        existing.refresh_from_db()
        self.assertEqual(
            existing.status, SignalHistory.Status.ACTIVE,
            "Existing signal must be left untouched when nothing viable was built",
        )

    def test_temporary_account_still_blocked(self):
        """Sanity check: the is_temporary block (C9) must survive the H5 restructure."""
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        guest = get_user_model().objects.create_user(
            username="guest_test_user", password="x", is_temporary=True,
        )
        client = APIClient()
        client.force_authenticate(user=guest)
        response = client.post("/api/stocks/delta-hedge/")

        self.assertEqual(response.status_code, 403)


class OptionBuyingSameDayExpiryTests(TestCase):
    """
    Audit fix M12: select_option_buying_strike() used naive (non-IST) datetime.now()
    with max(1, ...) silently flooring a genuinely same-day expiry up to "1 day
    left" — a 0-DTE contract has extreme gamma/theta risk never explicitly guarded
    against. Now rejects same-day (or already-past) expiry outright.
    """

    def test_same_day_expiry_is_rejected(self):
        from stocks.services.option_buying_service import select_option_buying_strike

        today_ist = datetime(2026, 6, 25, 11, 0, tzinfo=IST)
        mock_svc = MagicMock()
        mock_svc.get_nearest_strike.return_value = 1500.0
        mock_svc.get_option_quote.return_value = {
            "ltp": 20.0, "expiry": "25JUN2026", "trading_symbol": "TESTSTOCK25JUN1500CE",
        }

        with patch("stocks.services.option_buying_service.datetime") as mock_dt:
            mock_dt.now.return_value = today_ist
            mock_dt.strptime = datetime.strptime  # keep real parsing for the expiry string
            result = select_option_buying_strike("TESTSTOCK", 1490.0, "BUY_CE", mock_svc)

        self.assertIsNone(result, "A same-day-expiry contract must be rejected, not floored to t_days=1")

    def test_future_expiry_still_accepted(self):
        """Sanity check: the fix must not break the normal multi-day-expiry path."""
        from stocks.services.option_buying_service import select_option_buying_strike

        today_ist = datetime(2026, 6, 20, 11, 0, tzinfo=IST)
        mock_svc = MagicMock()
        mock_svc.get_nearest_strike.return_value = 1500.0
        mock_svc.get_option_quote.return_value = {
            "ltp": 20.0, "expiry": "25JUN2026", "trading_symbol": "TESTSTOCK25JUN1500CE",
        }

        with patch("stocks.services.option_buying_service.datetime") as mock_dt, \
             patch("stocks.services.option_buying_service.estimate_iv", return_value=0.20), \
             patch("stocks.services.option_buying_service.calculate_greeks", return_value={"delta": 0.50, "theta": -1.0}):
            mock_dt.now.return_value = today_ist
            mock_dt.strptime = datetime.strptime
            result = select_option_buying_strike("TESTSTOCK", 1490.0, "BUY_CE", mock_svc)

        self.assertIsNotNone(result)
        self.assertEqual(result["t_days"], 5)


class LotSizeFallbackTests(TestCase):
    """
    Audit fix H18: a failed lot-size lookup used to silently substitute 1 share
    (wrong by 50-1000x+ for almost every real NSE lot size), corrupting target/SL
    and credit math without anything logging it. get_lot_size() now returns 0 (an
    unmistakable "unknown" sentinel) instead, and every caller either aborts
    (new-signal generation) or falls back to its own previously known-good value
    (existing-position P&L refresh) rather than trading on a fabricated number.
    """

    def test_get_lot_size_returns_zero_not_one_when_unresolvable(self):
        from stocks.services.delta_hedge_service import get_lot_size

        with patch("stocks.services.delta_hedge_service.get_truedata_instance", return_value=None):
            self.assertEqual(get_lot_size("SOME_RANDOM_STOCK", "NSE"), 0)

    def test_get_lot_size_still_resolves_index_fallbacks(self):
        """Sanity check: the fix must not break the legitimate index fallback path."""
        from stocks.services.delta_hedge_service import get_lot_size

        with patch("stocks.services.delta_hedge_service.get_truedata_instance", return_value=None):
            self.assertEqual(get_lot_size("NIFTY", "NSE"), 50)
            self.assertEqual(get_lot_size("BANKNIFTY", "NSE"), 15)

    def test_build_specialist_hedge_aborts_when_lot_size_unresolvable(self):
        from stocks.services.delta_hedge_service import build_specialist_hedge

        orch = MagicMock()
        with patch("stocks.services.delta_hedge_service.get_lot_size", return_value=0):
            res = build_specialist_hedge("SOME_STOCK", "NFO", 1000.0, orch)

        self.assertEqual(res, [], "Must abort strangle build rather than price legs off a fake lot size")

    def test_compute_target_sl_returns_sentinel_when_lot_size_unresolvable(self):
        """(0.0, 0.0) sentinel — the caller's existing `target <= entry_premium`
        viability check already discards this, so no downstream change was needed."""
        from stocks.services.option_buying_service import _compute_target_sl

        with patch("stocks.services.delta_hedge_service.get_lot_size", return_value=0):
            target, sl = _compute_target_sl("SOME_STOCK", entry_premium=25.0)

        self.assertEqual((target, sl), (0.0, 0.0))
        # Confirm the existing downstream guard really would discard this candidate.
        entry_premium = 25.0
        self.assertTrue(target <= entry_premium or sl >= entry_premium or sl <= 0)

    def test_compute_target_sl_unaffected_when_lot_size_resolves(self):
        """Sanity check: the fix must not change the happy-path target/SL math."""
        from stocks.services.option_buying_service import _compute_target_sl

        with patch("stocks.services.delta_hedge_service.get_lot_size", return_value=500):
            target, sl = _compute_target_sl("SOME_STOCK", entry_premium=25.0)

        self.assertGreater(target, 25.0)
        self.assertLess(sl, 25.0)
        self.assertGreater(sl, 0.0)


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


class IntradayRRRecomputeAfterSlippageTests(TestCase):
    """
    Audit fix M11: rr was computed against the pre-slippage entry and never
    recomputed after the slippage-adjusted fill was applied — a candidate that
    narrowly cleared the 1.5 RR gate pre-slippage could be persisted with a real
    RR below the platform's own stated minimum.
    """

    def test_candidate_rejected_when_slippage_drops_rr_below_threshold(self):
        from stocks.services.intraday_service import _build_intraday_candidate

        # entry=100, stop=95 (risk=5), target=107.5 -> pre-slippage RR = 7.5/5 = 1.5
        # (exactly at the gate). An adverse BUY fill of +1 (entry->101) drops the
        # reward distance to 6.5 and raises the risk distance to 6 -> RR ~1.08.
        with patch(
            "stocks.services.trading_engine.cost_model.DEFAULT_COST_MODEL.slipped_fill",
            return_value=101.0,
        ):
            result = _build_intraday_candidate(
                ticker_sym="TESTSTOCK", strategy_key="TEST", strategy_name="Test",
                signal="BUY", price=100.0, entry=100.0, stop_loss=95.0, target=107.5,
                rr=1.5, reason="test", score=4.0, priority=1, vol_ratio=1.5,
            )

        self.assertIsNone(result, "Must reject once the slippage-adjusted RR falls below 1.5")

    def test_candidate_accepted_when_rr_still_clears_after_slippage(self):
        """Sanity check: a candidate with real headroom must still pass."""
        from stocks.services.intraday_service import _build_intraday_candidate

        with patch(
            "stocks.services.trading_engine.cost_model.DEFAULT_COST_MODEL.slipped_fill",
            return_value=100.1,
        ):
            result = _build_intraday_candidate(
                ticker_sym="RELIANCE", strategy_key="TEST", strategy_name="Test",
                signal="BUY", price=100.0, entry=100.0, stop_loss=95.0, target=120.0,
                rr=4.0, reason="test", score=4.0, priority=1, vol_ratio=1.5,
                liquidity={"adv_inr": 1e9, "daily_vol_pct": 1.0},
            )

        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["rr"], 1.5)


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

    def _closed_signal(self, symbol, entry, exit_price, status, strike=100.0, option_type="CE",
                        generated_at=None):
        from stocks.models import SignalHistory as SH
        sig = SH.objects.create(
            symbol=symbol, signal_type="BUY_CE", category="option_buying",
            entry_price=entry, stop_loss=entry * 0.5, target=entry * 2.0, rr=2.0,
            status=status, reason="test", exit_price=exit_price,
            strike_price=strike, option_type=option_type,
        )
        if generated_at is not None:
            # Fixed generated_at instead of auto_now_add's real "now" — Django's
            # generated_at__date lookup evaluates in settings.TIME_ZONE (UTC), so a
            # test using real wall-clock IST "now" is flaky for ~5.5h/day (IST
            # 00:00-05:30, when the IST and UTC calendar dates disagree). Picking a
            # fixed mid-day IST moment keeps both dates in agreement regardless of
            # when the suite actually runs.
            SH.objects.filter(pk=sig.pk).update(generated_at=generated_at)
        return sig

    @patch("stocks.services.delta_hedge_service.get_lot_size", return_value=50)
    def test_c2_halt_arms_from_closed_rows_with_empty_active_book(self, mock_lot_size):
        """A day of stopped-out option_buying trades (nothing ACTIVE) must still arm
        the halt — same empty-book scenario as C1's intraday fix."""
        from stocks.services.option_buying_service import (
            _enforce_option_buying_daily_loss_limit, OPTION_BUYING_DAILY_HALT_CACHE_KEY,
        )
        from stocks.services.intraday_service import INTRADAY_ACCOUNT_EQUITY

        now_ist = datetime(2026, 4, 1, 13, 15, tzinfo=IST)

        # 2-lot P&L per Re.1 move = lot_size(50)*2 = 100. Loss of Rs.3/premium * 100 =
        # Rs.300 per trade; enough trades to clear the 2% limit regardless of its value.
        limit = INTRADAY_ACCOUNT_EQUITY * 0.02
        per_trade_loss = 3.0 * 100
        n = int(limit // per_trade_loss) + 2
        for i in range(n):
            self._closed_signal(f"SYM{i}", entry=50.0, exit_price=47.0,
                                 status=SignalHistory.Status.HIT_SL, generated_at=now_ist)

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

