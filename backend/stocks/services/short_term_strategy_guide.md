# TradePulse AI: Upgraded Short-Term Swing Strategy Guide

This guide details the institutional multi-stage scanning pipeline used by the TradePulse system to identify, enter, and track high-probability swing setups.

---

## 1. Multi-Stage Screening Pipeline (Stock Selection)
A stock must pass through the following filters to be added to the watchlist:

| Stage | Rule | Technical Condition | Rationale |
| :--- | :--- | :--- | :--- |
| **1** | **Nifty Trend Filter** | Nifty Index > 50 EMA (Daily) | We only buy when the broader market is in a bullish regime. |
| **2** | **Universe** | Nifty 500 | Focuses on liquid mid-and-large cap stocks. |
| **3** | **Trend Alignment** | Price > 50 EMA > 200 EMA | Ensures strong, established long-term uptrends. |
| **4** | **Trend Strength** | ADX (14) > 25 | Confirms a strong, active trend exists (avoids sideways noise). |
| **5** | **Relative Strength** | Stock 20d Return > Nifty 20d Return | Focuses strictly on market leaders. |
| **6** | **Breakout / Momentum** | Within 5% of 52w High OR 20-day High Breakout | Confirms immediate momentum. |
| **7** | **Volume Confirmation** | Daily Volume > 1.5x of 20d Volume Average | Identifies strong institutional interest. |
| **8** | **Liquidity Filter** | Daily Volume >= 100,000 shares | Guarantees clean fills and narrow bid-ask spreads. |

---

## 2. Trade Execution Rules

### A. Calculate Entry Zone (Support levels)
The entry target is dynamically locked to the nearest major exponential moving average support:
* If the current price is less than 10% away from the **50 EMA**, then:
  $$\text{Entry Price} = 50\text{ EMA}$$
* Otherwise:
  $$\text{Entry Price} = 20\text{ EMA}$$

### B. ATR Stop Loss
The stop loss is calculated using Wilder's 14-day Average True Range (ATR):
$$\text{Stop Loss} = \text{Entry Price} - (2 \times \text{ATR})$$
*(Safety cap: Maximum Stop Loss width is limited to 10% from entry)*

### C. Target & Risk-Reward
Targets are calculated dynamically to achieve a minimum **2.5x Risk-Reward Ratio**:
$$\text{Target} = \text{Entry Price} + (2.5 \times \text{Risk Points})$$
*Where $\text{Risk Points} = \text{Entry Price} - \text{Stop Loss}$*

---

## 3. Pullback Reversal Candle Activation
When a watchlist stock is in `PENDING` state:
1. It must pull back to touch or drop below the calculated **Entry Price** (`Low <= Entry Price`).
2. **Reversal Trigger:** It will only transition to `ACTIVE` (triggering the Telegram buy alert) if the daily candle closes as **Bullish**:
   * $\text{Close} > \text{Open}$ (Green body)
   * $\text{Close} \ge \frac{\text{High} + \text{Low}}{2}$ (Closes in the upper 50% of the candle range)

---

## 4. Trailing Exit
Once a trade is `ACTIVE`, the stop loss is trailed using the daily 20 EMA:
* **EOD Trailing Exit:** If the daily candle closes below the **20 EMA**, the trade is closed immediately to protect profits/limit capital risk.
