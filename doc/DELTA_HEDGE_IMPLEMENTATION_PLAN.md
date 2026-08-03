# 🛡️ Delta Hedging — Option Selling Integration Plan

> **Strategy**: Short Strangle / Short Straddle on NSE Stock Options (NFO) + MCX NaturalGas Options  
> **Goal**: Protect directional positions using theta-decay (time value erosion) — zero directional guess, pure premium collection  
> **Status**: 📋 Ready for Implementation

---

## 📸 Reference Screenshot (What We're Building)

```
NATURALGAS  MCX  23 Apr 2026  260 CE  S  CF  -1 Lot (1 Lot = 1250)  16.5   18.15 (+9.01%)   -2,062.50 (-10.00%)
NATURALGAS  MCX  23 Apr 2026  260 PE  S  CF  -1 Lot (1 Lot = 1250)  11.2   10.20 (-18.40%)  +1,250.00 (+8.93%)
NATURALGAS  MCX  23 Apr 2026  270 PE  S  CF  -1 Lot (1 Lot = 1250)  18.5   15.45 (-15.80%)  +3,812.50 (+16.49%)
NATURALGAS  MCX  23 Apr 2026  280 CE  S  CF  -1 Lot (1 Lot = 1250)  10.8   10.05 (+7.49%)     +937.50 (+6.94%)
                                                                              Total G/L  →  +3,937.50 ✅
```

**Key Observations from Screenshot:**
- Exchange: MCX (NaturalGas options)
- Action: **S** = Sell (Short)
- Type: **CF** = Carry Forward (not intraday)
- Lot Size: 1 Lot = 1250 units
- Multiple strikes: 260, 270, 280 (both CE and PE)
- P&L: Green = profit (premium decayed), Red = loss (premium expanded)

---

## 🧠 Strategy Explained

### What is Short Strangle?
- **Sell OTM CE** (above spot) + **Sell OTM PE** (below spot)
- You collect premium from BOTH sides
- Profit when price stays between the two strikes (theta decay)
- Loss when price moves sharply beyond either strike

### Why This Works for Your System
| Your Signal | Hedge Action | Logic |
|---|---|---|
| BUY NaturalGas | Sell OTM CE + Sell OTM PE | Collect premium; if price rises moderately, CE loss offset by PE gain |
| SELL NaturalGas | Sell OTM CE + Sell OTM PE | Same strangle — theta works regardless of direction |
| BUY NSE Stock | Sell OTM CE + Sell OTM PE (NFO) | Stock options hedge the directional position |
| SELL NSE Stock | Sell OTM CE + Sell OTM PE (NFO) | Same — premium collection protects against slow moves |

### Risk Control Rules
- **Max Risk per trade**: 1–2% of capital
- **Strike Selection**: ATM ± 1 or 2 strikes (OTM for safety)
- **Lot Size**: Fixed (NATURALGAS = 1250, NSE stocks = from instrument master)
- **Expiry**: Nearest weekly (NSE) or monthly (MCX)
- **Exit Rule**: Exit if premium doubles (2x sell price = stop loss)

---

## 🏗️ Architecture

### Files to Create

#### 1. `backend/stocks/services/delta_hedge_service.py` *(New — Core Engine)*

**Functions:**

```python
def get_mcx_option_strikes(commodity: str, spot_price: float) -> list[dict]
```
- Scans Angel One instrument master for MCX option contracts
- Filters by: `exch_seg == "MCX"`, `name == "NATURALGAS"`, `instrumenttype == "OPTFUT"`
- Returns available strikes sorted by proximity to spot
- Example output: `[{"strike": 260, "expiry": "23APR2026"}, {"strike": 270, ...}]`

```python
def get_mcx_option_quote(commodity: str, strike: float, option_type: str, expiry: str) -> dict
```
- Resolves token from instrument master for the specific strike/expiry/type
- Calls `svc.get_live_price_by_token(token, exchange="MCX")`
- Returns: `{"ltp": 18.15, "change_pct": 9.01, "oi": ..., "volume": ...}`

```python
def get_nse_option_strikes(symbol: str, spot_price: float) -> list[dict]
```
- Scans instrument master for NFO option contracts
- Filters by: `exch_seg == "NFO"`, `name == symbol`, `instrumenttype == "OPTSTK"`
- Returns OTM strikes (1–2% away from spot)

```python
def get_nse_option_quote(symbol: str, strike: float, option_type: str) -> dict
```
- Uses existing `svc.get_option_quote(symbol, strike, option_type)` 
- Returns live premium from NFO

