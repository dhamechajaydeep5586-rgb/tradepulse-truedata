# Institutional Audit — TradePulse Intraday Engine

**Scope:** intraday buy/sell engine (`intraday_service.py` + supporting services).
**Method:** static audit of running code, verified against source — not against `CLAUDE.md`, which has drifted.

---

## 0. Two disclosures that bound everything below

**(a) There is no track record to audit.** `SignalHistory` currently holds **0 intraday rows**
(51 rows total, all other categories). This audit is therefore **structural, not empirical**. No
statement below is derived from your system's realized returns, because none exist.

**(b) Your own rule — "never recommend improvements unless statistically justified" — cannot
currently be satisfied by anything.** The measurement layer is biased in three independent ways
(§4.1, §8.1), so any statistic the system produces about itself is unreliable. **Building honest
measurement is therefore Priority 0**, ahead of every alpha idea in this document. Everything in
§1–§7 is a prior from published literature and standard practice, not a measurement of your
system.

Where the brief demands numeric deltas (Sharpe, win rate, drawdown), I give **ranges with
explicit error bars and label them as priors.** Point estimates would be fabrication. Two
recommendations (§0.1 and §6.2) are *arithmetic* rather than predictive — those hold regardless
of what your edge turns out to be, and are the only ones I would call statistically unconditional.

---

## 0.1 THE CRITICAL FINDING — transaction costs exceed the edge

This is the finding that dominates the audit. Everything else is secondary.

**Your stop distance is set to `0.8 × ATR(14)` on 5-minute bars, and your target is exactly 2R.**

Estimating 5-min ATR for a NIFTY100 name via square-root-of-time from a ~1.75% daily ATR:

```
σ_5min ≈ 1.75% / √75 ≈ 0.20% of price
R = 0.8 × 0.20% ≈ 0.16%
Target = 2R ≈ 0.32%
```

Realistic all-in round-trip friction for Indian intraday equity:

| Component | Cost |
|---|---|
| Brokerage (₹20/order, both legs) | 0.040% |
| STT (0.025%, sell side only) | 0.025% |
| Exchange transaction charges | 0.006% |
| Stamp duty (buy side) | 0.003% |
| SEBI turnover | 0.0002% |
| GST (18% on brokerage + txn) | 0.008% |
| **Statutory subtotal** | **≈ 0.082%** |
| Bid-ask crossing (both legs) | 0.030 – 0.060% |
| Slippage / impact (retail size) | 0.010 – 0.030% |
| **Realistic total `f`** | **0.12% – 0.17%** |

### The break-even math

Gross break-even win rate at 2:1 reward:risk is `1/(1+b) = 1/3 = 33.3%`.

Applying friction `f` to **both** winners and losers:

```
Net win  = 2R − f
Net loss = R + f
Break-even win rate  p* = (R + f) / (3R)
```

| Scenario | R | f | Required win rate |
|---|---|---|---|
| Theoretical (no costs) | — | 0 | **33.3%** |
| Typical stock, mid friction | 0.16% | 0.14% | **62.5%** |
| Tight-ATR stock, high friction | 0.12% | 0.17% | **≈ 72%** |
| Wide stop (VAL far, 0.60%) | 0.60% | 0.14% | **41.1%** |

**A 5-minute volume-profile system does not sustain a 62–72% hit rate.** Nothing in the published
literature on intraday breakout or value-area mean reversion supports hit rates in that band net
of costs. As currently parameterised, **expected value is negative with high confidence**, and no
improvement to entry logic can rescue it — the arithmetic is upstream of the signal.

### Why this is the most important row in the table

Note the last scenario. When `R` is wide (0.60%), required win rate collapses to a very
achievable 41%. **Your system's viability is almost entirely a function of `R`, and `R` is
currently uncontrolled** — it floats with ATR and VAL distance, with no floor. Some signals are
mathematically unrunnable; others are fine. You are trading both, identically sized.

**Fix (arithmetic, unconditional):** reject any signal where `target_distance < 3 × f`. At
f = 0.14%, that is a minimum target of 0.42%, i.e. minimum R of 0.21%. This does not predict
anything — it removes trades that cannot pay for themselves. Expect it to cut trade count 40–60%
and move expectancy from negative to approximately break-even *before* any alpha improvement.

- **Priority: CRITICAL.** Complexity: ~2 hours.
- Sharpe: undefined → positive territory (cannot quantify from a negative base).
- Win rate: +0 (unchanged per trade; the *population* shifts to viable trades).
- Max DD: −30% to −50%, purely from eliminating guaranteed-loss churn.

---

## 1. Universe Selection

**Current:** NIFTY100, fetched live from NSE archive CSV, cached 24h.

### Why the current choice is weak
The universe is selected by **index membership**, which is a proxy for market cap, not for
tradability. The variable that actually determines whether an intraday strategy is runnable is
**spread relative to target**, and index membership only loosely correlates with it.

### What professional firms use
A **liquidity- and cost-filtered universe**, rebuilt daily, with hard gates:

| Gate | Threshold (suggested for INR intraday) |
|---|---|
| 20-day median traded value | ≥ ₹50 crore |
| 20-day median quoted spread | ≤ 5 bps |
| Price floor | ≥ ₹100 (avoids tick-size quantisation — a ₹0.05 tick on a ₹60 stock is 8 bps) |
| F&O ban period | excluded |
| Earnings / result date | excluded (T−1 to T+1) |
| Corporate action (ex-div, split, bonus) | excluded |

### The decisive argument
**If the spread is 30 bps and your target is 32 bps, the strategy is unrunnable at any signal
quality.** The NIFTY500 tail contains names with 50–150 bps effective spreads. NIFTY500 is
therefore not a candidate universe for this strategy — it would silently poison the sample with
trades that are arithmetically dead on arrival, and worse, they'd *backtest fine* because your
backtester has no cost model (§8.1).

### Recommendation
**Liquidity-filtered NIFTY200** (expect ~120–150 survivors after gates) — not NIFTY100, not
NIFTY500.

- Rationale for widening 100 → 200: cross-sectional strategies need **breadth of candidates** to
  make ranking meaningful (§6). Selecting the top 5 from 100 names is a 95th-percentile cut;
  from 200 it is a 97.5th-percentile cut. Higher selectivity at identical trade count is free
  edge *if* the ranking has any signal.
- Rationale for not going to 500: the added 300 names are where spread cost exceeds target.
- **The rate-limit objection is real but solvable** — see §9.3. Your current bulk-quote batching
  (50/call) already handles 200 names in 4 calls.

| Metric | Estimate | Confidence |
|---|---|---|
| Sharpe | +0.05 to +0.15 | Low — depends entirely on ranking quality existing |
| Win rate | +1 to +3 pp | Low |
| Max DD | −5 to −10% | Medium (from event/ban exclusions specifically) |
| Priority | **HIGH** | Complexity: ~1 day |

**Drawback:** doubling the universe doubles candle-API load. The event-exclusion feed (earnings
calendar, F&O ban list) is an additional data dependency you do not currently have.

---

## 2. Market Regime

**Current:** `is_sideways_market()` — price within 0.15% of VWAP **and** day range < 1% →
`SIDEWAYS`, else `TRENDING`.

