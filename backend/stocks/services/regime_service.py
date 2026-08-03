"""DEPRECATED shim — moved to `stocks.services.shared.regime`.

Kept so the intraday engine and the existing test suites keep importing successfully
while the shared-layer promotion lands. Delete once all call sites import from
`stocks.services.shared` directly.

`_resolve_permissions` is explicitly re-exported despite the leading underscore:
`selfcheck_intraday_v2.py` imports it by name directly, and a plain `import *` never
re-exports underscore-prefixed names regardless of `__all__` — that combination
silently broke this exact import when the shim was first written, and the breakage
went unnoticed because the failing script exits via `sys.exit()` at module level
rather than raising a normal test failure. This class of script was later renamed
`selfcheck_*.py` (off of `test*.py`) specifically so Django's test discovery can no
longer import it as a collection side-effect — see backend-tests.yml's dedicated step.
"""
from stocks.services.shared.regime import *  # noqa: F401,F403
from stocks.services.shared.regime import _resolve_permissions  # noqa: F401
from stocks.services.shared import regime as _mod

__all__ = [n for n in dir(_mod) if not n.startswith("_")] + ["_resolve_permissions"]
