# Long-Term Investment Engine V2 — Architecture

**Status: PROPOSED — architecture only, no code.**

Source of truth: `doc/long_term_stock.md`, `doc/INSTITUTIONAL_AUDIT_PLATFORM.md`, and the
current implementation.

**Preserved:** 1–2 year holding period. Angel One as broker/price source. Django + DRF +
APScheduler + PostgreSQL. Everything else is redesigned.

---

## 0. THE PRECONDITION THAT DEFINES THIS DESIGN

**A quality/value/growth engine cannot be built on a price feed.**

Of the seventeen inputs requested — Quality, Growth, Value, Quality Ranking, Sector
Leadership, Market Cap, Institutional Ownership, Earnings Quality, Cash Flow Quality,
Capital Allocation, ROE, ROCE, Debt, Margins, Relative Strength, Risk Score, Momentum
Factor — **exactly three are computable today**: Momentum, Relative Strength, and a
partial Risk Score. The other fourteen require a fundamental data feed that does not exist
anywhere in the codebase.

The current engine's response to that gap was to return `roe: 22.5`, `debt_to_equity: 0.2`,
`profit_growth: 15.0` as hardcoded literals with the comment `# Quality proxy defaults`,
and to label `perf_100d` as `revenue_growth`. Those constants reach the UI and read as
data. **That is the single worst defect in the platform** — worse than absent data,
because absent data is visibly absent.

**Therefore the governing design rule of V2 is:**

> If the fundamental factor set is unavailable or stale, the engine emits **nothing**.
> It does not fall back to price momentum. It does not substitute defaults.

Falling back to momentum is exactly how the current book became 100% Adani.

### 0.1 Data procurement — the gating decision

| Tier | Source | Covers | Cost | Effort |
|---|---|---|---|---|
| **Free** | NSE/BSE XBRL quarterly + annual filings | Revenue, EPS, ROE inputs, debt, margins, cash flow | ₹0 | High — parsing, normalisation, restatement handling |
| **Free** | NSE/BSE shareholding-pattern filings | Promoter holding, **promoter pledge**, FII/DII per stock | ₹0 | Medium |
| **Mid** | Tijori / Trendlyne / Tickertape | Pre-normalised ratios, history | Low–mid | Low |
| **Institutional** | LSEG/Refinitiv, S&P Capital IQ, FactSet | Full, point-in-time, restatement-aware | High | Low |

**Recommendation: mid-tier commercial for ratios + free NSE/BSE filings for ownership and
pledge.** Shareholding data is genuinely free and is where the highest-value India-specific
signal lives.

**Point-in-time integrity is non-negotiable.** A vendor that overwrites restated
financials makes every backtest look better than reality. If the vendor cannot deliver
as-reported-at-the-time data, the backtest must be treated as an upper bound and labelled
as such.

**Lag rule:** a quarter's data becomes usable only after its filing date, never its period
end. Using Q2 numbers on 1 October when they were filed 12 November is lookahead bias, and
it is the most common way a fundamental backtest lies.

---

## 1. INSTITUTIONAL ARCHITECTURE

### 1.1 Position in the platform

The long-term engine is the **third consumer** of the shared services promoted in
`doc/SHORT_TERM_ENGINE_V2_ARCHITECTURE.md` §1.2. It duplicates none of them. It adds one
new subsystem the other two engines do not need: the fundamental data layer.

```
backend/stocks/services/
├── shared/                       ← consumed, not duplicated
│   ├── profiles.py               EngineProfile.LONG_TERM
│   ├── universe.py               profile=long_term
│   ├── regime.py                 horizon=structural
│   ├── ranking.py                LT factor weights
│   ├── portfolio_risk.py         LT caps + promoter-group cap
│   ├── risk_engine.py            vol-targeted sizing (NOT risk-parity — see §9.5)
│   ├── sector.py                 sector leadership from sector indices
│   ├── calendar_service.py       earnings dates → quarterly review trigger
│   └── outcome_store.py          LT trade ledger
├── fundamentals/                 [NEW — the only LT-specific subsystem]
│   ├── port.py                   FundamentalProvider protocol (vendor-agnostic)
│   ├── providers/                one adapter per vendor + NSE/BSE filings
│   ├── normalise.py              units, fiscal-year alignment, restatement handling
│   ├── quality.py                accruals, cash conversion, capital allocation
│   └── staleness.py              filing-lag enforcement + freshness gate
├── longterm_scoring.py   [NEW]   six-factor composite (§2)
└── longterm_service.py   [NEW]   orchestration: scan, review, rebalance, exits
```

