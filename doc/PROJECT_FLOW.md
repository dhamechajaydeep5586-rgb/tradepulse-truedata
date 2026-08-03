# Project Flow & Strategy Guide - TradePulse AI

This document explains the internal logic and "Truth Layer" flow for the three primary signal engines in TradePulse AI.

---

## 1. Global Pre-Flight: Total Holiday Silence
Before any service initializes or any API call is made, the system performs a **Static Pre-check**:
1.  **Static Calendar Check**: The system verifies the current date against a zero-dependency **Static Holiday Calendar**. If it's a Weekend or a National Holiday (e.g., Good Friday), the system enters **Silence Mode**.
2.  **Service Hibernation**: 
    - **Backend**: The Angel One WebSocket streamer, background updaters, and signal auditors skip all initialization to consume zero resources and produce zero log noise.
    - **Frontend**: The React dashboard detects the `CLOSED` status and automatically switches from a 1-minute polling interval to a **30-minute hibernation** interval.
3.  **Smart Wake-up**: For partial holidays (like MCX Morning-Closed), the system is programmed to automatically "Wake Up" at **17:00 IST** to catch the evening session.

---

## 2. The Market Heartbeat (Truth Layer)
Once the static check passes, the system monitors the **Live Heartbeat** to confirm exchange activity:
1.  **Native Pulse**: The system fetches the latest tick for the **Nifty Spot Index** (`99926000`) and **Crude Oil Future** (`CRUDEOIL`) directly from the Angel One pulse layer.
2.  **Activity Logic**: If the pulse is older than 15 minutes (Equity) or 30 minutes (MCX) during market hours, the system marks the segment as **CLOSED** (Heartbeat failure).
3.  **Result**: 100% reliance on Angel One data ensures that entries are based on executed prices, not delayed third-party feeds.

---

## 2. Intraday Equity Flow (The Sniper)
**Target**: Nifty 500 Stocks
**Interval**: 5-Minute Rescan (for Pending/Active signals) / 15-Minute Base

1.  **Selection**: Pulls symbols from the local `Stock` table (pre-filtered by **Relative Volume Surge** and **Momentum Thresholds**).
2.  **Historical Analysis**: Downloads 2 days of 15m candles from the **Angel One SmartAPI** to calculate:
    - **Volume Profile**: Identifying the Value Area High (VAH), Value Area Low (VAL), and Point of Control (POC/Value Area Bulk).
    - **JD Triple EMA**: Checking confluence between 9 EMA, 20 SMA, and 51 SMA on the 15m timeframe.
3.  **Signal Generation (The 3-Layer System)**:
    - **Layer 1 (Market Bias)**: Nifty Spot must be in alignment with the trade direction.
    - **Layer 2 (Volume/Momentum)**: Stock must have **1.5% Momentum** and **1.5x Relative Volume** spike.
    - **Layer 3 (Execution Zone)**: The entry is only triggered when the price enters an "Optimization Zone" (VAH/VAL/POC) identified by the Volume Profile.
4.  **Live Trigger**:
    - Uses the **Angel One WebSocket** as the "Truth Layer" for the exact `Last Traded Price` (LTP).
    - If `Live LTP` enters the zone and meets all filter criteria, a **BUY** or **SELL** signal is triggered.
4.  **Alert**: Signal is saved to the database and instantly pushed to the frontend via the API refresh.

---

## 3. Commodity Signal Flow (The Zone Trader)
**Target**: Crude Oil, Natural Gas, Gold, Silver (MCX)
**Interval**: 5-Minute Rescan (for Pending/Active signals) / 15-Minute Base

1.  **Contract Resolution**: Queries the Angel One Instrument Master to find the **active monthly futures contract** (handles monthly rolls automatically).
2.  **Volatility Analysis**:
    - Calculates the **ATR (Average True Range)** for the last 5 days.
    - Projects **ATR Support/Resistance Zones** (S1, S2, R1, R2).
3.  **Entry Logic**:
    - Monitors the **Live MCX Price** via WebSocket.
    - **Mean Reversion**: If the price hits the extreme **R2 (Resistance)** or **S2 (Support)** zone, it looks for a "Rejection Candle" to generate a counter-trend signal.
4.  **Persistence**: Updates the "Commodity Sniper" dashboard with entry, target, and trailing SL.

---

## 4. Option Selling Sniper (The Range Fade)
**Target**: Liquid Stock Options (F&O Stocks)
**Interval**: 15-Minute Base + Tick-by-Tick Options

1.  **Underlying Scan**: Monitors the **Nifty 500 stocks** that are currently in a "Range" or "Chop" day.
2.  **Trigger Detection**:
    - Monitors the **Underlying Stock LTP** (e.g., RELIANCE) via WebSocket.
    - Detects a "Touch" of the **VAH (Resistance)** or **VAL (Support)**.
3.  **Smart Strike Selection**:
    - Once the stock hits a zone, the system queries the **Instrument Master** for the exact CE/PE strikes.
    - **Logic**: Selects the strike that is **At-The-Money (ATM)** or slightly **Out-Of-The-Money (OTM)**.
