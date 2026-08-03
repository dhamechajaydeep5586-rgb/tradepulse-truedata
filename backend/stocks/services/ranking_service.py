"""DEPRECATED shim — moved to `stocks.services.shared.ranking`.

Kept so the intraday engine and the existing test suites keep importing successfully
while the shared-layer promotion lands. Delete once all call sites import from
`stocks.services.shared` directly.
"""
from stocks.services.shared.ranking import *  # noqa: F401,F403
from stocks.services.shared import ranking as _mod

__all__ = [n for n in dir(_mod) if not n.startswith("_")]
