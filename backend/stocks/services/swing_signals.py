"""Swing setup detection — pure functions over adjusted daily OHLCV.

No I/O, no persistence, no broker calls. Everything here is deterministic given a
DataFrame, which is what makes the backtest and the live path provably identical.

Two setup families, deliberately separated. The engine being replaced had one implicit
setup that contradicted itself: it *selected* breakouts (52-week-high proximity, 20-day
high) and then *entered* only if price fell back below the scan price. That is a momentum
screen with a mean-reversion trigger — the two halves want opposite things, and the
combination has no coherent thesis.

  MOMENTUM  — price clears a structural high on expanding volume. Wants continuation.
              Requires regime.allow_momentum.
  PULLBACK  — price retraces to the 20-EMA inside an intact 50>200 uptrend. Wants
              mean reversion to trend. Requires regime.allow_mean_reversion.

Both are evaluated on CLOSED bars only. The old engine ran at 10:00 and read
`Close.iloc[-1]` from an in-progress daily bar, which made every indicator repaint and
— measured over 49 candidates on 2026-07-26 — cut the volume-gate pass rate from 19/49
to 11/49, because a 20%-complete bar drags `vol_5d` down far harder than `vol_20d`.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from stocks.services.signal_utils import (
    compute_adx, compute_atr, round_to_tick, ta_ema, ta_sma,
)
from stocks.services.trading_engine.cost_model import cost_model_for
from stocks.services.shared.risk_engine import position_size

logger = logging.getLogger(__name__)

MOMENTUM_FAMILY = "MOMENTUM"
PULLBACK_FAMILY = "PULLBACK"

# Minimum closed daily bars. 201 is needed for a converged EMA200; we require 220 so a
# 365-day fetch (~245 bars) still has margin after the forming bar is dropped.
MIN_BARS = 220

ATR_PERIOD = 14
ATR_STOP_MULT = 2.0
MAX_STOP_PCT = 10.0
TARGET_R_MULTIPLES = (2.0, 3.0, 4.0)


def drop_forming_bar(df: pd.DataFrame, session_complete: bool) -> pd.DataFrame:
    """Remove the still-forming last bar unless the session has closed.

    Angel One returns the current incomplete bar as the final row. Evaluating triggers
    on it is what made the old engine repaint.
    """
    if df is None or df.empty:
        return df
    return df if session_complete else df.iloc[:-1]


def _breakout_quality(close: float, high_20d: float, high_52w: float) -> float:
    """0..1 — how decisively price has cleared structural resistance.

    Rewards a clean break over a marginal one, and proximity to the 52-week high, which
    is where overhead supply is thinnest.
    """
    if high_20d <= 0 or high_52w <= 0:
        return 0.0
    break_margin = np.clip((close / high_20d - 1.0) / 0.03, 0.0, 1.0)
    prox_52w = np.clip(1.0 - (high_52w - close) / max(high_52w * 0.15, 1e-9), 0.0, 1.0)
    return float(0.5 * break_margin + 0.5 * prox_52w)


def _momentum_persistence(close_series: pd.Series) -> float:
    """12-1 style momentum adapted to the swing horizon: 6-month return excluding the
    most recent 10 sessions.

    The skip matters. Raw trailing momentum is contaminated by short-term reversal —
    the most recent stretch tends to mean-revert, so including it partially cancels the
    signal. Jegadeesh-Titman skip a month at the 12-month horizon; 10 sessions is the
    proportionate skip at 6 months.
    """
    if len(close_series) < 130:
        return 0.0
    recent = float(close_series.iloc[-11])
    past = float(close_series.iloc[-130])
    if past <= 0:
        return 0.0
    return float((recent / past - 1.0) * 100.0)


def _relative_strength(close_series: pd.Series, benchmark_ret_pct: float,
                       lookback: int = 20) -> float:
    """Stock's trailing return minus the benchmark's, in percentage points."""
    if len(close_series) < lookback + 1:
        return 0.0
    past = float(close_series.iloc[-lookback - 1])
    if past <= 0:
        return 0.0
    stock_ret = (float(close_series.iloc[-1]) / past - 1.0) * 100.0
    return round(stock_ret - benchmark_ret_pct, 3)


def _levels(entry: float, atr: float) -> tuple[float, float, float, float] | None:
    """ATR stop with a percentage floor, and three R-multiple targets."""
    stop = entry - ATR_STOP_MULT * atr
    floor = entry * (1.0 - MAX_STOP_PCT / 100.0)
    stop = max(stop, floor)
    r = entry - stop
    if r <= 0:
        return None
    t1, t2, t3 = (entry + m * r for m in TARGET_R_MULTIPLES)
    return stop, t1, t2, t3


def detect_setup(
    symbol: str,
    df: pd.DataFrame,
    regime,
    profile,
    benchmark_ret_pct: float = 0.0,
    sector: str = "UNKNOWN",
    session_complete: bool = True,
    adv_inr: float | None = None,
    daily_vol_pct: float | None = None,
) -> dict[str, Any] | None:
    """Return a candidate dict for `symbol`, or None.

    The candidate carries every input the shared ranker needs, so ranking stays a pure
    cross-sectional operation over a list of these dicts.

    Only three conditions reject outright here — insufficient data, broken trend
    structure, and a target that cannot pay for its own round trip. Everything else that
    used to be a hard gate (ADX 25, volume 1.5x, 52-week proximity 5%) is emitted as a
    *factor* for cross-sectional ranking instead. The measured funnel showed ADX alone
    rejecting 12 of 50 candidates with a cluster of near-misses at 21-24.5 against a hard
    25 — discarding a stock with ADX 24 and exceptional relative strength is a threshold
    artefact, not a judgement.
    """
    try:
        df = drop_forming_bar(df, session_complete)
        if df is None or len(df) < MIN_BARS:
            return None

        close_s = df["Close"]
        close = float(close_s.iloc[-1])
        if close <= 0:
            return None

        ema20 = float(ta_ema(close_s, 20).iloc[-1])
        ema50 = float(ta_ema(close_s, 50).iloc[-1])
        ema200 = float(ta_ema(close_s, 200).iloc[-1])

        # ── Hard gate 1: trend structure ────────────────────────────────────────
        # Binary and structural: a long-only swing strategy has no thesis below its own
        # long-term average. Rejected 30 of 50 in the measured funnel, as expected in a
        # bearish tape.
        if not (close > ema50 > ema200):
            return None

        high_20d = float(df["High"].rolling(20).max().iloc[-2])
        high_52w = float(df["High"].rolling(252, min_periods=150).max().iloc[-1])
        prev_close = float(close_s.iloc[-2])

        vol_20d = float(df["Volume"].iloc[-20:].mean())
        vol_5d = float(df["Volume"].iloc[-5:].mean())
        vol_ratio = vol_5d / vol_20d if vol_20d > 0 else 1.0

        atr = compute_atr(df, ATR_PERIOD)
        if atr <= 0:
            return None

        # ── Family selection ────────────────────────────────────────────────────
        family = None
        reason = ""

        is_breakout = close >= high_20d or close >= high_52w * 0.97
        near_ema20 = abs(close - ema20) / ema20 <= 0.03
        reclaimed = prev_close < ema20 <= close

        if is_breakout and getattr(regime, "allow_momentum", True):
            family = MOMENTUM_FAMILY
            reason = ("20-day high breakout" if close >= high_20d
                      else "52-week high proximity breakout")
        elif (near_ema20 or reclaimed) and getattr(regime, "allow_mean_reversion", True):
            family = PULLBACK_FAMILY
            reason = "Pullback to 20-EMA in intact uptrend"

        if family is None:
            return None

        # ── Levels ──────────────────────────────────────────────────────────────
        entry = round_to_tick(close)
        lv = _levels(entry, atr)
        if lv is None:
            return None
        stop, t1, t2, t3 = (round_to_tick(x) for x in lv)

        # ── Hard gate 2: cost ───────────────────────────────────────────────────
        # Arithmetic, not prediction. At swing scale this almost never binds (2.4%
        # required against a 15-90 day hold) — which is the correct outcome, and shows
        # the intraday cost problem was a TIMEFRAME problem, not a strategy problem.
        qty, rupee_risk = position_size(entry, stop, profile,
                                        size_multiplier=getattr(regime, "size_multiplier", 1.0))
        if qty <= 0:
            return None

        notional = qty * entry
        cm = cost_model_for(profile)          # delivery model — STT on both legs
        cost_pct = cm.round_trip_pct(
            symbol=symbol, price=entry, qty=qty,
            adv_inr=adv_inr, daily_vol_pct=daily_vol_pct,
        )
        target_pct = abs(t1 - entry) / entry * 100.0
        required_pct = cost_pct * profile.min_target_cost_multiple
        if target_pct < required_pct:
            logger.debug("[SWING][COST_GATE] %s target %.2f%% < required %.2f%%",
                         symbol, target_pct, required_pct)
            return None

        rr = round(abs(t1 - entry) / abs(entry - stop), 2)

        return {
            "symbol": symbol,
            "signal": "BUY",
            "setup_family": family,
            "setup": reason,
            "entry": entry,
            "stop_loss": stop,
            "target": t1,
            "target2": t2,
            "target3": t3,
            "rr": rr,
            "qty": qty,
            "rupee_risk": rupee_risk,
            "notional": round(notional, 2),
            "target_pct": round(target_pct, 4),
            "cost_pct": round(cost_pct, 4),
            "atr": round(float(atr), 2),
            "sector": sector,
            # ── ranker inputs ───────────────────────────────────────────────────
            "vol_ratio": round(vol_ratio, 3),
            "relative_strength": _relative_strength(close_s, benchmark_ret_pct),
            "momentum_persistence": round(_momentum_persistence(close_s), 3),
            "breakout_quality": round(_breakout_quality(close, high_20d, high_52w), 3),
            "trend_quality": round(float(min(compute_adx(df, 14) / 40.0, 1.0)), 3),
            # ── diagnostics ─────────────────────────────────────────────────────
            "ema20": round(ema20, 2),
            "ema50": round(ema50, 2),
            "ema200": round(ema200, 2),
            "high_20d": round(high_20d, 2),
            "high_52w": round(high_52w, 2),
            "adx": round(float(compute_adx(df, 14)), 1),
        }

    except Exception as exc:
        logger.error("[SWING] Setup detection failed for %s: %s", symbol, exc)
        return None


def strategy_allowed(family: str, regime) -> bool:
    """Regime gate at the family level.

    Momentum and mean reversion have opposite-signed expected value by regime. Firing
    both unconditionally guarantees roughly half the signal population is structurally
    counter-regime — the same reasoning the intraday engine applies to its own three
    triggers.
    """
    if family == MOMENTUM_FAMILY:
        return bool(getattr(regime, "allow_momentum", True))
    if family == PULLBACK_FAMILY:
        return bool(getattr(regime, "allow_mean_reversion", True))
    # Fail closed, not open (audit fix H11 — same fail-open trap intraday_service's
    # regime gate had before "Compression Breakout" was found bypassing it entirely).
    # Both real families are covered above; an unrecognized one is blocked, loudly,
    # rather than silently ungated.
    logger.warning("[SWING_REGIME] Unclassified family %r — blocking by default.", family)
    return False
