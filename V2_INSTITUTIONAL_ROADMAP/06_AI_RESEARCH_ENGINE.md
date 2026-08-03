# 06 — AI Research Engine

## 1. What already exists (real foundation, not a green field)

`backend/insights/services/ai_insight_service.py` (337 lines) — a working integration: `_collect_structured_data()` gathers a structured daily snapshot, `_call_claude_api()` sends it to Claude (`model="claude-sonnet-4-20250514"`) with a system prompt, and there's a `_generate_local_insight()` rule-based fallback if the API call fails (good defensive design — the feature degrades gracefully rather than breaking the dashboard). Surfaced via `AIInsightBox.jsx` → `GET /api/insights/daily/`. This is the pattern the rest of this document extends, not replaces.

**One operational note**: the model string is pinned to `claude-sonnet-4-20250514`. Verify against current model availability before Phase 6 (model strings get deprecated) — not a design flaw, just a maintenance item.

## 2. Design Principle — the hard boundary from `01_PROJECT_ARCHITECTURE.md` §3.9

**The AI layer is read-only against `core/signal_lifecycle` and the Portfolio Holdings model (`05`).** It can analyze, summarize, score, and flag — it can never create, modify, or close a signal or holding directly. This isn't a policy note, it's an architectural boundary: the AI service layer should not even import the write-path functions of the signal/holding models. Given this project's stated objective is capital preservation, an AI-generated hallucination should never be able to directly touch capital — it can only ever produce a recommendation a human (or a separately-tested, deterministic rule engine) acts on.

## 3. Capabilities

### 3.1 Daily Market Summary (exists — extend)
Currently covers structured market data → prose. Extend `_collect_structured_data()` to include: sector rotation ranking (`04` §5), risk dashboard snapshot (`02` §8), and portfolio sleeve drift (`05` §4) — turning it from a market summary into a genuine daily briefing that references the user's *actual* portfolio state, not just the market.

### 3.2 Trade Review
Given a closed signal (any category, either model family), produce a structured critique: was the entry/exit timing sound given what was knowable at the time (not hindsight-biased against information that arrived after entry), was position size consistent with the risk engine's own recommendation at the time, psychology flags (see §3.7). Output format should mirror the "Trade Review" structure the user already validated works well in this engagement (Entry/Exit/Timing/Position Size/Risk/Reward/Psychology/Mistakes/Alternative Actions/Professional Improvements/Rating) — that structure is proven to produce useful output; reuse it as the actual system prompt template, don't reinvent a different one.

### 3.3 Portfolio Review (ties to `05_PORTFOLIO_MANAGEMENT.md` §8)
Weekly, automated. Same read-only boundary as §2.

### 3.4 News & Earnings Analysis (net new)
No news/earnings data source exists in the codebase today (confirmed: no news API integration found in any service file). This requires a new data source before the AI layer has anything to analyze — sequence this after a news/earnings feed is wired in (a corporate-announcements feed from NSE's own public endpoints, following the exact cookie-bootstrap pattern already proven in `signal_utils.py` and this session's `groww_free_service.py`, is the lowest-cost option before paying for a commercial news API).

### 3.5 Sentiment (net new, depends on §3.4)
Same dependency — no sentiment analysis is possible without a text source to analyze first.

### 3.6 Volatility Prediction
Distinct from IV Rank/Percentile (`03` §2.1, which is *historical positioning*, not prediction). A genuine volatility forecast (e.g., GARCH-family model or a simpler EWMA realized-vol projection) is a quant-research task, not primarily an LLM task — recommend building this as a `core/` statistical service, with the AI layer only summarizing its output in plain language, not generating the forecast itself. Don't ask an LLM to do arithmetic a proper model does better and more reproducibly.

### 3.7 Psychology Flags
Computable from data already in the system without any new LLM calls: overtrading (signal count vs. historical average), revenge trading (new entry within N minutes of a stop-loss on the same or a correlated symbol), holding losers past the system's own stop-loss recommendation (requires comparing actual exit vs. `RiskEngine`-recommended exit, once `02` exists), position-size deviation from the risk engine's sizing (large deviation = an emotional override happened). These are **deterministic checks**, not AI judgment calls — compute them as data, then optionally have the AI layer narrate them in the trade review (§3.2). Don't make an LLM guess at psychology from vibes when the actual behavioral data is sitting in the database.

### 3.8 Backtesting Assistant
Once `07_BACKTESTING_FRAMEWORK.md` exists: natural-language → backtest-parameter translation ("show me how the strangle engine did in high-VIX months" → a structured query against the backtest engine's stored results). This is a UI-convenience layer over real backtest infrastructure, not a substitute for it — the AI never *runs* a simulation itself, it constructs the query and narrates the (deterministically computed) result.

### 3.9 Learning System
Lowest priority, highest risk of overreach. "Learning" in the sense of *the AI adjusting its own future recommendations based on past outcomes* is exactly the kind of unsupervised-feedback-loop design that can quietly drift a trading system's behavior in ways nobody explicitly approved. Recommend instead: a periodic (monthly) human-reviewed report of "which of the AI's past trade reviews' suggested improvements were actually adopted, and what happened when they were" — a feedback loop with a human in it, not an autonomous one.

## 4. Prompt Engineering Standard

Every prompt used across §3.1-3.3 should follow the same structure this engagement's own system prompt (`doc/MASTER_TRADING_RESEARCH_ASSISTANT.md`) uses and that the user has now validated twice produces useful output: explicit role, explicit mission ("find weaknesses," not "agree"), explicit output format, explicit instruction to state confidence and flag missing data rather than guess. Store these as versioned prompt templates (not inline strings scattered across service files) so a prompt change is a reviewable diff, not a silent behavior change.

## 5. Research Agents

Framed narrowly: a "research agent" here means a scheduled job that gathers structured data and produces a report (§3.1-3.3), not an autonomous agent with tool-use/write access. Given §2's hard boundary, there is currently no scenario in this platform's design where an AI agent needs write access to anything — every capability requested is read-and-summarize. Keep it that way; expanding to write-access AI agents would need its own dedicated risk review, separate from this document, if ever proposed.
