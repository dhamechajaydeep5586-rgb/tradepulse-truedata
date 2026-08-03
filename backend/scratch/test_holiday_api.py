import requests
from datetime import datetime

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
})
session.get("https://www.nseindia.com", timeout=8)
resp = session.get("https://www.nseindia.com/api/holiday-master?type=trading", timeout=8)
if resp.status_code == 200:
    data = resp.json()
    cm_holidays = data.get('CM', [])
    for h in cm_holidays:
        dt = datetime.strptime(h['tradingDate'], "%d-%b-%Y").date()
        if dt.strftime('%Y-%m-%d') == '2026-06-26':
            print("Found today's holiday:", h)
