"""EngineProfile — the single place every tunable threshold lives.

Before this module, thresholds were scattered across four locations with three different
override mechanisms: module-level constants read from `settings` (intraday), a JSON file
whose keys were never actually read (`strategy_config.json`, swing), and bare literals in
function bodies (long-term). The same concept — "minimum liquidity" — existed as
`MIN_ADV_INR`, as `minimum_liquidity_floor`, and as `vol > 50000`, with no relationship
between them.

Design rules for anything added here:

1. **Downstream code never reads `settings` or a literal.** It reads a profile field. That
   is what makes a threshold change auditable and testable rather than a grep exercise.
2. **Every field is overridable via Django settings** using the documented naming pattern,
   so ops can retune without a deploy.
3. **A profile is frozen.** Engines must not mutate shared configuration at runtime; a
   scan that needs a different value asks for a different profile.

The three profiles differ because the strategies differ in holding period, and holding
period drives almost everything else — cost tolerance, liquidity requirement, how stale
data may be, and how many positions can be monitored at the required cadence.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from django.conf import settings


def _s(name: str, default):
    """Read `settings.<name>`, coercing to the default's type. Absent → default."""
    val = getattr(settings, name, None)
    if val is None:
        return default
    try:
        return type(default)(val)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class EngineProfile:
    """Complete configuration for one trading engine."""

    name: str

    # ── Universe ────────────────────────────────────────────────────────────────
    index: str
    bar_interval: str
    min_adv_inr: float
    min_price_inr: float
    max_spread_bps: float
    universe_ttl_secs: int

    # ── Regime ──────────────────────────────────────────────────────────────────
    regime_horizon: str          # "intraday" | "swing" | "structural"
    regime_ttl_secs: int
    regime_bar_interval: str
    regime_lookback_days: int

    # ── Ranking ─────────────────────────────────────────────────────────────────
    factor_weights: Mapping[str, float]
    min_rank_score: float

    # ── Portfolio ───────────────────────────────────────────────────────────────
    max_positions: int
    max_per_sector: int
    max_per_cluster: int
    max_per_promoter_group_pct: float
    max_position_pct: float
    corr_lookback_days: int
    corr_threshold: float
    max_portfolio_beta: float | None

    # ── Risk / sizing ───────────────────────────────────────────────────────────
    sizing_mode: str             # "risk_parity" | "inverse_vol"
    risk_per_trade_pct: float
    max_gross_pct: float
    equity_share_pct: float      # this engine's slice of total account equity
    daily_loss_limit_pct: float | None
    strategy_drawdown_limit_pct: float | None

    # ── Cost model ──────────────────────────────────────────────────────────────
    round_trip_cost_pct: float
    min_target_cost_multiple: float

    # ── Calendar ────────────────────────────────────────────────────────────────
    earnings_blackout_before: int = 0   # sessions before results
    earnings_blackout_after: int = 0    # sessions after results
    max_fundamental_staleness_days: int | None = None

    # ── Lifecycle ───────────────────────────────────────────────────────────────
    time_stop_minutes: int | None = None
    pending_expiry_days: int | None = None

    # Can this engine take a SELL/short position at all? Intraday can; swing and
    # long-term are both buy-only (confirmed against the current implementation — see
    # short_term_stock.md / long_term_stock.md, "no SELL/short path anywhere"). This
    # matters because the regime model's `allow_momentum` was originally direction-
    # agnostic: it permitted momentum entries whenever the market was TRENDING, whether
    # UP or DOWN. For intraday that's correct (a DOWN trend is a valid short-momentum
    # regime). For a long-only book it means "buy breakouts while the index falls" —
    # discovered live on 2026-07-26, when a DOWN/BEARISH regime still emitted 6 long
    # momentum signals. See `shared/regime.py::_resolve_permissions`.
    long_only: bool = False

    def with_overrides(self, **kw) -> "EngineProfile":
        """Return a copy with fields replaced. For tests and backtest sweeps only."""
        return replace(self, **kw)

    @property
    def min_target_pct(self) -> float:
        """Minimum target move that can pay for the round trip."""
        return self.round_trip_cost_pct * self.min_target_cost_multiple


