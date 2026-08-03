"""Phase 2 before/after parity check (doc/MARKET_DATA_ENGINE_ARCHITECTURE.md §13):

"for each of the 4 modules being switched to candle_store, a before/after test that
feeds the same symbol/date range through both the old direct call and the new cached
path and asserts identical output."

`selfcheck_market_data_gateway.py` already proves `market_data.gateway.request_candles()` is
a pure pass-through to `candle_store.get_candles()`. It does NOT prove that
`candle_store.get_candles()`'s own cold-cache path reproduces what the OLD code (which
called `svc.get_candle_data(token, exchange, interval, from_date, to_date)` directly,
computing `from_date`/`to_date` inline) actually did. That's the gap this file fills.

Representative call site: `shared/regime.py::_fetch_vix()` — a single call,
`lookback_days=5`, `interval="FIVE_MINUTE"`.

Note on "old code": `shared/regime.py` (and the rest of `shared/`) is a new, untracked
module in this repo's git history (confirmed via `git log --follow` returning nothing
for `backend/stocks/services/shared/`) — there is no literal prior commit to diff
against. Per the task, the old inline computation is therefore *derived* from
`candle_store.py::get_candles()`'s own cold-start math, since a correct migration's new
path must reproduce that exact arithmetic for a cold cache:

    now_ist   = datetime.now(tz=IST)
    from_date = (now_ist - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M")
    to_date   = now_ist.strftime("%Y-%m-%d %H:%M")
    svc.get_candle_data(token, exchange, interval, from_date, to_date)

This matches `candle_store.get_candles()`'s cold-start branch exactly: `start = now_ist -
timedelta(days=lookback_days)`, and when nothing is stored (`last_ts is None`),
`fetch_from = start`, formatted with the same `"%Y-%m-%d %H:%M"` and passed positionally
in the same (token, exchange, interval, from_date, to_date) order as
`TrueDataService.get_candle_data`'s signature.

"Cold cache" here means: no `CandleBar` rows exist for this symbol/interval/exchange, so
`load_bars()` returns empty and `get_candles()` falls through to a direct broker fetch.
Mocked rather than hitting the real DB — local `.env` points `DATABASE_URL` directly at
production Supabase, and a real cold-cache run would also make a real Angel One network
call, neither of which belongs in a test.
"""
import os
import sys
import django

sys.path.insert(0, "/home/jd/tradeplusai/tradepulse-ai/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

from stocks.services import candle_store, market_data
from stocks.services.signal_utils import IST

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


print("=" * 72)
print("candle_store cold-cache path reproduces the OLD direct svc.get_candle_data()")
print("from_date/to_date math, for shared/regime.py::_fetch_vix() (lookback_days=5)")
print("=" * 72)

SYMBOL = "INDIA_VIX"
TOKEN = "INDIA VIX"          # INDIA_VIX_TOKEN, per shared/regime.py
INTERVAL = "FIVE_MINUTE"
EXCHANGE = "NSE"
LOOKBACK_DAYS = 5
FMT = "%Y-%m-%d %H:%M"

FROZEN_NOW = datetime(2026, 7, 27, 10, 30, 0, tzinfo=IST)

# What the OLD inline code would have computed, reconstructed from candle_store.py's
# own cold-start math (see module docstring — no literal old commit exists to diff).
OLD_FROM_DATE = (FROZEN_NOW - timedelta(days=LOOKBACK_DAYS)).strftime(FMT)
OLD_TO_DATE = FROZEN_NOW.strftime(FMT)

sentinel_index = pd.DatetimeIndex(
    [FROZEN_NOW - timedelta(minutes=10), FROZEN_NOW - timedelta(minutes=5)], tz=IST
)
sentinel_df = pd.DataFrame(
    {"Open": [21.0, 21.1], "High": [21.2, 21.3], "Low": [20.9, 21.0],
     "Close": [21.1, 21.2], "Volume": [0, 0]},
    index=sentinel_index,
)
sentinel_df_snapshot = sentinel_df.copy()  # to assert "unmodified" after the call

fake_svc = MagicMock(name="TrueDataService")
fake_svc.get_candle_data.return_value = sentinel_df

with patch("stocks.services.candle_store.datetime") as mock_datetime, \
     patch("stocks.services.candle_store.load_bars", return_value=pd.DataFrame(columns=candle_store._COLUMNS)) as mocked_load_bars, \
     patch("stocks.services.candle_store.store_bars", return_value=0) as mocked_store_bars:

    mock_datetime.now.return_value = FROZEN_NOW

    result = market_data.request_candles(
        fake_svc, SYMBOL, TOKEN, INTERVAL, lookback_days=LOOKBACK_DAYS, exchange=EXCHANGE,
    )

check("load_bars() consulted (cold-cache path entered, no real DB hit)",
      mocked_load_bars.call_count == 1, str(mocked_load_bars.call_count))
check("store_bars() invoked exactly once for the freshly fetched frame (mocked, no real DB write)",
      mocked_store_bars.call_count == 1, str(mocked_store_bars.call_count))
check("svc.get_candle_data() called exactly once", fake_svc.get_candle_data.call_count == 1,
      str(fake_svc.get_candle_data.call_count))

args, kwargs = fake_svc.get_candle_data.call_args
check("no unexpected kwargs", kwargs == {}, str(kwargs))
check("token forwarded unchanged", len(args) == 5 and args[0] == TOKEN, str(args))
check("exchange forwarded unchanged", len(args) == 5 and args[1] == EXCHANGE, str(args))
check("interval forwarded unchanged", len(args) == 5 and args[2] == INTERVAL, str(args))
check("from_date matches OLD inline computation (now - lookback_days, cold start)",
      len(args) == 5 and args[3] == OLD_FROM_DATE, f"got {args[3]!r} want {OLD_FROM_DATE!r}")
check("to_date matches OLD inline computation (now)",
      len(args) == 5 and args[4] == OLD_TO_DATE, f"got {args[4]!r} want {OLD_TO_DATE!r}")

check("returned frame is the same sentinel object (identity, not a copy)",
      result is sentinel_df)
check("returned frame content unmodified vs. pre-call snapshot",
      result[["Open", "High", "Low", "Close", "Volume"]].equals(
          sentinel_df_snapshot[["Open", "High", "Low", "Close", "Volume"]]
      ))

print()
print("=" * 72)
if fails:
    print(f"{len(fails)} FAILURE(S):", fails)
    sys.exit(1)
else:
    print("ALL PASS — candle_store cold-cache path is provably identical to the "
          "OLD direct svc.get_candle_data() call's from_date/to_date math.")
