"""
live_signal_service.py (Refactored / Orchestration Layer)
This file acts as the primary orchestrator for specialized signal engines.
100% Angel One Infrastructure (yfinance purged).
"""

from __future__ import annotations
import logging
from datetime import datetime, time, timedelta, date
from django.core.cache import cache
from django.utils import timezone as dj_timezone

from stocks.models import SignalHistory, Notification, SignalChangeLog
from stocks.services.signal_utils import (
    IST, MARKET_OPEN, MARKET_CLOSE, INTRADAY_SIGNAL_CUTOFF,
    is_market_open, round_to_tick, gap_adjusted_stop_price
)

# Import specialty services
from stocks.services.intraday_service import (
    get_live_signals,
    DAILY_HALT_CACHE_KEY,
    INTRADAY_ACCOUNT_EQUITY,
    INTRADAY_DAILY_LOSS_LIMIT_PCT,
)
from stocks.services.delta_hedge_service import get_hedge_panel_data

logger = logging.getLogger(__name__)

# Intraday signals are generated on 5-min bars; 8 bars = 40 minutes. An intraday
# momentum/mean-reversion setup that hasn't resolved within that window has decayed to
# roughly zero expected value, so holding it only ties up risk budget.
INTRADAY_TIME_STOP_MINUTES = 8 * 5

def get_latest_prices(symbols: list[str]) -> dict[str, float]:
    """
    Fetch latest prices for symbols via MarketDataOrchestrator efficiently.
    Uses Bulk API to prevent rate limits.
    """
    from stocks.services.market_data_orchestrator import get_orchestrator
    orchestrator = get_orchestrator()
    return orchestrator.get_prices_bulk(symbols, exchange="NSE")


def _minute_bars_since(symbol: str, since_dt, svc):
    """Completed 1-min bars for `symbol` after `since_dt`, IST-aware.

    Anchored to the session open rather than to `since_dt` so the underlying candle
    cache key (which buckets by whole-day lookback) stays stable across calls — a cache
    hit is then always a superset of what we need, never a gap.
    """
    try:
        token = (svc.get_token_map([symbol], exchange="NSE") or {}).get(symbol)
        if not token:
            return None

        now_ist = datetime.now(tz=IST)
        df = svc.get_candle_data(
            token, "NSE", "ONE_MINUTE",
            now_ist.strftime("%Y-%m-%d 09:15"),
            now_ist.strftime("%Y-%m-%d %H:%M"),
        )
        if df is None or df.empty:
            return None

        df.index = (
            df.index.tz_localize(IST) if df.index.tz is None else df.index.tz_convert(IST)
        )
        if since_dt is not None:
            df = df[df.index > since_dt]
        return df if not df.empty else None
    except Exception as exc:
        logger.warning("[INTRADAY][AUDIT] 1-min bar fetch failed for %s: %s", symbol, exc)
        return None


def _scan_bars_for_activation(sig, bars):
    """First bar whose range touches the entry price (0.2% tolerance), else None."""
    entry = float(sig.entry_price)
    tol = entry * 0.002
    for ts, bar in bars.iterrows():
        if float(bar["Low"]) - tol <= entry <= float(bar["High"]) + tol:
            return ts
    return None


def _is_momentum(sig) -> bool:
    """Momentum setups trail; mean-reversion setups take a fixed target.

    Matching exit style to signal type matters because the two have opposite return
    shapes: momentum trades carry a fat right tail worth trailing for, while a
    mean-reversion bounce reverts to value and then stalls — trailing it just gives the
    gain back.
    """
    from stocks.services.regime_service import MEAN_REVERSION_STRATEGIES
    reason = (sig.reason or "").split("] ", 1)[-1]
    return reason not in MEAN_REVERSION_STRATEGIES


