# INSTITUTIONAL AUDIT — TradePulse AI

**Platform-wide audit. Reviewed as a multi-strategy quantitative fund would review it.**

Scope: Intraday Engine, Short-Term Swing Engine, Long-Term Investment Engine, and all
shared infrastructure. Source of truth: the code, plus `doc/short_term_stock.md`,
`doc/long_term_stock.md`, `doc/INTRADAY_BUY_SELL_LOGIC.md`, and
`doc/INSTITUTIONAL_AUDIT_INTRADAY.md`.

Audit date: 2026-07-26. Codebase: commit `bb7c962` plus uncommitted working-tree changes.

> Companion document: `doc/INSTITUTIONAL_AUDIT_INTRADAY.md` covers the intraday engine's
> own rebuild in depth. This document is platform-wide and assesses all three equity
> engines against each other.

---

## 1. EXECUTIVE SUMMARY

**This is not one platform. It is three platforms of wildly different quality sharing a
database.**

The intraday engine has been rebuilt to a standard acceptable on a real desk: a
transaction-cost gate derived from actual Indian friction (0.14% round-trip, itemised),
risk-parity sizing, a gross-exposure ceiling, a −2% daily kill switch, bar-confirmed exits
that resolve intrabar ambiguity *against* the strategy, a two-axis regime model with
hysteresis gating which trigger families may fire, cross-sectional percentile ranking
across eight factors, correlation clustering with N_eff reported, sector caps, and a
liquidity-filtered universe (ADV ≥ ₹50cr, spread ≤ 5bps). `portfolio_risk.kelly_fraction`
returns `0.0` until 300 trades exist and explains why in the docstring. That is a
professional constraint that most retail systems — and a fair number of funds — get wrong.

The swing engine and the long-term engine have none of it.

### The three findings that matter

1. **The 15–90 day swing engine's table is empty and nothing noticed.** ~~It has never
   produced a signal.~~ **[Corrected 2026-07-26 — see the B1 correction in §5.]** Phase 0
   diagnosis established that the engine *does* work: a rolled-back live replay produced
   5 signals and persisted them cleanly. Rows existed until ~2026-07-20 and were then
   deleted. Since 07-22 the engine has legitimately found nothing, for two measurable
   reasons — a bearish tape, and a self-inflicted handicap where the 10:00 scan reads an
   in-progress daily bar and halves its own volume-gate pass rate. **The finding that
   survives is the observability one:** an empty signal table, a wiped dataset, and a
   multi-day zero-signal streak all went unremarked because nothing monitors output.

2. **The entire long-term book is 100% one promoter group** — ADANIPORTS and ADANIENT,
   both opened 2026-07-22, both with stops sitting exactly on the 15% floor (meaning
   ATR14 > 5% of price on both). Two maximally-correlated positions in the most volatile
   large caps on the exchange, with **zero exit monitoring**. This is not bad luck:
   `scan_long_term_stocks()` ranks by 100-day price return and takes the top 5, so
   correlated complexes rank together, every time.

3. **The fix for both is already in the repo.** Institutional portfolio controls were
   built for intraday and never propagated. `portfolio_risk.apply_portfolio_constraints()`
   would have blocked the second Adani position outright.

**Verdict: IMPROVE — by propagation, not rewrite.** A rewrite would destroy the best asset
in the codebase.

**Institutional Score: 42/100.** Intraday alone scores ~74. The other two engines drag the
platform down.

---

## 2. ARCHITECTURE REVIEW

