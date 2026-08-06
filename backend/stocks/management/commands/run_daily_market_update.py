"""
Management command: run_daily_market_update

Runs the daily TradePulse analytics pipeline for a given date:

  1. Save Option Chain Snapshots → OptionChain model
  2. Log success

Usage:
  python manage.py run_daily_market_update
  python manage.py run_daily_market_update --date 2025-06-15
"""

import logging
import time
from datetime import date

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the daily market analytics pipeline (Option Chain Snapshots)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default=None,
            help="Target date in YYYY-MM-DD format. Defaults to today.",
        )

    def handle(self, *args, **options):
        target_date = self._resolve_date(options["date"])
        start_time = time.time()

        self.stdout.write("")
        self.stdout.write(self.style.NOTICE(f"{'=' * 50}"))
        self.stdout.write(self.style.NOTICE(f"  TradePulse AI — Daily Pipeline (Stats Only)"))
        self.stdout.write(self.style.NOTICE(f"  Date: {target_date}"))
        self.stdout.write(self.style.NOTICE(f"{'=' * 50}"))
        self.stdout.write("")

        logger.info("=== Starting daily pipeline for %s ===", target_date)

        # ── Step 1: Save Option Chain Snapshots ──────────────────
        oc_status = self._run_step(
            step=1,
            label="Save Option Chain Snapshots",
            func=self._step_option_chain,
            target_date=target_date,
            critical=False,
        )

        # ── Summary & logging ────────────────────────────
        elapsed = time.time() - start_time

        self.stdout.write("")
        self.stdout.write(self.style.NOTICE("  Summary:"))
        self.stdout.write(f"    Target date        : {target_date}")
        self.stdout.write(f"    Option snapshots   : {oc_status}")
        self.stdout.write(f"    Elapsed time       : {elapsed:.1f}s")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"  Daily pipeline completed successfully for {target_date}."
        ))
        self.stdout.write("")

    @staticmethod
    def _resolve_date(date_str: str | None) -> date:
        if date_str:
            try:
                return date.fromisoformat(date_str)
            except ValueError:
                raise CommandError(f"Invalid date format: '{date_str}'. Use YYYY-MM-DD.")
        return date.today()

    def _run_step(self, step: int, label: str, func, target_date: date, critical: bool = True):
        self.stdout.write(f"  [{step}/1] {label} ...", ending=" ")
        try:
            result = func(target_date)
            display = str(result)
            self.stdout.write(self.style.SUCCESS(display))
            return result
        except Exception as exc:
            if critical:
                self.stdout.write(self.style.ERROR(f"FAILED"))
                logger.exception("Step %d (%s) failed for %s", step, label, target_date)
                raise CommandError(f"Step {step} ({label}) failed: {exc}") from exc
            else:
                msg = f"failed ({exc})"
                self.stdout.write(self.style.WARNING(msg))
                return msg

    @staticmethod
    def _step_option_chain(target_date: date) -> str:
        from stocks.services.option_chain_service import get_option_chain
        try:
            get_option_chain("NIFTY")
            get_option_chain("BANKNIFTY")
            return "OK"
        except Exception as e:
            return f"error: {e}"