def _manage_dynamic_stop(sig, bars, now) -> bool:
    """Break-even and Chandelier trailing stop for momentum trades.

    Returns True if the stop was tightened. Stops only ever move in the favourable
    direction — a stop that can widen is not a stop.
    """
    if not _is_momentum(sig):
        return False

    meta = sig.metadata or {}
    entry = float(sig.entry_price)
    stop = float(sig.stop_loss)
    atr = float(meta.get("atr") or 0.0)
    is_buy = sig.signal_type == "BUY"
    r = abs(entry - stop)
    if r <= 0:
        return False

    high = float(bars["High"].max())
    low = float(bars["Low"].min())
    best = max(float(meta.get("best_price", entry)), high) if is_buy \
        else min(float(meta.get("best_price", entry)), low)
    meta["best_price"] = round(best, 2)

    new_stop = stop
    excursion = (best - entry) if is_buy else (entry - best)

    # Break-even once the trade has paid for itself (+1R).
    if excursion >= r:
        be = entry + (0.0005 * entry if is_buy else -0.0005 * entry)  # cover costs
        new_stop = max(new_stop, be) if is_buy else min(new_stop, be)
        meta["break_even_armed"] = True

    # Chandelier trail from the favourable extreme, once past 1.5R.
    if atr > 0 and excursion >= 1.5 * r:
        chandelier = (best - 2.0 * atr) if is_buy else (best + 2.0 * atr)
        new_stop = max(new_stop, chandelier) if is_buy else min(new_stop, chandelier)
        meta["trailing_armed"] = True

    tightened = (new_stop > stop) if is_buy else (new_stop < stop)
    if tightened:
        sig.stop_loss = round_to_tick(new_stop)
        meta["stop_moves"] = int(meta.get("stop_moves", 0)) + 1
        sig.metadata = meta
        sig.save(update_fields=["stop_loss", "metadata"])
        logger.info("[INTRADAY][TRAIL] %s stop -> %.2f (%.2fR excursion)",
                    sig.symbol, sig.stop_loss, excursion / r)
        return True

    sig.metadata = meta
    sig.save(update_fields=["metadata"])
    return False


def _vwap_exit_hit(sig, bars) -> float | None:
    """Session-VWAP exit for mean-reversion trades.

    A value-area bounce is a bet on reversion TO value; VWAP is where that thesis is
    complete. Holding past it converts a mean-reversion trade into a momentum trade
    nobody sized for.
    """
    if _is_momentum(sig):
        return None
    try:
        from stocks.services.signal_utils import compute_session_vwap
        if "Volume" not in bars.columns or len(bars) < 3:
            return None
        vwap = float(compute_session_vwap(bars).iloc[-1])
        last = float(bars["Close"].iloc[-1])
        entry = float(sig.entry_price)
        if sig.signal_type == "BUY" and entry < vwap <= last:
            return vwap
        if sig.signal_type == "SELL" and entry > vwap >= last:
            return vwap
    except Exception:
        return None
    return None


def _scan_bars_for_exit(sig, bars):
    """Walk bars chronologically for a target/stop touch.

    Returns (status, exit_price, exit_timestamp) or None.

    Pessimistic by design: when one bar's range contains BOTH the stop and the target,
    the stop is booked. The true intrabar path is unknowable at 1-min resolution, and
    resolving that ambiguity in the strategy's own favour is precisely what makes a
    performance record flattering rather than accurate.
    """
    target = float(sig.target)
    sl = float(sig.stop_loss)
    is_buy = sig.signal_type == "BUY"

    for ts, bar in bars.iterrows():
        high, low = float(bar["High"]), float(bar["Low"])
        if is_buy:
            if low <= sl:
                # Gap-through: fill at the bar's actual low, not the raw stop level.
                return SignalHistory.Status.HIT_SL, gap_adjusted_stop_price(sl, low, is_buy), ts
            if high >= target:
                return SignalHistory.Status.HIT_TARGET, target, ts
        else:
            if high >= sl:
                # Gap-through: fill at the bar's actual high, not the raw stop level.
                return SignalHistory.Status.HIT_SL, gap_adjusted_stop_price(sl, high, is_buy), ts
            if low <= target:
                return SignalHistory.Status.HIT_TARGET, target, ts
    return None


