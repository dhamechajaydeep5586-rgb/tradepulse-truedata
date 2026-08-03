"""
Cross-sectional 0-100 signal ranking.

See doc/INSTITUTIONAL_AUDIT_INTRADAY.md §6. The prior "score" was three hardcoded
constants (4.5/4.0/3.5) attached to trigger type, compared with max() over a
single-element list — a no-op that never ranked anything.

Design principle: rank CROSS-SECTIONALLY, not against absolute thresholds. An absolute
bar like `vol_ratio > 1.5` does not adapt — on a quiet day nothing qualifies, on a
volatile day everything does. Z-scoring each factor within the day's own candidate set
makes selectivity automatically relative to the opportunity actually available.

The threshold is deliberately allowed to reject every candidate. Producing zero signals
on a poor day is a feature of a professional system, and the exact opposite of the old
`relaxed` fallback, which *lowered* standards precisely when conditions were worst.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from django.conf import settings

logger = logging.getLogger(__name__)

# Weights sum to 100. Ordered by the strength of published evidence behind each factor:
# cross-sectional relative strength is the best-replicated equity anomaly, so it carries
# the most weight; reward/risk is included because it directly encodes the cost gate.
FACTOR_WEIGHTS = {
    "relative_strength": 20,
    "market_alignment": 15,
    "reward_risk": 15,
    "volume_confirmation": 12,
    "trend_quality": 12,
    "sector_strength": 10,
    "liquidity": 8,
    "volatility_fit": 8,
}

MIN_SCORE_THRESHOLD = float(getattr(settings, "INTRADAY_MIN_RANK_SCORE", 65.0))


def _pct_rank(values: list[float]) -> list[float]:
    """Percentile rank in [0, 1]. Flat input maps to 0.5 rather than dividing by zero."""
    s = pd.Series(values, dtype="float64")
    if len(s) <= 1 or s.nunique() <= 1:
        return [0.5] * len(s)
    return (s.rank(pct=True)).tolist()


def _alignment_score(cand: dict, regime) -> float:
    """Does this signal's direction agree with the index? Returns 0..1.

    Prefers a per-candidate `directional_bias` when present — set by the historical
    replay to the REAL market state at that candidate's own timestamp (see
    trading_engine/historical_regime.py) — over the single `regime` object passed for
    the whole batch, which in live use is one snapshot for the current scan cycle but
    in backtest use may be a placeholder covering an entire multi-month replay.
    """
    bias = cand.get("directional_bias") or getattr(regime, "directional_bias", "SIDEWAYS")
    is_buy = cand.get("signal") == "BUY"
    if bias == "SIDEWAYS":
        return 0.5
    if (bias == "BULLISH" and is_buy) or (bias == "BEARISH" and not is_buy):
        return 1.0
    return 0.0


def _volatility_fit(atr_pct: float) -> float:
    """Penalise both extremes: too quiet cannot reach target, too wild blows the stop.

    Peaks around 1.5% daily ATR, which is a normal large-cap intraday range.
    """
    if atr_pct <= 0:
        return 0.5
    ideal = 1.5
    return float(np.clip(1.0 - abs(atr_pct - ideal) / (ideal * 2.0), 0.0, 1.0))


def score_candidates(
    candidates: list[dict],
    regime,
    universe_stats: dict | None = None,
    sector_strength: dict | None = None,
    profile=None,
) -> list[dict]:
    """Attach `rank_score` (0-100) and per-factor detail to each candidate.

    Returns the list sorted best-first. Does NOT apply the threshold — selection is the
    caller's responsibility so the rejected set stays visible for measurement.

    `profile` selects the weight set. Every available factor is computed regardless; only
    those named in `profile.factor_weights` contribute, and the weighted sum is
    renormalised over the factors actually present. That keeps the 0-100 scale meaningful
    when an engine supplies a factor another engine does not (swing has
    `momentum_persistence` and `breakout_quality`; intraday has `market_alignment` and
    `reward_risk`), without every engine needing to know about every other engine's
    inputs.
    """
    if not candidates:
        return []

    if profile is None:
        from .profiles import INTRADAY
        profile = INTRADAY

    universe_stats = universe_stats or {}
    sector_strength = sector_strength or {}
    weights = dict(profile.factor_weights)

    # Cross-sectional inputs, ranked within this scan's candidate set.
    rs_rank = _pct_rank([c.get("relative_strength", 0.0) for c in candidates])
    vol_rank = _pct_rank([c.get("vol_ratio", 1.0) for c in candidates])
    rr_rank = _pct_rank([c.get("target_pct", 0.0) / max(c.get("cost_pct", 0.14), 1e-6)
                         for c in candidates])
    trend_rank = _pct_rank([c.get("trend_quality", 0.0) for c in candidates])
    liq_rank = _pct_rank([
        universe_stats.get(c["symbol"], {}).get("adv_inr", 0.0) for c in candidates
    ])
    # Swing-specific inputs. Absent for intraday, in which case _pct_rank returns a
    # flat 0.5 and the factor is not in intraday's weight set anyway.
    mom_rank = _pct_rank([c.get("momentum_persistence", 0.0) for c in candidates])
    brk_rank = _pct_rank([c.get("breakout_quality", 0.0) for c in candidates])

    for i, c in enumerate(candidates):
        sym = c["symbol"]
        atr_pct = universe_stats.get(sym, {}).get("daily_vol_pct", 1.5)
        sector = c.get("sector", "UNKNOWN")

        available = {
            "relative_strength": rs_rank[i],
            "market_alignment": _alignment_score(c, regime),
            "reward_risk": rr_rank[i],
            "volume_confirmation": vol_rank[i],
            "trend_quality": trend_rank[i],
            "sector_strength": float(np.clip(
                c.get("sector_strength", sector_strength.get(sector, 0.5)), 0.0, 1.0)),
            "liquidity": liq_rank[i],
            "volatility_fit": _volatility_fit(atr_pct),
            "momentum_persistence": mom_rank[i],
            "breakout_quality": brk_rank[i],
        }

        factors = {k: available[k] for k in weights if k in available}
        wsum = sum(weights[k] for k in factors) or 1.0
        score = sum(weights[k] * v for k, v in factors.items()) * (100.0 / wsum)

        c["rank_score"] = round(float(score), 2)
        c["rank_factors"] = {k: round(float(v), 3) for k, v in factors.items()}

    candidates.sort(key=lambda x: x["rank_score"], reverse=True)
    return candidates


def apply_threshold(
    candidates: list[dict],
    threshold: float | None = None,
    profile=None,
) -> list[dict]:
    """Keep only candidates clearing the quality bar. May legitimately return [].

    Returning nothing on a poor day is the intended behaviour and the direct replacement
    for the swing engine's `relaxed` fallback, which lowered every threshold precisely
    when conditions were worst — and additionally skipped the bearish-market gate.
    """
    if threshold is not None:
        t = threshold
    elif profile is not None:
        t = profile.min_rank_score
    else:
        t = MIN_SCORE_THRESHOLD
    kept = [c for c in candidates if c.get("rank_score", 0.0) >= t]
    if candidates and not kept:
        logger.info(
            "[RANK] No candidate cleared %.0f (best was %.1f) — emitting nothing.",
            t, candidates[0].get("rank_score", 0.0),
        )
    return kept
