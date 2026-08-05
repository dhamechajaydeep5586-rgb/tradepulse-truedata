"""
Replay the intraday strategy over stored candle history and report results.

IMPORTANT — read the output's "sample size" section before drawing any conclusion.
At the depth Angel One's intraday endpoints currently allow (~134 trading days for
15-min bars), this run is a SMOKE TEST: it proves the pipeline executes correctly end
to end on real data. It is not a statistically meaningful performance claim — see
doc/INSTITUTIONAL_AUDIT_INTRADAY.md §8.3 for why.

Usage:
    python manage.py run_historical_backtest
    python manage.py run_historical_backtest --interval FIVE_MINUTE --limit 30
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from stocks.models import CandleBar
from stocks.services.candle_store import load_bars
from stocks.services.signal_utils import IST


class Command(BaseCommand):
    help = "Replay the intraday strategy over locally stored candles and backtest it."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=str, default="FIFTEEN_MINUTE")
        parser.add_argument("--symbols", type=str, default="",
                            help="Comma-separated symbols to replay, e.g. RELIANCE,TCS. "
                                 "Takes priority over --limit.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Only replay the first N symbols (trial run).")
        parser.add_argument("--threshold", type=float, default=None,
                            help="Override the ranking score threshold (default 65).")
        parser.add_argument("--min-target-multiple", type=float, default=None,
                            help="Override MIN_TARGET_COST_MULTIPLE (default 3.0) — "
                                 "raise this to test whether requiring a bigger target "
                                 "relative to cost turns the strategy net-positive.")
        parser.add_argument("--exclude-trigger", type=str, default="",
                            help="Comma-separated substrings — drop any candidate whose "
                                 "reason contains one, BEFORE ranking, so other triggers "
                                 "compete for the freed slot instead of it just going unfilled.")
        parser.add_argument("--neutral-regime", action="store_true",
                            help="Use the old permissive placeholder regime instead of "
                                 "the real historical reconstruction — for isolating how "
                                 "much of a result the regime filter itself accounts for.")

    def handle(self, *args, **opts):
        interval = opts["interval"].upper()
        started = time.time()

        # Patched at the module level because _build_intraday_candidate reads this
        # constant directly rather than taking it as a parameter — same code path the
        # live engine uses, so this experiment is testing the real gate, not a copy of it.
        if opts["min_target_multiple"] is not None:
            import stocks.services.intraday_service as _isvc
            _isvc.MIN_TARGET_COST_MULTIPLE = opts["min_target_multiple"]
            self.stdout.write(self.style.WARNING(
                f"   MIN_TARGET_COST_MULTIPLE overridden to "
                f"{opts['min_target_multiple']} (default 3.0) for this run only."
            ))

        if opts["symbols"]:
            requested = [s.strip().upper() for s in opts["symbols"].split(",") if s.strip()]
            stored = set(
                CandleBar.objects.filter(interval=interval, symbol__in=requested)
                .values_list("symbol", flat=True).distinct()
            )
            symbols = [s for s in requested if s in stored]
            missing = [s for s in requested if s not in stored]
            if missing:
                self.stdout.write(self.style.WARNING(
                    f"   No stored {interval} bars for: {', '.join(missing)}"))
        else:
            symbols = list(
                CandleBar.objects.filter(interval=interval)
                .values_list("symbol", flat=True).distinct().order_by("symbol")
            )
            if opts["limit"]:
                symbols = symbols[: opts["limit"]]

        if not symbols:
            raise CommandError(
                f"No stored {interval} bars for the requested symbol(s) — "
                f"run backfill_candles first."
            )

        self.stdout.write(self.style.MIGRATE_HEADING("1. Loading stored bars"))
        from datetime import datetime, timedelta
        start = datetime.now(tz=IST) - timedelta(days=400)

        bars_by_symbol = {}
        for sym in symbols:
            df = load_bars(sym, interval, start)
            if len(df) >= 40:
                bars_by_symbol[sym] = df
        self.stdout.write(f"   {len(bars_by_symbol)}/{len(symbols)} symbols have enough bars")

        if not bars_by_symbol:
            raise CommandError("No symbol has enough stored bars to replay.")

        # ── 1b. Reconstruct the real historical regime (causal, no lookahead) ────────
        self.stdout.write(self.style.MIGRATE_HEADING("1b. Reconstructing historical regime"))
        from stocks.services.trading_engine.historical_regime import (
            build_breadth_series, build_nifty_regime_series,
        )

        nifty_bars = load_bars("NIFTY50", interval, start)
        regime_series = None
        if not opts.get("neutral_regime") and len(nifty_bars) >= 100:
            breadth = build_breadth_series(bars_by_symbol)
            regime_series = build_nifty_regime_series(nifty_bars, breadth)
            dist = regime_series["directional_bias"].value_counts().to_dict()
            self.stdout.write(f"   NIFTY bars: {len(nifty_bars)}  |  "
                              f"directional_bias distribution: {dist}")
            self.stdout.write(
                "   Real regime reconstructed — momentum/mean-reversion trigger "
                "families are now gated exactly as the live engine would, and the "
                "VA-Rejection directional gate uses the real historical index bias "
                "instead of a permissive SIDEWAYS placeholder."
            )
        else:
            self.stdout.write(self.style.WARNING(
                "   No NIFTY50 history stored (or --neutral-regime set) — falling back "
                "to a permissive placeholder. Run backfill for symbol=NIFTY50, "
                "symbol=NIFTY 50 first for a real regime filter."
            ))

        # ── 2. Replay the strategy over history (no lookahead) ───────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("2. Replaying signal logic"))
        from stocks.services.trading_engine.replay import replay_symbol, rank_per_timestamp
        from stocks.services.shared.universe import get_sector_map

        sectors = get_sector_map()
        all_candidates = []
        for i, (sym, df) in enumerate(bars_by_symbol.items(), 1):
            liq = {"adv_inr": 5e8, "daily_vol_pct": 1.5}  # store has no ADV history yet
            cands = replay_symbol(sym, df, liquidity=liq, sector=sectors.get(sym, "UNKNOWN"),
                                  regime_lookup=regime_series)
            all_candidates.extend(cands)
            if i % 40 == 0:
                self.stdout.write(f"   {i}/{len(bars_by_symbol)} symbols replayed, "
                                  f"{len(all_candidates)} raw candidates so far")

        self.stdout.write(f"   {len(all_candidates)} raw candidates across "
                          f"{len(bars_by_symbol)} symbols")

        if not all_candidates:
            self.stdout.write(self.style.WARNING(
                "   No triggers fired anywhere in the stored history. Nothing to backtest."
            ))
            return

        # ── 3. Cross-sectional ranking, cycle by cycle ───────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("3. Ranking + selection"))
        if len(bars_by_symbol) < 5:
            self.stdout.write(self.style.WARNING(
                f"   Only {len(bars_by_symbol)} symbol(s) — the ranking model scores "
                f"each candidate by percentile AGAINST OTHER CANDIDATES at the same "
                f"moment. With so few symbols, most factors can't differentiate and "
                f"collapse to a mid-range value, capping the score well under the "
                f"usual 65 cutoff regardless of trade quality. Effectively bypassing "
                f"ranking with --threshold 0 for a small symbol count."
            ))
            if opts["threshold"] is None:
                opts["threshold"] = 0.0
        # regime_service is now a deprecated shim over stocks.services.shared.regime;
        # its __all__ excludes underscore-prefixed names, so the private helper this
        # needs must be imported from the real module rather than the shim.
        from stocks.services.shared.regime import RegimeState, _resolve_permissions

        # Fallback object only — every candidate now carries its OWN directional_bias
        # from the real historical reconstruction (see step 1b above and
        # _alignment_score in shared/ranking.py, which prefers the per-candidate value).
        # This placeholder is only read if regime reconstruction was skipped
        # (--neutral-regime, or no NIFTY50 history stored).
        neutral_regime = _resolve_permissions(RegimeState())

        excluded_triggers = [s.strip() for s in opts["exclude_trigger"].split(",") if s.strip()]
        if excluded_triggers:
            before = len(all_candidates)
            all_candidates = [
                c for c in all_candidates
                if not any(x in c.get("reason", "") for x in excluded_triggers)
            ]
            self.stdout.write(self.style.WARNING(
                f"   Excluded triggers {excluded_triggers}: "
                f"{before} -> {len(all_candidates)} candidates"
            ))

        universe_stats = {s: {"adv_inr": 5e8, "daily_vol_pct": 1.5} for s in bars_by_symbol}
        signals_df = rank_per_timestamp(
            all_candidates, neutral_regime, universe_stats,
            max_per_cycle=5, threshold=opts["threshold"],
        )
        self.stdout.write(f"   {len(signals_df)} signals survived ranking + threshold "
                          f"(of {len(all_candidates)} raw)")

        if signals_df.empty:
            self.stdout.write(self.style.WARNING(
                "   Nothing cleared the ranking threshold. Nothing to backtest.\n"
                "   This itself is informative: it means the strategy, as currently\n"
                "   tuned, found no setup worth taking in this sample."
            ))
            return

        # ── 4. Portfolio backtest with real costs ────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("4. Running portfolio backtest"))
        from stocks.services.trading_engine.portfolio_backtest import (
            run_portfolio_backtest, BacktestConfig,
        )

        bars_dict = {sym: df for sym, df in bars_by_symbol.items()}
        result = run_portfolio_backtest(signals_df, bars_dict, BacktestConfig())
        m = result.metrics

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"   {result.summary()}"))
        self.stdout.write("")
        self.stdout.write(f"   trades          : {m.get('n_trades', 0)}")
        self.stdout.write(f"   win rate        : {m.get('win_rate', 0):.1%}")
        self.stdout.write(f"   gross P&L       : Rs.{m.get('gross_pnl', 0):,.0f}")
        self.stdout.write(f"   total costs     : Rs.{m.get('total_costs', 0):,.0f}")
        self.stdout.write(f"   net P&L         : Rs.{m.get('net_pnl', 0):,.0f}")
        self.stdout.write(f"   cost drag       : {m.get('cost_drag_pct', 0):.3f}% of equity")
        self.stdout.write(f"   profit factor   : {m.get('profit_factor', 0):.2f}")
        self.stdout.write(f"   expectancy/trade: Rs.{m.get('expectancy', 0):,.2f}")
        self.stdout.write(f"   max drawdown    : {m.get('max_drawdown_pct', 0):.2f}%")
        self.stdout.write(f"   Sharpe (naive)  : {m.get('sharpe', 0):.2f}")
        self.stdout.write(f"   exit breakdown  : {m.get('exit_breakdown', {})}")

        # ── 4b. Why did trades time out? ──────────────────────────────────────────
        # Distinguishes "price got close to target but reversed" from "price barely
        # moved at all" — those need different fixes (entry timing vs a wider target),
        # and the aggregate win/loss numbers alone can't tell them apart.
        timeouts = [t for t in result.trades if t.exit_reason == "TIME_STOP"]
        if timeouts:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("4b. Time-out diagnosis"))
            self.stdout.write(f"   {len(timeouts)} of {len(result.trades)} trades "
                              f"timed out ({len(timeouts)/len(result.trades):.0%})")

            mfe = [t.mfe_pct_of_target for t in timeouts]
            barely_moved = sum(1 for x in mfe if x < 0.25)
            got_close = sum(1 for x in mfe if x >= 0.75)
            self.stdout.write(f"   avg max favorable move reached: "
                              f"{sum(mfe)/len(mfe):.0%} of the distance to target")
            self.stdout.write(f"   barely moved at all (<25% of the way there): "
                              f"{barely_moved} ({barely_moved/len(timeouts):.0%})")
            self.stdout.write(f"   got close but reversed (>=75% of the way there): "
                              f"{got_close} ({got_close/len(timeouts):.0%})")

            by_strategy: dict = {}
            for t in timeouts:
                by_strategy.setdefault(t.strategy_reason or "(unknown)", []).append(t.mfe_pct_of_target)
            self.stdout.write("")
            self.stdout.write("   by trigger type:")
            for reason, vals in sorted(by_strategy.items(), key=lambda x: -len(x[1])):
                self.stdout.write(f"     {reason:<32} {len(vals):>5} timeouts, "
                                  f"avg reached {sum(vals)/len(vals):.0%} of target")

        # ── 5. Statistical validation — honesty check on the numbers above ───────
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("5. Statistical validity of the above"))
        from stocks.services.trading_engine.validation import (
            monte_carlo_drawdown, deflated_sharpe_ratio, sharpe_standard_error,
            minimum_track_record_length,
        )

        n_trades = m.get("n_trades", 0)
        if n_trades >= 10:
            pnls = [t.net_pnl for t in result.trades]
            mc = monte_carlo_drawdown(pnls, n_sims=2000, initial_equity=500_000.0)
            self.stdout.write(
                f"   Monte Carlo (2000 resamples of this trade sequence):"
            )
            self.stdout.write(f"     median max DD : {mc['median_max_dd_pct']:.1f}%")
            self.stdout.write(f"     worst-case DD : {mc['worst_max_dd_pct']:.1f}%")
            self.stdout.write(f"     P(loss)       : {mc['prob_of_loss']:.1%}")

            sharpe = m.get("sharpe", 0.0)
            years = 133 / 250.0
            se = sharpe_standard_error(sharpe, years)
            mtrl = minimum_track_record_length(sharpe) if sharpe > 0 else float("inf")
            self.stdout.write("")
            self.stdout.write(f"   Sharpe {sharpe:.2f} over ~{years:.2f} years "
                              f"has SE +/-{se:.2f}")
            self.stdout.write(f"   -> cannot distinguish this Sharpe from ZERO at this "
                              f"sample size" if se > abs(sharpe) else
                              f"   -> marginally distinguishable from zero")
            if mtrl < float("inf"):
                self.stdout.write(f"   Years needed to confirm this Sharpe > 0 at 95%: "
                                  f"{mtrl:.0f}")

            dsr = deflated_sharpe_ratio(sharpe, n_trials=1, n_obs=n_trades)
            self.stdout.write(f"   Deflated Sharpe probability (1 trial): "
                              f"{dsr.get('deflated_sharpe_probability', 0):.3f}")
        else:
            self.stdout.write(self.style.WARNING(
                f"   Only {n_trades} trades — too few for any statistical statement "
                f"(Monte Carlo/Sharpe would be noise)."
            ))

        # ── 6. The verdict this whole exercise exists to produce ─────────────────
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("VERDICT"))
        elapsed = time.time() - started
        mult = opts["min_target_multiple"] if opts["min_target_multiple"] is not None else 3.0
        self.stdout.write(f"   Completed in {elapsed:.0f}s. Pipeline runs end-to-end on "
                          f"real data — replay -> rank -> backtest -> validate all executed.")
        self.stdout.write(f"   Cost-gate multiple used: {mult}x round-trip cost "
                          f"(default is 3.0x).")
        self.stdout.write(f"   Regime filter: "
                          f"{'REAL historical reconstruction' if regime_series is not None else 'permissive placeholder (no NIFTY50 history / --neutral-regime)'}")
        self.stdout.write("")
        self.stdout.write(self.style.ERROR(
            "   THIS IS NOT A VALIDATED PERFORMANCE RESULT.\n"
            "   Sample is ~133 trading days on 15-min bars — the audit's own §8.3\n"
            "   sample-size bar needs several hundred to low thousands of trades before\n"
            "   any Sharpe/win-rate number means more than noise. Treat it as: the\n"
            "   machinery works and now includes the real regime filter; the\n"
            "   strategy's real long-run performance is still unknown."
        ))
