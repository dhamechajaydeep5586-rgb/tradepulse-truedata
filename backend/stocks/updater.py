import logging
import os
import sys
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from django.conf import settings
from django.core.management import call_command
from django.db import close_old_connections
import pytz

logger = logging.getLogger(__name__)

# Retention windows for run_signal_tables_cleanup() below — Django-settings-configurable,
# same getattr(settings, ..., default) pattern as INTRADAY_ACCOUNT_EQUITY in
# intraday_service.py (that constant is NOT defined in config/settings.py either; it's a
# module-level constant in the service file itself, so this follows the real precedent, not
# an assumed one). Added for doc/AUDIT_REMEDIATION_PLAN.md Phase 3 #3.2.2.
SIGNAL_HISTORY_RETENTION_DAYS = int(getattr(settings, "SIGNAL_HISTORY_RETENTION_DAYS", 180))
SIGNAL_CANDIDATE_RETENTION_DAYS = int(getattr(settings, "SIGNAL_CANDIDATE_RETENTION_DAYS", 180))
TELEGRAM_LOG_RETENTION_DAYS = int(getattr(settings, "TELEGRAM_LOG_RETENTION_DAYS", 90))
TRADE_HISTORY_RETENTION_DAYS = int(getattr(settings, "TRADE_HISTORY_RETENTION_DAYS", 365))

def run_daily_pipeline():
    """Wrapper function to safely call the Django management command."""
    close_old_connections()
    logger.info("Starting scheduled daily market update pipeline...")
    try:
        call_command("run_daily_market_update", skip_ai=False)
        logger.info("Successfully completed scheduled daily market update pipeline.")
    except Exception as e:
        logger.error("Error during scheduled daily update: %s", e)


def run_10am_strangle_scan():
    """
    Dedicated 10:00 AM IST daily signal generation job.
    Calls the `generate_strangle_signals` management command which:
      1. Clears the scanner throttle cache (guarantees scan always runs)
      2. Runs a synchronous strangle scan across the Nifty50 universe
      3. Waits 30s, then dispatches the Telegram daily summary
    This is the ONLY job that should generate daily signals.
    """
    close_old_connections()
    logger.info("[CRON-10AM] Starting guaranteed 10 AM strangle signal generation...")
    try:
        call_command("generate_strangle_signals", telegram_delay=30)
        logger.info("[CRON-10AM] Daily strangle scan completed successfully.")
    except Exception as e:
        logger.error("[CRON-10AM] Daily strangle scan FAILED: %s", e)

def run_short_term_scan():
    """
    Guaranteed daily short-term swing signal generation.
    Routes through the new Trade Engine for AI scoring + trade creation.
    """
    close_old_connections()
    logger.info("[SCHEDULER] Starting Trade Engine daily scanner...")
    try:
        from stocks.services.trade_engine import run_daily_scanner
        run_daily_scanner()
        logger.info("[SCHEDULER] Trade Engine daily scanner completed successfully.")
    except Exception as e:
        logger.error("[SCHEDULER] Trade Engine daily scanner FAILED: %s", e)


def run_long_term_scan():
    """12:00 PM: Long-term momentum screen (re-enabled 2026-07-28, LONG_TERM_GENERATION_ENABLED)."""
    close_old_connections()
    logger.info("[SCHEDULER] Starting long-term momentum scan...")
    try:
        from stocks.services.pro_system_service import run_long_term_scan as _run_lt_scan
        result = _run_lt_scan()
        logger.info("[SCHEDULER] Long-term scan completed: %s", result)
    except Exception as e:
        logger.error("[SCHEDULER] Long-term scan FAILED: %s", e)


def run_long_term_outcomes_audit():
    """4:00 PM: Audit open long-term positions for a 200-EMA trend break (audit
    remediation 2026-07-28). Deliberately NOT gated by LONG_TERM_GENERATION_ENABLED —
    this only closes already-open positions on a real trend break, it never opens a
    new one, so it stays safe to run even while new long-term generation is paused."""
    close_old_connections()
    try:
        from stocks.services.pro_system_service import update_long_term_outcomes
        result = update_long_term_outcomes()
        logger.info("[SCHEDULER] Long-term outcomes audit completed: %s", result)
    except Exception as e:
        logger.error("[SCHEDULER] Long-term outcomes audit FAILED: %s", e)


def run_swing_v2_shadow():
    """16:05 IST — Swing V2 SHADOW run. Dry run: generates and logs, persists nothing.

    Deliberately shadow-only. The V2 architecture treats the scheduler cutover as the
    single one-way door in the migration and requires ~20 sessions of shadow output
    first — an engine whose table has been empty needs to prove it produces signals
    before it is trusted to produce good ones.

    Runs at 16:05, after the 15:30 close, because V2 evaluates CLOSED daily bars. The
    legacy 10:00 scan reads an in-progress bar; measured on 2026-07-26 that alone cut
    the volume-gate pass rate from 19/49 to 11/49.

    To promote: swap dry_run=False, retire trade_engine_scanner_10am, and add the
    activation + EOD jobs.
    """
    close_old_connections()
    logger.info("[SCHEDULER] Swing V2 shadow scan starting...")
    try:
        from stocks.services.swing_service import run_swing_scan
        funnel = run_swing_scan(dry_run=True)
        logger.info("[SCHEDULER][SWING_V2_SHADOW] %s", funnel)

        # Durable shadow-session counter (added 2026-07-29) — the scan itself persists
        # nothing by design, so this is the only durable record of "a real session ran
        # today," used to answer "how many of the ~20 required sessions do we actually
        # have" without depending on Render's log history. get_or_create so a misfire
        # retry landing on the same day doesn't double-count.
        try:
            from datetime import date as _date
            from stocks.models import SwingV2ShadowRun
            SwingV2ShadowRun.objects.get_or_create(
                run_date=_date.today(), defaults={"funnel": funnel}
            )
        except Exception as counter_err:
            logger.error("[SCHEDULER] Swing V2 shadow session counter write failed: %s", counter_err)
    except Exception as e:
        logger.error("[SCHEDULER] Swing V2 shadow scan FAILED: %s", e)