`pro_system_service.py` is reduced to `get_market_direction()` and
`NIFTY500_FALLBACK`. `_fetch_long_term_quality`, `scan_long_term_stocks`,
`get_pro_system_data`, and `update_pro_system_outcomes` are deleted.

### 1.2 Cadence — four separate jobs, each with its own scheduler entry

The current engine has **no job at all**; it runs as a side effect of a Telegram
formatter, only on days the short-term strict pass is empty. That is replaced by:

| Job | Cadence | Purpose |
|---|---|---|
| **Screen** | Monthly, 1st trading day, 17:00 | Full universe rescore; produce candidate list |
| **Review** | Weekly, Saturday 07:00 | Technical deterioration, trailing stops, drawdown flags |
| **Earnings review** | Event-driven, T+2 after a holding reports | Fundamental re-score on fresh data |
| **Rebalance** | Semi-annual (April, October) | Weight drift correction, forced diversification |

Monthly screening is deliberate. Fundamentals update quarterly; scanning daily manufactures
turnover against data that has not changed, and turnover is pure cost in a 1–2 year strategy.

### 1.3 Data flow

```
NSE/BSE filings ─┐
Vendor ratios   ─┼─► fundamentals/normalise ─► staleness gate ─┐
Shareholding    ─┘        (filing-lag enforced)                │
                                                                ▼
Angel One daily candles ─► corporate-action adjust ─► momentum, RS, vol, beta
                                                                │
                                    ┌───────────────────────────▼──────────┐
                                    │  longterm_scoring: 6 factor families │
                                    │  sector-neutral z-scores → composite │
                                    └───────────────────────────┬──────────┘
                                                                ▼
                                             decile assignment (§3)
                                                                ▼
                       shared/portfolio_risk: sector, group, correlation, beta caps
                                                                ▼
                       shared/risk_engine: inverse-vol weights, capped
                                                                ▼
                              SignalHistory(category="long_term") + audit trail
```

---

## 2. COMPLETE SCORING MODEL

Six factor families. Every input is **z-scored within its sector**, winsorised at ±3σ,
then combined. Sector-neutrality matters: raw value screens buy the cheapest sector, which
is a sector bet wearing a factor costume.

### 2.1 Quality — 30%

The heaviest weight, because quality is what makes a 1–2 year hold survivable.

| Metric | Formula | Direction | Sub-weight |
|---|---|---|---|
| ROE | Net income / avg shareholders' equity | Higher | 20% |
| ROCE | EBIT / (total assets − current liabilities) | Higher | 20% |
| Debt / Equity | Total debt / equity | **Lower** | 15% |
| Interest coverage | EBIT / interest expense | Higher | 10% |
| Accruals ratio | (Net income − CFO) / total assets | **Lower** | 15% |
| Cash conversion | CFO / EBITDA | Higher | 10% |
| Margin stability | 1 / stdev(operating margin, 5y) | Higher | 10% |

**Accruals ratio is the earnings-quality signal.** A company whose reported profit
persistently exceeds its operating cash flow is accruing earnings it has not collected.
Sloan (1996) and thirty years of replication make this one of the better-evidenced
fundamental anomalies, and it is the specific check that separates real profit from
accounting profit.

### 2.2 Growth — 20%

| Metric | Formula | Sub-weight |
|---|---|---|
| Revenue CAGR | 3-year | 30% |
| EPS CAGR | 3-year | 30% |
| Operating margin trend | Slope of 8-quarter margin | 20% |
| ROCE trend | Slope of 3-year ROCE | 20% |

Trends, not just levels — improving mediocrity often beats deteriorating excellence over a
2-year horizon.

### 2.3 Value — 15%

| Metric | Formula | Sub-weight |
|---|---|---|
| Earnings yield | EBIT / enterprise value | 40% |
| FCF yield | (CFO − capex) / market cap | 40% |
| EV/EBITDA | Inverted | 20% |

Deliberately the smallest of the three fundamental families. In Indian large/mid caps,
quality and momentum have been the more reliable payers; value alone tends to buy
structurally impaired businesses. **Sector-neutral z-scoring is mandatory here.**

### 2.4 Momentum — 20%

| Metric | Formula | Sub-weight |
|---|---|---|
| 12-1 month return | Return from t−12m to t−1m | 50% |
| Distance above 200-DMA | (Close − DMA200) / DMA200 | 25% |
| 6-month relative strength vs Nifty 500 | 25% |

**12-1, not 12-0.** Skipping the most recent month avoids short-term reversal, which
contaminates raw 12-month momentum. This is the standard academic construction and the
current engine's `perf_100d` does not do it.

