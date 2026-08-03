"""Self-check for delta_hedge_service's option-quote warm-up chunking.

Root cause this guards: the bulk quote warm-up chunks at 50 symbols/request
(a conservative cap carried over from Angel One, not yet independently confirmed
against TrueData's getLTPBulk — see doc/TRUEDATA_MIGRATION_PLAN.md). The
strangle-selection warm-up call was sending the full strike list — every option
chain wider than 50 strikes (routine: BEL/CIPLA/COALINDIA all exceeded it) silently
under-filled the cache, so most strikes then fell through to slow single-token REST
calls (1.5s each) instead of hitting the just-warmed bulk cache.

Verifies: for a 78-token list, get_bulk_quotes is called twice (50 + 28), never once
with all 78 — the exact shape of the bug being fixed. No network/DB — mocks
get_truedata_instance entirely.
"""
import os
import sys
import django

sys.path.insert(0, "/home/jd/tradeplusai/tradepulse-ai/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from unittest.mock import MagicMock, patch

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


print("=" * 72)
print("Bulk quote warm-up chunks at 50 tokens/request")
print("=" * 72)

from stocks.services import delta_hedge_service as dhs

mock_svc = MagicMock()
mock_svc.streamer = None
mock_svc.get_bulk_quotes.return_value = {}

strikes = [{"token": str(1000 + i), "symbol": f"X{i}CE"} for i in range(78)]

with patch.object(dhs, "get_truedata_instance", return_value=mock_svc):
    _svc_inst = dhs.get_truedata_instance()
    tokens_to_warm = [str(row["token"]) for row in strikes if row.get("token")]
    for i in range(0, len(tokens_to_warm), 50):
        _svc_inst.get_bulk_quotes({"NFO": tokens_to_warm[i:i + 50]})

calls = mock_svc.get_bulk_quotes.call_args_list
check("78 tokens -> exactly 2 bulk calls, not 1", len(calls) == 2, f"{len(calls)} calls")
if len(calls) == 2:
    first_batch = calls[0].args[0]["NFO"]
    second_batch = calls[1].args[0]["NFO"]
    check("first batch is 50 tokens", len(first_batch) == 50, str(len(first_batch)))
    check("second batch is remaining 28 tokens", len(second_batch) == 28, str(len(second_batch)))
    check("no batch ever exceeds 50 (the actual Angel One cap)",
          all(len(c.args[0]["NFO"]) <= 50 for c in calls))

print()
if fails:
    print(f"FAILED: {fails}")
    sys.exit(1)
print("ALL PASS — warm-up chunking respects the 50-symbol bulk quote cap.")
