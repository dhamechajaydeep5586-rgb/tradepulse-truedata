import os
import django
import sys

# Setup Django
sys.path.append('/Users/indianic/tradepulse-ai/backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from stocks.services.truedata_service import get_truedata_instance
from stocks.services.delta_hedge_service import get_nse_option_quote
from stocks.services.market_data_orchestrator import get_orchestrator

def debug_price():
    symbol = "BAJAJ-AUTO"
    strike = 9900
    option_type = "CE"
    expiry = "28APR2026"
    
    print(f"Debugging {symbol} {strike} {option_type} {expiry}...")
    
    svc = get_truedata_instance()
    if not svc:
        print("TrueData service not available")
        return

    instruments = svc.get_option_chain(symbol, expiry)
    print(f"Found {len(instruments)} instruments for {symbol}/{expiry}.")

    raw_strike = float(strike) * 100.0
    matches = [
        row for row in instruments
        if row.get('instrumenttype') in ['OPTSTK', 'OPTIDX'] and
           abs(float(row.get('strike', 0)) - raw_strike) < 1.0 and
           row.get('symbol', '').endswith(option_type)
    ]

    for m in matches:
        print(f"Match: Token={m['token']}, Symbol={m['symbol']}, Expiry={m['expiry']}, Strike={m['strike']}")
        quote = svc.get_live_price_by_token(m['token'], exchange='NFO')
        print(f"Quote: {quote}")

if __name__ == "__main__":
    debug_price()
