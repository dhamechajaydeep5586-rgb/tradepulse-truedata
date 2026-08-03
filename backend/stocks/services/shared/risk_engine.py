"""Position sizing and exposure control, shared by all engines.

Extracted from `intraday_service.position_size()`, which was a module-level function
inside one engine while the other two engines had no server-side sizing at all — swing
computed quantity in React (`ProSystem.jsx`, 1.5% of a user-typed capital) and the
performance report used a third, unrelated convention (a flat Rs.5,000 risk). Three
surfaces disagreed on the quantity of the same position.

Two sizing modes, chosen per profile:

**risk_parity** (intraday, swing) — `qty = (equity x risk%) / (entry - stop)`.
    Correct when a trade has a defined invalidation price. Equal-notional sizing would
    mean a 0.10%-stop trade and a 0.60%-stop trade carry 6x different risk on identical
    capital, so portfolio P&L ends up driven by stop width rather than by conviction.

**inverse_vol** (long-term) — `w_i proportional to 1/sigma_i`, normalised, capped.
    Correct when there is no meaningful entry stop. A 1-2 year position exits on
    fundamental deterioration or time, not on a price level, so risk-parity has no
    denominator to divide by. Volatility targeting needs only a volatility estimate,
    which is forecastable — unlike the edge estimate Kelly requires.
"""
from __future__ import annotations

import logging

import numpy as np

from .profiles import EngineProfile, allocated_equity

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# RISK-PARITY SIZING
# ══════════════════════════════════════════════════════════════════════════════

def position_size(
    entry: float,
    stop_loss: float,
    profile: EngineProfile,
    equity: float | None = None,
    size_multiplier: float = 1.0,
) -> tuple[int, float]:
    """Risk-parity size. Returns (qty, rupee_risk).

    `size_multiplier` is the regime scalar — `RegimeState.size_multiplier` — so the book
    is smaller in conditions the regime model rates as poor. Passing 1.0 disables it.

    qty is additionally capped by `max_position_pct` so an unusually tight stop cannot
    translate into an oversized notional.
    """
    try:
        entry = float(entry)
        stop_loss = float(stop_loss)
    except (TypeError, ValueError):
        return 0, 0.0

    risk_per_share = abs(entry - stop_loss)
    if risk_per_share <= 0 or entry <= 0:
        return 0, 0.0

    eq = allocated_equity(profile) if equity is None else float(equity)
    risk_budget = eq * (profile.risk_per_trade_pct / 100.0) * max(0.0, size_multiplier)
    qty = int(risk_budget // risk_per_share)

    max_notional = eq * (profile.max_position_pct / 100.0)
    qty = min(qty, int(max_notional // entry))

    if qty <= 0:
        return 0, 0.0
    return qty, round(qty * risk_per_share, 2)


# ══════════════════════════════════════════════════════════════════════════════
# INVERSE-VOLATILITY SIZING
# ══════════════════════════════════════════════════════════════════════════════

def inverse_vol_weights(
    vols_pct: dict[str, float],
    profile: EngineProfile,
) -> dict[str, float]:
    """Normalised 1/sigma weights, capped at `max_position_pct`.

    Capping then renormalising is done iteratively: naively capping once and
    renormalising pushes other weights back above the cap.
    """
    valid = {s: v for s, v in vols_pct.items() if v and v > 0}
    if not valid:
        return {}

    cap = profile.max_position_pct / 100.0
    raw = {s: 1.0 / v for s, v in valid.items()}
    total = sum(raw.values())
    w = {s: r / total for s, r in raw.items()}

    for _ in range(20):
        over = {s: x for s, x in w.items() if x > cap}
        if not over:
            break
        spare = sum(x - cap for x in over.values())
        under = {s: x for s, x in w.items() if x < cap}
        if not under:
            break
        under_total = sum(under.values())
        for s in over:
            w[s] = cap
        for s in under:
            w[s] += spare * (w[s] / under_total)

    return {s: round(x, 6) for s, x in w.items()}


def inverse_vol_quantities(
    prices: dict[str, float],
    vols_pct: dict[str, float],
    profile: EngineProfile,
    equity: float | None = None,
) -> dict[str, int]:
    """Share counts implied by inverse-vol weights."""
    eq = allocated_equity(profile) if equity is None else float(equity)
    weights = inverse_vol_weights(vols_pct, profile)
    out: dict[str, int] = {}
    for sym, w in weights.items():
        px = prices.get(sym, 0.0)
        if px > 0:
            qty = int((eq * w) // px)
            if qty > 0:
                out[sym] = qty
    return out


# ══════════════════════════════════════════════════════════════════════════════
# EXPOSURE CONTROL
# ══════════════════════════════════════════════════════════════════════════════

def gross_budget(profile: EngineProfile, equity: float | None = None) -> float:
    eq = allocated_equity(profile) if equity is None else float(equity)
    return eq * (profile.max_gross_pct / 100.0)


def fits_gross_budget(
    used: float, incoming_notional: float, profile: EngineProfile,
    equity: float | None = None,
) -> bool:
    """A per-name cap cannot bound a portfolio: five separately-reasonable positions
    can still add up to an unreasonable book."""
    return (used + incoming_notional) <= gross_budget(profile, equity)


def cross_engine_gross_exposure() -> dict[str, float]:
    """Notional currently committed by EVERY engine, keyed by engine name.

    Each engine previously sized as if it owned the whole account. This is the function
    that makes the combined book visible; callers should compare the total against
    `TOTAL_ACCOUNT_EQUITY`, not against their own slice.
    """
    from stocks.models import ShortTermSignal, SignalHistory

    out: dict[str, float] = {}

    for cat, key in (("intraday", "intraday"), ("long_term", "long_term")):
        total = 0.0
        rows = SignalHistory.objects.filter(
            category=cat,
            status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
        ).values("entry_price", "metadata")
        for r in rows:
            qty = float((r["metadata"] or {}).get("qty") or 0)
            total += qty * float(r["entry_price"] or 0)
        out[key] = round(total, 2)

    swing_total = 0.0
    for r in ShortTermSignal.objects.filter(
        status__in=[ShortTermSignal.Status.PENDING, ShortTermSignal.Status.ACTIVE,
                    ShortTermSignal.Status.TARGET1, ShortTermSignal.Status.TARGET2]
    ).values("entry_price", "qty"):
        swing_total += float(r.get("qty") or 0) * float(r["entry_price"] or 0)
    out["swing"] = round(swing_total, 2)

    out["total"] = round(sum(v for k, v in out.items() if k != "total"), 2)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# VOLATILITY TARGETING
# ══════════════════════════════════════════════════════════════════════════════

def volatility_scalar(
    realized_vol_pct: float,
    target_vol_pct: float,
    lo: float = 0.25,
    hi: float = 2.0,
) -> float:
    """Scale sizing so realised portfolio vol tracks a constant target.

    Clamped: an unclamped scalar levers up violently in quiet regimes, which is exactly
    when a volatility shock is most likely to arrive.
    """
    if realized_vol_pct <= 1e-6:
        return 1.0
    return float(np.clip(target_vol_pct / realized_vol_pct, lo, hi))