def _close_signal(sig, status, exit_price, exit_time, reason: str):
    """Terminal-state a signal and record why it closed."""
    sig.status = status
    sig.exit_price = round_to_tick(float(exit_price))
    sig.exit_time = exit_time
    meta = sig.metadata or {}
    meta["exit_reason"] = reason
    sig.metadata = meta
    sig.save()


def _audit_intraday_signal(sig, price, now, svc) -> None:
    """Audit one intraday signal against 1-min bar highs/lows.

    Replaces point-in-time LTP comparison. With a stop distance of roughly 0.16% and an
    auditor that runs every 15 minutes, price could cross the stop and revert several
    times between polls: those touches were invisible, and the exit price recorded was
    the ideal level at a moment the system never actually observed.
    """
    last_seen = (sig.metadata or {}).get("last_audit_bar")
    since = None
    if last_seen:
        try:
            since = datetime.fromisoformat(last_seen)
        except ValueError:
            since = None
    if since is None:
        since = sig.active_time or sig.generated_at
    if since is not None and dj_timezone.is_aware(since):
        since = since.astimezone(IST)

    bars = _minute_bars_since(sig.symbol, since, svc)

    # No bar data (fetch failed / rate limited) — fall back to the LTP comparison so a
    # data outage degrades fidelity rather than leaving positions unaudited entirely.
    if bars is None:
        _audit_by_last_price(sig, price, now)
        return

    if sig.status == SignalHistory.Status.PENDING:
        activated_at = _scan_bars_for_activation(sig, bars)
        if activated_at is None:
            _remember_audit_position(sig, bars)
            return
        sig.status = SignalHistory.Status.ACTIVE
        sig.active_time = activated_at.to_pydatetime()
        sig.save()
        bars = bars[bars.index >= activated_at]

    if sig.status == SignalHistory.Status.ACTIVE:
        # Level checks run against the stop as it stood over these bars, BEFORE any
        # trailing adjustment — tightening the stop first and then testing the same bars
        # against it would retroactively stop out trades on price action that occurred
        # while the stop was still wide.
        outcome = _scan_bars_for_exit(sig, bars)
        if outcome:
            status, exit_price, ts = outcome
            _close_signal(sig, status, exit_price, ts.to_pydatetime(), "LEVEL_HIT")
            return

        # Mean-reversion trades exit at session VWAP — thesis complete.
        vwap_px = _vwap_exit_hit(sig, bars)
        if vwap_px is not None:
            _close_signal(sig, SignalHistory.Status.HIT_TARGET, vwap_px, now, "VWAP_EXIT")
            logger.info("[INTRADAY][VWAP_EXIT] %s closed at VWAP %.2f", sig.symbol, vwap_px)
            return

        # Momentum trades: arm break-even / trail for the NEXT cycle's level check.
        _manage_dynamic_stop(sig, bars, now)

        # Time stop — only once the level checks above have confirmed nothing was hit.
        if sig.active_time:
            held = (now - sig.active_time).total_seconds() / 60.0
            if held >= INTRADAY_TIME_STOP_MINUTES:
                _close_signal(sig, SignalHistory.Status.EXPIRED, price, now, "TIME_STOP")
                logger.info(
                    "[INTRADAY][TIME_STOP] %s closed after %.0f min with no resolution.",
                    sig.symbol, held,
                )
                return

    _remember_audit_position(sig, bars)


def _remember_audit_position(sig, bars) -> None:
    """Checkpoint the last bar examined so the next cycle resumes without re-scanning.

    Stores the last *bar* timestamp rather than `now`, so a slightly stale cached frame
    can never cause a bar to be skipped.
    """
    if bars is None or bars.empty:
        return
    meta = sig.metadata or {}
    meta["last_audit_bar"] = bars.index[-1].isoformat()
    sig.metadata = meta
    sig.save(update_fields=["metadata"])