| Component | Assessment | Score |
|---|---|---|
| **Scheduler** | APScheduler in-process, `Asia/Kolkata`, `max_instances=1`. Dies with the web process. No queue, no worker tier, no job-failure alerting, no dead-letter. Job IDs lie (`_325pm` fires 15:25; the "10:15–3:15 every 30 min" checker fires at 15:45, after close). Long-term has **no job at all** — it executes inside a Telegram *formatter*. | 38 |
| **Data Flow** | Coherent for intraday. For swing/LT, the flow in `trade_engine.py`'s own docstring (`StockDailyData → TradeScanner → Trade`) describes tables nothing writes. Indicators are computed and discarded; `StockDailyData` has `ema20/rsi14/adx14/atr14` columns that are never populated. | 40 |
| **Database** | Postgres, sane indexing, one good partial unique index (`unique_live_signal`). But: two parallel signal models with incompatible status vocabularies (6 vs 14 states), 4 fully dead models, `TradeHistory` FK'd to `ShortTermSignal` so long-term signals **cannot be audited at all**. | 45 |
| **Caching** | `FileBasedCache`. This is the load-bearing failure. Every distributed lock (`trade_engine_scanner_running`, `run_periodic_scanners_running`), the regime state, the correlation clusters, and the **daily loss kill switch** live in it. On a second instance or an ephemeral filesystem, the kill switch does not exist. | 25 |
| **Broker Layer** | Genuinely good. Per-category circuit breakers, global rate limiters (1.05s candle / 0.5s bulk), AG8001 re-auth with retry, WebSocket-first with staleness TTL, self-healing socket under a non-blocking lock, bootstrap dedup via `Event`, 240s candle cache keyed on rounded lookback. The most mature layer in the codebase. | 76 |
| **Signal Engine** | Bimodal. Intraday: cross-sectional, regime-gated, cost-aware. Swing: six absolute-threshold gates, produces nothing. LT: **one** gate (`close > EMA50 > EMA200`) on a 200-EMA computed from ~137 bars, which has not converged. | 48 |
| **Lifecycle** | Intraday now records `exit_reason` (`LEVEL_HIT` / `LEVEL_HIT_LTP` / `TIME_STOP` / `SQUARE_OFF_CUTOFF` / `DAILY_LOSS_LIMIT`) — exactly right for attribution. Swing overwrites every terminal status with `ARCHIVED`, so `win_rate` counts `HIT_TARGET` rows that can never exist → permanently 0.0%. LT never leaves `ACTIVE`. | 44 |
| **Risk Engine** | Intraday: real. Swing: per-trade only. LT: **none**. There is no cross-engine risk aggregation — intraday's ₹5,00,000 equity assumption is unaware of swing or LT capital. | 46 |
| **Portfolio Engine** | Exists for intraday (`portfolio_risk.py`). Does not exist for swing or LT. Three mutually contradictory sizing conventions across three surfaces. | 35 |
| **Dashboard** | `ProSystem.jsx` fetches once on mount, no polling. `get_dashboard_data()` runs `SignalHistory.objects.all()` unbounded and triggers a 365-day candle fetch per uncached request. | 40 |
| **Telegram** | Best non-broker subsystem. DB-backed queue, `select_for_update`, 3-retry with backoff, event-type idempotency. Flaw: `process_telegram_queue()` routes **everything** to the short-term chat regardless of origin. | 68 |
| **API** | JWT, thin views, correct read-only discipline (page views cannot trigger scans — explicitly commented). `CronScannerTriggerView` has a hardcoded fallback secret in source: `"trade_pulse_secure_cron_trigger_2026"`. | 55 |
| **Scalability** | Single process. Scanner is 50–75 sequential candle fetches at ≥1.05s. Cannot horizontally scale — the file cache and in-process scheduler both break. | 25 |
| **Fault Tolerance** | Good broker-level degradation. But an empty `ShortTermSignal` table for the platform's entire life proves there is **no output monitoring whatsoever**. | 35 |
| **Concurrency** | Locks exist and the rationale is documented (TLS corruption from a shared `requests.Session`). But `run_daily_scanner` and `run_periodic_scanners` take **different** keys, so a manual trigger can run two scans against one session concurrently — the exact failure the locks were added to prevent. | 40 |
| **Performance** | 240s candle cache and bulk batching are correct. `_get_active_long_term_holdings_lines()` issues one sequential `orch.get_price()` per holding — and since LT positions never close, this gets monotonically slower forever. | 45 |

---

## 3. TRADING REVIEW — Would this survive professional trading?

- **Intraday: yes, structurally.** Unproven, but the arithmetic is honest.
- **Swing: unknown — it has never traded.**
- **Long-term: no.**

