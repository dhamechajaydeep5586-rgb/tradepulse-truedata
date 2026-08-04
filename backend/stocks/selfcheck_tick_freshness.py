"""Self-check for get_bulk_quotes's WebSocket tick freshness gate.

Root cause this guards: freshness used to be `is_ws or age < 30` — once a
symbol's cache entry came from the WebSocket even once, its age was never
checked again, so a tick from 3 hours ago (WS silently dead) still passed as
"fresh" forever. That price feeds both the 1s live-price UI ticker and the
strangle-selling engine's spot-price/exit/rebalance checks, so a frozen tick
could silently drive real hedge decisions.

Verifies: a WS-sourced tick older than the new 60s bound is NOT treated as
fresh (falls through to the REST path, i.e. dropped from `results` when REST
is blocked by the circuit breaker) while a young WS tick still is. No
network/DB — pre-populates _STREAM_CACHE directly and forces the REST
circuit breaker open so any symbol NOT served from cache is provably absent
from the return value.
"""
import os
import sys
import time
import django

sys.path.insert(0, "/home/jd/tradepulse-truedata/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


print("=" * 72)
print("get_bulk_quotes: WS tick freshness expires with age, not just source")
print("=" * 72)

from stocks.services import truedata_service as tds
from stocks.services.truedata_streamer import _STREAM_CACHE, _STREAM_LOCK

svc = tds.TrueDataService(username="dummy", password="dummy")
svc.streamer = None  # skip the subscribe() warm-up call, not under test here

now = time.time()
with _STREAM_LOCK:
    _STREAM_CACHE["FRESH_WS"] = {
        "ltp": 100.0, "high": 101.0, "low": 99.0, "source": "websocket", "fetch_time": now - 10,
    }
    _STREAM_CACHE["STALE_WS"] = {
        "ltp": 200.0, "high": 201.0, "low": 199.0, "source": "websocket", "fetch_time": now - 90,
    }

# Force the REST fallback closed so anything NOT served from the cache is
# provably absent from `results` rather than silently re-fetched.
tds._REST_CIRCUIT_BREAKER_UNTIL["quote"] = time.time() + 300

try:
    results = svc.get_bulk_quotes({"NSE": ["FRESH_WS", "STALE_WS"]}, mode="FULL")
finally:
    tds._REST_CIRCUIT_BREAKER_UNTIL["quote"] = 0.0
    with _STREAM_LOCK:
        _STREAM_CACHE.pop("FRESH_WS", None)
        _STREAM_CACHE.pop("STALE_WS", None)

check("10s-old WS tick still served as fresh", "NSE:FRESH_WS" in results)
check("90s-old WS tick NOT served as fresh (age bound applies to WS too)",
      "NSE:STALE_WS" not in results)

print()
if fails:
    print(f"FAILED: {fails}")
    sys.exit(1)
print("ALL PASS — WS ticks expire with age; no source is trusted unbounded.")
