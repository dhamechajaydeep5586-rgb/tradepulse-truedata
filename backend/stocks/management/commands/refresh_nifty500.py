from __future__ import annotations

import io

import pandas as pd
import requests
from django.core.management.base import BaseCommand, CommandError

from stocks.services.live_signal_service import (
    _NIFTY500_URL,
    _persist_index_symbols,
)


class Command(BaseCommand):
    help = "Refresh the locally cached Nifty 500 constituent list from NSE."

    def handle(self, *args, **options):
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.nseindia.com/",
        }

        try:
            response = requests.get(_NIFTY500_URL, headers=headers, timeout=20)
            response.raise_for_status()
            df = pd.read_csv(io.StringIO(response.text))
        except Exception as exc:
            raise CommandError(f"Failed to download Nifty 500 CSV: {exc}") from exc

        if "Symbol" not in df.columns:
            raise CommandError("Unexpected NSE CSV format: missing 'Symbol' column")

        symbols_with_names = [
            (str(row["Symbol"]).strip(), str(row.get("Company Name", "")).strip())
            for _, row in df.iterrows()
            if pd.notna(row.get("Symbol"))
        ]

        if not symbols_with_names:
            raise CommandError("NSE CSV did not contain any Nifty 500 symbols")

        count = _persist_index_symbols(symbols_with_names)
        self.stdout.write(
            self.style.SUCCESS(
                f"Nifty 500 cache refreshed successfully with {count} active symbols."
            )
        )