def run_calendar_sync():
    """8:30 AM — persist NSE's earnings calendar and corporate-actions feed.

    `EarningsEvent` and `CorporateAction` existed as models with no ingestion since the
    V2 build added them; this is what actually populates them. Runs before the 9:05
    pre-market job and well before any scan, so the day's blackout/adjustment data is
    fresh before it is needed. Low cadence is deliberate — neither feed changes
    intraday, and NSE's endpoints are best not hit more than necessary.
    """
    close_old_connections()
    logger.info("[SCHEDULER] Calendar sync (earnings + corporate actions) starting...")
    try:
        from stocks.services.shared.calendar_service import sync_all
        result = sync_all()
        logger.info("[SCHEDULER][CALENDAR_SYNC] %s", result)
    except Exception as e:
        logger.error("[SCHEDULER] Calendar sync FAILED: %s", e)


def run_premarket_update():
    """9:05 AM: Update Nifty/Sector trends via Trade Engine."""
    close_old_connections()
    logger.info("[SCHEDULER] Starting pre-market update...")
    try:
        from stocks.services.trade_engine import run_premarket_update as _premarket
        _premarket()
        logger.info("[SCHEDULER] Pre-market update completed.")
    except Exception as e:
        logger.error("[SCHEDULER] Pre-market update FAILED: %s", e)


def run_intraday_check():
    """Every 10 min: Check entry activation for pending trades."""
    close_old_connections()
    try:
        from stocks.services.trade_engine import check_pending_activations
        check_pending_activations()
    except Exception as e:
        logger.error("[SCHEDULER] Intraday activation check FAILED: %s", e)


def run_eod_evaluation():
    """3:25 PM: EOD trailing stop, P&L update, portfolio status."""
    close_old_connections()
    logger.info("[SCHEDULER] Starting EOD evaluation...")
    try:
        from stocks.services.trade_engine import run_eod_evaluation as _eod
        _eod()
        logger.info("[SCHEDULER] EOD evaluation completed.")
    except Exception as e:
        logger.error("[SCHEDULER] EOD evaluation FAILED: %s", e)


def run_expiry_cleanup():
    """Weekend: Expire stale pending signals, close old positions."""
    close_old_connections()
    logger.info("[SCHEDULER] Starting expiry cleanup...")
    try:
        from stocks.services.trade_engine import run_expiry_cleanup as _cleanup
        _cleanup()
        logger.info("[SCHEDULER] Expiry cleanup completed.")
    except Exception as e:
        logger.error("[SCHEDULER] Expiry cleanup FAILED: %s", e)


def send_short_term_status_update():
    """
    Daily live P&L status update for all ACTIVE short-term swing holdings.
    Sent to the dedicated short-term Telegram channel at 3:30 PM IST.
    """
    close_old_connections()
    logger.info("[SCHEDULER] Sending short-term swing live status update...")
    try:
        from stocks.models import ShortTermSignal
        from stocks.services.telegram_service import queue_telegram_message, get_short_term_chat_id
        from stocks.services.market_data_orchestrator import get_orchestrator

        active_signals = list(ShortTermSignal.objects.filter(status=ShortTermSignal.Status.ACTIVE))
        pending_signals = list(ShortTermSignal.objects.filter(status=ShortTermSignal.Status.PENDING))

        if not active_signals and not pending_signals:
            logger.info("[SCHEDULER] No active or pending swing signals — skipping status update.")
            return

        orch = get_orchestrator()
        lines = []

        # ── ACTIVE holdings with live P&L ──
        if active_signals:
            lines.append("🟢 <b>ACTIVE HOLDINGS</b>")
            for sig in active_signals:
                try:
                    price_res = orch.get_price(sig.symbol, exchange="NSE") or {}
                    ltp = float(price_res.get("ltp", 0) or 0)
                except Exception:
                    ltp = 0

                entry = float(sig.entry_price)
                target = float(sig.target)
                sl = float(sig.stop_loss)

                if ltp > 0:
                    pnl_pct = ((ltp - entry) / entry) * 100
                    pnl_emoji = "📈" if pnl_pct >= 0 else "📉"
                    pnl_str = f"{pnl_pct:+.2f}%"
                    ltp_str = f"₹{ltp:.2f}"
                else:
                    pnl_emoji = "📊"
                    pnl_str = "Fetching..."
                    ltp_str = "—"

                lines.append(
                    f"\n  <b>{sig.symbol}</b> {pnl_emoji}\n"
                    f"  ▸ Entry: ₹{entry:.2f} | CMP: {ltp_str}\n"
                    f"  ▸ P&L: <b>{pnl_str}</b>\n"
                    f"  ▸ Target: ₹{target:.2f} | SL: ₹{sl:.2f}"
                )

        # ── PENDING setups summary ──
        if pending_signals:
            lines.append("\n⏳ <b>PENDING PULLBACKS</b>")
            for sig in pending_signals:
                lines.append(f"  • <b>{sig.symbol}</b> — Entry near ₹{float(sig.entry_price):.2f}")

        from datetime import datetime
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now_str = datetime.now(tz=ist).strftime("%d %b %Y, %I:%M %p IST")

        msg = (
            f"📊 <b>SHORT-TERM SWING STATUS UPDATE</b>\n"
            f"🕒 {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(lines) +
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Powered by TradePulse AI</i>"
        )

        chat_id = get_short_term_chat_id()
        queue_telegram_message(msg, chat_id=chat_id, event_type='SWING_STATUS_UPDATE')
        logger.info("[SCHEDULER] Short-term swing status update queued (%d active, %d pending)",
                    len(active_signals), len(pending_signals))
    except Exception as e:
        logger.error("[SCHEDULER] Short-term swing status update FAILED: %s", e)