| Dimension | Intraday | Swing | Long-Term |
|---|---|---|---|
| **Entry** | Closed-bar only, no repaint, live matches backtest ✅ | `ltp <= entry` pullback; entry is the ~10:00 price, checked 12×/day so touches between polls are missed ⚠️ | Born `ACTIVE`, no trigger, no confirmation ❌ |
| **Exit** | Bar-confirmed, pessimistic on ambiguous bars, time stop, cutoff ✅ | Once daily @15:25; 20-EMA trail evaluated **before** targets, so a gap through T3 exits at close as `TRAILING_EXIT` ⚠️ | **None** ❌ |
| **Market filter** | Two-axis regime, hysteresis, gates trigger families ✅ | Binary EMA20/50; BEARISH aborts strict then the relaxed pass **skips the gate entirely** ❌ | **None** ❌ |
| **Sector filter** | Max 2/sector ✅ | None ❌ | None — `"Large Cap"` is a hardcoded string ❌ |
| **Relative strength** | Cross-sectional percentile, 20% weight ✅ | vs Nifty, strict pass only ⚠️ | None ❌ |
| **Liquidity** | ADV ≥ ₹50cr, spread ≤ 5bps, price ≥ ₹100 ✅ | Share count only ⚠️ | Share count only ⚠️ |
| **Momentum** | Regime-gated ✅ | ADX + RSI scored ✅ | 100-day return = the *entire* ranking ❌ |
| **Breakout** | VA/POC on closed bars ✅ | 52wH ±5% OR 20dH ✅ | None ❌ |
| **ATR** | 0.8× stop, vol-fit scoring ✅ | 2× stop, 10% floor ✅ | 3× stop, 15% floor — **discarded by the live writer** ❌ |
| **EMA** | Not used | 50/200 stack + 20 trail ✅ | 200-EMA on ~137 bars — not converged ❌ |
| **R:R** | ≥1.5 **and** target ≥ 3× cost ✅ | Fixed 2.0, so the `rr < 2.0` check is unreachable ⚠️ | 2.5R computed, then overwritten to 2:1 ❌ |
| **Holding period** | 40-min time stop ✅ | ATR-derived 15–90d ✅ | "1–2 years" is a string; nothing enforces it ❌ |
| **Capital allocation** | Risk-parity + gross cap ✅ | None ❌ | None ❌ |
| **Correlation** | Clustered, max 2/cluster ✅ | None ❌ | None — hence 100% Adani ❌ |
| **Expected value** | Cost gate is an explicit EV floor ✅ | No base rates ❌ | No base rates ❌ |
| **Position sizing** | `qty = (equity × 0.30%) / (entry − stop)` ✅ | Client-side display only ❌ | Three conflicting conventions ❌ |
| **Max drawdown** | −2% daily kill switch ✅ | None ❌ | None ❌ |

### The single worst trading defect in the platform

The swing scanner's **relaxed fallback**. When the strict pass finds nothing — i.e. when
conditions are *worst* — it lowers ADX 25→15, drops the relative-strength filter entirely,
widens 52wH proximity 5%→10%, drops volume 1.5×→1.0×, **and skips the BEARISH market
gate**. It systematically trades worse setups in worse markets.

The `ranking_service` docstring already names this defect:

> *"the exact opposite of the old `relaxed` fallback, which lowered standards precisely
> when conditions were worst."*

It was diagnosed for intraday and left running in swing.

### Second worst

Relaxed mode uses `adx_limit = 15` in the *scoring* term
`min(12.5, (adx − adx_limit) × 0.5)`. The same ADX scores 5 points higher in relaxed mode.
Relaxed and strict scores are not comparable, yet both write to the same `ai_score` column
that is ranked and displayed.

---

## 4. MISSING INSTITUTIONAL FEATURES

| Feature | Intraday | Swing | LT |
|---|---|---|---|
| Sector rotation | Partial (strength ranking, no rotation model) | ❌ | ❌ |
| Market breadth | ✅ `_market_breadth`, 25-name sample | ❌ | ❌ |
| VIX | ✅ optional, degrades gracefully | ❌ | ❌ |
| Volatility regime | ✅ two-axis + hysteresis | ❌ | ❌ |
| Institutional flow | ❌ | ❌ | ❌ |
| Delivery % | ❌ *(parsed by `bhavcopy_service`, upsert disabled at line 282)* | ❌ | ❌ |
| FII/DII | ❌ *(aggregate exists as a dashboard card, never per-stock, never in selection)* | ❌ | ❌ |
| Options data / OI / PCR / IV | ❌ *(`option_chain_service` exists, feeds a card only — no cross-signal use)* | ❌ | ❌ |
| Relative volume | ✅ | ✅ | ❌ |
| Earnings filter | ❌ **Can be long into an earnings print with no awareness.** | ❌ | ❌ |
| Economic calendar | ❌ | ❌ | ❌ |
| Corporate actions | ❌ **No split/bonus adjustment. A 1:5 split shows as an 80% stop-out.** | ❌ | ❌ |
| Correlation | ✅ | ❌ | ❌ |
| Beta | ❌ | ❌ | ❌ |
| Alpha attribution | ❌ | ❌ | ❌ |
| Portfolio optimization | Constraint-based only, no MVO/HRP | ❌ | ❌ |
| Kelly sizing | ✅ correctly gated at n≥300 | ❌ | ❌ |
| Factor exposure | ❌ | ❌ | ❌ |
| Quality factor | ❌ — **no fundamental data exists anywhere** | ❌ | ❌ |
| Value factor | ❌ | ❌ | ❌ |
| Growth factor | ❌ | ❌ | ❌ |
| Liquidity factor | ✅ 8% weight | ❌ | ❌ |

