import os
import django
import sys

sys.path.append('/Users/indianic/tradepulse-ai/backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.db import connection
with connection.cursor() as cursor:
    cursor.execute("ALTER TABLE stocks_marketholiday RENAME TO market_holidays;")
print("Table renamed successfully!")