def send_intraday_option_buying_status_update():
    """3:32 PM IST — end-of-day position/P&L summary for intraday equity + option-buying
    signals, one-shot like Specialist's daily Telegram summary (not a live per-15-min
    update — that cadence stays on run_periodic_scanners for target/SL/cutoff enforcement,
    which is a separate concern from this notification).

    Scheduled 2 min after final_eod_update_328 (3:28 PM) so that job's exit/cutoff
    processing has already run — by 3:30 PM, intraday's 3:20 PM force-close and
    option-buying's 2:30 PM force-close mean today's rows should already be terminal
    (HIT_TARGET/HIT_SL/EXPIRED/CANCELLED), so this reads exit_price directly rather than
    fetching live LTP (unlike send_short_term_status_update, whose positions carry
    overnight and are still genuinely open when its message goes out).
    """
    close_old_connections()
    logger.info("[SCHEDULER] Sending intraday/option-buying EOD status update...")
    try:
        from django.utils import timezone as dj_tz
        from stocks.models import SignalHistory
        from stocks.services.telegram_service import queue_telegram_message, get_intraday_chat_id

        today = dj_tz.localtime().date()
        todays_signals = list(
            SignalHistory.objects.filter(
                category__in=["intraday", "option_buying"],
                generated_at__date=today,
            )
        )

        if not todays_signals:
            logger.info("[SCHEDULER] No intraday/option-buying signals today — skipping status update.")
            return

        terminal_statuses = {
            SignalHistory.Status.HIT_TARGET,
            SignalHistory.Status.HIT_SL,
            SignalHistory.Status.EXPIRED,
        }
        closed = [s for s in todays_signals if s.status in terminal_statuses]
        cancelled = [s for s in todays_signals if s.status == SignalHistory.Status.CANCELLED]
        still_open = [s for s in todays_signals if s.status in (SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING)]

        def _pnl_pct(sig):
            entry = float(sig.entry_price)
            exitp = float(sig.exit_price) if sig.exit_price is not None else None
            if not entry or exitp is None:
                return None
            if sig.category == "option_buying" or sig.signal_type == "BUY":
                return ((exitp - entry) / entry) * 100
            return ((entry - exitp) / entry) * 100

        lines = []
        for label, category in (("INTRADAY EQUITY", "intraday"), ("OPTION BUYING", "option_buying")):
            cat_closed = [s for s in closed if s.category == category]
            if not cat_closed:
                continue
            wins = [s for s in cat_closed if s.status == SignalHistory.Status.HIT_TARGET]
            losses = [s for s in cat_closed if s.status == SignalHistory.Status.HIT_SL]
            lines.append(f"\n📁 <b>{label}</b> — {len(wins)}W / {len(losses)}L / {len(cat_closed)} total")
            for sig in cat_closed:
                pnl = _pnl_pct(sig)
                pnl_str = f"{pnl:+.2f}%" if pnl is not None else "—"
                emoji = "✅" if sig.status == SignalHistory.Status.HIT_TARGET else (
                    "❌" if sig.status == SignalHistory.Status.HIT_SL else "⏱️"
                )
                lines.append(f"  {emoji} <b>{sig.symbol}</b> — {pnl_str} ({sig.status})")

        if still_open:
            lines.append(f"\n⚠️ <b>STILL OPEN AT EOD</b> ({len(still_open)}) — expected to be force-closed already:")
            for sig in still_open:
                lines.append(f"  • <b>{sig.symbol}</b> ({sig.category}, {sig.status})")

        if cancelled:
            lines.append(f"\n⬜ Cancelled (never triggered): {len(cancelled)}")

        from datetime import datetime
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now_str = datetime.now(tz=ist).strftime("%d %b %Y, %I:%M %p IST")

        msg = (
            f"📊 <b>INTRADAY / OPTION BUYING — EOD SUMMARY</b>\n"
            f"🕒 {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(lines) +
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Powered by TradePulse AI</i>"
        )

        chat_id = get_intraday_chat_id()
        queue_telegram_message(msg, chat_id=chat_id, event_type='INTRADAY_OPTBUY_EOD_SUMMARY')
        logger.info(
            "[SCHEDULER] Intraday/option-buying EOD status update queued (%d closed, %d still open, %d cancelled)",
            len(closed), len(still_open), len(cancelled),
        )
    except Exception as e:
        logger.error("[SCHEDULER] Intraday/option-buying EOD status update FAILED: %s", e)


def run_telegram_queue():
    """Every 1 minute: Process PENDING TelegramLog entries and dispatch to Telegram API."""
    close_old_connections()
    try:
        from stocks.services.telegram_service import process_telegram_queue
        process_telegram_queue()
    except Exception as e:
        logger.error("[SCHEDULER] Telegram queue dispatcher FAILED: %s", e)


def run_download_queue_drain():
    """Phase 3 of doc/MARKET_DATA_ENGINE_ARCHITECTURE.md (§2, §3, §12): drain the
    DownloadRequest queue as a proactive cache warmer, ahead of the engines'
    existing scan steps.

    Purely additive and safe to fail silently. No existing engine depends on this
    succeeding: intraday_service.py, swing_service.py, and every other scan step
    are unmodified and keep using their own direct candle_store-backed fetch
    exactly as before — this only pre-warms what they'd otherwise fetch on demand.
    Producer: run_candle_trickle_warmer() below, added 2026-07-27.
    """
    close_old_connections()
    try:
        from stocks.services.market_data import download_queue
        from stocks.services.market_data.gateway import get_service
        svc = get_service()
        summary = download_queue.drain_once(svc)
        logger.info("[SCHEDULER][DOWNLOAD_QUEUE] drain_once: %s", summary)
    except Exception as e:
        logger.error("[SCHEDULER] Download queue drain FAILED (non-fatal, no-op): %s", e)


