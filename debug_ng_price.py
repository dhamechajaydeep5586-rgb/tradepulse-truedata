import yfinance as yf

# Natural Gas April 2026 contract
ng = yf.Ticker('NG=F')
hist = ng.history(period='1d', interval='1m')

if not hist.empty:
    ng_usd = hist['Close'].iloc[-1]
    print(f"Yahoo NG=F: ${ng_usd:.4f}")
else:
    print("No NG data")

# USDINR rate
try:
    usdinr = yf.Ticker('INR=X')
    inr_rate = usdinr.fast_info.last_price
    print(f"USDINR: {inr_rate:.4f}")
    print(f"NG in INR: ₹{ng_usd * inr_rate:.2f}")
    print(f"Expected: ₹275.50")
    print(f"Difference: {((ng_usd * inr_rate - 275.50) / 275.50 * 100):.2f}%")
except Exception as e:
    print(f"Error: {e}")