# ══════════════════════════════════════════════════════════════════════════════
# TOTAL ACCOUNT EQUITY
# ══════════════════════════════════════════════════════════════════════════════
# Previously each engine assumed it owned the whole account: intraday hardcoded
# ₹5,00,000 and swing/long-term had no concept of capital at all. Three engines each
# sizing against the full balance is how a "3x gross" intraday limit silently becomes
# 3x of money that is already committed elsewhere.
#
# Equity is now declared once and split by `equity_share_pct`. The shares must not
# exceed 100 — asserted at import time below.
TOTAL_ACCOUNT_EQUITY = _s("TOTAL_ACCOUNT_EQUITY", 500_000.0)


# ══════════════════════════════════════════════════════════════════════════════
# FACTOR WEIGHTS
# ══════════════════════════════════════════════════════════════════════════════
# Each set sums to 100. Ordered by strength of published evidence for that horizon.

INTRADAY_FACTOR_WEIGHTS = {
    "relative_strength": 20,
    "market_alignment": 15,
    "reward_risk": 15,
    "volume_confirmation": 12,
    "trend_quality": 12,
    "sector_strength": 10,
    "liquidity": 8,
    "volatility_fit": 8,
}

# Swing reweights toward persistence rather than immediacy. Over 15-90 days,
# cross-sectional momentum and trend quality dominate; single-bar volume confirmation
# matters far less than it does on a 5-minute breakout, and intraday's `market_alignment`
# (direction agrees with today's index bias) is close to noise at this horizon.
SWING_FACTOR_WEIGHTS = {
    "relative_strength": 22,
    "trend_quality": 18,
    "momentum_persistence": 15,
    "breakout_quality": 12,
    "sector_strength": 12,
    "volume_confirmation": 8,
    "liquidity": 7,
    "volatility_fit": 6,
}

# Long-term is fundamentals-led. These are the *family* weights; the within-family
# metric weights live in the long-term scoring module. Momentum keeps meaningful weight
# because 12-1 momentum survives at multi-year horizons — but it can never be the whole
# model, which is precisely the failure mode of the engine being replaced.
LONG_TERM_FACTOR_WEIGHTS = {
    "quality": 30,
    "growth": 20,
    "momentum": 20,
    "value": 15,
    "ownership": 10,
    "size_liquidity": 5,
}


# ══════════════════════════════════════════════════════════════════════════════
# PROFILES
# ══════════════════════════════════════════════════════════════════════════════

INTRADAY = EngineProfile(
    name="intraday",
    # Narrowed NIFTY200 -> NIFTY50 2026-07-28 at the account owner's explicit request:
    # candle_bars was accumulating ONE_DAY/FIVE_MINUTE rows for the full 200-symbol
    # universe (including illiquid names never actually scanned), adding to Supabase
    # usage-limit pressure, and a 200-symbol universe takes far longer to backfill/
    # warm than a 50-symbol one. Override via INTRADAY_UNIVERSE_INDEX env var if the
    # universe needs widening back out later.
    index=_s("INTRADAY_UNIVERSE_INDEX", "NIFTY100"),
    bar_interval="FIVE_MINUTE",
    min_adv_inr=_s("INTRADAY_MIN_ADV_INR", 50_00_00_000.0),   # ₹50 cr
    min_price_inr=_s("INTRADAY_MIN_PRICE", 100.0),
    max_spread_bps=_s("INTRADAY_MAX_SPREAD_BPS", 5.0),
    universe_ttl_secs=6 * 3600,
    regime_horizon="intraday",
    regime_ttl_secs=300,
    regime_bar_interval="FIVE_MINUTE",
    regime_lookback_days=5,
    factor_weights=INTRADAY_FACTOR_WEIGHTS,
    min_rank_score=_s("INTRADAY_MIN_RANK_SCORE", 65.0),
    max_positions=_s("INTRADAY_MAX_POSITIONS", 5),
    max_per_sector=_s("INTRADAY_MAX_PER_SECTOR", 2),
    max_per_cluster=_s("INTRADAY_MAX_PER_CLUSTER", 2),
    max_per_promoter_group_pct=_s("INTRADAY_MAX_PROMOTER_GROUP_PCT", 40.0),
    max_position_pct=_s("INTRADAY_MAX_POSITION_PCT", 100.0),
    corr_lookback_days=30,
    corr_threshold=_s("INTRADAY_CORRELATION_THRESHOLD", 0.70),
    max_portfolio_beta=None,
    sizing_mode="risk_parity",
    risk_per_trade_pct=_s("INTRADAY_RISK_PER_TRADE_PCT", 0.30),
    max_gross_pct=_s("INTRADAY_MAX_GROSS_EXPOSURE_PCT", 300.0),
    equity_share_pct=_s("INTRADAY_EQUITY_SHARE_PCT", 30.0),
    daily_loss_limit_pct=_s("INTRADAY_DAILY_LOSS_LIMIT_PCT", 2.0),
    strategy_drawdown_limit_pct=None,
    round_trip_cost_pct=_s("INTRADAY_ROUND_TRIP_COST_PCT", 0.14),
    min_target_cost_multiple=_s("INTRADAY_MIN_TARGET_COST_MULTIPLE", 3.0),
    time_stop_minutes=40,
)

