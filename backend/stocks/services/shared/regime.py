"""
Two-axis market regime model.

See doc/INSTITUTIONAL_AUDIT_INTRADAY.md §2. The previous model collapsed the market into
one bit (SIDEWAYS/TRENDING) from two hard thresholds, had no volatility axis, no
hysteresis, and — critically — nothing consumed its output.

The reason regime matters more than entry quality here: the intraday engine runs three
triggers of two opposite types. POC Flip and VA Breakout are momentum; VA Rejection is
mean reversion. Firing all three unconditionally means roughly half the signal
population is structurally counter-regime at any moment.

    Unconditional EV = 0.40(+0.15R) + 0.60(-0.25R) = -0.09R     (negative)
    Regime-gated  EV = +0.15R on the 40% of days that suit it   (positive)

That converts a negative-expectancy system to positive without improving prediction at
all — purely by declining trades already known to be poor.

Axis 1 (trend):      ADX, price vs session VWAP normalised by ATR, market breadth
Axis 2 (volatility): realized-vol expansion RV(5)/RV(20), ATR percentile, India VIX

Each input is z-scored against its own rolling history so thresholds adapt instead of
being hardcoded, then averaged within axis. Hysteresis requires a state to persist two
consecutive reads before switching, which removes boundary whipsaw.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd
from django.core.cache import cache

from .. import market_data
from ..signal_utils import (
    compute_adx, compute_atr, compute_session_vwap, load_symbols_from_stock_table,
)

logger = logging.getLogger(__name__)

REGIME_CACHE_KEY = "intraday_regime_state_v2"
REGIME_HISTORY_KEY = "intraday_regime_pending_switch"
REGIME_TTL = 300  # 5 min

# TrueData symbol names, not numeric tokens — see truedata_service.py module docstring.
NIFTY50_TOKEN = "NIFTY 50"
INDIA_VIX_TOKEN = "INDIA VIX"  # optional; model degrades gracefully if unavailable. Unverified name — confirm via getAllSymbols?segment=in.

# Trigger families. Momentum setups need trend + expanding vol; mean reversion needs
# range + contracting vol. This mapping is what actually gates signal generation.
# Audit fix H11: "Compression Breakout"/"Compression Breakdown" (intraday_service.py's
# Trigger 4 — volatility-compression breakout continuation, same directional-breakout
# shape as POC Flip / VA Breakout) were missing from this set entirely, so
# strategy_allowed()'s old fail-open default let the whole trigger family bypass
# regime gating regardless of market state.
MOMENTUM_STRATEGIES = {"POC Bullish Flip", "POC Bearish Flip",
                       "Value Area Bullish Breakout", "Value Area Bearish Breakdown",
                       "Compression Breakout", "Compression Breakdown"}
MEAN_REVERSION_STRATEGIES = {"Value Area Low Rejection", "Value Area High Rejection"}


@dataclass
class RegimeState:
    trend_state: str = "NEUTRAL"       # UP / DOWN / NEUTRAL
    vol_state: str = "NORMAL"          # EXPANDING / CONTRACTING / NORMAL
    trend_score: float = 0.0           # z-composite, roughly -3..+3
    vol_score: float = 0.0
    directional_bias: str = "SIDEWAYS"  # BULLISH / BEARISH / SIDEWAYS
    breadth_pct: float = 50.0
    adx: float = 0.0
    vix: float | None = None
    rv_ratio: float = 1.0
    allow_momentum: bool = True
    allow_mean_reversion: bool = True
    size_multiplier: float = 1.0
    source: str = "computed"

    def as_dict(self) -> dict:
        return asdict(self)


def _safe_z(value: float, series: pd.Series) -> float:
    """Z-score `value` against `series`, clamped to +/-3 and 0.0 when degenerate."""
    try:
        s = pd.Series(series).dropna()
        if len(s) < 10:
            return 0.0
        mu, sd = float(s.mean()), float(s.std())
        if sd <= 1e-9:
            return 0.0
        return float(np.clip((value - mu) / sd, -3.0, 3.0))
    except Exception:
        return 0.0


def _realized_vol(close: pd.Series, window: int) -> float:
    """Annualisation is irrelevant here — only the ratio of two windows is used."""
    r = np.log(close / close.shift(1)).dropna()
    if len(r) < window:
        return float(r.std()) if len(r) > 1 else 0.0
    return float(r.tail(window).std())


def _market_breadth(svc, symbols: list[str], sample: int = 25,
                    horizon: str = "intraday") -> float:
    """% of a sampled subset trading above their reference level.

    Intraday: above session VWAP. Swing/structural: above the 50-day moving average —
    the standard breadth measure at that horizon, and the only one that is meaningful
    outside market hours (a session-VWAP breadth computed on a Sunday is noise).

    Sampled rather than exhaustive: breadth is a low-precision statistic and the Angel
    One candle endpoint is rate limited to ~1 call/sec, so a full NIFTY100 sweep would
    cost more than the signal is worth.
    """
    if not symbols:
        return 50.0
    step = max(1, len(symbols) // sample)
    subset = symbols[::step][:sample]

    token_map = svc.get_token_map(subset, exchange="NSE") or {}
    if not token_map:
        return 50.0

    is_intraday = horizon == "intraday"
    # A wider lookback than "today only" / "exactly 120 days" is harmless here: for the
    # intraday branch, compute_session_vwap groups by calendar day and .iloc[-1] pulls
    # today's session regardless of how much prior history rides along in the frame
    # (same reasoning already relied on by intraday_service.py's own candle_store use).
    if is_intraday:
        interval, min_bars, lookback_days = "FIVE_MINUTE", 3, 1
    else:
        interval, min_bars, lookback_days = "ONE_DAY", 55, 120

    above = total = 0
    for sym, token in token_map.items():
        try:
            df = market_data.request_candles(
                svc, sym, token, interval, lookback_days=lookback_days, exchange="NSE"
            )
            if df is None or df.empty or len(df) < min_bars:
                continue
            if is_intraday:
                ref = float(compute_session_vwap(df).iloc[-1])
            else:
                ref = float(df["Close"].rolling(50).mean().iloc[-1])
            if not np.isfinite(ref):
                continue
            total += 1
            if float(df["Close"].iloc[-1]) > ref:
                above += 1
        except Exception:
            continue

    return (above / total * 100.0) if total else 50.0


def _fetch_vix(svc) -> float | None:
    try:
        df = market_data.request_candles(
            svc, "INDIA_VIX", INDIA_VIX_TOKEN, "FIVE_MINUTE", lookback_days=5, exchange="NSE"
        )
        if df is None or df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception:
        return None


def _apply_hysteresis(new: RegimeState, cache_key: str = REGIME_CACHE_KEY,
                      hist_key: str = REGIME_HISTORY_KEY,
                      ttl: int = REGIME_TTL) -> RegimeState:
    """Require a state change to persist across two consecutive reads before adopting it.

    Without this, a composite sitting near a boundary flips on noise and produces
    exactly the whipsaw the regime filter is supposed to prevent.
    """
    prev = cache.get(cache_key)
    if not prev:
        return new

    prev_key = (prev.get("trend_state"), prev.get("vol_state"))
    new_key = (new.trend_state, new.vol_state)
    if prev_key == new_key:
        cache.delete(hist_key)
        return new

    pending = cache.get(hist_key)
    if pending == list(new_key) or pending == new_key:
        cache.delete(hist_key)
        return new

    # First sighting of the new state — hold the previous one for one more cycle.
    cache.set(hist_key, new_key, timeout=ttl * 2)
    held = RegimeState(**{k: v for k, v in prev.items() if k in RegimeState.__annotations__})
    held.source = "hysteresis_hold"
    return held


def _resolve_permissions(state: RegimeState, long_only: bool = False) -> RegimeState:
    """Map the 2-axis state onto which trigger families may fire, and at what size.

    `long_only` fixes a bug found live on 2026-07-26: a DOWN/BEARISH regime still
    emitted 6 long momentum signals for the swing engine, because "momentum allowed"
    was defined as merely TRENDING — true for both UP and DOWN. That is correct for
    intraday, which can short a downtrend. It is wrong for a buy-only book: "momentum
    allowed" in a falling index means "buy breakouts against the market," which the
    original design did not intend to permit.

    For long_only engines, momentum requires the trend to be UP specifically, not
    merely trending in either direction.
    """
    trending = state.trend_state in ("UP", "DOWN")
    expanding = state.vol_state == "EXPANDING"
    contracting = state.vol_state == "CONTRACTING"

    # Momentum wants a trend and does badly in a contracting range. A long-only book
    # additionally requires that trend to be UP — see docstring above.
    trend_ok_for_momentum = (state.trend_state == "UP") if long_only else trending
    state.allow_momentum = trend_ok_for_momentum and not contracting
    # Mean reversion wants a range; an expanding trend is where it gets run over.
    state.allow_mean_reversion = (not trending) and not expanding

    if trending and expanding:
        state.size_multiplier = 1.25      # cleanest momentum conditions
    elif contracting and not trending:
        state.size_multiplier = 1.0       # clean mean-reversion conditions
    elif expanding and not trending:
        state.size_multiplier = 0.5       # choppy and violent — worst of both
    else:
        state.size_multiplier = 0.75
    return state


def get_regime(svc=None, use_cache: bool = True, profile=None) -> RegimeState:
    """Current two-axis regime for the given engine profile.

    The composite, z-scoring and hysteresis are horizon-agnostic and shared. What
    changes per horizon is the INPUT: intraday reads 5-minute bars against session VWAP;
    swing and structural read daily bars against the 50-EMA, with volatility windows in
    days rather than in 5-minute bars. Each horizon gets its own cache entry and TTL —
    serving an intraday regime to a swing scan would gate a 90-day position on this
    morning's chop.
    """
    if profile is None:
        from .profiles import INTRADAY
        profile = INTRADAY
    horizon = profile.regime_horizon
    lookback_days = profile.regime_lookback_days
    cache_key = f"{REGIME_CACHE_KEY}_{horizon}"
    hist_key = f"{REGIME_HISTORY_KEY}_{horizon}"
    ttl = profile.regime_ttl_secs

    if use_cache:
        cached = cache.get(cache_key)
        if cached:
            s = RegimeState(**{k: v for k, v in cached.items()
                               if k in RegimeState.__annotations__})
            s.source = "cache"
            return s

    if svc is None:
        from ..truedata_service import get_truedata_instance
        svc = get_truedata_instance()
    if svc is None:
        logger.warning("[REGIME] No broker service — defaulting to permissive NEUTRAL.")
        return _resolve_permissions(RegimeState(source="no_service"), long_only=profile.long_only)

    try:
        is_intraday = horizon == "intraday"

        if is_intraday:
            df = market_data.request_candles(
                svc, "NIFTY50_INDEX", NIFTY50_TOKEN, "FIVE_MINUTE",
                lookback_days=10, exchange="NSE",
            )
            min_bars = 60
        else:
            # +60 buffer preserved from the direct-call version (weekend/holiday slack).
            df = market_data.request_candles(
                svc, "NIFTY50_INDEX", NIFTY50_TOKEN, "ONE_DAY",
                lookback_days=lookback_days + 60, exchange="NSE",
            )
            min_bars = 120

        if df is None or df.empty or len(df) < min_bars:
            return _resolve_permissions(RegimeState(source="insufficient_data"), long_only=profile.long_only)

        # Drop the forming bar for the same reason signal generation does.
        df = df.iloc[:-1]
        close = df["Close"]
        price = float(close.iloc[-1])

        # ── Axis 1: trend ────────────────────────────────────────────────────────
        adx = compute_adx(df, 14)
        atr = compute_atr(df, 14)
        if is_intraday:
            # Session VWAP is the right anchor intraday: it is what execution is
            # benchmarked against and it resets daily.
            anchor = float(compute_session_vwap(df).iloc[-1])
        else:
            # On daily bars a session VWAP is meaningless — the equivalent "where is
            # price relative to its own centre of gravity" measure is distance from the
            # 50-day EMA, normalised by ATR so it is comparable across regimes.
            anchor = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        vwap_dist_atr = ((price - anchor) / atr) if atr > 0 else 0.0

        adx_hist = pd.Series([compute_adx(df.iloc[:i], 14) for i in range(60, len(df), 5)])
        adx_z = _safe_z(adx, adx_hist) if len(adx_hist) >= 10 else (adx - 20.0) / 10.0

        symbols = load_symbols_from_stock_table()
        breadth = _market_breadth(svc, symbols, horizon=horizon)
        breadth_z = (breadth - 50.0) / 20.0

        # ADX is unsigned; direction comes from VWAP distance and breadth.
        trend_score = float(np.clip(
            0.4 * abs(adx_z) * np.sign(vwap_dist_atr)
            + 0.35 * np.clip(vwap_dist_atr, -3, 3)
            + 0.25 * breadth_z,
            -3, 3,
        ))

        if trend_score > 0.6:
            trend_state = "UP"
        elif trend_score < -0.6:
            trend_state = "DOWN"
        else:
            trend_state = "NEUTRAL"

        # ── Axis 2: volatility ───────────────────────────────────────────────────
        # Windows are expressed in BARS. Intraday runs on 5-min bars (75/session), so a
        # "5-day" window is 5x75; on daily bars it is simply 5.
        _w = (5 * 75, 20 * 75) if is_intraday else (5, 20)
        rv5, rv20 = _realized_vol(close, _w[0]), _realized_vol(close, _w[1])
        rv_ratio = (rv5 / rv20) if rv20 > 1e-9 else 1.0
        vix = _fetch_vix(svc)

        vol_score = float(np.clip((rv_ratio - 1.0) * 4.0, -3, 3))
        if vix is not None:
            # Blend in the level only as a mild tilt; RV ratio is the primary signal.
            vol_score = float(np.clip(0.75 * vol_score + 0.25 * ((vix - 15.0) / 5.0), -3, 3))

        if vol_score > 0.5:
            vol_state = "EXPANDING"
        elif vol_score < -0.5:
            vol_state = "CONTRACTING"
        else:
            vol_state = "NORMAL"

        if trend_state == "UP":
            bias = "BULLISH"
        elif trend_state == "DOWN":
            bias = "BEARISH"
        else:
            bias = "SIDEWAYS"

        state = RegimeState(
            trend_state=trend_state, vol_state=vol_state,
            trend_score=round(trend_score, 3), vol_score=round(vol_score, 3),
            directional_bias=bias, breadth_pct=round(breadth, 1),
            adx=round(adx, 2), vix=round(vix, 2) if vix is not None else None,
            rv_ratio=round(rv_ratio, 3),
        )
        state = _resolve_permissions(_apply_hysteresis(state, cache_key, hist_key, ttl), long_only=profile.long_only)
        cache.set(cache_key, state.as_dict(), timeout=ttl)

        logger.info(
            "[REGIME][" + horizon + "] trend=%s(%.2f) vol=%s(%.2f) bias=%s breadth=%.0f%% adx=%.1f "
            "rv=%.2f momentum=%s meanrev=%s size=%.2fx",
            state.trend_state, state.trend_score, state.vol_state, state.vol_score,
            state.directional_bias, state.breadth_pct, state.adx, state.rv_ratio,
            state.allow_momentum, state.allow_mean_reversion, state.size_multiplier,
        )
        return state

    except Exception as exc:
        logger.error("[REGIME] computation failed: %s", exc)
        return _resolve_permissions(RegimeState(source="error"), long_only=profile.long_only)


def strategy_allowed(strategy_reason: str, regime: RegimeState) -> bool:
    """Is this trigger family permitted in the current regime?

    Audit fix H11: the fallback used to be `return True` — any trigger name not yet
    added to MOMENTUM_STRATEGIES/MEAN_REVERSION_STRATEGIES silently bypassed regime
    gating entirely rather than failing safe (this is exactly how "Compression
    Breakout"/"Compression Breakdown" went ungated for however long they existed
    before being added above). Fails closed instead: an unclassified trigger is
    blocked, loudly, rather than silently ungated — a missing classification now
    shows up as "this trigger never fires" instead of "this trigger ignores regime."
    """
    if strategy_reason in MOMENTUM_STRATEGIES:
        return regime.allow_momentum
    if strategy_reason in MEAN_REVERSION_STRATEGIES:
        return regime.allow_mean_reversion
    logger.warning(
        "[REGIME] Unclassified trigger '%s' is not in MOMENTUM_STRATEGIES or "
        "MEAN_REVERSION_STRATEGIES — blocking by default instead of bypassing "
        "regime gating. Add it to the correct set in shared/regime.py.",
        strategy_reason,
    )
    return False