```python
def build_short_strangle(signal: SignalHistory, spot_price: float) -> list[dict]
```
- Core strategy builder
- For NATURALGAS: picks ATM strike + 1 OTM strike on each side
- For NSE stocks: picks 1–2% OTM CE and PE
- Returns list of legs: `[{strike, type, sell_premium, cmp, pnl, lot_size, expiry}, ...]`

```python
def calculate_pnl(sell_price: float, cmp: float, lot_size: int, lots: int = 1) -> dict
```
- `pnl = (sell_price - cmp) * lot_size * lots`
- Returns: `{"pnl": 3937.50, "pnl_pct": 16.49, "direction": "profit"}`

```python
def get_hedge_panel_data() -> dict
```
- Fetches all ACTIVE/PENDING signals from DB
- For each signal, calls `build_short_strangle()`
- Returns structured panel data for frontend

---

#### 2. `backend/stocks/views.py` *(Modified — Add 1 view)*

```python
class DeltaHedgeView(APIView):
    """GET /api/stocks/delta-hedge/ — Returns option selling hedge suggestions"""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        from stocks.services.delta_hedge_service import get_hedge_panel_data
        from django.core.cache import cache
        
        # Cache for 60 seconds (option premiums change frequently)
        cache_key = "delta_hedge_panel_60s"
        cached = cache.get(cache_key)
        if cached:
            return Response(cached)
        
        data = get_hedge_panel_data()
        cache.set(cache_key, data, timeout=60)
        return Response(data)
```

---

#### 3. `backend/stocks/urls.py` *(Modified — Register URL)*

```python
path('delta-hedge/', DeltaHedgeView.as_view(), name='delta-hedge'),
```

---

### Files to Create (Frontend)

#### 4. `frontend/src/components/DeltaHedgePanel.jsx` *(New — UI Panel)*

**UI Layout (matches your screenshot exactly):**

```
🛡️ Delta Hedge Panel — Option Selling Tracker        [🔄 Refresh]  [MCX Open 🟢]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Symbol      Exch  Expiry        Strike  Type  Lots  Sell@   CMP      Chg%     G/L
NATURALGAS  MCX   23 Apr 2026   260     CE    -1    16.50   18.15   +9.01%  -2,062.50
NATURALGAS  MCX   23 Apr 2026   260     PE    -1    11.20   10.20  -18.40%  +1,250.00
NATURALGAS  MCX   23 Apr 2026   270     PE    -1    18.50   15.45  -16.49%  +3,812.50
NATURALGAS  MCX   23 Apr 2026   280     CE    -1    10.80   10.05   +7.49%    +937.50
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                                                              Total G/L  +3,937.50 ✅
```

**Component Features:**
- ✅ Green G/L = profit (premium decayed = good for seller)
- ❌ Red G/L = loss (premium expanded = bad for seller)
- Live CMP polling every 60 seconds
- Lot size auto-displayed (1 Lot = 1250 for NATURALGAS)
- Separate sections: MCX NaturalGas | NSE Stocks
- Total G/L aggregated at bottom
- "Why this hedge?" expandable tooltip
- Loading skeleton while fetching

---

#### 5. `frontend/src/pages/Dashboard.jsx` *(Modified)*

Add `<DeltaHedgePanel />` below `<CommoditySignalsTable />`:

```jsx
{/* ── Delta Hedge Panel ── */}
<DeltaHedgePanel />
```

---

## 📊 API Response Structure

### `GET /api/stocks/delta-hedge/`

```json
{
  "timestamp": "2026-04-06T14:30:00Z",
  "market_status": "OPEN",
  "total_pnl": 3937.50,
  "total_pnl_pct": 12.5,
  "sections": [
    {
      "title": "MCX — NaturalGas Options",
      "exchange": "MCX",
      "underlying": "NATURALGAS",
      "spot_price": 265.0,
      "signal": "BUY",
      "legs": [
        {
          "symbol": "NATURALGAS",
          "exchange": "MCX",
          "expiry": "23 Apr 2026",
          "strike": 260,
          "option_type": "CE",
          "action": "SELL",
          "lots": -1,
          "lot_size": 1250,
          "sell_price": 16.50,
          "cmp": 18.15,
          "change_pct": 9.01,
          "pnl": -2062.50,
          "pnl_pct": -10.00,
          "status": "LOSS"
        },
        {
          "symbol": "NATURALGAS",
          "exchange": "MCX",
          "expiry": "23 Apr 2026",
          "strike": 260,
          "option_type": "PE",
          "action": "SELL",
          "lots": -1,
          "lot_size": 1250,
          "sell_price": 11.20,
          "cmp": 10.20,
          "change_pct": -18.40,
          "pnl": 1250.00,
          "pnl_pct": 8.93,
          "status": "PROFIT"
        }
      ],
      "section_pnl": 3937.50,
      "section_pnl_pct": 12.5
    },
    {
      "title": "NSE — Stock Options",
      "exchange": "NFO",
      "legs": []
    }
  ]
}
```

