"""Phase 1 parity check for stocks.services.market_data (doc/MARKET_DATA_ENGINE_ARCHITECTURE.md §11/§12).

Proves the gateway is a pure pass-through: same arguments in, same return value
out, nothing new. Uses unittest.mock to intercept at the boundary the gateway
forwards to (candle_store.get_candles, svc.get_bulk_quotes, svc.get_live_price_by_token)
so this never touches the real network or the production Supabase DB — local dev
points DATABASE_URL directly at production, so a test that exercised the real ORM
path here would read/write live tables for no reason.
"""
import os
import sys
import django

sys.path.insert(0, "/home/jd/tradeplusai/tradepulse-ai/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from unittest.mock import MagicMock, patch

import pandas as pd

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


print("=" * 72)
print("1. request_candles() forwards to candle_store.get_candles() unchanged")
print("=" * 72)

from stocks.services.market_data import gateway

fake_svc = MagicMock(name="TrueDataService")
sentinel_df = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [100]})

with patch("stocks.services.market_data.gateway.candle_store.get_candles", return_value=sentinel_df) as mocked:
    result = gateway.request_candles(
        fake_svc, "RELIANCE", "2885", "FIVE_MINUTE", lookback_days=3,
        exchange="NSE", allow_fetch=True,
    )
    check("returns candle_store's frame unmodified", result is sentinel_df)
    check("called exactly once", mocked.call_count == 1, str(mocked.call_count))
    args, kwargs = mocked.call_args
    check("positional args forwarded unchanged",
          args == (fake_svc, "RELIANCE", "2885", "FIVE_MINUTE", 3),
          str(args))
    check("keyword args forwarded unchanged",
          kwargs == {"exchange": "NSE", "allow_fetch": True},
          str(kwargs))

print()
print("=" * 72)
print("2. request_bulk_quote() forwards to svc.get_bulk_quotes() unchanged")
print("=" * 72)

fake_svc2 = MagicMock(name="TrueDataService")
sentinel_quotes = {"NSE:2885": {"ltp": 1234.5}}
fake_svc2.get_bulk_quotes.return_value = sentinel_quotes

token_map = {"NSE": ["2885", "3045"]}
result2 = gateway.request_bulk_quote(fake_svc2, token_map, mode="FULL")
check("returns svc's result unmodified", result2 is sentinel_quotes)
fake_svc2.get_bulk_quotes.assert_called_once_with(token_map, mode="FULL")
check("called with unchanged args", True)  # assert_called_once_with raises on mismatch

print()
print("=" * 72)
print("3. get_ltp() resolves tokens then forwards to svc.get_live_price_by_token()")
print("=" * 72)

from stocks.services.market_data import ws_read

fake_svc3 = MagicMock(name="TrueDataService")
fake_svc3.get_token_map.return_value = {"RELIANCE": "2885", "TCS": "11536"}
fake_svc3.get_live_price_by_token.side_effect = lambda token, exchange="NSE": {
    "2885": {"ltp": 1400.0, "source": "websocket"},
    "11536": {"ltp": 3600.0, "source": "websocket"},
}.get(token)

result3 = ws_read.get_ltp(fake_svc3, ["RELIANCE", "TCS"], exchange="NSE")
check("resolves symbol -> token via existing get_token_map",
      fake_svc3.get_token_map.call_args == (( ["RELIANCE", "TCS"],), {"exchange": "NSE"}),
      str(fake_svc3.get_token_map.call_args))
check("keyed by symbol, not token", set(result3.keys()) == {"RELIANCE", "TCS"}, str(result3.keys()))
check("values are svc's tick dicts unmodified",
      result3["RELIANCE"]["ltp"] == 1400.0 and result3["TCS"]["ltp"] == 3600.0,
      str(result3))
check("no result returned for empty symbol list", ws_read.get_ltp(fake_svc3, []) == {})

print()
print("=" * 72)
if fails:
    print(f"{len(fails)} FAILURE(S):", fails)
    sys.exit(1)
else:
    print("ALL PASS — gateway is a verified zero-behavior-change pass-through.")
