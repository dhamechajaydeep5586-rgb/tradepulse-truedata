# Requirements Document

## Introduction

TradePulse AI Core is a swing trading intelligence platform built on Django + React. It automatically scans Nifty 500 stocks every trading day at 10:00 AM, scores each stock using a 100-point AI model, creates trade opportunities, manages the full trade lifecycle from PENDING through EXITED, and delivers structured Telegram notifications at each key lifecycle event plus a single consolidated daily summary. The platform operates as a distinct module from the existing intraday engine, persisting a permanent historical price database and providing a multi-tab React dashboard with analytics.

---

## Glossary

- **Scanner**: The Celery task that runs at 10:00 AM to evaluate all Nifty 500 stocks.
- **Trade_Engine**: The dedicated Django service containing all buy/sell/expiry/trailing-stop business rules.
- **Trade**: A database record created when the AI decides an opportunity is worth tracking.
- **TradeScanner**: Today's scan results table; one record per scanned stock per scan date.
- **StockDailyData**: The append-only historical price and indicator table (one record per stock per day, never overwritten).
- **TelegramNotifier**: The service responsible for composing and dispatching all Telegram messages.
- **DailySummaryJob**: The Celery task that composes and sends the single consolidated end-of-day Telegram summary.
- **Nifty500**: The index universe of approximately 500 large/mid-cap Indian stocks used as the scan universe.
- **AI_Score**: The composite 100-point score computed per stock per scan (Trend 20 + Momentum 20 + Volume 15 + Relative Strength 15 + ADX 10 + Sector 10 + Fundamental 10).
- **RR**: Risk-to-reward ratio, calculated as (Target1 − Entry) / (Entry − StopLoss).
- **Trailing_Stop**: A stop-loss level that moves up as price advances, activated after Target 2 is hit.
- **EMA**: Exponential Moving Average.
- **RSI**: Relative Strength Index (14-period).
- **ADX**: Average Directional Index (14-period), measures trend strength.
- **ATR**: Average True Range (14-period), measures volatility.
- **IST**: Indian Standard Time (UTC+5:30).

---

## Requirements

---

### Requirement 1: Historical Price Database

**User Story:** As a developer, I want every trading day's OHLCV and technical indicator data stored permanently per stock, so that the system has a growing historical database for backtesting and indicator calculation.

#### Acceptance Criteria

1. THE StockDailyData table SHALL store one record per stock per trading date, with fields: open, high, low, close, volume, delivery_percentage, ema20, ema50, ema200, rsi14, adx14, atr14, 52week_high, 52week_low, sector_return, nifty_return.
2. WHEN a daily data record for a given stock and date already exists, THE Scanner SHALL skip insertion and leave the existing record unchanged.
3. THE Scanner SHALL calculate ema20, ema50, ema200, rsi14, adx14, and atr14 from the historical close series stored in StockDailyData before persisting each new daily record. WHEN a stock has fewer historical records than the indicator period, THE Scanner SHALL compute the indicator using all available records (e.g., EMA20 with only 5 records uses a 5-period EMA) and SHALL store the result; null values SHALL NOT be stored.
4. WHEN the 52-week high or low calculation requires more than 252 trading days of history and fewer records exist, THE Scanner SHALL compute the value using all available records for that stock.
5. THE StockDailyData table SHALL enforce a unique constraint on (stock_id, date) at the database level.

---

### Requirement 2: Market Filter Gate

**User Story:** As a trader, I want the scanner to halt if the overall market is not in an uptrend, so that no new swing trades are initiated during bear market conditions.

#### Acceptance Criteria

1. WHEN the 10:00 AM scan runs, THE Scanner SHALL first evaluate the Market Filter: Nifty Index close > 50 EMA AND 50 EMA > 200 EMA AND ADX > 20.
2. IF the Market Filter fails, THEN THE Scanner SHALL set the scan day status to "NO_BUY_TODAY", skip all stock-level scoring, and log the reason. WHEN the Market Filter passes, THE Scanner SHALL proceed to stock-level evaluation without setting a special day status.
3. WHEN the scan day status is "NO_BUY_TODAY", THE Scanner logic SHALL NOT attempt to create any new Trade or TradeScanner records for that date.
4. THE Scanner SHALL record the Nifty trend state (BULLISH / SIDEWAYS / BEARISH) and the ADX value in the scan session log for that date.