### 2.5 Ownership & Governance — 10%

India-specific, and the family that would have prevented the current book.

| Metric | Direction | Sub-weight |
|---|---|---|
| **Promoter pledge %** | **Lower — hard veto above threshold** | 35% |
| Promoter holding level | Higher | 20% |
| Promoter holding trend (4q) | Higher | 15% |
| FII holding trend (4q) | Higher | 15% |
| DII holding trend (4q) | Higher | 15% |

**Promoter pledge is a hard veto, not a score input, above 25%.** Pledged promoter equity
is the single best-documented precursor of catastrophic drawdown in Indian mid-caps: a
falling price triggers margin calls on the pledge, which forces sales, which accelerates
the fall. No composite score should be able to outvote it.

### 2.6 Size & Liquidity — 5%

Market-cap decile and 20-day ADV. Low weight — this is a tradability tiebreaker, not alpha.

### 2.7 Composite

```
composite = 0.30·Quality + 0.20·Growth + 0.15·Value
          + 0.20·Momentum + 0.10·Ownership + 0.05·Size
```

Then a **Risk Score** is computed separately (it modulates *size*, not rank — §9.5):
realised volatility, beta to Nifty 500, max drawdown over 3 years, earnings variability,
and pledge level.

### 2.8 Hard vetoes — applied before scoring

Any one disqualifies regardless of composite:

- Promoter pledge > 25%
- Debt/Equity > 3.0 (ex-financials)
- Negative CFO in 2 of the last 3 years
- Auditor resignation or qualified opinion in the last 4 quarters
- Fundamental data staleness > 180 days
- ADV < ₹5 cr or price < ₹50
- Regulatory action / exchange surveillance flag (ASM/GSM stage 2+)

---

## 3. RANKING MODEL

1. Compute composite for every surviving universe member.
2. Assign **deciles** cross-sectionally. Deciles, not raw scores, because the score's
   absolute level is not comparable across periods while its rank is.
3. Also record **sector-relative decile** — leadership within a sector is a distinct signal
   from leadership across the market.
4. Rank stability filter: require the stock to be in the top 3 deciles for **2 consecutive
   monthly screens** before it becomes buy-eligible. This removes single-period noise and
   is the reason the screen runs monthly rather than quarterly.

**Sector leadership** comes from `shared/sector.py` using real sector-index daily candles
(11 calls once a month — trivially affordable at this cadence, unlike intraday).

---

## 4. BUY RULES

All must hold:

| # | Condition |
|---|---|
| 1 | No hard veto (§2.8) |
| 2 | Composite in **decile 1–2** |
| 3 | Top-3-decile membership sustained across 2 consecutive monthly screens |
| 4 | `close > EMA200` on **weekly** bars — do not catch a falling knife |
| 5 | Sector not in the bottom 2 of sector-leadership rank |
| 6 | Structural regime not `RISK_OFF` (§9.6) |
| 7 | Passes portfolio constraints: stock ≤ 8%, sector ≤ 25%, promoter group ≤ 12%, cluster cap, portfolio beta ≤ 1.2 |
| 8 | Fundamental data fresh (< 180 days) and point-in-time valid |
| 9 | Position count < 20 |

**Entry is staged and mechanically tracked** — the current `entry_plan` string
("Buy 30% Now, 30% on 10% dip, 40% on major correction") becomes real:

| Tranche | Trigger | Size |
|---|---|---|
| 1 | On signal | 40% of target weight |
| 2 | −8% from tranche-1 price, thesis intact at next review | 30% |
| 3 | −15% from tranche-1 price, thesis intact at next review | 30% |

Tranches 2 and 3 require the composite to still be in decile 1–3 at the time of the dip.
**Averaging into a deteriorating thesis is how a 15% loss becomes a 60% loss.** Tracked via
a new `PositionTranche` table; average entry recomputed on each fill.

---

## 5. HOLD RULES

A position is held while all of:

- Composite in **decile 1–4** (wider than the buy band — deliberate hysteresis, so a
  position is not sold on a one-decile drift)
- No hard veto triggered
- `close > EMA200` on monthly bars
- Position drawdown from peak < 25%
- Holding period < 24 months

Hysteresis between buy (decile 1–2) and hold (decile 1–4) is the mechanism that keeps
turnover low. Without it, a position oscillating around the decile-2 boundary would be
bought and sold repeatedly, and in a 1–2 year strategy turnover is almost pure cost.

---

## 6. ADD RULES

Add only when:

