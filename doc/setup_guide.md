# TradePulse AI - Full Setup Guide

TradePulse AI is a high-performance trading signal engine built with Django (Backend) and React (Frontend). It uses TrueData (WebSocket + REST) for real-time and historical market data across Nifty 100/500. See `CLAUDE.md` and `doc/TRUEDATA_MIGRATION_PLAN.md` for the full architecture.

---

## 1. Prerequisites

Ensure you have the following installed on your system:
- **Python**: 3.12 or 3.13
- **Node.js**: 18.x or 20.x
- **PostgreSQL**: 14+ (Running on port 5432)
- **Git**

---

## 2. Backend Setup (Django)

### Step 1: Clone and Navigate
```bash
cd tradepulse-ai/backend
```

### Step 2: Create Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Install Dependencies
```bash
# Make sure your venv is active
./venv/bin/pip install -r requirements.txt
```

### Step 4: Environment Configuration
Create a `.env` file in the `backend/` directory:
```env
DEBUG=True
SECRET_KEY=your_django_secret_key
ALLOWED_HOSTS=localhost,127.0.0.1

# Database
DB_NAME=tradepulse_db
DB_USER=postgres
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5432

# TrueData
TRUEDATA_USERNAME=your_username
TRUEDATA_PASSWORD=your_password
TRUEDATA_WS_PORT=8084

# AI Insights
CLAUDE_API_KEY=your_anthropic_api_key

# WhatsApp Alerts (Optional)
WHATSAPP_ALERTS_ENABLED=false
WHATSAPP_ALERT_PHONE=91XXXXXXXXXX
```

### Step 5: Database Migrations
Make sure PostgreSQL is running and you've created the `tradepulse_db` database.
```bash
# Using the venv python
./venv/bin/python manage.py migrate
./venv/bin/python manage.py createsuperuser
```

### Step 6: Start the Server
```bash
./venv/bin/python manage.py runserver
```

---

## 3. Frontend Setup (React/Vite)

### Step 1: Navigate and Install
```bash
cd tradepulse-ai/frontend
npm install
```

### Step 2: Environment Configuration
Create a `.env` file in the `frontend/` directory:
```env
VITE_API_BASE_URL=http://127.0.0.1:8000/api
```

### Step 3: Start Development Server
```bash
npm run dev
```

---

## 4. Key Components Description

### **WebSocket Streamer**
- **Path**: `backend/stocks/services/truedata_streamer.py`
- **Function**: Maintains a persistent WebSocket connection to TrueData (`wss://push.truedata.in`).
- **Subscriptions**: Symbols are subscribed on demand as scans request them (see `truedata_service.get_bulk_quotes()`'s warm-up call) rather than a fixed Nifty 500 bootstrap.

### **Option Buying Engine**
- **Path**: `backend/stocks/services/option_buying_service.py`
- **Logic**: Buys near-ATM CE/PE on a confirmed VAH/VAL breakout with VWAP + ADX confirmation.

### **Strangle Selling Engine (specialist)**
- **Path**: `backend/stocks/services/delta_hedge_service.py`
- **Logic**: Deep-OTM/high-theta strangle selling, generated once daily at 10:45 AM IST.

### **Market Heartbeat & Holidays**
- **Path**: `backend/stocks/services/signal_utils.py`
- **Verification**: `get_market_status()` calls the live NSE API (`nseindia.com/api/marketStatus`) as the single source of truth, with a static calendar fallback — **never** a broker WebSocket pulse (see `CLAUDE.md`'s "Market Status" section for why).

---

## 5. Troubleshooting (Common Issues)

### 500 Internal Server Error (UnboundLocalError)
If you see crashes in `commodity_service.py`, ensure your `live_rows` fetching is correctly placed outside the `if/else` block. 
- *Check your codebase*: This was a known issue fixed on **2026-04-03**.

### Proxy / Connection Errors
If you see `AggregateError [ECONNREFUSED]`, ensure your **Backend Server** (Port 8000) is running before the Frontend attempts to fetch data.

### TrueData Login Failing
- Verify `TRUEDATA_USERNAME`/`TRUEDATA_PASSWORD` are correct and the subscription is active.
- Check logs for `[TRUEDATA] Throttling login attempts` — a prior failed login trips a 60s
  cooldown before the next attempt.

---

## 6. Maintenance Commands

- **Symbol resolution**: TrueData addresses symbols by name directly — there is no scrip master to refresh (see `CLAUDE.md`'s "Symbol addressing" note).
- **Clean Old Signals**: 
  ```bash
  ./venv/bin/python manage.py shell
  >>> from stocks.models import SignalHistory
  >>> SignalHistory.objects.filter(status='PENDING').delete()
  ```

---

> [!TIP]
> **Order of Operation**: Always run the **Backend** first. The Backend establishes the WebSocket session necessary for live price updates across all dashboards.