### Three defects, in order of severity

**2.1 — The regime output is dead code.** `get_standard_market_state()` returns only
`SIDEWAYS`/`TRENDING`/`UNKNOWN`. The only consumer, Trigger 3 in `_volume_profile_logic`, tests
`nifty_trend != "BEARISH"` and `!= "BULLISH"` — **strings that are never produced.** The gate is
a tautology; it has never blocked a single trade. You have regime detection code and zero regime
gating.

**2.2 — The VWAP is not a session VWAP.** `compute_vwap()` is
`(typical × volume).cumsum() / volume.cumsum()` over the entire dataframe, and the NIFTY frame is
a **2-day** lookback. So "distance from VWAP" is distance from a 2-day cumulative average, which
is heavily anchored by yesterday and barely moves intraday. The 0.15% threshold against a sticky
2-day mean is close to meaningless. Institutional VWAP is **anchored to session open** and resets
daily.

**2.3 — Two binary thresholds is not a regime model.** It collapses a continuous state space into
one bit, discards magnitude, and has no volatility axis at all.

### Why institutions rarely trade this way
Regime is treated as a **latent state with persistence**, estimated continuously, because the
*same signal has opposite expected value in different regimes*. A binary flag computed from two
hard thresholds has no hysteresis and will flip-flop at the boundary, producing exactly the
whipsaw behaviour it was meant to prevent.

### The design that matters most

The single highest-value structural change in this entire audit (after §0.1) is
**regime-conditional trigger enablement**, because your three triggers are of two opposite types:

| Trigger | Type | Profitable regime |
|---|---|---|
| POC Flip | Momentum / continuation | Trend, expanding vol |
| VA Breakout | Momentum / continuation | Trend, expanding vol |
| VA Rejection | **Mean reversion** | **Range, contracting vol** |

**You fire all three unconditionally.** In any given regime, roughly half your signal population
is structurally counter-regime.

#### The math for why this dominates entry-quality work

Suppose breakout triggers have `EV_trend = +0.15R` and `EV_range = −0.25R`, and the tape is
range-bound 60% of the time (typical for Indian large caps):

```
Unconditional EV = 0.40(+0.15R) + 0.60(−0.25R) = −0.09R      ← negative
Regime-gated  EV = +0.15R, traded on 40% of days             ← positive
```

Regime gating converts a negative-expectancy system to positive **without improving prediction at
all**, purely by not taking the trades you already know are bad. Trade count falls ~60%; per-trade
EV goes from −0.09R to +0.15R. This is why professional effort goes into regime before entries.

### Recommended model — 2-axis regime grid

Deliberately simple, because complexity here is not rewarded and is hard to validate:

**Axis 1 — Trend strength**
- `ADX(14)` on NIFTY 15-min (you already have `compute_adx`)
- Distance of NIFTY from **session-anchored** VWAP, normalised by ATR: `(P − aVWAP) / ATR`
- Breadth: NIFTY500 advance/decline ratio; % of universe above session VWAP

**Axis 2 — Volatility state**
- India VIX **level** and its 5-day change
- Realized vol ratio `RV(5d) / RV(20d)` — this is your ATR-expansion measure
- Intraday range vs 20-day average range

**Composite:** z-score each input against a 60-day rolling window, average within axis, then map
to a 3×3 grid (trend: down/neutral/up × vol: contracting/normal/expanding).

**Add hysteresis:** require the state to persist 2 consecutive reads before switching. This alone
removes most boundary whipsaw.

**Relative strength / sector rotation** belongs in the *ranking* layer (§6), not the regime layer
— it is cross-sectional, not a market state.

| Metric | Estimate | Confidence |
|---|---|---|
| Sharpe | +0.25 to +0.50 | **Medium-high** — regime conditioning is among the best-replicated results in the literature |
| Win rate | +5 to +10 pp | Medium |
| Max DD | −20 to −35% | Medium-high (removes the losing-regime clusters that create drawdown runs) |
| Priority | **CRITICAL** | Complexity: 3–5 days |

**Drawbacks:** cuts trade frequency 40–60%, which lengthens the time needed to reach statistical
significance (§8.3). Regime models are themselves fit to history and can break. India VIX is an
additional data dependency.

---

## 3. Entry Logic

### 3.1 Audit of current entries — defects ranked

**(a) Signals repaint on partial bars — CRITICAL.**
`df.iloc[-1]` from a live 5-min candle fetch is an **incomplete, still-forming bar.** Trigger 3
tests `current_candle["Close"] > current_candle["Open"]` on that partial bar. A signal can appear
at minute 2 of the bar and vanish by minute 5. Consequences:
- Any backtest on completed bars is testing a different system than the one running live.
- `vol_ratio` = partial-bar volume ÷ 10-bar average is **structurally biased low** early in the
  bar and rises monotonically through it. Your volume thresholds (1.2 / 1.5 / 1.1) are therefore
  not volume filters — they are **elapsed-time-within-bar filters.** Signals systematically fire
  in the last ~40% of each bar, for reasons unrelated to conviction.

**Fix:** evaluate on `df.iloc[-2]` (last *closed* bar) only. Non-negotiable for any measurable
system.

**(b) Volume profile is computed on a 2-day composite, then used as intraday levels.**
40 bins across two sessions blends yesterday's and today's distributions. Composite profiles are
legitimate — but as *reference* levels, not as intraday breakout triggers. The standard
construction is either today's **developing** profile or **prior-day** profile used as fixed S/R.
Yours is neither, and the VAH/VAL it produces will sit at levels that had meaning yesterday.

**(c) Trigger priority is arbitrary.** The `elif` chain hard-codes POC Flip > VA Breakout > VA
Rejection. This ordering is asserted, never validated. Because the chain is exclusive, **only one
candidate per symbol ever exists** — which makes the next defect possible.

**(d) The scoring system is vestigial.** `score` (4.5 / 4.0 / 3.5) is computed and stored, then
used as `max(candidates, key=score)` over a **single-element list.** It is a no-op. Scores never
compete across symbols.

**(e) Signal selection is alphabetical, not meritocratic — HIGH severity, trivial fix.**
The scan loop iterates `scan_symbols` in NSE CSV order and `break`s at `MAX_SIGNALS_PER_SCAN = 5`.
**You do not take the best 5 signals — you take the first 5 in list order.** Every symbol
alphabetically after the 5th trigger is silently discarded regardless of quality. If `score` has
*any* predictive power, sorting before truncation captures it at zero cost. If it has none, the
change is neutral. **This is the highest return-on-effort change in the codebase (~30 minutes).**

**(f) No relative-strength, sector, spread, or event filter** at entry.

**(g) Entry price assumes a market fill.** `entry = current price`, and exits are recorded at
*exactly* target/stop. No spread crossing, no slippage. See §8.1.

### 3.2 Institutional alternatives, ranked by strength of evidence

