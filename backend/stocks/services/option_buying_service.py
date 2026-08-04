from __future__ import annotations
import logging
from datetime import datetime, timedelta, time
from typing import Any

import pandas as pd
from django.conf import settings

from stocks.models import SignalHistory
from stocks.services import candle_store
from stocks.services.signal_utils import (
    IST, is_market_open, is_static_closed, round_to_tick, compute_session_vwap,
    compute_session_volume_profile, compute_adx, ta_sma, OPTION_BUYING_TIME_STOP,
    OPTION_BUYING_GENERATION_CUTOFF, OPTION_BUYING_GENERATION_START,
)
from django.core.cache import cache
from stocks.services.option_greeks_service import calculate_greeks, estimate_iv
from stocks.services.trading_engine.config import get_market_rules
from stocks.services.trading_engine.state_engine import (
    persist_live_signal_history as engine_persist_live_signal_history,
)

logger = logging.getLogger(__name__)

MAX_OPTION_BUY_SIGNALS_PER_SCAN = 3
CATEGORY = "option_buying"

# Fixed 2-lot rupee target/SL (replaced the old ADX-scaled 1.6x-2.0x target / fixed
# 0.625x SL at the account owner's explicit request 2026-07-31 — that formula wasn't
# a clean ratio and let SUNPHARMA 2020 CE run to +Rs.8,785 unrealized profit with no
# exit condition anywhere near that level before decaying back to a loss by the 2:30
# PM time-stop). Rupee-based, not percentage-based: a flat "x% of entry" target
# ignores lot size, so it treats a Rs.5 stock and a Rs.500 stock identically even
# though the real money outcome (move x lot_size x 2) differs enormously.
# Rs.5,000 profit : Rs.2,500 loss = clean 1:2 reward:risk. Matches the "P&L (2 Lots)"
# convention already used everywhere else for option buying (telegram_service.py).
OPTION_BUYING_PROFIT_RUPEES = 5000   # 2-lot profit target
OPTION_BUYING_LOSS_RUPEES = 2500     # 2-lot stop-loss

# Audit fix (C2): option buying — the platform's most leveraged, fastest-decaying
# strategy (immediate ACTIVE entry, no PENDING wait, Rs.2,500-5,000 per-trade swings)
# — had no session-level circuit breaker at all, unlike intraday's
# DAILY_HALT_CACHE_KEY/_enforce_daily_loss_limit. Same 2% default and same shared
# account-equity basis as intraday (see INTRADAY_ACCOUNT_EQUITY in intraday_service.py
# — CLAUDE.md documents it as "the real account size", not an intraday-only figure).
OPTION_BUYING_DAILY_LOSS_LIMIT_PCT = float(getattr(settings, "OPTION_BUYING_DAILY_LOSS_LIMIT_PCT", 2.0))
OPTION_BUYING_DAILY_HALT_CACHE_KEY = "option_buying_daily_loss_halt"


