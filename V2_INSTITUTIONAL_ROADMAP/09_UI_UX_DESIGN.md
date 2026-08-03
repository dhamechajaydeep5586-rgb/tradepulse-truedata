# 09 — UI/UX Design

## 1. Current Inventory (verified: 6 pages, 17 components, 1 context provider, 1 API client)

**Pages**: `Login`, `Register`, `Dashboard` (home), `PerformanceReports`, `ProSystem`, `GrowwPreview`.
**Components**: `AIInsightBox`, `CommoditySignalsTable`, `DeliveryChart`, `DeliveryTable`, `DeltaHedgePanel`, `DomesticMarketCard`, `FIIDIICard`, `GlobalMarketCard`, `LiveSignalsTable`, `MarketBiasSummary`, `NotificationBell`, `OIChangeChart`, `OptionChainTable`, `ProtectedRoute`, `SignalsTable`, `StrategyCompareCard`, `VolumeSpikeChart`, `VolumeSpikeTable`.

**Existing pattern worth keeping**: dark theme, Tailwind utility classes, status-badge color convention already partially consistent (emerald=pass/buy, red=reject/sell/loss, amber=warning/pending, gray=neutral/rejected, sky/indigo=info). Formalize this into a real token set (§2) rather than each component re-deriving similar-but-not-identical color strings (confirmed duplication: `GrowwPreview.jsx`'s `Badge` component and status-pill markup in `LiveSignalsTable.jsx`/`CommoditySignalsTable.jsx` implement visually-similar badges independently).

**No design-system/primitives layer** — confirmed in `01_PROJECT_ARCHITECTURE.md` §2.7. This document's Phase 8/9 UI work should extract shared primitives (`Badge`, `Card`, `StatTile`, `Button`) once, rather than continuing the current per-component pattern.

## 2. Design Tokens (formalize existing informal convention)

| Meaning | Color | Current usage evidence |
|---|---|---|
| Bullish / Buy / Pass / Win | Emerald | `LiveSignalsTable`, `GrowwPreview` `PASS` badge, Dashboard PRO/Reports links |
| Bearish / Sell / Reject / Loss | Red | Loss figures, `REJECTED`-adjacent contexts |
| Pending / Caution / Neutral-warning | Amber | `PENDING` badges, "Research only" banner on Groww preview |
| Neutral / Rejected (non-alarming) | Gray | `GrowwPreview` `REJECTED` badge (deliberately calmer than red — a rejection isn't a loss, correctly differentiated already) |
| Informational | Sky/Indigo | `OK` status, PRO branding accents |
| Error/System failure | Red, distinct usage from "loss" red — needs visual differentiation (e.g. red-with-icon vs. plain red text) so a network error and a losing trade are never visually confused at a glance |

## 3. Page-by-Page Design

### 3.1 Dashboard (home) — extend, don't replace
Current: Global markets, domestic markets, market bias, AI insight, live signals, commodity signals, delta hedge, FII/DII, option chain. **Add**: a compact Risk Dashboard summary tile (`02` §8 — portfolio heat, daily loss used, circuit breaker status) near the top, above the fold — this is the single most important new surface, since it replaces the previously-fictional risk numbers with real ones and the user should see it before anything else, every session.

### 3.2 Pro System — fix the fiction, then extend
The "Risk Management" panel (`ProSystem.jsx:372-384`) gets rewired to call `/api/risk/account/` and `/api/risk/dashboard/` (`08` §3.2) instead of local `useState`. The caption "Applied to calculate Position Size (qty) in the Active tab" becomes literally true instead of aspirational. Add a visible indicator when displayed `qty` was capped by a risk-engine limit (not just computed from capital) — e.g. a small badge "sized by risk engine" vs. "capped: sector limit" — so the user can see *why* a size was reduced, not just the final number.

### 3.3 New: Option Income page (replaces the conceptual gap where "Option Selling Sniper" was documented but never built)
One page, tabs per structure (Short Strangle / Iron Condor / CSP / Covered Call / Credit Spread), each showing candidates with the new POP/IV-Rank/OI columns from `03` §2. Structurally similar to `DeltaHedgePanel` (existing, keep as the Strangle tab's implementation) extended with the new structures as sibling tabs, not a separate rebuild.

### 3.4 New: Portfolio page (net new — nothing like this exists today)
Holdings table (from `05`'s new `Holding` model), sleeve allocation chart (target vs. actual, flagging drift per `05` §4), sector/correlation/beta summary, tax-lot aging view (`05` §7 — visually flag positions approaching the 1-year LTCG mark). This is the page that makes "portfolio management" a real, visible feature instead of an absent one.

### 3.5 New: Backtest page (net new)
Form to configure a `BacktestRun` (strategy, date range, regime tag preset from `07` §2.5's named windows), submitted async (per `08` §5 — queue-backed), results view showing equity curve, Sharpe/Sortino/Calmar, and **prominently, the sample-size/statistical-significance warning from `07` §3** if thresholds aren't met — this warning should be impossible to miss, not a small footnote, given it's the direct fix for the previous review's core finding.

### 3.6 Groww Preview — no change needed
Already well-designed for its scope (this session's own work) — default-hide-rejected pattern, clear research-only banner, universe-matching to the real pipeline. Cited here as the model other new pages (§3.3-3.5) should follow for "how to present system-generated candidates without overwhelming with noise."

### 3.7 Performance Reports — extend with regime tagging
Once `BacktestRun.regime_tag` (`08` §2) exists for historical simulation, live performance reports should support the same regime-filtered view — "how did we actually do during this month's high-VIX stretch" as a real, answerable question against live results, not just backtest results.

## 4. Alerts & Notifications

Existing: `NotificationBell` + `Notification` model (SUCCESS/ALERT/INFO), Telegram/WhatsApp delivery (existing, working, keep). **Add new notification types** tied to the risk engine: `CIRCUIT_BREAKER_TRIPPED` (critical, distinct visual treatment — this should be impossible to miss, likely a full-screen or persistent-banner treatment, not just a bell-icon badge, given what it means), `SECTOR_LIMIT_REACHED`, `MARGIN_WARNING` (approaching, not yet breached), `RISK_EVENT_BLOCKED` (informational — a signal was blocked, useful to know even though no action is needed). Route critical-severity notifications (circuit breaker) through Telegram/WhatsApp immediately, not just the in-app bell, since the whole point of a circuit breaker is to be noticed even if the user isn't looking at the dashboard at that moment.

## 5. Workflow: how a user should experience the new risk-aware system end to end

1. Open Dashboard → Risk Dashboard tile shows current heat/loss-used/circuit-breaker status immediately (§3.1).
2. Navigate to Option Income or Swing candidates → every candidate already reflects risk-engine-approved sizing, not a raw signal (§3.2, §3.3).
3. If a circuit breaker is armed-but-close (e.g. daily loss 80% of limit), a persistent (not dismissable-and-forgotten) banner communicates this before the user takes on more risk that session.
4. Trade review / portfolio review (from `06`) accessible from both Dashboard and Portfolio page, always labeled clearly as AI-generated analysis, never presented as if it were a system-computed deterministic number (visually distinguish AI-narrated content from `RiskEngine`-computed numbers — e.g. a small "AI analysis" tag — so the read-only boundary from `06` §2 is visible to the user, too, not just enforced in the backend).

## 6. What NOT to build

No mobile app, no real-time streaming charts beyond what already exists (the 1-second price ticker pattern is sufficient and already rate-limit-conscious per `CLAUDE.md`'s documented 403 history) — matching this project's actual scale (single/small user, retail account) rather than over-building for a scale that isn't the current reality.
