"""
Management command: run_daily_market_update

Runs the full TradePulse daily pipeline for a given date:

  1. Fetch global market data    → GlobalMarket model
  2. Calculate market bias       → GlobalMarket.market_bias
  3. Generate AI insight         → Insight model (via Claude API)
  4. Save Option Chain Snapshots → OptionChain model
  5. Log success

Usage:
  python manage.py run_daily_market_update
  python manage.py run_daily_market_update --date 2025-06-15
"""

import logging
import time
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the daily market analytics pipeline (Global Market & AI Insights)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default=None,
            help="Target date in YYYY-MM-DD format. Defaults to today.",
        )
        parser.add_argument(
            "--skip-ai",
            action="store_true",
            default=False,
            help="Skip AI insight generation.",
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

        # ── Step 1: Fetch global market data ─────────────────────
        gm_status = self._run_step(
            step=1,
            label="Fetch global market data",
            func=self._step_global_market,
            target_date=target_date,
        )

        # ── Step 2: Calculate market bias ────────────────────────
        bias = self._run_step(
            step=2,
            label="Calculate market bias",
            func=self._step_market_bias,
            target_date=target_date,
        )

        # ── Step 3: Generate AI insight ──────────────────────────
        if options["skip_ai"]:
            ai_status = "skipped (--skip-ai flag)"
            self.stdout.write(f"  [3/4] Generate AI insight ... {ai_status}")
        else:
            ai_status = self._run_step(
                step=3,
                label="Generate AI insight",
                func=self._step_ai_insight,
                target_date=target_date,
                critical=False,
            )

        # ── Step 4: Save Option Chain Snapshots ──────────────────
        oc_status = self._run_step(
            step=4,
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
        self.stdout.write(f"    Global market      : {gm_status}")
        self.stdout.write(f"    Market bias        : {bias}")
        self.stdout.write(f"    AI insight         : {ai_status}")
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
        self.stdout.write(f"  [{step}/4] {label} ...", ending=" ")
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
    def _step_global_market(target_date: date) -> str:
        from global_market.services import fetch_global_market_data
        gm = fetch_global_market_data(target_date)
        return f"OK (id={gm.pk})" if gm else "no data"

    @staticmethod
    def _step_market_bias(target_date: date) -> str:
        from global_market.services import calculate_market_bias_for_date
        bias = calculate_market_bias_for_date(target_date)
        return bias or "N/A"

    @staticmethod
    def _step_ai_insight(target_date: date) -> str:
        from insights.services import generate_daily_insight
        insight = generate_daily_insight(target_date)
        return f"generated (id={insight.pk})" if insight else "skipped"

    @staticmethod
    def _step_option_chain(target_date: date) -> str:
        from stocks.services.option_chain_service import get_option_chain
        try:
            get_option_chain("NIFTY")
            get_option_chain("BANKNIFTY")
            return "OK"
        except Exception as e:
            return f"error: {e}"