def _option_breakout_logic(
    ticker_sym: str,
    df: pd.DataFrame,
    relaxed: bool = False,
) -> dict[str, Any] | None:
    """
    Deliberately reimplements (does NOT call) the VA-Breakout trigger from
    intraday_service._volume_profile_logic — that function returns an equity-shaped
    candidate (stock entry/SL/target), not a direction signal we can hand off to
    strike selection. This is intentionally a STRICTER variant: option buyers pay a
    decaying premium and need real conviction, so on top of the same VA-breakout +
    vol_ratio>1.5 condition, this additionally requires VWAP alignment (not enforced
    by the breakout branch itself) and ADX>20 trend confirmation (no equivalent gate
    exists for equity signals, which tolerate chop better than a decaying option).

    relaxed=True lowers the volume/ADX bar (mirrors intraday_service's own
    strict/relaxed fallback) when the strict pass hasn't produced a setup for a
    while — see the is_fallback check in get_option_buying_signals().

    Evaluated on the last CLOSED bar only (same non-repainting rule as
    intraday_service — see CLAUDE.md). Fixed 2026-07-30: this used to accept an
    optional live WebSocket tick and trigger off that instead of the closed bar,
    which let a few-second intrabar spike through VAH/VAL fire an entry that
    reverted before the bar actually closed — a real cause of same-day losses.
    """
    if df is None or len(df) < 30:
        return None

    poc, vah, val, va_source = compute_session_volume_profile(df, bins=40)
    current_candle = df.iloc[-1]
    prev_candle = df.iloc[-2]
    price = float(current_candle["Close"])
    prev_price = float(prev_candle["Close"])

    vol_sma = ta_sma(df["Volume"], 10).iloc[-1]
    cur_vol = df["Volume"].iloc[-1]
    vol_ratio = cur_vol / vol_sma if vol_sma > 0 else 1.0

    vwap_series = compute_session_vwap(df)
    cur_vwap = float(vwap_series.iloc[-1])
    adx = compute_adx(df, 14)

    vol_min = 1.2 if relaxed else 1.5
    adx_min = 15 if relaxed else 20

    direction = None
    reason = ""
    if prev_price <= vah and price > vah and vol_ratio > vol_min and price > cur_vwap and adx > adx_min:
        direction = "BUY_CE"
        reason = "Value Area Bullish Breakout, VWAP+ADX confirmed"
    elif prev_price >= val and price < val and vol_ratio > vol_min and price < cur_vwap and adx > adx_min:
        direction = "BUY_PE"
        reason = "Value Area Bearish Breakdown, VWAP+ADX confirmed"

    if not direction:
        return None

    return {
        "direction": direction,
        "reason": reason,
        "price": round_to_tick(price),
        "vol_ratio": round(vol_ratio, 2),
        "adx": round(adx, 2),
        "vwap": round(cur_vwap, 2),
        "poc": round(poc, 2), "vah": round(vah, 2), "val": round(val, 2),
        "va_source": va_source,
    }


def select_option_buying_strike(symbol: str, spot: float, direction: str, svc) -> dict[str, Any] | None:
    """
    ATM/near-ATM strike selection for buying (opposite of delta_hedge_service's
    deep-OTM/high-theta selling logic — find_strike_by_delta there targets ~0.25
    delta; buyers want the strike to actually behave like the underlying, so this
    targets a 0.40-0.60 delta band around the nearest real tradable strike).
    """
    option_type = "CE" if direction == "BUY_CE" else "PE"

    atm_strike = svc.get_nearest_strike(symbol, spot)
    if not atm_strike:
        return None

    quote = svc.get_option_quote(symbol, atm_strike, option_type)
    if not quote or not quote.get("ltp"):
        return None

    premium = round_to_tick(float(quote["ltp"]))
    if premium <= 0:
        return None

    expiry_str = quote.get("expiry")
    try:
        expiry_dt = datetime.strptime(expiry_str, "%d%b%Y")
        t_days = max(1, (expiry_dt.date() - datetime.now().date()).days)
    except (TypeError, ValueError):
        t_days = 7

    sigma = estimate_iv(spot, atm_strike, t_days, premium, option_type)
    greeks = calculate_greeks(spot, atm_strike, t_days, sigma=sigma, option_type=option_type)
    delta = abs(greeks.get("delta", 0) or 0)
    if not (0.40 <= delta <= 0.60):
        logger.info("[OPTION_BUYING][REJECT] %s strike=%s delta=%.2f outside 0.40-0.60 band", symbol, atm_strike, delta)
        return None

    return {
        "strike": atm_strike,
        "option_type": option_type,
        "premium": premium,
        "expiry": expiry_str,
        "trading_symbol": quote.get("trading_symbol"),
        "delta": greeks.get("delta"),
        "theta": greeks.get("theta"),
        "sigma": round(sigma, 4) if sigma else None,
        "t_days": t_days,
    }