def run_candle_trickle_warmer():
    """Every ~20s (Mon-Fri): enqueue + drain exactly one symbol's daily candles.

    Added 2026-07-27 in response to a rate-limit incident: shared/universe.py's
    _liquidity_stats() does ~150-200 candle calls back-to-back once per 15-min scan
    cycle — paced at 1.5s/call (already respecting Angel One's documented per-second
    limit) but still a burst, all crammed into the first few minutes of every cycle.
    This spreads the identical work across one tick per symbol instead, using the
    existing DownloadRequest queue (download_queue.py) rather than a new mechanism —
    enqueue_next_trickle_symbol() is the producer that module's own docstring said was
    "intentionally left for later". Draining immediately (small batch_limit) rather
    than waiting for the separate 5-min drain job keeps this genuinely one-symbol-at-
    a-time instead of letting a backlog build and drain in a sub-burst.

    Does NOT fix an Angel One account/endpoint restriction already in effect — a
    single isolated call fails identically to a burst (confirmed 2026-07-27 with a
    standalone script making exactly one request, zero surrounding load). This only
    reduces how often bursts happen once Angel One access is behaving normally.
    """
    close_old_connections()
    try:
        from stocks.services.market_data import download_queue
        from stocks.services.market_data.gateway import get_service

        row = download_queue.enqueue_next_trickle_symbol()
        if row is None:
            return

        svc = get_service()
        if svc is None:
            return

        summary = download_queue.drain_once(svc, batch_limit=3)
        logger.info("[SCHEDULER][CANDLE_TRICKLE] %s -> %s", row.symbol, summary)
    except Exception as e:
        logger.error("[SCHEDULER] Candle trickle warmer FAILED (non-fatal): %s", e)


def run_candle_bars_cleanup():
    """Daily off-hours prune of CandleBar. Added 2026-07-28 after a one-time manual
    cleanup found FIFTEEN_MINUTE rows (leftover from a past backtest run, confirmed
    unused by any live code path — see doc/SIGNAL_ENGINES_CURRENT_STATE.md) made up
    roughly half the table, and FIVE_MINUTE bars accumulate unbounded from the tick
    aggregator with nothing ever pruning them.

    Three different retention windows, deliberately NOT a single cutoff — ONE_DAY and
    FIVE_MINUTE have very different real consumers:
      - FIVE_MINUTE: intraday reads back 3 days, option-buying reads back 2 — 4 days
        gives a day of margin over the longer of the two.
      - FIFTEEN_MINUTE: zero live consumers (grep-confirmed), delete unconditionally.
      - ONE_DAY: short-term's AI scoring and NIFTY50_INDEX's benchmark both read back
        365 days — 400 days keeps a margin without ever approaching that limit. Do
        NOT drop this to match FIVE_MINUTE's window; that would break the intraday
        liquidity filter (40d), long-term quality checks (200d), and short-term/
        market-direction's NIFTY benchmark (365d) the same way today's incident did.
    """
    close_old_connections()
    try:
        from django.utils import timezone as dj_tz
        from datetime import timedelta
        from stocks.models import CandleBar

        deleted_15m, _ = CandleBar.objects.filter(interval="FIFTEEN_MINUTE").delete()

        five_min_cutoff = dj_tz.now() - timedelta(days=4)
        deleted_5m, _ = CandleBar.objects.filter(
            interval="FIVE_MINUTE", ts__lt=five_min_cutoff
        ).delete()

        one_day_cutoff = dj_tz.now() - timedelta(days=400)
        deleted_1d, _ = CandleBar.objects.filter(
            interval="ONE_DAY", ts__lt=one_day_cutoff
        ).delete()

        remaining = CandleBar.objects.count()
        logger.info(
            "[SCHEDULER][CANDLE_CLEANUP] Deleted FIFTEEN_MINUTE=%d FIVE_MINUTE(>4d)=%d ONE_DAY(>400d)=%d. Remaining=%d",
            deleted_15m, deleted_5m, deleted_1d, remaining,
        )
        return {
            "deleted_fifteen_minute": deleted_15m,
            "deleted_five_minute_over_4d": deleted_5m,
            "deleted_one_day_over_400d": deleted_1d,
            "remaining_rows": remaining,
        }
    except Exception as e:
        logger.error("[SCHEDULER] Candle bars cleanup FAILED (non-fatal): %s", e)
        return {"error": str(e)}


