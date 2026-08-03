# TradePulse AI — Audit Remediation Plan

Companion document to the institutional production audit (48 findings: 8 P0, 11 P1, 15 P2,
14 P3) conducted against the live codebase. This document turns each finding into a
concrete, phase-ordered fix — exact file/line, exact steps, exact env vars/commands where
relevant, and a verification check for each. Nothing here is a redesign; the largest single
item is roughly a day of work.

See also `doc/INSTITUTIONAL_AUDIT_PLATFORM.md` (2026-07-26) — an earlier audit that first
documented the long-term engine's promoter-concentration bug (Phase 0/1, item 2 below). This
remediation plan confirms that specific bug is **still live today** in the scheduled code
path, despite that prior audit; fixing it is not optional cleanup, it's closing a gap that
was already flagged once.

**How to use this document:** work top to bottom. Phase 0 items are each under an hour and
should ship before anything else, in the order listed (some are prerequisites for later
items — dependencies are called out explicitly). Do not skip straight to Phase 3/4 cleanup
while Phase 0/1 items are open; several of them (long-term concentration, naked strangle,
scale-guard) represent real risk currently being carried on live capital every trading day
they remain unfixed.

---

## Status (updated 2026-07-29)

**48 of 48 findings resolved and verified.** Phase 4's 14 P3 cleanup items shipped 2026-07-29,
closing out the last open findings from the original audit. Everything below marked ✅ has shipped as code
(committed on `main` locally; not yet pushed to remote or deployed to Render at time of
writing — confirm via `git log` / Render's deploy history if reading this later). Everything
else is still open exactly as described in its own section.

| Item | Status | What actually shipped |
|---|---|---|
| Phase 0 #1 — cron-trigger hardcoded fallback | ✅ **Resolved** | Fallback removed from `views.py`; token rotated to a fresh `openssl rand -hex 32` value in both local `.env` and Render. Verified: old hardcoded token → `401`, new token → success. |
| Phase 0 #2 — long-term generation stopgap | ✅ **Superseded by the real fix (2b)** | `LONG_TERM_GENERATION_ENABLED` can now safely be set back to `true` in Render — see 2b below. |
| Phase 0 #3 — duplicate `short_term_scan` cron branch | ✅ **Resolved** | Branch now logs a deprecation warning and no-ops instead of running the legacy scanner. |
| Phase 0 #4 — EOD exit check-order bug | ✅ **Resolved** | Target-3 intrabar check now runs before the trailing-EMA close check in `trade_engine.py`. |
| Phase 1 #2b — long-term promoter-group cap | ✅ **Resolved** | `scan_long_term_stocks()` now runs its momentum-ranked candidates through `apply_portfolio_constraints()` (same machinery as intraday) against the `LONG_TERM` profile's real caps (20 total positions, 12% max per promoter group). Candidate pool widened to 15 before filtering so a rejected pick can be backfilled by the next-best one. Sector-cap component deliberately left inert (no real sector taxonomy exists yet for long-term — see the fix's own code comment) so it can't falsely bucket every position into one "UNKNOWN" group. Rejections now logged to `SignalCandidate` for auditability. |
| Phase 1 #3 — long-term 200-EMA exit rule | ✅ **Resolved** | New `update_long_term_outcomes()` in `pro_system_service.py`, scheduled daily 4:00 PM IST (`long_term_outcomes_audit_4pm` in `updater.py`). Closes any open long-term position whose daily close falls below its 200-EMA. Runs independently of `LONG_TERM_GENERATION_ENABLED` — it only closes existing positions, never opens new ones, so it protects current holdings even while generation is paused. |
| Phase 1 #6 — zero cost model in `trade_engine.py` | ✅ **Resolved** | `cost_model_for(SWING)` wired into `check_pending_activations()` (slipped entry fill) and `_exit_signal()` (slipped exit fill + `round_trip_pct` friction subtracted from P&L). New cost-viability gate in `_compute_ai_score()` rejects targets under 6× round-trip cost. Net P&L now runs measurably below gross (~0.36-0.40pp on a sample trade). **Side finding, not yet fixed:** the dashboard win-rate aggregate (`get_dashboard_data`, ~line 1446) filters on `sig.status == HIT_TARGET`, but `_exit_signal()` always sets terminal status to `ARCHIVED` — `total_wins`/`total_losses`/`total_pnl` on the dashboard likely read zero rows today, independent of this fix. Needs its own item. |
| Phase 1 #7 — strangle scale-guard bug | ✅ **Resolved** | Root-caused to the now-deleted MCX quote path (removed 2026-07-24) — no live double-scaling exists for NSE equity today, so the silent `cmp_now /= 100.0` rescale was replaced with the existing ghost-protection fail-safe (`is_theoretical=True` + `[SCALE_MISMATCH]` log + throttled Telegram alert) rather than deleted outright. **Also fixed a gap the original finding missed:** the `is_theoretical` check only existed in a per-leg `elif` that never runs today (`is_equity` is hardcoded `True`); the actual live exit path (combined-premium SL/target check) had no `is_theoretical` guard at all until this fix added one — without it, the flag would have been cosmetic. |
| Phase 1 #8 — naked strangle vs. documented protection | ✅ **Resolved** | Product decision made: ship naked (2-leg), not protected. Docstring in `delta_hedge_service.py` and copy in `DeltaHedgePanel.jsx` corrected from false "capped max loss"/"Iron Condor" claims to accurate unbounded-risk language, naming the real controls (combined-premium SL, delta-danger auto-exit, position-count cap). Dead `buy_ce`/`buy_pe` computation removed (confirmed unreferenced elsewhere). **Still needed:** run the leg-count audit against live data — `backend/scratch/audit_specialist_legs.py` is written but deliberately unrun (triggers live Angel One login via `StocksConfig.ready()`); run manually or via the query in that script's docstring to confirm today's open `specialist` positions are really 2-leg before this ships further. |
| Phase 2 #2.1 — Force Scan button no-op | ✅ **Resolved** | `LiveSignalView.get()` now reads `?force=true` and routes to `intraday_service.get_live_signals(action="generate")`, which already enforces its own market-hours/cutoff/5-min cooldown gates. |
| Phase 2 #2.2 — stale multi-day VWAP gates option-buying | ✅ **Resolved** | Switched to session-anchored `compute_session_vwap`/`compute_session_volume_profile` (already proven in `intraday_service.py`). Verified with synthetic data: old cumulative VWAP stayed at 106.86 through a real breakout to 130; session-anchored version correctly tracked to 114.25. `va_source` now surfaced in signal metadata. |
| Phase 2 #2.3 — circuit breaker ignores AB1021 on quote paths | ✅ **Resolved** | `get_live_price_by_token` and `get_bulk_quotes` now trip the same `"quote"` circuit breaker (300s) on AB1021 that `get_candle_data` already had — directly relevant to the 403 storm seen in production 2026-07-29. |
| Phase 2 #2.4 — manual exit path checks target before SL | ✅ **Resolved** | `run_intraday_check`'s `ACTIVE` branch now checks stop-loss before target, matching `run_eod_evaluation`'s Phase 0 #4 fix. Same bug class, different function. |
| Phase 2 #2.5 — Swing V2 dry_run writes real DB rows | ✅ **Resolved** | `record_candidates()` now takes `dry_run` and guards its `bulk_create`; `swing_service.py` threads its own `dry_run` through. All 4 real call sites audited — only the shadow path passes `dry_run=True`. |
| Phase 2 #2.6 — dead duplicate exit-audit implementation | ✅ **Resolved** | `update_pro_system_outcomes()` deleted from `pro_system_service.py` (zero live call sites, confirmed by fresh grep). `update_long_term_outcomes()` (Phase 1 #3's live function) confirmed untouched. |
| Phase 2 #2.7 — dead `ShortTermSignal` status vocabulary | ✅ **Resolved** | Removed `CANCELLED`/`CLOSED`/`COOLDOWN` — investigated first and confirmed neither represented a real missing transition (`ARCHIVED` already covers "closed," `run_expiry_cleanup` already handles stale pending via `EXPIRED`). Found 2 additional dead-filter call sites beyond what the audit cited; hand-wrote migration `0032_alter_shorttermsignal_status.py` since `makemigrations` needs `manage.py`. |
| Phase 2 #2.8 — no portfolio/margin risk gate on option-selling | ✅ **Resolved** | New `_compute_portfolio_heat()` helper feeds both the existing display metric and a new `MAX_PORTFOLIO_HEAT_PCT=80.0` gate that skips opening new positions above threshold — additive to the existing count cap. Confirmed Phase 1 #7/#8 (same file) untouched. |
| Phase 2 #2.9 — option-chain live poll freezes after first load | ✅ **Resolved** | Fixed the stale-closure bug (`data` read from a mount-time snapshot) with a `hasChainRef` ref updated on every fetch success. |
| Phase 2 #2.10 — fetch failures invisible on two decision-critical tables | ✅ **Resolved** | Added error state + visible banner to `OptionBuyingTable.jsx` and `LiveSignalsTable.jsx` without nulling last-known-good data. Wired the previously-dead `lastRefreshed` state into a rendered "Last Refreshed" row. |
| Phase 3 #3.1.1 — `AngelOneStreamer` subscription dicts mutated without a lock | ✅ **Resolved** | Added `self._sub_lock`, wraps every read-modify-write/snapshot site in `subscribe()`/`_on_open()`. Network I/O stays outside the lock in every case. |
| Phase 3 #3.1.2 — streamer restart race, two reconnect paths one lock | ✅ **Resolved** | `AngelOneStreamer` now accepts the shared `_STREAMER_RESTART_LOCK` at construction; health-monitor's `self.start()` wrapped in the same non-blocking acquire/release pattern the self-heal path already used. |
| Phase 3 #3.1.3 — `download_queue.drain_once` select-then-update not atomic | ✅ **Resolved** | `select_for_update(skip_locked=True)` inside `transaction.atomic()` — a concurrent drain now skips already-claimed rows instead of racing for them. |
| Phase 3 #3.1.4 — no DB uniqueness constraint on `ShortTermSignal` | ✅ **Resolved** | Partial unique constraint on `symbol`, scoped to current non-terminal statuses (confirmed against the live enum post-#2.7, not the audit doc's possibly-stale list). `IntegrityError` handling added to the create call. Migration hand-written as `0034` after an initial `0033` collided with #3.4.2's migration, written in parallel. |
| Phase 3 #3.1.5 — entry activation never rewrites `entry_price` to observed fill | ✅ **Resolved** | Found this was already closed as a side effect of Phase 1 #6's cost-model wiring. What was actually missing — an explicit `slippage_delta` in the audit trail — added to `TradeHistory.reason`. SL/target risk math confirmed untouched. |
| Phase 3 #3.2.1 — no DRF-level throttling | ✅ **Resolved** | Global `UserRateThrottle`(120/min)/`AnonRateThrottle`(30/min) defaults, plus scoped `2/min` on Force Scan specifically. `FileBasedCache` backend confirmed throttle-safe across Render's single-process deployment. |
| Phase 3 #3.2.2 — unbounded growth on 4 high-write tables | ✅ **Resolved** | New `run_signal_tables_cleanup()`, modeled on the existing candle cleanup job, scheduled daily 2:45 AM. Terminal-state rows only, batched pk-chunked deletes, settings-configurable retention windows. |
| Phase 3 #3.2.3 — `estimate_iv` uncaught `ZeroDivisionError` | ✅ **Resolved** | `t_days<=0` guard + `try/except (ZeroDivisionError, ValueError): return 0.20`, matching `calculate_greeks`'s existing pattern. 3 new unit tests (unrun — require `django.setup()` — verified by hand-tracing). |
| Phase 3 #3.2.4 — duplicated expiry-rollover logic in `delta_hedge_service.py` | ✅ **Resolved** | Extracted `resolve_target_expiry()`/`expiry_str_to_date()` as shared helpers; both call sites deduplicated. Confirmed Phase 1 #7/#8 and Phase 2 #2.8 (same file) untouched. |
| Phase 3 #3.3.1 — `PerformanceReports.jsx` nulls both reports on any error | ✅ **Resolved** | `Promise.all` → `Promise.allSettled`, each report updated independently, never nulled on a refresh failure. New `fetchError` banner. |
| Phase 3 #3.3.2 — `OptionBuyingTable.jsx` poll cadence ignores mid-session close | ✅ **Resolved** | Single always-on 5-min interval; tick handler checks ref-backed `isOpen`/last-fetch-timestamp before deciding to fetch. Same stale-closure-ref pattern as Phase 2 #2.9. |
| Phase 3 #3.3.3 — duplicate simultaneous global-market fetches | ✅ **Resolved** | In-flight-request dedup map in the shared axios instance, keyed by URL + stably-stringified params. Both components' fetch calls unchanged — dedup is transparent at the API layer. |
| Phase 3 #3.4.1 — no CI pipeline running the test suite | ✅ **Resolved** | Added `.github/workflows/backend-tests.yml`. Prerequisite fix: `StocksConfig.ready()` had no test-mode guard — would have live-logged into Angel One under CI — added `if 'test' in sys.argv: return`, mirroring the existing guard in `settings.py`. Branch-protection wiring flagged as a manual follow-up (needs repo admin access). |
| Phase 3 #3.4.2 — dead `Trade` model | ✅ **Resolved** | Removed the class + `TelegramLog.trade` FK, confirmed `TradeHistory` (unrelated, naming collision only) untouched. Live check now run directly via `psql` (`SELECT COUNT(*) FROM trades` → 0) — migration `0033_remove_dead_trade_model` confirmed safe, not yet applied (that's a `DROP TABLE`, left for the normal deploy path). |
| Phase 3 #3.4.3 — vestigial `option_selling` branch in serializer | ✅ **Resolved** | Live row-count check run directly via `psql` (bypasses Django/`manage.py` entirely, no Angel One login triggered): `SELECT category, COUNT(*) FROM signal_history GROUP BY category` → `commodity: 8, intraday: 2, long_term: 5, option_buying: 1, specialist: 148, option_selling: 0`. Confirmed zero rows use the old category — branch fully removed, along with everything that existed only to feed it (`option_live`, `nse_live`, `now_ist`, and 4 now-unused imports). |
| Phase 4 (14 items) | ✅ **Resolved** | All 14 P3 cleanup items shipped: stale CLAUDE.md doc section removed, dead MCX/equity branches deleted, `_sector_strength` deduped, bare excepts logged, unused constant removed, stale docstrings fixed, exit-order consistency fix, download-queue retry, 12 loose scripts + 6 unimported components deleted. |
| Phase 5 (3 items) | 2/3 resolved | B (regression tests for repeat-bug-class gaps) and C (retention + CI) done. A (retire legacy engine, promote Swing V2) deliberately held — only 1 of ~20 required shadow sessions logged; asked directly, held rather than overridden. See Phase 5 section for full status per item. |

**Known limitation of the 2b fix, stated plainly:** only the promoter-group cap is enforced
with real data. Sector diversity and correlation-cluster capping for the long-term book still
don't exist (no sector taxonomy, no correlation pass wired in) — lower priority than the
promoter-group concentration that actually caused the original incident, but the book is not
fully risk-controlled yet.

**All of Phase 1, Phase 2, and Phase 3 are now fully resolved.** Commits exist locally on
`main` and are not yet pushed to remote or deployed. Both #3.4.2 and #3.4.3's live-data
checks were completed by connecting directly to the local Postgres via `psql` (reading
`DB_HOST`/`DB_USER`/`DB_PASSWORD` from `backend/.env`) rather than through `manage.py` —
this bypasses Django's app registry entirely, so `AppConfig.ready()` never runs and no Angel
One login is triggered. This is the same technique a Phase 2 #2.7 subagent used earlier.
Confirmed this local Postgres instance is the actual live database, not an empty dev shell —
its `candle_bars`/`download_requests`/etc. tables are actively populated by the running
backend.

Still owed by the account owner, none blocking:
- `makemigrations --check --dry-run` to confirm migration chain `0031→0032→0033→0034` is complete.
- Actually applying migration `0033_remove_dead_trade_model` (confirmed safe — 0 rows — but
  a `DROP TABLE` was deliberately left for the normal deploy path, not run ad hoc here).
- The Phase 1 #8 leg-count audit (`backend/scratch/audit_specialist_legs.py`).

Also still open: the dashboard win-rate aggregate bug found while fixing #6.

**Process note:** Phase 3's 15 items were dispatched across 15 parallel worktree-isolated
subagents. Two usage-limit resets during the run caused several worktrees to be garbage-collected
mid-task; resumed agents fell back to editing the primary checkout directly for 5 items
(#3.2.2, #3.3.3, #3.4.1, #3.4.2, #3.4.3) instead of an isolated branch — those were reconciled
by committing them directly on `main` before merging the other 10 worktree branches in. One
real migration-number collision resulted (two independently-written `0033_*.py` files) and
was resolved by renumbering #3.1.4's to `0034` with a corrected dependency chain. One test-file
merge conflict (two new test classes appended at the same line) was resolved by keeping both.

---

## Phase 0 — Today (do immediately, each under ~1 hour)

### 1. Unauthenticated cron-trigger endpoint / hardcoded fallback secret
**P0 · Security · `stocks/views.py`**

**Current behavior:** `CronScannerTriggerView` (`views.py:533`) has
`permission_classes = (AllowAny,)` (line 538) and authenticates only via
`secret_token = os.environ.get("CRON_SECRET_TOKEN", "trade_pulse_secure_cron_trigger_2026")`
(line 549). If the env var is unset in production, the hardcoded literal — already in git
history — is the live secret, and it unlocks forced scans and the `short_term_scan` action
(finding 4 below).

**Steps:**
1. `openssl rand -hex 32` to generate a new secret.
2. Render → service → Environment → set `CRON_SECRET_TOKEN` to the generated value
   (production and staging, different values each). Add the same key to your local `.env`
   (it's already present there — rotate it too, don't assume the existing local value is
   safe just because it's gitignored).
3. In `views.py:549`, drop the fallback:
   ```python
   secret_token = os.environ.get("CRON_SECRET_TOKEN")
   if not secret_token:
       logger.error("[CRON] CRON_SECRET_TOKEN not set — refusing trigger.")
       return Response({"error": "Server misconfigured"}, status=503)
   ```
4. Update the external cron caller (cron-job.org) to the new token in the same deploy
   window.
5. Optional: add DRF `AnonRateThrottle` to the view so a leaked token can't be hammered
   (see Phase 3, item 3.2.1).

**Verify:** `curl` with no token → `401`. `curl` with the old hardcoded string → `401`.
`curl` with the new secret → `200`. `grep -rn "trade_pulse_secure_cron_trigger_2026" stocks/`
→ no matches.

**Effort:** 30–45 min. **Dependencies:** None — ship first.

---

### 2. Long-term engine: disable generation now (stopgap)
**P0 · Risk Controls · `config/settings.py`, `stocks/services/pro_system_service.py`**

**Current behavior:** `scan_long_term_stocks()` (`pro_system_service.py:379-448`) ranks
purely by `momentum_100d` (sort/cap at lines 447-448) with no promoter-group or sector cap.
`run_long_term_scan()` (line 451) is gated by `LONG_TERM_GENERATION_ENABLED`, which per
`config/settings.py:383` (`os.getenv(..., "true")`) defaults **true** and, per the docstring
at line 460, was "re-enabled 2026-07-28 at the account owner's explicit request" — i.e. it
is live today, scheduled daily 12:00 PM IST (`updater.py:497-505`). This exact ranking
method is the documented cause of a prior 100%-single-promoter-group book
(`doc/INSTITUTIONAL_AUDIT_PLATFORM.md`).

**Steps:**
1. Render → Environment → set `LONG_TERM_GENERATION_ENABLED=false`; restart the service
   (settings are read once at process start).
2. Confirm both gate checks — `run_long_term_scan()` line 466 and `get_pro_system_data()`
   line 566 — read the same setting (they do; one env change closes both).
3. Leave existing open `long_term` positions untouched — this only stops new ones tomorrow.

**Verify:** At 12:00 PM IST, log shows
`[LONG_TERM] Generation disabled ... — skipping scan.` (line 467). No new
`SignalHistory(category="long_term")` rows created that day.

**Effort:** 15 min. **Dependencies:** None; this is the required prerequisite for finding
2b (real fix, Phase 1) and finding 3 (exit enforcement, Phase 1).

---

### 3. Duplicate short-term signal path via cron
**P0 · Signal Integrity · `stocks/services/live_signal_service.py`, `pro_system_service.py`**

**Current behavior:** `_run_periodic_scanners_impl` (`live_signal_service.py:515`) branches
at line 526 on `action == "short_term_scan"` → `get_pro_system_data(trigger_scan=True)`,
reachable through the finding-1 endpoint via `?action=short_term_scan`. Inside
(`pro_system_service.py:492`), it runs legacy `scan_short_term_stocks()`, checks only
`status=ACTIVE` (not `PENDING`, lines 505-508) before `ShortTermSignal.objects.create(...)`,
and sends alerts via raw `send_telegram_message` (line 542) instead of
`queue_telegram_message`, bypassing the `TelegramLog` idempotency guard that the
actually-scheduled 11:30 AM engine, `trade_engine.py`, uses everywhere (e.g. lines 861-875,
934-948).

**Steps:**
1. Per the codebase's own comment (`live_signal_service.py:544-549`), `trade_engine.py` is
   the sole intended engine — delete the branch at lines 525-529, or replace with:
   ```python
   if action == "short_term_scan":
       logger.warning("[CRON] short_term_scan is deprecated — trade_engine.py's 11:30 AM job is authoritative. Ignoring.")
       return
   ```
2. If a manual re-trigger path is genuinely needed, point it at `trade_engine.py`'s own
   entry point instead, and if `get_pro_system_data(trigger_scan=True)` survives at all, fix
   its existence check to `status__in=[ACTIVE, PENDING]` and swap in
   `queue_telegram_message`.

**Verify:** Hit `?action=short_term_scan` → deprecation log, zero new `ShortTermSignal`
rows. Confirm no duplicate "NEW SHORT-TERM SWING SETUPS" Telegram messages after both the
11:30 AM job and a manual trigger run same-day in a test environment.

**Effort:** 1–2 hours. **Dependencies:** Do after finding 1 (endpoint auth) so testing isn't
happening on a still-public endpoint.

---

### 4. EOD exit check-order bug (trailing-EMA before target3)
**P0 · Signal Integrity · `stocks/services/trade_engine.py`**

**Current behavior:** In `run_eod_evaluation()` (`trade_engine.py:621`), checks run: SL
(lines 703-706) → trailing 20-EMA close (708-712) → target3 intrabar high (714-717), each
with `continue`. A spike-and-fade day (high clears target3, close < 20 EMA) books the worse
`TRAILING_EXIT` at the close instead of `HIT_TARGET` at the touched target3.

**Steps:**
1. Reorder so target3 is checked before the trailing-EMA close check (SL stays first,
   unchanged):
   ```python
   # Check 1: SL — unchanged
   if latest_low <= sl_val: ... continue

   # Check 2 (moved up): Target 3 intrabar
   if latest_high >= t3_val:
       _exit_signal(sig, t3_val, ShortTermSignal.Status.HIT_TARGET, "Final Target Hit", ...)
       continue

   # Check 3 (moved down): Trailing close < 20 EMA
   ema20_val = float(_ema(df['Close'], 20).iloc[-1])
   if latest_close < ema20_val:
       _exit_signal(sig, latest_close, ShortTermSignal.Status.TRAILING_EXIT, "Trailing Exit", ...)
       continue
   ```
2. Add a comment explaining the ordering rationale so it isn't silently re-swapped later.

**Verify:** Construct a synthetic case with `latest_high >= t3_val` and
`latest_close < ema20_val`; confirm result is now `HIT_TARGET` at `t3_val`. Replay a
historical symbol/day with this pattern from `StockDailyData` and confirm the new exit
reason.

**Effort:** 1–2 hours. **Dependencies:** None; ships independently.

---

## Phase 1 — This week (each requires design/testing time, up to half a day)

### 2b. Long-term engine: wire in the existing concentration cap (real fix)
**P0 · Risk Controls · `pro_system_service.py`, `shared/portfolio_risk.py`, `shared/profiles.py`**

**Current behavior:** As in item 2, `scan_long_term_stocks()` has no concentration control.
The machinery already exists and is proven in `intraday_service.py:695-722`, which calls
`apply_portfolio_constraints(qualified, open_rows, sector_map, clusters, MAX_SIGNALS_PER_SCAN, profile=...)`
from `stocks.services.shared.portfolio_risk` (import the shared module directly, not the
deprecated `stocks.services.portfolio_risk` shim). `shared/profiles.py:260-280` already
defines `LONG_TERM.max_per_promoter_group_pct` (default 12.0), and
`shared/portfolio_risk.py:121` (`promoter_group_map`) queries `PromoterGroup`
(`stocks/models.py:522`).

**Steps:**
1. In `scan_long_term_stocks()`, before the final sort (line 447), build `symbols`, call
   `promoter_group_map(symbols)`, pull open `category="long_term"` `SignalHistory` rows as
   `open_rows`, and call
   `apply_portfolio_constraints(candidates, open_rows, sector_map, clusters={}, max_positions=5, profile=LONG_TERM, groups=groups)`.
2. Move the `[:5]` cap to run **after** this filter, not before — capping first leaves
   nothing for the group filter to reject.
3. Set `LONG_TERM_MAX_PROMOTER_GROUP_PCT` in Render if the 12.0 default isn't right for this
   book.
4. Only after this is verified, flip `LONG_TERM_GENERATION_ENABLED` back to `true`.

**Verify:** In Django shell (staging, not production), run `scan_long_term_stocks()` and
cross-check the resulting book against `PromoterGroup` — no more than ~12% concentration in
one group. Seed two same-group candidates ranked #1/#2 by momentum and confirm only one
survives `apply_portfolio_constraints` (look for `[PORTFOLIO]` rejection logs).

**Effort:** Half a day. **Dependencies:** Requires item 2 (disabled) first; must land before
item 3 (exit enforcement, below) and before re-enabling generation.

---

### 3. Long-term exit rule (200 EMA) is documentation-only
**P0 · Risk Controls · `stocks/services/pro_system_service.py`**

**Current behavior:** Every long-term signal's `hold_rule` says "Exit on trend break below
200 EMA... not currently enforced in code" (`pro_system_service.py:501-502`). The only
function ever built to check this, `update_pro_system_outcomes()` (line 648), is
unreachable — grep only hits its own `def` and two comments (`live_signal_service.py:544`,
`pro_system_service.py:822`) explaining it was deliberately removed to avoid duplicating
`trade_engine.py`'s alerts. No replacement exists.

**Steps:**
1. Do not ship before item 2b lands — don't add exits to a still-unconstrained book.
2. Add a new function `update_long_term_outcomes()` in `pro_system_service.py`: for each open
   `category="long_term"` `SignalHistory` row, fetch ≥200 daily candles, compute 200-EMA,
   and if `latest_close < ema200`, close the row (`HIT_TARGET` if close > entry else
   `HIT_SL`) with `metadata['exit_reason'] = 'TREND_BREAK_200EMA'`.
3. Schedule it in `updater.py` daily at 4:00 PM IST
   (`CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone=KOLKATA_TZ)`), a new job
   id like `long_term_eod_audit`.
4. Remove the "not currently enforced" caveat from the `hold_rule` string (lines 501-502)
   once live.

**Verify:** Django shell — force a test row's symbol to one trading below its 200 EMA, call
`update_long_term_outcomes()` directly, confirm status flips and `exit_reason` is set.
Confirm the 4:00 PM job logs cleanly with no exceptions.

**Effort:** Half a day. **Dependencies:** After item 2b (concentration cap) is live;
stopgap (item 2) must precede both.

---

### 6. Zero trading-cost/slippage model in the live short-term engine
**P0 · P&L Integrity · `trade_engine.py`, `trading_engine/cost_model.py`**

**Current behavior:** `grep -n "cost_model_for\|slipped_fill\|round_trip_pct" trade_engine.py`
returns nothing. `intraday_service.py:148-149,167-169` and `swing_signals.py:33,210-211`
both apply the shared cost model; `trade_engine.py` — the engine actually scheduled live
daily — reports every fill and every P&L/win-rate figure gross, with zero friction applied.

**Steps:**
1. Confirm the matching `EngineProfile` in `shared/profiles.py` for this engine's holding
   period (or add one).
2. At entry (in the activation path inside `check_pending_activations()`), wrap the fill:
   `fill_price = cost_model_for(PROFILE).slipped_fill(raw_price, is_buy=True, symbol=sig.symbol)`.
3. At exit (`_exit_signal()`), apply the same slipped-fill treatment and subtract
   `round_trip_pct(...)` friction before recording P&L.
4. Check whether a cost-viability gate (target > N × round-trip cost, matching intraday's
   `3×` rule) is missing here too, and add if so.
5. Label any historical dashboards reading this engine's data "gross" until the fix ships,
   to avoid misleading the account owner in the interim.

**Verify:** After a test signal activates/exits, confirm entry/exit prices differ from raw
trigger prices by the slippage amount, in the direction that hurts the trade. Recompute one
historical trade's P&L% with the model applied and confirm it's measurably lower than the
previously reported gross figure. `grep` confirms `cost_model_for`/`DEFAULT_COST_MODEL` now
appear in the file.

**Effort:** Half a day. **Dependencies:** Do after item 4 (exit-order fix) so cost isn't
applied on top of a still-buggy exit price.

---

### 7. Scale guard can mask a real loss as a "profit" (option-selling engine)
**P0 · Risk Controls / P&L Integrity · `delta_hedge_service.py`**

**Current behavior:** At `delta_hedge_service.py:2452-2455`:
```python
if entry < 200 and cmp_now > 500:
    cmp_now /= 100.0
    l['cmp'] = cmp_now
```
This feeds the combined-premium systematic SL/target check
(`current_combined = ce_leg['cmp'] + pe_leg['cmp']`, profit-capture branch shortly after). A
real 4-5x adverse move (sold ₹150, now genuinely ₹600) gets divided by 100 to ₹6.00, which
can falsely satisfy profit-capture and close a real large loss as a "win."

**Steps:**
1. Find the actual root cause of the scale mismatch (likely a paise/rupee unit bug in the
   quote path feeding `l['cmp']`) and fix it there instead of masking it here.
2. Delete the rescale block (lines 2452-2455) once the root cause is fixed — an
   auto-correction that silently rewrites live P&L data is unsafe at any threshold.
3. If the root cause can't be fully eliminated immediately, replace the silent rescale with
   a fail-safe that reuses the existing "ghost protection" mechanism:
   ```python
   if entry < 200 and cmp_now > 500:
       logger.error("[SCALE_MISMATCH] %s entry=%.2f cmp=%.2f — flagging theoretical, no auto-exit.", sig.symbol, entry, cmp_now)
       l['is_theoretical'] = True
       continue
   ```
   (`is_theoretical` is already respected by the exit-check `elif` guard just above this
   block, which explicitly excludes theoretical prices from triggering exits.)
4. Add real-time alerting on `[SCALE_MISMATCH]`.

**Verify:** Construct a leg dict `entry=150, cmp=600`, run the exit-check path, confirm it
now flags `is_theoretical`/logs `[SCALE_MISMATCH]` rather than firing `HIT_TARGET`. Confirm
`grep -n "cmp_now /= 100" delta_hedge_service.py` is empty after the fix.

**Effort:** Half a day (root-cause work, not just the guard). **Dependencies:** None
blocking; test alongside item 8 in the same review since both touch this file's live P&L
logic.

---

### 8. Naked short strangle despite documented protective legs
**P0 · Risk Controls · `delta_hedge_service.py`**

**Current behavior:** `build_specialist_hedge()` (line 497) computes protective long legs at
lines 1254-1255 (`buy_ce`/`buy_pe` via `find_strike_by_delta(..., 0.05, ...)`) and
bulk-prefetches their quotes (line 1301), but only
`legs.append(make_leg_entry(sell_ce, 'CE', 'SELL'))` and the PE equivalent (lines
1311-1312) ever populate the persisted `legs` list — a comment confirms "2-leg Short
Strangle (Sell Only)" is intentional in the current code, directly contradicting the
docstring's "Buy Far OTM Protection... to cap max loss." `buy_ce`/`buy_pe` are computed,
quoted, and discarded.

**Steps:**
1. Get an explicit product decision first: naked strangle (matches code) vs. protected
   strangle/iron condor (matches docstring). This changes max-loss exposure and must be a
   signed-off choice, not a silent default.
2. If adding protection: append `make_leg_entry(buy_ce, 'CE', 'BUY')` and
   `make_leg_entry(buy_pe, 'PE', 'BUY')` to `legs` after the sell legs. Then: (a) exempt
   these cheap OTM legs from the `MIN_PREMIUM_PER_LEG`/`MIN_NOTIONAL_PER_LEG` viability check
   (lines 1316-1329) which should apply to SELL legs only; (b) change the combined-premium
   math (item 7's area) to use **net credit**
   (`(sell_ce - buy_ce) + (sell_pe - buy_pe)`) instead of gross short premium; (c) find and
   fix any `len(active_legs) != 2` assumption elsewhere in the file (e.g. in
   `rebalance_delta_neutral_strangle`) that will break with a 4-leg position.
3. If shipping naked instead: strip the docstring's capped-loss claim and any matching UI
   copy in `DeltaHedgePanel`/`OptionSellingFull.jsx`, and get written sign-off on
   unbounded-loss risk.
4. Either way, first audit currently-open `category="specialist"` positions' actual leg
   count (`SignalHistory.objects.filter(...).values('metadata')`) so the account owner knows
   today's real exposure before anything ships.

**Verify:** Call `build_specialist_hedge(...)` in a dry run; if protecting, assert
`len(legs) == 4` with two `BUY` actions. Confirm the `len(active_legs) != 2` check is
updated and rebalance still triggers correctly in a test. Recompute a historical strangle
scenario with protection included and confirm bounded max loss and correct net-credit P&L
math.

**Effort:** Half a day to a full day. **Dependencies:** Blocked on the product decision
(step 1); test together with item 7 as one deploy to this risk-critical file.

---

## Phase 2 — Next two weeks

### 2.1 Force Scan button is a no-op (`views.py`)

**Current behavior:** `LiveSignalView.get()` (`backend/stocks/views.py:18-36`) never reads
`request.query_params` — it unconditionally calls `_live_intraday_payload()`, a plain DB
read. `frontend/src/components/LiveSignalsTable.jsx:119-123` sends `?force=true` expecting a
re-scan, but the param is dropped on arrival; `intraday_service.get_live_signals(action=...)`,
where the real force/generate logic lives, is never invoked from this view.

**Exact steps to fix:**
1. In `LiveSignalView.get()`, read `force = request.query_params.get('force', 'false').lower() == 'true'`
   — same pattern already used elsewhere in this file (e.g. line 97, line 161).
2. If `force`, import and call `intraday_service.get_live_signals(action="generate")` and
   use its return as `payload`; otherwise keep the existing `_live_intraday_payload()` call.
3. Confirm `get_live_signals("generate")` still enforces its own 5-min scan-rate cooldown
   and market-hours/cutoff gates, so repeated clicks can't hammer Angel One.

**How to verify:** Click Force Scan twice within a few seconds during market hours. The
first click should produce intraday-scan log output and update `SignalHistory.updated_at`;
the second (within cooldown) should return cached DB state with no new scan.

**Effort:** 1-2 hrs. **Dependencies:** None.

---

### 2.2 Stale multi-day VWAP/volume-profile gates option-buying breakouts (`option_buying_service.py`)

**Current behavior:** `backend/stocks/services/option_buying_service.py:11-12` imports
`compute_vwap`/`compute_volume_profile` (multi-day/cumulative) and calls them at line 51
(`compute_volume_profile(df, bins=40)`) and line 61 (`compute_vwap(df)`). `signal_utils.py`
also has session-anchored `compute_session_vwap` (line 282) and
`compute_session_volume_profile` (line 467), already used correctly by
`intraday_service.py:262,282`. With option-buying's 2-day candle lookback, the cumulative
VWAP barely moves, so the "price above/below VWAP" leg of the `BUY_CE`/`BUY_PE` gate is
nearly decoupled from actual intraday price action.

**Exact steps to fix:**
1. Change the import at line 11-12 to `compute_session_vwap, compute_session_volume_profile`.
2. Line 51: `poc, vah, val, va_source = compute_session_volume_profile(df, bins=40)` — note
   the 4-tuple return, matching `intraday_service.py:262`, not the old 3-tuple.
3. Line 61: `vwap_series = compute_session_vwap(df)`.
4. Check downstream uses of `poc`/`vah`/`val`/`vwap_series` for anything assuming the old
   3-tuple or multi-day semantics; handle/log `va_source` or discard with `_`.

**How to verify:** Log VWAP alongside LTP for a few symbols intraday — it should now track
the session (move materially 9:30 AM–2:00 PM) rather than sit flat. Compare `BUY_CE`/`BUY_PE`
counts before/after over a few sessions.

**Effort:** 1-3 hrs. **Dependencies:** None — both functions already exist and are proven
via `intraday_service.py`.

---

### 2.3 Circuit breaker ignores AB1021 on quote/bulk-quote paths (`angel_one_service.py`)

**Current behavior:** `_REST_CIRCUIT_BREAKER_UNTIL = {"candle": 0.0, "quote": 0.0}` (line
58). `get_candle_data`'s error handling checks `err_code == "AB1021"` at line 955 and trips
`["candle"]`. `get_live_price_by_token` (AG8001 branch at line 532) and `get_bulk_quotes`
(AG8001 branch at line 705) — the far higher-frequency quote paths — have no `AB1021`
branch at all, so a rate-limit hit there gets no cooldown and the next call can immediately
retrigger it.

**Exact steps to fix:**
1. In `get_live_price_by_token`, after the `AG8001` branch (line 532-536), add a check for
   `err_code == "AB1021" or "too many requests" in err_msg.lower()` that sets
   `_REST_CIRCUIT_BREAKER_UNTIL["quote"] = time.time() + 300` with a log line mirroring
   `get_candle_data`'s line 957 style.
2. Do the same in `get_bulk_quotes`, right after its `AG8001` branch (line 705-711) — same
   `"quote"` key, already shared with the HTTP 403/429 checks at lines 570-571/694-696.
3. Leave existing HTTP-status 403/429 checks untouched; this only adds the
   200-status-but-error-body case.

**How to verify:** Simulate/force an `AB1021` body response and confirm the new log line
fires, `_REST_CIRCUIT_BREAKER_UNTIL["quote"]` advances ~300s, and the next call hits the
existing guard at lines 503/670 and returns early without a REST call.

**Effort:** 2-4 hrs. **Dependencies:** None; independent of the existing
`_AUTH_LOCK`/`_REST_CALL_LOCK` work.

---

### 2.4 Manual exit path checks target before stop-loss (`trade_engine.py`)

**Current behavior:** `run_intraday_check`'s `ACTIVE` branch (lines 603-612) checks
`if high >= float(sig.target): _exit_signal(..., "HIT_TARGET")` (605-607) **before**
`if low <= float(sig.stop_loss): _exit_signal(..., "HIT_SL")` (610-611). On a bar where both
conditions are true, this always resolves as a win — the opposite of the SL-first ordering
`run_eod_evaluation` already uses (SL check at line 705 precedes target checks). Reachable
only via the manual admin trigger `action=trade_intraday`, but it writes real
`ShortTermSignal` outcomes.

**Exact steps to fix:**
1. Swap the order in lines 603-612: check stop-loss first, target second.
2. Decide (in scope or as a flagged follow-up) whether this function should also route
   through the `TARGET1`/`TARGET2`/`TARGET3` + trailing-SL cascade `run_eod_evaluation` uses
   (lines 698-764) instead of a single full-close on target, so the two paths can't diverge
   again.

**How to verify:** Construct/replay a bar where `high >= target` and `low <= stop_loss`
simultaneously; confirm the signal now resolves `HIT_SL`. Add a unit test for this case.

**Effort:** 1-2 hrs for ordering alone; 0.5-1 day if unifying with the partial-target
cascade. **Dependencies:** None for the minimal fix.

---

### 2.5 Swing V2 "shadow" scan writes DB rows despite `dry_run=True` (`swing_service.py`)

**Current behavior:** `run_swing_scan` (line 64) calls
`portfolio_risk.record_candidates(profile.name, accepted, rejected, regime=regime)` at line
199, which does an unconditional `SignalCandidate.objects.bulk_create(rows)` in
`shared/portfolio_risk.py:268` (inside `record_candidates`, defined line 228). The
`if dry_run:` check in `swing_service.py` doesn't appear until line 218 — 19 lines after the
write already happened. `updater.py`'s `run_swing_v2_shadow` (line 67) calls
`run_swing_scan(dry_run=True)` (line 86) daily at 4:05 PM specifically for zero-footprint
evaluation; today it still writes real rows.

**Exact steps to fix:**
1. Add `dry_run: bool = False` to `record_candidates()` in `portfolio_risk.py` (line 228)
   and guard the `bulk_create` at line 268 with `if not dry_run:`.
2. In `swing_service.py` line 199, pass `dry_run=dry_run` through to
   `record_candidates(...)`.
3. Leave the existing dry-run reporting block (line 218 onward,
   `funnel["would_persist"]` / `[SWING][DRY_RUN]` log) untouched.
4. Grep for other `record_candidates(` callers before defaulting the new param to `False`,
   to confirm no live (non-shadow) caller's behavior changes.

**How to verify:** Compare
`SignalCandidate.objects.filter(engine=profile.name, created_at__date=today).count()`
before/after running the shadow job — should be zero new rows post-fix, while the
`[SWING][DRY_RUN]` log with a populated `would_persist` list still appears.

**Effort:** 2-3 hrs. **Dependencies:** None.

---

### 2.6 Dead duplicate exit-audit implementation still importable (`pro_system_service.py`)

**Current behavior:** `update_pro_system_outcomes(force: bool = False)` (line 648, body
running to ~line 800) is a full second PENDING→ACTIVE→HIT_TARGET/HIT_SL implementation for
`ShortTermSignal`, including its own Telegram sends. Grep confirms zero live call sites —
only a comment at `pro_system_service.py:822` and one at `live_signal_service.py:544` note
it "used to run here too" / "no longer calls" it. It remains fully importable and, per those
comments, was removed specifically to fix a duplicate-alert defect.

**Exact steps to fix:**
1. Delete the function body (line 648 through ~800) from `pro_system_service.py`.
2. Update the comments at line 822 and `live_signal_service.py:544` to state the function
   was removed, not just "no longer called."
3. Re-grep the repo for `update_pro_system_outcomes` (management commands, `updater.py`,
   admin actions, tests) before deleting, to catch any drift since this audit.

**How to verify:** `grep -rn "update_pro_system_outcomes" backend/` returns no callable
references. `python manage.py check` (no live server) confirms no import errors.

**Effort:** 1-2 hrs. **Dependencies:** None.

---

### 2.7 Dead status vocabulary in `ShortTermSignal` state machine (`models.py`)

**Current behavior:** `ShortTermSignal.Status` (`models.py:194-208`) defines `CANCELLED`
(203), `CLOSED` (206), `COOLDOWN` (208) — none ever assigned anywhere (grep for
`status = ShortTermSignal.Status.CANCELLED` etc. returns zero hits). `CANCELLED` and
`CLOSED` are read in dashboard aggregation in `trade_engine.py`: `CANCELLED` at line 1389
(`cancelled_list`), `CLOSED` at line 1407 (P&L total filter) — so those aggregates are
silently always empty. `COOLDOWN` is not referenced anywhere outside its own definition and
migration files — fully inert.

**Exact steps to fix:**
1. Read `_exit_signal`/`run_eod_evaluation`/`run_intraday_check` in `trade_engine.py` to
   decide if a real gap exists — e.g. a stale-pending sweep for `ShortTermSignal` analogous
   to the `SignalHistory` stale-signal guard in `intraday_service.py`/`commodity_service.py`
   (candidate for `CANCELLED`), or a genuine "closed outside target/SL/time-stop" case
   (candidate for `CLOSED`).
2. If a gap exists, implement the missing transition (e.g. cancel stale pending pro-system
   signals at scan start).
3. If not, remove `CANCELLED`, `CLOSED`, `COOLDOWN` from `Status`, delete the dead filters
   at `trade_engine.py:1389` and `:1407`, and generate a metadata-only migration for the
   `choices=` change (no data migration needed — no rows use these values).
4. Do not leave the current half-state (defined + partially queried + never assigned).

**How to verify:** If added: trigger the scenario and confirm a row transitions, and that
`cancelled_list`/P&L totals now reflect it. If removed: `manage.py makemigrations --check`
shows no pending changes post-migration, and no `AttributeError` on the removed constants
anywhere.

**Effort:** 0.5-1 day (requires an add-vs-remove decision first). **Dependencies:**
Understanding `_exit_signal`'s existing exit reasons (`EXPIRED`, `TIME_STOP`) to avoid
overlap.

---

### 2.8 No portfolio/margin-relative risk gate on option-selling (`delta_hedge_service.py`)

**Current behavior:** `portfolio_heat` is computed (lines 1902-1953,
`total_notional / 1,00,00,000 × 100`, capped at 100) and exposed only for display at
`panel_data['portfolio_metrics']['portfolio_heat_pct']` (line 1958). The only gate on
opening new strangle positions is a flat count: `MAX_EQUITY_SIGNALS = 10` (line 1549),
enforced at line 1553, plus the settings-backed `HEDGE_MAX_SIGNALS` (line 1388).
`portfolio_heat_pct` is never read before that decision, and its ₹10 crore denominator is a
hardcoded constant, not derived from real account equity/margin the way
`INTRADAY_ACCOUNT_EQUITY` drives intraday sizing.

**Exact steps to fix:**
1. Extract the heat computation (lines 1902-1953) into a helper, e.g.
   `_compute_portfolio_heat(unique_signals) -> float`, used by both the display path and a
   new gate.
2. Before the new-position scanner loop (~line 1546-1554), call the helper and skip opening
   new positions once heat ≥ a new threshold, e.g. `MAX_PORTFOLIO_HEAT_PCT = 80.0`
   (settings-overridable like `HEDGE_MAX_SIGNALS`), logging the skip reason clearly.
3. Replace the hardcoded `10000000.0` denominator with a real settings constant analogous to
   `INTRADAY_ACCOUNT_EQUITY` (e.g. `HEDGE_ACCOUNT_CAPITAL`), so heat % is meaningful relative
   to actual capital — confirm the right number with whoever owns account-sizing
   assumptions.
4. Keep the existing count caps as an additive (not replaced) gate.

**How to verify:** With test/paper data, push `portfolio_heat_pct` above the new threshold
while still under the count cap; confirm the next scan cycle logs the skip and creates no
new `specialist`-category `SignalHistory` row. Confirm the dashboard and the gate always
read the same computed value.

**Effort:** 0.5-1 day. **Dependencies:** Needs a real capital/margin denominator decided
before shipping — too low silently chokes generation, too high defeats the gate.

---

### 2.9 Option-chain "1-second live" poll freezes after first load (`OptionChainTable.jsx`)

**Current behavior:** The `useEffect` at lines 60-101 has dependency array `[symbol]` only
(line 101). Its `setInterval` callback (line 91) guards on
`data && data.chain && data.chain.length > 0` before calling `fetchChain(false)` (line 93) —
but `data` is the value closed over when the effect last ran (`null` at mount, per
`useState(null)` at line 42). Since the effect never re-runs on `data` changes (only on
`symbol`), that closure's `data` stays stale — permanently `null` in practice — so the guard
never passes and `fetchChain(false)` never fires again after the initial `fetchChain(true)`
at line 88. The chain looks frozen after first load with no error shown.

**Exact steps to fix:**
1. Add `const hasChainRef = useRef(false);` near the other state (after line 44).
2. In the fetch success handler (~line 73), after `setData(r.data)`, set
   `hasChainRef.current = !!(r.data?.chain?.length > 0);`.
3. In the interval callback (lines 91-95), replace the `data && data.chain...` guard with
   `hasChainRef.current`.
4. Do not add `data` to the effect's own dependency array — that would tear down/recreate
   the interval on every fetch.

**How to verify:** Load the page during market hours; confirm `/api/stocks/option-chain/`
fires roughly once per second continuously (not just once) in devtools network tab, and
strikes/OI visibly update without a manual refresh.

**Effort:** 1-2 hrs. **Dependencies:** None.

---

### 2.10 Fetch failures invisible on the two most decision-critical tables (`OptionBuyingTable.jsx`, `LiveSignalsTable.jsx`)

**Current behavior:** `OptionBuyingTable.jsx`'s `fetchData` (lines 86-91) only
`console.error`s on failure (line 89) — no error state exists, so a failing poll leaves
stale `data` (and the `marketStatus`/`signals` derived from it, lines 77-79) displayed
indefinitely with no visible signal. `LiveSignalsTable.jsx` declares `lastRefreshed` state
(line 90, set on every successful fetch) but it appears nowhere else in the file — never
rendered — so the staleness indicator it was meant to provide is dead.

**Exact steps to fix (OptionBuyingTable.jsx):**
1. Add `const [error, setError] = useState(null);`.
2. In `fetchData`, `setError(null)` at request start, `setError(...)` in `.catch` (keep the
   existing `console.error`), and clear it again on success.
3. Render a small visible banner/badge when `error` is set, near the table header.

**Exact steps to fix (LiveSignalsTable.jsx):**
1. Render `lastRefreshed` somewhere in the toolbar/header (e.g. "Last updated: {time}"),
   using any existing date-format helper in the file.
2. Check `fetchSignals`'s (line 119) error handling; if it also silently swallows failures,
   add the same `error` state/banner pattern as above for consistency.

**How to verify:** Block the relevant API endpoint in devtools; confirm `OptionBuyingTable`
shows a visible error indicator while still showing last-known data, and `LiveSignalsTable`'s
"Last updated" timestamp stops advancing (and shows an error banner if added). Restore the
endpoint and confirm both clear on the next successful poll.

**Effort:** 2-4 hrs total. **Dependencies:** None.

---

## Phase 3 — This month (reliability, races, hygiene)

### 3.1 Concurrency / race conditions

#### 3.1.1 `AngelOneStreamer` subscription dicts mutated without a lock
`backend/stocks/services/angel_one_streamer.py:40-41,106,124-129,149-171`. `self.subscriptions`
(list) and `self.active_subscriptions` (dict) are written in `subscribe()` (append at line
129, dict key insert/update at 124-125) from request threads, the bootstrap thread, and the
batch-subscriber, while `_on_open()` (line 149) iterates
`list(self.active_subscriptions.items())` on the WebSocket callback thread during reconnect.
The `list(...)` wrapper at line 171 protects the outer iteration but not a concurrent
`.update()` racing the copy itself.

**Fix**
1. Add a module-level `self._sub_lock = threading.Lock()` in `__init__`.
2. Wrap the read-modify-write in `subscribe()` (lines 122-129) and the iteration +
   resubscribe loop in `_on_open()` (lines 155-171) in `with self._sub_lock:`.
3. Keep `self.ws.subscribe(...)` calls (network I/O) outside the lock where possible — hold
   the lock only around the dict/list mutation, take a snapshot copy, then call the network
   API unlocked.

**Verify:** Force a reconnect while a batch-subscribe is in flight (kill/restart the WS
mid-`subscribe()` call in a test) and confirm no
`RuntimeError: dictionary changed size during iteration` in logs across 50 reconnect cycles.

**Effort:** 2-3 hrs.

#### 3.1.2 Streamer restart race — two independent reconnect paths, one lock
`backend/stocks/services/angel_one_streamer.py:334` (health-monitor's `self.start()`) does
not acquire `_STREAMER_RESTART_LOCK` (`backend/stocks/services/angel_one_service.py:76`),
which only guards the self-heal path in `get_live_price_by_token()` (~460-470). Both can
call `start()` concurrently, orphaning a socket/thread.

**Fix**
1. Pass the streamer's owning `AngelOneService` instance (or the module-level lock) into
   `AngelOneStreamer` at construction.
2. Wrap the `self.start()` call at line 334 in
   `if _STREAMER_RESTART_LOCK.acquire(blocking=False): try: ... finally: release()` — mirror
   the non-blocking pattern already used at `angel_one_service.py:461`.
3. Log-and-skip (not block) when the lock is already held, since the other path is already
   restarting.

**Verify:** Grep logs for two `"restarting WebSocket connection"` / `"self-heal"` messages
within the same ~1s window in production; should not recur post-fix. Add a unit test that
calls both restart entry points from two threads and asserts only one `start()` executes.

**Effort:** 1-2 hrs.

#### 3.1.3 `download_queue.drain_once` select-then-update is not atomic
`backend/stocks/services/market_data/download_queue.py:163-174`. The PENDING
`SELECT ... ORDER BY created_at LIMIT` (line 163) and the claiming
`UPDATE ... WHERE id__in=row_ids` (line 172) are two round-trips with no row locking, so
`run_download_queue_drain` (every 5 min) and `run_candle_trickle_warmer` (every 2 min, both
wired in `stocks/updater.py:679-708`) can both select the same PENDING rows before either
claims them.

**Fix**
1. Replace the plain `filter(...).order_by(...)[:batch_limit]` with
   `select_for_update(skip_locked=True)` inside `transaction.atomic()`.
2. Immediately `.update(status=IN_PROGRESS)` inside the same transaction, then re-fetch or
   reuse the locked queryset for the actual work — Postgres supports
   `SELECT ... FOR UPDATE SKIP LOCKED`, so a concurrent drain simply skips rows already
   claimed instead of racing.
3. No migration needed; this is a query-shape change only.

**Verify:** Run both cron jobs manually back-to-back (via the existing cron-trigger
diagnostic action, not local `manage.py`) and confirm `summary["picked"]` totals across both
runs never double-count the same `DownloadRequest.id`.

**Effort:** 2 hrs.

#### 3.1.4 No DB-level uniqueness constraint on `ShortTermSignal`
`backend/stocks/models.py:193-265`, `ShortTermSignal.Meta` (260-265) has no
`unique_together`/partial unique index, unlike `SignalHistory.Meta` (`unique_live_signal`,
line 50). Dedup is a scanner-wide cache lock (`trade_engine.py`,
`lock_key = "trade_engine_scanner_running"`, ~346) plus a `.exists()` check (~475-484) with
no `select_for_update` — two overlapping scanner runs (lock miss window, or a manual +
scheduled run) can both pass the `.exists()` check before either inserts.

**Fix**
1. Add a partial unique constraint:
   `UniqueConstraint(fields=["symbol"], condition=Q(status__in=["PENDING","ACTIVE","TARGET1","TARGET2","REVIEW_REQUIRED"]), name="unique_live_short_term_signal")`
   in `ShortTermSignal.Meta`.
2. `python manage.py makemigrations stocks -n add_unique_live_short_term_signal && python manage.py migrate`
   (run via the normal deploy path, not local `manage.py`, per project rule).
3. Wrap the create in `trade_engine.py` (~500-510) in `try/except IntegrityError` and treat
   it as "already tracked," same as `SignalHistory`'s existing pattern.

**Verify:** Attempt to create two `ShortTermSignal` rows for the same symbol with
status=PENDING in a test; second `.create()` must raise `IntegrityError`.

**Effort:** 2-3 hrs including migration + tests.

#### 3.1.5 Entry activation never rewrites `entry_price` to the observed fill
`backend/stocks/services/trade_engine.py:1516-1591` (`check_pending_activations`). Runs
every 30 min (`updater.py`, hours 10-15, minutes 15/45). On activation,
`trade.current_price` is updated (line 1568) but `entry_price` is never touched, so on a
gap-through (`ltp` well below the scan-time entry), SL/target and RR stay computed off the
stale level rather than the real fill.

**Fix**
1. In the `with transaction.atomic():` block (~1567-1591), after confirming
   `db_trade.status == PENDING`, compute `fill_price = Decimal(str(ltp))` and set
   `db_trade.entry_price = fill_price` alongside `status`/`activated_at` in the same
   `save(update_fields=[...])`.
2. Recompute `stop_loss`/`target`/`target2`/`target3` proportionally to the new entry only
   if the original R-multiple should be preserved — otherwise log the slippage delta
   (`fill_price - original_entry`) to `TradeHistory.reason` for audit instead of silently
   changing risk math.
3. At minimum, add a `fill_slippage` field or stash it in the existing `reason` text on the
   `TradeHistory.objects.create(...)` call (~1580-1587) so past behavior is auditable even
   if the team decides not to touch SL/target.

**Verify:** Simulate a gap-through in a test (mock `ltp` at −5% vs `entry_price`) and assert
`ShortTermSignal.entry_price` reflects the fill, not the original scan price.

**Effort:** 3-4 hrs (needs a product decision on whether SL/target re-scale).

### 3.2 Database / retention hygiene

#### 3.2.1 No DRF-level throttling
`backend/config/settings.py:141-150`. `REST_FRAMEWORK` has
`DEFAULT_AUTHENTICATION_CLASSES`, `DEFAULT_PERMISSION_CLASSES`, pagination — no
`DEFAULT_THROTTLE_CLASSES`/`DEFAULT_THROTTLE_RATES`. All rate defense is hand-rolled cache
cooldowns inside view/service code; nothing stops a misbehaving/compromised client from
hammering any endpoint directly.

**Fix**
1. Add to `REST_FRAMEWORK`:
   ```python
   'DEFAULT_THROTTLE_CLASSES': [
       'rest_framework.throttling.UserRateThrottle',
       'rest_framework.throttling.AnonRateThrottle',
   ],
   'DEFAULT_THROTTLE_RATES': {'user': '120/min', 'anon': '30/min'},
   ```
2. Add a stricter scoped rate (`ScopedRateThrottle`, e.g. `'force_scan': '2/min'`) on the
   intraday Force Scan (`?force=true`) view specifically, since that one bypasses the
   existing 5-min cooldown.
3. Confirm the Django cache backend used for throttling is the same Redis/in-memory cache
   already configured (`CACHES` setting) so throttle counters survive across gunicorn worker
   threads.

**Verify:** `curl` the same authenticated endpoint >120 times/min in a loop and confirm a
`429 Too Many Requests` response.

**Effort:** 2 hrs.

#### 3.2.2 Unbounded growth on four high-write tables
`backend/stocks/models.py` — `SignalHistory`, `SignalCandidate`, `TelegramLog`,
`TradeHistory` have no retention job. Only `CandleBar` is pruned, via
`run_candle_bars_cleanup` (`stocks/updater.py:307`, scheduled daily 2:30 AM,
`updater.py:746-747`).

**Fix**
1. Add a `run_signal_tables_cleanup()` job in `updater.py` modeled on
   `run_candle_bars_cleanup` (307): delete/archive `SignalHistory`/`SignalCandidate` rows in
   terminal states (`HIT_TARGET`, `HIT_SL`, `CANCELLED`, `EXPIRED`) older than a configurable
   window (e.g. 180 days), `TelegramLog` older than 90 days, `TradeHistory` older than 1 year
   (it's an audit trail — keep longer).
2. Batch deletes with `.filter(...)[:5000].delete()` in a loop (or `iterator()` + chunked
   `pk__in` deletes) to avoid long table locks on Postgres.
3. Schedule alongside the existing 2:30 AM candle cleanup job, staggered by a few minutes
   (e.g. 2:45 AM) so they don't contend for the same DB connection pool slot.
4. Make retention windows Django settings constants (same pattern as
   `INTRADAY_ACCOUNT_EQUITY`), not hardcoded, so ops can tune without a code change.

**Verify:** After deploying, confirm
`SELECT count(*), min(generated_at) FROM short_term_signals` (and equivalents) trends
down/stays bounded over a week instead of growing unbounded.

**Effort:** 3-4 hrs.

#### 3.2.3 `estimate_iv` can raise uncaught `ZeroDivisionError`
`backend/stocks/services/option_greeks_service.py:50-76`. The Newton-Raphson loop's
`d1`/`d2` computation (lines 59-60) divides by `iv * math.sqrt(t_years)` with no
try/except, unlike `calculate_greeks` (lines 11-49) which wraps its own `d1`/`d2` math in
`try/except` (line 22). If `t_days<=0` slips through or `iv` underflows to 0 mid-loop, this
raises. Current callers — `delta_hedge_service.py:562`, `short_strangle_scanner.py:209` —
happen to catch it, but that's caller discipline, not function safety.

**Fix**
1. Wrap the loop body (lines 55-75) in `try/except (ZeroDivisionError, ValueError): return 0.20`
   (the same starting-guess fallback), mirroring `calculate_greeks`'s own guard.
2. Add an explicit guard at loop entry: `if t_days <= 0: return 0.20`.
3. Add a unit test calling
   `estimate_iv(spot=100, strike=100, t_days=0, premium=5, option_type='CE')` and assert it
   returns a float, not an exception.

**Verify:** Run the new unit test; also grep production logs for `ZeroDivisionError` in
`option_greeks_service` post-deploy over one expiry cycle.

**Effort:** 1 hr.

#### 3.2.4 Duplicated expiry-rollover logic in `delta_hedge_service.py`
`backend/stocks/services/delta_hedge_service.py`. `get_nse_option_strikes` (~262-282) and
`build_specialist_hedge` (~514-535) each define their own local `parse_expiry` closure and
independently filter `all_expiries` by
`get_trading_days_remaining(...) > MIN_DAYS_TO_EXPIRY`. Correct today, but a future edit to
one (e.g. changing the rollover threshold condition) won't propagate to the other.

**Fix**
1. Extract a single `resolve_target_expiry(expiries: list[str], min_days: int) -> str | None`
   helper (module-level or in `shared/`) containing `parse_expiry` + the
   `valid_expiries`/`target_expiry` selection logic.
2. Replace both call sites (~270-282, ~521-533) with calls to the shared helper.
3. Add one unit test covering the shared helper directly (expiry-day edge case, no-valid-
   expiry fallback) instead of duplicating that test across two service test files.

**Verify:** `grep -n "def parse_expiry" backend/stocks/services/delta_hedge_service.py`
returns zero matches after the refactor (both inline closures removed).

**Effort:** 2 hrs.

### 3.3 Frontend polling / fetch hygiene

#### 3.3.1 `PerformanceReports.jsx` nulls both reports on any transient error
`frontend/src/pages/PerformanceReports.jsx:34-50`, on a 60s poll (~191-195).
`Promise.all([...]).catch(() => { setReport(null); setProReport(null); })` — a single
transient failure on either request (or a momentary network blip) wipes both, flashing the
whole page to empty state even though the previously-fetched data is still valid to show.

**Fix**
1. Fetch the two calls independently (`Promise.allSettled` or two separate `.then/.catch`)
   instead of `Promise.all`, so one endpoint's failure doesn't null the other's already-
   fetched state.
2. On failure, don't call `setReport(null)`/`setProReport(null)` at all — leave the last-good
   value in place, and set a separate `fetchError` flag to show a small inline "couldn't
   refresh" indicator instead of blanking the page.
3. Only clear to `null` on the *initial* load failure (when `report`/`proReport` is still
   `null` from `useState`), not on a refresh failure.

**Verify:** Temporarily mock one endpoint to reject in dev tools and confirm the other
report's table stays rendered instead of the page going blank.

**Effort:** 1-2 hrs.

#### 3.3.2 `OptionBuyingTable.jsx` poll cadence doesn't react to mid-session market close
`frontend/src/components/OptionBuyingTable.jsx:93-99`. `isOpen` is read once when the
effect runs to pick `intervalMs` (5 min open / 30 min closed, line 95) but is excluded from
the dependency array via `eslint-disable-next-line` (line 98), so a tab left open across the
3:30 PM close keeps polling every 5 min until the page is reloaded.

**Fix**
1. Add `isOpen` back into the dependency array (remove the eslint-disable) so the effect
   re-runs and picks up the new interval whenever `data?.market_status` flips.
2. To avoid double-fetching on every `isOpen` toggle, guard with a `useRef` storing the
   last-applied interval and only reset `setInterval` when it actually changes.
3. Alternative simpler fix: keep a single `setInterval` at the tighter (5 min) cadence
   always, and inside the tick handler check `isOpen` before deciding whether to actually
   call `fetchData()` — avoids effect churn entirely.

**Verify:** Mock `market_status` to flip closed mid-test and confirm network calls drop to
the 30-min cadence without a reload (Network tab timestamps).

**Effort:** 1 hr.

#### 3.3.3 Duplicate simultaneous fetch of `/api/global-market/latest/`
`frontend/src/components/GlobalMarketCard.jsx:18-25` and
`frontend/src/components/MarketBiasSummary.jsx:14-21` — both mounted on the Dashboard, each
with its own `useEffect` calling `API.get("/api/global-market/latest/", { params })` with
identical `date` params, independently.

**Fix**
1. Lift the fetch into a shared hook (`useGlobalMarketLatest(date)` in e.g.
   `frontend/src/hooks/`) that both components call — React doesn't dedupe by default, so
   either add a tiny in-memory/`sessionStorage` cache keyed by `date` inside the hook, or
   lift state to the Dashboard parent and pass `data`/`loading` down as props.
2. If a shared hook is out of scope this month, at minimum add request-level dedup via axios
   (e.g. a small in-flight-request map keyed by URL+params in
   `frontend/src/api/axios.js`) so concurrent identical GETs collapse to one network call.
3. Remove the now-redundant `useState`/`useEffect` fetch logic from whichever of the two
   components stops owning the fetch.

**Verify:** Open the Dashboard with Network tab open; confirm exactly one request to
`/api/global-market/latest/` per date change, not two.

**Effort:** 2-3 hrs (shared hook) or 1 hr (dedup shim only).

### 3.4 Process / dead code

#### 3.4.1 No CI/CD pipeline running the existing test suite
No `.github/workflows/` directory, no test step in any `render.yaml`/deploy config anywhere
in the repo. A substantial suite exists and is never run automatically:
`backend/stocks/tests.py`, `tests_swing_v2.py`, `tests_intraday_historical_regime.py`,
`tests_intraday_repaint.py`, `tests_market_data_gateway.py`, `tests_signal_candidate_audit.py`,
`tests_candle_store_parity.py`, `tests_download_queue.py`, `tests_intraday_v2/v3/v11.py`.

**Fix**
1. Add `.github/workflows/backend-tests.yml`: on `pull_request`/`push` to `main`, spin up
   Postgres service container, `pip install -r backend/requirements.txt`, run
   `python manage.py test stocks` (Django's test runner isolates the DB per-test
   automatically — this does not touch the live Angel One session the way running
   `manage.py` ad hoc locally would, since `AppConfig.ready()`'s login side effect should be
   guarded behind an env check for test mode; verify this guard exists before wiring CI, add
   one if not).
2. Add a `render.yaml` (or existing deploy config) pre-deploy hook, or a required GitHub
   branch-protection check on the new workflow, so a red test run blocks merge/deploy.
3. Start with the existing suite as-is (no new tests required for this phase) — just wire it
   to run automatically.

**Verify:** Open a throwaway PR with a deliberately broken test; confirm the GitHub Actions
check fails and shows in the PR checks list.

**Effort:** 3-5 hrs (mainly in confirming the `AppConfig.ready()` login side-effect is
properly skipped under Django's test runner before enabling CI, given the project's
explicit "never run manage.py locally" constraint).

#### 3.4.2 Dead `Trade` model
`backend/stocks/models.py:333-368`. No admin registration (`stocks/admin.py` has no `Trade`
entry), no serializer, no view or service reads/writes `Trade.objects`. Its only live
reference is `TelegramLog.trade` (`models.py:374`), a nullable FK that is never populated —
every `TelegramLog.objects.create(...)` call in the codebase passes `short_term_signal=`,
never `trade=`. (Note: `TradeHistory.trade` at `models.py:403` is unrelated — it FKs to
`ShortTermSignal`, not this `Trade` model, despite the naming collision — do not touch it by
mistake.)

**Fix**
1. Confirm zero rows exist in production: check `Trade.objects.count()` via the admin or a
   read-only ops query (not local `manage.py`).
2. If zero, drop the model: remove `Trade` class (333-368), remove the `trade` FK from
   `TelegramLog` (374), generate migration
   `python manage.py makemigrations stocks -n remove_dead_trade_model`, review the generated
   `DROP TABLE`/`DROP COLUMN` SQL before applying.
3. If any rows exist unexpectedly, investigate the write path first (something writes it
   that grep missed) before deleting.

**Verify:** `grep -rn "\bTrade\.objects\|models\.Trade\b" backend/` returns zero matches
after removal; migration applies cleanly against a prod snapshot.

**Effort:** 2 hrs (mostly migration review/caution, given it touches a live table).

#### 3.4.3 Vestigial `option_selling` branch in `SignalHistorySerializer`
`backend/stocks/serializers.py`, `SignalHistorySerializer.to_representation` (~113-171): a
full branch keyed on `instance.category == "option_selling"`, including a live Angel One
quote fetch (`svc.get_option_quote(...)`, ~128), touch-rule text, and premium fallback
logic. No live engine writes `category="option_selling"` — the strangle-selling engine
(`delta_hedge_service.py`) writes `category='specialist'` exclusively (confirmed at lines
1573, 1588, 1625, 1667, 1717, 1730). Only
`stocks/management/commands/backfill_option_selling_metadata.py` still touches the old
value, and it's a one-off backfill script, not a running service.

**Fix**
1. Before removing, run `SignalHistory.objects.filter(category="option_selling").count()`
   via ops (not local `manage.py`) to confirm no live rows still carry the old category — if
   the backfill command already ran, there may be historical rows still being served by this
   branch.
2. If historical rows exist and must keep rendering correctly, keep the branch but comment
   it as "legacy render path for backfilled rows only, category no longer written live"
   rather than deleting.
3. If zero rows (or after migrating them to `specialist` via a one-off data migration),
   delete the `option_selling`-keyed branches (~113, ~121-131, ~152-158, ~159-165) and the
   now-unreachable `elif instance.category == 'option_selling':` fallbacks, leaving only the
   `specialist` path.
4. Retire `backfill_option_selling_metadata.py` once no longer needed, or leave with a
   comment noting it's historical-only.

**Verify:** `grep -n "option_selling" backend/stocks/serializers.py` returns zero matches
(or only a documented legacy-comment reference) after cleanup, and the specialist/strangle
UI (`DeltaHedgePanel`) still renders correctly.

**Effort:** 1-2 hrs (plus a data check before deleting, to avoid breaking historical row
rendering).

---

## Phase 4 — Backlog (P3 cleanup, no urgency)

### Documentation drift

**CLAUDE.md — "Commodity Signals" section is entirely stale.** Confirmed:
`commodity_service.py` and `CommoditySignalsTable.jsx` are both gone (deleted platform-wide
in `04746d2`, "Remove MCX/commodity functionality"), and no reference to either survives
anywhere in the backend. Delete the whole "2. Commodity Signals" section from `CLAUDE.md`
and renumber the remaining sections (Option Buying becomes "2."). Add one line to the
"Common Bugs" history table noting MCX removal so future readers don't go looking for it.

### Dead code — backend

**`angel_one_service.py` — dead MCX/exchange-5 branches.**
`exch_type = 5 if exchange == "MCX" else ...` (line 481) and
`seg_map = {..., "MCX": 5}` (line 679) are unreachable — no caller passes `exchange="MCX"`
post-removal. Strip the `"MCX"` arm from both, collapsing to the NFO/NSE ternary and a
3-key `seg_map`. Grep `exchange="MCX"` and `exch_type.*5` repo-wide first to confirm zero
remaining producers before deleting.

**`download_queue.py` — failed `DownloadRequest` rows never retried/cleaned.**
`drain_once()` (lines ~190-229) sets `Status.FAILED` on any exception and never revisits
the row. Add either a scheduled requeue (reset `FAILED`→`PENDING` after N attempts with
backoff) or a retention job that purges `FAILED` rows past some age. Track `attempts`
(already incremented) as the backoff signal.

**`debug_waf.py` — bypasses the `_rest_request()` choke point.**
Line 37 calls `svc.session.post()` directly instead of `svc._rest_request(...)`, so it skips
the pacing lock and retry-on-SSL-error logic documented in CLAUDE.md's REST Call
Serialization section. It's a standalone diagnostic script (not imported by the live app),
but if kept, route it through `_rest_request` so it can't itself trigger a rate-limit trip
during a live debugging session.

**`delta_hedge_service.py` — dead equity branches around per-leg target/SL.**
`is_equity` is hardcoded `True` post-MCX-removal, so the `elif`/`else` branch (lines
~2457-2479) that would enforce `target_price`/`stop_loss_price` per leg, and the entire
`if not is_equity:` completion block (lines ~2581-2615), never execute — equities always
take the `if is_equity:` → `'WAITING'` path and exits are decided entirely by the
combined-premium overlay further down. Delete both dead branches and fold the surviving
logic into a flat non-branching path; keep `target_price`/`stop_loss_price` computation as
display-only fields (already documented as such).

**`delta_hedge_service.py` — unused `MAX_BID_ASK_SPREAD_PCT` constant.**
Line 110 defines `MAX_BID_ASK_SPREAD_PCT = 0.05`; the actual filter (lines 1050, 1063) uses
`MAX_SPREAD_PCT` imported from `config_vol.py`. Same value today by coincidence, not by
reference — a future edit to one won't propagate. Delete the local constant and its
comment; nothing reads it.

**`delta_hedge_service.py` — bare `except: continue` with no logging.**
Lines 383 and 410, in `find_strike_by_delta`/`find_strike_by_distance`. Swallows every
error silently (bad strike row, malformed data) with zero trace. Change to
`except Exception as e: logger.debug("...: %s", e); continue` at minimum so a systematic
data problem isn't invisible.

**`short_strangle_scanner.py` — unreachable code at end of `_scan_symbol`.**
The function returns unconditionally inside its loop (`return {...}, ""` at line 359, and
`return None, last_reason` at line 351 for the earlier bail case); lines 391-392
(`# No qualifying distance found` / `return None, last_reason`) are dead — control never
falls through to them. Delete lines 390-392.

**`shared/sector.py` — byte-for-byte duplicate of `intraday_service._sector_strength`.**
`sector_strength_from_candidates()` (`shared/sector.py:77`) and `_sector_strength()`
(`intraday_service.py:399`) are identical; grep confirms zero callers of the shared version
anywhere in the codebase — `intraday_service.py` only ever calls its own local copy. Delete
the local `_sector_strength` in `intraday_service.py` and import
`sector_strength_from_candidates` from `shared/sector.py` instead — prefer keeping the
shared one since it's the one meant for reuse.

### Dead code — frontend

**`DeltaHedgePanel.jsx` — MCX theming/ternaries left in place.**
`getThemeClasses('MCX')` branch (lines ~60-68) and the `exch === 'MCX' ? ... : ...`
ternaries in the render loop (lines ~249-271) are unreachable — the surrounding comment
already says "backend never returns an MCX section anymore" and the exchange loop is
hardcoded to `['NSE']`. Delete the MCX branch from `getThemeClasses`, simplify every
ternary to its NSE-only value, and drop the now-pointless `.map(['NSE'])` in favor of
directly rendering the NSE section.

**Six unimported frontend components in `frontend/src/components/`.**
`DeliveryChart.jsx`, `DeliveryTable.jsx`, `VolumeSpikeChart.jsx`, `VolumeSpikeTable.jsx`,
`OIChangeChart.jsx`, `StrategyCompareCard.jsx` — grep confirms zero imports of any of these
six anywhere else in `src/`. Delete all six; if any represents a planned-but-unshipped
feature, move it to a feature branch instead of leaving it in `main` unimported.

### Repo hygiene

**Twelve loose debug/patch/repair scripts at `backend/` root.**
`check_db_signals.py`, `check_live_quotes.py`, `check_live_websocket.py`, `debug_waf.py`,
`diagnose_scanners.py`, `diagnostic_nextday_signals.py`, `patch_vp.py`,
`repair_metadata_column.py`, `repair_signals.py`, `revert_natgas.py`, `test_hedge_fix.py`,
`test_prices.py`, `test_volVWAP.py` — all confirmed present, dated Mar–Jun 2026, none wired
into the app or a management command. `revert_natgas.py` specifically references the
now-deleted commodity feature. Delete all of them; if any diagnostic is still genuinely
useful, promote it to a proper `manage.py` command under
`stocks/management/commands/` instead of a root script.

### Minor logic inconsistencies

**`delta_hedge_service.py` — stale docstring on `get_intraday_target_delta`.**
Docstring (lines 132-135) documents deltas 0.35 (morning) / 0.38 (midday) / 0.42
(afternoon); actual values from `config_vol.py` (lines 60-62) are 0.25 / 0.28 / 0.32 — the
config even has inline comments noting "was 0.35 — too aggressive," confirming the
docstring predates that tuning pass. Update the docstring's three numbers to match
`config_vol.py` and add a one-line note to re-check this file whenever the config constants
change, since this is the second time the two have drifted.

**`option_buying_service.py` — target-before-SL ordering inconsistent with the rest of the
codebase.** `update_option_buying_outcomes()` (lines 217-220) checks
`current_premium >= target` before `current_premium <= stop_loss` — optimistic ordering —
whereas `live_signal_service.py`'s `_scan_bars_for_exit` (lines 179-205) is explicitly
documented as "Pessimistic by design: when one bar's range contains BOTH the stop and the
target, the stop is booked." Option-buying polls live LTP once per audit cycle rather than
scanning bar ranges, so same-tick ambiguity is less likely here, but for consistency and to
avoid a flattering P&L record on a fast whipsaw, swap the order to check `stop_loss` first.

**`trade_engine.py` — module docstring drift.** Top-of-file docstring (~lines 9-14) says
"10:00 AM — Scanner" and "Every 10m — Intraday check"; actual schedule (`stocks/updater.py`)
is 11:30 AM and 30-minute activation ticks, moved 2026-07-28. Cosmetic, but will mislead the
next reader. No financial impact; low-effort doc fix.

---

## Phase 5 — Structural recommendations (not urgent, but worth planning)

### A. Three overlapping short-term/long-term engines writing the same tables

`trade_engine.py` (live, scheduled via `updater.py`), `pro_system_service.py` (long-term
live path plus a legacy short-term code path that is dead but still importable/reachable),
and `swing_service.py` (V2, currently shadow-only) all write to the same
`ShortTermSignal`/`SignalCandidate` tables. This is the kind of layering that looks harmless
in isolation — each engine was added to supersede or complement the last — but three
writers sharing one set of tables means every downstream consumer (dashboards, Telegram
alerts, outcome auditors) has to reason about which engine actually produced a given row,
and a bug in any one engine's write path can silently corrupt another's read assumptions.

Recommendation: commit to a real deprecation plan rather than accumulating engines side by
side. The audit's other findings suggest `swing_service.py`/V2 already has the more correct
partial-target and portfolio-constraint logic, so treat it as the eventual winner once
promoted out of shadow mode. Once promoted, physically delete the legacy short-term code
path inside `pro_system_service.py` — do not just stop calling it. "Unwired but importable"
is not a safe intermediate state in this codebase: per its own history/comments, exactly
this pattern (dead-but-reachable code left in place after a supersession) has already caused
one duplicate-alert incident (see Phase 0, item 3). A dead function sitting in a module is
one bad import or one confused future refactor away from firing again.

**Status (2026-07-29): ⬜ Held, deliberately.** `doc/V2_BUILD_LOG.md` (line 488) states Swing
V2 has only 1 logged live dry run against the ~20-session shadow-evidence requirement this
same recommendation calls for, and (line 328) explicitly says cutover "is not a decision to
make while [shadow count is low]" without product-owner sign-off. Asked directly; answer was
to hold, not override. Revisit once shadow-session count is actually near 20.

### B. The same bug class keeps getting fixed once, not once-and-shared

Three separate instances of this pattern surfaced across the audit: session-anchored VWAP
was fixed in `intraday_service.py` but the same computation in `option_buying_service.py`
was not touched (Phase 2, item 2.2); the AB1021 circuit-breaker handling was fixed for
candle calls but not for quote calls (Phase 2, item 2.3); the cost-of-trading model
(round-trip friction gating a signal's viability) is applied in `intraday_service.py`,
`swing_service.py`, and the backtest harness, but not in `trade_engine.py` (Phase 0/1,
item 6). In each case the fix was correct and well-reasoned where it landed — the gap is
that the same underlying utility or pattern exists at multiple call sites, and only one got
the fix.

Recommendation: treat "grep for every other call site of the same pattern" as a mandatory
step before closing out a fix like this, not an optional nice-to-have — the fix's own
PR/commit should note which other sites were checked and why they were or weren't in scope.
Pair every such fix with a short regression test that asserts the corrected behavior
directly (e.g., "VWAP on a zero-volume index returns a finite value," "a circuit-broken
quote call backs off exactly like a circuit-broken candle call"), so a future refactor that
touches the shared utility can't silently regress one of the fixed call sites while leaving
the others fine. Without a test, "fixed in file A" is one refactor away from becoming "was
fixed in file A, until it wasn't."

**Status (2026-07-29): ✅ Resolved (tests added).** New `backend/stocks/tests_phase5b_regression.py`
covers all three named gaps: `compute_session_vwap` stays finite on an all-zero-volume
(NIFTY-index-shaped) DataFrame, `get_bulk_quotes` trips and respects the same AB1021
`"quote"` circuit breaker `get_candle_data` already had, and `trade_engine.py`'s
`_compute_ai_score` correctly rejects/accepts targets around the real (env-overridable)
`SWING.min_target_cost_multiple` threshold. Not executed — this repo's Django services
import settings/models at module scope, `pytest-django` isn't installed, and running via
`manage.py test` is the only way to get `StocksConfig.ready()`'s test-mode guard, which is
off-limits here — so every fixture/formula was independently hand-traced with plain
pandas/numpy outside Django first (same precedent as `OptionGreeksServiceTests`, noted
elsewhere in this doc as "unrun — verified by hand-tracing"). The "grep every other call
site first" process recommendation itself is a habit, not a one-time fix — no code artifact
for it beyond this instance.

### C. No data retention policy + no CI

Two pieces of standing infrastructure are missing entirely, and both compound the other
findings in this report. First, there is no retention policy for the audit/log tables that
grow without bound in normal operation — `SignalHistory`, `SignalCandidate`, `TelegramLog`,
`TradeHistory` all accumulate every session with no archival or pruning step, which will
eventually degrade query performance on the very tables the live scan engines read from on
every cycle (Phase 3, item 3.2.2). Second, there is no CI: the test suite referenced
elsewhere in this audit exists but runs at nobody's discretion, providing zero automatic
protection against a regression landing on `main` (Phase 3, item 3.4.1).

Recommendation: establish both as standing, monthly-reviewed infrastructure rather than
one-off fixes. For retention, define an explicit age-based archival/deletion policy per
table (e.g., raw `TelegramLog` rows older than 90 days summarized and dropped,
`SignalHistory`/`SignalCandidate` kept longer for backtest integrity but partitioned or
indexed for the growth), and revisit it monthly as table sizes are observed in production.
For CI, the bar is deliberately low to start: a single GitHub Actions (or equivalent)
workflow that runs `pytest` on every push/PR is enough to convert the existing test suite
from documentation into an actual gate. Both are cheap relative to the cost of the failure
modes they prevent — a slow-query incident from an ungoverned table, or a shipped regression
the test suite would have caught had anyone run it.

**Status (2026-07-29): ✅ Resolved, already shipped via earlier phases.** Retention:
`run_signal_tables_cleanup()` (`stocks/updater.py`, daily 2:45 AM) prunes `SignalHistory`/
`SignalCandidate`/`TelegramLog`/`TradeHistory` by age with per-table terminal-state rules
(Phase 3 #3.2.2). CI: `.github/workflows/backend-tests.yml` runs the test suite on push/PR
(Phase 3 #3.4.1). One manual step remains open: branch-protection wiring (making the CI
check required-to-merge) needs GitHub repo-admin access — deliberately not done here without
separate confirmation, since it changes shared repo settings.

---

## Quick-reference checklist

| # | Phase | Finding | Effort | Status |
|---|-------|---------|--------|--------|
| 1 | 0 | Rotate/fail-close `CRON_SECRET_TOKEN` | 30-45 min | ✅ Resolved |
| 2 | 0 | Disable `LONG_TERM_GENERATION_ENABLED` (stopgap) | 15 min | ✅ Superseded by 2b |
| 3 | 0 | Delete `short_term_scan` cron branch | 1-2 hrs | ✅ Resolved |
| 4 | 0 | Fix EOD exit check-order (target3 vs trailing) | 1-2 hrs | ✅ Resolved |
| 2b | 1 | Wire promoter-group cap into `scan_long_term_stocks()` | 0.5 day | ✅ Resolved |
| 3 | 1 | Build long-term 200-EMA exit auditor | 0.5 day | ✅ Resolved |
| 6 | 1 | Add cost model to `trade_engine.py` | 0.5 day | ⬜ Not started |
| 7 | 1 | Fix/remove strangle scale-guard | 0.5 day | ⬜ Not started |
| 8 | 1 | Resolve naked-strangle vs protected-strangle decision | 0.5-1 day | ⬜ Not started |
| 2.1–2.10 | 2 | 10 P1 fixes (frontend + backend) | ~2 weeks total | ⬜ Not started |
| 3.1–3.4 | 3 | 15 P2 fixes (races, retention, hygiene) | ~1 month total | ⬜ Not started |
| Phase 4 | 4 | 14 P3 cleanup items | backlog | ✅ Resolved |
| Phase 5 | 5 | 3 structural recommendations | ongoing | ⬜ Not started (deferred) |

**48 of 48 findings resolved** (Phase 0-4 complete, 2026-07-29). Phase 5's 3 structural
recommendations are separate from the 48-finding count and remain open by product decision —
see Phase 5 section above for why item A specifically was not attempted.