def _compute_target_sl(symbol: str, entry_premium: float) -> tuple[float, float]:
    """Fixed 2-lot rupee target/SL — see OPTION_BUYING_PROFIT_RUPEES/_LOSS_RUPEES above.
    Converts the rupee amounts to a premium price via this symbol's own lot size, so
    Rs.5,000 profit / Rs.2,500 loss means the same real money outcome regardless of
    how large a move that requires for this particular stock's premium."""
    from stocks.services.delta_hedge_service import get_lot_size
    lot_size = get_lot_size(symbol, "NSE")
    if lot_size <= 0:
        # Audit fix H18: get_lot_size() now returns 0 (not a fabricated 1) when it
        # can't resolve a real lot size. (0.0, 0.0) is a sentinel the caller's
        # existing `target <= entry_premium` viability check already discards —
        # avoids both a ZeroDivisionError and trading on a fake target/SL.
        return 0.0, 0.0
    per_rupee_move = lot_size * 2  # 2-lot P&L per Re.1 of premium move
    profit_move = OPTION_BUYING_PROFIT_RUPEES / per_rupee_move
    loss_move = OPTION_BUYING_LOSS_RUPEES / per_rupee_move
    target = round_to_tick(entry_premium + profit_move)
    sl = round_to_tick(entry_premium - loss_move)
    return target, sl


def _live_option_buying_payload() -> dict[str, Any]:
    """
    Deliberately does NOT use SignalHistorySerializer — that serializer's
    to_representation() only populates premium_cmp (and fetches a live quote) when
    category == 'option_selling'; for any other category (including this one) it
    silently omits premium_cmp. Rather than patch a shared serializer built around
    option-selling's specific needs, this uses its own direct field mapping, reading
    premium_cmp from the DB (kept fresh by update_option_buying_outcomes()) instead
    of fetching live on every page load.
    """
    live_rows = SignalHistory.objects.filter(
        category=CATEGORY,
        status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
    ).order_by("-generated_at")

    signals = [{
        "id": sig.id,
        "symbol": sig.symbol,
        "option_type": sig.option_type,
        "strike_price": float(sig.strike_price) if sig.strike_price else None,
        "entry": float(sig.entry_price) if sig.entry_price else None,
        "current_premium": float(sig.premium_cmp) if sig.premium_cmp is not None else None,
        "target": float(sig.target) if sig.target else None,
        "stop_loss": float(sig.stop_loss) if sig.stop_loss else None,
        "status": sig.status,
        "expiry": sig.option_expiry,
        "generated_at": sig.generated_at.isoformat() if sig.generated_at else None,
    } for sig in live_rows]

    return {
        "signals": signals,
        "signal_for": "Local DB live signals",
        "timestamp": datetime.now(tz=IST).isoformat(),
    }