---

### Requirement 3: Daily Stock Scan and AI Scoring

**User Story:** As a trader, I want every Nifty 500 stock evaluated against a structured 10-step filter each morning, so that only the highest-conviction setups are surfaced.

#### Acceptance Criteria

1. WHEN the Market Filter passes, THE Scanner SHALL evaluate all active Nifty 500 stocks in sequence against the following filters in order:
   - Step 2 — EMA Stack: Price > EMA20 > EMA50 > EMA200
   - Step 3 — Momentum: Price is within 5% of 52-week high OR a 20-day price breakout has occurred
   - Step 4 — Relative Strength: 20-day stock return > 20-day Nifty return
   - Step 5 — Volume Surge: 5-day average volume > 1.5 × 20-day average volume
   - Step 6 — Liquidity: Daily traded value > ₹25 Crore
   - Step 7 — Fundamentals: ROE > 15% AND revenue growth (TTM) > 10% AND debt-to-equity < 1.0
2. WHEN a stock fails any filter step, THE Scanner SHALL record the failure reason in TradeScanner and skip remaining steps for that stock.
3. WHEN a stock passes all seven filter steps, THE Scanner SHALL compute the AI_Score as: Trend(max 20) + Momentum(max 20) + Volume(max 15) + Relative_Strength(max 15) + ADX(max 10) + Sector(max 10) + Fundamental(max 10).
4. THE Scanner SHALL persist one TradeScanner record per stock per scan_date — including stocks that fail early filter steps — containing: ai_score (0 for filter failures), each sub-score, entry_price, stop_loss, target1, target2, target3, holding_days, expiry_days, and status.
5. WHEN the AI_Score is below 60, THE Scanner SHALL set the TradeScanner status to "FILTERED_LOW_SCORE" and SHALL NOT create a Trade record.

---

### Requirement 4: Entry, Stop Loss, and Target Calculation

**User Story:** As a trader, I want the system to automatically calculate precise entry, stop loss, and three targets for each qualifying setup, so that I have a complete trade plan before entry.

#### Acceptance Criteria