---

## 🔑 Strike Selection Logic

### MCX NaturalGas
```
Spot Price = 265
Strike Interval = 10 (NaturalGas uses 10-point intervals)
ATM Strike = 260 (round down to nearest 10)

Short Strangle Setup:
  Leg 1: Sell 260 CE (ATM Call)
  Leg 2: Sell 260 PE (ATM Put)
  Leg 3: Sell 270 CE (1 strike OTM Call) — optional
  Leg 4: Sell 250 PE (1 strike OTM Put) — optional
```

### NSE Stock Options (NFO)
```
Spot Price = 2950 (RELIANCE)
Strike Interval = 50 (most stocks use 50-point intervals)
OTM CE = 3000 (1.7% above spot)
OTM PE = 2900 (1.7% below spot)

Short Strangle Setup:
  Leg 1: Sell 3000 CE
  Leg 2: Sell 2900 PE
```

---

## ⚙️ Instrument Master Filtering

### For MCX NaturalGas Options
```python
# Filter from Angel One instrument master:
row["exch_seg"] == "MCX"
row["name"] == "NATURALGAS"
row["instrumenttype"] == "OPTFUT"  # MCX options are OPTFUT
row["expiry"] >= today  # Active contracts only
```

### For NSE Stock Options (NFO)
```python
# Filter from Angel One instrument master:
row["exch_seg"] == "NFO"
row["name"] == symbol  # e.g. "RELIANCE"
row["instrumenttype"] == "OPTSTK"  # NSE stock options
row["expiry"] >= today
```

---

## 📋 Implementation Checklist

### Backend
- [ ] Create `backend/stocks/services/delta_hedge_service.py`
  - [ ] `get_mcx_option_strikes()` — MCX NaturalGas strike resolver
  - [ ] `get_mcx_option_quote()` — Live MCX option premium fetcher
  - [ ] `get_nse_option_strikes()` — NSE stock option strike resolver
  - [ ] `get_nse_option_quote()` — Live NFO option premium fetcher
  - [ ] `build_short_strangle()` — Strategy builder (ATM + OTM legs)
  - [ ] `calculate_pnl()` — P&L calculator per leg
  - [ ] `get_hedge_panel_data()` — Aggregator for all active signals
- [ ] Add `DeltaHedgeView` to `backend/stocks/views.py`
- [ ] Register `delta-hedge/` URL in `backend/stocks/urls.py`

### Frontend
- [ ] Create `frontend/src/components/DeltaHedgePanel.jsx`
  - [ ] Table layout matching screenshot
  - [ ] Green/Red P&L coloring
  - [ ] Live CMP polling (60s interval)
  - [ ] Total G/L footer
  - [ ] Loading skeleton
  - [ ] "Why this hedge?" tooltip
- [ ] Add `<DeltaHedgePanel />` to `frontend/src/pages/Dashboard.jsx`

---

## 🚀 Upgrade Path (After Core is Stable)

| Phase | Feature | Description |
|---|---|---|
| **Phase 1** ✅ | Short Strangle | Sell CE + PE at ATM/OTM strikes |
| **Phase 2** | Protective Hedge | Buy far OTM CE/PE as insurance (defined risk) |
| **Phase 3** | Iron Condor | Add long OTM wings to cap max loss |
| **Phase 4** | Auto-Adjustment | If price moves 50% toward strike, roll the position |
| **Phase 5** | Greeks Display | Show Delta, Theta, Vega per leg |

---

## 💡 Pro Trading Mindset

```
Beginners:  Focus on profit
Professionals: Focus on RISK first

Short Strangle = Theta is your friend
Time decay works FOR you every day
You don't need to predict direction
You just need price to STAY in a range

NATURALGAS lot size = 1250 units
Sell 260 CE @ 16.5 → Collect ₹20,625 premium
Sell 260 PE @ 11.2 → Collect ₹14,000 premium
Total collected = ₹34,625 per strangle
```

---

## 📌 Notes

1. **No new DB migration needed** — hedge data is computed on-the-fly (premiums change every minute)
2. **Angel One instrument master** already has MCX option tokens — we just need to filter `instrumenttype == "OPTFUT"` for MCX
3. **Lot size** for NATURALGAS is already in `ANGEL_ONE_MCX_CONTRACTS` dict (`lot_size: 1250`)
4. **Existing `get_option_quote()`** in `angel_one_service.py` handles NFO stocks — we extend it for MCX
5. **Cache**: 60-second cache on hedge panel API to avoid hammering Angel One

---

*Document created: 06 Apr 2026*  
*Author: TradePulse AI System*  
*Version: 1.0*