def _audit_by_last_price(sig, price, now) -> None:
    """Degraded-mode auditor: single-price comparison when bar data is unavailable."""
    target, sl = float(sig.target), float(sig.stop_loss)

    if sig.status == SignalHistory.Status.PENDING:
        entry = float(sig.entry_price)
        if abs(price - entry) / entry < 0.002:
            sig.status = SignalHistory.Status.ACTIVE
            sig.active_time = now
            sig.save()

    if sig.status != SignalHistory.Status.ACTIVE:
        return

    if sig.signal_type == "BUY":
        if price <= sl:
            _close_signal(sig, SignalHistory.Status.HIT_SL, sl, now, "LEVEL_HIT_LTP")
        elif price >= target:
            _close_signal(sig, SignalHistory.Status.HIT_TARGET, target, now, "LEVEL_HIT_LTP")
    else:
        if price >= sl:
            _close_signal(sig, SignalHistory.Status.HIT_SL, sl, now, "LEVEL_HIT_LTP")
        elif price <= target:
            _close_signal(sig, SignalHistory.Status.HIT_TARGET, target, now, "LEVEL_HIT_LTP")


def _enforce_daily_loss_limit(prices: dict, now) -> None:
    """Flatten intraday and halt generation once the day's loss limit is breached.

    Counts realised P&L on today's closed signals plus unrealised on open ones, using
    the risk-based qty each signal was actually sized at.
    """
    if cache.get(DAILY_HALT_CACHE_KEY):
        return

    today = datetime.now(tz=IST).date()
    rows = list(SignalHistory.objects.filter(category="intraday", generated_at__date=today))
    if not rows:
        return

    closed = {SignalHistory.Status.HIT_TARGET, SignalHistory.Status.HIT_SL,
              SignalHistory.Status.EXPIRED}
    total = 0.0
    for s in rows:
        qty = int((s.metadata or {}).get("qty") or 0)
        if qty <= 0:
            continue
        entry = float(s.entry_price)
        direction = 1 if s.signal_type == "BUY" else -1
        if s.status in closed and s.exit_price is not None:
            total += (float(s.exit_price) - entry) * qty * direction
        elif s.status == SignalHistory.Status.ACTIVE:
            px = prices.get(s.symbol)
            if px:
                total += (float(px) - entry) * qty * direction

    limit = INTRADAY_ACCOUNT_EQUITY * (INTRADAY_DAILY_LOSS_LIMIT_PCT / 100.0)
    if total > -limit:
        return

    logger.error(
        "[INTRADAY][KILL_SWITCH] Daily P&L Rs.%.2f breached limit Rs.-%.2f — flattening.",
        total, limit,
    )

    for s in rows:
        if s.status == SignalHistory.Status.PENDING:
            _close_signal(s, SignalHistory.Status.CANCELLED, s.entry_price, now, "DAILY_LOSS_LIMIT")
        elif s.status == SignalHistory.Status.ACTIVE:
            px = prices.get(s.symbol) or float(s.entry_price)
            _close_signal(s, SignalHistory.Status.EXPIRED, px, now, "DAILY_LOSS_LIMIT")

    # Hold the halt until end of day so no later scan reopens positions.
    eod = datetime.now(tz=IST).replace(hour=23, minute=59, second=0, microsecond=0)
    cache.set(DAILY_HALT_CACHE_KEY, True,
              timeout=max(60, int((eod - datetime.now(tz=IST)).total_seconds())))