- Tranche 2 or 3 trigger fires (§4) **and** composite still decile 1–3
- Position weight has drifted below 60% of target and composite is still decile 1–2
- Semi-annual rebalance restores an underweight position

Never add to a position that is down for a *fundamental* reason. The tranche triggers are
price-based; the decile re-check is what distinguishes "cheaper" from "broken."

---

## 7. REDUCE RULES

Trim to half weight when any one fires:

| Trigger | Rationale |
|---|---|
| Composite falls to **decile 5–6** | Thesis weakening but not broken |
| Position weight > 12% (from appreciation) | Concentration drift |
| Sector exposure > 25% | Sector drift |
| Promoter group exposure > 12% | Group drift — the Adani control |
| Portfolio beta > 1.2 | Reduce the highest-beta holding first |
| Pledge rises above 15% but below the 25% veto | Early warning |

Reductions are executed at the next scheduled review, not intraday. This is a 1–2 year
strategy; same-day reaction is noise.

---

## 8. EXIT RULES

Full exit on any one. Ordered by precedence — first match wins, and the order matters
because it determines the recorded `exit_reason`.

| # | Rule | Trigger | Evaluated |
|---|---|---|---|
| 1 | **Hard veto breach** | Pledge > 25%, auditor resignation, D/E > 3.0, governance flag | On filing / event |
| 2 | **Fundamental deterioration** | Composite in decile 8–10 | Monthly screen |
| 3 | **Sustained deterioration** | Composite in decile 7+ for **2 consecutive** screens | Monthly screen |
| 4 | **Technical deterioration** | Monthly close < EMA200 for **2 consecutive months** | Monthly |
| 5 | **Trailing stop** | Chandelier: peak close − 3×ATR(14, weekly), ratcheting up only | Weekly review |
| 6 | **Max drawdown** | −30% from peak | Weekly review |
| 7 | **Time exit** | 24 months held | Weekly review |
| 8 | **Rebalance exit** | Displaced by a higher-decile candidate when at max positions | Semi-annual |

Design notes:

- **Rules 2 and 3 differ deliberately.** Decile 8+ is decisive enough to act on
  immediately; decile 7 requires confirmation. Single-period fundamental scores are noisy.
- **Rule 4 uses monthly closes, not daily.** The current `hold_rule` string says "exit if
  trend breaks below 200 EMA" — on daily bars that would whipsaw a 2-year position out
  several times a year.
- **Rule 6 is −30%, wider than the reduce-level review at −25%.** A quality large cap can
  draw down 25% in a market-wide event without the thesis breaking.
- The current ×0.5 stop-loss and ×2.0 target are **deleted entirely.** Fixed multiples of
  entry price are not an exit strategy for a 2-year hold; they are placeholders. Nothing
  in V2 uses a fixed price target.

**Every exit writes `exit_reason` and a `TradeHistory` audit row** — long-term signals
currently have no audit trail at all, because `TradeHistory.trade` is an FK to
`ShortTermSignal`. That model change is specified in the companion document §3.3.

---

## 9. PORTFOLIO RULES

### 9.1 Concentration caps

| Constraint | Limit |
|---|---|
| Max per stock | 8% of LT allocation |
| Max per sector | 25% |
| **Max per promoter group** | **12%** |
| Max per correlation cluster (ρ ≥ 0.6, 250d) | 2 positions |
| Min positions | 12 |
| Max positions | 20 |

**The promoter-group cap is the single most important addition.** ADANIPORTS is
Infrastructure and ADANIENT is Diversified — different sectors, so a sector cap alone
would have permitted the current 100%-Adani book. Requires a `PromoterGroup` mapping table,
seeded from a curated list of the ~20 major Indian groups and refined from shareholding
filings.

### 9.2 Beta

Portfolio beta to Nifty 500, computed on 250-day returns, capped at **1.2**. On breach,
trim the highest-beta holding first. Reported on the dashboard whether or not it breaches.

### 9.3 Correlation

`shared/portfolio_risk.build_correlation_clusters` with `lookback=250d`, `ρ ≥ 0.6`.
**N_eff is reported on every screen** — `N_eff = n / (1 + (n−1)ρ̄)`. A 15-position book
with ρ̄ = 0.5 delivers N_eff ≈ 2.6. Making that visible is what stops "we hold 15 stocks"
being mistaken for diversification.

### 9.4 Diversification floor

Below 12 positions the engine may not add risk to existing holdings — it must either find
new names or hold cash. This prevents concentration by attrition as exits fire.

### 9.5 Sizing — inverse volatility, NOT risk parity, NOT Kelly

