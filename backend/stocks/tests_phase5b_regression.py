"""
AUDIT_REMEDIATION_PLAN.md — Phase 5, item B ("the same bug class keeps getting fixed
once, not once-and-shared").

Three unrelated bugs were each fixed at exactly one call site, with the same underlying
pattern left unchecked/untested at other call sites using the same utility. Nothing
guarded against the fix silently regressing later. This file pins down the CURRENT
(already-fixed) correct behaviour for each of the three so a future refactor of the
shared utility can't quietly break one of them while leaving the others fine:

1. `compute_session_vwap()` (signal_utils.py) must return a finite value on zero-volume
   candles (NIFTY 50 index bars) instead of NaN — fixed 2026-07-26, see CLAUDE.md's
   "Historical bug, fixed 2026-07-26" section.
2. The AB1021 circuit breaker must trip identically for quote calls
   (`get_bulk_quotes`) as it already does for candle calls — Phase 2 #2.3.
3. `trade_engine.py`'s `_compute_ai_score()` must reject a candidate whose target is
   under 6x round-trip cost, matching the pattern already enforced in
   `intraday_service.py` / `swing_service.py` — Phase 1 #6.

NOTE: these tests require `django.setup()` (all three modules under test import Django
models/settings at module scope) and this repo's `StocksConfig.ready()` logs into the
live TrueData session as a side effect UNLESS `'test' in sys.argv` (see
`stocks/apps.py`) — which is exactly the guard Django's own `manage.py test` / test
runner trips.

Audit fix H16: this file was originally committed hand-traced but never actually
executed (see AUDIT_REMEDIATION_PLAN.md Phase 5 item B and the matching caveat that
used to be here). Run for real via `manage.py test stocks.tests_phase5b_regression` —
all 6 pass. It's picked up automatically by `manage.py test stocks` (Django's default
discovery matches any `test*.py` module) and by the CI workflow (Phase 3 #3.4.1), so it
runs on every push going forward; no special invocation is needed.
"""
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
from django.test import TestCase


class SessionVwapZeroVolumeIndexTests(TestCase):
    """CLAUDE.md's "Historical bug, fixed 2026-07-26": NIFTY 50 index candles report
    Volume=0 on every bar (only constituents trade, not the index itself), which used
    to divide-by-zero into NaN inside compute_session_vwap() and propagate through
    get_regime()'s trend-score math, silently collapsing directional_bias to SIDEWAYS
    regardless of the real market. compute_session_vwap() now falls back to an
    unweighted running average of typical price within the session when volume is 0.
    """

    def test_session_vwap_zero_volume_index_returns_finite(self):
        from stocks.services.signal_utils import compute_session_vwap

        idx = pd.date_range("2026-07-28 09:15", periods=10, freq="15min")
        df = pd.DataFrame(
            {
                "High": np.linspace(22050, 22140, 10),
                "Low": np.linspace(22000, 22090, 10),
                "Close": np.linspace(22025, 22115, 10),
                "Volume": [0] * 10,  # index candles: never traded directly
            },
            index=idx,
        )

        result = compute_session_vwap(df)

        self.assertFalse(result.isna().any(), "VWAP must not be NaN on zero-volume candles")
        self.assertTrue(np.isfinite(result).all(), "VWAP must be a finite number, not inf/NaN")
        # Sanity: the unweighted fallback should still land inside the day's price range,
        # not some divide-by-zero artifact.
        self.assertTrue((result >= df["Low"].min()).all())
        self.assertTrue((result <= df["High"].max()).all())

    def test_session_vwap_mixed_zero_and_nonzero_volume_still_finite(self):
        """A session that starts with zero-volume bars (pre-open prints) and picks up
        real volume later must not NaN-poison the whole cumulative series once volume
        turns nonzero — the two calculations are joined via .fillna(), so only the
        genuinely zero-volume rows should use the fallback.
        """
        from stocks.services.signal_utils import compute_session_vwap

        idx = pd.date_range("2026-07-28 09:15", periods=6, freq="15min")
        df = pd.DataFrame(
            {
                "High": [100, 101, 102, 103, 104, 105],
                "Low": [99, 100, 101, 102, 103, 104],
                "Close": [99.5, 100.5, 101.5, 102.5, 103.5, 104.5],
                "Volume": [0, 0, 1000, 1500, 0, 2000],
            },
            index=idx,
        )

        result = compute_session_vwap(df)

        self.assertFalse(result.isna().any())
        self.assertTrue(np.isfinite(result).all())


