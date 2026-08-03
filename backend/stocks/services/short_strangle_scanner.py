"""
short_strangle_scanner.py

Standalone intraday short strangle scanner for NSE F&O stocks.
Scans Nifty 50 stocks, applies intraday strike selection rules,
validates delta / premium / spread, and returns qualifying setups.

Usage (via management command):
    python manage.py scan_strangles --force
    python manage.py scan_strangles --force --symbols ADANIENT,RELIANCE,ITC
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from django.utils import timezone

from stocks.services.delta_hedge_service import (
    get_nse_option_strikes,
    get_lot_size,
    NIFTY_50_STOCKS,
)
from stocks.services.option_greeks_service import (
    calculate_greeks,
    estimate_iv,
    normal_cdf,
)
from stocks.services.market_data_orchestrator import get_orchestrator
from stocks.services.signal_utils import IST

logger = logging.getLogger(__name__)


# ─── Intraday Strike Distance Rules (% from spot) ───────────────────
# Strikes placed at 1.2x–1.5x expected intraday range
# CRITICAL FIX: Raised minimums to prevent near-ATM strikes (was 1.5-2.5%)
INTRADAY_DISTANCE = {
    "HIGH":   {"min": 0.045, "max": 0.080, "step": 0.005},
    "MEDIUM": {"min": 0.040, "max": 0.070, "step": 0.005},
    "LOW":    {"min": 0.035, "max": 0.060, "step": 0.005},
}

# ─── Validation Thresholds ───────────────────────────────────────────
MIN_DELTA_DEFAULT = 0.10
MAX_DELTA_DEFAULT = 0.30         # Intraday: lower delta for safer OTM strikes (was 0.35)
MIN_CREDIT_PCT_DEFAULT = 0.015   # 1.5% of spot for stocks
MAX_BID_ASK_SPREAD = 0.05       # 5%


class ShortStrangleScanner:
    """Scans NSE F&O stocks for intraday short strangle opportunities."""

    def __init__(self, min_credit_pct: float | None = None, max_delta: float | None = None):
        self.orch = get_orchestrator()
        self.min_credit_pct = min_credit_pct or MIN_CREDIT_PCT_DEFAULT
        self.max_delta = max_delta or MAX_DELTA_DEFAULT

    @staticmethod
    def get_vol_bucket(iv_pct: float) -> str:
        """Classify volatility dynamically based on live IV percentage."""
        if iv_pct < 16.0:
            return "LOW"
        if iv_pct <= 22.0:
            return "MEDIUM"
        return "HIGH"

    def scan(
        self,
        symbols: list[str] | None = None,
        progress_callback=None,
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        """
        Run the full scan.

        Returns:
            (results, rejections)
            results: list of qualifying setup dicts
            rejections: list of (symbol, reason) tuples
        """
        symbols = symbols or NIFTY_50_STOCKS()
        results: list[dict[str, Any]] = []
        rejections: list[tuple[str, str]] = []

        # ── 1. Bulk fetch spot prices and warm up quotes (LTP, High, Low) ──
        from stocks.services.truedata_service import get_truedata_instance
        svc = get_truedata_instance()
        if svc:
            token_map = svc.get_token_map(symbols, "NSE")
            if token_map:
                spot_tokens = list(token_map.values())
                try:
                    logger.info("[WARM-UP] Bulk pre-fetching %d spot quotes (LTP, High, Low) for scanner", len(spot_tokens))
                    if svc.streamer:
                        svc.streamer.subscribe(1, spot_tokens)
                    svc.get_bulk_quotes({"NSE": spot_tokens})
                except Exception as e:
                    logger.warning("[WARM-UP] Bulk spot pre-fetch failed: %s", e)

        spot_prices = self.orch.get_prices_bulk(symbols, "NSE")

        # ── 2. Scan each symbol ──────────────────────────────────
        for idx, symbol in enumerate(symbols):
            if progress_callback:
                progress_callback(idx + 1, len(symbols), symbol)

            spot = spot_prices.get(symbol, 0)
            if spot <= 0:
                rejections.append((symbol, "No spot price available"))
                continue

            setup, reason = self._scan_symbol(symbol, spot)
            if setup:
                if setup["risk"] not in ["Low", "Low-Medium"]:
                    rejections.append((symbol, f"High risk rating: {setup['risk']}"))
                else:
                    results.append(setup)
            else:
                rejections.append((symbol, reason))

        # Sort by: Risk (Low first), then IV ascending (lower movement/volatility first), then credit_pct descending
        results.sort(key=lambda x: (
            0 if x["risk"] == "Low" else 1,
            x["iv"],
            -x["credit_pct"]
        ))
        
        # Limit to exactly top 10 setups
        results = results[:10]
        return results, rejections

    # ──────────────────────────────────────────────────────────────
    # SINGLE SYMBOL SCAN
    # ──────────────────────────────────────────────────────────────

    def _scan_symbol(
        self, symbol: str, spot: float
    ) -> tuple[dict[str, Any] | None, str]:
        """Scan a single symbol for an intraday strangle opportunity."""

        # 1. Fetch option strikes (auto-filtered to nearest expiry)
        strikes = get_nse_option_strikes(symbol, spot)
        if not strikes:
            return None, "No option strikes available"

        # Bulk warm-up option quotes to prevent WAF block on individual queries
        from stocks.services.truedata_service import get_truedata_instance
        _svc_inst = get_truedata_instance()
        if _svc_inst and strikes:
            tokens_to_warm = [str(row["token"]) for row in strikes if row.get("token")]
            if tokens_to_warm:
                logger.info("[WARM-UP] Bulk pre-fetching %d option quotes for %s in scanner", len(tokens_to_warm), symbol)
                try:
                    if _svc_inst.streamer:
                        _svc_inst.streamer.subscribe(2, tokens_to_warm)
                    _svc_inst.get_bulk_quotes({"NFO": tokens_to_warm})
                except Exception as w_err:
                    logger.warning("[WARM-UP] Bulk pre-fetch failed for scanner: %s", w_err)

        # 2. Parse expiry and DTE
        today = timezone.now().astimezone(IST).date()
        try:
            target_expiry = strikes[0]["expiry"]
            expiry_date = datetime.strptime(target_expiry, "%d%b%Y").date()
            dte = max(1, (expiry_date - today).days)
        except Exception:
            return None, "Failed to parse expiry"

        if dte <= 0:
            return None, f"DTE={dte}, expiry-day risk"

        # 3. Lot size
        lot_size = get_lot_size(symbol, "NFO")

        # 4. Separate CE / PE maps
        ce_map: dict[float, dict] = {}
        pe_map: dict[float, dict] = {}

        for row in strikes:
            sp = row.get("strike_price", 0)
            sym_str = row.get("symbol", "")
            if sp <= 0:
                continue
            if sym_str.endswith("CE") and sp > spot:
                ce_map[sp] = row
            elif sym_str.endswith("PE") and sp < spot:
                pe_map[sp] = row

        if not ce_map or not pe_map:
            return None, "No OTM strikes available"

        ce_sorted = sorted(ce_map.keys())
        pe_sorted = sorted(pe_map.keys())

        # 5. Estimate IV from ATM option
        sigma = 0.20
        try:
            atm_sv = min(ce_map.keys(), key=lambda s: abs(s - spot))
            atm_row = ce_map[atm_sv]
            atm_res = self.orch.get_option_data(
                symbol, atm_sv, "CE", atm_row["expiry"], "NFO"
            )
            atm_quote = atm_res[0] if isinstance(atm_res, tuple) else atm_res
            atm_ltp = float(atm_quote.get("ltp", 0))
            if atm_ltp > 0:
                sigma = estimate_iv(spot, atm_sv, dte, atm_ltp, "CE")
        except Exception:
            pass

        # 5.1 Real-Time Volatility & Movement Checks
        iv_pct = sigma * 100
        if iv_pct > 22.0:
            return None, f"Live IV {iv_pct:.1f}% exceeds safety cap (22.0%)"

        # Intraday range check
        intraday_range_pct = 999.0
        try:
            price_res = self.orch.get_price(symbol, exchange='NSE') or {}
            ltp  = float(price_res.get('ltp', 0) or 0)
            high = float(price_res.get('high', 0) or 0)
            low  = float(price_res.get('low', 0) or 0)
            if ltp > 0 and high > low > 0:
                intraday_range_pct = (high - low) / ltp * 100.0
        except Exception:
            pass
        if intraday_range_pct > 1.5:
            return None, f"Intraday range {intraday_range_pct:.2f}% exceeds safety cap (1.5%)"

        # 6. Find all candidate strikes within the volatility bucket range
        bucket = self.get_vol_bucket(iv_pct)
        cfg = INTRADAY_DISTANCE[bucket]
        min_dist = cfg["min"]
        max_dist = cfg["max"]

        ce_candidates = []
        for strike_val, row in ce_map.items():
            dist = (strike_val - spot) / spot
            if min_dist <= dist <= max_dist:
                ce_candidates.append((strike_val, row))

        pe_candidates = []
        for strike_val, row in pe_map.items():
            dist = (spot - strike_val) / spot
            if min_dist <= dist <= max_dist:
                pe_candidates.append((strike_val, row))

        # Fallback to closest available OTM strike if none are in the range
        if not ce_candidates and ce_map:
            closest_ce = min(ce_map.keys(), key=lambda s: abs(s - spot * (1 + (min_dist + max_dist)/2)))
            ce_candidates.append((closest_ce, ce_map[closest_ce]))
        if not pe_candidates and pe_map:
            closest_pe = min(pe_map.keys(), key=lambda s: abs(s - spot * (1 - (min_dist + max_dist)/2)))
            pe_candidates.append((closest_pe, pe_map[closest_pe]))

        if not ce_candidates or not pe_candidates:
            return None, "No candidate strikes available after filtering"

        # 7. Evaluate candidates and fetch quotes/Greeks
        valid_ce_list = []
        for ce_sv, ce_row in ce_candidates:
            ce_greeks = calculate_greeks(spot, ce_sv, dte, sigma=sigma, option_type="CE")
            ce_delta = abs(ce_greeks.get("delta", 0))
            if ce_delta > self.max_delta:
                continue

            try:
                ce_res = self.orch.get_option_data(symbol, ce_sv, "CE", ce_row["expiry"], "NFO")
                ce_quote = ce_res[0] if isinstance(ce_res, tuple) else ce_res
            except Exception:
                ce_quote = {}

            # Live quote only — a theoretical Black-Scholes fallback here would present
            # an unfillable price as a "SELL @" instruction to users. Skip the leg instead.
            ce_premium = float(ce_quote.get("ltp", 0))
            if ce_premium <= 0:
                continue

            ce_bid = float(ce_quote.get("bid", 0))
            ce_ask = float(ce_quote.get("ask", 0))
            if ce_bid > 0 and ce_ask > 0:
                ce_spread = (ce_ask - ce_bid) / ce_premium
                if ce_spread > MAX_BID_ASK_SPREAD:
                    continue

            valid_ce_list.append({
                "strike": ce_sv,
                "premium": ce_premium,
                "delta": ce_delta,
                "theta": ce_greeks.get("theta", 0),
                "distance_pct": (ce_sv - spot) / spot * 100,
                "quote": ce_quote
            })

        valid_pe_list = []
        for pe_sv, pe_row in pe_candidates:
            pe_greeks = calculate_greeks(spot, pe_sv, dte, sigma=sigma, option_type="PE")
            pe_delta = abs(pe_greeks.get("delta", 0))
            if pe_delta > self.max_delta:
                continue

            try:
                pe_res = self.orch.get_option_data(symbol, pe_sv, "PE", pe_row["expiry"], "NFO")
                pe_quote = pe_res[0] if isinstance(pe_res, tuple) else pe_res
            except Exception:
                pe_quote = {}

            # Live quote only — see CE leg above for rationale.
            pe_premium = float(pe_quote.get("ltp", 0))
            if pe_premium <= 0:
                continue

            pe_bid = float(pe_quote.get("bid", 0))
            pe_ask = float(pe_quote.get("ask", 0))
            if pe_bid > 0 and pe_ask > 0:
                pe_spread = (pe_ask - pe_bid) / pe_premium
                if pe_spread > MAX_BID_ASK_SPREAD:
                    continue

            valid_pe_list.append({
                "strike": pe_sv,
                "premium": pe_premium,
                "delta": pe_delta,
                "theta": pe_greeks.get("theta", 0),
                "distance_pct": (spot - pe_sv) / spot * 100,
                "quote": pe_quote
            })

        # 8. Find the best pair minimizing absolute premium difference
        best_pair = None
        min_prem_diff = float("inf")
        last_reason = "No candidate pairs passed credit or delta thresholds"

        for ce_item in valid_ce_list:
            for pe_item in valid_pe_list:
                net_credit = ce_item["premium"] + pe_item["premium"]
                credit_pct = (net_credit / spot) * 100

                if credit_pct < self.min_credit_pct * 100:
                    last_reason = f"Credit {credit_pct:.2f}% < {self.min_credit_pct * 100:.1f}% min"
                    continue

                prem_diff = abs(ce_item["premium"] - pe_item["premium"])
                if prem_diff < min_prem_diff:
                    min_prem_diff = prem_diff
                    best_pair = (ce_item, pe_item)

        if not best_pair:
            return None, last_reason

        ce_best, pe_best = best_pair
        net_credit = ce_best["premium"] + pe_best["premium"]
        credit_pct = (net_credit / spot) * 100
        risk = self._assess_risk(bucket, dte, ce_best["delta"], pe_best["delta"])
        pop = self._estimate_pop(spot, ce_best["strike"], pe_best["strike"], net_credit, dte, sigma)

        return {
            "symbol": symbol,
            "spot": round(spot, 2),
            "dte": dte,
            "expiry": target_expiry,
            "lot_size": lot_size,
            "vol_bucket": bucket,
            # CE leg
            "ce_strike": ce_best["strike"],
            "ce_premium": round(ce_best["premium"], 2),
            "ce_delta": round(ce_best["delta"], 4),
            "ce_theta": round(ce_best["theta"], 4),
            "ce_distance_pct": round(ce_best["distance_pct"], 1),
            # PE leg
            "pe_strike": pe_best["strike"],
            "pe_premium": round(pe_best["premium"], 2),
            "pe_delta": round(-pe_best["delta"], 4),
            "pe_theta": round(pe_best["theta"], 4),
            "pe_distance_pct": round(pe_best["distance_pct"], 1),
            # Combined
            "net_credit": round(net_credit, 2),
            "credit_pct": round(credit_pct, 2),
            "credit_per_lot": round(net_credit * lot_size, 2),
            "upper_be": round(ce_best["strike"] + net_credit, 2),
            "lower_be": round(pe_best["strike"] - net_credit, 2),
            "pop": pop,
            "risk": risk,
            "target_profit": round(net_credit * 0.15, 2),
            "stop_loss": round(net_credit, 2),
            "iv": round(sigma * 100, 1),
        }, ""

    # ──────────────────────────────────────────────────────────────
    # RISK & POP HELPERS
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _assess_risk(
        bucket: str, dte: int, ce_delta: float, pe_delta: float
    ) -> str:
        avg = (ce_delta + pe_delta) / 2
        if bucket == "HIGH" or avg > 0.30 or dte <= 5:
            return "Medium"
        if avg > 0.25:
            return "Low-Medium"
        return "Low"

    @staticmethod
    def _estimate_pop(
        spot: float,
        ce_strike: float,
        pe_strike: float,
        net_credit: float,
        dte: int,
        sigma: float,
    ) -> int:
        """Estimate probability of profit from breakevens using lognormal model."""
        upper_be = ce_strike + net_credit
        lower_be = pe_strike - net_credit
        try:
            t = max(0.001, dte / 365.0)
            vol_move = spot * sigma * math.sqrt(t)
            if vol_move <= 0:
                return 75
            d_up = (upper_be - spot) / vol_move
            d_low = (lower_be - spot) / vol_move
            pop = (normal_cdf(d_up) - normal_cdf(d_low)) * 100
            return min(95, max(50, round(pop)))
        except Exception:
            return 75
