import os
import django
import math
from datetime import datetime
from django.utils import timezone

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from stocks.services.delta_hedge_service import get_nse_option_strikes, estimate_iv, calculate_theoretical_premium
from stocks.services.market_data_orchestrator import get_orchestrator

orch = get_orchestrator()

def test_symbol(symbol):
    price_res = orch.get_price(symbol, exchange='NSE')
    spot_price = float(price_res.get('ltp', 0)) if price_res else 0.0
    print(f"\nSymbol: {symbol} | Spot: {spot_price}")
    strikes = get_nse_option_strikes(symbol, spot_price)
    print(f"Total strikes fetched: {len(strikes)}")
    
    # Resolve expiry
    today = timezone.now().date()
    all_expiries = sorted(set(s.get('expiry', '') for s in strikes), key=lambda x: datetime.strptime(x, "%d%b%Y").date() if x else datetime.max.date())
    valid_expiries = [e for e in all_expiries if (datetime.strptime(e, "%d%b%Y").date() - today).days > 0]
    target_expiry = valid_expiries[0] if valid_expiries else None
    print(f"Target expiry: {target_expiry}")
    
    if not target_expiry:
        return
        
    strikes = [s for s in strikes if s.get('expiry') == target_expiry]
    
    # Print CE and PE options with distance and LTP
    ce_options = []
    pe_options = []
    
    for row in strikes:
        sym_str = row.get('symbol', '')
        sv = float(row.get('strike', 0)) / 100.0
        if sym_str.endswith('CE') and sv > spot_price:
            res = orch.get_option_data(symbol, sv, 'CE', target_expiry, 'NFO')
            q = res[0] if isinstance(res, tuple) else res
            ltp = float(q.get('ltp', 0)) if q else 0.0
            dist = (sv - spot_price) / spot_price * 100
            ce_options.append((sv, ltp, dist))
        elif sym_str.endswith('PE') and sv < spot_price:
            res = orch.get_option_data(symbol, sv, 'PE', target_expiry, 'NFO')
            q = res[0] if isinstance(res, tuple) else res
            ltp = float(q.get('ltp', 0)) if q else 0.0
            dist = (spot_price - sv) / spot_price * 100
            pe_options.append((sv, ltp, dist))
            
    print("\nCE Options (OTM):")
    for sv, ltp, dist in sorted(ce_options)[:15]:
        print(f"  Strike: {sv} | LTP: {ltp} | Dist: {dist:.2f}%")
        
    print("\nPE Options (OTM):")
    for sv, ltp, dist in sorted(pe_options, reverse=True)[:15]:
        print(f"  Strike: {sv} | LTP: {ltp} | Dist: {dist:.2f}%")

test_symbol("SBIN")
test_symbol("NESTLEIND")