class BulkQuotesQuotaExceededCircuitBreakerTests(TestCase):
    """Phase 2 #2.3 (ported for TrueData): the "quota exceeded" rate-limit signal
    previously only tripped the circuit breaker inside get_candle_data(). get_bulk_quotes()
    has since grown the identical branch (truedata_service.py, `_is_quota_exceeded()`) —
    this test pins that down and also confirms the breaker actually short-circuits the
    NEXT call within its 300s window instead of hitting the network again.

    TrueData signals a rate-limit breach as plain text in an HTTP 200 body ("API calls
    quota exceeded! maximum admitted 1 per Second." — Market Data API doc's tick/bar
    history error tables), not a JSON error-code field like Angel One's AB1021 — the
    mock below reflects that shape, not the old one.
    """

    def setUp(self):
        from stocks.services.truedata_service import _REST_CIRCUIT_BREAKER_UNTIL
        self._breaker = _REST_CIRCUIT_BREAKER_UNTIL
        self._breaker["quote"] = 0.0  # ensure a clean breaker state before each test

    def tearDown(self):
        self._breaker["quote"] = 0.0  # don't leak circuit-breaker state into other tests

    def _make_service(self):
        import time
        from stocks.services.truedata_service import TrueDataService
        svc = TrueDataService(username="X", password="Y")
        svc.streamer = None  # skip the WebSocket subscribe warm-up path entirely
        # Pre-authenticate so get_bulk_quotes' _ensure_fresh_token() is a no-op —
        # isolates the assertion to the quote call itself, not an incidental auth call.
        svc.is_authenticated = True
        svc.token_expires_at = time.time() + 3600
        return svc

    def test_bulk_quotes_quota_exceeded_trips_circuit_breaker(self):
        import time
        from stocks.services.truedata_service import _REST_CIRCUIT_BREAKER_UNTIL

        svc = self._make_service()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.text = "API calls quota exceeded! maximum admitted 1 per Second."
        svc._rest_request = MagicMock(return_value=fake_response)

        with patch("stocks.services.truedata_service.get_stream_price", return_value=None):
            result = svc.get_bulk_quotes({"NSE": ["RELIANCE"]}, mode="FULL")

        self.assertEqual(result, {}, "quota-exceeded response should yield no parsed quotes")
        self.assertEqual(svc._rest_request.call_count, 1)
        self.assertGreater(
            _REST_CIRCUIT_BREAKER_UNTIL["quote"], time.time(),
            "quota-exceeded on a bulk quote call must trip the shared 'quote' circuit breaker",
        )

    def test_bulk_quotes_short_circuits_within_breaker_window(self):
        """A second call while the breaker is still tripped must return immediately
        without ever reaching _rest_request (no network call) — this is the whole
        point of the circuit breaker existing.
        """
        import time

        svc = self._make_service()
        svc._rest_request = MagicMock()  # should never be called in this test

        # Simulate a breaker already tripped by a prior AB1021 response.
        self._breaker["quote"] = time.time() + 300

        with patch("stocks.services.truedata_service.get_stream_price", return_value=None):
            result = svc.get_bulk_quotes({"NSE": ["3045"]}, mode="FULL")

        self.assertEqual(result, {})
        svc._rest_request.assert_not_called()


