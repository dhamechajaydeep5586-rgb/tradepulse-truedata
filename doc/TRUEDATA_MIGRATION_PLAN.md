# Angel One → TrueData Migration Plan

**Status: code migration complete (2026-08-03). Angel One is fully removed from this
codebase.** This doc now records what was done and — more importantly — exactly which
parts still need to be confirmed against a live TrueData account before this is trusted
in production. Read the "Verify before trusting" section before running any real trades.

Target repo: `github.com/dhamechajaydeep5586-rgb/tradepulse-truedata` was empty when this
migration started — there was nothing to diff against, so this repo (renamed from
`tradepulse-ai-main` to `tradepulse-truedata`) is the source of truth, not a merge of two
histories.

## What changed, in one paragraph

`angel_one_service.py` and `angel_one_streamer.py` are deleted. Their replacements —
`truedata_service.py` (REST: candles, quotes, option chain, Greeks) and
`truedata_streamer.py` (WebSocket: live ticks) — expose the same method names
(`get_candle_data`, `get_bulk_quotes`, `get_token_map`, `get_stock_quote`,
`get_option_quote`, `is_market_open`, ...) so every one of the ~50 call sites across the
codebase needed only its import line changed, not its logic. The one design decision that
made this possible: TrueData addresses everything by symbol **name** (`"RELIANCE"`,
`"NIFTY 50"`, `"CRUDEOIL-I"`), not a broker-issued numeric token, so `get_token_map()` is
now an identity map and every `token` field in this codebase holds a symbol string. Nothing
here ever parsed `token` as an int — checked before committing to this design — so it
required zero call-site logic changes.

## Phases (as actually executed)

- **Phase 0 — Inventory.** `grep -ril "angel.?one|smartapi"` found ~70 files. Full list
  preserved below for reference.
- **Phase 1 — Widen the seam.** `market_data/gateway.py`'s existing forwarding layer had its
  type hints swapped from `AngelOneService` to `TrueDataService`; its shape was already
  provider-agnostic.
- **Phase 2 — Built `truedata_service.py` / `truedata_streamer.py`** against the real
  TrueData API (`TrueDataAPIDocument/` — Market Data API v2.6 PDF + TD Postman collections),
  not guessed. Auth (`auth.truedata.in/token`, OAuth2 password grant), historical bars
  (`history.truedata.in/getbars`), bulk quotes (`getLTPBulk`), option chain + Greeks
  (`api.truedata.in/getoptionchain`, `greeks.truedata.in/api/getLTPwithGreeks`), and
  WebSocket streaming (`wss://push.truedata.in:8084`, JSON tick/touchline/bar messages).
- **Phase 3 — Swapped the singleton.** `settings.ANGEL_ONE` → `settings.TRUEDATA`,
  `apps.py`'s startup init, `init_angel_one` → `init_truedata` management command.
- **Phase 4 — Migrated ~50 call sites.** Mechanical import swap
  (`angel_one_service`→`truedata_service`, `get_angel_one_instance`→`get_truedata_instance`,
  etc.) across every service, view, and management command that touched the old module.
- **Phase 5 — Token re-derivation.** The non-mechanical part. Hardcoded Angel One numeric
  index tokens (`"99926000"` for NIFTY 50, 11 sector index tokens in `shared/sector.py`,
  etc.) were replaced with TrueData's plain-name symbols (`"NIFTY 50"`, `"NIFTY BANK"`, ...).
  The specialist (strangle-selling) engine's direct instrument-master reads
  (`delta_hedge_service.get_lot_size`/`get_nse_option_strikes`/`get_nse_option_quote`) were
  rewritten against two new `TrueDataService` methods, `get_expiry_list()` and
  `get_option_chain()`, shaped like the old Angel One master rows so the surrounding
  rollover/filter logic didn't need to change.
- **Phase 6 — Docs.** `CLAUDE.md` and `doc/setup_guide.md` updated (broker section, env
  vars, rate limits, file references). Not every doc file was rewritten — see below.
