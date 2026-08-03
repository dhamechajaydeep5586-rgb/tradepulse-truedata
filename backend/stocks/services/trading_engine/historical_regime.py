"""
Causal (no-lookahead) reconstruction of the market regime, for backtesting only.

Every prior replay run in this investigation used a PERMISSIVE PLACEHOLDER regime
(neutral, momentum and mean-reversion both always allowed) because the live regime
model (`stocks.services.shared.regime.get_regime`) is built for wall-clock use — it
fetches "now," and its hysteresis is a Django cache comparison against "last time this
ran," which has no meaning when replaying the past. That gap was disclosed explicitly
in replay.py rather than silently faked. This module closes it.

The live regime model gates the strategy's mean-reversion trigger (Value Area
Rejection) on index direction, and gates which trigger FAMILY may fire at all
(momentum vs mean-reversion) on trend x volatility. Every backtest run so far let all
of that through unconditionally, which is a strictly looser filter than production —
so every trade count and P&L number produced until now is an upper bound on what the
live engine would actually have taken.

Design constraint: at bar i, only bars [0..i] may be used. This is enforced by
computing every input as a `pandas.rolling()` series over the WHOLE frame in one call
(rolling windows are inherently backward-looking — the value at row i depends only on
rows <= i) rather than by re-slicing and recomputing per row, which is both slower
(O(n^2)) and easier to get wrong.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..signal_utils import compute_session_vwap

# Same 2-axis thresholds as the live model (stocks/services/shared/regime.py) —
# duplicated rather than imported because the live functions return only the LATEST
# value (a scalar), not a full causal series, and rewriting them to do both would risk
# behavior drift in the live path for the sake of a backtest-only need.
_TREND_UP, _TREND_DOWN = 0.6, -0.6
_VOL_EXPANDING, _VOL_CONTRACTING = 0.5, -0.5


def compute_adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Full causal ADX series — same math as signal_utils.compute_adx, not just its
    last value. `.rolling()` only ever looks backward, so this is lookahead-safe."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_high, prev_low, prev_close = high.shift(1), low.shift(1), close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr_smooth = tr.rolling(window=period).mean()
    plus_dm_smooth = plus_dm.rolling(window=period).mean()
    minus_dm_smooth = minus_dm.rolling(window=period).mean()

    plus_di = 100 * (plus_dm_smooth / tr_smooth.replace(0, np.nan))
    minus_di = 100 * (minus_dm_smooth / tr_smooth.replace(0, np.nan))
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.rolling(window=period).mean()


def compute_atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Full causal ATR series."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def build_breadth_series(bars_by_symbol: dict[str, pd.DataFrame]) -> pd.Series:
    """% of the (available) universe trading above its own session VWAP, per bar.

    Session VWAP is itself cumulative-within-day, so this is causal by construction —
    no future bar of any symbol ever contributes to another symbol's reading at time t.
    """
    per_symbol_flags = []
    for sym, df in bars_by_symbol.items():
        if df is None or len(df) < 5:
            continue
        try:
            vwap = compute_session_vwap(df)
            above = (df["Close"] > vwap).astype(float)
            per_symbol_flags.append(above)
        except Exception:
            continue

    if not per_symbol_flags:
        return pd.Series(dtype=float)

    combined = pd.concat(per_symbol_flags, axis=1)
    return combined.mean(axis=1, skipna=True) * 100.0


def build_nifty_regime_series(nifty_df: pd.DataFrame, breadth: pd.Series | None = None) -> pd.DataFrame:
    """Causal regime state at every NIFTY bar: trend axis x volatility axis, with
    2-consecutive-reads hysteresis (mirrors `_apply_hysteresis` in the live model, but
    walked forward through history instead of compared against a cache).

    Returns a DataFrame indexed like `nifty_df`, columns:
        trend_state, vol_state, directional_bias, allow_momentum,
        allow_mean_reversion, size_multiplier
    """
    df = nifty_df.copy()
    price = df["Close"]

    vwap = compute_session_vwap(df)
    atr = compute_atr_series(df, 14)
    vwap_dist_atr = ((price - vwap) / atr.replace(0, np.nan)).fillna(0.0)

    adx = compute_adx_series(df, 14).fillna(0.0)
    adx_roll_mean = adx.rolling(60, min_periods=10).mean()
    adx_roll_std = adx.rolling(60, min_periods=10).std().replace(0, np.nan)
    adx_z = ((adx - adx_roll_mean) / adx_roll_std).fillna((adx - 20.0) / 10.0)

    if breadth is not None and not breadth.empty:
        breadth_aligned = breadth.reindex(df.index, method="ffill").fillna(50.0)
    else:
        breadth_aligned = pd.Series(50.0, index=df.index)
    breadth_z = (breadth_aligned - 50.0) / 20.0

    trend_score = (
        0.4 * adx_z.abs() * np.sign(vwap_dist_atr.replace(0, 1e-9))
        + 0.35 * vwap_dist_atr.clip(-3, 3)
        + 0.25 * breadth_z
    ).clip(-3, 3)

    log_ret = np.log(price / price.shift(1))
    rv5 = log_ret.rolling(5 * 25, min_periods=25).std()      # ~5 sessions of 15-min bars
    rv20 = log_ret.rolling(20 * 25, min_periods=100).std()   # ~20 sessions
    rv_ratio = (rv5 / rv20.replace(0, np.nan)).fillna(1.0)
    vol_score = ((rv_ratio - 1.0) * 4.0).clip(-3, 3)

    trend_state = pd.Series(
        np.select([trend_score > _TREND_UP, trend_score < _TREND_DOWN], ["UP", "DOWN"], default="NEUTRAL"),
        index=df.index,
    )
    vol_state = pd.Series(
        np.select([vol_score > _VOL_EXPANDING, vol_score < _VOL_CONTRACTING],
                  ["EXPANDING", "CONTRACTING"], default="NORMAL"),
        index=df.index,
    )

    # ── Hysteresis: walked forward, not cache-compared ──────────────────────────────
    # A state change is only adopted once its (trend, vol) pair has been the CANDIDATE
    # on two consecutive bars — otherwise the previous held state persists. Removes the
    # boundary flip-flop a single noisy bar would otherwise cause.
    held_trend, held_vol = "NEUTRAL", "NORMAL"
    pending_key, pending_seen = None, 0
    out_trend, out_vol = [], []

    for t_state, v_state in zip(trend_state, vol_state):
        key = (t_state, v_state)
        if key == (held_trend, held_vol):
            pending_key, pending_seen = None, 0
        elif key == pending_key:
            pending_seen += 1
            if pending_seen >= 2:
                held_trend, held_vol = t_state, v_state
                pending_key, pending_seen = None, 0
        else:
            pending_key, pending_seen = key, 1
        out_trend.append(held_trend)
        out_vol.append(held_vol)

    df["trend_state"] = out_trend
    df["vol_state"] = out_vol
    df["directional_bias"] = np.select(
        [df["trend_state"] == "UP", df["trend_state"] == "DOWN"], ["BULLISH", "BEARISH"], default="SIDEWAYS"
    )

    trending = df["trend_state"].isin(["UP", "DOWN"])
    expanding = df["vol_state"] == "EXPANDING"
    contracting = df["vol_state"] == "CONTRACTING"
    df["allow_momentum"] = trending & ~contracting
    df["allow_mean_reversion"] = (~trending) & ~expanding

    df["size_multiplier"] = np.select(
        [trending & expanding, contracting & ~trending, expanding & ~trending],
        [1.25, 1.0, 0.5], default=0.75,
    )

    return df[["trend_state", "vol_state", "directional_bias",
                "allow_momentum", "allow_mean_reversion", "size_multiplier"]]


def lookup_asof(regime_df: pd.DataFrame, timestamps: pd.DatetimeIndex) -> pd.DataFrame:
    """Vectorized as-of lookup: for each candidate timestamp, the most recent regime
    row at or before it. `merge_asof` requires both sides sorted — regime_df already
    is (it's indexed by the NIFTY series in chronological order).
    """
    right = regime_df.reset_index().rename(columns={regime_df.index.name or "index": "regime_ts"})
    left = pd.DataFrame({"ts": pd.DatetimeIndex(timestamps)}).sort_values("ts")
    merged = pd.merge_asof(left, right.sort_values("regime_ts"),
                           left_on="ts", right_on="regime_ts", direction="backward")
    return merged.set_index("ts")
