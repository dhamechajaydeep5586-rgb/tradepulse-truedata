from __future__ import annotations
import logging
import pandas as pd
import time as time_module
from datetime import datetime, timezone, time, timedelta
from typing import Any
from stocks.models import SignalHistory, IndexConstituent, Notification
from stocks.serializers import SignalHistorySerializer
from stocks.services.signal_utils import (
    IST, MARKET_OPEN, MARKET_CLOSE, INTRADAY_SIGNAL_CUTOFF, INTRADAY_GENERATION_CUTOFF,
    INTRADAY_GENERATION_START,
    is_market_open, round_to_tick, compute_atr, compute_session_vwap,
    compute_volume_profile, compute_session_volume_profile, compute_compression,
    load_symbols_from_stock_table,
    compute_triple_ema, compute_atr_zones, ta_sma
)
from django.conf import settings
from django.core.cache import cache
from stocks.services.market_intelligence_service import (
    is_sideways_market, is_trading_window_active, get_standard_market_state
)
from stocks.services.shared.sector import sector_strength_from_candidates
from stocks.services.trading_engine.config import get_market_rules
from stocks.services.trading_engine.state_engine import (
    is_signal_active as engine_is_signal_active,
    persist_live_signal_history as engine_persist_live_signal_history,
)

logger = logging.getLogger(__name__)

# ── Cost & Risk Model ────────────────────────────────────────────────────────────
# See doc/INSTITUTIONAL_AUDIT_INTRADAY.md §0.1 and §5 for the derivation.
#
# Round-trip friction for Indian intraday equity, as % of notional:
#   brokerage (₹20 x 2 legs)     0.040%
#   STT (0.025%, sell side only) 0.025%
#   exchange transaction charges 0.006%
#   stamp duty (buy side)        0.003%
#   SEBI turnover                0.0002%
#   GST (18% on brokerage + txn) 0.008%
#   bid-ask crossing + slippage  0.040%
#                                ───────
#                                ≈0.122%  — rounded up to 0.14% for a safety margin
ROUND_TRIP_COST_PCT = float(getattr(settings, "INTRADAY_ROUND_TRIP_COST_PCT", 0.14))

# A trade whose target is not at least this multiple of round-trip cost cannot pay for
# itself. Break-even win rate is (R + f) / 3R at 2:1 RR — with f = 0.14% and the old
# 0.8xATR(5min) stop (R ~= 0.16%), that demanded a ~62% hit rate against a 33% gross
# break-even. This gate is arithmetic, not prediction: it removes trades whose expected
# value is negative regardless of how good the signal is.
MIN_TARGET_COST_MULTIPLE = float(getattr(settings, "INTRADAY_MIN_TARGET_COST_MULTIPLE", 3.0))

# ── Position Sizing ──────────────────────────────────────────────────────────────
# Fixed rupee risk per trade (replaced the old equity% x regime-scalar sizing at the
# account owner's explicit request 2026-07-31, same "flat rupee, nothing else" rule
# applied to option_buying_service's target/SL the same day). Every trade now risks
# exactly INTRADAY_FIXED_RISK_RUPEES, and since target is always exactly 2R (see the
# tgt = entry +/- risk*2.0 lines below), every trade also targets exactly 2x that —
# a clean 1:2, matching option buying's Rs.5,000/Rs.2,500 rule.
INTRADAY_ACCOUNT_EQUITY = float(getattr(settings, "INTRADAY_ACCOUNT_EQUITY", 500_000.0))
INTRADAY_FIXED_RISK_RUPEES = float(getattr(settings, "INTRADAY_FIXED_RISK_RUPEES", 2500.0))

# Per-name notional ceiling, as % of equity. Set at 100% deliberately: risking 0.30%
# of equity behind a 0.30% stop *requires* ~100% of equity in notional — that is the
# arithmetic of the stop distance, not leverage creep. A tighter cap would bind on
# ordinary trades and silently override risk parity instead of bounding it, so the
# per-name cap is an outlier guard (it only engages on unusually tight stops) and the
# gross cap below does the actual portfolio-level work.
INTRADAY_MAX_POSITION_PCT = float(getattr(settings, "INTRADAY_MAX_POSITION_PCT", 100.0))

# Portfolio ceiling across all concurrent intraday positions, as % of equity. With 5
# slots this bounds the book at 3x equity, within normal MIS intraday leverage.
INTRADAY_MAX_GROSS_EXPOSURE_PCT = float(
    getattr(settings, "INTRADAY_MAX_GROSS_EXPOSURE_PCT", 300.0)
)

# Kill switch: once the day's realised + unrealised intraday P&L breaches this, open
# positions are flattened and no new signals are generated for the rest of the session.
INTRADAY_DAILY_LOSS_LIMIT_PCT = float(getattr(settings, "INTRADAY_DAILY_LOSS_LIMIT_PCT", 2.0))

