# 04 — Swing Trading Engine: Redesign

## 1. Current State — Correcting a Misconception From Earlier in This Engagement

**Important correction**: the live swing engine is `trade_engine.py`, not `pro_system_service.py`. Confirmed via `views.py:205`: `ProSystemView` (the endpoint the `/pro-system` frontend page actually calls for its main data) imports `get_dashboard_data` from `trade_engine.py`. `pro_system_service.py` contributes only the performance-report aggregation endpoint and is otherwise dead code (per `01_PROJECT_ARCHITECTURE.md` §2.1). Every finding below is about `trade_engine.py`, the file that's actually running.

### 1.1 The existing "AI Score" composite (`trade_engine.py:~253-292`)

```
ai_score = trend_score(0-25) + momentum_score(0-25*) + volume_score(0-20) + sector_score(0-15) + risk_score(0-15)
```

(*momentum combines ADX + RSI contributions, exact cap not confirmed at range boundary — verify in Phase 3.)

**Concrete finding**: `fundamental_score = 10.0  # Default neutral` — hardcoded, always exactly 10.0 regardless of the actual company. Despite the model (`TradeScanner.fundamental_score`) and the user's own requested scoring criteria (Revenue Growth, Profit Growth, ROE, ROCE, Debt, Cash Flow, Promoter Holding, FII/DII Holding) implying real fundamental analysis, **zero fundamental data is fetched or computed anywhere in this codebase.** The "AI Score" is a technical/momentum/volume score wearing a fundamental-sounding name.