| Rank | Method | Evidence | Feasible on your data? |
|---|---|---|---|
| 1 | **Cross-sectional relative strength** | Strongest replicated equity anomaly (Jegadeesh–Titman and 30 yrs of follow-up). Works intraday vs sector/index. | ✅ Yes |
| 2 | **Session-anchored VWAP** (reclaim / bounce) | Institutions are *benchmarked* to VWAP, creating genuine reflexive support | ✅ Yes |
| 3 | **Volatility compression → expansion** (NR7, squeeze) | Vol clustering is the most robust stylised fact in finance (GARCH). Excellent asymmetry: small stop, large potential R — **directly addresses §0.1** | ✅ Yes |
| 4 | **Opening range breakout, vol-normalised** | Documented, but heavily arbitraged; needs an RS filter to survive | ✅ Yes |
| 5 | **Liquidity sweep / stop-run reversal** | Real microstructure effect | ⚠️ Marginal — needs L2 depth |
| 6 | **Market structure shift / order block** | Largely discretionary; weak published evidence | ⚠️ Hard to specify testably |
| 7 | **Cumulative delta / volume delta / footprint** | Real at institutional data tiers | ❌ **No — see below** |

#### Be honest about order flow: you cannot build it on this data feed

Genuine cumulative delta requires **tick-by-tick trades classified against the prevailing
bid/ask**. Angel One's retail WebSocket delivers throttled LTP snapshots, not a full-depth
timestamped trade feed. Applying Lee-Ready tick-rule classification to throttled snapshots
produces a series whose sign is dominated by sampling artefacts, not by aggressor direction.

**Footprint, order blocks, and cumulative delta on this feed would be noise wearing an
institutional costume.** Do not build them. This is the item on your list I would most strongly
advise against — it is expensive, looks sophisticated, and would be measuring nothing.

### 3.3 The combination that statistically outperforms

Layer as **multiplicative filters**, not as an OR of triggers (your current design ORs three
triggers, which *maximises* false positives — the union of three noisy conditions is noisier than
any one of them):

```
Regime gate  (§2)          → is this trigger type valid today at all?
    × RS rank              → is this stock leading or lagging its sector?
    × Volatility compression → is R small relative to the expansion potential?
    × Liquidity/spread gate → can this trade pay for itself? (§0.1)
    → session-anchored VWAP used for entry *execution*, not signal generation
```

Rationale for that specific stack: compression sets up a **favourable R** (attacking §0.1
directly), RS supplies **directional edge**, regime supplies **conditional validity**, and the
liquidity gate supplies **arithmetic survivability**.

| Metric | Estimate | Confidence |
|---|---|---|
| Sharpe | +0.20 to +0.45 | Medium (RS component is high-confidence; the rest is medium) |
| Win rate | +3 to +8 pp | Low-medium |
| Max DD | −10 to −20% | Medium |
| Priority | **HIGH** (repaint fix = CRITICAL; sort-before-cap = CRITICAL/trivial) | Complexity: repaint 2h · sort 30min · full stack 1–2 weeks |

---

## 4. Exit Logic

**Current:** fixed 2R target, fixed stop at `min(VAL, entry − 0.8·ATR)`, hard close 3:20 PM. No
trail, no partial, no time stop, no break-even.

### 4.1 The exit *measurement* defect — CRITICAL

Exits are audited by `update_signal_outcomes()`, which runs on the periodic scanner:
**every 15 minutes, 11:00–15:15 only**, and compares a **single LTP snapshot** against target and
stop. It never sees bar high/low.

With a stop distance of ~0.16%, a stock can cross the stop and revert several times inside one
15-minute polling gap. Therefore:

1. **Intrabar touches are invisible.** A trade that hit target and reversed is never recorded as
   `HIT_TARGET`.
2. **Recorded exit prices are fiction.** `exit_price` is set to *exactly* `target` or
   `stop_loss` — the ideal fill, at a moment the system did not actually observe.
3. **Realized slippage is unbounded and unmeasured.** Detection happens up to 15 minutes after
   the level was breached, at whatever price the poll returns.
4. **No signals exist before 11:00 AM** — the unattended scanner does not run in the 9:15–11:00
   window, which is the highest-volume, highest-opportunity portion of the Indian session.

**Any win rate or P&L this system reports is not measuring the strategy.** Fixing this is a
prerequisite for §0's "statistically justified" standard.

**Fix:** audit exits against the **high/low of completed 1-min bars** since the last check, not a
point-in-time LTP. This is exact for stop/target detection at 1-min granularity and costs one
extra candle fetch per open position.

### 4.2 Exit mechanism audit

| Mechanism | Verdict for your system |
|---|---|
| **Fixed stop** | ✅ Structurally sound — anchoring to `min(VAL, ATR)` (the *wider* of the two) is genuinely good practice, better than a pure ATR stop |
| **Fixed 2R target** | ⚠️ Arbitrary. Intraday return distributions are fat-tailed; a fixed multiple forfeits the tail that pays for the losers |
| **Time stop** | ❌ **Absent — highest-value missing exit.** Intraday momentum decays fast; a signal that hasn't worked in 6–10 bars has EV ≈ 0 or below. Costs nothing to add, frees capital, cuts exposure |
| **Trailing stop** | ❌ Absent. A Chandelier (`high − k·ATR`) or structural trail captures the fat tail the fixed target forfeits |
| **Break-even stop** | ❌ Absent. Reduces DD; *lowers* win rate (more scratches). Net Sharpe effect usually mildly positive |
| **Partial / scale-out** | ❌ Absent — **and I recommend against it for you right now**, see below |
| **VWAP exit** | ❌ Absent. Reasonable for mean-reversion trades: exit VA Rejection longs at session VWAP |
| **3:20 PM hard close** | ✅ Correct and correctly implemented |

### 4.3 Where standard best practice is wrong for you

**Partial scale-out is conventional advice that your cost structure cannot afford.** Splitting an
exit into two fills **doubles the brokerage leg and adds a second spread crossing** — roughly
+0.04% to +0.07% per trade. Against a net win of ~0.18% (§0.1), scaling out consumes 20–40% of
expectancy to buy a psychological benefit.

**Do not add partial exits until minimum-R is enforced (§0.1) and R ≥ ~0.5%.** At that point the
cost is proportionally small and scaling becomes net-positive.

This is exactly the kind of recommendation that would be wrong if copied from a US-equity or
futures playbook where per-trade costs are an order of magnitude lower.

### 4.4 Recommended exit stack

1. Keep structural stop `min(VAL, entry − 0.8·ATR)` — but **enforce a minimum R** (§0.1)
2. **Add a time stop: 8 bars (40 min) with no progress → exit at market**
3. Replace fixed 2R with: **fixed 1.5R target on 100% for mean-reversion trades** (VA Rejection),
   **trailing Chandelier stop for momentum trades** (POC Flip, VA Breakout) — match exit style to
   signal type
4. Break-even stop after +1R **only for momentum trades**
5. Audit exits on 1-min bar high/low (§4.1)

| Metric | Estimate | Confidence |
|---|---|---|
| Sharpe | +0.15 to +0.35 | Medium (time stop is the high-confidence component) |
| Win rate | −2 to +4 pp | Low — trailing *lowers* win rate while raising expectancy |
| Max DD | −15 to −25% | Medium-high |
| Priority | **CRITICAL** (§4.1 measurement) / **HIGH** (time stop) | Complexity: 4.1 = 1 day · time stop = 2h · full stack = 3 days |

---

## 5. Risk Engine