1. WHEN a stock passes all scanner filters, THE Trade_Engine SHALL set entry_price as the next-day opening price reference (current day's close as proxy until opening price is confirmed).
2. THE Trade_Engine SHALL calculate stop_loss as entry_price − (1.0 × ATR14), rounded to 2 decimal places.
3. THE Trade_Engine SHALL calculate target1 as entry_price + (1.5 × ATR14).
4. THE Trade_Engine SHALL calculate target2 as entry_price + (2.5 × ATR14).
5. THE Trade_Engine SHALL calculate target3 as entry_price + (4.0 × ATR14).
6. WHEN the resulting RR (target1 − entry) / (entry − stop_loss) is less than 1.5, THE Trade_Engine SHALL discard the opportunity and SHALL NOT create a Trade record.
7. THE Trade_Engine SHALL store all five price levels (entry, stop_loss, target1, target2, target3) and the computed RR in the Trade record.

---

### Requirement 5: Trade Creation and PENDING Status

**User Story:** As a trader, I want each qualifying opportunity to become a tracked trade with PENDING status and an immediate Telegram notification, so that I know a setup is armed and waiting for entry.

#### Acceptance Criteria

1. WHEN a stock passes all scanner filters and the RR condition is satisfied, THE Trade_Engine SHALL create one Trade record with status PENDING.
2. THE Trade_Engine SHALL set expiry_date on the Trade record to buy_date + 30 trading days.
3. WHEN a Trade record is created with status PENDING, THE TelegramNotifier SHALL send a "🔥 NEW SWING TRADE" message containing: symbol, AI_Score, entry_price, stop_loss, target1, RR, and status.
4. THE Trade_Engine SHALL NOT create a duplicate Trade record for the same stock if a Trade with status PENDING or ACTIVE already exists for that stock.
5. WHEN a Trade is created, THE TelegramLog table SHALL record the message type, content, delivery status, and sent_at timestamp.

---

### Requirement 6: Trade Activation (PENDING → ACTIVE)

**User Story:** As a trader, I want the system to automatically activate a trade the moment the market price touches or crosses my entry level, so that I'm notified of a live buy opportunity in real time.

#### Acceptance Criteria

1. THE Activation_Job (running every 10 minutes during market hours 9:05 AM – 3:30 PM IST) SHALL check every PENDING trade to determine whether the current day's low price is less than or equal to entry_price.
2. WHEN current_low ≤ entry_price for a PENDING trade, THE Trade_Engine SHALL transition the trade status to ACTIVE and set activated_date to the current date.
3. WHEN a trade transitions to ACTIVE, THE TelegramNotifier SHALL send a "🔔 BUY ACTIVATED" message containing: symbol, buy_price (entry_price), and activated_date.
4. WHEN a trade transitions to ACTIVE, THE Trade_Engine SHALL set buy_date to the activation date.
5. THE Activation_Job SHALL process all PENDING trades in a single pass and SHALL complete within 60 seconds.

---

### Requirement 7: Daily Trade Update (3:25 PM Job)

**User Story:** As a trader, I want the system to update all live trades every afternoon at 3:25 PM, so that current price, profit, and status reflect the day's outcome.

#### Acceptance Criteria

1. THE Daily_Update_Job SHALL run at 3:25 PM IST on every market day and SHALL update current_price and profit for every trade with status in {ACTIVE, HOLDING, TARGET1, TARGET2, TRAILING}.
2. THE Trade_Engine SHALL calculate profit as ((current_price − entry_price) / entry_price) × 100, stored as a percentage rounded to 2 decimal places.
3. THE Trade_Engine SHALL increment holding_days by 1 for every trade with status in {ACTIVE, HOLDING, TARGET1, TARGET2, TRAILING} at each 3:25 PM run.
4. WHEN current_price ≥ target1 and the trade status is ACTIVE or HOLDING, THE Trade_Engine SHALL transition status to TARGET1 and SHALL move stop_loss to entry_price (break-even protection).
5. WHEN current_price ≥ target2 and the trade status is TARGET1, THE Trade_Engine SHALL transition status to TARGET2 and SHALL activate trailing stop logic.
6. WHEN current_price ≥ target3 and the trade status is TARGET2 or TRAILING, THE Trade_Engine SHALL transition status to EXITED with reason "TARGET3_HIT".
7. WHEN current_price ≤ stop_loss for any trade with status in {ACTIVE, HOLDING, TARGET1, TARGET2, TRAILING}, THE Trade_Engine SHALL transition status to STOPLOSS and set exit_date to the current date.
8. THE Daily_Update_Job SHALL send a "📈 HOLDING UPDATE" Telegram message for each trade with status HOLDING or TARGET1, containing: symbol, holding_days, current profit %, and distance to next target %.

---

### Requirement 8: Trailing Stop Management

**User Story:** As a trader, I want the system to trail my stop loss automatically after Target 2 is hit, so that my profits are protected while allowing further upside.

#### Acceptance Criteria

1. WHEN a trade transitions to TARGET2, THE Trade_Engine SHALL set the trailing stop to target1 (locking in minimum profit).
2. WHILE a trade has status TRAILING or TARGET2, THE Trade_Engine SHALL update the trailing stop each day to max(current trailing stop, current_price − (1.5 × ATR14)).
3. WHEN current_price ≤ trailing_stop for a trade with status TRAILING or TARGET2, THE Trade_Engine SHALL transition status to EXITED with reason "TRAILING_STOP_HIT" and record exit_date and exit_price.
4. WHEN a trade transitions to TRAILING status, THE TelegramNotifier SHALL send a "🎯 TARGET 2 HIT" message containing: symbol, current profit %, and confirmation that trailing stop is enabled.
5. THE Trade_Engine SHALL store the trailing_stop value in the Trade record and update it daily.

---

### Requirement 9: Trade Expiry Rules

**User Story:** As a trader, I want stale trades automatically expired based on defined time rules, so that capital is not tied up in setups that never triggered or overstayed their window.

#### Acceptance Criteria

1. WHEN a trade with status PENDING has been open for more than 30 trading days without activation, THE Trade_Engine SHALL transition status to EXPIRED and record the reason "PENDING_TIMEOUT_30_DAYS".
2. WHEN a trade with status ACTIVE has been open for more than 90 calendar days, THE Trade_Engine SHALL flag the trade for review with status EXPIRED and reason "ACTIVE_TIMEOUT_90_DAYS".
3. WHEN a trade transitions to EXPIRED from PENDING, THE TelegramNotifier SHALL send a "⌛ EXPIRED" message containing: symbol, entry_price, and the note "Never reached entry, 30 trading days completed".
4. WHEN a trade has status TARGET1 or higher (TARGET2, TRAILING), THE Trade_Engine SHALL NOT expire the trade regardless of holding_days.
5. THE Weekend_Job SHALL run expiry checks every Saturday and SHALL process all eligible PENDING and ACTIVE trades in a single batch.

---

### Requirement 10: Telegram Notifications

**User Story:** As a trader, I want structured Telegram messages for every key trade event, so that I can monitor my portfolio without logging into the dashboard.

#### Acceptance Criteria

1. THE TelegramNotifier SHALL send the following message types at the specified triggers:
   - "🔥 NEW SWING TRADE" — on Trade creation (PENDING)
   - "🔔 BUY ACTIVATED" — on transition to ACTIVE
   - "📈 HOLDING UPDATE" — daily at 3:25 PM for HOLDING and TARGET1 trades
   - "🎯 TARGET 1 HIT" — on transition to TARGET1
   - "🎯 TARGET 2 HIT" — on transition to TARGET2
   - "❌ STOP LOSS HIT" — on transition to STOPLOSS
   - "⌛ EXPIRED" — on transition to EXPIRED
2. WHEN a "TARGET 1 HIT" event occurs, THE TelegramNotifier SHALL include: symbol, profit %, and the note "Stop Loss moved to cost".
3. WHEN a "STOP LOSS HIT" event occurs, THE TelegramNotifier SHALL include: symbol and loss %.
4. THE TelegramNotifier SHALL record every dispatch attempt in the TelegramLog table with fields: trade_id, type, message, status (SENT / FAILED), and sent_at.
5. IF a Telegram API call fails, THEN THE TelegramNotifier SHALL retry up to 3 times with a 5-second delay between attempts. WHEN the maximum of 3 retries is reached without a successful response, THE TelegramNotifier SHALL record the status as FAILED in the TelegramLog table.
6. THE TelegramNotifier SHALL NOT send duplicate messages for the same trade event if the TelegramLog already contains a SENT record for that trade_id and message type.

---

### Requirement 11: Consolidated Daily Summary Telegram Message

**User Story:** As a trader, I want a single end-of-day Telegram message summarising my entire portfolio, so that I can review the full picture in one place without reading multiple individual alerts.

#### Acceptance Criteria

1. THE DailySummaryJob SHALL run at 3:45 PM IST on every market day and SHALL compose one consolidated Telegram message covering all open and recently closed trades.
2. THE consolidated message SHALL include the following sections in order: portfolio summary (total open trades, total P&L %), active winners (status TARGET1 / TARGET2 / TRAILING), holdings (status HOLDING / ACTIVE), today's exits (EXITED / STOPLOSS / EXPIRED on the current date), and new opportunities (trades created today with status PENDING). Each section SHALL always be included; WHEN no trades exist for a section, THE DailySummaryJob SHALL display a placeholder such as "No active winners today".
3. THE DailySummaryJob SHALL send the consolidated message as a single Telegram message (not split across multiple messages) unless the message exceeds Telegram's 4096-character limit, in which case THE DailySummaryJob SHALL split it into sequential messages.
4. THE TelegramLog table SHALL record the consolidated summary as type "DAILY_SUMMARY" with the full message text and delivery status.

---

### Requirement 12: Cron Job Schedule

**User Story:** As a developer, I want all background jobs to run on a well-defined schedule using Celery Beat, so that the system is fully automated without manual intervention.

#### Acceptance Criteria

1. THE system SHALL schedule five Celery Beat tasks at the following times (IST):
   - 9:05 AM — Update Nifty Trend, Update Sector Trend, Update Opening Price
   - 10:00 AM — Download Nifty 500 data → Calculate indicators → Compute AI Score → Create new opportunities
   - Every 10 minutes (9:05 AM – 3:30 PM) — Check PENDING trades for activation
   - 3:25 PM — Daily trade update (price, profit, holding days, target/SL checks, trailing stop)
   - 3:45 PM — Consolidated daily summary Telegram message
2. THE Weekend_Job SHALL run every Saturday at 8:00 AM IST and SHALL perform: refresh fundamentals, refresh sector rankings, expire stale PENDING trades, and generate weekly analytics reports.
3. WHEN a scheduled job fails with an unhandled exception, THE system SHALL log the error with full stack trace and SHALL NOT block subsequent scheduled runs of the same job.
4. THE system SHALL use Redis as the Celery broker and PostgreSQL as the Celery result backend.

---

### Requirement 13: Dashboard Tabs and API

**User Story:** As a trader, I want a React dashboard with dedicated tabs for each trade status category, so that I can quickly review any slice of my portfolio.

#### Acceptance Criteria

1. THE Dashboard SHALL provide the following tabs, each backed by a dedicated Django REST API endpoint: Scanner, Pending, Active, Holding, Target (TARGET1 / TARGET2), Stop Loss (STOPLOSS), Expired, Portfolio, Analytics.
2. THE Scanner tab API SHALL return today's TradeScanner records sorted by ai_score descending, including all sub-scores and trade plan details.
3. THE Pending tab API SHALL return all Trade records with status PENDING, sorted by expiry_date ascending.
4. THE Portfolio tab API SHALL return aggregated statistics: total open trades, total realised profit (EXITED trades), total unrealised profit (ACTIVE/HOLDING/TARGET trades), current win rate (%), and average holding days.
5. WHEN a tab API is requested, THE system SHALL respond within 500ms for data sets up to 500 records.

---

### Requirement 14: Analytics

**User Story:** As a trader, I want analytics covering win rate, returns, and sector performance, so that I can evaluate and improve the AI scoring strategy over time.

#### Acceptance Criteria

1. THE Analytics API SHALL return the following metrics computed from all EXITED and STOPLOSS trades: win rate (%), average return per trade (%), average holding period (days), best performing sector, and monthly profit/loss summary.
2. THE Analytics API SHALL accept optional filter parameters: date_from, date_to, and sector, to enable time-boxed and sector-specific analysis.
3. WHEN fewer than 10 completed trades exist (including zero), THE Analytics API SHALL return the available metrics (or zero/empty values when no trades exist) with a flag "insufficient_data": true in the response.
4. THE system SHALL pre-compute and cache analytics results for up to 1 hour to avoid recalculating on every request.

---

### Requirement 15: Trade Engine Isolation

**User Story:** As a developer, I want all trade business rules in a single isolated Trade Engine service, so that rule changes never require touching the scanner, cron jobs, or API layers.

#### Acceptance Criteria

1. THE Trade_Engine SHALL be implemented as a dedicated Python service module within the Django project, separate from views, serializers, and scanner logic.
2. THE Trade_Engine SHALL expose the following public methods: create_trade(scanner_result), activate_trade(trade_id, current_price), update_daily(trade_id, current_price), expire_trade(trade_id, reason), and get_trade_summary(trade_id).
3. WHEN any Trade_Engine method modifies a trade's status, THE Trade_Engine SHALL write a status change entry to a TradeChangeLog table capturing: trade_id, old_status, new_status, reason, and changed_at.
4. THE Trade_Engine SHALL validate all input parameters and raise a descriptive ValueError for invalid inputs (e.g., negative prices, unknown trade_id) before making any database writes.
5. THE Trade_Engine SHALL operate within a single database transaction per method call so that partial updates are never committed on failure.

---

### Requirement 16: Weekend Maintenance Jobs

**User Story:** As a developer, I want automated weekend maintenance to keep fundamentals, sector rankings, and data quality up to date, so that Monday's scan uses fresh reference data.

#### Acceptance Criteria

1. THE Weekend_Job SHALL refresh fundamental data (ROE, sales growth, debt-to-equity) for all active Nifty 500 stocks from the configured data source.
2. THE Weekend_Job SHALL recalculate and persist sector return rankings based on the most recent 20 trading days of StockDailyData.
3. THE Weekend_Job SHALL delete or archive TradeScanner records older than 90 calendar days to prevent unbounded table growth.
4. THE Weekend_Job SHALL generate and persist a weekly performance report covering: new trades created, trades activated, trades exited (profit vs stop loss), total portfolio P&L for the week.
5. WHEN the Weekend_Job completes all tasks, THE TelegramNotifier SHALL send a "📊 WEEKLY REPORT" summary message.