- **Phase 7 — Deleted Angel One.** `angel_one_service.py`, `angel_one_streamer.py`,
  `init_angel_one.py` management command, `smartapi-python`/`pyotp` from
  `requirements.txt`. Also deleted `backend/debug_waf.py` (a script purpose-built to
  diagnose Angel One's undocumented WAF behavior — has no TrueData equivalent, since
  TrueData's rate limits are documented rather than mysterious).

## Verification performed

- `python3 -m py_compile` clean across the entire backend.
- `manage.py check` clean (full app registry loads, including `apps.py`'s TrueData init
  path — correctly logs "Credentials missing" rather than crashing with no `.env` set).
- Every touched management command (`init_truedata`, `generate_strangle_signals`,
  `scan_strangles`, `backfill_candles`, `backfill_option_selling_metadata`,
  `run_historical_backtest`) imports and loads cleanly.
- `stocks.tests_phase5b_regression` (6 tests) and `stocks.tests` (32/34 — the other 2 fail
  on a Python-3.14-vs-Django template-context bug in the *test venv*, unrelated to this
  migration and not present in the project's pinned Python 3.13) pass.
- All 5 standalone `selfcheck_*.py` scripts pass, including the ones exercising
  `market_data/gateway.py`'s pass-through and `candle_store.py`'s cache-key math against
  the new symbol-string tokens.
- **None of this hit the live TrueData API** — no credentials were available in this
  session. Everything above verifies the code is wired together correctly and the pure
  logic (interval mapping, tick field parsing, expiry format conversion) is correct against
  hand-built fixtures matching the documented response shapes. It does **not** verify that
  TrueData's real responses actually match those documented shapes.

## Verify before trusting (in priority order)

1. **`get_option_chain()`'s CSV column names** (`truedata_service.py`). The `getoptionchain`
   endpoint's exact response schema was not available during this migration (no sample
   response in the docs or Postman collection, no live account to call it). Field lookups
   try multiple likely names defensively, but this feeds strike/expiry/lot-size resolution
   for the **strangle-selling (specialist) engine** — real money. Confirm against one real
   call before that engine trades.
2. **`getLTPwithGreeks` response shape** (`get_option_quote()`) — same issue, feeds the
   option-buying engine's delta-based strike selection (0.40–0.60 band).
3. **`getLTPBulk` response columns** (`get_bulk_quotes()`) — assumed `symbol,ltp,high,low,
   close,change,changeper,volume,oi` based on convention, not a captured sample.
4. **Sector index symbol names** (`shared/sector.py` — NIFTY MEDIA, NIFTY PSU BANK, etc.)
   and **INDIA VIX** (`shared/regime.py`) — only "NIFTY 50" and "NIFTY BANK" are directly
   confirmed in TrueData's own docs; the rest follow the same naming convention but weren't
   individually checked. A wrong index name fails the same way the old Angel One sector-token
   bug did: silently returns data for a *different* valid instrument, not an error (see
   `shared/sector.py`'s own 2026-07-26 postmortem comment on that exact failure mode).
5. **MCX/commodity price units** — moot for now (MCX was removed platform-wide before this
   migration), but if MCX ever comes back, TrueData's rupee-vs-paise convention for that
   segment needs its own confirmation; don't assume it matches Angel One's.
6. **Bulk quote chunk size** (50/request, in `delta_hedge_service.py`'s warm-up path) —
   carried over from Angel One's documented cap, not independently confirmed for
   `getLTPBulk`.

## Not done in this pass

- **Docs beyond `CLAUDE.md`/`setup_guide.md`.** ~20 other `.md` files (architecture docs,
  audit reports, roadmaps) mention Angel One in passing — none of them are *about* Angel One
  specifically (verified: no dedicated Angel-One-only doc file exists anywhere in the repo),
  so none were deleted. `doc/MARKET_DATA_ENGINE_ARCHITECTURE.md` is the next-highest-value
  one to update if you want the docs fully current (44 Angel One mentions in 514 lines).
- **Pushing to the `tradepulse-truedata` remote.** This repo isn't git-initialized yet.

## Full inventory (grep: `angel.?one|smartapi`, case-insensitive, as of migration start)

```
backend/config/settings.py
backend/debug_waf.py                                  [deleted]
backend/global_market/migrations/0004_remove_dow_nasdaq.py            [historical, untouched]
backend/global_market/services/data_fetch_service.py
backend/scratch/audit_specialist_legs.py
backend/scratch/debug_bajaj_price.py
backend/stocks/apps.py
backend/stocks/management/commands/backfill_candles.py
backend/stocks/management/commands/backfill_option_selling_metadata.py
backend/stocks/management/commands/generate_strangle_signals.py
backend/stocks/management/commands/init_angel_one.py   [deleted -> init_truedata.py]
backend/stocks/management/commands/run_historical_backtest.py
backend/stocks/management/commands/scan_strangles.py
backend/stocks/migrations/0031-0035_*.py                              [historical, untouched]
backend/stocks/models.py
backend/stocks/selfcheck_bulk_quote_chunking.py
backend/stocks/selfcheck_candle_store_parity.py
backend/stocks/selfcheck_download_queue.py
backend/stocks/selfcheck_market_data_gateway.py
backend/stocks/selfcheck_option_buying_target_sl.py
backend/stocks/services/angel_one_service.py            [deleted -> truedata_service.py]
backend/stocks/services/angel_one_streamer.py           [deleted -> truedata_streamer.py]
backend/stocks/services/bhavcopy_service.py
backend/stocks/services/candle_store.py
backend/stocks/services/delta_hedge_service.py
backend/stocks/services/intraday_service.py
backend/stocks/services/live_signal_service.py
backend/stocks/services/market_data/__init__.py
backend/stocks/services/market_data/download_queue.py
backend/stocks/services/market_data/gateway.py
backend/stocks/services/market_data/tick_aggregator.py
backend/stocks/services/market_data/ws_read.py
backend/stocks/services/market_data_orchestrator.py
backend/stocks/services/market_intelligence_service.py
backend/stocks/services/option_buying_service.py
backend/stocks/services/pro_system_service.py
backend/stocks/services/shared/calendar_service.py
backend/stocks/services/shared/regime.py
backend/stocks/services/shared/sector.py
backend/stocks/services/shared/universe.py
backend/stocks/services/short_strangle_scanner.py
backend/stocks/services/signal_utils.py
backend/stocks/services/swing_service.py
backend/stocks/services/swing_signals.py
backend/stocks/services/telegram_service.py
backend/stocks/services/trade_engine.py
backend/stocks/tests.py
backend/stocks/tests_phase5b_regression.py
backend/stocks/updater.py
backend/stocks/views.py
frontend/src/components/DeltaHedgePanel.jsx             [cosmetic string only, updated]
+ CLAUDE.md, V2_INSTITUTIONAL_ROADMAP/*.md, doc/*.md, docs/dashboard-cards/04-option-selling.md
```
