"""One-off backfill for historical SignalHistory rows with category='option_selling'.

Historical-only — not part of the live pipeline. No live engine writes
category='option_selling' anymore; the strangle-selling engine
(delta_hedge_service.py) writes category='specialist' exclusively (confirmed
2026-07 audit, see AUDIT_REMEDIATION_PLAN.md #3.4.3). This command exists only
to have backfilled strike/premium/expiry metadata onto pre-existing
option_selling rows so the legacy render branch in
SignalHistorySerializer.to_representation could display them correctly; it is
not scheduled and should not be run against current data.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from stocks.models import SignalHistory
from stocks.services.truedata_service import get_truedata_instance

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill option-selling rows with strike, premium CMP, and expiry from Angel One."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            default=False,
            help="Refresh all option-selling rows, not just rows missing metadata.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Maximum number of rows to process. Default processes all matching rows.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print what would be updated without saving changes.",
        )

    def handle(self, *args, **options):
        svc = get_truedata_instance()
        if not svc:
            raise CommandError("Angel One session is not available.")

        refresh_all = options["all"]
        limit = options["limit"]
        dry_run = options["dry_run"]

        queryset = SignalHistory.objects.filter(category="option_selling").order_by("-generated_at")
        if not refresh_all:
            queryset = queryset.filter(
                Q(strike_price__isnull=True)
                | Q(option_type__isnull=True)
                | Q(premium_cmp__isnull=True)
                | Q(option_expiry__isnull=True)
                | Q(option_expiry="")
            )

        rows = list(queryset[:limit] if limit > 0 else queryset)
        if not rows:
            self.stdout.write(self.style.WARNING("No option-selling rows need backfilling."))
            return

        updated = 0
        skipped = 0
        failed = 0

        self.stdout.write(
            self.style.NOTICE(
                f"Backfilling {len(rows)} option-selling rows from Angel One..."
            )
        )

        for idx, row in enumerate(rows, 1):
            option_type = self._infer_option_type(row)
            strike = self._infer_strike(row, svc)

            if not option_type or strike is None:
                skipped += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[{idx}/{len(rows)}] {row.symbol}: skipped (missing option type or strike)"
                    )
                )
                continue

            quote = svc.get_option_quote(row.symbol, float(strike), option_type)
            if not quote:
                failed += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[{idx}/{len(rows)}] {row.symbol}: no Angel One option quote for {strike} {option_type}"
                    )
                )
                continue

            premium_cmp = self._to_decimal(quote.get("ltp"))
            expiry = quote.get("expiry")

            if premium_cmp is None or premium_cmp <= 0:
                failed += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[{idx}/{len(rows)}] {row.symbol}: invalid premium from Angel One quote"
                    )
                )
                continue

            updates = {
                "strike_price": Decimal(str(strike)),
                "option_type": option_type,
                "premium_cmp": premium_cmp,
                "option_expiry": expiry,
            }

            if dry_run:
                self.stdout.write(
                    self.style.NOTICE(
                        f"[{idx}/{len(rows)}] {row.symbol}: would set strike={strike} option={option_type} premium={premium_cmp} expiry={expiry}"
                    )
                )
                updated += 1
                continue

            with transaction.atomic():
                for field, value in updates.items():
                    setattr(row, field, value)
                row.save(update_fields=list(updates.keys()))

            updated += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{idx}/{len(rows)}] {row.symbol}: strike={strike} option={option_type} premium={premium_cmp} expiry={expiry}"
                )
            )

        self.stdout.write("")
        self.stdout.write(self.style.NOTICE("Backfill summary:"))
        self.stdout.write(f"  Updated : {updated}")
        self.stdout.write(f"  Skipped : {skipped}")
        self.stdout.write(f"  Failed  : {failed}")

    @staticmethod
    def _infer_option_type(row: SignalHistory) -> str | None:
        candidates = [row.option_type, row.signal_type, row.reason]
        for candidate in candidates:
            if not candidate:
                continue
            text = str(candidate).upper()
            if "CE" in text:
                return "CE"
            if "PE" in text:
                return "PE"
        return None

    @staticmethod
    def _to_decimal(value) -> Decimal | None:
        try:
            if value is None:
                return None
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None

    def _infer_strike(self, row: SignalHistory, svc) -> Decimal | None:
        if row.strike_price is not None:
            return Decimal(str(row.strike_price))

        quote = svc.get_stock_quote(row.symbol)
        if not quote:
            return None

        ltp = quote.get("ltp")
        try:
            if ltp is None or float(ltp) <= 0:
                return None
            return Decimal(str(round(float(ltp), -2)))
        except (TypeError, ValueError, InvalidOperation):
            return None
