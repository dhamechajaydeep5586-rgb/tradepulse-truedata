"""
Management command: generate_strangle_signals

Runs a guaranteed daily short-strangle signal generation scan.
Designed to be triggered by the 10:00 AM IST APScheduler cron job.

This command:
  1. Clears the throttle cache so the scan always runs (even if called multiple times)
  2. Forces a synchronous scan across the Nifty50 specialist universe
  3. Sends a Telegram summary after signals are confirmed in the DB

Usage:
  python manage.py generate_strangle_signals
  python manage.py generate_strangle_signals --force         # Skip market-hours check
  python manage.py generate_strangle_signals --no-telegram   # Skip Telegram dispatch
"""

import logging
import time

from django.core.management.base import BaseCommand
from django.core.cache import cache

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Generate daily short-strangle signals at 10 AM IST (cron-safe, throttle-bypass)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Force scan even if market is closed (useful for testing).",
        )
        parser.add_argument(
            "--no-telegram",
            action="store_true",
            default=False,
            help="Skip sending the Telegram daily summary after scan.",
        )
        parser.add_argument(
            "--telegram-delay",
            type=int,
            default=30,
            help="Seconds to wait after scan before sending Telegram summary (default: 30s).",
        )

    def handle(self, *args, **options):
        force = options["force"]
        skip_telegram = options["no_telegram"]
        telegram_delay = options["telegram_delay"]

        self.stdout.write(self.style.NOTICE("=" * 55))
        self.stdout.write(self.style.NOTICE("  TradePulse AI — Daily Strangle Signal Generator"))
        self.stdout.write(self.style.NOTICE("=" * 55))

        # ── Guard: Market Hours Check ─────────────────────────────
        if not force:
            from stocks.services.signal_utils import is_static_closed
            if is_static_closed("NSE"):
                self.stdout.write(self.style.WARNING(
                    "  ⏸  Market is closed today (holiday/weekend). "
                    "Skipping scan. Use --force to override."
                ))
                return

        start_time = time.time()

        # ── Step 1: Initialize Angel One session ─────────────────
        self.stdout.write("  [1/4] Initializing Angel One session ...", ending=" ")
        try:
            from django.conf import settings
            from stocks.services import truedata_service

            if not truedata_service.is_truedata_ready():
                config = getattr(settings, "TRUEDATA", {})
                truedata_service.initialize_truedata(
                    username=config.get("USERNAME"),
                    password=config.get("PASSWORD"),
                )
            self.stdout.write(self.style.SUCCESS("OK"))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"WARN — {e} (continuing anyway)"))

        # ── Step 2: Clear throttle + stale signal cleanup ────────
        self.stdout.write("  [2/4] Resetting throttle and cleaning stale signals ...", ending=" ")
        try:
            # Delete the throttle cache key so the scan always runs at 10 AM
            cache.delete("delta_hedge_scanner_throttle_5m")
            # Also delete the 5s panel cache so we get fresh data
            cache.delete("delta_hedge_panel_live_5s")
            # Delete stale signal cleanup flag so it re-runs today
            cache.delete("stale_signal_cleanup_done")
            self.stdout.write(self.style.SUCCESS("OK (throttle cleared)"))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"WARN — {e}"))

        # ── Step 3: Run synchronous signal generation scan ───────
        self.stdout.write("  [3/4] Running synchronous strangle scan (all symbols) ...")
        try:
            from stocks.services.delta_hedge_service import get_hedge_panel_data

            panel = get_hedge_panel_data(action="generate", sync_scan=True)

            sections = panel.get("sections", [])
            active_count = len([s for s in sections if s.get("status") == "ACTIVE"])
            pending_count = len([s for s in sections if s.get("status") == "PENDING"])

            self.stdout.write(self.style.SUCCESS(
                f"  ✅ Scan complete — "
                f"{len(sections)} signals total "
                f"({active_count} ACTIVE, {pending_count} PENDING)"
            ))
            logger.info(
                "[10AM_SCAN] Scan completed: %d signals (%d ACTIVE, %d PENDING)",
                len(sections), active_count, pending_count
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  ❌ Scan failed: {e}"))
            logger.exception("[10AM_SCAN] Critical scan failure: %s", e)
            return

        # ── Step 4: Send Telegram daily summary ──────────────────
        if skip_telegram:
            self.stdout.write("  [4/4] Telegram summary skipped (--no-telegram)")
        else:
            self.stdout.write(
                f"  [4/4] Waiting {telegram_delay}s before sending Telegram summary ...",
                ending=" "
            )
            time.sleep(telegram_delay)
            try:
                from stocks.services.telegram_service import send_daily_picks_summary
                sent = send_daily_picks_summary()
                if sent:
                    self.stdout.write(self.style.SUCCESS("✅ Telegram summary sent"))
                    logger.info("[10AM_SCAN] Telegram daily summary dispatched successfully")
                else:
                    self.stdout.write(self.style.WARNING("⚠ Telegram summary returned False (no signals or disabled)"))
            except Exception as tg_e:
                self.stdout.write(self.style.WARNING(f"⚠ Telegram failed: {tg_e}"))
                logger.warning("[10AM_SCAN] Telegram daily summary failed: %s", tg_e)

        # ── Summary ───────────────────────────────────────────────
        elapsed = time.time() - start_time
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"  ✅ Done in {elapsed:.1f}s"
        ))
        self.stdout.write(self.style.NOTICE("=" * 55))