**Current for intraday: none.** No backend sizing exists. The frontend computes
`qty = floor(capital × leverage / entry)` — **equal notional per position.**

(Note: `pro_system_service.py` *does* implement risk-based sizing —
`qty = max_risk_inr / sl_points`. The correct pattern already exists in your codebase; intraday
simply doesn't use it.)

### 5.1 Why equal-notional sizing is the second-worst defect in the system

Because `R` varies per trade (§0.1) but rupee exposure is constant, **risk per position varies by
the ratio of stop distances.** A signal with a 0.10% stop and one with a 0.60% stop receive
identical capital and therefore carry **6× different risk.** Portfolio P&L is then dominated by
whichever trades happened to have wide stops — i.e. by noise, not by conviction.

**Fix — risk parity at trade level.** This is table stakes, not an optimisation:

```
qty = (equity × risk_per_trade) / (entry − stop)
```

with `risk_per_trade` ≈ 0.25%–0.50% of equity for a 5-position intraday book.

### 5.2 Should sizing depend on Kelly? — No. Use fractional Kelly or vol targeting.

Kelly: `f* = (p(b+1) − 1) / b`. With `p = 0.45, b = 2` → `f* = 0.175` (17.5% of capital per trade).

**Why full Kelly is dangerous here — the estimation-error argument:**

```
SE(p) with n = 100 trades = √(0.45 × 0.55 / 100) = 0.050

p = 0.40  →  f* = 0.100
p = 0.45  →  f* = 0.175
p = 0.50  →  f* = 0.250
```

A ±1 SE error in `p` moves optimal leverage by **2.5×**. Kelly's growth curve is steeply
asymmetric — overbetting past `2f*` produces **negative** log growth even with a genuinely
positive edge. With no track record (§0), your `p` has effectively infinite standard error.

**Use ¼-Kelly at most, and only after ≥300 recorded trades.** Institutions overwhelmingly prefer
**volatility targeting** instead: scale positions so portfolio vol hits a constant target.

### 5.3 Correlation — your "5 positions" are ~1.5 independent bets

No correlation or sector constraint exists. NIFTY100 is ~35% financials, and intraday large-cap
correlations in a trending tape run ρ ≈ 0.5–0.7.

Effective number of independent bets:

```
N_eff = n / (1 + (n−1)ρ)
      = 5 / (1 + 4 × 0.6)
      = 1.47
```

Portfolio vol relative to a single position:

```
σ_p/σ = √((1 + (n−1)ρ)/n) = √(3.4/5) = 0.825
```

**You believe you hold a 5-position diversified book; you hold roughly 1.5 independent bets and
capture 17.5% vol reduction instead of the 55% that 5 uncorrelated positions would give.** On a
trend day, all five signals are the same trade in five costumes — and drawdowns arrive
simultaneously.

### 5.4 Recommended risk engine

| Layer | Rule |
|---|---|
| Trade | `qty = (equity × 0.35%) / (entry − stop)` |
| Position cap | ≤ 15% of equity notional in any one name |
| Sector cap | ≤ 30% of gross in any one sector |
| Correlation cap | cluster the universe on 20-day return correlation; ≤ 2 positions per cluster |
| Gross exposure | ≤ 3× equity intraday |
| Portfolio vol target | scale all sizes by `target_vol / realized_vol(20d)` |
| Regime scalar | 0.5× size in adverse regime, 1.0× neutral, 1.25× favourable |
| Daily loss limit | −2% of equity → flatten and stop trading for the day |
| Beta | net portfolio beta within ±0.3 (optional; matters more for overnight) |

| Metric | Estimate | Confidence |
|---|---|---|
| Sharpe | +0.30 to +0.60 | **High** — risk normalisation is the most reliable Sharpe improvement available; it raises Sharpe by reducing return variance without touching the signal |
| Win rate | +0 pp | **High** — sizing does not change hit rate, by construction |
| Max DD | **−30 to −45%** | **High** — the single largest drawdown lever in this document |
| Priority | **CRITICAL** | Complexity: trade-level 4h · full stack 1 week |

**Drawback:** the daily loss limit will occasionally stop you out immediately before a recovery.
This is a deliberate, accepted cost of survival.

---

## 6. Ranking Engine

**Current:** vestigial (§3.1d, §3.1e). Signals are emitted in alphabetical order, capped at 5.

### 6.1 Design principle — rank cross-sectionally, not against absolute thresholds

Absolute thresholds (e.g. `vol_ratio > 1.5`) don't adapt: on a quiet day nothing qualifies; on a
volatile day everything does. **Z-score each factor within the day's candidate set, then take
top-K.** This makes selectivity automatically adaptive to the opportunity set.

### 6.2 The free win — sort before truncating

Independent of any new scoring model: **sort candidates by score, then apply the cap.** Currently
`break`-at-5 discards everything alphabetically downstream regardless of quality. If the score
carries signal, sorting captures it; if not, it's neutral. **Strictly non-negative expected value,
~30 minutes of work.** Like §0.1, this is arithmetic, not prediction.

### 6.3 Proposed 0–100 composite

| Factor | Weight | Measure |
|---|---|---|
| Relative strength | 20 | Stock return vs sector index, 5-day + intraday, z-scored |
| Market alignment | 15 | Signal direction vs regime state (§2) |
| Reward/risk | 15 | `target_distance / f` — **directly encodes §0.1** |
| Volume confirmation | 12 | `vol_ratio` on *closed* bars, z-scored cross-sectionally |
| Trend quality | 12 | ADX + EMA stack alignment on the stock |
| Sector strength | 10 | Sector index RS rank |
| Liquidity | 8 | ADV and spread percentile |
| Volatility fit | 8 | ATR percentile — penalise both extremes |

**Threshold:** emit only `score ≥ 65` **and** take at most top-5 by score. On days when nothing
clears 65, **emit nothing.** The willingness to produce zero signals is a defining feature of
professional systems and the opposite of your current `relaxed=True` fallback.

### 6.4 Remove the relaxed-mode fallback

`relaxed=True` **lowers** volume thresholds when no signal has fired in 2 hours. This inverts the
correct logic: a quiet tape is evidence that conditions are poor, and the system responds by
loosening standards specifically at the moment it should tighten them. It is an
engagement-maximising heuristic, not an EV-maximising one — it manufactures trades to keep the UI
populated.

**Expect `relaxed` signals to have materially negative expectancy.** Instrument them separately
before deleting, so the removal is evidence-based — but I expect the evidence to be one-sided.

| Metric | Estimate | Confidence |
|---|---|---|
| Sharpe | +0.20 to +0.40 | Medium |
| Win rate | +4 to +9 pp | Medium (selectivity mechanically raises hit rate) |
| Max DD | −10 to −20% | Medium |
| Priority | **HIGH** (§6.2 is CRITICAL and trivial) | Complexity: 6.2 = 30min · full model = 3–5 days |

---

## 7. False Signal Reduction ≥ 40%

Achievable, and mostly without sacrificing good opportunities — because most of the reduction
comes from removing trades that are **arithmetically** or **structurally** invalid, not from
tightening prediction.

| # | Filter | Est. FP reduction | Good-trade loss | Justification |
|---|---|---|---|---|
| 1 | Confirmed-bar only (kill repaint, §3.1a) | 15–25% | ~0% | Removes phantom signals that never existed on closed data |
| 2 | Regime gate (§2) | 30–40% | ~10% | Removes structurally counter-regime trigger types |
| 3 | Minimum R ≥ 3×f (§0.1) | 20–30% | ~0% | Pure arithmetic — these trades cannot profit |
| 4 | RS filter (long only leaders, short only laggards) | 15–20% | ~8% | Strongest replicated equity anomaly |
| 5 | Spread/liquidity gate (§1) | 5–10% | ~0% | Arithmetic |
| 6 | Event blackout (earnings, F&O ban) | 3–7% | ~3% | Removes unmodelled jump risk |
| 7 | Delete `relaxed` mode (§6.4) | 5–10% | ~0% | Negative-EV by construction |

Filters 1, 3, 5, and 7 have **near-zero good-trade cost** — they are free. Composed (they overlap,
so this is not additive), expect **50–65% total signal reduction** with roughly 15–20% loss of
genuinely good trades. **Net precision improvement is large and the target is comfortably met.**

- **Priority: CRITICAL** (filters 1, 3, 7) / **HIGH** (2, 4, 5, 6)
- Sharpe: +0.35 to +0.70 combined · Win rate: +8 to +15 pp · Max DD: −25 to −40%
- Confidence: **medium-high**, because the majority of the effect is arithmetic rather than predictive.
- **Drawback:** trade count falls sharply → longer time to statistical significance (§8.3).

---

## 8. Backtesting

### 8.1 Audit of `run_backtest_for_signal()` — three disqualifying defects

**(a) Optimism bias — target is always checked before stop.**
```python
if signal_type == "BUY":
    if high >= target:  ...   # checked FIRST
    if low <= stop_loss: ...  # only if target missed
```
When a bar's range contains **both** target and stop, the code always books a **win**. Real
intrabar path is unknown. This systematically inflates win rate — and it inflates it *most* for
wide-range bars, which are exactly the volatile bars where the true outcome is most uncertain.
**Standard practice is the pessimistic assumption: if both are inside the bar, book the stop.**

**(b) Off-by-one cancels signals before they can trigger.**
With `pending_max_candles = 2`, at `idx == 2` the check `if idx >= rules.pending_max_candles`
fires **before** activation is evaluated on that bar. A pending signal effectively gets **one**
bar to trigger, not two.

**(c) No costs, no slippage, no portfolio.** It replays **one signal** against candles supplied
in the POST body. There is no equity curve, no concurrent positions, no capital constraint, no
correlation, no transaction costs. It cannot produce a Sharpe ratio or a drawdown.

**This is a signal replayer, not a backtester.** Combined with §4.1, the system has no honest
feedback loop at all.

### 8.2 Required framework

| Component | Specification |
|---|---|
| Engine | Event-driven, bar-by-bar, **portfolio-level** with shared capital and position limits |
| Intrabar | **Pessimistic**: both levels in one bar → assume stop. Optionally resolve with 1-min data |
| Costs | Explicit Indian model (§0.1 table), applied per leg |
| Slippage | `f(spread, order_size / ADV)`; minimum half-spread on every fill |
| Data hygiene | Survivorship-bias-free universe (**your live NSE CSV fetch gives *today's* NIFTY100 — backtesting on it is survivorship-biased by construction**); adjust for splits/bonuses |
| Validation | Walk-forward: rolling train/test, never a single in-sample fit |
| Cross-validation | **Purged K-fold with embargo** (López de Prado) — standard K-fold leaks when labels span overlapping bars |
| Monte Carlo | Bootstrap trade sequence → *distribution* of max DD, not a point estimate |
| Multiple testing | **Deflated Sharpe Ratio** (Bailey & López de Prado) — essential once you try many parameter sets |
| Parameter stability | Select a **plateau**, not a peak. A parameter optimum surrounded by cliffs is overfit |
| Regime testing | Report performance separately per regime (§2) — a strategy profitable only in one regime must be sized by regime |

### 8.3 Sample size — how much data before belief is justified

To distinguish a true Sharpe of 0.5 from 0 at 95% confidence, the standard error of an estimated
Sharpe over `T` years is approximately `√((1 + SR²/2)/T)`:

```
T = 4 years  →  SE ≈ 0.53   (cannot distinguish 0.5 from 0)
T = 8 years  →  SE ≈ 0.37
T = 16 years →  SE ≈ 0.26   (roughly 2 SE — marginally conclusive)
```

In trade terms, at ~5 signals/day × 250 days ≈ 1,250 trades/year, **a weak edge needs 1–2 years
of trades — and after §7's filtering cuts volume 50–65%, 2–4 years.**

**Practical consequence:** you will not statistically validate a Sharpe-0.5 strategy from forward
trading in any reasonable timeframe. This is precisely why a rigorous historical backtester is
not optional — and why the Deflated Sharpe correction matters, since you will inevitably test
many variants against the same history.

- **Priority: CRITICAL** · Complexity: 2–3 weeks
- Sharpe: no direct effect — **but it is the precondition for validating every other item here**
- Confidence: high that the current framework produces misleading results

---

## 9. How Jane Street / Citadel / Renaissance / Two Sigma Would Build This

### 9.1 The differences that actually matter

**Jane Street** — principally a market maker and ETF arbitrageur; would not build a directional
5-minute breakout scanner at all. *Transferable idea:* they price **everything** in expected value
net of explicit costs, before considering the signal. Your §0.1 problem would have been caught on
day one, because cost accounting is the *first* step in their process, not an afterthought.

**Citadel** — multi-PM under strict centralized risk. Every strategy is measured by **P&L
attribution**: how much came from alpha vs. from market beta vs. from sector tilt? *Transferable
idea:* your engine cannot currently answer "is this edge, or is this just long exposure on an up
day?" — on a trend day, five correlated longs (§5.3) will look brilliant for reasons that have
nothing to do with volume profile.

**Renaissance** — very large feature spaces, short holding periods, ruthless cross-validation, and
execution modelling treated as core research. *Transferable idea:* they win by aggregating
**thousands of weak, weakly-correlated bets.** Your 5 highly-correlated bets/day is the opposite
structure. Given `N_eff ≈ 1.47`, the fastest Sharpe improvement is **decorrelation, not stronger
signals** — Sharpe scales with `√N_eff`.

**Two Sigma** — engineering-first: versioned data, reproducible research pipelines, rigorous
separation between research and production. *Transferable idea:* the research infrastructure *is*
the product. Notably, **your software engineering is genuinely closer to this standard than your
trading logic is** (see §10).

### 9.2 The single honest generalisation

None of these firms would operate a system in which the recorded exit price is *assumed* to equal
the stop level (§4.1), or in which the backtester resolves ambiguous bars in its own favour
(§8.1a). **The gap between retail and institutional is mostly measurement honesty, not exotic
alpha.** Most of this audit is about measurement, and that is not a coincidence.

### 9.3 On your rate-limit constraint

Angel One REST limits are a genuine architectural constraint, but the current design amplifies
them: a synchronous per-symbol candle fetch inside the scan loop. Standard fixes:
- **Persist candles locally** (you already cache; write to DB and fetch only deltas) — a scan
  should hit the network for *new* bars only, not full history per symbol per cycle
- **WebSocket-first bar construction** — build 1/5-min bars from the tick stream instead of
  polling REST for history you already received
- Async/batched fetches with a token-bucket limiter

This decouples universe size (§1) from rate-limit pressure and removes the reason NIFTY500 → 100
was necessary in the first place.

---

## 10. Final Score

Scored as a **trading system**, not as a Django application. The distinction matters:

| Dimension | Score | Rationale |
|---|---|---|
| **Architecture (software)** | **7.0 / 10** | Genuinely good: clean service boundaries, caching, idempotency routing, stale guards, rate-limit batching, overlap locks, informative comments. Above typical retail standard |
| **Architecture (trading)** | **3.0 / 10** | Signal → persistence exists; regime, ranking, sizing, and portfolio layers are absent or dead |
| **Execution** | **2.0 / 10** | No sizing, no cost model, market-fill assumption, exits polled every 15 min on LTP |
| **Risk** | **1.5 / 10** | No intraday sizing, no correlation/sector/exposure caps, no daily loss limit. Correct pattern exists in `pro_system_service` but is unused here |
| **Scalability** | **4.0 / 10** | Rate-limit bound, single broker, synchronous scan loop; caching partly mitigates |
| **Accuracy / measurement fidelity** | **2.0 / 10** | Repainting signals, optimism-biased backtester, 15-min LTP exit polling, survivorship-biased universe |
| **Maintainability** | **6.5 / 10** | Readable, well-commented, clear boundaries; docs have drifted from code |
| **Institutional quality** | **1.5 / 10** | Would not pass a risk committee — primarily on §0.1, §4.1, and §5 |

### Overall: **30 / 100**

**The fair reading: you have built competent software around an unvalidated and currently
negative-expectancy trading strategy.** The engineering is the strong part. The score is low
because the criteria are institutional, and by that standard the absence of a risk engine and the
cost/edge inversion (§0.1) are each individually disqualifying regardless of code quality.

**The encouraging reading:** almost everything holding the score down is *known, bounded, and
mostly arithmetic*. §0.1, §3.1e, §5.1, and §6.2 together are a few days of work and no research
risk. They would plausibly move the score to the mid-50s without a single new alpha idea.

---

## Roadmap

Ordered strictly by **performance gain per unit of engineering effort.** No cosmetic items.

### v1.1 — "Make it honest and arithmetically viable" ✅ IMPLEMENTED
*Effort: ~1 week. No new alpha. Highest ROI in the entire roadmap.*

| # | Task | Status | Why |
|---|---|---|---|
| 1 | **Sort candidates by score before applying the cap** (§6.2) | ✅ Done | Strictly non-negative EV. Best ROI in the codebase |
| 2 | **Minimum R ≥ 3× round-trip cost** (§0.1) | ✅ Done | Removes arithmetically unprofitable trades |
| 3 | **Evaluate on closed bars only** (§3.1a) | ✅ Done | Stops repainting; makes backtest and live agree |
| 4 | **Risk-based position sizing** (§5.1) | ✅ Done | Equalises risk; largest single DD lever |
| 5 | **Audit exits on 1-min bar high/low** (§4.1) | ✅ Done | Makes recorded results real |
| 6 | **Fix backtester optimism bias + off-by-one** (§8.1a,b) | ✅ Done | Stops the tool from lying |
| 7 | **Daily loss limit (−2% → flatten)** (§5.4) | ✅ Done | Survival |
| 8 | **Add time stop (8 bars)** (§4.4) | ✅ Done | Cheapest real expectancy gain |
| 9 | **Instrument `relaxed` mode separately** (§6.4) | ✅ Done | Evidence before deletion |
| 10 | Fix the dead regime gate, or delete it (§2.1) | ✅ Done | Remove the illusion of a filter |

**Expected:** expectancy negative → ~break-even/positive · Max DD −35 to −50% · **and for the
first time, trustworthy numbers.**

#### Additional defects found *during* implementation (not in the original audit)

| Finding | Severity | Resolution |
|---|---|---|
| **`run_backtest_for_signal()` never ran at all.** `enumerate(candles.iterrows())` yields `(idx, (timestamp, row))`, so `candle` was a tuple and `candle.get("Close")` raised `AttributeError` on the very first bar of every call. The `/api/stocks/signal-backtest/` endpoint could only ever have returned HTTP 400. | Critical | Unpacked the timestamp. The optimism-bias fix would otherwise have been applied to a function that could not execute |
| **A per-name notional cap set at 15% of equity silently overrode risk parity.** Risking 0.30% of equity behind a 0.30% stop *requires* ~100% of equity in notional; a 15% cap therefore bound on essentially every trade, so sizing was still effectively notional-driven | High | Per-name cap raised to 100% (a genuine outlier guard) and a real portfolio-level gross-exposure cap added at 300% |
| **Relaxed-mode decision was recomputed inside the per-symbol loop**, issuing one DB query per scanned symbol despite being a session-level property | Medium | Hoisted out of the loop |
| **Bulk quote fetch became dead weight** once signals moved to closed bars — it existed only to supply `live_price` to the trigger | Low | Removed; saves REST calls against the shared rate limiter |

#### Implementation notes worth knowing

- **Configuration is settings-overridable**, so no code edit is needed to retune:
  `INTRADAY_ROUND_TRIP_COST_PCT` (0.14), `INTRADAY_MIN_TARGET_COST_MULTIPLE` (3.0),
  `INTRADAY_ACCOUNT_EQUITY` (₹5,00,000), `INTRADAY_RISK_PER_TRADE_PCT` (0.30),
  `INTRADAY_MAX_POSITION_PCT` (100), `INTRADAY_MAX_GROSS_EXPOSURE_PCT` (300),
  `INTRADAY_DAILY_LOSS_LIMIT_PCT` (2.0).
  **`INTRADAY_ACCOUNT_EQUITY` defaults to ₹5,00,000 and must be set to the real figure**
  — every position size derives from it.
- **Exit auditing degrades rather than fails.** If the 1-min bar fetch is unavailable
  (rate limit, circuit breaker), the auditor falls back to the old LTP comparison
  instead of leaving positions unaudited. Fallback exits are tagged `LEVEL_HIT_LTP` so
  degraded-fidelity records stay distinguishable from real ones.
- **Every exit now records *why*** in `metadata.exit_reason`: `LEVEL_HIT`,
  `LEVEL_HIT_LTP`, `TIME_STOP`, `SQUARE_OFF_CUTOFF`, `DAILY_LOSS_LIMIT`. Exit-rule
  attribution is a prerequisite for v2.0's P&L attribution work.
- **Expect trade count to fall sharply.** The cost gate alone rejects any setup whose
  target is under 0.42%, which on 5-min bars is a large fraction of prior signals. Fewer
  signals is the intended outcome, not a regression — but it does mean statistical
  significance now takes proportionally longer to reach (§8.3).
- **The still-open measurement gap:** the scanner and auditor still only run 11:00–15:15
  on a 15-minute cron. Exit *detection* is now exact to the minute via bar scanning, but
  detection still cannot happen more often than the auditor runs. Moving to a tighter
  cadence, or to WebSocket-driven bar construction (§9.3), is v2.0 work.

### v1.2 — "Regime and selectivity" ✅ IMPLEMENTED
*New modules: `regime_service.py`, `ranking_service.py`, `universe_service.py`, `portfolio_risk.py`*

| # | Task | Status |
|---|---|---|
| 1 | Two-axis regime model with hysteresis (§2) | ✅ `regime_service.get_regime()` |
| 2 | Regime-conditional trigger enablement | ✅ `strategy_allowed()` — momentum vs mean-reversion families |
| 3 | 0–100 cross-sectional ranking, threshold 65 | ✅ `ranking_service.score_candidates()` |
| 4 | Session-anchored VWAP (§2.2) | ✅ `compute_session_vwap()` |
| 5 | Sector + correlation exposure caps (§5.3) | ✅ `portfolio_risk.apply_portfolio_constraints()` |
| 6 | Liquidity-filtered NIFTY200 universe (§1) | ✅ `universe_service.get_trading_universe()` |
| 7 | `relaxed` mode | ✅ Disabled by default; kept behind a flag so its EV stays measurable |

### v2.0 — "Validation infrastructure and real alpha" — ✅ FRAMEWORK BUILT
*New modules: `trading_engine/cost_model.py`, `portfolio_backtest.py`, `validation.py`*

| # | Task | Status |
|---|---|---|
| 1 | Event-driven portfolio backtester with costs + slippage | ✅ `run_portfolio_backtest()` |
| 2 | Walk-forward + purged K-fold with embargo | ✅ `validation.py` |
| 3 | Monte Carlo DD distributions; Deflated Sharpe | ✅ `validation.py` |
| 4 | Local candle store + WebSocket bar construction (§9.3) | ❌ **Not built** — still REST-bound |
| 5 | Relative-strength alpha layer | ✅ `_relative_strength()`, 20% ranking weight |
| 6 | Volatility-compression entry | ❌ **Not built** — new trigger, needs validation first |
| 7 | Regime-differentiated exits | ⚠️ Partial — time stop + regime sizing; no trailing stop yet |
| 8 | P&L attribution | ⚠️ Partial — `exit_reason` attribution done; alpha-vs-beta not |

### v3.0 — "Portfolio of strategies" — partially implemented

| # | Task | Status |
|---|---|---|
| 1 | Multiple weakly-correlated strategies | ❌ Requires strategies that do not exist yet |
| 2 | Portfolio vol targeting + regime-scaled exposure | ✅ `volatility_scalar()`, regime `size_multiplier` |
| 3 | Execution modelling (impact, participation) | ✅ Square-root impact in `cost_model.py` |
| 4 | ¼-Kelly sizing | ✅ Implemented, **hard-gated at 300 trades — currently returns 0.0** |
| 5 | ML regime classification (HMM) | ⚠️ Z-score composite instead; HMM needs training data |
| 6 | Second data vendor | ❌ **Cannot be done in code** — needs a subscription + credentials |

---

### Final tranche — remaining §1–§9 defects ✅ IMPLEMENTED

These were section-level defects that never appeared in the roadmap tables, so they
survived the v1.1/v1.2 passes. All are now closed and covered by
`stocks/tests_intraday_v3.py`.

| § | Defect | Resolution |
|---|---|---|
| 3.1b | VP built on a 2-day composite, used as intraday levels | `compute_session_volume_profile()` — today's developing profile, prior-day fallback while the session is thin |
| 3.1c | Exclusive `elif` chain hardcoded trigger priority | All firing triggers are emitted; the ranking model decides, then one best candidate per symbol is kept |
| 3.1f | No event blackout | `event_filter_service.py` — NSE F&O ban CSV + earnings calendar, T±1 window |
| 3.1g | Live entry assumed a mid fill | `slipped_fill()` now applied to live entries, identical to the backtester |
| 4.2 | No trailing / break-even / VWAP exit | Break-even at +1R, Chandelier trail past 1.5R (momentum only), session-VWAP exit (mean reversion only) |
| 4.1 residual | Scanner ran 11:00–15:15 only | Extended to 9:00–15:15; unblocked by the prior-day VP fallback |
| 5.4 | No beta constraint | `compute_betas()` + `beta_constrained()`, net-beta band ±1.5 |
| 9.3 | No local candle store | `candle_store.py` + `CandleBar` model — delta fetching, `backfill()` for bulk history |
| 3.2 r3 | No volatility-compression entry | `compute_compression()` (NR7 + Bollinger squeeze) driving a fourth trigger |
| v2.0/8 | No P&L attribution | `attribute_pnl()` — market / sector / residual alpha decomposition |

**Defects found while implementing this tranche** (not in the original audit):

- The **exit ordering matters**: trailing the stop *before* re-scanning the same bars
  would retroactively stop out trades on price action that occurred while the stop was
  still wide. Level checks now run against the stop as it stood over those bars, and
  trailing only arms the *next* cycle.
- **Storing the forming bar** in the candle store would have reintroduced the repaint
  problem at the data layer, where it is far harder to notice. `store_bars()` drops it.

## What is genuinely still blocked, and why

Honesty matters more here than a clean checklist, because several of these look done
from the module list but are not usable yet.

1. **UPDATE 2026-07-26: a first real backtest HAS now been run** — see "Empirical
   validation" below. What is still true: it only covers ~133 trading days (Angel One's
   intraday history ceiling per interval, not a choice), so it remains a preliminary
   read, not the years of history genuine walk-forward/Monte Carlo confidence needs.
   Full-depth validation is still blocked on either (a) accumulating more days forward
   from here, or (b) a paid data vendor with deeper intraday archives.
2. **Kelly returns 0.0 and will keep doing so** until 300 validated trades exist. That
   gate is deliberate (§5.2): a ±1 SE error in win rate moves optimal leverage by 2.5×.
3. **The ranking threshold of 65 is unvalidated.** It is a reasonable prior, not a fitted
   value, and it cannot be properly fitted until years of history exist. The same
   applies to every regime cutoff — though the regime reconstruction itself is now real,
   not a placeholder (see below).
4. **Second data vendor is not a code task.** It needs a commercial subscription.
5. **An edge now has SOME empirical support**, which is a real change from before this
   was written — see the results table below. It is not yet validated at a sample size
   that means anything statistically (§8.3), and net P&L has not yet turned durably
   positive in any configuration tested. Read the "Empirical validation" section for
   the actual numbers and their caveats before treating any of this as decided.

## Empirical validation — first real results (2026-07-26)

Item 1 above ("no validation has actually been RUN") is no longer fully true. A candle
store was backfilled (183 NIFTY200 symbols + the NIFTY 50 index itself, 15-min bars,
~133 usable trading days — Angel One silently caps intraday history per interval
regardless of the range requested, so this is the real ceiling, not a choice), a
historical replay engine was built (`trading_engine/replay.py`), and the portfolio
backtester was run against real price history for the first time. New commands:
`backfill_candles`, `run_historical_backtest`.

**This remains a preliminary read, not a validated edge** — 133 days is well under the
audit's own §8.3 bar. But it is no longer a pure code-review guess; it is data.

### A real bug found in the process: the live regime model was likely silently broken

Building a *historical* regime series (to stop replay from using a permissive
placeholder — see below) required calling `compute_session_vwap()` on the NIFTY 50
index. Its volume column is **zero on every bar** (an index isn't itself traded, only
its constituents are), which made the volume-weighted VWAP divide 0/0 → `NaN` on every
call. `NaN` then propagates through `np.sign()` and `np.clip()` in the trend-score math
(`shared/regime.py`), and every `> threshold` / `< threshold` comparison against `NaN`
evaluates `False` — collapsing the directional trend axis to `NEUTRAL`/`SIDEWAYS` no
matter what the market was actually doing. **This is the exact same function the LIVE
`get_regime()` calls on the same zero-volume feed** (`shared/regime.py:299`), so this
was very likely also broken in production, not just in the backtest. Fixed in
`compute_session_vwap()` (`signal_utils.py`) with a fallback to an unweighted
session-average of typical price when volume is zero — verified to eliminate 100% of
the NaNs on real NIFTY data and produce a believable BULLISH/BEARISH/SIDEWAYS mix
(previously: SIDEWAYS on all 3,325 bars tested).

### Historical regime reconstruction (closes the biggest gap in §2)

`trading_engine/historical_regime.py` reconstructs the two-axis regime causally — every
input (`compute_adx_series`, `compute_atr_series`, session VWAP, realized-vol ratio,
market breadth from the replay's own loaded symbols) is a full `pandas.rolling()`
series computed once per backtest, not recomputed per bar, so it is both correct
(rolling only ever looks backward) and fast enough to use. Hysteresis is walked forward
bar-by-bar (2 consecutive reads to adopt a state change) rather than compared against a
live cache. `replay.py` looks up the regime as of each candidate's own timestamp via a
vectorized `merge_asof` and now actually enforces `strategy_allowed()` — momentum
triggers declined in a chop/mean-reversion regime, mean-reversion triggers declined in
a trending one — exactly what the live engine's gate is supposed to do and what every
prior backtest run in this file was missing. Verified with a dedicated test suite
(`tests_intraday_historical_regime.py`): causality, hysteresis, as-of lookup direction,
and the gating itself (a deterministic stand-in signal proves momentum candidates are
actually removed while mean-reversion candidates survive the same gate).

### Results (all on 15-min bars, ~133 usable trading days)

| Config | Symbols | Trades | Win rate | Gross P&L | Net P&L | Max DD |
|---|---|---|---|---|---|---|
| Baseline (3× cost gate) | 50 | 1,178 | 43.8% | +₹47,291 | −₹72,504 | 16.6% |
| 6× cost gate | 50 | 1,036 | 43.3% | +₹49,245 | −₹51,419 | 12.1% |
| Baseline (3×) | 10 | 434 | 42.2% | −₹11,571 | −₹65,157 | 15.4% |
| 3× + exclude 2 weakest triggers | 10 | 463 | 44.3% | +₹13,227 | −₹45,658 | 11.2% |
| 6× + exclude triggers | 1 (RELIANCE) | 179 | 44.1% | +₹14,495 | −₹17,928 | 6.3% |
| Cost-gate sweep 8×/10×/15×/20× | 1 (RELIANCE) | 133→22 | 43–50% | — | −6.8K→−8.2K→−10.9K→**+₹97** | — |
| **3×, neutral regime placeholder** | 1 (RELIANCE) | 333 | 38.4% | — | **−₹1,08,424** | 22.7% |
| **3×, REAL regime reconstruction** | 1 (RELIANCE) | 238 | 39.1% | −₹10,424 | **−₹64,409** | 13.2% |
| All three fixes combined (real regime + 6× + exclude triggers) | 1 (RELIANCE) | 136 | 43.4% | +₹8,355 | −₹16,141 | 4.7% |
| **3×, REAL regime reconstruction** | 10 | 745 | 45.1% | **+₹87,162** | **−₹19,295** | 14.2% |

### What this actually shows

1. **Gross trading logic is often net-positive; real transaction costs are what turn it
   negative.** True at 50 symbols, 10 symbols, and with the weakest triggers removed.
   This is the audit's §0.1 finding confirmed with real replayed data, not just
   arithmetic.
2. **The regime filter is the single largest lever found so far, and it holds up at
   10 symbols, not just on one stock.** RELIANCE alone: real regime cut the loss ~40%
   and drawdown ~42% vs. the identical run with a neutral placeholder. At 10 symbols,
   real regime (3×, no trigger exclusion) took gross P&L from **−₹11,571 to +₹87,162**
   and net loss from −₹65,157 to **−₹19,295** — Sharpe went from −2.57 to −0.30. This is
   the best result in the entire investigation, and the biggest lever by a wide margin
   over the cost-gate sweep or trigger exclusion tested individually.
3. **The cost-gate sweep (8×→20×) is NOT a clean improving trend** — it bounces
   (8× beat 10× and 15×) before the 20× result lands near breakeven on only 22 trades.
   That pattern, not the +₹97 itself, is the finding: shrinking the sample is what
   produced the near-zero number, not a real improving edge. **Do not read +₹97/22
   trades as "found the fix."**
4. **Fixes overlap rather than stack additively.** Real regime alone: −40% loss.
   Real regime + 6× + trigger exclusion together: only ~10% better than 6×+exclusion
   alone. Much of what the regime filter would have caught was already caught by the
   other two filters.
5. **Across every configuration tested, net P&L has never once turned durably
   positive** on a sample size that means anything — but the gap has closed a lot.
   The best result (10 symbols, real regime, 3× cost gate) is −₹19,295 against
   +₹87,162 gross: costs are still bigger than the edge, but not by an order of
   magnitude anymore. The strategy clearly has a real directional signal; whether it
   can be made to durably clear its own costs is the open question, not whether it can
   pick direction.

### Explicitly not recommended

- **Cumulative delta / footprint / order blocks** (§3.2) — unimplementable on Angel One's retail
  feed. Would produce sophisticated-looking noise.
- **Partial scale-out** — until minimum-R is enforced and R ≥ ~0.5% (§4.3). Doubles costs against
  a thin edge.
- **Full Kelly sizing** — estimation error makes it actively dangerous with no track record (§5.2).
- **Expanding to NIFTY500** (§1) — the added names' spreads exceed the strategy's targets.

---

## Summary — the five things that matter

1. **§0.1 — Transaction costs exceed the edge.** Required win rate is 62–72%, not 33%. Nothing
   else matters until minimum-R is enforced. *Arithmetic, not a prediction.*
2. **§4.1 / §8.1 — The system cannot measure itself.** Repainting signals, 15-minute LTP exit
   polling, and a backtester that resolves ties in its own favour. Every reported statistic is
   unreliable.
3. **§5 — There is no risk engine.** Equal-notional sizing means risk varies ~6× across trades;
   `N_eff ≈ 1.47` means "5 positions" is ~1.5 independent bets.
4. **§2 — Regime detection exists but is disconnected.** Half your triggers are counter-regime at
   any given time. This is the highest-value alpha fix.
5. **§3.1e / §6.2 — You emit the first 5 signals alphabetically, not the best 5.** Thirty minutes
   of work, strictly non-negative expected value.

Items 1, 3, and 5 require no research and no new data. They are the fastest path off a 30/100.
