import os
import sys
import django
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from stocks.services.delta_hedge_service import get_lot_size

stocks = ["SHREECEM", "SOLARINDS", "CUMMINSIND", "MARUTI", "ULTRACEMCO", "APOLLOHOSP", "EICHERMOT", "SIEMENS", "HAL", "ASIANPAINT"]
for s in stocks:
    print(f"{s}: {get_lot_size(s, 'NFO')}")
