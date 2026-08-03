from __future__ import annotations

from typing import Any

from django.utils import timezone as dj_timezone

from stocks.models import SignalHistory
from .config import MarketRules

SETUP_PRICE_TOLERANCE = 0.11


def is_signal_active(signal_type: str, current_price: float | int | None, entry_price: float | int | None, trigger_mode: str = "price_cross") -> bool:
    try:
        current = float(current_price)
        entry = float(entry_price)
    except (TypeError, ValueError):
        return False

    if trigger_mode != "price_cross":
        return False

    if signal_type == "BUY":
        return current >= entry
    if signal_type == "SELL":
        return current <= entry
    return False


def _close_enough(left: float | int, right: float | int, tolerance: float = SETUP_PRICE_TOLERANCE) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def persist_live_signal_history(result: dict[str, Any], category: str, rules: MarketRules) -> tuple[SignalHistory, bool]:
    """
    Save or refresh a live signal while preserving signal_time and active_time.
    Returns (signal_row, created_new_signal_event).
    """
    now = dj_timezone.now()
    # Trading lifecycle rule:
    # signal_time = when setup is generated
    # active_time = only when entry is touched later
    # Respect explicit lifecycle status from the scanner when provided.
    initial_status = result.get("status")
    if initial_status not in SignalHistory.Status.values:
        initial_status = SignalHistory.Status.PENDING

    from django.db import transaction, IntegrityError
    
    try:
        with transaction.atomic():
            existing = SignalHistory.objects.select_for_update().filter(
                symbol=result["symbol"],
                status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
                category=category,
            ).first()

            if existing:
                # Keep the currently-live row as the single source of truth.
                # A symbol should not be rescanned into a new live setup until
                # the current one resolves to a terminal state.
                return existing, False

            # Collect all computed technical metrics into metadata so they
            # survive the DB round-trip and can be surfaced in the serializer.
            metadata = result.get("metadata") or {}
            for key in ("vol_ratio", "vwap", "atr", "poc", "vah", "val",
                        "score", "priority", "strategy_key", "strategy_name",
                        # Sizing + cost economics and provenance. Intraday populates
                        # these; other categories simply don't set them, so the
                        # setdefault copy is a no-op there.
                        "qty", "rupee_risk", "target_pct", "cost_pct", "relaxed"):
                if key in result and result[key] is not None:
                    metadata.setdefault(key, result[key])

            obj = SignalHistory.objects.create(
                symbol=result["symbol"],
                signal_type=result["signal"],
                entry_price=result["entry"],
                stop_loss=result["stop_loss"],
                target=result["target"],
                rr=result["rr"],
                reason=result["reason"],
                status=initial_status,
                category=category,
                strike_price=result.get("strike_price"),
                option_type=result.get("option_type"),
                premium_cmp=result.get("premium_cmp"),
                option_expiry=result.get("expiry"),
                active_time=result.get("active_time"),
                exit_price=result.get("exit_price"),
                exit_time=result.get("exit_time"),
                metadata=metadata,
            )
            return obj, True
    except IntegrityError:
        # Handle the race condition where another process created it concurrently.
        # This occurs if select_for_update saw nothing, but someone else inserted before our create.
        existing = SignalHistory.objects.filter(
            symbol=result["symbol"],
            status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
            category=category,
        ).first()
        return existing, False