**Two omissions are dangerous, not merely absent:** corporate actions and earnings.
Unadjusted price series will manufacture false stop-outs and false breakouts. Neither is
expensive to fix.

---

## 5. BUGS

### P0 — Live money or silent total failure

| # | Bug | Evidence |
|---|---|---|
| B1 | **~~Swing engine has produced zero signals, ever~~ — SUPERSEDED, see below** | `ShortTermSignal` count = 0, all statuses |

> ### ⚠️ B1 CORRECTION — 2026-07-26, after Phase 0 diagnosis
>
> **The original claim was wrong.** The swing engine has produced signals, and the
> scanner is functional. Evidence from the `TelegramLog` timeline and a rolled-back
> live replay:
>
> - `DAILY_SCANNER_SUMMARY` fired 2026-07-17 — that message is only sent when
>   `new_trades` is non-empty, so rows were created.
> - `EOD_PORTFOLIO_STATUS` (×2) and `SWING_STATUS_UPDATE` fired 2026-07-20 — both return
>   early when there are no ACTIVE rows, so positions were live that day.
> - `TradeHistory` is also 0, which is consistent with `ShortTermSignal` rows being
>   **deleted** (the FK cascades), not with rows never having existed.
> - A rolled-back replay of `_run_daily_scanner_impl(relaxed=True)` on 2026-07-26
>   produced **5 signals and persisted them cleanly** (HEG, LALPATHLAB, UNITDSPR,
>   RRKABEL, PAYTM).
>
> **Revised finding — three compounding causes, no crash:**
>
> 1. **The table was cleared** between 2026-07-20 and 2026-07-22. Cause not yet
>    identified; `repair_signals.py` and `recreate_missing_tables.py` exist in the repo
>    and are candidates.
> 2. **The 10:00 scan reads an in-progress daily bar**, which halves the volume-gate
>    pass rate. Measured over the same 49 candidates: strict `≥1.5×` falls **8 → 5**,
>    relaxed `≥1.0×` falls **19 → 11**. Today's partial volume drags `vol_5d` down far
>    harder than `vol_20d`, so the *ratio* collapses. This is the quantified case for
>    scanning on closed bars.
> 3. **The market has been BEARISH** since at least 2026-07-22 (Nifty 23,767 below both
>    EMA20 and EMA50), so the strict pass aborts instantly every day and only the
>    relaxed pass ever runs.
>
> `SCANNER_NO_SETUPS` on 07-22, 07-23 and 07-24 is therefore the engine working as
> written, on a weak tape, with a self-inflicted volume handicap — not a failure.
>
> **The observability finding stands and is unchanged:** nothing alerts on an empty
> table or a zero-signal streak, and nothing recorded that the table was wiped.

**Measured funnel, 2026-07-26 (strict pass, 500-name Nifty 500 universe):**

```
universe                     500
tokens resolved              500   (0 unresolved)
quotes returned              500
  REJECT change_pct <= 0.5  -362
  REJECT volume <= 50000      -7
prefilter survivors          131
  REJECT rank > 50            -81
scored                        50
  REJECT trend stack          -30   <-- dominant gate
  REJECT ADX < 25             -12
  REJECT breakout              -3
  REJECT volume ratio          -2
  REJECT len < 201             -1
  REJECT rel strength          -1
PASS                            1   (NUVAMA, ai_score 63.28)
```