4.  **Premium Execution**:
    - Dynamically subscribes to the **Option Scrip** on the WebSocket.
    - **Wait for Retrace**: The system doesn't enter immediately; it waits for the premium to "retrace" to a specific entry level for a better Risk/Reward ratio.
5.  **Monitoring**: monitors the **Premium SL and Target** tick-by-tick. If the premium hits the SL, the trade is terminated instantly.

---

## 5. Signal Lifecycle & Execution Flow (PENDING to ACTIVE)

The system manages every signal through a precise state machine to ensure professional execution.

### **Status 1: PENDING (The "Armed" State)**
- **When**: The scanner detects a high-conviction setup (VAH/VAL touch or Breakout).
- **Condition**: The current price hasn't reached the **Optimal Entry Level** yet.
- **Action**: The system "Arms" the sniper and starts watching the **Live Ticks** from the WebSocket. It is waiting for the perfect "Touch."

### **Status 2: ACTIVE (The "Filled" State)**
- **When**: The **Live LTP** from the WebSocket touches or crosses your **Entry Price**.
- **Transformation**: The system updates the status to **ACTIVE**. 
- **Notification**: A real-time alert is pushed to the dashboard (and WhatsApp if enabled).
- **Monitoring**: The system now enters "Strict Monitoring" mode, watching the **Stop Loss (SL)** and **Target 1/Target 2** tick-by-tick.

### **Status 3: CLOSED (The "Finished" State)**
- **Exit Logic**: A signal is closed only under these 3 conditions:
    1.  **Target Hit**: The price hits your profit level.
    2.  **Stop Loss Hit**: The price hits your protection level.
    3.  **Auto Square-Off**: The daily time cutoff is reached.

---

## 6. Auto Square-Off & Safety Cutoffs

To protect your capital and avoid broker penalties for overnight carry-forward, TradePulse enforces strict time rules for both NSE and MCX:

### **NSE / Equity / Options (Day Session)**
1.  **Intraday Cutoff (3:20 PM IST)**: 
    - At exactly 15:20, the system automatically closes **EVERY** open/pending intraday signal.
    - Status is updated to `CLOSED` with the reason `MARKET_CUTOFF`.
2.  **Option Selling Cutoff (3:15 PM IST)**:
    - Option positions are squared off at 15:15 to stay ahead of broker leverage triggers.
3.  **Scanning Halt (3:15 PM IST)**: 
    - The scanner stops looking for **NEW** signals after 3:15 PM.

### **MCX / Commodities (Evening Session)**
1.  **Commodity Cutoff (11:15 PM IST)**: 
    - At exactly 23:15, the system automatically closes **EVERY** open/pending MCX commodity signal.
    - Status is updated to `CLOSED` or `EXPIRED` with a notification for ₹ level at exit.
2.  **1-Hour Auto-Cancel Rule**: 
    - If a Commodity signal stays `PENDING` for more than **60 minutes** without being triggered (touched), the system automatically cancels it. This prevents "old" logic from firing on delayed price action.
3.  **Scanning Halt (11:15 PM IST)**: 
    - Commodity scanning halts for the night at 23:15 IST.

---

## Summary Table

| Signal Type | Primary Indicator | Live Price Source | Execution Style | Square-Off Time |
| :--- | :--- | :--- | :--- | :--- |
| **Intraday** | Volume Profile + 15m Zone | WebSocket (Angel One) | Momentum Breakout | **3:20 PM IST** |
| **Commodity** | ATR Zones (S2/R1) | WebSocket (Angel One) | Zone Reversal | **11:15 PM IST** |
| **Option Sniper** | Value Area (VAH/VAL) | WebSocket (Angel One) | Range-Fade / Selling | **3:15 PM IST** |

---

## 7. System Calibration & Selection Standards (Numeric Truths)

To maintain a "Sniper" level of conviction, the TradePulse AI engine enforces these strict numeric thresholds across all scanners:

### **Selection Filters (The High-Pass Filter)**
- **Momentum Threshold**: A stock must show at least **1.5% price movement** in the current session (Bias alignment).
- **Volume Surge**: A stock must have at least **1.5x Relative Volume** compared to its 5-day average.
- **Spread Tolerance**: Entry is only valid if the current LTP is within **0.15% (Equity)** or **ATR-based (MCX)** distance from the calculated Entry Point.

### **Risk/Reward Architecture**
- **Default R:R**: The system targets a minimum **1:2 Risk-to-Reward ratio**.
- **Stop Loss**: Set at **1.0x ATR** or below the recent swing low/VAL.
- **Target**: Set at **2.0x ATR** or the next major profile level (VAH/POC).

### **Scanning Frequency (Adaptive Polling)**
- **Idle Mode**: 15-minute 
- **Monitoring Mode**: When a signal is `PENDING` or `ACTIVE`, the scanner increases its internal rescan frequency to **5 minutes** to capture target hits with minimal slippage.
- **Holiday Mode**: 30-minute "Heartbeat" polling.

---

> [!IMPORTANT]
> **The Truth Layer**: TradePulse AI is **100% Angel One Native**. We have completely purged third-party dependencies like Yahoo Finance to eliminate data discrepancies. We use the **Angel One SmartAPI** for high-performance historical analysis and the **SmartWebSocketV2** for our internal State Engine and auto-square-off logic.
