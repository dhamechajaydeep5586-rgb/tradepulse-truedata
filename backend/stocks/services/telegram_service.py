"""
telegram_service.py — Telegram Bot Integration for Specialist Option-Selling Signals

Sends real-time alerts to a Telegram channel/group when:
  1. A new specialist signal is created (daily 10 NSE stocks)
  2. A signal transitions from PENDING → ACTIVE
  3. A signal hits Target or Stop Loss
  4. Daily summary of all picks at 9:45 AM IST

Uses raw HTTP requests to Telegram Bot API — no extra dependencies needed.
100% FREE (Telegram Bot API has zero cost).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

def _config() -> dict[str, str]:
    """Read Telegram config from Django settings with env fallback."""
    base = getattr(settings, "TELEGRAM_ALERTS", {})
    return {
        "ENABLED": str(base.get("ENABLED", os.getenv("TELEGRAM_ALERTS_ENABLED", "false"))).strip().lower(),
        "BOT_TOKEN": str(base.get("BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", ""))).strip(),
        "CHAT_ID": str(base.get("CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", ""))).strip(),
        "DAILY_SUMMARY_ENABLED": str(base.get("DAILY_SUMMARY_ENABLED", os.getenv("TELEGRAM_DAILY_SUMMARY", "true"))).strip().lower(),
    }


def is_enabled() -> bool:
    """Check if Telegram alerts are fully configured and enabled."""
    cfg = _config()
    return cfg["ENABLED"] in ("true", "1", "yes") and bool(cfg["BOT_TOKEN"]) and bool(cfg["CHAT_ID"])


# ──────────────────────────────────────────────
# CORE SEND FUNCTION
# ──────────────────────────────────────────────

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"


def get_short_term_chat_id() -> str:
    """Read Short Term swing channel ID from settings or environment."""
    base = getattr(settings, "TELEGRAM_ALERTS", {})
    return str(base.get("SHORT_TERM_CHAT_ID", os.getenv("TELEGRAM_SHORT_TERM_CHAT_ID", ""))).strip()


def get_intraday_chat_id() -> str:
    """Read the intraday/option-buying channel ID from settings or environment.

    Added 2026-07-28 at the account owner's explicit request — both intraday equity
    BUY/SELL and option-buying CE/PE alerts route here, separate from the default
    CHAT_ID (specialist/option-selling). Defaults to the chat ID the owner supplied
    directly (their own private chat with the bot); override via
    TELEGRAM_INTRADAY_CHAT_ID if it should point elsewhere later.
    """
    base = getattr(settings, "TELEGRAM_ALERTS", {})
    return str(base.get("INTRADAY_CHAT_ID", os.getenv("TELEGRAM_INTRADAY_CHAT_ID", "784052961"))).strip()


def send_telegram_message(text: str, parse_mode: str = "HTML", chat_id: str | None = None) -> bool:
    """
    Send a message via Telegram Bot API.

    Args:
        text: Message text (supports HTML formatting)
        parse_mode: 'HTML' or 'Markdown' (default: HTML)
        chat_id: Optional custom chat/channel ID (defaults to settings CHAT_ID)

    Returns:
        True if message was sent successfully, False otherwise
    """
    cfg = _config()
    if cfg["ENABLED"] not in ("true", "1", "yes"):
        return False

    target_chat_id = chat_id or cfg["CHAT_ID"]
    if not cfg["BOT_TOKEN"] or not target_chat_id:
        logger.warning("[TELEGRAM] Alerts enabled but BOT_TOKEN or CHAT_ID is missing.")
        return False

    url = TELEGRAM_API_URL.format(token=cfg["BOT_TOKEN"], method="sendMessage")
    payload = {
        "chat_id": target_chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()

        if result.get("ok"):
            logger.info("[TELEGRAM] ✅ Message sent successfully (chat_id=%s)", target_chat_id)
            return True
        else:
            logger.error("[TELEGRAM] ❌ API error: %s", result.get("description", "Unknown error"))
            return False
    except requests.exceptions.Timeout:
        logger.warning("[TELEGRAM] Request timed out after 10s")
        return False
    except Exception as exc:
        logger.error("[TELEGRAM] Failed to send message: %s", exc)
        return False


# ──────────────────────────────────────────────
# MESSAGE FORMATTERS
# ──────────────────────────────────────────────

def _get_ist_time_str() -> str:
    """Get current IST time as a formatted string."""
    from stocks.services.signal_utils import IST
    return datetime.now(tz=IST).strftime("%I:%M %p IST")


def format_new_signal_message(signal) -> str:
    """
    Format a new specialist signal for Telegram notification.
    Utilizes the upgraded systematic layout format.
    """
    meta = signal.metadata or {}
    legs = meta.get("legs", [])
    confidence = round(meta.get("confidence", 90))
    spot = float(signal.entry_price) if signal.entry_price else 0.0

    ce_leg = next((l for l in legs if l.get("action") == "SELL" and l.get("option_type") == "CE"), None)
    pe_leg = next((l for l in legs if l.get("action") == "SELL" and l.get("option_type") == "PE"), None)
    lot_size = ce_leg.get("lot_size", 0) if ce_leg else (pe_leg.get("lot_size", 0) if pe_leg else 0)

    formatted_legs = []
    for leg in legs:
        f_leg = dict(leg)
        entry_pr = float(leg.get("original_sell_price") or leg.get("sell_price", 0))
        f_leg["sell_price"] = entry_pr
        formatted_legs.append(f_leg)

    sig_data = {
        "symbol": signal.symbol,
        "spot_price": spot,
        "lot_size": lot_size,
        "confidence": confidence,
        "legs": formatted_legs
    }

    from stocks.services.vol_telegram_formatter import format_entry_signal
    return format_entry_signal(sig_data)




def format_daily_summary(signals) -> str:
    """
    Format the daily summary of all specialist signals.
    
    Args:
        signals: QuerySet or list of SignalHistory instances
    """
    from stocks.services.signal_utils import IST

    today_str = datetime.now(tz=IST).strftime("%d %b %Y")

    equity_lines = []
    total_premium = 0
    eq_count = 0

    for sig in signals:
        meta = sig.metadata or {}
        legs = meta.get("legs", [])
        confidence = round(meta.get("confidence", 0))

        ce_strike = "—"
        pe_strike = "—"
        sig_premium = 0

        for leg in legs:
            if leg.get("action") == "SELL":
                premium = float(leg.get("sell_price", 0))
                sig_premium += premium
                if leg.get("option_type") == "CE":
                    ce_strike = leg.get("strike", "—")
                elif leg.get("option_type") == "PE":
                    pe_strike = leg.get("strike", "—")

        total_premium += sig_premium
        eq_count += 1
        equity_lines.append(
            f"  {eq_count}. <b>{sig.symbol}</b> — {confidence}% conf — CE {ce_strike} / PE {pe_strike}"
        )

    msg = (
        f"📋 <b>DAILY OPTION SELLING PICKS</b>\n"
        f"📅 {today_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    if equity_lines:
        msg += f"🏆 <b>Top {len(equity_lines)} Equity Strangles:</b>\n"
        msg += "\n".join(equity_lines) + "\n\n"
    else:
        msg += "No specialist signals generated today.\n\n"

    msg += (
        f"💰 <b>Total Premium:</b> ₹{total_premium:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <i>Powered by TradePulse AI</i>"
    )

    return msg


# ──────────────────────────────────────────────
# NOTIFICATION DISPATCHERS (called from delta_hedge_service)
# ──────────────────────────────────────────────

def maybe_send_telegram_new_signal(signal) -> bool:
    """Send Telegram alert for a newly created specialist signal (idempotent)."""
    if not is_enabled():
        logger.warning("[TELEGRAM] Skipping new signal alert — Telegram not enabled or misconfigured (check TELEGRAM_ALERTS_ENABLED, BOT_TOKEN, CHAT_ID)")
        return False
    if signal.telegram_signal_sent:
        logger.info("[TELEGRAM] Skipping %s — telegram_signal_sent already True (idempotent)", signal.symbol)
        return False

    msg = format_new_signal_message(signal)
    success = send_telegram_message(msg, parse_mode="Markdown")

    if success:
        signal.telegram_signal_sent = True
        signal.save(update_fields=["telegram_signal_sent"])
        logger.info("[TELEGRAM] New signal alert sent for %s", signal.symbol)
    else:
        logger.error("[TELEGRAM] Failed to send new signal alert for %s", signal.symbol)

    return success


def send_consolidated_new_signals(signals) -> bool:
    """Send a single consolidated Telegram alert for a list of newly created specialist signals."""
    if not is_enabled():
        return False
        
    filtered_signals = [sig for sig in signals if not sig.telegram_signal_sent]
    if not filtered_signals:
        return False
        
    signals_data = []
    for sig in filtered_signals:
        meta = sig.metadata or {}
        legs = meta.get("legs", [])
        confidence = round(meta.get("confidence", 90))
        spot = float(sig.entry_price) if sig.entry_price else 0.0

        ce_leg = next((l for l in legs if l.get("action") == "SELL" and l.get("option_type") == "CE"), None)
        pe_leg = next((l for l in legs if l.get("action") == "SELL" and l.get("option_type") == "PE"), None)
        lot_size = ce_leg.get("lot_size", 0) if ce_leg else (pe_leg.get("lot_size", 0) if pe_leg else 0)

        formatted_legs = []
        for leg in legs:
            f_leg = dict(leg)
            entry_pr = float(leg.get("original_sell_price") or leg.get("sell_price", 0))
            f_leg["sell_price"] = entry_pr
            formatted_legs.append(f_leg)

        signals_data.append({
            "symbol": sig.symbol,
            "spot_price": spot,
            "lot_size": lot_size,
            "confidence": confidence,
            "legs": formatted_legs
        })
        
    from stocks.services.vol_telegram_formatter import format_consolidated_entry_signals
    msg = format_consolidated_entry_signals(signals_data)
    success = send_telegram_message(msg, parse_mode="Markdown")
    
    if success:
        for sig in filtered_signals:
            sig.telegram_signal_sent = True
            sig.save(update_fields=["telegram_signal_sent"])
        logger.info("[TELEGRAM] Consolidated new signal alert sent with %d signals", len(filtered_signals))
        
    return success


def send_intraday_new_signals(signals) -> bool:
    """Queue one consolidated Telegram alert for newly generated intraday equity signals.

    Unlike specialist's send_consolidated_new_signals (synchronous, direct send), this uses
    queue_telegram_message — non-blocking, so a slow/failed Telegram call can never stall the
    scan loop that's also racing intraday's own signal-generation cutoff.
    """
    if not is_enabled() or not signals:
        return False

    header = "📈 <b>NEW INTRADAY SIGNAL(S)</b>"
    entries = []
    for sig in signals:
        arrow = "🟢 BUY" if sig.signal_type == "BUY" else "🔴 SELL"
        entries.append(
            f"<b>{sig.symbol}</b> — {arrow}\n"
            f"Entry: ₹{float(sig.entry_price):.2f}  |  SL: ₹{float(sig.stop_loss):.2f}  |  Target: ₹{float(sig.target):.2f}"
            + (f"  |  RR: 1:{sig.rr}" if sig.rr else "")
        )
    # Fixed 2026-07-28: the old `"\n\n".join(["header", "", *entries])` doubled up the
    # blank line — "" as its own list element plus "\n\n" as the join separator on both
    # sides of it produced 3-4 newlines between the header and the first signal instead
    # of one blank line. Join the header and body separately with a single "\n\n".
    msg = header + "\n\n" + "\n\n".join(entries)
    return queue_telegram_message(msg, chat_id=get_intraday_chat_id(), event_type="INTRADAY_NEW_SIGNAL")


def send_option_buying_new_signals(signals) -> bool:
    """Queue one consolidated Telegram alert for newly generated option-buying (CE/PE) signals."""
    if not is_enabled() or not signals:
        return False

    header = "🎯 <b>NEW OPTION BUYING SIGNAL(S)</b>"
    entries = []
    for sig in signals:
        entries.append(
            f"<b>{sig.symbol} {sig.strike_price} {sig.option_type}</b> (exp {sig.option_expiry})\n"
            f"Entry: ₹{float(sig.entry_price):.2f}  |  SL: ₹{float(sig.stop_loss):.2f}  |  Target: ₹{float(sig.target):.2f}"
        )
    # Same double-blank-line fix as send_intraday_new_signals() above — join header and
    # body with a single "\n\n" instead of putting "" in the joined list.
    msg = header + "\n\n" + "\n\n".join(entries)
    return queue_telegram_message(msg, chat_id=get_intraday_chat_id(), event_type="OPTION_BUYING_NEW_SIGNAL")


def send_no_setup_today(engine_label: str, event_type: str) -> bool:
    """Queue a one-shot 'no qualifying setup today' notice. Added 2026-07-29 alongside
    the once-per-day generation cap in intraday_service.py/option_buying_service.py —
    those engines now attempt generation exactly once per day, so a quiet day should
    say so explicitly rather than leave the absence of a signal to be mistaken for
    a bug (or actually be one)."""
    if not is_enabled():
        return False
    msg = (
        f"📭 <b>{engine_label}</b> — No qualifying setup today.\n"
        f"Existing open positions (if any) continue to be monitored as usual."
    )
    return queue_telegram_message(msg, chat_id=get_intraday_chat_id(), event_type=event_type)


def maybe_send_telegram_activation(signal) -> bool:
    """Send Telegram alert when signal moves PENDING → ACTIVE (idempotent)."""
    # Disabled per user request: "✅ SIGNAL ACTIVATED not required this message to send to telegram please"
    logger.info("[TELEGRAM] Skipping activation alert for %s (disabled per user request)", signal.symbol)
    return False



def maybe_send_telegram_exit(signal, exit_status: str) -> bool:
    """Send Telegram alert when signal hits TGT or SL (idempotent)."""
    # Disabled per user request: Only position update messages should go to Telegram.
    # No SL hit or target hit exit alerts.
    logger.info("[TELEGRAM] Skipping exit alert for %s (%s) — disabled per user request (intraday mode)", signal.symbol, exit_status)
    return False


def send_daily_picks_summary() -> bool:
    """
    Send daily summary of all specialist signals to Telegram.
    Called by the APScheduler job at 10:00 AM IST.
    TIME-GUARDED: Only sends before 11:00 AM IST to prevent duplicate
    summary messages in the afternoon. After 11 AM, only the periodic
    P&L update ("Strangles Session Update") should be sent.
    """
    # Disabled per user request (Only the "Daily Specialist Strangles" consolidated signal message is needed)
    logger.info("[TELEGRAM] Daily summary picks message disabled per user request.")
    return False


def send_periodic_pnl_updates() -> bool:
    """
    Send periodic running P&L update for all active specialist signals to Telegram.
    Consolidated into a single concise message containing all active signals.
    """
    if not is_enabled():
        logger.info("[TELEGRAM] Periodic P&L update skipped — alerts not enabled")
        return False

    from stocks.services.signal_utils import IST
    now_ist = datetime.now(tz=IST)
    # Suppress the update before 10:10 AM IST (prevents redundant 10:00 AM session update message)
    if now_ist.hour == 10 and now_ist.minute < 10:
        logger.info("[TELEGRAM] Periodic P&L update skipped before 10:10 AM IST (starts at 10:15 AM)")
        return False

    from stocks.models import SignalHistory
    from stocks.services.signal_utils import IST

    today_start = timezone.now().astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)

    # Fetch all specialist signals generated today (ACTIVE, PENDING, HIT_TARGET, HIT_SL, EXPIRED)
    active_signals = SignalHistory.objects.filter(
        category="specialist",
        status__in=[
            SignalHistory.Status.ACTIVE,
            SignalHistory.Status.PENDING,
            SignalHistory.Status.HIT_TARGET,
            SignalHistory.Status.HIT_SL,
            SignalHistory.Status.EXPIRED,
        ],
        generated_at__gte=today_start
    ).order_by("generated_at")

    filtered_signals = list(active_signals)

    if not filtered_signals:
        logger.info("[TELEGRAM] Periodic P&L update skipped — no specialist signals today")
        return False

    updates_data = []
    for sig in filtered_signals:
        meta = sig.metadata or {}
        legs = meta.get("legs", [])

        # Separate CE and PE legs. Prefer non-EXPIRED (i.e. still-live) legs — after a
        # delta rebalance, the old and new leg both have action=='SELL' and the old one
        # stays in the list, so without this preference `next()` could pick the stale
        # rolled-off leg over the live replacement.
        #
        # Fall back to the most recent leg regardless of status once no non-EXPIRED leg
        # exists — at EOD square-off, delta_hedge_service marks BOTH legs EXPIRED when
        # the whole position closes (not just a rolled-away one), so the strict filter
        # alone found nothing and rendered "CE 0.00 -> 0.00 / Lot: 0" here even though
        # final_pnl (computed separately, from the same pre-close mark-to-market pass)
        # was correct — a real, closed position showing impossible zeroed-out legs next
        # to a genuine non-zero P&L. reversed() picks the LAST matching leg so a
        # rolled-then-closed position shows its final contract, not the original one.
        ce_leg = (
            next((l for l in legs if l.get("action") == "SELL" and l.get("option_type") == "CE" and l.get("status") != "EXPIRED"), None)
            or next((l for l in reversed(legs) if l.get("action") == "SELL" and l.get("option_type") == "CE"), None)
        )
        pe_leg = (
            next((l for l in legs if l.get("action") == "SELL" and l.get("option_type") == "PE" and l.get("status") != "EXPIRED"), None)
            or next((l for l in reversed(legs) if l.get("action") == "SELL" and l.get("option_type") == "PE"), None)
        )

        ce_strike = float(ce_leg.get("strike", 0.0)) if ce_leg else 0.0
        pe_strike = float(pe_leg.get("strike", 0.0)) if pe_leg else 0.0

        ce_original = float(ce_leg.get("original_sell_price") or ce_leg.get("sell_price", 0)) if ce_leg else 0.0
        pe_original = float(pe_leg.get("original_sell_price") or pe_leg.get("sell_price", 0)) if pe_leg else 0.0
        ce_cmp = float(ce_leg.get("cmp", 0.0)) if ce_leg else 0.0
        pe_cmp = float(pe_leg.get("cmp", 0.0)) if pe_leg else 0.0

        ce_delta = float(ce_leg.get("delta", 0.0)) if ce_leg else 0.0
        pe_delta = float(pe_leg.get("delta", 0.0)) if pe_leg else 0.0

        lot_size = ce_leg.get("lot_size", 0) if ce_leg else (pe_leg.get("lot_size", 0) if pe_leg else 0)

        # ── P&L CALCULATION FOR 2 LOTS ────────────────────────────────────
        # Always calculate P&L from first principles: (entry - current) × lot_size × 2 lots
        # This avoids the zero P&L bug where process_legs() suppresses leg.pnl to 0
        # for PENDING signals and persists that to the DB.
        sell_legs = [l for l in legs if l.get("action") == "SELL"]
        is_closed = sig.status in [
            SignalHistory.Status.HIT_TARGET,
            SignalHistory.Status.HIT_SL,
            SignalHistory.Status.EXPIRED,
            SignalHistory.Status.CANCELLED,
        ]
        if is_closed and "final_pnl" in meta and float(meta["final_pnl"]) != 0.0:
            # Closed/locked position with non-zero recorded P&L — use the persisted value
            running_pnl = float(meta["final_pnl"]) * 2.0
            pnl_pct = float(meta.get("final_pnl_pct", 0.0))
        else:
            # Live or PENDING position — compute P&L from original entry vs current CMP
            running_pnl = 0.0
            total_entry_value = 0.0
            for l in sell_legs:
                entry_price = float(l.get("original_sell_price") or l.get("sell_price", 0))
                current_price = float(l.get("cmp", 0))
                ls = float(l.get("lot_size", 1))
                if entry_price > 0 and current_price > 0:
                    # Short selling P&L: profit when premium decays (entry - current)
                    leg_pnl = (entry_price - current_price) * ls
                    running_pnl += leg_pnl
                    total_entry_value += entry_price * ls
            # Scale to 2 lots
            running_pnl *= 2.0
            total_entry_value *= 2.0
            pnl_pct = (running_pnl / total_entry_value * 100.0) if total_entry_value > 0 else 0.0
        # ─────────────────────────────────────────────────────────────────

        # Live spot price
        spot = float(sig.entry_price) if sig.entry_price else 0.0
        try:
            from stocks.services.market_data_orchestrator import get_orchestrator
            orch = get_orchestrator()
            price_res = orch.get_price(sig.symbol, exchange="NSE") or {}
            live_spot = price_res.get("ltp", 0)
            if live_spot > 0:
                spot = live_spot
        except Exception:
            pass  # Use stored spot on failure

        updates_data.append({
            "symbol": sig.symbol,
            "spot_price": spot,
            "lot_size": lot_size,
            "ce_strike": ce_strike,
            "ce_old_premium": ce_original,
            "ce_new_premium": ce_cmp,
            "ce_delta": ce_delta,
            "pe_strike": pe_strike,
            "pe_old_premium": pe_original,
            "pe_new_premium": pe_cmp,
            "pe_delta": pe_delta,
            "pnl": running_pnl,
            "pnl_pct": pnl_pct,
        })

    # Skip sending if no active positions exist
    if not updates_data:
        logger.info(
            "[TELEGRAM] Periodic P&L update skipped — no active specialist signals today",
            len(filtered_signals)
        )
        return False

    from stocks.services.vol_telegram_formatter import format_consolidated_live_updates
    msg = format_consolidated_live_updates(updates_data)
    success = send_telegram_message(msg, parse_mode="Markdown")

    if success:
        logger.info(
            "[TELEGRAM] Periodic P&L update sent (%d positions)",
            len(updates_data)
        )
    else:
        logger.error("[TELEGRAM] Failed to send periodic P&L update")

    return success


def send_intraday_periodic_pnl_updates() -> bool:
    """Send a consolidated running P&L update for active/pending intraday equity
    signals, mirroring send_periodic_pnl_updates()'s pattern for specialist but with
    equity BUY/SELL math (entry/current × qty) instead of short-strangle premium math.

    Added 2026-07-28 at the account owner's explicit request — same 15-min cadence as
    the scan cycle that calls this. Routes to get_intraday_chat_id(), not the default
    CHAT_ID specialist uses.
    """
    if not is_enabled():
        return False

    from stocks.models import SignalHistory
    from stocks.services.signal_utils import IST

    today_start = timezone.now().astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    # Includes closed statuses (2026-07-30 fix) so a position that hits target/SL is
    # reported once more with its outcome instead of silently vanishing from the next
    # periodic update — CANCELLED excluded, it never triggered and carries no P&L.
    live_signals = list(SignalHistory.objects.filter(
        category="intraday",
        status__in=[
            SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING,
            SignalHistory.Status.HIT_TARGET, SignalHistory.Status.HIT_SL,
            SignalHistory.Status.EXPIRED,
        ],
        generated_at__gte=today_start,
    ))
    if not live_signals:
        logger.info("[TELEGRAM] Intraday periodic P&L skipped — no active/pending intraday signals today")
        return False

    open_symbols = [
        s.symbol for s in live_signals
        if s.status in (SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING)
    ]
    from stocks.services.market_data_orchestrator import get_orchestrator
    orch = get_orchestrator()
    prices = orch.get_prices_bulk(open_symbols, exchange="NSE") or {} if open_symbols else {}

    STATUS_LABEL = {
        SignalHistory.Status.HIT_TARGET: "🎯 TARGET HIT",
        SignalHistory.Status.HIT_SL: "🛑 SL HIT",
        SignalHistory.Status.EXPIRED: "⏰ EXPIRED",
    }

    # Redesigned 2026-07-28 to match the specialist strangle update's numbered,
    # multi-line style (see vol_telegram_formatter.format_consolidated_live_updates)
    # instead of one cramped line per position.
    header = "📊 <b>INTRADAY — Running P&amp;L</b>"
    entries = []
    total_pnl = 0.0
    for i, sig in enumerate(live_signals, 1):
        entry = float(sig.entry_price)
        is_closed = sig.status in (
            SignalHistory.Status.HIT_TARGET, SignalHistory.Status.HIT_SL, SignalHistory.Status.EXPIRED,
        )
        # Closed positions freeze at their recorded exit price, not the current
        # live price — the trade is done, showing a moving CMP after close would
        # misrepresent P&L that's already locked in.
        if is_closed and sig.exit_price is not None:
            current = float(sig.exit_price)
        else:
            current = float(prices.get(sig.symbol) or entry)
        qty = float((sig.metadata or {}).get("qty") or 0)
        is_buy = sig.signal_type == "BUY"
        pnl_per_share = (current - entry) if is_buy else (entry - current)
        pnl = pnl_per_share * qty
        total_pnl += pnl
        pnl_pct = (pnl_per_share / entry * 100.0) if entry > 0 else 0.0
        emoji = "🟢" if pnl >= 0 else "🔴"
        arrow = "BUY" if is_buy else "SELL"
        status_label = STATUS_LABEL.get(sig.status, sig.status)
        entries.append(
            f"{i}. <b>{sig.symbol}</b> ({arrow}, {status_label})\n"
            f"   Entry: ₹{entry:.2f} → {'Exit' if is_closed else 'CMP'}: ₹{current:.2f}\n"
            f"   P&amp;L: {emoji} ₹{pnl:+.2f} ({pnl_pct:+.2f}%)"
        )

    total_emoji = "🟢" if total_pnl >= 0 else "🔴"
    footer = f"\n\n{total_emoji} <b>Total Session P&amp;L: ₹{total_pnl:,.2f}</b>"
    msg = header + "\n\n" + "\n\n".join(entries) + footer
    success = send_telegram_message(msg, chat_id=get_intraday_chat_id())
    if success:
        logger.info("[TELEGRAM] Intraday periodic P&L update sent (%d positions)", len(live_signals))
    else:
        logger.error("[TELEGRAM] Failed to send intraday periodic P&L update")
    return success


def send_option_buying_periodic_pnl_updates() -> bool:
    """Send a consolidated running P&L update for active option-buying (CE/PE)
    signals. Premium-space math (long option — profit when premium rises), no
    qty/lot_size stored on these signals so this reports % premium move, matching
    how option-buying's own target/SL are expressed (premium levels, not ₹ × qty).

    Added 2026-07-28 at the account owner's explicit request — routes to the same
    get_intraday_chat_id() channel as intraday's periodic update.

    Fixed 2026-07-28: this used to call svc.get_option_quote() itself, which was
    silently returning None for every position (always "+0.00%", CMP == entry).
    Root cause: this function runs LAST in run_periodic_scanners' cycle
    (live_signal_service.py), *after* the specialist scan, option-buying scan, and
    intraday's heavy 500-symbol sweep — any 403 anywhere in that run trips
    truedata_service's module-level quote circuit breaker
    (_REST_CIRCUIT_BREAKER_UNTIL["quote"], 5-min cooldown), and freshly-selected
    option strikes are never pre-subscribed on the WebSocket stream, so every one
    of these quote calls falls through to the REST path get_live_price_by_token()
    silently short-circuits (logged at debug level, easy to miss) once the breaker
    is tripped. update_option_buying_outcomes() (option_buying_service.py) hits the
    exact same svc.get_option_quote() call with the exact same args but runs FIRST
    in the cycle, before that REST volume has a chance to trip the breaker, and
    persists the result to sig.premium_cmp on every ACTIVE signal — so by the time
    THIS function runs moments later in the same cycle, a fresh premium is already
    sitting in the DB. Read it from there (same pattern option_buying_service.py's
    own _live_option_buying_payload() already uses, per its docstring) instead of
    firing a second, redundant, breaker-exposed REST call.
    """
    if not is_enabled():
        return False

    from stocks.models import SignalHistory
    from stocks.services.signal_utils import IST

    today_start = timezone.now().astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    # Includes HIT_TARGET/HIT_SL (2026-07-30 fix) so a closed option-buying position
    # is reported once more with its outcome instead of silently vanishing — option
    # buying has no PENDING/EXPIRED lifecycle state (see CLAUDE.md), only these three.
    live_signals = list(SignalHistory.objects.filter(
        category="option_buying",
        status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.HIT_TARGET, SignalHistory.Status.HIT_SL],
        generated_at__gte=today_start,
    ))
    if not live_signals:
        logger.info("[TELEGRAM] Option-buying periodic P&L skipped — no active option-buying signals")
        return False

    # Redesigned 2026-07-28 to match the specialist strangle update's numbered,
    # multi-line style (see vol_telegram_formatter.format_consolidated_live_updates).
    # Redesigned again 2026-07-30 (account owner's request) to also show ₹ P&L for
    # 2 lots, same convention as the strangle update's "P&L (2 Lots)" — option
    # buying is a single long leg (not a two-leg credit spread) so this is just
    # (current - entry) * lot_size * 2, no combined-premium math needed.
    from stocks.services.delta_hedge_service import get_lot_size

    STATUS_LABEL = {
        SignalHistory.Status.HIT_TARGET: "🎯 TARGET HIT",
        SignalHistory.Status.HIT_SL: "🛑 SL HIT",
    }

    header = "🎯 <b>OPTION BUYING — Running P&amp;L</b>"
    entries = []
    total_pnl = 0.0
    for i, sig in enumerate(live_signals, 1):
        entry = float(sig.entry_price)
        is_closed = sig.status in (SignalHistory.Status.HIT_TARGET, SignalHistory.Status.HIT_SL)
        # Closed positions freeze at their recorded exit price, not the live premium —
        # the trade is done, a moving CMP after close would misrepresent locked-in P&L.
        if is_closed and sig.exit_price is not None:
            current = float(sig.exit_price)
        else:
            current = float(sig.premium_cmp) if sig.premium_cmp is not None else entry
        pnl_pct = ((current - entry) / entry * 100.0) if entry > 0 else 0.0
        lot_size = get_lot_size(sig.symbol, "NFO")
        pnl_2_lots = (current - entry) * lot_size * 2
        total_pnl += pnl_2_lots
        emoji = "🟢" if current >= entry else "🔴"
        status_label = STATUS_LABEL.get(sig.status)
        label_suffix = f" — {status_label}" if status_label else ""
        entries.append(
            f"{i}. <b>{sig.symbol} {sig.strike_price} {sig.option_type}</b> (exp {sig.option_expiry}){label_suffix}\n"
            f"   Entry: ₹{entry:.2f} → {'Exit' if is_closed else 'CMP'}: ₹{current:.2f}\n"
            f"   P&amp;L (2 Lots): {emoji} ₹{pnl_2_lots:,.2f} ({pnl_pct:+.2f}%)"
        )

    total_emoji = "🟢" if total_pnl >= 0 else "🔴"
    footer = f"\n\n{total_emoji} <b>Total Session P&amp;L (2 Lots): ₹{total_pnl:,.2f}</b>"
    msg = header + "\n\n" + "\n\n".join(entries) + footer
    success = send_telegram_message(msg, chat_id=get_intraday_chat_id())
    if success:
        logger.info("[TELEGRAM] Option-buying periodic P&L update sent (%d positions)", len(live_signals))
    else:
        logger.error("[TELEGRAM] Failed to send option-buying periodic P&L update")
    return success


# ──────────────────────────────────────────────
# ASYNC QUEUE ALERTS & DISPATCHER
# ──────────────────────────────────────────────

def queue_telegram_message(text: str, chat_id: str | None = None, short_term_signal = None, event_type: str | None = None, log_type: str = 'INFO') -> bool:
    """
    Queue a message in the database (TelegramLog) to be sent asynchronously by the background task.
    This call is non-blocking and safe from network timeouts.
    """
    from stocks.models import TelegramLog
    try:
        cfg = _config()
        target_chat = chat_id or cfg["CHAT_ID"]
        
        # Check if identical event has already been queued to prevent duplicate runs
        if short_term_signal and event_type:
            already_exists = TelegramLog.objects.filter(
                short_term_signal=short_term_signal,
                event_type=event_type
            ).exists()
            if already_exists:
                logger.info("[TELEGRAM_QUEUE] Event %s already exists for %s, skipping queue request", event_type, short_term_signal.symbol)
                return True

        TelegramLog.objects.create(
            short_term_signal=short_term_signal,
            type=log_type,
            event_type=event_type or 'GENERIC',
            message=text,
            status='PENDING',
            delivery_status='PENDING',
            retry_count=0,
            # Added 2026-07-28: target_chat was resolved above but never persisted —
            # process_telegram_queue() had no way to know which chat a message was
            # meant for and hardcoded the short-term chat for everything. Store the
            # explicit override only (blank = "use the short-term chat" default,
            # preserving old behavior for every other existing caller).
            chat_id=chat_id or '',
        )
        logger.info("[TELEGRAM_QUEUE] Message queued successfully for event: %s", event_type or 'GENERIC')
        return True
    except Exception as e:
        logger.error("[TELEGRAM_QUEUE] Failed to queue Telegram message: %s", e)
        return False


def process_telegram_queue():
    """
    Asynchronously processes queued PENDING Telegram alerts.
    Managed by the 1-minute APScheduler cron worker.
    """
    from stocks.models import TelegramLog
    from django.db import transaction

    pending_logs = list(TelegramLog.objects.filter(
        status='PENDING',
        retry_count__lt=3
    ).order_by('sent_at')[:20])  # Batch limit of 20 to prevent locking or timing out

    if not pending_logs:
        return

    logger.info("[TELEGRAM_QUEUE] Processing %d pending queued messages...", len(pending_logs))

    for log in pending_logs:
        try:
            with transaction.atomic():
                db_log = TelegramLog.objects.select_for_update().get(id=log.id)
                if db_log.status != 'PENDING':
                    continue

                # Fixed 2026-07-28: this used to hardcode get_short_term_chat_id() for
                # every queued message — correct back when this queue was only ever
                # populated by the swing/trade-engine pipeline, wrong since intraday
                # and option-buying's new-signal alerts started using this same queue
                # (they need get_intraday_chat_id(), not the short-term chat). Blank
                # chat_id (rows queued before this field existed, or any future caller
                # that doesn't specify one) still falls back to the short-term chat.
                success = send_telegram_message(
                    text=db_log.message,
                    chat_id=db_log.chat_id or get_short_term_chat_id()
                )

                if success:
                    db_log.status = 'SENT'
                    db_log.delivery_status = 'SENT'
                else:
                    db_log.retry_count += 1
                    if db_log.retry_count >= 3:
                        db_log.status = 'FAILED'
                        db_log.delivery_status = 'FAILED'
                
                db_log.save(update_fields=['status', 'delivery_status', 'retry_count'])
        except Exception as e:
            logger.error("[TELEGRAM_QUEUE] Exception processing log ID %d: %s", log.id, e)