The `close > EMA50 > EMA200` trend gate rejects 30 of 50 — expected in a bearish tape.
ADX rejects 12 more, with a cluster of near-misses at 21–24.5 against the hard 25
threshold, which is the evidence for converting ADX from a hard gate to a ranked factor
(architecture decision D4).
| B2 | **Long-term book has no exit path** | `update_signal_outcomes` excludes `long_term`; `update_pro_system_outcomes` has 0 callers |
| B3 | **100% single-group concentration, unguarded** | 2/2 positions Adani |
| B4 | **Kill switch in a file cache** | `DAILY_HALT_CACHE_KEY` → `FileBasedCache`; does not survive multi-instance |
| B5 | **`_exit_signal()` TypeError** | `trade_engine.py:610,615` pass 4 args; signature requires 5. Any intraday-check exit crashes |
| B6 | **Uncaught `IntegrityError`** | `get_pro_system_data`'s date-scoped `.get()` re-attempts `create()` for an already-ACTIVE symbol → violates `unique_live_signal` |

### P1 — Wrong results

| # | Bug |
|---|---|
| B7 | Swing `win_rate` structurally 0.0% — counts statuses `_exit_signal` never leaves on the row |
| B8 | `TIME_STOP` exits match neither legacy win nor loss bucket (`"Time-Stop Exit"` contains neither `'Target'` nor `'Stop'`/`'Trailing'`) |
| B9 | A +8.3% trailing exit is classified a **loss** by `hit_sl_legacy` string matching |
| B10 | Relaxed vs strict `ai_score` not comparable (variable `adx_limit`), stored in one column |
| B11 | LT ATR stop/target computed then discarded by the live writer (×0.5 / ×2.0 substituted) |
| B12 | LT 200-EMA on ~137 bars from a 200-*calendar*-day window; guard only requires 100 |
| B13 | Claude daily insight reads `Stock`/`IntradaySignal` — **both permanently empty** |
| B14 | `unrealized_pnl` = sum of percentages across positions. Not a portfolio return under any weighting |
| B15 | `NIFTY500_FALLBACK` contains dead tickers (`HDFC`, `AAPL`, `RCOM`, `MTNL`, `TVSMOTORS`) |

### P2 — Race conditions & dead code

| # | Issue |
|---|---|
| B16 | `run_daily_scanner` and `run_periodic_scanners` hold different lock keys → concurrent scans on one `requests.Session` |
| B17 | Activation checker (10:15) fires while the 10:00 scanner is still running (~3–4 min) |
| B18 | Telegram *formatter* `_send_telegram_scanner_summary` runs the entire LT scan and persists DB rows — a side effect in a presentation function |
| B19 | LT only runs when the strict swing pass is **empty**, so a BEARISH market makes it *more* likely to run |
| B20 | Dead: `TradeScanner`, `Trade`, `StockDailyData`, `SignalChangeLog`, `update_pro_system_outcomes`, `scan_short_term_stocks`, `trade_engine.run_intraday_check`, `ThreadPoolExecutor` import, `expiry_days`, `fundamental_score`, statuses `CLOSED`/`CANCELLED`/`COOLDOWN` |
| B21 | `strategy_config.json` keys `nifty_500_prefilter_pct`, `_volume`, `top_candidates_count` are **not read** — values hardcoded. Tuning the config does nothing |
| B22 | Three ADX implementations (two Wilder, one simple rolling) |
| B23 | `get_dashboard_data` → unbounded `.objects.all()` + a 365-day candle fetch per uncached request |
| B24 | Hardcoded cron secret fallback in source |
| B25 | `REVIEW_REQUIRED` removes a position from the EOD evaluation set — it stops being monitored while still open |

---

## 6. RISK ASSESSMENT

| Risk | Severity | Live now? |
|---|---|---|
| Unmonitored, maximally-correlated LT book | **Critical** | **Yes** |
| Kill switch not durable across processes | **Critical** | Yes |
| No corporate-action adjustment | High | Yes |
| No earnings blackout | High | Yes |
| No cross-engine capital aggregation — three engines each assume they own the account | High | Yes |
| Single-process scheduler = single point of failure | High | Yes |
| No output monitoring (B1 went undetected indefinitely) | High | Yes |
| No execution layer — zero fill/slippage realism outside intraday's 0.04% assumption | Medium | Yes |
| Angel One single-vendor dependency | Medium | Yes |

**Immediate action: halve the Adani exposure and attach a manual stop today.** That is an
open, unmanaged, ~30%-if-both-stop risk in 5%-ATR names with no automated exit.

---

## 7. IMPROVEMENT ROADMAP

### P0 — Now (days)

