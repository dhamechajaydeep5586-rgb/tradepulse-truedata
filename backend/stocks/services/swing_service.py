"""Swing engine V2 — orchestration only.

Contains no indicator maths and no thresholds. Everything it needs comes from
`services/shared/` (universe, regime, ranking, portfolio risk, sizing, cost model,
sector) or from `swing_signals.py` (setup detection). That is the whole point: the
engine being replaced carried its own copies of EMA/ATR/ADX/RSI, its own liquidity
rules, its own scoring, and no portfolio layer at all.

Replaces `trade_engine.py`'s scanner, activation checker, EOD evaluator and cleanup.

Five changes that define V2 (see doc/SHORT_TERM_ENGINE_V2_ARCHITECTURE.md §1.5):

  D1  Scan AFTER the close on CLOSED bars, arming entries for the next session.
      Measured: the old 10:00 scan read an in-progress bar and halved its own
      volume-gate pass rate (19/49 -> 11/49 relaxed, 8/49 -> 5/49 strict).
  D2  No relaxed fallback. The threshold may legitimately return zero.
  D3  Cross-sectional `rank_score` replaces the absolute-threshold `ai_score`.
  D4  ADX / RS / volume / 52w-proximity become ranked factors, not hard gates.
  D5  Exits are bar-confirmed and correctly ordered, and record `exit_reason`.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone as dj_timezone

from stocks.models import ShortTermSignal, TradeHistory, TradeOutcome
from stocks.services import candle_store
from stocks.services.shared import SWING, allocated_equity
from stocks.services.shared import portfolio_risk, ranking, sector as sector_svc
from stocks.services.shared.regime import get_regime
from stocks.services.shared.risk_engine import fits_gross_budget, gross_budget
from stocks.services.shared.universe import get_trading_universe
from stocks.services.signal_utils import IST, round_to_tick, ta_ema
from stocks.services import swing_signals

logger = logging.getLogger(__name__)

SCAN_LOCK_KEY = "swing_scanner_running"
HALT_KEY = "swing_drawdown_halt"
LOCK_TTL = 900

OPEN_STATUSES = [
    ShortTermSignal.Status.PENDING,
    ShortTermSignal.Status.ACTIVE,
    ShortTermSignal.Status.TARGET1,
    ShortTermSignal.Status.TARGET2,
]
HELD_STATUSES = [
    ShortTermSignal.Status.ACTIVE,
    ShortTermSignal.Status.TARGET1,
    ShortTermSignal.Status.TARGET2,
]


# ══════════════════════════════════════════════════════════════════════════════
# 1. SCAN  — post-close, closed bars only
# ══════════════════════════════════════════════════════════════════════════════

def run_swing_scan(profile=SWING, dry_run: bool = False) -> dict:
    """Full swing scan. Returns a funnel dict — every stage count is reported.

    The funnel is a first-class output, not a debug aid. The old engine's rejections
    were invisible, which is how an empty signal table went unnoticed indefinitely.
    """
    if not cache.add(SCAN_LOCK_KEY, True, timeout=LOCK_TTL):
        logger.warning("[SWING] Scan already running — skipping overlapping trigger.")
        return {"skipped": "lock_held"}

    funnel: dict = {"started_at": dj_timezone.now().isoformat(), "dry_run": dry_run}
    try:
        if cache.get(HALT_KEY):
            logger.warning("[SWING] Drawdown halt active — no new signals this session.")
            return {**funnel, "halted": True}

        from stocks.services.truedata_service import get_truedata_instance
        svc = get_truedata_instance()
        if not svc:
            return {**funnel, "error": "broker_unavailable"}

        # ── Universe ────────────────────────────────────────────────────────────
        uni = get_trading_universe(svc, profile=profile)
        symbols = uni.get("symbols") or []
        stats = uni.get("stats", {})
        sector_map = uni.get("sectors", {})
        funnel["universe"] = len(symbols)
        funnel["universe_rejected"] = uni.get("rejected", {})
        if not symbols:
            return {**funnel, "error": "empty_universe"}

        # ── Regime ──────────────────────────────────────────────────────────────
        regime = get_regime(svc, profile=profile)
        funnel["regime"] = {
            "trend": regime.trend_state, "vol": regime.vol_state,
            "bias": regime.directional_bias, "breadth": regime.breadth_pct,
            "vix": regime.vix, "size_mult": regime.size_multiplier,
            "allow_momentum": regime.allow_momentum,
            "allow_mean_reversion": regime.allow_mean_reversion,
        }

        # ── Already-open symbols are not rescanned ──────────────────────────────
        open_rows = list(
            ShortTermSignal.objects.filter(status__in=OPEN_STATUSES)
            .values("symbol", "entry_price", "qty")
        )
        open_symbols = {r["symbol"] for r in open_rows}
        scan_symbols = [s for s in symbols if s not in open_symbols]
        funnel["open_positions"] = len(open_rows)
        funnel["to_scan"] = len(scan_symbols)

        # ── Event blackout (earnings + F&O ban) — reuses the intraday filter ────
        try:
            from stocks.services.event_filter_service import filter_symbols
            scan_symbols, excluded = filter_symbols(scan_symbols)
            funnel["event_blackout"] = len(excluded)
            funnel["event_blackout_detail"] = dict(list(excluded.items())[:20])
        except Exception as exc:
            logger.warning("[SWING] Event filter unavailable (%s) — proceeding UNFILTERED.", exc)
            funnel["event_blackout"] = "unavailable"

        # ── Benchmark return for relative strength ──────────────────────────────
        benchmark_ret = _benchmark_return(svc, lookback=20)
        funnel["benchmark_20d_ret_pct"] = benchmark_ret

        # ── Sector strength from real sector indices ────────────────────────────
        index_scores = sector_svc.sector_strength_from_indices(svc)
        funnel["sector_indices_ranked"] = len(index_scores)

        # ── Setup detection on closed daily bars ────────────────────────────────
        now_ist = datetime.now(tz=IST)
        session_complete = _session_complete(now_ist)
        funnel["session_complete"] = session_complete

        token_map = svc.get_token_map(scan_symbols, exchange="NSE")
        candidates, scanned, no_data = [], 0, 0

        for sym in scan_symbols:
            token = token_map.get(sym)
            if not token:
                continue
            try:
                df = candle_store.get_candles(svc, sym, token, "ONE_DAY", lookback_days=420, exchange="NSE")
            except Exception as exc:
                logger.debug("[SWING] candle fetch failed %s: %s", sym, exc)
                continue
            if df is None or df.empty:
                no_data += 1
                continue
            scanned += 1

            st = stats.get(sym, {})
            cand = swing_signals.detect_setup(
                sym, df, regime, profile,
                benchmark_ret_pct=benchmark_ret,
                sector=sector_map.get(sym, "UNKNOWN"),
                session_complete=session_complete,
                adv_inr=st.get("adv_inr"),
                daily_vol_pct=st.get("daily_vol_pct"),
            )
            if not cand:
                continue
            if not swing_signals.strategy_allowed(cand["setup_family"], regime):
                continue

            cand["sector_strength"] = sector_svc.strength_for_symbol(
                sym, sector_map, index_scores, {}
            )
            candidates.append(cand)

        funnel["scanned"] = scanned
        funnel["no_data"] = no_data
        funnel["setups_found"] = len(candidates)

        if not candidates:
            logger.info("[SWING][SCAN] %s", funnel)
            return {**funnel, "persisted": 0}

        # ── Cross-sectional ranking (D3) ────────────────────────────────────────
        candidates = ranking.score_candidates(
            candidates, regime, universe_stats=stats, profile=profile
        )
        funnel["best_score"] = candidates[0]["rank_score"]
        qualified = ranking.apply_threshold(candidates, profile=profile)
        funnel["cleared_threshold"] = len(qualified)
        funnel["threshold"] = profile.min_rank_score

        # ── Portfolio constraints ───────────────────────────────────────────────
        clusters = portfolio_risk.build_correlation_clusters(
            svc, [c["symbol"] for c in qualified], profile=profile
        )
        accepted, rejected = portfolio_risk.apply_portfolio_constraints(
            qualified, open_rows, sector_map, clusters,
            profile.max_positions, profile=profile,
        )
        portfolio_risk.record_candidates(profile.name, accepted, rejected, regime=regime, dry_run=dry_run)
        funnel["portfolio_rejected"] = len(rejected)
        funnel["portfolio_reject_reasons"] = _reason_counts(rejected)

        # ── Gross exposure ──────────────────────────────────────────────────────
        used = sum(float(r.get("qty") or 0) * float(r["entry_price"]) for r in open_rows)
        budget = gross_budget(profile)
        selected = []
        for cand in accepted:
            if not fits_gross_budget(used, cand["notional"], profile):
                cand["reject_reason"] = "GROSS_EXPOSURE"
                rejected.append(cand)
                continue
            selected.append(cand)
            used += cand["notional"]
        funnel["gross_used"] = round(used, 2)
        funnel["gross_budget"] = round(budget, 2)
        funnel["selected"] = len(selected)

        if dry_run:
            funnel["would_persist"] = [
                {k: c[k] for k in ("symbol", "rank_score", "setup_family", "entry",
                                   "stop_loss", "target", "qty")}
                for c in selected
            ]
            logger.info("[SWING][DRY_RUN] %s", funnel)
            return funnel

        # ── Persist ─────────────────────────────────────────────────────────────
        valid_until = (now_ist.date() + timedelta(days=profile.pending_expiry_days or 5))
        persisted = 0
        for c in selected:
            try:
                with transaction.atomic():
                    sig = ShortTermSignal.objects.create(
                        symbol=c["symbol"],
                        entry_price=Decimal(str(c["entry"])),
                        stop_loss=Decimal(str(c["stop_loss"])),
                        target=Decimal(str(c["target"])),
                        target2=Decimal(str(c["target2"])),
                        target3=Decimal(str(c["target3"])),
                        current_price=Decimal(str(c["entry"])),
                        vol_ratio=Decimal(str(c["vol_ratio"])),
                        setup=c["setup"],
                        setup_family=c["setup_family"],
                        status=ShortTermSignal.Status.PENDING,
                        qty=c["qty"],
                        rupee_risk=Decimal(str(c["rupee_risk"])),
                        rank_score=Decimal(str(c["rank_score"])),
                        rank_factors=c.get("rank_factors", {}),
                        regime_snapshot=regime.as_dict(),
                        cost_pct=Decimal(str(c["cost_pct"])),
                        target_pct=Decimal(str(c["target_pct"])),
                        entry_valid_until=valid_until,
                        expected_holding_days=_expected_holding_days(c),
                    )
                    TradeHistory.objects.create(
                        trade=sig, old_status="NONE",
                        new_status=ShortTermSignal.Status.PENDING,
                        price=sig.entry_price,
                        reason=f"Scanner: {c['setup']} (rank {c['rank_score']})",
                        triggered_by="SYSTEM",
                    )
                persisted += 1
            except Exception as exc:
                logger.error("[SWING] Persist failed for %s: %s", c["symbol"], exc)

        funnel["persisted"] = persisted
        logger.info("[SWING][SCAN_DONE] %s", funnel)

        if persisted:
            _notify_new_signals(selected[:persisted], regime, funnel)
        return funnel

    finally:
        cache.delete(SCAN_LOCK_KEY)


# ══════════════════════════════════════════════════════════════════════════════
# 2. ACTIVATION  — next session
# ══════════════════════════════════════════════════════════════════════════════

def check_swing_activations(profile=SWING) -> dict:
    """Activate PENDING setups whose entry has been touched, using the session's
    daily bar LOW rather than a point-in-time LTP.

    The old checker compared LTP at 12 polling moments, so a touch between polls was
    invisible. Reading the bar's low means a touch anywhere in the session counts,
    which is both more accurate and what a backtest would assume.
    """
    from stocks.services.truedata_service import get_truedata_instance
    svc = get_truedata_instance()
    if not svc:
        return {"error": "broker_unavailable"}

    pending = list(ShortTermSignal.objects.filter(status=ShortTermSignal.Status.PENDING))
    if not pending:
        return {"pending": 0, "activated": 0}

    today = datetime.now(tz=IST).date()
    token_map = svc.get_token_map([s.symbol for s in pending], exchange="NSE")
    activated = expired = 0

    for sig in pending:
        # Expire stale arms first — a setup found five sessions ago is no longer the
        # setup that was found.
        if sig.entry_valid_until and today > sig.entry_valid_until:
            _close_position(sig, float(sig.entry_price), ShortTermSignal.Status.EXPIRED,
                            "ENTRY_WINDOW_EXPIRED", record_outcome=False)
            expired += 1
            continue

        token = token_map.get(sig.symbol)
        if not token:
            continue
        try:
            q = svc.get_bulk_quotes({"NSE": [token]}, mode="FULL").get(f"NSE:{token}")
        except Exception:
            continue
        if not q:
            continue

        ltp = float(q.get("ltp") or 0)
        low = float(q.get("low") or ltp)
        if ltp <= 0:
            continue

        sig.current_price = Decimal(str(ltp))
        entry = float(sig.entry_price)

        if low <= entry:
            try:
                with transaction.atomic():
                    row = ShortTermSignal.objects.select_for_update().get(id=sig.id)
                    if row.status != ShortTermSignal.Status.PENDING:
                        continue
                    row.status = ShortTermSignal.Status.ACTIVE
                    row.activated_at = dj_timezone.now()
                    row.current_price = Decimal(str(ltp))
                    row.save(update_fields=["status", "activated_at", "current_price"])
                    TradeHistory.objects.create(
                        trade=row, old_status=ShortTermSignal.Status.PENDING,
                        new_status=ShortTermSignal.Status.ACTIVE,
                        price=Decimal(str(entry)), reason="Entry touched (session low)",
                        triggered_by="SYSTEM",
                    )
                activated += 1
            except Exception as exc:
                logger.error("[SWING] Activation failed for %s: %s", sig.symbol, exc)
        else:
            sig.save(update_fields=["current_price"])

    logger.info("[SWING][ACTIVATION] pending=%d activated=%d expired=%d",
                len(pending), activated, expired)
    return {"pending": len(pending), "activated": activated, "expired": expired}


# ══════════════════════════════════════════════════════════════════════════════
# 3. EOD EVALUATION  — bar-confirmed, correctly ordered (D5)
# ══════════════════════════════════════════════════════════════════════════════

def run_swing_eod_evaluation(profile=SWING) -> dict:
    """Evaluate held positions against the session's daily bar.

    Ordering is the fix. The old evaluator checked the 20-EMA trailing exit BEFORE any
    target, so a bar that gapped through target 3 booked as TRAILING_EXIT at the close.
    Correct precedence is: stop, then final target, then time stop, then the milestone
    transitions.

    Pessimistic on ambiguous bars: when one bar's range contains both the stop and a
    target, the stop is booked. The intrabar path is unknowable at daily resolution and
    resolving it in the strategy's favour is what makes a track record flattering
    rather than accurate.
    """
    from stocks.services.truedata_service import get_truedata_instance
    svc = get_truedata_instance()
    if not svc:
        return {"error": "broker_unavailable"}

    held = list(ShortTermSignal.objects.filter(status__in=HELD_STATUSES))
    if not held:
        return {"held": 0}

    token_map = svc.get_token_map([s.symbol for s in held], exchange="NSE")
    counts = {"exited": 0, "target1": 0, "target2": 0, "held": 0}

    for sig in held:
        token = token_map.get(sig.symbol)
        if not token:
            continue
        try:
            df = candle_store.get_candles(svc, sig.symbol, token, "ONE_DAY", lookback_days=200, exchange="NSE")
        except Exception:
            continue
        if df is None or df.empty or len(df) < 25:
            continue

        bar = df.iloc[-1]
        high, low, close = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        entry = float(sig.entry_price)
        stop = float(sig.stop_loss)
        t1 = float(sig.target)
        t2 = float(sig.target2) if sig.target2 else t1 * 1.5
        t3 = float(sig.target3) if sig.target3 else t1 * 2.0

        sig.current_price = Decimal(str(close))
        if entry > 0:
            peak = ((high - entry) / entry) * 100.0
            trough = ((low - entry) / entry) * 100.0
            if peak > float(sig.highest_profit):
                sig.highest_profit = Decimal(str(round(peak, 2)))
            if trough < float(sig.max_drawdown):
                sig.max_drawdown = Decimal(str(round(trough, 2)))
            sig.pnl_pct = Decimal(str(round(((close - entry) / entry) * 100.0, 2)))

        # ── 1. Stop (pessimistic — checked first) ───────────────────────────────
        if low <= stop:
            _close_position(sig, stop, ShortTermSignal.Status.HIT_SL, "LEVEL_HIT_STOP")
            counts["exited"] += 1
            continue

        # ── 2. Final target ─────────────────────────────────────────────────────
        if high >= t3:
            _close_position(sig, t3, ShortTermSignal.Status.HIT_TARGET, "LEVEL_HIT_TARGET3")
            counts["exited"] += 1
            continue

        # ── 3. Time stop ────────────────────────────────────────────────────────
        if sig.expected_holding_days and sig.activated_at:
            elapsed = (dj_timezone.now().date() - sig.activated_at.date()).days
            if elapsed >= sig.expected_holding_days:
                _close_position(sig, close, ShortTermSignal.Status.EXPIRED, "TIME_STOP")
                counts["exited"] += 1
                continue

        # ── 4. Trailing 20-EMA (now AFTER the target checks) ────────────────────
        ema20 = float(ta_ema(df["Close"], 20).iloc[-1])
        if close < ema20 and sig.status in (ShortTermSignal.Status.TARGET1,
                                            ShortTermSignal.Status.TARGET2):
            # Only trails once a milestone is banked. Trailing an un-progressed
            # position on the 20-EMA just duplicates the stop with worse arithmetic.
            _close_position(sig, close, ShortTermSignal.Status.HIT_TARGET, "TRAILING_EMA20")
            counts["exited"] += 1
            continue

        # ── 5/6. Milestones ─────────────────────────────────────────────────────
        if high >= t2 and sig.status in (ShortTermSignal.Status.ACTIVE,
                                         ShortTermSignal.Status.TARGET1):
            _transition(sig, ShortTermSignal.Status.TARGET2, close,
                        f"Target 2 reached ({t2:.2f}); stop trailed to entry")
            counts["target2"] += 1
        elif high >= t1 and sig.status == ShortTermSignal.Status.ACTIVE:
            _transition(sig, ShortTermSignal.Status.TARGET1, close,
                        f"Target 1 reached ({t1:.2f}); stop locked to entry")
            counts["target1"] += 1
        else:
            sig.save()
            counts["held"] += 1

    _check_drawdown_halt(profile)
    logger.info("[SWING][EOD] %s", counts)
    return counts


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _transition(sig, new_status, price: float, reason: str) -> None:
    old = sig.status
    sig.status = new_status
    sig.stop_loss = sig.entry_price      # lock to break-even
    sig.save()
    TradeHistory.objects.create(
        trade=sig, old_status=old, new_status=new_status,
        price=Decimal(str(round(price, 2))), reason=reason, triggered_by="SYSTEM",
    )


def _close_position(sig, exit_price: float, exit_status: str, exit_reason: str,
                    record_outcome: bool = True) -> None:
    """Terminal-state a position, write the audit rows, and append to TradeOutcome.

    `TradeOutcome` is the ledger that base rates, expected value and Kelly are computed
    from. Nothing wrote it before, which is why probability and EV were unavailable
    platform-wide.
    """
    old = sig.status
    entry = float(sig.entry_price)
    stop = float(sig.stop_loss) if sig.stop_loss else entry
    risk = abs(entry - stop) or 1e-9
    pnl_pct = ((exit_price - entry) / entry * 100.0) if entry else 0.0
    r_multiple = (exit_price - entry) / risk

    sig.exit_price = Decimal(str(round_to_tick(exit_price)))
    sig.exited_at = dj_timezone.now()
    sig.exit_reason = exit_reason
    sig.pnl_pct = Decimal(str(round(pnl_pct, 2)))
    sig.status = ShortTermSignal.Status.ARCHIVED
    sig.cooldown_until = dj_timezone.now() + timedelta(days=28)
    sig.save()

    TradeHistory.objects.create(
        trade=sig, old_status=old, new_status=exit_status,
        price=Decimal(str(round(exit_price, 2))), reason=exit_reason, triggered_by="SYSTEM",
    )

    if not record_outcome:
        return

    holding_days = 0
    if sig.activated_at:
        holding_days = (dj_timezone.now().date() - sig.activated_at.date()).days

    try:
        TradeOutcome.objects.create(
            engine=TradeOutcome.Engine.SWING,
            symbol=sig.symbol,
            setup_family=sig.setup_family or "",
            direction="BUY",
            entry_price=sig.entry_price,
            exit_price=sig.exit_price,
            stop_loss=sig.stop_loss,
            target=sig.target,
            qty=sig.qty or 0,
            r_multiple=Decimal(str(round(r_multiple, 3))),
            pnl_pct=Decimal(str(round(pnl_pct, 4))),
            pnl_inr=Decimal(str(round((exit_price - entry) * (sig.qty or 0), 2))),
            cost_pct=sig.cost_pct,
            mfe_pct=sig.highest_profit,
            mae_pct=sig.max_drawdown,
            exit_reason=exit_reason,
            regime_snapshot=sig.regime_snapshot or {},
            rank_score=sig.rank_score,
            rank_factors=sig.rank_factors or {},
            holding_days=holding_days,
            opened_at=sig.activated_at,
            closed_at=sig.exited_at,
        )
    except Exception as exc:
        logger.error("[SWING] TradeOutcome write failed for %s: %s", sig.symbol, exc)


def _check_drawdown_halt(profile) -> None:
    """Trip a session halt if open+realised drawdown breaches the strategy limit."""
    limit = profile.strategy_drawdown_limit_pct
    if not limit:
        return
    equity = allocated_equity(profile)
    open_pnl = 0.0
    for s in ShortTermSignal.objects.filter(status__in=HELD_STATUSES):
        if s.pnl_pct and s.qty and s.entry_price:
            open_pnl += float(s.pnl_pct) / 100.0 * float(s.entry_price) * s.qty
    if equity > 0 and (open_pnl / equity) * 100.0 <= -limit:
        cache.set(HALT_KEY, True, timeout=24 * 3600)
        logger.error("[SWING] DRAWDOWN HALT — open P&L Rs.%.0f is %.1f%% of allocated "
                     "equity (limit %.1f%%). New signals suspended.",
                     open_pnl, open_pnl / equity * 100.0, limit)


def _benchmark_return(svc, lookback: int = 20) -> float:
    from stocks.services.shared.regime import NIFTY50_TOKEN
    try:
        df = candle_store.get_candles(
            svc, "NIFTY50_INDEX", NIFTY50_TOKEN, "ONE_DAY",
            lookback_days=lookback + 40, exchange="NSE",
        )
        if df is None or len(df) < lookback + 1:
            return 0.0
        past = float(df["Close"].iloc[-lookback - 1])
        return round((float(df["Close"].iloc[-1]) / past - 1.0) * 100.0, 3) if past else 0.0
    except Exception:
        return 0.0


def _session_complete(now_ist: datetime) -> bool:
    """True once today's daily bar is closed (or on a non-session day)."""
    if now_ist.weekday() >= 5:
        return True
    return now_ist.time() >= datetime.strptime("15:30", "%H:%M").time()


