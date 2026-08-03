"""Seed the PromoterGroup map.

Why this exists: sector caps cannot prevent single-group concentration. In the live
long-term book, ADANIPORTS is classified Infrastructure and ADANIENT is Diversified, so a
2-per-sector rule admitted both — producing a two-position portfolio that was 100% one
promoter group, with shared financing, shared news flow and shared regulatory exposure.
Effective diversification was approximately one position.

Coverage is deliberately partial: only groups where cross-holdings create genuine
correlated risk. A symbol absent from this map is treated as its own group, which is the
safe default (it can never cause a false rejection).

Refresh:  python manage.py seed_promoter_groups
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from stocks.models import PromoterGroup

# group name -> symbols. Sourced from public promoter/holding-company relationships.
GROUPS: dict[str, list[str]] = {
    "Adani": [
        "ADANIENT", "ADANIPORTS", "ADANIPOWER", "ADANIGREEN", "ADANIENSOL",
        "ATGL", "AWL", "ACC", "AMBUJACEM", "NDTV", "SANGHIIND",
    ],
    "Tata": [
        "TCS", "TATAMOTORS", "TATASTEEL", "TITAN", "TATAPOWER", "TATACONSUM",
        "TATACHEM", "TATACOMM", "TATAELXSI", "TRENT", "VOLTAS", "INDHOTEL",
        "TATATECH", "TATAINVEST", "NELCO", "TRF", "AUTOIND",
    ],
    "Reliance": ["RELIANCE", "JIOFIN"],
    "Aditya Birla": [
        "HINDALCO", "GRASIM", "ULTRACEMCO", "ABCAPITAL", "ABFRL", "ABSLAMC",
        "ABREL", "ABLBL",
    ],
    "Bajaj": ["BAJFINANCE", "BAJAJFINSV", "BAJAJ-AUTO", "BAJAJHLDNG", "BAJAJHFL", "MUKANDLTD"],
    "Mahindra": ["M&M", "TECHM", "MAHLIFE", "MAHSCOOTER", "MAHINDCIE", "MMFSL", "MHRIL"],
    "JSW": ["JSWSTEEL", "JSWENERGY", "JSWINFRA", "JSWHL", "JSLHISAR", "JSL"],
    "Vedanta": ["VEDL", "HINDZINC"],
    "Larsen & Toubro": ["LT", "LTIM", "LTTS", "LTF", "LTFOODS"],
    "Murugappa": ["TUBEINVEST", "CHOLAFIN", "CGCL", "COROMANDEL", "EIDPARRY", "CARBORUNIV"],
    "HDFC": ["HDFCBANK", "HDFCLIFE", "HDFCAMC"],
    "ICICI": ["ICICIBANK", "ICICIGI", "ICICIPRULI"],
    "SBI": ["SBIN", "SBILIFE", "SBICARD"],
    "Godrej": ["GODREJCP", "GODREJPROP", "GODREJIND", "GODREJAGRO"],
    "Hinduja": ["ASHOKLEY", "HINDUJAGLOB", "GULFOILLUB"],
    "Piramal": ["PEL", "PPLPHARMA"],
    "Torrent": ["TORNTPHARM", "TORNTPOWER"],
    "Naveen Jindal": ["JINDALSTEL", "JINDALSAW"],
    "TVS": ["TVSMOTOR", "TVSHLTD", "SUNDRMFAST", "SUNDARMFIN"],
    "Kalyani": ["BHARATFORG", "BFUTILITIE", "BFINVEST"],
    "Zydus": ["ZYDUSLIFE", "ZYDUSWELL"],
    "Kotak": ["KOTAKBANK"],
    "Hero": ["HEROMOTOCO", "HFCL"],
    "RPG": ["CEAT", "KEC", "ZENSARTECH", "RPGLIFE"],
    "Shriram": ["SHRIRAMFIN", "SHRIPISTON"],
    "Emami": ["EMAMILTD", "EMAMIREAL"],
    "Dalmia": ["DALBHARAT", "DALMIASUG"],
    "Apollo": ["APOLLOHOSP", "APOLLOTYRE"],
}


class Command(BaseCommand):
    help = "Seed or refresh the promoter-group mapping used by portfolio concentration caps."

    def handle(self, *args, **options):
        created = updated = 0
        with transaction.atomic():
            for group, symbols in GROUPS.items():
                for sym in symbols:
                    _, was_created = PromoterGroup.objects.update_or_create(
                        symbol=sym.strip().upper(),
                        defaults={"group_name": group, "source": "curated"},
                    )
                    created += was_created
                    updated += not was_created

        total = PromoterGroup.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f"Promoter groups seeded: {created} created, {updated} updated, "
            f"{total} total across {len(GROUPS)} groups."
        ))

        # Show the groups currently represented in the live book, since that is the
        # concentration this control exists to bound.
        from stocks.models import SignalHistory
        live = SignalHistory.objects.filter(
            category="long_term", status=SignalHistory.Status.ACTIVE
        ).values_list("symbol", flat=True)
        if live:
            mapping = {
                r.symbol: r.group_name
                for r in PromoterGroup.objects.filter(symbol__in=list(live))
            }
            self.stdout.write("\nLive long-term book by promoter group:")
            for sym in live:
                self.stdout.write(f"  {sym:14} -> {mapping.get(sym, '(unmapped)')}")