def run_signal_tables_cleanup():
    """Daily off-hours prune of four high-write tables that had no retention job at all
    before this — SignalHistory, SignalCandidate, TelegramLog, TradeHistory. Modeled on
    run_candle_bars_cleanup() above (same close_old_connections() + try/except-non-fatal
    shape). Added for doc/AUDIT_REMEDIATION_PLAN.md Phase 3 #3.2.2.

    Terminal-state filtering (confirmed against the LIVE model definitions in models.py
    at the time this was written, not assumed from any older doc or from SignalHistory's
    vocabulary):
      - SignalHistory.Status: PENDING, ACTIVE, CANCELLED, HIT_TARGET, HIT_SL, EXPIRED.
        Only CANCELLED/HIT_TARGET/HIT_SL/EXPIRED are terminal; PENDING/ACTIVE rows are
        never touched regardless of age.
      - SignalCandidate.Status: PENDING, ACCEPTED, REJECTED — a DIFFERENT vocabulary from
        SignalHistory despite the similar-sounding model name (this is the exact mix-up
        risk flagged when this job was written — don't assume HIT_TARGET/HIT_SL/CANCELLED/
        EXPIRED apply here, they don't exist on this model). In practice every row is
        written by bulk_create with status already set to ACCEPTED or REJECTED (see
        selfcheck_signal_candidate_audit.py) — PENDING is just the model's unused default.
        Only ACCEPTED/REJECTED are pruned by age; a PENDING row, if one ever exists, is
        left alone — same conservative rule as SignalHistory's PENDING/ACTIVE.
      - TelegramLog / TradeHistory: pure audit logs with no lifecycle status relevant to
        deletion — pruned by age alone (sent_at / timestamp). TradeHistory gets the
        longest window since it's the audit trail of record.

    Batches deletes at 5000 rows via a pk__in loop rather than `queryset[:5000].delete()`
    directly — Django's QuerySet.delete() rejects a sliced/limited queryset outright
    ("Cannot use 'limit' or 'offset' with delete()"), so the slice is only used to fetch a
    page of ids, matching the "iterator() + chunked pk__in deletes" alternative already
    named in the remediation plan for this item.
    """
    close_old_connections()
    try:
        from django.utils import timezone as dj_tz
        from datetime import timedelta
        from stocks.models import SignalHistory, SignalCandidate, TelegramLog, TradeHistory

        def _batched_delete(queryset, batch_size=5000):
            model = queryset.model
            total = 0
            while True:
                ids = list(queryset.values_list("pk", flat=True)[:batch_size])
                if not ids:
                    break
                deleted, _ = model.objects.filter(pk__in=ids).delete()
                total += deleted
                if len(ids) < batch_size:
                    break
            return total

        signal_history_cutoff = dj_tz.now() - timedelta(days=SIGNAL_HISTORY_RETENTION_DAYS)
        deleted_signal_history = _batched_delete(
            SignalHistory.objects.filter(
                status__in=[
                    SignalHistory.Status.CANCELLED,
                    SignalHistory.Status.HIT_TARGET,
                    SignalHistory.Status.HIT_SL,
                    SignalHistory.Status.EXPIRED,
                ],
                generated_at__lt=signal_history_cutoff,
            )
        )

        signal_candidate_cutoff = dj_tz.now() - timedelta(days=SIGNAL_CANDIDATE_RETENTION_DAYS)
        deleted_signal_candidate = _batched_delete(
            SignalCandidate.objects.filter(
                status__in=[
                    SignalCandidate.Status.ACCEPTED,
                    SignalCandidate.Status.REJECTED,
                ],
                created_at__lt=signal_candidate_cutoff,
            )
        )

        telegram_log_cutoff = dj_tz.now() - timedelta(days=TELEGRAM_LOG_RETENTION_DAYS)
        deleted_telegram_log = _batched_delete(
            TelegramLog.objects.filter(sent_at__lt=telegram_log_cutoff)
        )

        trade_history_cutoff = dj_tz.now() - timedelta(days=TRADE_HISTORY_RETENTION_DAYS)
        deleted_trade_history = _batched_delete(
            TradeHistory.objects.filter(timestamp__lt=trade_history_cutoff)
        )

        logger.info(
            "[SCHEDULER][SIGNAL_TABLES_CLEANUP] Deleted SignalHistory(terminal,>%dd)=%d "
            "SignalCandidate(terminal,>%dd)=%d TelegramLog(>%dd)=%d TradeHistory(>%dd)=%d",
            SIGNAL_HISTORY_RETENTION_DAYS, deleted_signal_history,
            SIGNAL_CANDIDATE_RETENTION_DAYS, deleted_signal_candidate,
            TELEGRAM_LOG_RETENTION_DAYS, deleted_telegram_log,
            TRADE_HISTORY_RETENTION_DAYS, deleted_trade_history,
        )
        return {
            "deleted_signal_history": deleted_signal_history,
            "deleted_signal_candidate": deleted_signal_candidate,
            "deleted_telegram_log": deleted_telegram_log,
            "deleted_trade_history": deleted_trade_history,
        }
    except Exception as e:
        logger.error("[SCHEDULER] Signal tables cleanup FAILED (non-fatal): %s", e)
        return {"error": str(e)}


def run_tick_aggregator_tick():
    """Every ~30s during market hours (Mon-Fri): roll already-arrived WebSocket
    ticks up into 5-min OHLCV bars for the scan universe, purely from local
    memory — see market_data/tick_aggregator.py for why this is zero Angel One
    REST calls and therefore zero rate-limit exposure, unlike every other job in
    this file. Lets intraday_service.py and option_buying_service.py keep
    detecting breakouts even while getCandleData is blocked, since both read
    CandleBar locally instead of calling it directly.

    Originally every 15s calling tick_aggregator per-symbol (one cache file
    read+write per symbol via FileBasedCache) — confirmed in production
    2026-07-27 that this made the job's own runs pile up and get skipped for
    4+ minutes straight. Fixed by batching into one cache read/write per tick
    (roll_up_universe); widened to 30s on top of that fix for extra headroom.
    """
    close_old_connections()
    try:
        from stocks.services.market_data.gateway import get_service
        from stocks.services.market_data import tick_aggregator, download_queue
        from stocks.services.shared.regime import NIFTY50_TOKEN

        svc = get_service()
        if svc is None:
            return

        universe = set(download_queue.get_universe_symbols())
        try:
            universe |= set(svc.get_fo_stocks())
        except Exception:
            pass
        if not universe:
            return

        token_map = svc.get_token_map(list(universe), exchange="NSE") or {}
        token_map["NIFTY50_INDEX"] = NIFTY50_TOKEN

        processed = tick_aggregator.roll_up_universe(token_map, exchange="NSE")
        logger.debug("[SCHEDULER][TICK_AGG] rolled up %d/%d symbols", processed, len(token_map))
    except Exception as e:
        logger.error("[SCHEDULER] Tick aggregator FAILED (non-fatal): %s", e)


_scheduler = None

