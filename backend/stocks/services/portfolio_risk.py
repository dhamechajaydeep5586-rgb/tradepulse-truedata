"""DEPRECATED shim — moved to `stocks.services.shared.portfolio_risk`.

Kept so the intraday engine and the existing test suites keep importing successfully
while the shared-layer promotion lands. Delete once all call sites import from
`stocks.services.shared` directly.
"""
from stocks.services.shared.portfolio_risk import *  # noqa: F401,F403
from stocks.services.shared import portfolio_risk as _mod

__all__ = [n for n in dir(_mod) if not n.startswith("_")]