def update_signal_outcomes(force: bool = False, action: str | None = None):
    """
    High-speed auditor for all active/pending signals.
    """
    from stocks.services.signal_utils import is_static_closed
    
    # Skip auditing if markets are closed (Static)
    # This ensures 100% silence on holidays (Total Silence Hotfix)
    if is_static_closed("NSE"):
        return

    now = dj_timezone.now()
    active_signals = SignalHistory.objects.filter(
        status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING]
    ).exclude(category__in=['specialist', 'long_term', 'option_buying'])
    # long_term is a 1-2 year buy-and-hold with no target/SL — it must never be
    # auto-closed by this same-day price check (see pro_system_service.py hold_rule).
    # option_buying uses premium-scale entry/target/SL against signal_type "BUY_CE"/
    # "BUY_PE" (never exactly "BUY"), so it always fell into the SELL branch below and
    # got compared against the underlying stock's price instead of the option premium —
    # force-closing every signal as a bogus HIT_SL within one scan cycle. It has its own
    # correct auditor, update_option_buying_outcomes() (see run_periodic_scanners below).

    # Audit loop only runs when there's something open to audit — but the daily loss
    # limit below must run unconditionally. An empty book is exactly the state right
    # after a string of stop-losses closes it out, which is precisely when the kill
    # switch most needs to be armed, not the condition that used to skip it entirely
    # (audit fix: this function used to `return` here before ever reaching step 4).
    prices: dict = {}
    if active_signals.exists():
        # Audit symbols in bulk
        symbols = list(active_signals.values_list('symbol', flat=True).distinct())
        prices = get_latest_prices(symbols)

        now_time = datetime.now(tz=IST).time()

        # Broker handle for 1-min bar auditing of intraday positions. Resolved once for
        # the whole pass rather than per signal.
        _svc = None
        if any(s.category == 'intraday' for s in active_signals):
            from stocks.services.truedata_service import get_truedata_instance
            _svc = get_truedata_instance()

        for sig in active_signals:
            price = prices.get(sig.symbol)
            if price is None: continue

            # 1. Cutoff Checks
            if sig.category == 'intraday' and now_time >= INTRADAY_SIGNAL_CUTOFF:
                sig.status = SignalHistory.Status.CANCELLED if sig.status == SignalHistory.Status.PENDING else SignalHistory.Status.EXPIRED
                sig.exit_price = price
                sig.exit_time = now
                meta = sig.metadata or {}
                meta.setdefault("exit_reason", "SQUARE_OFF_CUTOFF")
                sig.metadata = meta
                sig.save()
                continue

            # 2. Intraday: audit against 1-min bar highs/lows, with a time stop. Exits
            # are auto-squared off on a Target/SL touch exactly like every other
            # category; anything still open at the 3:20 PM cutoff above is
            # force-closed regardless.
            if sig.category == 'intraday' and _svc is not None:
                try:
                    _audit_intraday_signal(sig, price, now, _svc)
                except Exception as exc:
                    logger.error("[INTRADAY][AUDIT] %s failed: %s", sig.symbol, exc)
                continue

            # 3. Standard Equity Logic (other categories)
            target = float(sig.target)
            sl = float(sig.stop_loss)

            if sig.status == SignalHistory.Status.PENDING:
                entry = float(sig.entry_price)
                if abs(price - entry) / entry < 0.002: # 0.2% tolerance for trigger
                    sig.status = SignalHistory.Status.ACTIVE
                    sig.active_time = now
                    sig.save()

            if sig.status == SignalHistory.Status.ACTIVE:
                if sig.signal_type == "BUY":
                    if price >= target:
                        sig.status = SignalHistory.Status.HIT_TARGET
                        sig.exit_price = target
                        sig.exit_time = now
                        sig.save()
                    elif price <= sl:
                        sig.status = SignalHistory.Status.HIT_SL
                        sig.exit_price = sl
                        sig.exit_time = now
                        sig.save()
                else: # SELL
                    if price <= target:
                        sig.status = SignalHistory.Status.HIT_TARGET
                        sig.exit_price = target
                        sig.exit_time = now
                        sig.save()
                    elif price >= sl:
                        sig.status = SignalHistory.Status.HIT_SL
                        sig.exit_price = sl
                        sig.exit_time = now
                        sig.save()

    # 4. Daily loss limit — evaluated after outcomes are settled so realised P&L is
    # current. Flattens intraday and blocks further generation for the session. Runs
    # even when active_signals was empty above — see comment before that block.
    try:
        _enforce_daily_loss_limit(prices, now)
    except Exception as exc:
        logger.error("[INTRADAY][KILL_SWITCH] evaluation failed: %s", exc)

    # 5. Specialist Setup Audit (Iron Condors / Strangles)
    # DEPRECATED: Specialist signals are directly managed by delta_hedge_service.py get_hedge_panel_data
    # audit_specialist_setups(prices, now, now_time)



