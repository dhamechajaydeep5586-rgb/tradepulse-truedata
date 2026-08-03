import os
import django
import datetime

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from stocks.models import SignalHistory

today = datetime.date.today()
signals = SignalHistory.objects.filter(category='specialist', generated_at__date=today)

print("\n" + "="*80)
print(f"📊 TRADEPULSE SPECIALIST SIGNALS GENERATED TODAY ({today})")
print("="*80)

if not signals.exists():
    print("No signals found in the database for today.")
else:
    for idx, s in enumerate(signals, 1):
        legs = s.metadata.get('legs', []) if s.metadata else []
        ce_leg = next((l for l in legs if l.get('option_type') == 'CE'), {})
        pe_leg = next((l for l in legs if l.get('option_type') == 'PE'), {})
        
        ce_strike = ce_leg.get('strike', 'N/A')
        ce_price = ce_leg.get('sell_price', 'N/A')
        pe_strike = pe_leg.get('strike', 'N/A')
        pe_price = pe_leg.get('sell_price', 'N/A')
        
        print(f"{idx}. {s.symbol} — Spot: ₹{s.entry_price:,.2f} — Status: {s.status}")
        print(f"   • SELL CE {ce_strike} @ ₹{ce_price}")
        print(f"   • SELL PE {pe_strike} @ ₹{pe_price}")
        print(f"   • Credit: ₹{float(ce_price or 0) + float(pe_price or 0):.2f}\n")
print("="*80 + "\n")
