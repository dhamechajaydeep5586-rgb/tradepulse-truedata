# 08 — Database & API Design

## 1. Current Database — Full Inventory (11 models, verified by full read of `stocks/models.py`)

| Model | Table | Status | Notes |
|---|---|---|---|
| `SignalHistory` | `signal_history` | **Live** | Generic, `category`-discriminated. Used by intraday/commodity/strangle. Unique constraint on `(symbol, category, status IN [PENDING,ACTIVE])`. |
| `IndexConstituent` | `index_constituents` | Live | Nifty 500 membership cache. |
| `SignalChangeLog` | `signal_change_log` | Live | Audit trail for `SignalHistory` transitions. |
| `Notification` | `notifications` | Live | User-facing notification bell. |
| `OptionChain` | `option_chains` | Live | Snapshot cache for option chain view. |
| `Stock` | `stocks` | Live | EOD OHLCV + delivery %, used by `trade_engine.py`. |
| `IntradaySignal` | `intraday_signals` | **Dead** | Zero references outside `models.py`/migrations. Delete in Phase 1. |
| `MarketHoliday` | `market_holidays` | Live | Secondary to the NSE-API-first holiday check per `CLAUDE.md`. |
| `ShortTermSignal` | `short_term_signals` | Live | The real swing-signal table, written by `trade_engine.py`. Richest state machine in the codebase (13 statuses). |
| `StockDailyData` | `stock_daily_data` | Live | Technical indicator cache (EMA/RSI/ADX/ATR) per stock per day. |
| `TradeScanner` | `trade_scanner` | Live | Scoring snapshot per scan (ai/trend/momentum/volume/sector/fundamental/risk scores — fundamental is hardcoded 10.0, see `04`). |
| `Trade` | `trades` | Live | Older/parallel trade-tracking model, used alongside `ShortTermSignal` inside `trade_engine.py`. |
| `TelegramLog` | `telegram_logs` | Live | Delivery audit for Telegram alerts. |
| `TradeHistory` | `trade_history` | Live | Status-transition audit trail for `ShortTermSignal`. |

**Architectural finding restated from `01`**: two unrelated model families (`SignalHistory`-based vs. `Stock`/`Trade`/`ShortTermSignal`-based) coexist with no shared base or lifecycle contract. Every new table proposed below is designed to sit *behind* a unifying service interface rather than adding a third incompatible family.

## 2. New Tables Required (consolidated from docs 02, 03, 05, 07)

```python
# core/risk_engine (from 02)
class Account(models.Model): ...          # total_capital, limits, circuit breaker state
class SectorMapping(models.Model): ...     # symbol -> real sector, replaces cosmetic labels
class RiskEvent(models.Model): ...         # audit trail of every risk-engine allow/block decision

# core/portfolio (from 05)
class Holding(models.Model): ...           # confirmed positions, distinct from signals
class CashLedger(models.Model): ...        # running cash balance

# engines/option_income (from 03)
class IVSnapshot(models.Model):            # daily ATM IV per symbol, powers IV Rank/Percentile
    symbol = models.CharField(max_length=20, db_index=True)
    date = models.DateField(db_index=True)
    atm_iv = models.DecimalField(max_digits=6, decimal_places=2)
    class Meta:
        unique_together = [['symbol', 'date']]

# engines/swing (from 04)
class FundamentalSnapshot(models.Model):   # quarterly-refresh, powers a REAL fundamental_score
    symbol = models.CharField(max_length=20, db_index=True)
    period = models.CharField(max_length=10)  # e.g. "2026Q1"
    revenue_growth_yoy = models.DecimalField(max_digits=8, decimal_places=2, null=True)
    profit_growth_yoy = models.DecimalField(max_digits=8, decimal_places=2, null=True)
    roe = models.DecimalField(max_digits=8, decimal_places=2, null=True)
    roce = models.DecimalField(max_digits=8, decimal_places=2, null=True)
    debt_to_equity = models.DecimalField(max_digits=8, decimal_places=2, null=True)
    promoter_holding_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True)
    promoter_holding_change_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True)
    class Meta:
        unique_together = [['symbol', 'period']]

# backtesting (from 07)
class BacktestRun(models.Model):
    strategy = models.CharField(max_length=50)
    regime_tag = models.CharField(max_length=50, blank=True)  # "covid_crash", "high_vix", etc.
    start_date = models.DateField()
    end_date = models.DateField()
    config_snapshot = models.JSONField()   # exact RiskEngine/strategy config used, for reproducibility
    results = models.JSONField()            # sharpe, sortino, calmar, win_rate, max_dd, etc.
    created_at = models.DateTimeField(auto_now_add=True)
```

## 3. API Surface — Current (13 routes) + Proposed New