# Set by the auditor (live_signal_service) when the daily loss limit trips.
DAILY_HALT_CACHE_KEY = "intraday_daily_loss_halt"

# See the note at the `is_fallback` assignment: relaxed mode loosened standards exactly
# when conditions were worst. Off by default; flip only to measure it.
INTRADAY_ENABLE_RELAXED_MODE = bool(getattr(settings, "INTRADAY_ENABLE_RELAXED_MODE", False))


def position_size(entry: float, stop_loss: float,
                  size_multiplier: float = 1.0) -> tuple[int, float]:
    """Fixed rupee risk: qty = INTRADAY_FIXED_RISK_RUPEES / per-share risk, no scaling.

    Returns (qty, rupee_risk). qty is additionally capped by INTRADAY_MAX_POSITION_PCT
    so a very tight stop cannot translate into an oversized notional position.

    `size_multiplier` (the old regime scalar, 0.5x-1.25x) is kept in the signature so
    existing call sites don't need to change, but is no longer applied — risk is flat
    regardless of regime, per the "nothing else" rule above.
    """
    risk_per_share = abs(float(entry) - float(stop_loss))
    if risk_per_share <= 0 or entry <= 0:
        return 0, 0.0

    risk_budget = INTRADAY_FIXED_RISK_RUPEES
    qty = int(risk_budget // risk_per_share)

    max_notional = INTRADAY_ACCOUNT_EQUITY * (INTRADAY_MAX_POSITION_PCT / 100.0)
    qty = min(qty, int(max_notional // float(entry)))

    if qty <= 0:
        return 0, 0.0
    return qty, round(qty * risk_per_share, 2)

def _build_intraday_candidate(
    *,
    ticker_sym: str,
    strategy_key: str,
    strategy_name: str,
    signal: str,
    price: float,
    entry: float,
    stop_loss: float,
    target: float,
    rr: float,
    reason: str,
    score: float,
    priority: int,
    vol_ratio: float,
    relaxed: bool = False,
    size_multiplier: float = 1.0,
    liquidity: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not signal:
        return None
    entry = round_to_tick(entry)
    stop_loss = round_to_tick(stop_loss)
    target = round_to_tick(target)
    if abs(stop_loss - entry) == 0 or entry <= 0:
        return None
    rr = round(abs(target - entry) / abs(stop_loss - entry), 2)
    if rr < 1.5:
        return None

    # ── Entry fill models crossing the spread (§3.1g) ────────────────────────────
    # A signal computed at the mid is not obtainable at the mid: a buy lifts the offer,
    # a sell hits the bid. Recording the touch price as the entry silently books
    # half-spread of phantom edge on every trade, and it is the same assumption the
    # backtester was corrected for — live and backtest must fill identically.
    from stocks.services.trading_engine.cost_model import DEFAULT_COST_MODEL as _CM
    entry = round_to_tick(_CM.slipped_fill(entry, is_buy=(signal == "BUY"), symbol=ticker_sym))
    if abs(stop_loss - entry) == 0:
        return None

    # Audit fix M11: rr was computed above against the pre-slippage entry and never
    # recomputed — a candidate that narrowly cleared the 1.5 RR gate before the
    # slippage-adjusted fill could be persisted and displayed with a real RR below
    # the platform's own stated minimum. Recompute against the actual fill price
    # and re-check the same threshold.
    rr = round(abs(target - entry) / abs(stop_loss - entry), 2)
    if rr < 1.5:
        return None

    # ── Risk-based sizing (§5.1) ─────────────────────────────────────────────────
    # Sized before the cost gate because real friction depends on notional: the ₹20
    # brokerage cap means cost-as-a-percentage falls as position size rises.
    qty, rupee_risk = position_size(entry, stop_loss, size_multiplier)
    if qty <= 0:
        return None

    # ── Cost gate: reject trades that cannot pay for themselves (§0.1) ───────────
    # Priced with the SAME cost model the backtester uses, on this trade's actual
    # notional. A hardcoded constant here would mean live and backtest applied different
    # gates — the precise kind of divergence that makes a backtest untrustworthy.
    # Market impact is only estimable when ADV and volatility are known. Where they
    # aren't, fall back to the conservative flat constant rather than silently pricing
    # impact at zero — under-charging cost is what lets negative-EV trades through.
    from stocks.services.trading_engine.cost_model import DEFAULT_COST_MODEL
    if liquidity and liquidity.get("adv_inr"):
        cost_pct = DEFAULT_COST_MODEL.round_trip_pct(
            symbol=ticker_sym, price=entry, qty=qty,
            adv_inr=liquidity.get("adv_inr"),
            daily_vol_pct=liquidity.get("daily_vol_pct"),
        )
    else:
        cost_pct = ROUND_TRIP_COST_PCT
    target_pct = abs(target - entry) / entry * 100.0
    min_target_pct = MIN_TARGET_COST_MULTIPLE * cost_pct
    if target_pct < min_target_pct:
        logger.debug(
            "[INTRADAY][COST_GATE] %s rejected: target %.3f%% < required %.3f%% (cost %.4f%%)",
            ticker_sym, target_pct, min_target_pct, cost_pct,
        )
        return None

    payload = {
        "symbol": ticker_sym.replace(".NS", ""),
        "price": round_to_tick(price),
        "signal": signal,
        "entry": entry,
        "stop_loss": stop_loss,
        "target": target,
        "rr": rr,
        "vol_ratio": vol_ratio,
        "reason": f"[{strategy_key}] {reason}",
        "category": "intraday",
        "strategy_key": strategy_key,
        "strategy_name": strategy_name,
        "score": round(score, 2),
        "priority": priority,
        # Sizing + cost economics, persisted to metadata so realised performance can be
        # measured against the assumptions the signal was actually generated under.
        "qty": qty,
        "rupee_risk": rupee_risk,
        "target_pct": round(target_pct, 3),
        "cost_pct": round(cost_pct, 4),
        # Provenance for measuring relaxed-mode EV separately before deciding whether
        # the fallback is worth keeping at all (§6.4).
        "relaxed": relaxed,
    }
    if extra:
        payload.update(extra)
    return payload

def _volume_profile_logic(
    ticker_sym: str,
    df: pd.DataFrame,
    nifty_bias: str = "SIDEWAYS",
    relaxed: bool = False,
    size_multiplier: float = 1.0,
    liquidity: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Institutional Volume Profile Specialist.
    Analyzes price interaction with POC, VAH, and VAL.

    Setups:
    1. POC Flip: Bullish/Bearish sentiment shift at Point of Control.
    2. VA Breakout: Momentum drive breaking out of the 70% Value Area.
    3. Structural Rejection: Price bounce off VAL or rejection at VAH.

    Evaluated strictly on CLOSED candles — see the bar-trimming note below.

    `nifty_bias` is the index directional read (BULLISH/BEARISH/SIDEWAYS) from
    get_standard_market_state()["directional_bias"]. It gates the mean-reversion
    trigger only.

    relaxed=True lowers the volume-confirmation bar on each trigger and drops the
    directional gate on VA Rejection — used as a same-candle fallback (see
    `is_fallback` in get_live_signals) when the strict thresholds haven't produced a
    setup for a while. Relaxed signals are tagged in metadata so their expected value
    can be measured separately.
    """
    try:
        # Need 31 so that 30 remain after dropping the forming bar below.
        if df is None or len(df) < 31: return []

        # ── Drop the still-forming bar (§3.1a) ──────────────────────────────────
        # Angel One's candle API returns the current, incomplete bar as the last row.
        # Evaluating triggers on it made signals repaint: a setup could appear at
        # minute 2 of a 5-min bar and be gone by minute 5. It also broke the volume
        # filter — a partial bar's volume is understated early and grows through the
        # bar, so `vol_ratio > 1.5` was effectively measuring "how far into this bar
        # are we" rather than conviction, biasing every signal toward the bar's tail.
        # Working only on closed bars is also what makes live agree with the backtest.
        df = df.iloc[:-1]

        # ── Session-anchored value area (§3.1b) ─────────────────────────────────
        # A profile spanning two sessions blends yesterday's volume distribution into
        # today's levels, so VAH/VAL end up marking prices that mattered in a session
        # that has already closed. Use today's developing profile, or the prior day's
        # completed profile early on when today is still too thin to be meaningful.
        poc, vah, val, va_source = compute_session_volume_profile(df, bins=40)
        if poc <= 0:
            return []

        current_candle = df.iloc[-1]
        prev_candle = df.iloc[-2]
        price = float(current_candle["Close"])
        prev_price = float(prev_candle["Close"])

        # Volume Confirmation
        vol_sma = ta_sma(df["Volume"], 10).iloc[-1]
        cur_vol = df["Volume"].iloc[-1]
        vol_ratio = cur_vol / vol_sma if vol_sma > 0 else 1.0

        poc_flip_vol_min = 1.0 if relaxed else 1.2
        va_breakout_vol_min = 1.2 if relaxed else 1.5
        va_rejection_vol_min = 0.9 if relaxed else 1.1

        atr = compute_atr(df, 14)
        compression = compute_compression(df)
        cur_vwap = float(compute_session_vwap(df).iloc[-1])

        # ── Collect EVERY trigger that fires (§3.1c) ────────────────────────────
        # Previously an exclusive elif chain hardcoded POC > Breakout > Rejection, an
        # ordering that was asserted and never validated, and which meant only one
        # candidate per symbol could ever exist. Emitting all of them lets the
        # cross-sectional ranking model decide on evidence instead.
        fired: list[tuple[str, str, float]] = []   # (direction, reason, legacy_score)

        # Trigger 1 — POC Flip (momentum)
        if prev_price < poc and price > poc and vol_ratio > poc_flip_vol_min:
            fired.append(("BUY", "POC Bullish Flip", 4.5))
        elif prev_price > poc and price < poc and vol_ratio > poc_flip_vol_min:
            fired.append(("SELL", "POC Bearish Flip", 4.5))

        # Trigger 2 — Value Area Breakout (momentum)
        if prev_price <= vah and price > vah and vol_ratio > va_breakout_vol_min:
            fired.append(("BUY", "Value Area Bullish Breakout", 4.0))
        elif prev_price >= val and price < val and vol_ratio > va_breakout_vol_min:
            fired.append(("SELL", "Value Area Bearish Breakdown", 4.0))

        # Trigger 3 — Value Area Rejection (mean reversion). The only mean-reverting
        # trigger, so the only one that needs the index directional gate: buying a
        # value-area bounce while the index itself is selling off is a materially worse
        # bet. Reads `directional_bias`, which actually emits BULLISH/BEARISH — the old
        # code compared against `market_state`, which never does, so it never blocked.
        if price >= val and prev_candle["Low"] <= val and current_candle["Close"] > current_candle["Open"]:
            if (relaxed or nifty_bias != "BEARISH") and vol_ratio > va_rejection_vol_min:
                fired.append(("BUY", "Value Area Low Rejection", 3.5))
        elif price <= vah and prev_candle["High"] >= vah and current_candle["Close"] < current_candle["Open"]:
            if (relaxed or nifty_bias != "BULLISH") and vol_ratio > va_rejection_vol_min:
                fired.append(("SELL", "Value Area High Rejection", 3.5))

        # Trigger 4 — Volatility compression breakout (§3.2 rank 3). Volatility
        # clustering means contraction precedes expansion; entering while range is
        # compressed buys a small stop against a large potential move. That improves R
        # directly, which is the structural fix for the cost-gate problem rather than a
        # prediction improvement.
        if compression["is_compressed"] and (compression["is_nr7"] or vol_ratio > 1.3):
            if price > cur_vwap and price > poc:
                fired.append(("BUY", "Compression Breakout", 4.2))
            elif price < cur_vwap and price < poc:
                fired.append(("SELL", "Compression Breakdown", 4.2))

        if not fired:
            return []

        extra = {
            "poc": round(poc, 2), "vah": round(vah, 2), "val": round(val, 2),
            "vwap": round(cur_vwap, 2), "atr": round(atr, 2),
            "va_source": va_source,
            "compression": compression["bandwidth_pct"],
        }

        out: list[dict[str, Any]] = []
        for signal_dir, strategy_reason, legacy_score in fired:
            if signal_dir == "BUY":
                entry = price
                sl = min(val, entry - (atr * 0.8))
                tgt = entry + (abs(entry - sl) * 2.0)
            else:
                entry = price
                sl = max(vah, entry + (atr * 0.8))
                tgt = entry - (abs(sl - entry) * 2.0)

            cand = _build_intraday_candidate(
                ticker_sym=ticker_sym,
                strategy_key="VOL_PROFILE",
                strategy_name="Volume Profile",
                signal=signal_dir,
                price=price,
                entry=entry,
                stop_loss=sl,
                target=tgt,
                rr=2.0,
                reason=strategy_reason,
                score=legacy_score,
                priority=1,
                vol_ratio=round(vol_ratio, 2),
                relaxed=relaxed,
                size_multiplier=size_multiplier,
                liquidity=liquidity,
                extra=extra,
            )
            if cand:
                out.append(cand)
        return out
    except Exception as e:
        logger.error(f"Volume Profile error for {ticker_sym}: {e}")
        return []

def _relative_strength(df: pd.DataFrame, benchmark_return: float) -> float:
    """Stock's session return minus the index's, in percentage points.

    Cross-sectional relative strength is the strongest replicated equity anomaly
    (Jegadeesh-Titman and three decades of follow-up), which is why it carries the
    largest weight in the ranking model.
    """
    try:
        session = df[df.index.date == df.index[-1].date()]
        if len(session) < 2:
            session = df.tail(12)
        stock_ret = (float(session["Close"].iloc[-1]) / float(session["Close"].iloc[0]) - 1.0) * 100.0
        return round(stock_ret - benchmark_return, 3)
    except Exception:
        return 0.0


def _trend_quality(df: pd.DataFrame) -> float:
    """ADX-based trend cleanliness, normalised to roughly 0..1."""
    try:
        from stocks.services.signal_utils import compute_adx
        return float(min(compute_adx(df, 14) / 40.0, 1.0))
    except Exception:
        return 0.0


def _live_intraday_payload() -> dict[str, Any]:
    """Return the current live intraday rows from the local database."""
    live_rows = SignalHistory.objects.filter(
        category="intraday",
        status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
    ).defer('metadata').order_by("-generated_at")
    serializer = SignalHistorySerializer(live_rows, many=True)
    return {
        "signals": serializer.data,
        "signal_for": "Local DB live signals",
        "scanned": cache.get("intraday_last_processed_count", 0),
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }

def get_live_signals(action: str | None = None) -> dict[str, Any]:
    """High-performance optimized scan for Intraday Equity Signals."""
    from stocks.services.signal_utils import is_static_closed, is_market_open
    
    # 0. Total Silence Check (Static)
    if is_static_closed("NSE"):
        return {"signals": [], "market_status": "CLOSED", "reason": "market_closed_static"}

    market_open = is_market_open()
    if not market_open:
        logger.info("[INTRADAY] Market closed or holiday, skipping scan.")
        return {"signals": [], "market_status": "CLOSED", "reason": "market_closed"}

    now_ist = datetime.now(tz=IST)  # defined once here, used by all guards below

    # ── Daily Loss Limit Kill Switch (§5.4) ──────────────────────────────────────
    # Set by the auditor in live_signal_service once the day's realised + unrealised
    # intraday P&L breaches INTRADAY_DAILY_LOSS_LIMIT_PCT. Positions are flattened
    # there; here we simply refuse to open anything new for the rest of the session.
    if cache.get(DAILY_HALT_CACHE_KEY):
        logger.warning("[INTRADAY] Daily loss limit reached — signal generation halted for today.")
        payload = _live_intraday_payload()
        payload.update({
            "market_status": "OPEN",
            "scanned": 0,
            "halted": True,
            "reason": "daily_loss_limit",
        })
        return payload

    # ── Strict Action and Generation Router ──────────────────────────────────
    # Retry every periodic tick (~15 min) until a signal actually lands, not just
    # INTRADAY_MAX_GENERATION_ATTEMPTS (4) times then going silent for the rest of
    # the day — same fix as option_buying_service.py / delta_hedge_service.py's
    # equivalent gates. Was capped specifically to avoid repeated full ~100-symbol
    # universe scans (150-200 REST calls each) contributing to TrueData rate-limit
    # exhaustion on a bad day — removing the cap trades that protection for the
    # account owner's explicit ask (2026-08-06) of continuous retries. If rate-limit
    # exhaustion from repeated all-zero scans becomes a real problem, re-add a cap
    # here rather than assuming it won't recur.
    today_key = now_ist.date().isoformat()
    generation_attempted_key = f"intraday_generation_attempts_{today_key}"
    attempts_so_far = cache.get(generation_attempted_key, 0)
    is_first_attempt_today = attempts_so_far == 0
    today_has_any_signal = SignalHistory.objects.filter(
        category="intraday", generated_at__date=now_ist.date(),
    ).exists()

    resolved_action = action
    if resolved_action is None:
        resolved_action = "update" if today_has_any_signal else "generate"

    # Before INTRADAY_GENERATION_START (9:45 AM), skip generation entirely — the opening
    # 9:15-9:45 range is dominated by gap-driven noise, and VAH/VAL/POC crosses in it are
    # disproportionately false breakouts. Applies even to an explicit generate/Force Scan;
    # neither can manufacture a reliable setup out of an unsettled opening range.
    if resolved_action == "generate" and now_ist.time() < INTRADAY_GENERATION_START:
        logger.info("[INTRADAY] Before generation start (%s) — skipping scan, opening range still settling.", INTRADAY_GENERATION_START)
        payload = _live_intraday_payload()
        payload.update({
            "sentiment": cache.get("intraday_nifty_sentiment", "SIDEWAYS"),
            "market_status": "OPEN",
            "scanned": 0,
            "reason": "before_generation_start",
        })
        return payload

    if resolved_action == "update":
        logger.info("[INTRADAY] skipping signal scan (update action or today's signal already exists). Returning live payload.")
        cached_sentiment = cache.get("intraday_nifty_sentiment", "SIDEWAYS")
        payload = _live_intraday_payload()
        payload.update({
            "sentiment": cached_sentiment,
            "market_status": "OPEN" if market_open else "CLOSED",
            "scanned": 0,
            "timeframe": "15-min update"
        })
        return payload

    # Stop opening new positions well before the hard force-close cutoff
    # (INTRADAY_SIGNAL_CUTOFF, 3:20 PM) that expires anything still ACTIVE/PENDING —
    # a signal minted right against that cutoff had almost no runway to resolve on
    # its own. Existing ACTIVE/PENDING positions are untouched; this only gates
    # opening NEW ones.
    if now_ist.time() >= INTRADAY_GENERATION_CUTOFF:
        logger.info("[INTRADAY] Past generation cutoff (%s) — skipping new scan, existing positions still tracked.", INTRADAY_GENERATION_CUTOFF)
        cached_sentiment = cache.get("intraday_nifty_sentiment", "SIDEWAYS")
        payload = _live_intraday_payload()
        payload.update({
            "sentiment": cached_sentiment,
            "market_status": "OPEN",
            "scanned": 0,
            "reason": "generation_cutoff",
        })
        return payload

    logger.info("[INTRADAY] Resolved action: GENERATE. Proceeding with active scan...")
    cache.set(generation_attempted_key, attempts_so_far + 1, timeout=12 * 3600)
    # ────────────────────────────────────────────────────────────────────────

    # Liquidity- and cost-filtered universe. Index membership alone is a market-cap
    # proxy, not a tradability test — a name whose spread exceeds the target is
    # unrunnable regardless of signal quality.
    from stocks.services.shared.universe import get_trading_universe
    from stocks.services import candle_store
    _universe = get_trading_universe()
    symbols = _universe.get("symbols") or load_symbols_from_stock_table()
    universe_stats = _universe.get("stats", {})
    sector_map = _universe.get("sectors", {})

    # Event blackout: F&O ban period and the earnings window. Both inject jump risk the
    # stop cannot bound — a gap through a stop is not a 1R loss.
    from stocks.services.event_filter_service import filter_symbols
    symbols, _excluded_events = filter_symbols(symbols)

    # ── Stale Signal Guard ───────────────────────────────────────────────────
    # Cancel any PENDING/ACTIVE intraday signals from previous trading days.
    # This prevents stale signals accumulating across days (the 121-signal bug).
    stale_cancelled = SignalHistory.objects.filter(
        category="intraday",
        status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
        generated_at__date__lt=now_ist.date(),
    ).update(status=SignalHistory.Status.CANCELLED)
    if stale_cancelled:
        logger.info("[INTRADAY] Auto-cancelled %d stale signal(s) from previous day(s).", stale_cancelled)
    # ────────────────────────────────────────────────────────────────────────

    live_rows = SignalHistory.objects.filter(
        category="intraday",
        status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
    ).defer('metadata').order_by("-generated_at")
    
    live_symbols = set(live_rows.values_list("symbol", flat=True))
    scan_symbols = [s for s in symbols if s not in live_symbols]
    
    MAX_SIGNALS_PER_SCAN = 5  # Increased to 5 to provide more Sniper setups
    signals_persisted = 0

    logger.info(
        "[INTRADAY][SCAN_START] market_open=%s total_symbols=%d live_signals=%d scan_symbols=%d cap=%d",
        market_open,
        len(symbols),
        len(live_symbols),
        len(scan_symbols),
        MAX_SIGNALS_PER_SCAN,
    )

    from stocks.services.truedata_service import get_truedata_instance
    svc = get_truedata_instance()
    if not svc:
        return {"signals": [], "market_status": "OPEN", "error": "service_unavailable"}

    import time as _time

    # ── Scan Rate Guard ──────────────────────────────────────────────────
    # Full scan is expensive (HTTP calls per stock). Only run once per 5 min.
    INTRADAY_SCAN_KEY = "intraday_last_full_scan"
    SCAN_COOLDOWN = 5 * 60  # 5 minutes
    last_scan = cache.get(INTRADAY_SCAN_KEY)
    live_count = SignalHistory.objects.filter(
        category="intraday",
        status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
        generated_at__date=now_ist.date(),
    ).count()
    if last_scan and live_count > 0:
        logger.info(
            "[INTRADAY] Scan cooldown active (%ds ago, %d live signals). Returning DB payload.",
            int(_time.time() - last_scan), live_count,
        )
        cached_sentiment = cache.get("intraday_nifty_sentiment", "SIDEWAYS")
        payload = _live_intraday_payload()
        payload.update({"sentiment": cached_sentiment, "market_status": "OPEN", "scanned": 0, "timeframe": "5-min refresh"})
        return payload
    cache.set(INTRADAY_SCAN_KEY, _time.time(), timeout=SCAN_COOLDOWN)
    # ────────────────────────────────────────────────────────────────────────

    # 1. Regime — two-axis model (trend x volatility) with hysteresis. This decides
    # WHICH TRIGGER FAMILIES may fire at all, which matters more than entry quality:
    # momentum and mean-reversion triggers have opposite-signed EV by regime, so firing
    # both unconditionally guarantees half the signal population is counter-regime.
    from stocks.services.regime_service import get_regime, strategy_allowed, NIFTY50_TOKEN
    regime = get_regime(svc)
    nifty_trend = "SIDEWAYS" if regime.trend_state == "NEUTRAL" else "TRENDING"
    nifty_bias = regime.directional_bias

    # Benchmark session return — the baseline every candidate's relative strength is
    # measured against. Local-only read as of 2026-07-27 — see the scan-symbol read
    # below for why (same tick_aggregator-backed CandleBar source, no REST call).
    nifty_ret = 0.0
    try:
        _ndf = candle_store.load_bars(
            "NIFTY50_INDEX", "FIVE_MINUTE",
            now_ist.replace(hour=9, minute=15, second=0, microsecond=0),
            exchange="NSE",
        )
        if _ndf is not None and len(_ndf) > 1:
            nifty_ret = (float(_ndf["Close"].iloc[-1]) / float(_ndf["Close"].iloc[0]) - 1.0) * 100.0
    except Exception:
        pass

    token_map = svc.get_token_map(scan_symbols, exchange="NSE")  # {symbol: token}

    # Relaxed-mode decision is a property of the session, not of any one symbol — it was
    # previously recomputed inside the loop, issuing one DB query per scanned symbol.
    today = now_ist.date()
    last_signal = (
        SignalHistory.objects.filter(category="intraday", generated_at__date=today)
        .order_by("-generated_at")
        .first()
    )
    # Relaxed mode is DISABLED by default. It lowered volume thresholds when nothing had
    # fired for two hours — inverting the correct logic, since a quiet tape is evidence
    # that conditions are poor. It manufactured trades to keep the UI populated rather
    # than to maximise expected value. Kept behind a flag (with metadata tagging intact)
    # so its EV can still be measured on demand rather than assumed.
    if not INTRADAY_ENABLE_RELAXED_MODE:
        is_fallback = False
    elif last_signal:
        is_fallback = (datetime.now(tz=timezone.utc) - last_signal.generated_at).total_seconds() > 7200
    else:
        is_fallback = now_ist.time() > time(11, 30)

    # 2. Main Scan — collect every candidate, then rank. The scan no longer breaks
    # early at the cap: previously it persisted signals in symbol-list order and
    # stopped at 5, so the output was the first 5 alphabetically rather than the best
    # 5, and every stronger setup later in the alphabet was silently discarded.
    candidates: list[dict[str, Any]] = []
    processed_count = 0
    for s in scan_symbols:
        try:
            token = token_map.get(s)
            if not token:
                continue

            # Local-only read as of 2026-07-27 — deliberately NOT candle_store.get_candles()
            # (which can still fall back to a live REST call). Bars come solely from
            # market_data/tick_aggregator.py, which builds today's FIVE_MINUTE CandleBar
            # rows from already-arrived WebSocket ticks (see updater.py's tick_aggregator
            # job) — zero Angel One REST calls, so this scan can't trip the getCandleData
            # circuit breaker. Trade-off: only today's bars exist this way (no REST-
            # fetched history), so len(df) < 31 below waits well into the session for
            # enough of today's own bars to accumulate, same as any cold start.
            df = candle_store.load_bars(s, "FIVE_MINUTE", now_ist - timedelta(days=3), exchange="NSE")
            if df is None or df.empty or len(df) < 31:
                continue

            processed_count += 1

            fired = _volume_profile_logic(s, df, nifty_bias, relaxed=is_fallback,
                                          size_multiplier=regime.size_multiplier,
                                          liquidity=universe_stats.get(s))
            if not fired:
                continue

            rs = _relative_strength(df, nifty_ret)
            tq = _trend_quality(df)
            for vp in fired:
                # Regime gate: drop triggers whose family is structurally wrong today.
                strategy = vp["reason"].split("] ", 1)[-1]
                if not strategy_allowed(strategy, regime):
                    continue
                vp["sector"] = sector_map.get(s, "UNKNOWN")
                vp["relative_strength"] = rs
                vp["trend_quality"] = tq
                candidates.append(vp)

        except Exception as e:
            logger.error(f"Error scanning {s}: {e}")
            continue

    # 3. Score cross-sectionally (0-100), threshold, then apply concentration caps.
    from stocks.services.shared.ranking import score_candidates, apply_threshold
    from stocks.services.shared.portfolio_risk import (
        build_correlation_clusters, apply_portfolio_constraints, effective_bets,
        compute_betas, beta_constrained, net_portfolio_beta, record_candidates,
    )

    sector_strength = sector_strength_from_candidates(candidates)
    candidates = score_candidates(candidates, regime, universe_stats, sector_strength)
    qualified = apply_threshold(candidates)

    # One symbol can now fire several triggers (§3.1c). Keep only its best-ranked one
    # so a single name cannot occupy multiple slots in the book.
    _best: dict[str, dict] = {}
    for c in qualified:
        prev = _best.get(c["symbol"])
        if prev is None or c["rank_score"] > prev["rank_score"]:
            _best[c["symbol"]] = c
    qualified = sorted(_best.values(), key=lambda x: x["rank_score"], reverse=True)

    open_rows = list(
        SignalHistory.objects.filter(
            category="intraday",
            status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
        ).values("symbol", "entry_price", "metadata")
    )
    clusters = build_correlation_clusters(svc, [c["symbol"] for c in qualified])
    qualified, rejected = apply_portfolio_constraints(
        qualified, open_rows, sector_map, clusters, MAX_SIGNALS_PER_SCAN,
    )
    record_candidates("intraday", qualified, rejected, regime=regime)
    candidates = qualified

    # Gross exposure ceiling across the whole book, counting positions already open.
    # A per-name cap alone cannot bound the portfolio: five separately-reasonable
    # positions can still add up to an unreasonable book.
    gross_budget = INTRADAY_ACCOUNT_EQUITY * (INTRADAY_MAX_GROSS_EXPOSURE_PCT / 100.0)
    gross_used = 0.0
    # live_rows defers `metadata`, so read it via an explicit values() query rather than
    # attribute access, which would issue one extra SELECT per open position.
    open_positions = SignalHistory.objects.filter(
        category="intraday",
        status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
    ).values("entry_price", "metadata")
    for row in open_positions:
        qty = float((row["metadata"] or {}).get("qty") or 0)
        gross_used += qty * float(row["entry_price"])

    # Beta-weighted net exposure (§5.4). Gross exposure bounds size; beta bounds
    # *directional* risk — five long signals can respect every gross and sector cap and
    # still amount to a leveraged bet on the index.
    betas = compute_betas(svc, [c["symbol"] for c in candidates])
    beta_book = [
        {"symbol": r["symbol"], "qty": (r["metadata"] or {}).get("qty", 0),
         "entry_price": r["entry_price"], "signal_type": "BUY"}
        for r in open_rows
    ]

    selected: list[dict[str, Any]] = []
    if market_open:
        for cand in candidates:
            if len(selected) >= MAX_SIGNALS_PER_SCAN:
                break
            notional = cand["qty"] * cand["entry"]
            if gross_used + notional > gross_budget:
                logger.info(
                    "[INTRADAY] Skipping %s — gross exposure cap (used Rs.%.0f + Rs.%.0f > Rs.%.0f)",
                    cand["symbol"], gross_used, notional, gross_budget,
                )
                continue
            if betas and not beta_constrained(cand, beta_book, betas, INTRADAY_ACCOUNT_EQUITY):
                logger.info("[INTRADAY] Skipping %s — net beta cap.", cand["symbol"])
                continue
            beta_book.append({
                "symbol": cand["symbol"], "qty": cand["qty"],
                "entry_price": cand["entry"], "signal_type": cand["signal"],
            })
            selected.append(cand)
            gross_used += notional

    newly_created: list = []
    for result in selected:
        try:
            signal_row, is_new = engine_persist_live_signal_history(result, "intraday", get_market_rules("intraday"))
            signals_persisted += 1
            if is_new:
                newly_created.append(signal_row)
        except Exception as e:
            logger.error("Error persisting intraday signal %s: %s", result.get("symbol"), e)

    if newly_created:
        try:
            from stocks.services.telegram_service import send_intraday_new_signals
            send_intraday_new_signals(newly_created)
        except Exception as tg_err:
            logger.error("[TELEGRAM] Intraday new-signal alert failed: %s", tg_err)
    elif is_first_attempt_today:
        # Only the day's FIRST generation attempt found nothing gets a Telegram
        # notice — with bounded retries (M10) now possible, later retry attempts
        # finding nothing too must not resend the same "no setup" message.
        try:
            from stocks.services.telegram_service import send_no_setup_today
            send_no_setup_today("INTRADAY EQUITY", "INTRADAY_NO_SETUP")
        except Exception as tg_err:
            logger.error("[TELEGRAM] Intraday no-setup notice failed: %s", tg_err)

    n_open = len(open_rows) + signals_persisted
    logger.info(
        "[INTRADAY][SCAN_DONE] processed=%d persisted=%d | regime trend=%s vol=%s "
        "momentum=%s meanrev=%s size=%.2fx | N_eff=%.2f",
        processed_count, signals_persisted, regime.trend_state, regime.vol_state,
        regime.allow_momentum, regime.allow_mean_reversion, regime.size_multiplier,
        effective_bets(n_open, 0.6),
    )

    # Persist scanned count for UI
    cache.set("intraday_last_processed_count", processed_count, timeout=10 * 60)

    payload = _live_intraday_payload()
    payload.update({
        "sentiment": nifty_trend,
        "directional_bias": nifty_bias,
        "market_status": "OPEN" if market_open else "CLOSED",
        "scanned": processed_count,
        "candidates_found": len(candidates),
        "universe_size": len(symbols),
        "regime": regime.as_dict(),
        "effective_bets": round(effective_bets(n_open, 0.6), 2),
        "net_beta": net_portfolio_beta(beta_book, betas, INTRADAY_ACCOUNT_EQUITY),
        "timeframe": "5-min refresh"
    })
    return payload