**Second finding**: `delivery_percentage` is fetched by `bhavcopy_service.py` (confirmed real data exists, it's in the NSE bhavcopy) but is **never read by any scoring function** in `trade_engine.py` or elsewhere. It's on the `Stock`/`StockDailyData` models, populated, and unused. Free win: this is data already flowing into the system that isn't being used for anything.

**Third finding**: relative strength appears as `sector_score` (comparing a stock's return spread vs. its sector), but there's no dedicated Relative-Strength-vs-Nifty calculation independent of sector — the user's requested criteria list "Relative Strength" and "Sector Strength" as separate items; today they're conflated into one score.

**Fourth finding**: "Institutional Buying" (FII/DII activity, block deals) — `fii_dii_service.py` exists and is used elsewhere (`FIIDIICard.jsx`, `FIIDIIView`) for a market-wide daily FII/DII net figure, but is **never cross-referenced per-stock** in the swing scanner. The data pipeline exists; the join to individual stock scoring doesn't.

---

## 2. Redesigned Scoring System

### 2.1 Make every claimed input real, or remove it from the score

Two options, pick one per input, don't leave placeholders silently blended into a number that looks precise (a "72.4/100" score built partly from a hardcoded `10.0` is a false-precision problem — the composite looks more rigorous than it is):

| Input | Current state | Recommendation |
|---|---|---|
| Fundamentals (ROE/ROCE/Debt/Growth) | Hardcoded 10.0 | Source from a fundamentals data provider (screener.in has an unofficial API pattern similar to what this session already built for Groww; or a paid provider — Tijori/Trendlyne — if budget allows) or explicitly remove `fundamental_score` from the composite until real data exists. **Do not ship a "fundamental score" that isn't one.** |
| Delivery % | Fetched, unused | Wire into `volume_score` or a new `conviction_score` — high delivery % on a volume spike is a stronger institutional-conviction signal than volume alone; this is genuinely free, since the data pipeline already exists. |
| Relative Strength vs. Nifty | Conflated with sector_score | Split into two independent sub-scores: RS-vs-Nifty (already partially computed — `scan_short_term_stocks`'s dead code at `pro_system_service.py:340` compares `stock_20d_ret` vs `nifty_20d_ret`; this logic is *correct* and should be salvaged from the dead file, not rewritten) and RS-vs-sector. |
| FII/DII institutional buying | Market-wide only, no per-stock join | Requires per-stock delivery/block-deal data (NSE publishes bulk/block deal data separately) — net-new data source, medium effort. |
| Promoter/FII/DII Holding % | Not fetched anywhere | Requires shareholding-pattern data (quarterly, NSE/BSE corporate filings) — lowest-frequency-refresh input, fine to update monthly rather than daily. |

### 2.2 Composite score v2

```
score_0_100 = (
    technical_score * 0.30 +      # existing trend+momentum, salvaged as-is (it's sound)
    volume_conviction_score * 0.15 +  # volume + delivery% combined (new, cheap)
    relative_strength_score * 0.15 +  # split RS-vs-Nifty and RS-vs-sector, averaged
    fundamental_score * 0.25 +     # REAL data only, or 0 weight until it exists — never a fake placeholder
    institutional_score * 0.10 +    # FII/DII per-stock, once available
    risk_score * 0.05              # existing, keep (52-week-range-based, already sound)
)
```

Weights are a starting proposal, not a mandate — the point is the structure (every component traceable to real data) matters more than the exact split, and should be tuned once backtesting (`07`) can actually measure which components predict forward returns.

### 2.3 Confidence Score (distinct from the 0-100 score)

The 0-100 score answers "how good does this setup look." Confidence should answer "how much do we trust this specific score right now" — e.g., lower confidence when: fewer than N days of data for a recently-listed stock, wide bid-ask spread, thin volume relative to the stock's own recent average (as opposed to the absolute volume_score), or when data for one of the composite's inputs is stale/missing (this ties directly into `02_RISK_MANAGEMENT_ENGINE.md`'s "fail closed" principle — if fundamental data is missing for a stock, confidence should drop, not silently default to neutral).

---

## 3. Fundamental Scanner (net new)

Minimum viable version: Revenue growth (YoY, 3yr CAGR), Profit growth (YoY, 3yr CAGR), ROE, ROCE, Debt/Equity, Free Cash Flow trend, Promoter holding % (and *change* in promoter holding — a promoter *reducing* stake is a materially different signal than a stable high holding, and this distinction is more informative than the raw number alone). Source: quarterly-refresh batch job (this data doesn't change daily — no need to fetch it on every scan cycle, unlike price/volume).

## 4. Technical Scanner (exists, salvage + extend)

Keep: EMA spread (trend), ADX+RSI (momentum), ATR (already computed, used for stop distance). Add: MACD histogram (momentum confirmation, currently absent), a genuine breakout detector (distance from 52-week high combined with volume confirmation — today `risk_score` uses 52-week range only for risk classification, not as a breakout signal) and a pullback detector (price near a rising 20/50 EMA after a prior uptrend — the "EMA Bounce" pattern the user's framework explicitly names, currently unimplemented).

## 5. Sector Rotation Engine (net new)

Rank all NSE sectors by trailing 1-week/1-month relative return vs. Nifty, refreshed daily. Feed this ranking into `relative_strength_score` (§2.1) so a technically strong stock in a weak/rotating-out sector scores lower than the same technical setup in a rotating-in sector — sector rotation context is currently entirely absent; today's `sector_score` measures a stock vs. its own sector, never the sector's own strength vs. the market.

## 6. Breakout & Pullback Engines (net new, per §4)

Two independent signal generators feeding the same scoring composite, rather than the single monolithic `_analyze_short_term` function today (`pro_system_service.py:166`, dead code, but structurally worth learning from — even mid-file it already tries to distinguish setup types via the `setup` string field on `ShortTermSignal`). Splitting into named, independently-testable detector functions (breakout vs. pullback vs. reversal) makes backtesting each pattern's individual hit rate possible (`07_BACKTESTING_FRAMEWORK.md`) — currently impossible since they're blended into one score with no per-pattern attribution.

## 7. Portfolio Ranking & Output

Final output: top-N candidates ranked by composite score, each tagged with setup type (breakout/pullback/reversal), confidence, and — critically, tying back to `02_RISK_MANAGEMENT_ENGINE.md` — a **risk-engine-approved position size**, not a raw signal the frontend has to size itself (fixing the exact `ProSystem.jsx` disconnect found in the original review).

## 8. What to explicitly salvage from the "dead" `pro_system_service.py` rather than rewrite

Despite being unreachable code, `pro_system_service.py:340`'s relative-strength-vs-Nifty comparison and `scan_short_term_stocks`'s pre-filter funnel (change% + volume threshold → top-40 → RS filter → detailed analysis) is a reasonable funnel shape and shouldn't be thrown out just because it's currently dead — it should be the starting point for `trade_engine.py`'s equivalent logic once the two are unified under `engines/swing/` per `01_PROJECT_ARCHITECTURE.md` §3.2, keeping whichever implementation is more correct per a side-by-side diff (a Phase 3 task, not assumed here).