SWING = EngineProfile(
    name="swing",
    long_only=True,
    index=_s("SWING_UNIVERSE_INDEX", "NIFTY500"),
    bar_interval="ONE_DAY",
    # A 15-90 day hold can scale in over several sessions, so it tolerates far less
    # liquidity than an intraday position that must be exited the same day.
    min_adv_inr=_s("SWING_MIN_ADV_INR", 10_00_00_000.0),      # ₹10 cr
    min_price_inr=_s("SWING_MIN_PRICE", 50.0),
    max_spread_bps=_s("SWING_MAX_SPREAD_BPS", 25.0),
    universe_ttl_secs=24 * 3600,
    regime_horizon="swing",
    regime_ttl_secs=12 * 3600,
    regime_bar_interval="ONE_DAY",
    regime_lookback_days=250,
    factor_weights=SWING_FACTOR_WEIGHTS,
    min_rank_score=_s("SWING_MIN_RANK_SCORE", 60.0),
    max_positions=_s("SWING_MAX_POSITIONS", 12),
    max_per_sector=_s("SWING_MAX_PER_SECTOR", 3),
    max_per_cluster=_s("SWING_MAX_PER_CLUSTER", 2),
    max_per_promoter_group_pct=_s("SWING_MAX_PROMOTER_GROUP_PCT", 20.0),
    max_position_pct=_s("SWING_MAX_POSITION_PCT", 15.0),
    corr_lookback_days=90,
    corr_threshold=_s("SWING_CORRELATION_THRESHOLD", 0.65),
    max_portfolio_beta=_s("SWING_MAX_PORTFOLIO_BETA", 1.5),
    sizing_mode="risk_parity",
    risk_per_trade_pct=_s("SWING_RISK_PER_TRADE_PCT", 0.75),
    max_gross_pct=_s("SWING_MAX_GROSS_EXPOSURE_PCT", 100.0),
    equity_share_pct=_s("SWING_EQUITY_SHARE_PCT", 40.0),
    daily_loss_limit_pct=None,
    strategy_drawdown_limit_pct=_s("SWING_DRAWDOWN_LIMIT_PCT", 8.0),
    # DELIVERY friction, not intraday. The dominant term is STT at 0.1% on BOTH legs
    # (intraday pays 0.025% on the sell leg only), plus DP charges on the sell.
    # Itemised in cost_model.py — do not edit this number without editing that table.
    round_trip_cost_pct=_s("SWING_ROUND_TRIP_COST_PCT", 0.40),
    min_target_cost_multiple=_s("SWING_MIN_TARGET_COST_MULTIPLE", 6.0),
    earnings_blackout_before=_s("SWING_EARNINGS_BLACKOUT_BEFORE", 2),
    earnings_blackout_after=_s("SWING_EARNINGS_BLACKOUT_AFTER", 1),
    pending_expiry_days=_s("SWING_PENDING_EXPIRY_DAYS", 5),
)

