# Greeks-Based Dynamic Strangle Optimization Plan

This implementation plan outlines the structural upgrades to our option selling strategy, shifting from static short strangles to **Dynamic Delta-Hedged Strangles**. By actively managing Delta, Gamma, and Theta, we aim to eliminate large directional losses (such as the recent BAJAJFINSV move) and capture consistent daily decay to target your ₹5,000 daily goal.

## User Review Required

> [!IMPORTANT]
> **No Long Option Purchases**: As requested, we will NOT buy protection options (wings) to form an Iron Condor. The strategy remains 100% naked.
>
> **Dynamic Adjustments (Leg Rolling)**: The system will automatically close and roll the untested side of the strangle when the spot price trends strongly. This will realize small, controlled losses/gains on the rolled leg to collect new premium and protect the challenged leg.

---

## Proposed Changes

We will modify `delta_hedge_service.py` to incorporate live delta monitoring and automatic rebalancing during the 15-minute periodic scanner sweeps.

### 1. Shift Strangle Entry to 11:00 AM IST
* **Logic**: In `updater.py`, change the daily scan job from 10:00 AM to **11:00 AM IST**. 
* **Benefit**: Bypasses early morning premium inflation (high IV) and starts capturing rapid theta decay immediately.

### 2. Strict Gamma Expiry Guard
* **Logic**: Block any strangle signal generation if the days to expiry (DTE) is <= 3 days. Roll forward to the next liquid weekly/monthly expiry.
* **Benefit**: Prevents gamma explosions where option prices fluctuate wildly near the expiry date.

### 3. Dynamic Delta-Hedged Strangle Rebalancing
During the 15-minute updates:
1. Calculate the real-time Delta of the active Call and Put legs.
2. If the net Delta imbalance |Delta_CE - Delta_PE| >= 0.15:
   * **Identify the challenged leg** (the one with the higher delta, e.g., CE Delta = 0.40).
   * **Identify the safe leg** (the one with the decayed delta, e.g., PE Delta = 0.08).
   * **Action (The Roll)**: Close the safe leg and open a new strike on that side matching the delta of the challenged leg (e.g., selling a new PE at 0.40 Delta).
   * **Persist changes**: Update the database `metadata` with the new rolled strike and premium details, logging the transaction in `SignalChangeLog`.

---

## Verification Plan

### Automated Verification
* Run dry-run scans in the Django shell to simulate price breakouts and verify that the rebalancing engine triggers the correct rolls.
* Validate that DTE calculation correctly rolls over near-month contracts when DTE <= 3.

### Manual Verification
* Monitor the Telegram consolidated alerts to ensure rolled strikes and updated P&L calculations are displayed cleanly.

---

## Local Testing Procedure (Before Deploying to Production)

To verify the strangle generation and Greeks calculations locally tomorrow morning without writing to the database or triggering Telegram alerts:

1. **Activate your virtual environment** in your local terminal:
   ```bash
   source backend/venv/bin/activate
   ```
2. **Run the local strangle test script**:
   ```bash
   python backend/test_local_strangle.py
   ```
This script will query live Angel One API quotes, calculate the optimal `0.25 Delta` strangles for Crude Oil, Natural Gas, and all other assets, and display them directly in your terminal for your review.