def run_periodic_scanners(action: str | None = None):
    """Background worker to run heavy signal scans and updates.

    Guards against overlapping runs: a manual cron-trigger hit landing on the same
    moment as the scheduled 15-min cycle (or two manual triggers close together)
    would otherwise double the REST call rate from specialist+option_buying+intraday
    running twice at once — enough on its own to trip the shared Angel One circuit
    breaker (confirmed: a manual trigger fired right at a 15-min boundary tripped it
    within seconds). Mirrors the same lock pattern trade_engine.run_daily_scanner
    already uses for this exact reason.
    """
    from django.core.cache import cache
    lock_key = "run_periodic_scanners_running"
    if not cache.add(lock_key, True, timeout=600):
        logger.warning("[CRON] run_periodic_scanners already running (overlapping trigger) — skipping this run")
        return
    try:
        _run_periodic_scanners_impl(action=action)
    finally:
        cache.delete(lock_key)


def _run_periodic_scanners_impl(action: str | None = None):
    from django.db import close_old_connections
    close_old_connections()
    try:
        # Initialize and connect Angel One session at start of cycle
        from stocks.services.truedata_service import get_truedata_instance
        svc = get_truedata_instance()
        if svc:
            logger.info("[CRON] Angel One session successfully connected at start of cycle.")
            
        # DEPRECATED 2026-07-28 (audit remediation): this used to route to
        # pro_system_service.get_pro_system_data(trigger_scan=True), the legacy
        # short-term scanner. That path checks only for an existing ACTIVE
        # ShortTermSignal (not PENDING) before creating a new one and sends Telegram via
        # the raw synchronous sender, bypassing the TelegramLog idempotency guard
        # trade_engine.py relies on — able to create duplicate signals and duplicate
        # Telegram alerts for the same symbol. trade_engine.py's own scheduled 11:30 AM
        # job (run_short_term_scan in updater.py) is the sole live short-term engine;
        # this action now no-ops rather than racing it.
        if action == "short_term_scan":
            logger.warning(
                "[CRON] short_term_scan action is deprecated and now a no-op — "
                "trade_engine.py's 11:30 AM scheduled job is the sole short-term engine."
            )
            return

        # First, always audit outcomes (high speed)
        update_signal_outcomes(action=action)

        # Option-buying is excluded from update_signal_outcomes() (same precedent as
        # 'specialist' below) since its premium-space exits and hard time-stop don't
        # fit that function's equity-oriented branches — self-audited here.
        try:
            from stocks.services.option_buying_service import update_option_buying_outcomes
            update_option_buying_outcomes()
        except Exception as ob_err:
            logger.error("[CRON] Option-buying outcome audit failed: %s", ob_err)

        # NOTE: pro_system_service.update_pro_system_outcomes() used to run here too,
        # but it duplicated trade_engine.py's check_pending_activations()/
        # run_eod_evaluation() — both watched the same ShortTermSignal rows for the same
        # trailing-20-EMA exit condition and each sent their own Telegram alert,
        # causing duplicate messages (e.g. two different "exit" alerts for the same
        # stock at different times). trade_engine.py is the newer, more complete engine
        # (adds target1/2/3, time-stop exits) so it's the sole source of truth; the
        # dead update_pro_system_outcomes() function itself was REMOVED from
        # pro_system_service.py entirely (audit remediation Phase 2 #2.6, 2026-07-29) —
        # it's not just uncalled anymore, it no longer exists.

        market_open = is_market_open()

        if market_open:
            # ── SPECIALIST SCAN (generates signals + sends Telegram) ──
            # This MUST run before the intraday scan because get_live_signals()
            # sweeps the whole Nifty 100 universe via paced REST calls, taking
            # a couple of minutes. Running specialist first ensures Telegram
            # messages fire promptly.
            try:
                specialist_panel = get_hedge_panel_data(action=action, sync_scan=True)
                logger.info("[CRON] Specialist scan completed successfully.")
                # Use the panel's own resolved_action, not the `action` argument above —
                # the scheduled 15-min job always calls this with action=None, and
                # get_hedge_panel_data() resolves None to "generate"/"update" internally
                # based on whether today's specialist signal already exists. Checking the
                # stale `action` param here meant the automatic retry could silently
                # create new signals without ever sending the "new signals" Telegram
                # summary — only an explicit ?action=generate cron-trigger hit this path.
                if (specialist_panel or {}).get("resolved_action") == "generate":
                    try:
                        from stocks.services.telegram_service import send_daily_picks_summary
                        send_daily_picks_summary()
                    except Exception as tg_sum_err:
                        logger.error("[TELEGRAM] Daily picks summary failed: %s", tg_sum_err)
            except Exception as spec_err:
                logger.error("[CRON] Specialist scan failed: %s", spec_err)

            # Send periodic running P&L update to Telegram
            # Fires on "update" action OR when today's specialist signals already exist
            try:
                from stocks.models import SignalHistory as _SH
                from stocks.services.signal_utils import IST as _IST
                from datetime import datetime as _dt
                today_has_signals = _SH.objects.filter(
                    category="specialist",
                    generated_at__date=_dt.now(tz=_IST).date()
                ).exists()
                if action == "update" or today_has_signals:
                    from stocks.services.telegram_service import send_periodic_pnl_updates
                    send_periodic_pnl_updates()
            except Exception as tg_err:
                logger.error("[TELEGRAM] Periodic P&L update failed: %s", tg_err)

            # ── OPTION BUYING SCAN (runs before intraday on purpose) ──
            # Its 2:30 PM hard time-stop is earlier than intraday's 3:20 PM cutoff, so it
            # needs to run while there's still time on its own clock — intraday's 500-symbol
            # sweep "takes many minutes" (see below), which would eat into that window if
            # option-buying ran after it. Runs after specialist though, preserving today's
            # rate-limit-driven ordering.
            try:
                from stocks.services.option_buying_service import get_option_buying_signals
                get_option_buying_signals(action=action)
            except Exception as ob_scan_err:
                logger.error("[CRON] Option-buying scan failed: %s", ob_scan_err)

            # ── INTRADAY SCAN (heavy, 500 symbols — runs last) ──
            try:
                get_live_signals(action=action)
            except Exception as intra_err:
                logger.error("[CRON] Intraday scan failed: %s", intra_err)

            # Periodic running P&L updates for intraday + option buying, added
            # 2026-07-28 at the account owner's explicit request — same 15-min
            # cadence as this scan cycle, same pattern as specialist's periodic
            # update above. Each is self-contained (checks its own live/active
            # signals, no-ops if none) so a failure in one can't block the other.
            try:
                from stocks.services.telegram_service import send_intraday_periodic_pnl_updates
                send_intraday_periodic_pnl_updates()
            except Exception as tg_err:
                logger.error("[TELEGRAM] Intraday periodic P&L update failed: %s", tg_err)

            try:
                from stocks.services.telegram_service import send_option_buying_periodic_pnl_updates
                send_option_buying_periodic_pnl_updates()
            except Exception as tg_err:
                logger.error("[TELEGRAM] Option-buying periodic P&L update failed: %s", tg_err)

    except Exception as e:
        logger.error("[CRON] Error in run_periodic_scanners: %s", e)