1. Diagnose B1. An engine that has never fired is the largest unknown.
2. Restore an LT exit auditor; reduce Adani to one name manually today.
3. Move cache to Redis. The kill switch must be durable.
4. Fix B5 (`_exit_signal` arity) and B6 (`IntegrityError`).
5. Output monitoring + alerting: "scanner ran, persisted 0" must page someone.
6. Rotate the cron secret out of source.

### P1 — Institutional (weeks)

7. **Promote `universe_service` / `regime_service` / `ranking_service` / `portfolio_risk`
   to shared infrastructure. Re-platform swing and LT on them.** Highest-ROI item in this
   document.
8. Delete the relaxed fallback; replace with cross-sectional thresholding that may
   legitimately return zero.
9. Corporate-action adjustment; earnings blackout.
10. Persist backtest results → base rates → real probability and EV.
11. Re-enable delivery %.
12. Buy a fundamentals feed. Until then, delete the hardcoded ROE/D-E/growth constants —
    they are worse than absent because they look like data.
13. Cross-engine capital and risk aggregation.
14. Server-side sizing, one convention.

### P2 — Performance

15. Worker tier (Celery/RQ) separate from web; parallelise scans behind a shared
    token-bucket rate limiter.
16. Bound `get_dashboard_data`; bulk-fetch LT holding prices.
17. Persist indicators to `StockDailyData` or delete the model.

### P3 — Nice to have

18. Beta/alpha attribution, factor exposure, HRP allocation, sector-rotation model,
    OI/PCR/IV as cross-signal inputs, second data vendor.

---

## 8. INSTITUTIONAL SCORE

| Module | Score | Note |
|---|---|---|
| Architecture | **52** | Broker layer 76; file-cache and single-process drag it |
| Data Quality | **34** | Price-only; delivery disabled; fundamentals fabricated |
| Trading Logic | **48** | Intraday 78, swing 55 (untested), LT 22 |
| Risk Management | **46** | Intraday 85, swing 45, LT 8 |
| Portfolio Management | **35** | Exists in exactly one engine |
| Automation | **45** | Runs reliably; one engine yields nothing |
| Institutional Readiness | **30** | No execution, no attribution, no base rates |
| Code Quality | **52** | New services excellent; legacy duplicated and dead |
| Maintainability | **40** | Two parallel scanners, four dead models, side-effect coupling |
| Reliability | **42** | Good degradation, but B1 proves no observability |

### OVERALL: 42 / 100

Intraday in isolation: **~74**. That gap is the whole story.

---

## 9. TOP 50 IMPROVEMENTS RANKED BY ROI

### Tier 1 — Do this week (effort: hours–days)

| # | Action | Why |
|---|---|---|
| 1 | Diagnose empty `ShortTermSignal` | Largest unknown |
| 2 | Cut Adani to one position, manual stop | Open live risk |
| 3 | Redis for cache | Kill-switch durability |
| 4 | Alert on "scan persisted 0" | Would have caught #1 |
| 5 | Fix `_exit_signal` arity (B5) | Crashes on exit |
| 6 | Wrap LT writer, fix `IntegrityError` (B6) | Silent LT failure |
| 7 | LT exit auditor (reinstate, scoped to LT only) | No exits today |
| 8 | Apply `apply_portfolio_constraints` to LT | Blocks Adani #2 |
| 9 | Capture `Industry` from the NSE CSV | Unblocks sector caps everywhere |
| 10 | Rotate cron secret | Credential in git |
| 11 | Delete the relaxed fallback in swing | Trades worst setups in worst markets |
| 12 | Delete hardcoded ROE/D-E/growth | Fabricated data in the UI |
| 13 | Re-enable delivery upsert | One line, real signal |
| 14 | Fix win-rate to read `exit_reason` | Metric is 0.0% by construction |
| 15 | Unify the two scanner lock keys | Removes the concurrency race |

### Tier 2 — This month

| # | Action |
|---|---|
| 16 | Re-platform swing on `ranking_service` |
| 17 | Re-platform swing on `portfolio_risk` |
| 18 | Re-platform LT on both |
| 19 | Cost gate for swing/LT |
| 20 | Risk-parity sizing server-side, all engines |
| 21 | Cross-engine gross exposure |
| 22 | Corporate-action adjustment |
| 23 | Earnings blackout |
| 24 | Persist backtest → base rates |
| 25 | Probability + EV in signals |
| 26 | `exit_reason` on swing/LT |
| 27 | Audit table for `SignalHistory` |
| 28 | Bound `get_dashboard_data` |
| 29 | Bulk LT price fetch |
| 30 | Move LT out of the Telegram formatter into its own job |