def start():
    """Start the background scheduler."""
    global _scheduler
    
    # Always run on Render. In local dev with the autoreloader, only run in the
    # reloaded child process (RUN_MAIN) to avoid double-starting the scheduler
    # (once in the reloader's watcher process, once in the actual child it
    # spawns). With --noreload there's no watcher/child split at all — Django's
    # autoreload machinery never runs, so RUN_MAIN never gets set even though
    # this is the one and only process — that combination used to make start()
    # silently return here forever, so no scheduled job (including the 15-min
    # intraday/option-buying scan) ever ran locally when using --noreload.
    is_render = os.environ.get('RENDER') == 'true'
    is_dev_main = os.environ.get('RUN_MAIN') == 'true'
    is_noreload = '--noreload' in sys.argv

    if not is_render and not is_dev_main and not is_noreload:
        return

    if _scheduler is not None:
        logger.info("APScheduler already running. Skipping initialization.")
        return

    # Eagerly import modules that multiple concurrently-scheduled jobs below
    # otherwise each lazily import on their own first call — run_download_queue_drain,
    # run_candle_trickle_warmer, and run_tick_aggregator_tick all reach into
    # stocks.services.market_data.{gateway,download_queue,tick_aggregator}. Python's
    # import system isn't safe against two threads concurrently first-importing the
    # same not-yet-loaded module: confirmed in production 2026-07-27, right after a
    # fresh deploy, two job threads landing on the same tick both tried to import
    # gateway for the first time and got "deadlock detected by
    # _ModuleLock('stocks.services.market_data.gateway')" — which then left both jobs
    # skipping for several minutes before self-recovering. Importing once here, single-
    # threaded, before the scheduler (and its worker threads) exists at all removes the
    # race entirely.
    import stocks.services.market_data.gateway
    import stocks.services.market_data.download_queue
    import stocks.services.market_data.tick_aggregator

    logger.info("Initializing APScheduler for TradePulse daily tasks...")
    KOLKATA_TZ = pytz.timezone("Asia/Kolkata")
    _scheduler = BackgroundScheduler(
        timezone=KOLKATA_TZ,
        job_defaults={
            'misfire_grace_time': 300,   # 5-minute grace (accounts for cold starts)
            'max_instances': 1,
            'replace_existing': True
        }
    )

    # ──────────────────────────────────────────────────────────────────
    # TRADE ENGINE PIPELINE JOBS
    # Full event-driven lifecycle: Pre-market → Scanner → Intraday → EOD
    # ──────────────────────────────────────────────────────────────────

    # 8:30 AM — Calendar sync: persist earnings + corporate actions (before pre-market)
    _scheduler.add_job(
        run_calendar_sync,
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=30, second=0, timezone=KOLKATA_TZ),
        id="calendar_sync_0830",
    )
    logger.info("[SCHEDULER] Registered: calendar_sync @ 8:30 AM IST (Mon-Fri)")

    # 9:05 AM — Pre-market: Update Nifty/Sector trends
    _scheduler.add_job(
        run_premarket_update,
        trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=5, second=0, timezone=KOLKATA_TZ),
        id="trade_engine_premarket",
    )

    # 11:30 AM — Daily Scanner: AI score + Trade creation + Telegram
    # Moved from 10:00 AM (2026-07-28) — that collided with intraday's and option-buying's
    # own first generation attempts (9:45 AM / 10:00 AM respectively), all three burning
    # Angel One REST budget in the same window. 11:30 sits clear of both, past the 10:45
    # strangle scan, and off the entry-activation-checker's :15/:45 ticks.
    # misfire_grace_time=1 (overriding the 300s job default): this scan pulls candles
    # for up to 50 Nifty 500 candidates in one go. If a redeploy lands in the 5-minute
    # catch-up window, letting it fire late stacks that burst on top of an already-cold
    # Angel One session/candle cache — better to skip this occurrence and resume next
    # trading day than risk tripping the WAF circuit breaker at cold start.
    _scheduler.add_job(
        run_short_term_scan,
        trigger=CronTrigger(day_of_week="mon-fri", hour=11, minute=30, second=0, timezone=KOLKATA_TZ),
        id="trade_engine_scanner_10am",
        misfire_grace_time=1,
    )

    # 11:35 AM — Status update after scanner
    _scheduler.add_job(
        send_short_term_status_update,
        trigger=CronTrigger(day_of_week="mon-fri", hour=11, minute=35, second=0, timezone=KOLKATA_TZ),
        id="trade_engine_status_1005am",
    )

    # 12:00 PM — Long-term momentum screen (re-enabled 2026-07-28, LONG_TERM_GENERATION_ENABLED).
    # Own dedicated slot rather than piggybacking on the short-term scan's Telegram summary
    # (the old side-effect pattern — see run_long_term_scan()'s docstring), and staggered
    # 30 min after the short-term scan above so the two don't burst Angel One at once.
    _scheduler.add_job(
        run_long_term_scan,
        trigger=CronTrigger(day_of_week="mon-fri", hour=12, minute=0, second=0, timezone=KOLKATA_TZ),
        id="long_term_scanner_12pm",
        misfire_grace_time=1,
    )
    logger.info("[SCHEDULER] Registered: long_term_scanner @ 12:00 PM IST (Mon-Fri)")

    # 4:00 PM — Long-term outcomes audit: close positions on a 200-EMA trend break
    # (audit remediation 2026-07-28). Runs after the market close so the day's final
    # daily bar is used, and independently of LONG_TERM_GENERATION_ENABLED — see
    # run_long_term_outcomes_audit()'s docstring for why it's safe to always run.
    _scheduler.add_job(
        run_long_term_outcomes_audit,
        trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=0, second=0, timezone=KOLKATA_TZ),
        id="long_term_outcomes_audit_4pm",
    )
    logger.info("[SCHEDULER] Registered: long_term_outcomes_audit @ 4:00 PM IST (Mon-Fri)")

    # 10:15 AM - 3:15 PM — Entry Activation Checker: every 30 minutes (Mon-Fri)
    _scheduler.add_job(
        run_intraday_check,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="10-15",
            minute="15,45",
            second=0,
            timezone=KOLKATA_TZ
        ),
        id="trade_engine_activation_checker",
    )

    # 3:25 PM — EOD evaluation: trailing stop, P&L, portfolio Telegram
    _scheduler.add_job(
        run_eod_evaluation,
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=25, second=0, timezone=KOLKATA_TZ),
        id="trade_engine_eod_325pm",
    )

    # 3:35 PM — Final status update
    _scheduler.add_job(
        send_short_term_status_update,
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=35, second=0, timezone=KOLKATA_TZ),
        id="trade_engine_status_335pm",
    )

    # Saturday 6 AM — Weekly cleanup: Expire stale setups
    _scheduler.add_job(
        run_expiry_cleanup,
        trigger=CronTrigger(day_of_week="sat", hour=6, minute=0, second=0, timezone=KOLKATA_TZ),
        id="trade_engine_weekly_cleanup",
    )

    # 4:05 PM — Swing V2 SHADOW (dry run, persists nothing). Accumulates the evidence
    # required before the cutover in Phase 5. See run_swing_v2_shadow().
    _scheduler.add_job(
        run_swing_v2_shadow,
        trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=5, second=0, timezone=KOLKATA_TZ),
        id="swing_v2_shadow_1605",
    )
    logger.info("[SCHEDULER] Registered: swing_v2_shadow @ 4:05 PM IST (Mon-Fri, DRY RUN)")

    logger.info("[SCHEDULER] Registered Trade Engine pipeline: pre-market(9:05), scanner(10:00), intraday(10min), EOD(3:25), cleanup(Sat 6AM)")

    # ──────────────────────────────────────────────────────────────────
    # JOB 1 — 10:45 AM IST (Mon–Fri)
    # Guaranteed daily strangle signal generation.
    # Clears throttle cache → runs full sync scan → sends Telegram summary.
    # This is the SINGLE source of truth for signal generation.
    # ──────────────────────────────────────────────────────────────────
    _scheduler.add_job(
        run_10am_strangle_scan,
        trigger=CronTrigger(day_of_week="mon-fri", hour=10, minute=45, timezone=KOLKATA_TZ),
        id="strangle_signal_10am",
    )
    logger.info("[SCHEDULER] Registered: strangle_signal_10am @ 10:45 AM IST (Mon-Fri)")

    # ──────────────────────────────────────────────────────────────────
    # JOB 2 — Every 15 minutes 9:00 AM → 3:15 PM IST (Mon–Fri)
    # Live position updates: CMP refresh, P&L, SL/Target checks.
    #
    # kwargs is intentionally omitted (action defaults to None), NOT "action":
    # "update". intraday_service.get_live_signals() and
    # option_buying_service.get_option_buying_signals() both auto-resolve
    # action=None to "generate" the first time today's signal doesn't exist
    # yet, and "update" every call after — that's the only path that ever
    # generates a fresh intraday/option-buying signal, since the 10:45 AM
    # run_10am_strangle_scan job only generates specialist/strangle signals.
    # Hardcoding "update" here permanently short-circuits that auto-resolve
    # check, so intraday/option-buying could never generate anything, ever.
    # ──────────────────────────────────────────────────────────────────
    from stocks.services.live_signal_service import run_periodic_scanners
    # Hours 9-14: every 15 min. Previously started at 11:00, which excluded the
    # 9:15-11:00 window — the highest-volume, highest-opportunity part of the NSE
    # session — and meant open positions went unaudited during it. Extending this was
    # gated on the volume profile having a prior-day fallback (signal_utils
    # compute_session_volume_profile), since a developing profile is too thin to trade
    # off in the first hour. The 9:15 slot itself is skipped: the scan needs closed bars.
    # misfire_grace_time=1 on both jobs below (overriding the 300s job default): each
    # run scans the full liquidity-filtered universe via _liquidity_stats(), which on a
    # cold candle cache is itself a ~150-200-call REST burst. A redeploy landing in the
    # catch-up window would otherwise fire this burst immediately at cold start, on top
    # of Angel One session/WS bootstrap already in flight — skip that occurrence instead;
    # the next slot is only 15 minutes away.
    _scheduler.add_job(
        run_periodic_scanners,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="9-14",
            minute="0,15,30,45",
            second=0,
            timezone=KOLKATA_TZ,
        ),
        id="live_signal_update_hourly",
        misfire_grace_time=1,
    )
    # Hour 15: runs at 3:00 PM and 3:15 PM
    _scheduler.add_job(
        run_periodic_scanners,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=15,
            minute="0,15",
            second=0,
            timezone=KOLKATA_TZ,
        ),
        id="live_signal_update_final_hour",
        misfire_grace_time=1,
    )
    logger.info("[SCHEDULER] Registered: live_signal_update @ every 15min (9:00–15:15 IST Mon-Fri)")

    # ──────────────────────────────────────────────────────────────────
    # JOB 3 — 3:28 PM IST (Mon–Fri)
    # Final EOD update report (No auto square-off).
    # ──────────────────────────────────────────────────────────────────
    _scheduler.add_job(
        run_periodic_scanners,
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=28, second=0, timezone=KOLKATA_TZ),
        kwargs={"action": "update"},
        id="final_eod_update_328",
    )
    logger.info("[SCHEDULER] Registered: final_eod_update_328 @ 3:28 PM IST (Mon-Fri)")

    # 3:32 PM — Intraday/option-buying EOD position summary (Telegram), same one-shot
    # pattern as Specialist's daily summary. 4 min after final_eod_update_328 so that
    # job's exit/cutoff processing has already landed.
    _scheduler.add_job(
        send_intraday_option_buying_status_update,
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=32, second=0, timezone=KOLKATA_TZ),
        id="intraday_optbuy_status_332",
    )
    logger.info("[SCHEDULER] Registered: intraday_optbuy_status_332 @ 3:32 PM IST (Mon-Fri)")

    # ──────────────────────────────────────────────────────────────────
    # JOB 4 — 4:30 PM IST (Mon–Fri)
    # Full daily analytics pipeline: global market data + AI insights.
    # ──────────────────────────────────────────────────────────────────
    _scheduler.add_job(
        run_daily_pipeline,
        trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone=KOLKATA_TZ),
        id="daily_market_update",
    )
    logger.info("[SCHEDULER] Registered: daily_market_update @ 4:30 PM IST (Mon-Fri)")

    # ──────────────────────────────────────────────────────────────────
    # JOB 5 — Every 1 minute (all days)
    # User account cleanup (temporary guest session expiry).
    # ──────────────────────────────────────────────────────────────────
    _scheduler.add_job(
        run_user_cleanup,
        trigger="interval",
        minutes=1,
        id="user_account_cleanup",
    )
    logger.info("[SCHEDULER] Registered: user_account_cleanup @ every 1min")

    # ──────────────────────────────────────────────────────────────────
    # JOB 6 — Every 1 minute (all days)
    # Asynchronous Telegram Queue Dispatcher.
    # Picks up PENDING TelegramLog entries and delivers them to Telegram.
    # Non-blocking: trading engine writes to DB, this job sends later.
    # ──────────────────────────────────────────────────────────────────
    _scheduler.add_job(
        run_telegram_queue,
        trigger="interval",
        minutes=1,
        id="telegram_queue_dispatcher",
    )
    logger.info("[SCHEDULER] Registered: telegram_queue_dispatcher @ every 1min")

    # ──────────────────────────────────────────────────────────────────
    # JOB 7 — Every 5 minutes, 9 AM–3:30 PM IST (Mon–Fri)
    # Download Queue drain (Phase 3 of MARKET_DATA_ENGINE_ARCHITECTURE.md).
    # Proactive cache warmer, mops up anything JOB 8 (below) enqueues plus
    # whatever it hasn't drained itself yet. Does NOT gate, replace, or precede
    # any existing engine's own scan step; every engine keeps fetching via its
    # own existing candle_store-backed path unchanged. Safe to fail silently.
    # ──────────────────────────────────────────────────────────────────
    _scheduler.add_job(
        run_download_queue_drain,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="*/5",
            second=0,
            timezone=KOLKATA_TZ,
        ),
        id="download_queue_drain",
    )
    logger.info("[SCHEDULER] Registered: download_queue_drain @ every 5min (9:00-15:55 IST Mon-Fri)")

    # ──────────────────────────────────────────────────────────────────
    # JOB 8 — Every 2 minutes, all day (Mon–Fri)
    # Candle trickle warmer — added 2026-07-27, slowed from every 20s to every
    # 2 min on 2026-07-28. At 20s, this job's own REST call landed inside every
    # single circuit-breaker-clear window (the local 5-min cooldown expiring),
    # so the account's very next attempt kept re-tripping the WAF before it had
    # any real quiet stretch — observed in production as a fresh 403 roughly
    # every 5-8 minutes, continuously, for over an hour on 2026-07-28, well
    # outside any redeploy window. Spacing ticks out to 2 min gives Angel One's
    # side an actual gap to stop treating this account as still-bursting.
    # Slower cache warm-up as a tradeoff, but a cache that never finishes
    # warming because it keeps re-triggering its own ban is worse.
    # See run_candle_trickle_warmer()'s docstring for why this doesn't fix an
    # already-blocked account, only reduces how often bursts happen once
    # access is normal.
    # ──────────────────────────────────────────────────────────────────
    _scheduler.add_job(
        run_candle_trickle_warmer,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            minute="*/2",
            second=0,
            timezone=KOLKATA_TZ,
        ),
        id="candle_trickle_warmer",
    )
    logger.info("[SCHEDULER] Registered: candle_trickle_warmer @ every 2min (all day, Mon-Fri)")

    # ──────────────────────────────────────────────────────────────────
    # JOB 9 — Every 30 seconds, 9:00 AM–3:30 PM IST (Mon–Fri)
    # Tick aggregator — added 2026-07-27, see run_tick_aggregator_tick()'s
    # docstring. Zero Angel One REST calls (pure local WebSocket-cache reads),
    # so unlike every other job here this one has no rate-limit exposure and
    # doesn't need pacing. Confined to market hours (unlike JOB 8) because
    # there's nothing to roll up outside them — no ticks arrive when the
    # market's closed. Was 15s originally; widened to 30s after the batching
    # fix (see roll_up_universe) for extra headroom.
    # ──────────────────────────────────────────────────────────────────
    _scheduler.add_job(
        run_tick_aggregator_tick,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            second="*/30",
            timezone=KOLKATA_TZ,
        ),
        id="tick_aggregator",
    )
    logger.info("[SCHEDULER] Registered: tick_aggregator @ every 30s (9:00-15:59 IST Mon-Fri)")

    # candle_bars cleanup — added 2026-07-28, see run_candle_bars_cleanup()'s
    # docstring for the three retention windows. Runs once daily, off-hours
    # (well before the 9:00 AM market-hours jobs start), so a slower DELETE over
    # what's normally a much larger table doesn't contend with live scanning.
    _scheduler.add_job(
        run_candle_bars_cleanup,
        trigger=CronTrigger(hour=2, minute=30, second=0, timezone=KOLKATA_TZ),
        id="candle_bars_cleanup",
    )
    logger.info("[SCHEDULER] Registered: candle_bars_cleanup @ 2:30 AM IST (daily)")

    # signal_tables cleanup — added for doc/AUDIT_REMEDIATION_PLAN.md Phase 3 #3.2.2, see
    # run_signal_tables_cleanup()'s docstring for the per-model retention windows and
    # terminal-state filtering. Staggered 15 minutes after candle_bars_cleanup (2:45 AM vs
    # 2:30 AM) so the two daily cleanup jobs don't contend for the same DB connection pool
    # slot by running at the exact same instant.
    _scheduler.add_job(
        run_signal_tables_cleanup,
        trigger=CronTrigger(hour=2, minute=45, second=0, timezone=KOLKATA_TZ),
        id="signal_tables_cleanup",
    )
    logger.info("[SCHEDULER] Registered: signal_tables_cleanup @ 2:45 AM IST (daily)")

    _scheduler.start()
    logger.info("[SCHEDULER] APScheduler started with %d jobs.", len(_scheduler.get_jobs()))


def run_user_cleanup():
    """Hard delete temporary users based on their first login or creation time."""
    close_old_connections()
    from django.utils import timezone
    from datetime import timedelta
    from users.models import CustomUser

    now = timezone.now()
    
    # 1. Delete users who logged in more than 1 minute ago
    session_threshold = now - timedelta(minutes=1)
    expired_sessions = CustomUser.objects.filter(
        is_temporary=True, 
        first_login_at__lt=session_threshold
    )
    
    # 2. Delete users who never logged in and were created more than 30 minutes ago
    unclaimed_threshold = now - timedelta(minutes=30)
    unclaimed_accounts = CustomUser.objects.filter(
        is_temporary=True,
        first_login_at__isnull=True,
        created_at__lt=unclaimed_threshold
    )
    
    count_expired = expired_sessions.count()
    count_unclaimed = unclaimed_accounts.count()
    
    if count_expired > 0:
        logger.info(f"Cleaning up {count_expired} expired guest sessions...")
        expired_sessions.delete()
        
    if count_unclaimed > 0:
        logger.info(f"Cleaning up {count_unclaimed} unclaimed guest accounts...")
        unclaimed_accounts.delete()