def _expected_holding_days(cand: dict) -> int:
    """ATR-derived horizon, clamped to the strategy's 15-90 day band."""
    atr_pct = (cand["atr"] / cand["entry"]) * 100.0 if cand["entry"] else 2.0
    return max(15, min(90, int(60 / max(atr_pct, 0.5))))


def _reason_counts(rejected: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rejected:
        key = str(r.get("reject_reason", "UNKNOWN")).split(":")[0]
        out[key] = out.get(key, 0) + 1
    return out


def _notify_new_signals(selected: list[dict], regime, funnel: dict) -> None:
    try:
        from stocks.services.telegram_service import queue_telegram_message
        lines = [
            f"{i}. <b>{c['symbol']}</b> (rank {c['rank_score']:.0f}/100, {c['setup_family']})\n"
            f"   Entry ₹{c['entry']} | SL ₹{c['stop_loss']} | T1 ₹{c['target']}\n"
            f"   Qty {c['qty']} | Risk ₹{c['rupee_risk']:.0f}"
            for i, c in enumerate(selected, 1)
        ]
        msg = (
            f"🚀 <b>SWING SETUPS — next session</b>\n"
            f"Regime: {regime.trend_state}/{regime.vol_state} "
            f"({regime.directional_bias}), breadth {regime.breadth_pct:.0f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines) +
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>{funnel['scanned']} scanned → {funnel['setups_found']} setups → "
            f"{funnel['cleared_threshold']} cleared rank {funnel['threshold']:.0f} → "
            f"{funnel['selected']} selected</i>"
        )
        queue_telegram_message(msg, event_type="SWING_V2_SIGNALS")
    except Exception as exc:
        logger.error("[SWING] Telegram queue failed: %s", exc)
