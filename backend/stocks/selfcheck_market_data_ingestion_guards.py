"""Self-check for three market-data ingestion guards, all in the same
candle/quote pipeline (truedata_service.py + market_data/tick_aggregator.py).

1. get_bulk_quotes()'s REST fallback (truedata_service.py) used to pair
   response rows to requested symbols by raw zip() position with no length
   check. getLTPBulk's CSV has no symbol column, so a short or reordered
   response silently shifted every subsequent pairing — misattributing prices
   for the rest of the batch, feeding wrong prices into live signal
   generation, with nothing logged. Fix: validate row count == request count
   before pairing; abort (and log) the whole batch on mismatch instead.

2. roll_up_universe() (market_data/tick_aggregator.py) read get_stream_price()
   with no staleness check. A silently-dead WebSocket keeps the same frozen
   cached tick forever, which used to get folded into the in-progress 5-min
   bar as if it were live — producing flat, fabricated OHLC candles persisted
   into CandleBar as real history. Fix: skip a tick older than 60s instead of
   aggregating it.

3. get_candle_data() (truedata_service.py) ran pd.to_numeric(errors="coerce")
   on the OHLCV columns but never dropped the NaN rows that produces for a
   malformed source row — Postgres accepts NaN silently, permanently
   corrupting the candle store. Fix: dropna on the OHLCV columns before the
   frame is cached/returned/stored.

No network/DB — mocks _rest_request / get_stream_price directly, same
pattern as selfcheck_tick_freshness.py and selfcheck_bulk_quote_chunking.py.
"""
import os
import sys
import time
import django

sys.path.insert(0, "/home/jd/tradepulse-truedata/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from unittest.mock import patch

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


# ======================================================================
# 1. get_bulk_quotes: mismatched-length REST response is rejected, not paired
# ======================================================================
print("=" * 72)
print("get_bulk_quotes: row-count mismatch aborts the batch instead of mispairing")
print("=" * 72)

from stocks.services import truedata_service as tds
from stocks.services.truedata_streamer import _STREAM_CACHE, _STREAM_LOCK

svc = tds.TrueDataService(username="dummy", password="dummy")
svc.streamer = None
svc.is_authenticated = True
svc.token_expires_at = time.time() + 3600  # skip real auth in _ensure_fresh_token

SYMS = ["TESTQ_A", "TESTQ_B", "TESTQ_C"]
tds._REST_CIRCUIT_BREAKER_UNTIL["quote"] = 0.0
with _STREAM_LOCK:
    for s in SYMS:
        _STREAM_CACHE.pop(s, None)

try:
    # 3 requested, only 2 rows returned — the exact TrueData failure mode this guards.
    svc._rest_request = lambda method, url, **kw: FakeResponse(200, "ltp\n100.5\n200.25\n")
    mismatched = svc.get_bulk_quotes({"NSE": list(SYMS)})
    check("mismatched row count returns no quotes for the batch", mismatched == {}, str(mismatched))

    # Sanity: matching row count still pairs correctly (the fix isn't just "always empty").
    svc._rest_request = lambda method, url, **kw: FakeResponse(200, "ltp\n100.5\n200.25\n300.75\n")
    matched = svc.get_bulk_quotes({"NSE": list(SYMS)})
    check("matched row count pairs all 3 symbols", set(matched.keys()) == {f"NSE:{s}" for s in SYMS},
          str(matched.keys()))
    check("prices paired in request order", matched.get("NSE:TESTQ_A", {}).get("ltp") == 100.5
          and matched.get("NSE:TESTQ_C", {}).get("ltp") == 300.75)
finally:
    with _STREAM_LOCK:
        for s in SYMS:
            _STREAM_CACHE.pop(s, None)


# ======================================================================
# 2. roll_up_universe: a stale (>60s) tick is excluded from bar roll-up
# ======================================================================
print()
print("=" * 72)
print("roll_up_universe: stale WS tick is skipped, not folded into the bar")
print("=" * 72)

from stocks.services.market_data import tick_aggregator
from django.core.cache import cache as dj_cache

dj_cache.delete(tick_aggregator._STATE_CACHE_KEY)

now = time.time()


def fake_get_stream_price(token):
    if token == "FRESH_SYM":
        return {"ltp": 100.0, "trade_volume": 500.0, "fetch_time": now}
    if token == "STALE_SYM":
        return {"ltp": 200.0, "trade_volume": 300.0, "fetch_time": now - 90}  # 90s old > 60s bound
    return None


try:
    with patch.object(tick_aggregator, "get_stream_price", side_effect=fake_get_stream_price), \
         patch.object(tick_aggregator.candle_store, "store_completed_bars", return_value=0):
        processed = tick_aggregator.roll_up_universe({"FRESH_SYM": "FRESH_SYM", "STALE_SYM": "STALE_SYM"})

    check("only the fresh tick counts as processed", processed == 1, str(processed))

    state = dj_cache.get(tick_aggregator._STATE_CACHE_KEY) or {}
    check("fresh symbol's window state was recorded", "NSE:FRESH_SYM" in state, str(state.keys()))
    check("stale symbol's window state was NOT recorded (not folded into the bar)",
          "NSE:STALE_SYM" not in state, str(state.keys()))
finally:
    dj_cache.delete(tick_aggregator._STATE_CACHE_KEY)


# ======================================================================
# 3. get_candle_data: a NaN-producing malformed row is dropped, not stored
# ======================================================================
print()
print("=" * 72)
print("get_candle_data: malformed row (coerces to NaN) is dropped before caching")
print("=" * 72)

svc2 = tds.TrueDataService(username="dummy", password="dummy")
svc2.streamer = None
svc2.is_authenticated = True
svc2.token_expires_at = time.time() + 3600
tds._REST_CIRCUIT_BREAKER_UNTIL["candle"] = 0.0

CANDLE_CSV = (
    "timestamp,open,high,low,close,volume\n"
    "2026-08-04 09:15:00,100,101,99,100.5,1000\n"
    "2026-08-04 09:20:00,BAD,101,99,100.5,1000\n"  # malformed open -> coerces to NaN
)
svc2._rest_request = lambda method, url, **kw: FakeResponse(200, CANDLE_CSV)

# Unique symbol per run so a leftover FileBasedCache entry from a prior run can't
# short-circuit the fetch path this check is exercising.
test_symbol = f"TESTCANDLE_NAN_{int(time.time() * 1000)}"
df = svc2.get_candle_data(test_symbol, "NSE", "FIVE_MINUTE", "2026-08-04 09:00", "2026-08-04 10:00")

check("2 source rows in, 1 malformed row dropped -> 1 row survives", len(df) == 1, f"got {len(df)} rows")
check("surviving row has no NaN in OHLCV columns", not df.isna().any().any() if not df.empty else False,
      str(df))


print()
print("=" * 72)
if fails:
    print(f"{len(fails)} FAILURE(S):", fails)
    sys.exit(1)
else:
    print("ALL PASS — bulk-quote pairing, tick staleness, and candle NaN rows are all guarded.")