Three deliberate choices:

- **Not stop-based risk parity.** The short-term engine sizes as
  `(equity × risk%) / (entry − stop)`. Long-term has no meaningful entry stop — exits are
  fundamental and time-based — so there is no denominator.
- **Inverse-volatility weighting**, normalised, capped at the 8% per-stock limit:
  `w_i ∝ 1/σ_i`. Modulated by the §2.7 Risk Score so high-pledge, high-beta, high-earnings-
  variability names get less weight at equal composite.
- **Kelly is structurally unavailable and must not be implemented here.** At ~5–10
  positions per year, reaching the `kelly_fraction` gate of n = 300 takes 30+ years. The
  existing gate is correct; the correct action is to **not call it from this engine.**
  Volatility targeting needs only a volatility estimate, which is forecastable; Kelly needs
  an edge estimate, which at this sample size is not.

### 9.6 Structural regime

`shared/regime.py` with `horizon="structural"` — monthly bars, longer lookback, its own
cache TTL. Two states:

- `RISK_ON` / `NEUTRAL` — normal operation
- `RISK_OFF` — Nifty 500 below its 200-week average **and** breadth < 30%. New buys are
  suspended; existing holdings follow the normal exit rules. It is a **brake on additions,
  not a liquidation trigger** — forced selling into a structural drawdown is how long-term
  strategies realise their worst outcomes.

---

## 10. IMPLEMENTATION ROADMAP

Sequenced so that nothing is built on unavailable data.

| Phase | Work | Gate to proceed |
|---|---|---|
| **0 — Stop the bleeding** *(days)* | Reduce the live Adani book to one name with a manual stop. Delete the hardcoded `roe`/`debt_to_equity`/`profit_growth` constants and the `"Large Cap"` sector literal from every response. Mark existing LT rows as legacy. | No fabricated data reaches any surface |
| **1 — Data procurement** *(weeks, external)* | Select and contract a fundamental vendor. Build NSE/BSE shareholding + pledge ingestion. Define the `FundamentalProvider` port first so the engine is vendor-agnostic. | Point-in-time integrity confirmed; filing-lag rule enforceable |
| **2 — Shared layer** *(depends on companion doc)* | Consume `shared/` once promoted by the short-term V2 work. Add `LONG_TERM` profile, structural regime horizon, LT factor weights, promoter-group cap, generic `TradeHistory`. | Short-term V2 Phases 1–2 complete |
| **3 — Fundamentals subsystem** | `fundamentals/` package: port, adapters, normalisation, restatement handling, staleness gate, quality metrics (accruals, cash conversion). | Reconciles against 20 hand-checked companies across 3 sectors |
| **4 — Scoring + backtest** | `longterm_scoring.py`. Backtest the composite over ≥10 years with point-in-time data. Report decile spread, turnover, factor attribution, worst drawdown. | Decile 1 must outperform decile 10 out-of-sample. **If it does not, the model is wrong and must not ship.** |
| **5 — Portfolio engine** | Tranche tracking, inverse-vol weights, all caps, beta, N_eff reporting. | Constraint tests pass; caps provably never breached |
| **6 — Lifecycle + monitoring** | Four scheduled jobs, all eight exit rules, audit trail, Telegram with a dedicated LT channel and its own event types. | Shadow run across 2 quarterly cycles |
| **7 — Migration** | Retire legacy rows; delete `_fetch_long_term_quality`, `scan_long_term_stocks`, `get_pro_system_data`, `update_pro_system_outcomes`; remove the Telegram-formatter side effect. | Shadow output accepted |

### Critical path

Phase 1 is external and gates 3, 4, 5, 6, and 7. **Start vendor selection immediately** —
it is the long pole, and every other phase is idle without it.

### What must not happen

Do not build Phases 3–7 against the current hardcoded constants "to have something
working." That reproduces the exact defect being removed, and a backtest run against
constant ROE will look magnificent and mean nothing.

**Until Phase 1 completes, the correct state for the long-term engine is: disabled,
emitting nothing, with the two legacy positions manually managed.** An engine that emits
nothing is strictly better than one that emits picks ranked by 100-day price momentum
wearing the label "Top Sector Leader."

---

*Companion: `doc/SHORT_TERM_ENGINE_V2_ARCHITECTURE.md`. As-built reference:
`doc/long_term_stock.md`. Findings this document closes:
`doc/INSTITUTIONAL_AUDIT_PLATFORM.md` §5 (B2, B3, B6, B11, B12) and §4 (Quality, Value,
Growth, Institutional Flow, Beta).*