### 3.1 Current (verified against `stocks/urls.py`, all `IsAuthenticated`)
`live-signals/`, `commodity-signals/`, `option-chain/`, `fii-dii/`, `live-price-updates/`, `performance-report/`, `signal-backtest/`, `pro-system/`, `pro-performance-report/`, `delta-hedge/`, `notifications/`, `cron-trigger/`, `groww-preview/`. Full detail in `01_PROJECT_ARCHITECTURE.md` §1.7.

### 3.2 New endpoints required

| Endpoint | Method | Purpose | Doc reference |
|---|---|---|---|
| `/api/risk/dashboard/` | GET | Real risk metrics, replaces client-side fiction in `ProSystem.jsx` | `02` §8 |
| `/api/risk/account/` | GET/PATCH | Read/update `Account.total_capital` and limits | `02` §7 |
| `/api/portfolio/holdings/` | GET | Current confirmed positions | `05` §2 |
| `/api/portfolio/allocation/` | GET | Sleeve allocation vs. target, rebalance flags | `05` §3-4 |
| `/api/option-income/candidates/` | GET | Multi-structure candidates (strangle/IC/CSP/covered call/credit spread) | `03` §3 |
| `/api/swing/candidates/` | GET | Redesigned composite-score swing candidates | `04` §2 |
| `/api/backtest/run/` | POST | Trigger a portfolio-level backtest (async — see §5) | `07` §2.2 |
| `/api/backtest/results/<id>/` | GET | Retrieve a completed `BacktestRun` | `07` |
| `/api/ai/trade-review/<signal_ref>/` | GET | On-demand AI trade review | `06` §3.2 |
| `/api/ai/portfolio-review/` | GET | Weekly portfolio review (also generated on schedule) | `06` §3.3 |

## 4. Authentication & Authorization

Current: JWT via `rest_framework_simplejwt`, custom `User` model in `users/` app, `IsAuthenticated` on every stocks endpoint, `IsAdminUser` gate on registration (confirmed: only an admin can create new users — appropriate for a single/small-user private trading tool). **No role differentiation beyond admin/non-admin** — fine at current single-user scale; if multi-user is ever a real requirement, add a `role` field (`OWNER`, `VIEWER`) and gate write endpoints (risk config, account settings) to `OWNER` only. Not needed now — noted for future expansion per `01` §3.9, not a Phase 1-10 requirement.

## 5. Caching, Redis, Queue, Scheduler (full detail in `01_PROJECT_ARCHITECTURE.md` §3.4-3.5 — summarized here for the DB/API document's completeness)

- **Redis** replaces `LocMemCache`. Concretely: `CACHES = {"default": {"BACKEND": "django_redis.cache.RedisCache", "LOCATION": os.getenv("REDIS_URL")}}`. Render offers a managed Redis add-on; if avoiding additional cost is a hard constraint, a free-tier Redis (Upstash has a free tier suitable for this traffic volume) is a reasonable substitute — flag the cost decision explicitly rather than assuming either.
- **Queue**: RQ (over Celery) per `01` §3.5's reasoning — lower operational overhead, sufficient for this project's scheduled+on-demand job volume.
- **Scheduler**: keep APScheduler for trigger *definitions* (it's not the problem — where the jobs *execute* is), but have each job enqueue an RQ task rather than running inline in the gunicorn process.

## 6. Audit Logs

Two already exist and are good patterns to extend, not replace: `SignalChangeLog` (SignalHistory-family transitions) and `TradeHistory` (ShortTermSignal-family transitions). Add `RiskEvent` (§2, from `02`) as the third — every risk-engine decision, allowed or blocked. **Do not build a fourth, unrelated audit-log model** — if the unified `core/signal_lifecycle` service (`01` §3.1) ships, it should absorb both existing audit logs behind one interface rather than each new engine inventing its own.

## 7. Versioning & Migration Strategy

- **API versioning**: not currently needed (single first-party frontend, no external API consumers) — don't add `/v1/`/`/v2/` prefixes speculatively. Revisit only if a third-party integration or public API is ever planned.
- **DB migrations**: standard Django migrations, already in use. One concrete process improvement: `IntradaySignal`'s deletion (§1, dead model) should be its own dedicated migration/PR, run and verified in isolation, *before* any of the larger schema additions in §2 — smaller, reversible steps, consistent with this engagement's own commit discipline (one logical change per commit) applied at the migration level.
- **Data backfill**: `IVSnapshot` and `FundamentalSnapshot` (§2) both need historical backfill before they're useful (a single day of IV data can't compute a rank). Sequence: ship the model and start collecting forward data immediately (Phase 2), backfill history opportunistically where a data source allows it, but don't block the feature launch on having a full year of history — IV Rank becomes gradually more meaningful as forward data accumulates, and that's an acceptable interim state as long as it's surfaced honestly (e.g., "IV Rank based on 45 days of data" rather than presenting it as a full 252-day rank prematurely).