class TradeEngineCostGateTests(TestCase):
    """Phase 1 #6: trade_engine.py's _compute_ai_score() now runs the same cost-of-
    trading gate intraday_service.py / swing_service.py already enforce — a candidate
    is rejected if its target1 is under `cost_model_for(SWING).round_trip_pct(...) *
    SWING.min_target_cost_multiple` (6x round-trip cost for the delivery/swing cost
    model; see shared/profiles.py's SWING_MIN_TARGET_COST_MULTIPLE default of 6.0).

    The gate runs BEFORE the ai_score/min_ai_score check (trade_engine.py ~line
    269-284, well above the "AI Score Calculation" section at ~line 286), so a rejection
    here is unambiguously the cost gate, not some other filter.

    cost_model_for() is mocked directly rather than fighting the real CostModel's
    ADV/spread internals — the gate's own arithmetic (`target1_pct < cost_pct *
    multiple`) is what's under test, not CostModel's fee schedule (which has its own
    coverage elsewhere).
    """

    def _make_uptrend_df(self, n: int = 260) -> pd.DataFrame:
        """A monotonically increasing daily-bar series so every trend/breakout/volume
        filter in _compute_ai_score passes cleanly, isolating the cost gate as the only
        variable between the two test cases. Hand-traced (atr_period=14,
        atr_stop_loss_multiplier=2.0) to give sl_points ~5.88% of entry and
        target1_pct ~11.77% (target1_risk_reward=2.0) — see this file's companion
        scratch verification; both test cases below only vary the mocked cost_pct
        around that fixed ~11.77% target, not the fixture itself.
        """
        closes = [100.0]
        for _ in range(1, n):
            closes.append(closes[-1] * 1.003)
        closes = pd.Series(closes)
        idx = pd.bdate_range("2024-01-01", periods=n)
        return pd.DataFrame(
            {
                "Close": closes.values,
                "High": (closes * 1.01).values,
                "Low": (closes * 0.98).values,
                "Volume": [200_000.0] * n,
            },
            index=idx,
        )

    def _patched_config(self):
        # Mirrors backend/stocks/config/strategy_config.json verbatim as of this
        # writing, pinned locally so this test can't silently drift if that file (or
        # DEFAULT_CONFIG) changes later.
        return {
            "indicators": {
                "ema_trend_short": 50, "ema_trend_long": 200, "ema_trailing": 20,
                "adx_period": 14, "rsi_period": 14, "atr_period": 14,
            },
            "strategy": {
                "min_ai_score": 25.0, "min_adx_threshold": 25.0,
                "adx_relaxed_threshold": 15.0, "min_volume_multiplier": 1.5,
                "volume_relaxed_multiplier": 1.0, "near_52w_high_percent": 5.0,
                "near_52w_relaxed_percent": 10.0, "minimum_liquidity_floor": 100000,
                "min_risk_reward_ratio": 2.0,
            },
            "trade_lifecycle": {
                "atr_stop_loss_multiplier": 2.0, "max_stop_loss_floor_pct": 10.0,
                "target1_risk_reward": 2.0, "target2_risk_reward": 3.0,
                "target3_risk_reward": 4.0,
            },
        }

    def test_trade_engine_rejects_target_below_cost_threshold(self):
        from stocks.services import trade_engine
        from stocks.services.shared import SWING

        # target1_pct is fixed by the fixture at ~11.7695% (hand-traced, see
        # _make_uptrend_df's docstring) regardless of the real multiplier value, so the
        # mocked cost_pct is derived from SWING.min_target_cost_multiple rather than
        # assuming its default (6.0) — this stays correct even if
        # SWING_MIN_TARGET_COST_MULTIPLE is overridden via env var in some environment.
        multiple = SWING.min_target_cost_multiple
        df = self._make_uptrend_df()
        fake_cost_model = MagicMock()
        fake_cost_model.round_trip_pct.return_value = 12.0 / multiple  # required = 12.0%

        with patch.dict(trade_engine.STRATEGY_CONFIG, self._patched_config(), clear=True), \
             patch.object(trade_engine, "cost_model_for", return_value=fake_cost_model):
            # required_target_pct = (12.0/multiple) * multiple = 12.0%, comfortably
            # above the fixture's ~11.77% target1_pct -> must be rejected.
            result = trade_engine._compute_ai_score(
                df, nifty_20d_ret=-1.0, relaxed=True, symbol="COSTGATE_REJECT",
            )

        self.assertIsNone(result, "A target under N x round-trip cost must be rejected")

    def test_trade_engine_accepts_target_above_cost_threshold(self):
        from stocks.services import trade_engine
        from stocks.services.shared import SWING

        multiple = SWING.min_target_cost_multiple
        df = self._make_uptrend_df()
        fake_cost_model = MagicMock()
        fake_cost_model.round_trip_pct.return_value = 0.3 / multiple  # required = 0.3%

        with patch.dict(trade_engine.STRATEGY_CONFIG, self._patched_config(), clear=True), \
             patch.object(trade_engine, "cost_model_for", return_value=fake_cost_model):
            # required_target_pct = (0.3/multiple) * multiple = 0.3%, well under the
            # fixture's ~11.77% target1_pct -> must NOT be rejected by the cost gate.
            result = trade_engine._compute_ai_score(
                df, nifty_20d_ret=-1.0, relaxed=True, symbol="COSTGATE_ACCEPT",
            )

        self.assertIsNotNone(result, "A target comfortably above N x round-trip cost must not be rejected")
        self.assertIn("target1", result)
        self.assertGreater(result["target1"], result["entry_price"])
