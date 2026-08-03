"""Shared, timeframe-agnostic trading infrastructure.

Every engine (intraday, swing, long-term) consumes these services with its own
`EngineProfile`. Nothing in this package may contain engine-specific logic — if a
change would only ever benefit one engine, it belongs in that engine's module, not here.

The rule that keeps this package honest: **no module here imports from an engine
module.** Dependencies point inward only.
"""
from .profiles import (  # noqa: F401
    EngineProfile,
    INTRADAY,
    SWING,
    LONG_TERM,
    TOTAL_ACCOUNT_EQUITY,
    get_profile,
    allocated_equity,
)

__all__ = [
    "EngineProfile",
    "INTRADAY",
    "SWING",
    "LONG_TERM",
    "TOTAL_ACCOUNT_EQUITY",
    "get_profile",
    "allocated_equity",
]