LONG_TERM = EngineProfile(
    name="long_term",
    long_only=True,
    index=_s("LONG_TERM_UNIVERSE_INDEX", "NIFTY500"),
    bar_interval="ONE_DAY",
    min_adv_inr=_s("LONG_TERM_MIN_ADV_INR", 5_00_00_000.0),   # ₹5 cr
    min_price_inr=_s("LONG_TERM_MIN_PRICE", 50.0),
    max_spread_bps=_s("LONG_TERM_MAX_SPREAD_BPS", 50.0),
    universe_ttl_secs=7 * 24 * 3600,
    regime_horizon="structural",
    regime_ttl_secs=7 * 24 * 3600,
    regime_bar_interval="ONE_DAY",
    regime_lookback_days=1000,
    factor_weights=LONG_TERM_FACTOR_WEIGHTS,
    min_rank_score=_s("LONG_TERM_MIN_RANK_SCORE", 55.0),
    max_positions=_s("LONG_TERM_MAX_POSITIONS", 20),
    max_per_sector=_s("LONG_TERM_MAX_PER_SECTOR", 4),
    max_per_cluster=_s("LONG_TERM_MAX_PER_CLUSTER", 2),
    # The control that would have prevented the 100%-Adani book. ADANIPORTS is
    # Infrastructure and ADANIENT is Diversified, so a SECTOR cap alone permits it.
    max_per_promoter_group_pct=_s("LONG_TERM_MAX_PROMOTER_GROUP_PCT", 12.0),
    max_position_pct=_s("LONG_TERM_MAX_POSITION_PCT", 8.0),
    corr_lookback_days=250,
    corr_threshold=_s("LONG_TERM_CORRELATION_THRESHOLD", 0.60),
    max_portfolio_beta=_s("LONG_TERM_MAX_PORTFOLIO_BETA", 1.2),
    # Not risk_parity: a long-term position has no meaningful entry stop to size
    # against — its exits are fundamental and time-based, so there is no denominator.
    sizing_mode="inverse_vol",
    risk_per_trade_pct=0.0,
    max_gross_pct=_s("LONG_TERM_MAX_GROSS_EXPOSURE_PCT", 100.0),
    equity_share_pct=_s("LONG_TERM_EQUITY_SHARE_PCT", 30.0),
    daily_loss_limit_pct=None,
    strategy_drawdown_limit_pct=_s("LONG_TERM_DRAWDOWN_LIMIT_PCT", 15.0),
    round_trip_cost_pct=_s("LONG_TERM_ROUND_TRIP_COST_PCT", 0.40),
    min_target_cost_multiple=_s("LONG_TERM_MIN_TARGET_COST_MULTIPLE", 15.0),
    # Long-term deliberately holds THROUGH earnings — the event is the thesis update,
    # not a hazard to dodge. It triggers a review instead (see the LT exit rules).
    earnings_blackout_before=0,
    earnings_blackout_after=0,
    max_fundamental_staleness_days=_s("LONG_TERM_MAX_STALENESS_DAYS", 180),
)


_PROFILES = {p.name: p for p in (INTRADAY, SWING, LONG_TERM)}


def get_profile(name: str) -> EngineProfile:
    """Look up a profile by engine name. Raises on unknown names rather than
    silently defaulting — a typo must not quietly hand an engine intraday's
    3x gross limit."""
    try:
        return _PROFILES[name]
    except KeyError:
        raise ValueError(
            f"Unknown engine profile {name!r}. Known: {sorted(_PROFILES)}"
        ) from None


def allocated_equity(profile: EngineProfile) -> float:
    """This engine's share of total account equity, in rupees."""
    return TOTAL_ACCOUNT_EQUITY * (profile.equity_share_pct / 100.0)


def _validate() -> None:
    total_share = sum(p.equity_share_pct for p in _PROFILES.values())
    if total_share > 100.0 + 1e-9:
        raise ValueError(
            f"Engine equity shares sum to {total_share}% (>100%). "
            "Each engine would be sizing against capital another engine has already "
            "committed. Adjust *_EQUITY_SHARE_PCT in settings."
        )
    for p in _PROFILES.values():
        w = sum(p.factor_weights.values())
        if abs(w - 100.0) > 1e-6:
            raise ValueError(f"{p.name} factor weights sum to {w}, expected 100.")


_validate()