### Tier 3 — This quarter

| # | Action |
|---|---|
| 31 | Fundamentals vendor |
| 32 | Quality/Value/Growth factors |
| 33 | Beta + alpha attribution |
| 34 | Factor-exposure reporting |
| 35 | Worker tier |
| 36 | Parallel scan behind a token bucket |
| 37 | Delete 4 dead models |
| 38 | Delete 3 dead functions |
| 39 | Consolidate 3 ADX implementations |
| 40 | Make `strategy_config.json` actually read |
| 41 | Unify the two signal models |
| 42 | LT 365-day window (fix the 200-EMA) |
| 43 | Route Telegram by origin |
| 44 | Sector-rotation model |
| 45 | Volatility targeting live |

### Tier 4 — Opportunistic

| # | Action |
|---|---|
| 46 | OI/PCR/IV as cross-signal inputs |
| 47 | HRP allocation |
| 48 | Second data vendor |
| 49 | Economic calendar |
| 50 | Execution/OMS layer |

---

## 10. FINAL VERDICT

# IMPROVE

- **Not *Keep*** — three P0 defects are live, and one engine has never worked.
- **Not *Rewrite*** — a rewrite would discard `regime_service`, `ranking_service`,
  `portfolio_risk`, `universe_service`, the cost model, and the broker layer, which are
  the most valuable code in the repository.
- **Not *Replace*** — nothing off-the-shelf provides an India-specific, cost-aware,
  regime-gated intraday stack, and one already exists here.

**The defining problem is not a lack of institutional engineering. It is that it was done
once, for one engine, and the other two were left on the old architecture.**

A docstring exists explaining why lowering standards in bad conditions is wrong — and the
swing engine still does it. Correlation clustering was built — and the long-term book is
100% one promoter group. Kelly was gated on sample size because `p` has infinite standard
error with no track record — while two engines run with no track record at all.

Propagate what has been built. Delete the legacy duplicates. Then, and only then, buy the
fundamental data that would make an institutional mandate executable.

### Caveat on these numbers

Every score above measures **process, not profitability**. With `ShortTermSignal` empty,
no persisted backtest, and 62% of the specialist history sitting in `EXPIRED`/`CANCELLED`
with no P&L, **it is not possible to assess whether any of these strategies makes money.**

The 65% specialist hit rate (11 `HIT_TARGET` / 6 `HIT_SL`) is **not** evidence of edge —
short-premium payoffs are asymmetric, and a 65% hit rate is entirely consistent with
negative expectancy. Item 24 is what converts this audit from an engineering review into
an investment one.

---

## APPENDIX — Live database state at audit time

Queried 2026-07-26 against the production Supabase instance.

```
ShortTermSignal          — 0 rows.  Zero. Any status. Ever.
SignalHistory/intraday   — 0 rows.
SignalHistory/long_term  — 2 rows, both ACTIVE
SignalHistory/specialist — 45 rows (11 HIT_TARGET, 6 HIT_SL, 21 EXPIRED, 7 CANCELLED)
SignalHistory/commodity  — 4 rows (dead category)
```

### The long-term book in full

| Symbol | Entry | Stop | Target | Risk | Reward | R:R | Generated |
|---|---|---|---|---|---|---|---|
| ADANIPORTS | ₹1,841.90 | ₹1,565.62 | ₹2,532.61 | −15.00% | +37.50% | 2.5:1 | 2026-07-22 |
| ADANIENT | ₹3,187.50 | ₹2,709.38 | ₹4,382.81 | −15.00% | +37.50% | 2.5:1 | 2026-07-22 |

Both stops sit exactly on the 15% floor: `1841.90 × 0.85 = 1565.615` and
`3187.50 × 0.85 = 2709.375`. The floor only binds when `3 × ATR14 > 0.15 × price`, i.e.
**ATR14 exceeded 5% of price on both names** — roughly 3× a typical Indian large-cap.

Both level sets are `entry × 0.85` and `entry + 2.5 × sl_points`, which are the
**ATR/15%-floor levels of the legacy manual path** (`get_pro_system_data`), not the
scheduled path's `×0.5` / `×2.0`. The scheduled long-term writer
(`_scan_new_long_term_setups`) has therefore **never persisted a row**.
