"""
Historical signal replay — the bridge between the candle store and the backtester.

The live engine (`intraday_service.get_live_signals`) only works on *now*: it checks
market hours, reads the current quote, and writes to SignalHistory. To measure the
strategy you have to reconstruct what it WOULD have signalled at each past bar, using
only the information available at that bar.

Lookahead discipline is the whole point of this module:

  * At bar `i` the strategy is handed `df[:i+1]`. `_volume_profile_logic` internally
    discards its last row as the still-forming bar, so it actually decides on bar `i-1`
    and the signal is stamped at `i` — the first moment it could have been acted on.
  * The window is a trailing slice, never the full frame, so a decision can never see
    volume-profile levels built from bars that had not happened yet.
  * Ranking is applied per-timestamp across symbols, exactly as the live scan ranks a
    single cycle's candidate set — not across the whole history, which would leak.

Regime reconstruction: `historical_regime.py` builds a causal (no-lookahead) series of
market state from real NIFTY history, and `regime_lookup` below applies it via an
as-of merge — the trend/vol/bias in effect at each candidate's own timestamp, computed
using only data up to that point. Passing `regime_lookup=None` falls back to a
permissive neutral state throughout (the old behavior) — useful for isolating how much
of a result is attributable to the regime filter itself.

What is still NOT reproduced here:

  * The 5-minute scan cooldown and the 11:00 start, which throttle live signal count.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

MIN_BARS = 31          # matches the strategy's own minimum
WINDOW_BARS = 160      # trailing lookback; ~2 sessions of 5-min or ~6 of 15-min bars


def replay_symbol(
    symbol: str,
    df: pd.DataFrame,
    liquidity: dict | None = None,
    sector: str = "UNKNOWN",
    step: int = 1,
    regime_lookup: pd.DataFrame | None = None,
) -> list[dict]:
    """Every candidate the strategy would have produced for one symbol.

    Returns dicts carrying both the execution fields the backtester needs and the
    cross-sectional fields the ranking model needs.

    `regime_lookup`, if given, must be a DataFrame indexed by timestamp with columns
    trend_state, vol_state, directional_bias, allow_momentum, allow_mean_reversion,
    size_multiplier (the output of historical_regime.build_nifty_regime_series). The
    as-of match per bar is done in one vectorized pass up front rather than per bar in
    the loop below, since a per-bar merge_asof call would be the dominant cost.
    """
    from stocks.services.intraday_service import _volume_profile_logic

    if df is None or len(df) < MIN_BARS + 1:
        return []

    regime_by_bar = None
    if regime_lookup is not None and not regime_lookup.empty:
        from .historical_regime import lookup_asof
        regime_by_bar = lookup_asof(regime_lookup, df.index)
        regime_by_bar.index = df.index  # lookup_asof reorders by timestamp; realign 1:1

    out: list[dict] = []
    for i in range(MIN_BARS, len(df), step):
        window = df.iloc[max(0, i - WINDOW_BARS): i + 1]
        if len(window) < MIN_BARS:
            continue

        if regime_by_bar is not None:
            row = regime_by_bar.iloc[i]
            bias = row.get("directional_bias", "SIDEWAYS") or "SIDEWAYS"
            allow_mom = bool(row.get("allow_momentum", True))
            allow_mr = bool(row.get("allow_mean_reversion", True))
            size_mult = float(row.get("size_multiplier", 1.0) or 1.0)
        else:
            bias, allow_mom, allow_mr, size_mult = "SIDEWAYS", True, True, 1.0

        try:
            fired = _volume_profile_logic(
                symbol, window, nifty_bias=bias, liquidity=liquidity,
                size_multiplier=size_mult,
            )
        except Exception:
            continue

        for c in fired:
            # Regime-conditional trigger enablement (audit §2): a momentum trigger in a
            # regime that only permits mean reversion (or vice versa) is exactly the
            # counter-regime trade the live engine's strategy_allowed() gate declines —
            # applying it here is what makes this a real replay of that gate rather than
            # the permissive placeholder every prior run in this investigation used.
            from stocks.services.shared.regime import MOMENTUM_STRATEGIES, MEAN_REVERSION_STRATEGIES
            trigger_name = c["reason"].split("] ", 1)[-1]
            if trigger_name in MOMENTUM_STRATEGIES and not allow_mom:
                continue
            if trigger_name in MEAN_REVERSION_STRATEGIES and not allow_mr:
                continue

            out.append({
                "timestamp": df.index[i],
                "symbol": symbol,
                "direction": c["signal"],
                "entry": c["entry"],
                "stop_loss": c["stop_loss"],
                "target": c["target"],
                "reason": c["reason"],
                "vol_ratio": c.get("vol_ratio", 1.0),
                "target_pct": c.get("target_pct", 0.0),
                "cost_pct": c.get("cost_pct", 0.14),
                "qty_hint": c.get("qty", 0),
                "sector": sector,
                "directional_bias": bias,
            })
    return out


def rank_per_timestamp(candidates: list[dict], regime, universe_stats: dict,
                       max_per_cycle: int = 5, threshold: float | None = None) -> pd.DataFrame:
    """Apply the cross-sectional ranking model within each timestamp.

    Ranking must be scoped to one scan cycle. Scoring the entire history at once would
    let a candidate's percentile depend on setups from months later.
    """
    from stocks.services.shared.ranking import score_candidates, apply_threshold

    if not candidates:
        return pd.DataFrame()

    by_ts: dict = {}
    for c in candidates:
        by_ts.setdefault(c["timestamp"], []).append(c)

    kept: list[dict] = []
    for ts in sorted(by_ts):
        batch = by_ts[ts]
        for b in batch:
            b.setdefault("relative_strength", 0.0)
            b.setdefault("trend_quality", 0.0)
            b["signal"] = b["direction"]          # ranking model reads `signal`
        scored = score_candidates(batch, regime, universe_stats, {})
        qualified = apply_threshold(scored, threshold)

        # One position per symbol per cycle, best first.
        seen, picked = set(), []
        for c in qualified:
            if c["symbol"] in seen:
                continue
            seen.add(c["symbol"])
            picked.append(c)
            if len(picked) >= max_per_cycle:
                break
        kept.extend(picked)

    if not kept:
        return pd.DataFrame()
    return pd.DataFrame(kept).sort_values("timestamp").reset_index(drop=True)
