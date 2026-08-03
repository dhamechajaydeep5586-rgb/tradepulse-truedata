"""
Download historical OHLCV into the local candle store.

This is the job that unblocks validation. The portfolio backtester and the walk-forward /
Monte Carlo framework can only measure a strategy against real history, and until this
has run there is no history stored to measure against.

It runs offline by design: Angel One enforces a ~1 request/second global lock and trips
a 5-minute circuit breaker on 403, so a full universe takes tens of minutes to hours.
Resumable — re-running after an interruption only fetches the missing chunks.

Examples:
    python manage.py backfill_candles --days 365
    python manage.py backfill_candles --symbols RELIANCE,TCS --days 90 --interval ONE_DAY
    python manage.py backfill_candles --days 365 --limit 20      # trial run
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError

from stocks.models import CandleBar
from stocks.services.candle_store import backfill_symbol
from stocks.services.signal_utils import IST


class Command(BaseCommand):
    help = "Backfill historical candles into the local store (unblocks backtesting)."

    def add_arguments(self, parser):
        parser.add_argument("--symbols", type=str, default="",
                            help="Comma-separated symbols. Default: the filtered trading universe.")
        parser.add_argument("--interval", type=str, default="FIVE_MINUTE",
                            help="ONE_MINUTE | FIVE_MINUTE | FIFTEEN_MINUTE | ONE_DAY")
        parser.add_argument("--days", type=int, default=365,
                            help="How far back to fetch (default 365).")
        parser.add_argument("--limit", type=int, default=0,
                            help="Only process the first N symbols — useful for a trial run.")
        parser.add_argument("--refetch", action="store_true",
                            help="Re-fetch chunks that already have stored bars.")

    def handle(self, *args, **opts):
        interval = opts["interval"].upper()
        days = int(opts["days"])
        started = time.time()

        from stocks.services.truedata_service import get_truedata_instance
        svc = get_truedata_instance()
        if not svc:
            raise CommandError(
                "Angel One service unavailable — cannot backfill. Check credentials "
                "and that the session authenticates."
            )

        # ── Resolve the symbol list ──────────────────────────────────────────────
        if opts["symbols"]:
            symbols = [s.strip().upper() for s in opts["symbols"].split(",") if s.strip()]
        else:
            from stocks.services.universe_service import get_trading_universe
            universe = get_trading_universe(svc)
            symbols = universe.get("symbols") or []
            self.stdout.write(
                f"Universe: {len(symbols)} symbols "
                f"({'liquidity-filtered' if universe.get('filtered') else 'UNFILTERED'})"
            )

        if not symbols:
            raise CommandError("No symbols resolved — nothing to backfill.")
        if opts["limit"]:
            symbols = symbols[: opts["limit"]]

        token_map = svc.get_token_map(symbols, exchange="NSE") or {}
        missing = [s for s in symbols if s not in token_map]
        if missing:
            self.stdout.write(self.style.WARNING(
                f"No Angel One token for {len(missing)} symbol(s): {', '.join(missing[:10])}"
            ))

        resolved = [(s, token_map[s]) for s in symbols if s in token_map]
        if not resolved:
            raise CommandError("No symbols could be resolved to tokens.")

        # ── Cost estimate, so an hours-long job is not a surprise ────────────────
        chunk_days = {"ONE_MINUTE": 25, "FIVE_MINUTE": 90,
                      "FIFTEEN_MINUTE": 180, "ONE_DAY": 1825}.get(interval, 90)
        chunks_each = max(1, -(-days // chunk_days))
        est_requests = len(resolved) * chunks_each
        est_minutes = est_requests * 1.1 / 60.0

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Backfill plan"))
        self.stdout.write(f"  symbols        : {len(resolved)}")
        self.stdout.write(f"  interval       : {interval}")
        self.stdout.write(f"  history        : {days} days")
        self.stdout.write(f"  requests (max) : ~{est_requests}  ({chunks_each} chunks/symbol)")
        self.stdout.write(f"  estimated time : ~{est_minutes:.0f} min at the 1 req/sec limit")
        self.stdout.write("")

        # ── Run ──────────────────────────────────────────────────────────────────
        total_written = total_requests = 0
        failures: list[str] = []

        for i, (sym, token) in enumerate(resolved, 1):
            try:
                res = backfill_symbol(
                    svc, sym, token, interval, days,
                    skip_existing=not opts["refetch"],
                )
                total_written += res["written"]
                total_requests += res["requests"]

                elapsed = time.time() - started
                rate = i / elapsed if elapsed > 0 else 0
                eta_min = ((len(resolved) - i) / rate / 60.0) if rate > 0 else 0
                self.stdout.write(
                    f"  [{i}/{len(resolved)}] {sym:<14} +{res['written']:>6} bars  "
                    f"({res['requests']} req, {res['skipped_chunks']} cached)  "
                    f"ETA {eta_min:.0f}m"
                )
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING(
                    "\nInterrupted — progress is saved. Re-run to resume."
                ))
                break
            except Exception as exc:
                failures.append(sym)
                self.stdout.write(self.style.ERROR(f"  [{i}] {sym} FAILED: {exc}"))

        # ── Report ───────────────────────────────────────────────────────────────
        mins = (time.time() - started) / 60.0
        stored = CandleBar.objects.filter(interval=interval).count()
        distinct = (CandleBar.objects.filter(interval=interval)
                    .values("symbol").distinct().count())
        oldest = (CandleBar.objects.filter(interval=interval)
                  .order_by("ts").values_list("ts", flat=True).first())

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Backfill complete"))
        self.stdout.write(f"  new bars written : {total_written:,}")
        self.stdout.write(f"  broker requests  : {total_requests:,}")
        self.stdout.write(f"  elapsed          : {mins:.1f} min")
        self.stdout.write(f"  store now holds  : {stored:,} bars across {distinct} symbols")
        if oldest:
            span = (datetime.now(tz=IST) - oldest).days
            self.stdout.write(f"  history depth    : {span} days (from {oldest.date()})")
        if failures:
            self.stdout.write(self.style.WARNING(
                f"  failed symbols   : {len(failures)} ({', '.join(failures[:10])})"
            ))

        # ── Honest depth assessment ──────────────────────────────────────────────
        # Must be measured as trading days with FULL symbol coverage, for THIS interval.
        # An earlier version compared the oldest timestamp across all intervals, so a
        # handful of daily bars from a trial run made a 3-month 5-min store look like a
        # year. A self-check that can report success on absent data is worse than none.
        from django.db.models import Count
        from django.db.models.functions import TruncDate

        by_day = list(
            CandleBar.objects.filter(interval=interval)
            .annotate(d=TruncDate("ts")).values("d")
            .annotate(syms=Count("symbol", distinct=True)).order_by("d")
        )
        well_covered = [r for r in by_day if r["syms"] >= max(1, int(distinct * 0.8))]
        depth_days = len(well_covered)

        self.stdout.write(f"  usable depth     : {depth_days} trading days "
                          f"with >=80% symbol coverage")
        self.stdout.write("")

        # Angel One serves progressively less intraday history the finer the interval;
        # it silently returns its maximum window rather than erroring on an older range.
        if depth_days >= 500:
            self.stdout.write(self.style.SUCCESS(
                "  Enough history for walk-forward validation."))
        elif depth_days >= 200:
            self.stdout.write(self.style.WARNING(
                "  Preliminary read only. A weak edge needs multiple years to separate\n"
                "  from noise (audit §8.3) — treat any Sharpe from this as indicative."))
        else:
            self.stdout.write(self.style.ERROR(
                f"  INSUFFICIENT for validation ({depth_days} days).\n"
                f"  Angel One caps intraday history per interval and silently returns\n"
                f"  its maximum window instead of erroring on an older request:\n"
                f"     FIVE_MINUTE ~68d | FIFTEEN_MINUTE ~134d | ONE_DAY ~245d\n"
                f"  A backtest on this measures whether the engine RUNS, not whether it\n"
                f"  makes money. Accumulate forward, use a coarser interval, or source\n"
                f"  history from another vendor before drawing conclusions."))