def get_performance_report(target_date: date | None = None):
    _dt = target_date if target_date else dj_timezone.now().date()
    signals = SignalHistory.objects.filter(generated_at__date=_dt).order_by("-generated_at")

    # Signals still ACTIVE (target/SL not yet hit — includes long_term, which never
    # auto-closes) have no exit_price yet, so without a live quote the UI has nothing
    # to show but "—" until the position eventually closes. Fetch current prices in
    # bulk so the report can show today's running (unrealized) P&L instead.
    active_symbols = list({s.symbol for s in signals if s.status == SignalHistory.Status.ACTIVE})
    live_prices = get_latest_prices(active_symbols) if active_symbols else {}

    history = []
    for s in signals:
        record = {
            "symbol": s.symbol,
            "type": s.signal_type,
            "category": s.category,
            "entry": float(s.entry_price),
            "target": float(s.target) if s.target is not None else None,
            "sl": float(s.stop_loss) if s.stop_loss is not None else None,
            "status": s.status,
            "exit_price": float(s.exit_price) if s.exit_price else None,
            "time": s.generated_at.astimezone(IST).strftime("%I:%M %p")
        }
        # Long-term is buy-and-hold — no target/SL, exit is via the hold rule instead.
        if s.category == "long_term":
            record["hold_rule"] = "Hold 1-2 yrs — exit if trend breaks below 200 EMA"

        if s.status == SignalHistory.Status.ACTIVE:
            current_price = live_prices.get(s.symbol)
            if current_price is not None:
                record["current_price"] = current_price

        # Inject Multi-Leg / Combined P&L if available
        if s.metadata:
            if 'final_pnl' in s.metadata:
                record['final_pnl'] = s.metadata['final_pnl']
            if 'final_pnl_pct' in s.metadata:
                record['final_pnl_pct'] = s.metadata['final_pnl_pct']
            # Risk-based size the signal was actually generated at. Surfaced so the
            # report shows realised P&L on the true position rather than recomputing
            # an equal-notional quantity the engine never traded.
            if s.metadata.get('qty'):
                record['qty'] = s.metadata['qty']
            if s.metadata.get('rupee_risk') is not None:
                record['rupee_risk'] = s.metadata['rupee_risk']
            # Why the position closed: LEVEL_HIT / TIME_STOP / SQUARE_OFF_CUTOFF /
            # DAILY_LOSS_LIMIT. Needed to attribute outcomes to the right exit rule.
            if s.metadata.get('exit_reason'):
                record['exit_reason'] = s.metadata['exit_reason']
            # Provenance for measuring relaxed-mode expected value separately.
            if 'relaxed' in s.metadata:
                record['relaxed'] = s.metadata['relaxed']

        history.append(record)

    # Split by category to match what PerformanceReports.jsx's per-strategy tabs
    # actually expect (intraday_history) — the flat `history` list above stayed
    # the only thing returned for a long time, so that tab silently rendered
    # empty regardless of date. specialist_history (Option Selling),
    # long_term_history, and option_buying_history are new — all of these
    # categories already live in SignalHistory and this function was already
    # date-aware, so splitting them out here needed no new querying, just
    # grouping what was already being fetched.
    intraday_history = [r for r in history if r["category"] == "intraday"]
    specialist_history = [r for r in history if r["category"] == "specialist"]
    long_term_history = [r for r in history if r["category"] == "long_term"]
    option_buying_history = [r for r in history if r["category"] == "option_buying"]

    return {
        "date": _dt.isoformat(),
        "total": signals.count(),
        "history": history,
        "intraday_history": intraday_history,
        "specialist_history": specialist_history,
        "long_term_history": long_term_history,
        "option_buying_history": option_buying_history,
    }