def _enforce_option_buying_daily_loss_limit(now_ist: datetime) -> bool:
    """Flatten today's open option_buying positions and halt further generation once
    the day's realised + unrealised P&L breaches the limit — mirrors
    live_signal_service._enforce_daily_loss_limit's pattern for intraday (audit
    finding C2). Returns True if the halt is (now, or already) active.
    """
    from stocks.services.delta_hedge_service import get_lot_size
    from stocks.services.intraday_service import INTRADAY_ACCOUNT_EQUITY

    if cache.get(OPTION_BUYING_DAILY_HALT_CACHE_KEY):
        return True

    today = now_ist.date()
    rows = list(SignalHistory.objects.filter(category=CATEGORY, generated_at__date=today))
    if not rows:
        return False

    closed = {SignalHistory.Status.HIT_TARGET, SignalHistory.Status.HIT_SL}
    total = 0.0
    for s in rows:
        if s.entry_price is None:
            continue
        entry = float(s.entry_price)
        # Same 2-lot convention as _compute_target_sl — buying CE or PE is always a
        # long-premium trade, so P&L direction is always +1 (profit when premium rises).
        lot_size = get_lot_size(s.symbol, "NSE")
        if lot_size <= 0:
            # Audit fix H18: an unresolved lot size must not silently zero out this
            # row's contribution to the daily P&L sum (0 * anything = 0, understating
            # a real loss right when the kill switch most needs an accurate total).
            logger.warning("[OPTION_BUYING][KILL_SWITCH] Skipping %s in daily P&L sum — no real lot size resolved.", s.symbol)
            continue
        per_rupee_move = lot_size * 2
        if s.status in closed and s.exit_price is not None:
            total += (float(s.exit_price) - entry) * per_rupee_move
        elif s.status == SignalHistory.Status.ACTIVE and s.premium_cmp is not None:
            total += (float(s.premium_cmp) - entry) * per_rupee_move

    limit = INTRADAY_ACCOUNT_EQUITY * (OPTION_BUYING_DAILY_LOSS_LIMIT_PCT / 100.0)
    if total > -limit:
        return False

    logger.error(
        "[OPTION_BUYING][KILL_SWITCH] Daily P&L Rs.%.2f breached limit Rs.-%.2f — flattening.",
        total, limit,
    )

    for s in rows:
        if s.status != SignalHistory.Status.ACTIVE:
            continue
        entry = float(s.entry_price) if s.entry_price is not None else 0.0
        exit_px = float(s.premium_cmp) if s.premium_cmp is not None else entry
        s.status = SignalHistory.Status.HIT_TARGET if exit_px >= entry else SignalHistory.Status.HIT_SL
        s.exit_price = exit_px
        s.exit_time = datetime.now(tz=IST)
        meta = s.metadata or {}
        meta["exit_reason"] = "DAILY_LOSS_LIMIT"
        s.metadata = meta
        s.save(update_fields=["status", "exit_price", "exit_time", "metadata"])

    # Hold the halt until end of day so no later scan reopens positions.
    eod = now_ist.replace(hour=23, minute=59, second=0, microsecond=0)
    cache.set(OPTION_BUYING_DAILY_HALT_CACHE_KEY, True,
              timeout=max(60, int((eod - now_ist).total_seconds())))
    return True


def update_option_buying_outcomes() -> None:
    """
    Self-contained audit loop, mirroring delta_hedge_service's precedent of excluding
    option-selling ("specialist") from the shared update_signal_outcomes() and
    self-auditing instead — option-buying's premium-space math and hard time-stop
    don't fit that shared function's equity/commodity-oriented branches.
    """
    from stocks.services.truedata_service import get_truedata_instance

    now_ist = datetime.now(tz=IST)
    past_time_stop = now_ist.time() >= OPTION_BUYING_TIME_STOP

    live_rows = SignalHistory.objects.filter(category=CATEGORY, status=SignalHistory.Status.ACTIVE)
    # The per-signal audit loop below needs a broker handle and a non-empty book, but
    # the daily loss limit check after it must run regardless (same C1 pattern as
    # live_signal_service.update_signal_outcomes: an empty book is exactly the state
    # right after a string of stop-losses closes it, which is precisely when the kill
    # switch is needed most) — so neither an empty queryset nor a down broker skips it.
    svc = get_truedata_instance() if live_rows.exists() else None
    for sig in (live_rows if svc else []):
        try:
            quote = svc.get_option_quote(sig.symbol, float(sig.strike_price), sig.option_type, expiry=sig.option_expiry)
            entry_premium = float(sig.entry_price)
            target = float(sig.target) if sig.target else None
            stop_loss = float(sig.stop_loss) if sig.stop_loss else None

            if not quote or not quote.get("ltp"):
                # Audit fix (C3): a failed/empty quote must NOT also skip the mandatory
                # 2:30 PM force-close below — that used to `continue` right here,
                # letting a position with one bad audit tick (illiquid strike, transient
                # API hiccup) stay ACTIVE indefinitely with a stale premium straight
                # through market close. Only the target/SL cross-checks need a fresh
                # quote; the time-stop must fire unconditionally. Fall back to the last
                # known premium (or entry price if none yet) for the force-close fill.
                if not past_time_stop:
                    continue
                current_premium = float(sig.premium_cmp) if sig.premium_cmp is not None else entry_premium
                logger.warning(
                    "[OPTION_BUYING] %s quote unavailable at time-stop — force-closing at last known premium %.2f.",
                    sig.symbol, current_premium,
                )
            else:
                current_premium = round_to_tick(float(quote["ltp"]))

            new_status = None
            # Pessimistic by design (matches live_signal_service._scan_bars_for_exit): if a
            # single audit tick's premium has crossed both target and stop_loss, book the
            # stop first — resolving the ambiguity in the strategy's own favour would make
            # the P&L record flattering rather than accurate.
            if stop_loss is not None and current_premium <= stop_loss:
                new_status = SignalHistory.Status.HIT_SL
            elif target is not None and current_premium >= target:
                new_status = SignalHistory.Status.HIT_TARGET
            elif past_time_stop:
                # Hard time-stop: theta decay means every extra minute open is a cost,
                # regardless of P&L — force-exit at whatever the premium currently is.
                new_status = SignalHistory.Status.HIT_TARGET if current_premium >= entry_premium else SignalHistory.Status.HIT_SL

            if new_status:
                sig.status = new_status
                sig.exit_price = current_premium
                sig.exit_time = datetime.now(tz=IST)
                sig.premium_cmp = current_premium
                sig.save(update_fields=["status", "exit_price", "exit_time", "premium_cmp"])
                logger.info("[OPTION_BUYING] %s %s exited: status=%s premium %.2f -> %.2f",
                            sig.symbol, sig.option_type, new_status, entry_premium, current_premium)
            else:
                sig.premium_cmp = current_premium
                sig.save(update_fields=["premium_cmp"])
        except Exception as e:
            logger.error("[OPTION_BUYING] Outcome check failed for %s: %s", sig.symbol, e)
            continue

    # ── Daily Loss Limit Kill Switch (C2) ────────────────────────────────────────
    # Evaluated after outcomes are settled so realised/unrealised P&L is current —
    # same placement as live_signal_service's intraday equivalent.
    try:
        _enforce_option_buying_daily_loss_limit(now_ist)
    except Exception as exc:
        logger.error("[OPTION_BUYING][KILL_SWITCH] evaluation failed: %s", exc)


