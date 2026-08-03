from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time


@dataclass(frozen=True)
class MarketRules:
    name: str
    trigger_mode: str = "price_cross"
    pending_max_candles: int = 5
    pending_timeout_minutes: int = 30
    avoid_windows: tuple[tuple[time, time], ...] = field(default_factory=tuple)
    chop_zone_atr_fraction: float = 0.25
    one_trade_at_a_time: bool = True
    candidate_limit: int | None = None
    max_signals: int | None = None

    def is_avoid_time(self, current_time: time | None) -> bool:
        if current_time is None:
            return False
        return any(start <= current_time <= end for start, end in self.avoid_windows)


MARKET_RULES: dict[str, MarketRules] = {
    "intraday": MarketRules(
        name="intraday",
        trigger_mode="price_cross",
        pending_max_candles=2,
        pending_timeout_minutes=20,
        avoid_windows=(),  # Liquidity Sweep has its own 9:30-13:30 time guard
        chop_zone_atr_fraction=0.20,
    ),
    "option_selling": MarketRules(
        name="option_selling",
        trigger_mode="price_cross",
        pending_max_candles=2,
        pending_timeout_minutes=30,
        avoid_windows=((time(10, 30), time(13, 30)),),
        chop_zone_atr_fraction=0.20,
        candidate_limit=60,
        max_signals=8,
    ),
    "option_buying": MarketRules(
        name="option_buying",
        trigger_mode="price_cross",
        pending_max_candles=2,
        pending_timeout_minutes=15,  # tighter than intraday's 20 — premium decays faster than an equity setup going stale
        avoid_windows=(),
        chop_zone_atr_fraction=0.20,
        candidate_limit=40,
        max_signals=3,
    ),
    "nextday": MarketRules(
        name="nextday",
        trigger_mode="price_cross",
        pending_max_candles=1,
        pending_timeout_minutes=1440,
        avoid_windows=(),
        chop_zone_atr_fraction=0.15,
        one_trade_at_a_time=False,
    ),
}


def get_market_rules(category: str) -> MarketRules:
    return MARKET_RULES.get(category, MARKET_RULES["intraday"])
