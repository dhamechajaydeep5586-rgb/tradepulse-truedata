from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pandas as pd

from .config import MarketRules
from .state_engine import is_signal_active


def run_backtest_for_signal(
    candles: pd.DataFrame,
    signal: dict[str, Any],
    rules: MarketRules,
) -> dict[str, Any]:
    """
    Replay one signal against a candle dataframe using the same state rules as live mode.
    Expected columns: High, Low, Close and a datetime index.
    """
    status = "PENDING"
    active_time = None
    exit_time = None
    exit_price = None
    signal_type = signal["signal"]
    entry = float(signal["entry"])
    stop_loss = float(signal["stop_loss"])
    target = float(signal["target"])

    # iterrows() yields (timestamp, row); the timestamp must be unpacked or `candle`
    # is a tuple and every `.get()` below raises AttributeError — which it did on the
    # first bar of every call, so this function could not previously complete a run.
    bars: Iterable[tuple[Any, pd.Series]] = candles.iterrows()
    for idx, (_ts, candle) in enumerate(bars, start=1):
        current_price = float(candle.get("Close", entry))

        if status == "PENDING":
            # Activation is checked BEFORE the expiry test, and expiry uses a strict
            # `>`: with pending_max_candles=2 the previous `idx >= max` ordering fired
            # on bar 2 before that bar had a chance to trigger, so a signal really got
            # one bar to activate rather than the two the rule allows.
            if is_signal_active(signal_type, current_price, entry, rules.trigger_mode):
                status = "ACTIVE"
                active_time = idx
                continue

            if idx > rules.pending_max_candles:
                status = "CANCELLED"
                exit_time = idx
                exit_price = current_price
                break

        if status != "ACTIVE":
            continue

        low = float(candle.get("Low", current_price))
        high = float(candle.get("High", current_price))

        # Stop is tested BEFORE target. When a single bar's range spans both levels the
        # true intrabar path is unknown, so the loss is booked. Checking the target
        # first — as this previously did — silently resolved every ambiguous bar as a
        # win, and did so most often on wide-range bars, which are exactly the bars
        # where the outcome is least certain. That inflates backtested win rate.
        if signal_type == "BUY":
            if low <= stop_loss:
                status = "HIT_SL"
                exit_time = idx
                exit_price = stop_loss
                break
            if high >= target:
                status = "HIT_TARGET"
                exit_time = idx
                exit_price = target
                break
        else:
            if high >= stop_loss:
                status = "HIT_SL"
                exit_time = idx
                exit_price = stop_loss
                break
            if low <= target:
                status = "HIT_TARGET"
                exit_time = idx
                exit_price = target
                break

    return {
        "status": status,
        "signal_time": signal.get("signal_time"),
        "active_time": active_time,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "entry": entry,
        "target": target,
        "stop_loss": stop_loss,
    }
