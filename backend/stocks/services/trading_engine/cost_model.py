"""
Transaction cost and slippage model for Indian intraday equity.

See doc/INSTITUTIONAL_AUDIT_INTRADAY.md §0.1 and §8.2. The audit's central finding was
that friction exceeded the strategy's edge: a 0.16% stop at 2:1 reward:risk needs a
~62% win rate to break even once costs are applied, against 33% gross. Every backtest
and every live signal must therefore price costs explicitly rather than assume a
frictionless fill.

Nothing here is a "safety margin" guess — the statutory components are the published
NSE/SEBI rates, and the discretionary components (spread, impact) are modelled as
functions of the instrument and order size rather than as a flat fudge factor.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── Statutory / exchange components (fractions of turnover, per side) ────────────
STT_SELL_PCT = 0.00025          # 0.025% — intraday equity, sell side only
EXCHANGE_TXN_PCT = 0.0000297    # NSE transaction charge, both sides
SEBI_TURNOVER_PCT = 0.000001    # 0.0001%, both sides
STAMP_DUTY_PCT = 0.00003        # 0.003%, buy side only
GST_RATE = 0.18                 # on (brokerage + exchange txn + SEBI)

# ── DELIVERY (CNC) overrides ─────────────────────────────────────────────────────
# The swing and long-term engines hold overnight, which is a materially different cost
# structure — not a tweak to the intraday numbers. STT is the dominant term and it is
# 8x worse: 0.1% on BOTH legs versus 0.025% on the sell leg only. Stamp duty is 5x.
# There is also a flat DP charge on every sell, which matters disproportionately on
# small positions.
#
# Round trip on Rs.1,00,000, ignoring impact:
#   intraday  ~0.12%      delivery  ~0.34%
# At 2:1 RR that moves the break-even win rate materially, which is why a 0.3% intraday
# target is uneconomic while the same target over 15-90 days is not.
STT_DELIVERY_PCT = 0.001        # 0.1%, BOTH sides
STAMP_DUTY_DELIVERY_PCT = 0.00015   # 0.015%, buy side only
DP_CHARGE_INR = 15.34           # per sell, flat; GST applied on top


# Discount-broker flat brokerage: min(0.03% of turnover, ₹20) per executed order.
BROKERAGE_PCT = 0.0003
BROKERAGE_CAP_INR = 20.0

# Default half-spread in basis points when live quote depth is unavailable. NIFTY100
# large caps sit around 1-3bps; the tail of NIFTY500 is far wider, which is precisely
# why the audit rejected NIFTY500 as a universe for a sub-1% target strategy.
DEFAULT_HALF_SPREAD_BPS = 1.5

# Square-root market impact coefficient. Impact ≈ k · σ · √(order / ADV) is the standard
# functional form (Almgren et al.); k ≈ 0.5-1.0 empirically. Conservative default.
IMPACT_COEFFICIENT = 0.8


@dataclass
class FillCost:
    """Cost breakdown for a single side of a trade, in rupees."""
    brokerage: float = 0.0
    stt: float = 0.0
    dp_charge: float = 0.0
    exchange_txn: float = 0.0
    sebi: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0
    spread: float = 0.0
    impact: float = 0.0

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.exchange_txn + self.sebi
                + self.stamp_duty + self.gst + self.spread + self.impact
                + self.dp_charge)

    def as_dict(self) -> dict[str, float]:
        d = {k: round(v, 4) for k, v in self.__dict__.items()}
        d["total"] = round(self.total, 4)
        return d


@dataclass
class CostModel:
    """Configurable cost model. Defaults target a discount broker on NSE intraday."""
    # "intraday" (MIS) or "delivery" (CNC). Switches STT, stamp duty and DP charges.
    product: str = "intraday"
    half_spread_bps: float = DEFAULT_HALF_SPREAD_BPS
    impact_coefficient: float = IMPACT_COEFFICIENT
    brokerage_pct: float = BROKERAGE_PCT
    brokerage_cap: float = BROKERAGE_CAP_INR
    # Per-symbol overrides: {"SYMBOL": half_spread_bps}. Populated from live quote
    # depth where available so liquid names aren't charged the generic default.
    spread_overrides: dict[str, float] = field(default_factory=dict)

    def half_spread_for(self, symbol: str) -> float:
        return self.spread_overrides.get(symbol, self.half_spread_bps)

    def side_cost(
        self,
        *,
        symbol: str,
        price: float,
        qty: int,
        is_buy: bool,
        adv_inr: float | None = None,
        daily_vol_pct: float | None = None,
    ) -> FillCost:
        """Cost of one leg (entry or exit), in rupees."""
        turnover = max(0.0, float(price) * int(qty))
        if turnover <= 0:
            return FillCost()

        c = FillCost()
        c.brokerage = min(turnover * self.brokerage_pct, self.brokerage_cap)
        c.exchange_txn = turnover * EXCHANGE_TXN_PCT
        c.sebi = turnover * SEBI_TURNOVER_PCT
        if self.product == "delivery":
            # STT applies to BOTH legs on delivery, at 4x the intraday sell-side rate.
            c.stt = turnover * STT_DELIVERY_PCT
            c.stamp_duty = turnover * STAMP_DUTY_DELIVERY_PCT if is_buy else 0.0
            c.dp_charge = 0.0 if is_buy else DP_CHARGE_INR
        else:
            c.stt = 0.0 if is_buy else turnover * STT_SELL_PCT
            c.stamp_duty = turnover * STAMP_DUTY_PCT if is_buy else 0.0
        c.gst = (c.brokerage + c.exchange_txn + c.sebi + c.dp_charge) * GST_RATE

        # Crossing the spread costs the half-spread on each side.
        c.spread = turnover * (self.half_spread_for(symbol) / 10_000.0)

        # Square-root impact, only when we know participation and volatility.
        if adv_inr and adv_inr > 0 and daily_vol_pct and daily_vol_pct > 0:
            participation = turnover / adv_inr
            impact_pct = self.impact_coefficient * (daily_vol_pct / 100.0) * (participation ** 0.5)
            c.impact = turnover * impact_pct

        return c

    def round_trip(
        self,
        *,
        symbol: str,
        entry_price: float,
        exit_price: float,
        qty: int,
        is_long: bool,
        adv_inr: float | None = None,
        daily_vol_pct: float | None = None,
    ) -> float:
        """Total rupee friction for a completed round trip."""
        entry = self.side_cost(symbol=symbol, price=entry_price, qty=qty, is_buy=is_long,
                               adv_inr=adv_inr, daily_vol_pct=daily_vol_pct)
        exit_ = self.side_cost(symbol=symbol, price=exit_price, qty=qty, is_buy=not is_long,
                               adv_inr=adv_inr, daily_vol_pct=daily_vol_pct)
        return entry.total + exit_.total

    def round_trip_pct(
        self,
        *,
        symbol: str = "",
        price: float = 1000.0,
        qty: int = 100,
        adv_inr: float | None = None,
        daily_vol_pct: float | None = None,
    ) -> float:
        """Round-trip friction as a percentage of notional.

        This is the number the signal-generation cost gate compares targets against.
        """
        turnover = price * qty
        if turnover <= 0:
            return 0.0
        total = self.round_trip(symbol=symbol, entry_price=price, exit_price=price,
                                qty=qty, is_long=True, adv_inr=adv_inr,
                                daily_vol_pct=daily_vol_pct)
        return total / turnover * 100.0

    def slipped_fill(self, price: float, is_buy: bool, symbol: str = "") -> float:
        """Adverse fill price after crossing the half-spread.

        Applied to backtest fills so a modelled entry/exit is never the mid price the
        signal was computed at. Assuming the touch price is what you get is one of the
        commonest ways a backtest flatters itself.
        """
        edge = price * (self.half_spread_for(symbol) / 10_000.0)
        return price + edge if is_buy else price - edge


DEFAULT_COST_MODEL = CostModel()

# Delivery variant for the swing and long-term engines. Half-spread is wider because
# their universes reach further down the liquidity curve (Rs.10cr ADV vs Rs.50cr), where
# quoted depth is thinner.
DELIVERY_COST_MODEL = CostModel(product="delivery", half_spread_bps=4.0)


def cost_model_for(profile) -> CostModel:
    """The right cost model for an engine profile.

    One place decides intraday-vs-delivery, so an engine can never accidentally price a
    multi-day hold at intraday STT — an error that would understate friction ~3x and
    silently pass trades the cost gate should reject.
    """
    return DEFAULT_COST_MODEL if getattr(profile, "name", "") == "intraday" else DELIVERY_COST_MODEL