def get_option_buying_signals(action: str | None = None) -> dict[str, Any]:
    """Scan F&O-eligible stocks for high-conviction breakout setups and buy near-ATM
    CE/PE. Mirrors intraday_service.get_live_signals()'s structure (static/market-open
    checks, action router, stale-signal guard, scan-rate guard) applied to options."""
    if is_static_closed("NSE"):
        return {"signals": [], "market_status": "CLOSED", "reason": "market_closed_static"}

    market_open = is_market_open()
    if not market_open:
        return {"signals": [], "market_status": "CLOSED", "reason": "market_closed"}

    now_ist = datetime.now(tz=IST)

    # ── Daily Loss Limit Kill Switch (C2) ────────────────────────────────────────
    # Set by update_option_buying_outcomes() once today's realised + unrealised P&L
    # breaches OPTION_BUYING_DAILY_LOSS_LIMIT_PCT; positions are flattened there, here
    # we simply refuse to open anything new for the rest of the session — same
    # placement/pattern as intraday_service.get_live_signals()'s equivalent check.
    if cache.get(OPTION_BUYING_DAILY_HALT_CACHE_KEY):
        logger.warning("[OPTION_BUYING] Daily loss limit reached — signal generation halted for today.")
        payload = _live_option_buying_payload()
        payload.update({"market_status": "OPEN", "halted": True, "reason": "daily_loss_limit"})
        return payload

    # Stop opening new positions well before the hard time-stop (OPTION_BUYING_TIME_STOP,
    # 2:30 PM) that force-exits them regardless of P&L — a position needs real runway to
    # reach its 1.6x-2.0x premium target, not just a few minutes before being force-closed.
    # Existing ACTIVE/PENDING positions are untouched; this only gates NEW generation.
    if now_ist.time() >= OPTION_BUYING_GENERATION_CUTOFF:
        return _live_option_buying_payload()

    # Before OPTION_BUYING_GENERATION_START (10:00 AM), skip generation — ADX>20 trend
    # confirmation is unreliable on the still-unsettled opening range, and a bad option
    # entry (leveraged, decaying) is costlier than a bad equity one. 15 min later than
    # intraday's own opening-range skip for the same reason.
    if now_ist.time() < OPTION_BUYING_GENERATION_START:
        return _live_option_buying_payload()

    # Once-per-day generation cap (added 2026-07-29, same reasoning/pattern as
    # intraday_service.get_live_signals() — see that function's comment for the
    # rate-limit rationale). Gates on whether a generation ATTEMPT happened today,
    # not whether one succeeded, so a day with zero qualifying candidates doesn't
    # keep re-scanning the F&O universe every 15 min. Explicit action="generate"
    # (Force Scan) still bypasses this.
    today_key = now_ist.date().isoformat()
    generation_attempted_key = f"option_buying_generation_attempted_{today_key}"
    generation_already_attempted = cache.get(generation_attempted_key, False)
    resolved_action = action or ("update" if generation_already_attempted else "generate")

    if resolved_action == "update":
        return _live_option_buying_payload()

    cache.set(generation_attempted_key, True, timeout=12 * 3600)

    # ── Stale Signal Guard ── cancel PENDING/ACTIVE option_buying rows from previous days.
    stale_cancelled = SignalHistory.objects.filter(
        category=CATEGORY,
        status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
        generated_at__date__lt=now_ist.date(),
    ).update(status=SignalHistory.Status.CANCELLED)
    if stale_cancelled:
        logger.info("[OPTION_BUYING] Auto-cancelled %d stale signal(s) from previous day(s).", stale_cancelled)

    from stocks.services.truedata_service import get_truedata_instance
    svc = get_truedata_instance()
    if not svc:
        return {"signals": [], "market_status": "OPEN", "error": "service_unavailable"}

    # ── Scan Rate Guard ── same 5-min cooldown pattern as intraday.
    SCAN_KEY = "option_buying_last_full_scan"
    SCAN_COOLDOWN = 5 * 60
    live_count = SignalHistory.objects.filter(
        category=CATEGORY,
        status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
        generated_at__date=now_ist.date(),
    ).count()
    if cache.get(SCAN_KEY) and live_count > 0:
        return _live_option_buying_payload()
    cache.set(SCAN_KEY, True, timeout=SCAN_COOLDOWN)

    # Narrowed to NIFTY50 2026-07-28 at the account owner's explicit request — same
    # reasoning as intraday's universe narrowing (see shared/profiles.py). F&O
    # eligibility alone (get_fo_stocks()) pulled in names well outside NIFTY50.
    from stocks.services.market_data.download_queue import get_universe_symbols
    nifty50 = set(get_universe_symbols())
    fo_stocks = set(svc.get_fo_stocks()) & nifty50
    live_symbols = set(SignalHistory.objects.filter(
        category=CATEGORY,
        status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
    ).values_list("symbol", flat=True))
    scan_symbols = [s for s in fo_stocks if s not in live_symbols][:40]  # candidate_limit, matches MARKET_RULES

    # Same strict/relaxed fallback as intraday_service: if nothing has qualified in a
    # while, ease the volume/ADX bar rather than risk an empty scan every cycle. The
    # window here is much shorter (9:00 AM start via run_periodic_scanners, 2:30 PM
    # hard time-stop) than intraday's, so the thresholds are tighter than intraday's
    # own fallback trigger.
    last_signal = SignalHistory.objects.filter(
        category=CATEGORY, generated_at__date=now_ist.date()
    ).order_by("-generated_at").first()
    if last_signal:
        is_fallback = (now_ist - last_signal.generated_at).total_seconds() > 5400  # 90 min
    else:
        is_fallback = now_ist.time() > time(12, 0)

    signals_persisted = 0
    newly_created: list = []
    for s in scan_symbols:
        if signals_persisted >= MAX_OPTION_BUY_SIGNALS_PER_SCAN:
            break
        try:
            # Local-only read as of 2026-07-27 — deliberately NOT svc.get_candle_data()
            # or candle_store.get_candles() (either of which can still fall back to a
            # live REST call). Bars come solely from market_data/tick_aggregator.py,
            # which builds today's FIVE_MINUTE CandleBar rows from already-arrived
            # WebSocket ticks (see updater.py's tick_aggregator job) — zero Angel One
            # REST calls, so this scan can't trip the getCandleData circuit breaker.
            # Trade-off: only today's bars exist this way (no REST-fetched history),
            # so len(df) < 10 below waits ~50 min into the session for enough of
            # today's own bars to accumulate, same as any cold start.
            df = candle_store.load_bars(s, "FIVE_MINUTE", now_ist - timedelta(days=2), exchange="NSE")
            if df.empty or len(df) < 10:
                continue

            breakout = _option_breakout_logic(s, df, relaxed=is_fallback)
            if not breakout:
                continue

            spot = breakout["price"]
            strike_info = select_option_buying_strike(s, spot, breakout["direction"], svc)
            if not strike_info:
                continue

            entry_premium = strike_info["premium"]
            target, sl = _compute_target_sl(s, entry_premium)
            if target <= entry_premium or sl >= entry_premium or sl <= 0:
                continue

            result = {
                "symbol": s,
                # BUY_CE/BUY_PE are both an option BUY — category isolation already
                # distinguishes this from equity BUY/SELL signals, so this is purely
                # for readability in the DB/UI, not a lifecycle distinction.
                "signal": "BUY_CE" if breakout["direction"] == "BUY_CE" else "BUY_PE",
                "entry": entry_premium,
                "stop_loss": sl,
                "target": target,
                "rr": round(abs(target - entry_premium) / abs(entry_premium - sl), 2),
                "reason": f"[OPTION_BUYING] {breakout['reason']} (spot={spot}, delta={strike_info['delta']:.2f})",
                # Immediate market entry at the live premium already fetched above —
                # unlike equity signals (which often wait for a future breakout level
                # to be touched), there's no "pending" state to wait through here.
                "status": SignalHistory.Status.ACTIVE,
                "strike_price": strike_info["strike"],
                "option_type": strike_info["option_type"],
                "premium_cmp": entry_premium,
                "expiry": strike_info["expiry"],
                "active_time": datetime.now(tz=IST),
                "metadata": {
                    "spot_at_entry": spot,
                    "adx": breakout["adx"],
                    "vwap": breakout["vwap"],
                    "poc": breakout["poc"], "vah": breakout["vah"], "val": breakout["val"],
                    "delta": strike_info["delta"], "theta": strike_info["theta"],
                    "sigma": strike_info["sigma"], "t_days": strike_info["t_days"],
                    "trading_symbol": strike_info["trading_symbol"],
                },
            }
            signal_row, is_new = engine_persist_live_signal_history(result, CATEGORY, get_market_rules(CATEGORY))
            signals_persisted += 1
            if is_new:
                newly_created.append(signal_row)
            logger.info("[OPTION_BUYING] Persisted %s %s strike=%s premium=%.2f target=%.2f sl=%.2f",
                        s, result["option_type"], result["strike_price"], entry_premium, target, sl)
        except Exception as e:
            logger.error("[OPTION_BUYING] Error scanning %s: %s", s, e, exc_info=True)
            continue

    if newly_created:
        try:
            from stocks.services.telegram_service import send_option_buying_new_signals
            send_option_buying_new_signals(newly_created)
        except Exception as tg_err:
            logger.error("[TELEGRAM] Option-buying new-signal alert failed: %s", tg_err)
    elif not generation_already_attempted:
        # Day's first (now only) generation attempt found nothing — say so once
        # rather than leaving silence to be interpreted as a bug.
        try:
            from stocks.services.telegram_service import send_no_setup_today
            send_no_setup_today("OPTION BUYING", "OPTION_BUYING_NO_SETUP")
        except Exception as tg_err:
            logger.error("[TELEGRAM] Option-buying no-setup notice failed: %s", tg_err)

    payload = _live_option_buying_payload()
    payload.update({"market_status": "OPEN", "scanned": len(scan_symbols), "persisted": signals_persisted})
    return payload
