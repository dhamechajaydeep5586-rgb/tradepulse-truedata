import os
import django
import logging

# Configure logging to stdout
logging.basicConfig(level=logging.INFO)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from stocks.services.delta_hedge_service import get_hedge_panel_data
from stocks.models import SignalHistory

print("=== DELETING TODAY'S SPECIALIST SIGNALS AND THROTTLE FOR A CLEAN TEST ===")
from django.core.cache import cache
cache.delete("delta_hedge_scanner_throttle_5m")
cache.delete("delta_hedge_panel_live_5s")

from django.utils import timezone
from zoneinfo import ZoneInfo
IST = ZoneInfo("Asia/Kolkata")
today = timezone.now().astimezone(IST).date()
deleted = SignalHistory.objects.filter(
    category='specialist',
    generated_at__date=today
).delete()
print(f"Deleted {deleted} existing specialist signals for today to allow fresh generation.")

print("\n=== RUNNING SPECIALIST GENERATION SCAN ===")
try:
    panel = get_hedge_panel_data(action="generate", sync_scan=True)
    print("\n=== SCAN COMPLETED SUCCESSFULLY ===")
    print(f"Market Status: {panel.get('market_status')}")
    print(f"Total Sections/Signals: {len(panel.get('sections', []))}")
    
    # Check what signals were created in DB
    new_sigs = SignalHistory.objects.filter(category='specialist', generated_at__date=today)
    print(f"\nSignals in DB for today ({today}): {new_sigs.count()}")
    for s in new_sigs:
        print(f"  - {s.symbol} | Status: {s.status} | Entry: {s.entry_price}")
except Exception as e:
    print(f"\n=== SCAN FAILED ===")
    import traceback
    traceback.print_exc()
