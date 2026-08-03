"""
Management command: scan_strangles

Runs the intraday short strangle scanner and prints a rich terminal report.
Does NOT modify the database or send Telegram alerts — pure terminal output.

Usage:
    python manage.py scan_strangles --force
    python manage.py scan_strangles --force --symbols ADANIENT,RELIANCE,ITC
    python manage.py scan_strangles --force --min-credit 2.0 --max-delta 0.30
"""

import sys
import time
import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


# ─── ANSI colors for terminal ────────────────────────────────────────
class C:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    MAGENTA = "\033[95m"
    RESET = "\033[0m"


RISK_EMOJI = {
    "Low": f"{C.GREEN}🟢 Low{C.RESET}",
    "Low-Medium": f"{C.GREEN}🟢 Low-Medium{C.RESET}",
    "Medium": f"{C.YELLOW}🟡 Medium{C.RESET}",
    "High": f"{C.RED}🔴 High{C.RESET}",
}

VOL_EMOJI = {
    "HIGH": f"{C.RED}🔴 High{C.RESET}",
    "MEDIUM": f"{C.YELLOW}🟡 Medium{C.RESET}",
    "LOW": f"{C.GREEN}🟢 Low{C.RESET}",
}


class Command(BaseCommand):
    help = "Scan NSE F&O stocks for intraday short strangle setups (terminal output only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Skip market-hours check.",
        )
        parser.add_argument(
            "--symbols",
            type=str,
            default="",
            help="Comma-separated list of symbols to scan (e.g. ADANIENT,RELIANCE,ITC).",
        )
        parser.add_argument(
            "--min-credit",
            type=float,
            default=1.5,
            help="Minimum net credit as %% of spot (default: 1.5).",
        )
        parser.add_argument(
            "--max-delta",
            type=float,
            default=0.35,
            help="Maximum allowed delta per leg (default: 0.35).",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            default=False,
            help="Show rejection reasons for every symbol.",
        )

    def handle(self, *args, **options):
        force = options["force"]
        symbols_str = options["symbols"]
        min_credit = options["min_credit"] / 100.0
        max_delta = options["max_delta"]
        verbose = options["verbose"]

        # Parse symbols
        symbols = None
        if symbols_str:
            symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]

        # ── Header ────────────────────────────────────────────────
        self._header()

        # ── Guard: Market hours ───────────────────────────────────
        if not force:
            from stocks.services.signal_utils import is_static_closed
            if is_static_closed("NSE"):
                self.stdout.write(
                    f"\n  {C.YELLOW}⏸  Market is closed (holiday/weekend).{C.RESET}"
                    f"\n  Use {C.BOLD}--force{C.RESET} to override.\n"
                )
                return

        start_time = time.time()

        # ── Step 1: Initialize Angel One ──────────────────────────
        self.stdout.write(f"\n  {C.DIM}[1/3]{C.RESET} Initializing Angel One session ... ", ending="")
        sys.stdout.flush()
        try:
            from stocks.services import truedata_service
            if not truedata_service.is_truedata_ready():
                from django.conf import settings
                config = getattr(settings, "TRUEDATA", {})
                truedata_service.initialize_truedata(
                    username=config.get("USERNAME"),
                    password=config.get("PASSWORD"),
                )
            self.stdout.write(f"{C.GREEN}OK{C.RESET}")
        except Exception as e:
            self.stdout.write(f"{C.YELLOW}WARN — {e}{C.RESET}")

        # ── Step 2: Run scanner ───────────────────────────────────
        sym_count = len(symbols) if symbols else 51
        self.stdout.write(f"  {C.DIM}[2/3]{C.RESET} Scanning {sym_count} symbols ...")

        from stocks.services.short_strangle_scanner import ShortStrangleScanner

        scanner = ShortStrangleScanner(
            min_credit_pct=min_credit,
            max_delta=max_delta,
        )

        def progress(i, total, symbol):
            bar_width = 40
            filled = int(bar_width * i / total)
            bar = "█" * filled + "░" * (bar_width - filled)
            pct = int(100 * i / total)
            sys.stdout.write(
                f"\r  {C.DIM}[2/3]{C.RESET} [{C.CYAN}{bar}{C.RESET}] "
                f"{i}/{total} {C.DIM}{symbol}{C.RESET}    "
            )
            sys.stdout.flush()

        results, rejections = scanner.scan(
            symbols=symbols,
            progress_callback=progress,
        )

        # Clear progress line
        sys.stdout.write("\r" + " " * 80 + "\r")

        pass_count = len(results)
        fail_count = len(rejections)
        self.stdout.write(
            f"  {C.GREEN}✅ {pass_count} setups passed all filters{C.RESET}"
        )
        self.stdout.write(
            f"  {C.RED}❌ {fail_count} rejected{C.RESET}"
        )

        # ── Step 3: Print results ─────────────────────────────────
        self.stdout.write(f"  {C.DIM}[3/3]{C.RESET} Generating report ...\n")

        if not results:
            self.stdout.write(
                f"\n  {C.YELLOW}No qualifying setups found.{C.RESET} "
                f"Try lowering --min-credit or raising --max-delta.\n"
            )
        else:
            self._print_results(results)

        # ── Rejections (verbose) ──────────────────────────────────
        if verbose and rejections:
            self._print_rejections(rejections)

        # ── Summary ───────────────────────────────────────────────
        elapsed = time.time() - start_time
        self._footer(results, elapsed)

    # ──────────────────────────────────────────────────────────────
    # OUTPUT FORMATTING
    # ──────────────────────────────────────────────────────────────

    def _header(self):
        from stocks.services.signal_utils import IST
        from datetime import datetime
        now = datetime.now(tz=IST)

        self.stdout.write("")
        self.stdout.write(f"  {C.CYAN}{'═' * 60}{C.RESET}")
        self.stdout.write(f"  {C.BOLD}📊 TradePulse AI | Intraday Short Strangle Scanner{C.RESET}")
        self.stdout.write(
            f"  {C.DIM}⏰ {now.strftime('%B %d, %Y')} | "
            f"{now.strftime('%I:%M %p IST')} | NRML Mode{C.RESET}"
        )
        self.stdout.write(f"  {C.CYAN}{'═' * 60}{C.RESET}")

    def _print_results(self, results):
        for i, s in enumerate(results):
            risk_str = RISK_EMOJI.get(s["risk"], s["risk"])
            vol_str = VOL_EMOJI.get(s["vol_bucket"], s["vol_bucket"])

            self.stdout.write(f"  {C.CYAN}{'─' * 60}{C.RESET}")
            self.stdout.write(
                f"  {C.BOLD}{i + 1}. {s['symbol']}{C.RESET}"
                f"  {C.DIM}(INTRADAY — NRML){C.RESET}"
                f"  {vol_str}"
            )
            self.stdout.write(f"  {C.CYAN}{'─' * 60}{C.RESET}")

            self.stdout.write(
                f"  Spot: {C.WHITE}₹{s['spot']:,.2f}{C.RESET}"
                f"    Lot: {C.WHITE}{s['lot_size']:,}{C.RESET}"
                f"    DTE: {C.WHITE}{s['dte']}{C.RESET}"
                f"    IV: {C.WHITE}{s['iv']:.1f}%{C.RESET}"
                f"    Expiry: {C.WHITE}{s['expiry']}{C.RESET}"
            )
            self.stdout.write(
                f"  {C.GREEN}SELL CE{C.RESET} {s['ce_strike']:,.0f}"
                f" @ {C.BOLD}₹{s['ce_premium']:.2f}{C.RESET}"
                f"  (+{s['ce_distance_pct']:.1f}%)"
                f"  Δ{s['ce_delta']:.2f}"
            )
            self.stdout.write(
                f"  {C.RED}SELL PE{C.RESET} {s['pe_strike']:,.0f}"
                f" @ {C.BOLD}₹{s['pe_premium']:.2f}{C.RESET}"
                f"  (-{s['pe_distance_pct']:.1f}%)"
                f"  Δ{s['pe_delta']:.2f}"
            )
            self.stdout.write(
                f"  {C.BOLD}Net Credit{C.RESET}: ₹{s['net_credit']:.2f}"
                f"  ({s['credit_pct']:.2f}% of spot)"
                f"    Credit/Lot: {C.BOLD}₹{s['credit_per_lot']:,.2f}{C.RESET}"
            )
            self.stdout.write(
                f"  Upper BE: ₹{s['upper_be']:,.2f}"
                f"  |  Lower BE: ₹{s['lower_be']:,.2f}"
                f"  |  POP: ~{s['pop']}%"
                f"  |  Risk: {risk_str}"
            )
            self.stdout.write(
                f"  Target: {C.GREEN}₹{s['target_profit']:.2f} profit{C.RESET}"
                f"  |  SL: {C.RED}₹{s['stop_loss']:.2f} loss{C.RESET}"
                f"  |  Hard Exit: {C.BOLD}3:30 PM IST{C.RESET}"
            )
            self.stdout.write("")

    def _print_rejections(self, rejections):
        self.stdout.write(f"\n  {C.DIM}{'─' * 60}{C.RESET}")
        self.stdout.write(f"  {C.YELLOW}Rejected Symbols:{C.RESET}")
        self.stdout.write(f"  {C.DIM}{'─' * 60}{C.RESET}")
        for sym, reason in rejections:
            self.stdout.write(f"  {C.DIM}❌ {sym}: {reason}{C.RESET}")

    def _footer(self, results, elapsed):
        self.stdout.write(f"\n  {C.CYAN}{'═' * 60}{C.RESET}")
        self.stdout.write(f"  {C.BOLD}Portfolio Summary (1 lot each){C.RESET}")
        self.stdout.write(f"  {C.CYAN}{'═' * 60}{C.RESET}")

        if results:
            total_credit = sum(r["credit_per_lot"] for r in results)
            low_count = sum(1 for r in results if r["risk"] in ("Low", "Low-Medium"))
            med_count = sum(1 for r in results if r["risk"] == "Medium")

            self.stdout.write(
                f"  Total Credit: {C.BOLD}₹{total_credit:,.2f}{C.RESET}"
            )
            self.stdout.write(
                f"  Setups: {C.BOLD}{len(results)}{C.RESET}"
                f"  |  {C.GREEN}Low Risk: {low_count}{C.RESET}"
                f"  |  {C.YELLOW}Medium Risk: {med_count}{C.RESET}"
            )
        else:
            self.stdout.write(f"  {C.DIM}No qualifying setups.{C.RESET}")

        self.stdout.write(
            f"  Done in {C.BOLD}{elapsed:.1f}s{C.RESET}"
        )
        self.stdout.write(f"  {C.CYAN}{'═' * 60}{C.RESET}\n")
