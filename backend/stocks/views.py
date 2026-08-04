from datetime import date, datetime

from django.conf import settings
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle, AnonRateThrottle

from .models import SignalHistory
from .serializers import SignalHistorySerializer
from stocks.services.intraday_service import get_live_signals
from stocks.services.live_signal_service import get_latest_prices, update_signal_outcomes, get_performance_report
from stocks.services.signal_utils import IST, is_market_open
from stocks.services.option_chain_service import get_option_chain, get_option_chain_db_snapshot
from stocks.services.fii_dii_service import get_fii_dii_data
from stocks.services.trading_engine import candles_to_dataframe, get_market_rules, run_backtest_for_signal

# LivePriceUpdateView's server-side symbol cap. The frontend poller already limits
# itself to ACTIVE/PENDING signals (<=5 symbols, see LiveSignalsTable.jsx), but that
# was never enforced server-side — see the "403 rate limit errors" / "poller hitting
# 500+ symbols" entry in CLAUDE.md's Common Bugs history. Generous headroom over every
# engine's legitimate poll size, far below "hundreds". Same getattr(settings, ...,
# default) pattern as INTRADAY_ACCOUNT_EQUITY and friends elsewhere in this codebase.
LIVE_PRICE_MAX_SYMBOLS = int(getattr(settings, "LIVE_PRICE_MAX_SYMBOLS", 20))


class LiveSignalView(APIView):
    """GET /api/stocks/live-signals/ — instantly returns DB signals, offloads scanning to apscheduler"""
    permission_classes = (IsAuthenticated,)
    # 3.2.1: on top of the global user/anon DEFAULT_THROTTLE_RATES, this view carries its
    # own stricter 'force_scan' scope — the ?force=true path is designed to bypass
    # intraday_service.get_live_signals()'s 5-min scan-rate cooldown (see get() below), so
    # it needs a DRF-level cap independent of that cooldown rather than relying on it alone.
    throttle_classes = [UserRateThrottle, AnonRateThrottle, ScopedRateThrottle]
    throttle_scope = 'force_scan'

    def get_throttles(self):
        # throttle_scope is a class attribute, so ScopedRateThrottle would otherwise cap
        # EVERY request to this view at force_scan's rate (2/min) — including the plain
        # 5-min poll that isn't forcing anything. Only apply it to ?force=true requests,
        # matching the comment above's actual intent.
        if self.request.query_params.get('force', 'false').lower() == 'true':
            return [t() for t in self.throttle_classes]
        return [t() for t in self.throttle_classes if t is not ScopedRateThrottle]

    def get(self, request):
        try:
            from django.core.cache import cache
            from stocks.services.intraday_service import _live_intraday_payload, get_live_signals

            force = request.query_params.get('force', 'false').lower() == 'true'
            cached_sentiment = cache.get("intraday_nifty_sentiment", "SIDEWAYS")

            if force:
                # Force Scan: route through the real engine, which enforces its own
                # market-hours/cutoff gates and 5-min scan-rate cooldown — this does not
                # bypass those guards, just triggers a "generate" attempt subject to them.
                payload = get_live_signals(action="generate")
            else:
                # Resilience: Defer metadata to prevent 500 if column is missing
                payload = _live_intraday_payload()

            payload.setdefault("sentiment", cached_sentiment)
            payload.setdefault("market_status", "OPEN" if is_market_open() else "CLOSED")
            payload.setdefault("scanned", 0)
            payload.setdefault("timeframe", "1-min UI fetch")
            return Response(payload)
        except Exception as e:
            from django.utils import timezone
            return Response({
                "signals": [],
                "error": str(e),
                "market_status": "OPEN" if is_market_open() else "CLOSED",
                "timestamp": timezone.now().isoformat()
            }, status=500)


class PerformanceReportView(APIView):
    """GET /api/stocks/performance-report/"""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        from datetime import date
        from stocks.services.live_signal_service import get_performance_report
        update_signal_outcomes(force=True)
        
        target_date = None
        date_str = request.query_params.get('date')
        if date_str:
            try:
                target_date = date.fromisoformat(date_str)
            except ValueError:
                pass
                
        data = get_performance_report(target_date)
        return Response(data)


class SignalBacktestView(APIView):
    """POST /api/stocks/signal-backtest/ with candles + signal payload."""
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        candles = request.data.get("candles") or []
        signal = request.data.get("signal") or {}
        category = request.data.get("category", "intraday")

        if not candles or not signal:
            return Response({"detail": "Both 'candles' and 'signal' are required."}, status=400)

        try:
            df = candles_to_dataframe(candles)
            result = run_backtest_for_signal(df, signal, get_market_rules(category))
        except Exception as exc:
            return Response({"detail": f"Backtest failed: {exc}"}, status=400)

        return Response(result)


class OptionChainView(APIView):
    """GET /api/stocks/option-chain/?symbol=NIFTY"""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        from django.core.cache import cache
        # Local import removed as it is now at top-level
        symbol = request.query_params.get('symbol', 'NIFTY').upper()
        force = request.query_params.get('force', 'false').lower() == 'true'
        cache_key = f'option_chain_{symbol}_5m'
        
        if not force:
            cached_data = cache.get(cache_key)
            if cached_data:
                return Response(cached_data)

        # If market is closed, don't fetch new data
        if not is_market_open():
            # Return cached data if available
            cached_data = cache.get(cache_key)
            if cached_data:
                return Response(cached_data)
            
            # Fallback to the latest saved record in the database
            snapshot = get_option_chain_db_snapshot(symbol)
            if snapshot:
                return Response(snapshot)

            return Response({})

        data = get_option_chain(symbol)
        cache.set(cache_key, data, timeout=60 * 5)  # Cache for 5 minutes
        return Response(data)


class FIIDIIView(APIView):
    """GET /api/stocks/fii-dii/"""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        data = get_fii_dii_data()
        return Response(data)
class LivePriceUpdateView(APIView):
    """GET /api/stocks/live-price-updates/?symbols=RELIANCE,ACC"""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        symbols_str = request.query_params.get('symbols', '')
        if not symbols_str:
            return Response({})
        symbols = [s.strip() for s in symbols_str.split(',') if s.strip()]
        if len(symbols) > LIVE_PRICE_MAX_SYMBOLS:
            return Response(
                {"error": f"Too many symbols requested (max {LIVE_PRICE_MAX_SYMBOLS})."},
                status=400
            )
        data = get_latest_prices(symbols)
        return Response(data)

class ProSystemView(APIView):
    """GET /api/stocks/pro-system/ — Trade Engine Dashboard with tab-grouped signals & analytics"""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        from django.core.cache import cache
        from stocks.services.trade_engine import get_dashboard_data

        force = request.query_params.get('force', 'false').lower() == 'true'
        cache_key = 'trade_engine_dashboard_30s'

        if not force:
            cached_data = cache.get(cache_key)
            if cached_data:
                return Response(cached_data)

        data = get_dashboard_data()
        cache.set(cache_key, data, timeout=30)  # 30s cache — fresh during market hours
        return Response(data)

class ProPerformanceReportView(APIView):
    """GET /api/stocks/pro-performance-report/?date=YYYY-MM-DD"""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        from datetime import date
        from stocks.services.pro_system_service import get_pro_performance_report

        target_date = None
        date_str = request.query_params.get('date')
        if date_str:
            try:
                target_date = date.fromisoformat(date_str)
            except ValueError:
                pass

        data = get_pro_performance_report(target_date)
        return Response(data)


class DeltaHedgeView(APIView):
    """GET /api/stocks/delta-hedge/ — Returns option selling hedge suggestions.
    ?force=true (Force Scan button) bypasses the 2s cache and lets action auto-resolve
    instead of hardcoding "update" — same effect as the 10:45 AM cron's own call, just
    triggered on demand. get_hedge_panel_data's existing today_spec_exists check means
    this only actually scans if no specialist signal exists yet today; otherwise it's
    just a fresh "update" read, same as a plain poll."""
    permission_classes = (IsAuthenticated,)
    throttle_classes = [UserRateThrottle, AnonRateThrottle, ScopedRateThrottle]
    throttle_scope = 'force_scan'

    def get_throttles(self):
        # Only cap ?force=true requests at force_scan's rate — same fix as
        # LiveSignalView/OptionBuyingView.get_throttles(), not every 3s poll.
        # POST (Reset Strategy) is equally destructive/expensive as a forced scan
        # — cancels every live signal and immediately rebuilds — so it always gets
        # the same force_scan scope regardless of query params.
        if self.request.method == 'POST' or self.request.query_params.get('force', 'false').lower() == 'true':
            return [t() for t in self.throttle_classes]
        return [t() for t in self.throttle_classes if t is not ScopedRateThrottle]

    def get(self, request):
        from stocks.services.delta_hedge_service import get_hedge_panel_data
        from django.core.cache import cache

        force = request.query_params.get('force', 'false').lower() == 'true'

        # Cache for 2 seconds (live performance) — skipped on a forced scan so the
        # button's response reflects the fresh state, not a just-cached stale one.
        cache_key = "delta_hedge_panel_2s"
        if not force:
            cached = cache.get(cache_key)
            if cached:
                return Response(cached)

        try:
            # action="update" explicitly on a plain poll — a passive page view must
            # never trigger a fresh "generate" scan (that decision belongs solely to
            # the scheduled internal job in updater.py / run_periodic_scanners, which
            # calls this with action=None and lets it auto-decide). Force Scan passes
            # action=None too, so it gets that same auto-decide behavior instead of
            # always generating. sync_scan runs any resulting scan inline so a forced
            # call returns the fresh result immediately rather than via background
            # thread on the next poll.
            data = get_hedge_panel_data(action=None if force else "update", sync_scan=force)
            if not force:
                cache.set(cache_key, data, timeout=2)
            return Response(data)
        except Exception as e:
            from django.utils import timezone
            from stocks.services.signal_utils import is_market_open
            # Return an empty but safe structure if the DB is migrating or failing
            return Response({
                'timestamp': timezone.now().isoformat(),
                'market_status': 'OPEN' if is_market_open() else 'CLOSED',
                'sections': [],
                'error': f"System warming up or migrating: {str(e)}"
            })

    def post(self, request):
        """Reset Strategy: cancel all today's signals and immediately spawn fresh ones.

        Audit fix H5: this used to cancel the existing book FIRST, then separately try
        to rebuild it with no atomicity between the two — a scan failure partway through
        (or even a total scan failure) left the account flat with its hedge already
        cancelled and nothing to replace it, no automatic retry. Restructured into two
        phases: (1) a pre-scan that does all the slow network I/O (quotes, option
        chains) and builds candidate legs WITHOUT touching any existing signal; (2) a
        single DB transaction that cancels the old book and creates the new one
        together, only entered once phase 1 has confirmed there's something to create.
        If phase 1 finds nothing viable, the existing signals are left untouched and an
        error is returned instead of silently leaving the account unhedged.
        """
        from django.db import transaction
        from stocks.models import SignalHistory
        from django.core.cache import cache
        from django.utils import timezone
        import logging
        logger = logging.getLogger('stocks.views')

        # Guest/demo accounts (RegisterView always creates these with is_temporary=True,
        # see users/views.py) must not have destructive power over the live strangle
        # book — same is_temporary convention ProfileView already enforces for session
        # expiry. This blocks the specific disclosed hole without touching
        # permission_classes, so it can't lock out the real owner account.
        if getattr(request.user, 'is_temporary', False):
            logger.warning("[RESET] Blocked temporary account %s from resetting strategy", request.user.pk)
            return Response({"error": "This action is not available on a temporary account."}, status=403)

        today = timezone.now().date()
        from stocks.services.signal_utils import IST
        today_start = timezone.now().astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)

        # ── Phase 1: pre-scan candidates & build legs (network I/O — stays OUTSIDE any
        # DB transaction, and deliberately runs BEFORE any existing signal is touched).
        built = []  # list of (cand, spot, legs)
        try:
            from stocks.services.delta_hedge_service import (
                NIFTY_50_STOCKS, DEFAULT_STOCK_SIGMA,
                ENTRY_WINDOW_START, ENTRY_WINDOW_END,
                build_specialist_hedge, MIN_DAYS_TO_EXPIRY, MIN_STOCK_PRICE
            )
            from stocks.services.market_intelligence_service import get_symbol_market_state
            from stocks.services.market_data_orchestrator import get_orchestrator
            from stocks.services.truedata_service import get_truedata_instance

            svc = get_truedata_instance()
            orch = get_orchestrator()
            now_time = timezone.now().astimezone(IST).time()

            candidate_equities = []

            # Scan equities (all Nifty 50 stocks)
            if ENTRY_WINDOW_START <= now_time <= ENTRY_WINDOW_END:
                for sym in NIFTY_50_STOCKS():
                    if sym in ['NIFTY', 'BANKNIFTY', 'FINNIFTY']:
                        continue
                    try:
                        intel = get_symbol_market_state(sym, exchange='NSE', svc=svc)
                        vah, val = intel.get('vah', 0), intel.get('val', 0)
                        if vah > 0 and val > 0 and not intel.get('is_within_va', True):
                            continue
                        cur_price = intel.get('current_price', 0)
                        if 0 < cur_price < MIN_STOCK_PRICE:
                            continue
                        candidate_equities.append({
                            'symbol': sym, 'exchange': 'NFO',
                            'sigma': DEFAULT_STOCK_SIGMA, 'confidence': intel.get('confidence', 0)
                        })
                    except Exception:
                        pass

            # Rank & select: up to 10 equities
            candidate_equities.sort(key=lambda x: x['confidence'], reverse=True)
            selected = candidate_equities[:10]

            for i, cand in enumerate(selected):
                try:
                    price_res = orch.get_price(cand['symbol'], exchange='NSE') or {}
                    spot = price_res.get('ltp', 0)
                    if spot <= 0:
                        continue
                    legs = build_specialist_hedge(cand['symbol'], cand['exchange'], spot, orch, sigma=cand['sigma'])
                    if not legs:
                        continue
                    built.append((cand, spot, legs, i))
                except Exception as ce:
                    logger.error("[RESET] Failed to build candidate for %s: %s", cand.get('symbol'), ce)

        except Exception as scan_err:
            logger.error("[RESET] Pre-scan failed: %s", scan_err)

        if not built:
            logger.error("[RESET] Pre-scan produced zero viable candidates — leaving existing signals untouched.")
            return Response({
                "error": "Could not build any new strangle candidates — existing signals were left untouched.",
                "cancelled": 0,
                "created": 0,
            }, status=503)

        # ── Phase 2: DB writes, atomic. Either the whole cancel+recreate lands
        # together, or (on any DB-level exception) none of it does — no more
        # half-cancelled, half-rebuilt state.
        with transaction.atomic():
            cancelled = SignalHistory.objects.filter(
                category='specialist',
                status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
                generated_at__date=today
            ).update(status=SignalHistory.Status.CANCELLED)
            logger.info("[RESET] Cancelled %d today's specialist signals", cancelled)

            stale = SignalHistory.objects.filter(
                category='specialist',
                status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
                generated_at__lt=today_start
            ).update(status=SignalHistory.Status.EXPIRED)
            if stale:
                logger.info("[RESET] Expired %d stale signals from previous days", stale)

            created_count = 0
            for cand, spot, legs, i in built:
                # Guard: don't create if one already exists for today. Valid again now
                # that the cancel above ran first in this same transaction — Django
                # sees that write when this query runs.
                exists = SignalHistory.objects.filter(
                    symbol=cand['symbol'], category='specialist',
                    status__in=[SignalHistory.Status.PENDING, SignalHistory.Status.ACTIVE],
                    generated_at__gte=today_start
                ).exists()
                if exists:
                    continue
                SignalHistory.objects.create(
                    symbol=cand['symbol'], signal_type='STRANGLE',
                    entry_price=spot, target=0, stop_loss=0,
                    status=SignalHistory.Status.PENDING,
                    category='specialist',
                    metadata={'legs': legs, 'confidence': cand.get('confidence', 0), 'rank': i + 1}
                )
                created_count += 1
                logger.info("[RESET] Created signal for %s spot=%.2f legs=%d",
                            cand['symbol'], spot, len(legs))

        # Clear ALL relevant caches so the panel and scanner reflect the fresh state.
        for key in [
            "delta_hedge_panel_2s",
            "delta_hedge_panel_live_5s",
            "delta_hedge_panel_60s",
            "delta_hedge_scanner_throttle_5m",
            "stale_signal_cleanup_done",
        ]:
            cache.delete(key)

        # Set scanner throttle AFTER we've just run, so the background scanner
        # doesn't run again immediately and overwrite our fresh signals.
        cache.set("delta_hedge_scanner_throttle_5m", True, timeout=120)

        return Response({
            "status": "Strategy reset successful",
            "cancelled": cancelled,
            "created": created_count
        })


class OptionBuyingView(APIView):
    """GET /api/stocks/option-buying/ — instantly returns DB signals, same pattern as
    LiveSignalView: scanning happens only via run_periodic_scanners()/cron, a passive
    page view must never trigger a live scan. ?force=true bypasses that (same
    force_scan throttle scope as LiveSignalView) and routes through the real engine,
    which still enforces its own market-hours/time-stop/scan-rate guards."""
    permission_classes = (IsAuthenticated,)
    throttle_classes = [UserRateThrottle, AnonRateThrottle, ScopedRateThrottle]
    throttle_scope = 'force_scan'

    def get_throttles(self):
        # Same fix as LiveSignalView.get_throttles() — only cap ?force=true requests
        # at force_scan's 2/min rate, not every plain poll (see that view's comment).
        if self.request.query_params.get('force', 'false').lower() == 'true':
            return [t() for t in self.throttle_classes]
        return [t() for t in self.throttle_classes if t is not ScopedRateThrottle]

    def get(self, request):
        from stocks.services.option_buying_service import get_option_buying_signals
        try:
            force = request.query_params.get('force', 'false').lower() == 'true'
            payload = get_option_buying_signals(action="generate" if force else "update")
            return Response(payload)
        except Exception as e:
            from django.utils import timezone
            return Response({
                "signals": [],
                "timestamp": timezone.now().isoformat(),
                "error": f"System warming up or migrating: {str(e)}"
            })


class DashboardSummaryView(APIView):
    """
    GET /api/stocks/dashboard-summary/ — top-3-per-category preview for the Dashboard's
    preview cards. DB-reads only, never calls truedata_service directly, so it can be
    hit on every dashboard load without adding any Angel One REST call volume.
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        from django.core.cache import cache
        from django.utils import timezone
        from stocks.models import ShortTermSignal

        cache_key = 'dashboard_summary_20s'
        cached = cache.get(cache_key)
        if cached:
            return Response(cached)

        def _fmt_intraday(sig):
            return {
                'id': sig.id,
                'symbol': sig.symbol,
                'signal': sig.signal_type,
                'entry_price': float(sig.entry_price) if sig.entry_price else None,
                'target': float(sig.target) if sig.target else None,
                'stop_loss': float(sig.stop_loss) if sig.stop_loss else None,
                'status': sig.status,
                'generated_at': sig.generated_at.isoformat() if sig.generated_at else None,
            }

        def _fmt_shortterm(sig):
            return {
                'id': sig.id,
                'symbol': sig.symbol,
                'setup': sig.setup,
                'entry_price': float(sig.entry_price) if sig.entry_price else None,
                'target': float(sig.target) if sig.target else None,
                'stop_loss': float(sig.stop_loss) if sig.stop_loss else None,
                'status': sig.status,
                'ai_score': float(sig.ai_score) if sig.ai_score else None,
                'generated_at': sig.generated_at.isoformat() if sig.generated_at else None,
            }

        def _fmt_longterm(sig):
            return {
                'id': sig.id,
                'symbol': sig.symbol,
                'entry_price': float(sig.entry_price) if sig.entry_price else None,
                'target': float(sig.target) if sig.target else None,
                'stop_loss': float(sig.stop_loss) if sig.stop_loss else None,
                'status': sig.status,
                'reason': sig.reason,
                'generated_at': sig.generated_at.isoformat() if sig.generated_at else None,
            }

        def _fmt_option_buying(sig):
            return {
                'id': sig.id,
                'symbol': sig.symbol,
                'option_type': sig.option_type,
                'strike_price': float(sig.strike_price) if sig.strike_price else None,
                'entry_price': float(sig.entry_price) if sig.entry_price else None,
                'current_premium': float(sig.premium_cmp) if sig.premium_cmp is not None else None,
                'target': float(sig.target) if sig.target else None,
                'stop_loss': float(sig.stop_loss) if sig.stop_loss else None,
                'status': sig.status,
                'generated_at': sig.generated_at.isoformat() if sig.generated_at else None,
            }

        intraday_qs = SignalHistory.objects.filter(
            category='intraday',
            status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
        ).order_by('-generated_at')

        st_qs = ShortTermSignal.objects.exclude(
            status__in=[ShortTermSignal.Status.HIT_TARGET, ShortTermSignal.Status.HIT_SL,
                        ShortTermSignal.Status.EXPIRED, ShortTermSignal.Status.ARCHIVED]
        ).order_by('-generated_at')

        lt_qs = SignalHistory.objects.filter(
            category='long_term',
            status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
        ).order_by('-generated_at')

        ob_qs = SignalHistory.objects.filter(
            category='option_buying',
            status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
        ).order_by('-generated_at')

        from stocks.services.delta_hedge_service import get_hedge_panel_summary

        data = {
            'intraday': {
                'count': intraday_qs.count(),
                'items': [_fmt_intraday(s) for s in intraday_qs[:3]],
            },
            'short_term': {
                'count': st_qs.count(),
                'items': [_fmt_shortterm(s) for s in st_qs[:3]],
            },
            'long_term': {
                'count': lt_qs.count(),
                'items': [_fmt_longterm(s) for s in lt_qs[:3]],
            },
            'option_selling': get_hedge_panel_summary(limit=3),
            'option_buying': {
                'count': ob_qs.count(),
                'items': [_fmt_option_buying(s) for s in ob_qs[:3]],
            },
            'timestamp': timezone.now().isoformat(),
        }
        cache.set(cache_key, data, timeout=20)
        return Response(data)


class NotificationView(APIView):
    """API for fetching, marking read, and clearing notifications."""
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        from stocks.models import Notification
        from stocks.serializers import NotificationSerializer
        from django.db.models import Q

        notifications = Notification.objects.filter(Q(user=request.user) | Q(user__isnull=True)).order_by('-created_at')[:50]
        serializer = NotificationSerializer(notifications, many=True)
        unread_count = Notification.objects.filter(Q(user=request.user) | Q(user__isnull=True), is_read=False).count()

        return Response({
            "notifications": serializer.data,
            "unread_count": unread_count
        })

    def patch(self, request):
        from stocks.models import Notification
        from django.db.models import Q

        action = request.data.get('action')
        if action == 'mark_all_read':
            Notification.objects.filter(Q(user=request.user) | Q(user__isnull=True), is_read=False).update(is_read=True)
            return Response({"status": "All marked as read"})

        elif action == 'mark_read':
            notif_id = request.data.get('id')
            if notif_id:
                try:
                    notif = Notification.objects.get(id=notif_id)
                    notif.is_read = True
                    notif.save()
                    return Response({"status": "Marked as read"})
                except Notification.DoesNotExist:
                    return Response({"error": "Not found"}, status=404)

        return Response({"error": "Invalid action"}, status=400)

    def delete(self, request):
        from stocks.models import Notification
        from django.db.models import Q
        Notification.objects.filter(Q(user=request.user) | Q(user__isnull=True)).delete()
        return Response({"status": "All cleared"})


class CronScannerTriggerView(APIView):
    """
    Secure endpoint to manually trigger live signal scanners and delta hedge panel updates.
    Targeted by external cron services (e.g. cron-job.org).
    """
    permission_classes = (AllowAny,)

    def get(self, request):
        import os
        import logging
        import threading
        from django.db import close_old_connections
        from stocks.services.live_signal_service import run_periodic_scanners

        logger = logging.getLogger(__name__)

        provided_token = request.query_params.get("token") or request.headers.get("X-Cron-Token")
        action = request.query_params.get("action")
        # No hardcoded fallback (audit remediation 2026-07-28): a missing
        # CRON_SECRET_TOKEN now fails closed instead of silently accepting a fixed,
        # source-committed default. Set CRON_SECRET_TOKEN in the deployment
        # environment (e.g. Render's dashboard) for this endpoint to work.
        secret_token = os.environ.get("CRON_SECRET_TOKEN")
        if not secret_token:
            logger.error("[CRON] CRON_SECRET_TOKEN is not set — refusing all trigger requests.")
            return Response({"error": "Server misconfigured: CRON_SECRET_TOKEN not set"}, status=503)
        if provided_token != secret_token:
            return Response({"error": "Unauthorized"}, status=401)

        # CENTRALIZED WEEKEND & HOLIDAY GUARD
        from stocks.services.signal_utils import is_market_open_today

        force = request.query_params.get("force", "").lower() in ("1", "true", "yes")

        logger.info("[CRON] Received trigger request for action=%s force=%s", action, force)
        if not is_market_open_today():
            if not force:
                logger.info("[CRON] Skipping trigger for action=%s because market is closed today.", action)
                return Response({
                    "skipped": True,
                    "reason": "Market closed / NSE holiday — add &force=1 to override"
                })
            logger.info("[CRON] Market closed but force=1 — proceeding anyway.")

        # Clear scanner throttle cache so the scan always runs when manually triggered
        if action == "generate":
            from django.core.cache import cache as dj_cache
            dj_cache.delete("delta_hedge_scanner_throttle_5m")
            dj_cache.delete("delta_hedge_panel_live_5s")
            logger.info("[CRON] Cleared scanner throttle + panel cache for fresh generate run.")

        # ── Trade Engine Manual Triggers ──
        if action == "trade_scan":
            import threading
            is_relaxed = request.query_params.get("relaxed", "").lower() in ("1", "true", "yes") or force
            def _run_trade_scanner():
                from stocks.services.trade_engine import run_daily_scanner
                close_old_connections()
                run_daily_scanner(relaxed=is_relaxed)
                close_old_connections()
            thread = threading.Thread(target=_run_trade_scanner, daemon=True)
            thread.start()
            return Response({"status": "Success", "message": f"Trade Engine scanner triggered in background (relaxed={is_relaxed})"})

        if action == "trade_intraday":
            import threading
            def _run_intraday():
                from stocks.services.trade_engine import run_intraday_check
                close_old_connections()
                run_intraday_check()
                close_old_connections()
            thread = threading.Thread(target=_run_intraday, daemon=True)
            thread.start()
            return Response({"status": "Success", "message": "Trade Engine intraday check triggered"})

        if action == "trade_eod":
            import threading
            def _run_eod():
                from stocks.services.trade_engine import run_eod_evaluation
                close_old_connections()
                run_eod_evaluation()
                close_old_connections()
            thread = threading.Thread(target=_run_eod, daemon=True)
            thread.start()
            return Response({"status": "Success", "message": "Trade Engine EOD evaluation triggered"})

        # One-off manual trigger, added 2026-07-28: candle_trickle_warmer's default
        # 5-day lookback can never give a cold symbol enough ONE_DAY history to pass
        # _liquidity_stats()'s 10-bar minimum (confirmed in production — most of
        # NIFTY200 stuck at no_stats all session). This runs the existing
        # candle_store.backfill() — built for exactly this, never wired to anything
        # live before now — for the whole NIFTY200 universe in one paced batch
        # (~1.5s/call, ~5 min total for 200 symbols) instead of waiting weeks for the
        # trickle warmer's shallow window to age past 10 days on its own.
        if action == "backfill_universe":
            import threading
            from django.core.cache import cache as dj_cache
            lock_key = "backfill_universe_running"
            if not dj_cache.add(lock_key, True, timeout=1800):
                return Response({"status": "Skipped", "message": "Backfill already running"})

            def _run_backfill():
                close_old_connections()
                try:
                    from stocks.services.truedata_service import get_truedata_instance
                    from stocks.services.market_data.download_queue import get_universe_symbols
                    from stocks.services import candle_store
                    svc = get_truedata_instance()
                    if not svc:
                        logger.error("[BACKFILL_UNIVERSE] No Angel One service instance available.")
                        return
                    symbols = get_universe_symbols()
                    logger.info("[BACKFILL_UNIVERSE] Starting ONE_DAY backfill for %d symbols...", len(symbols))
                    result = candle_store.backfill(svc, symbols, interval="ONE_DAY", days=60, exchange="NSE")
                    logger.info("[BACKFILL_UNIVERSE] Completed: %s", result)
                except Exception as exc:
                    logger.exception("[BACKFILL_UNIVERSE] Failed: %s", exc)
                finally:
                    dj_cache.delete(lock_key)
                    close_old_connections()

            thread = threading.Thread(target=_run_backfill, daemon=True)
            thread.start()
            return Response({"status": "Success", "message": "NIFTY50 ONE_DAY backfill triggered in background"})

        # One-off diagnostic, added 2026-07-28: verifies option_buying_service.py's
        # NIFTY50 ∩ F&O candidate intersection (added earlier today) isn't empty or
        # near-empty due to a symbol-naming mismatch between Angel One's F&O
        # instrument master and the NSE NIFTY50 CSV — the one real bug risk in
        # today's narrowing change, never actually confirmed against live data.
        if action == "debug_option_buying_candidates":
            from stocks.services.truedata_service import get_truedata_instance
            from stocks.services.market_data.download_queue import get_universe_symbols
            from stocks.models import SignalHistory
            svc = get_truedata_instance()
            if not svc:
                return Response({"status": "Error", "message": "No Angel One instance"}, status=500)
            fo_stocks = set(svc.get_fo_stocks())
            nifty50 = set(get_universe_symbols())
            overlap = fo_stocks & nifty50
            live_symbols = set(SignalHistory.objects.filter(
                category="option_buying",
                status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
            ).values_list("symbol", flat=True))
            scan_symbols = [s for s in overlap if s not in live_symbols][:40]
            return Response({
                "status": "Success",
                "fo_stocks_count": len(fo_stocks),
                "nifty50_count": len(nifty50),
                "overlap_count": len(overlap),
                "overlap_sample": sorted(overlap)[:15],
                "nifty50_not_in_fo": sorted(nifty50 - fo_stocks)[:15],
                "final_scan_symbols_count": len(scan_symbols),
            })

        # One-off manual trigger, added 2026-07-28, converted to synchronous the same
        # day after the first (background-thread) version silently died mid-run to a
        # Render restart — confirmed via Render logs showing the HTTP access line but
        # none of this action's own log output. Reuses updater.run_candle_bars_cleanup()
        # (also registered as the daily 2:30 AM scheduled job) so the manual trigger
        # and the automated one can never drift out of sync on retention windows.
        if action == "cleanup_candle_bars":
            from stocks.updater import run_candle_bars_cleanup
            result = run_candle_bars_cleanup()
            if "error" in result:
                return Response({"status": "Error", **result}, status=500)
            return Response({"status": "Success", **result})

        # One-off manual trigger, added 2026-07-30: option_buying_service's once-per-day
        # generation cap (05e2c75) gates on ATTEMPT not success, so a day where the
        # single 10:00 AM attempt errors out on every symbol otherwise has to wait until
        # tomorrow to retry. This clears today's cache key so the next 15-min
        # run_periodic_scanners cycle re-attempts generation in the same production
        # process (avoids opening a second concurrent Angel One session).
        if action == "clear_option_buying_cap":
            from django.core.cache import cache as dj_cache
            from stocks.services.signal_utils import IST
            from datetime import datetime
            today_key = datetime.now(tz=IST).date().isoformat()
            cache_key = f"option_buying_generation_attempted_{today_key}"
            existed = dj_cache.get(cache_key, False)
            dj_cache.delete(cache_key)
            return Response({"status": "Success", "cache_key": cache_key, "was_set": bool(existed)})

        # One-off diagnostic, added 2026-07-28: verifies the account owner's manual
        # `VACUUM FULL candle_bars` (run after deleting ~1.45M FIFTEEN_MINUTE rows)
        # actually reclaimed on-disk space — a plain DELETE doesn't shrink the table,
        # only VACUUM FULL does, and Supabase's free tier caps the DB at 500 MB total.
        # Runs the account owner's own diagnostic query through Django's existing DB
        # connection (same Postgres instance) instead of requiring SQL editor access.
        if action == "db_size_check":
            from django.db import connection
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT relname AS table_name,
                               pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
                               pg_total_relation_size(relid) AS total_bytes
                        FROM pg_catalog.pg_statio_user_tables
                        ORDER BY pg_total_relation_size(relid) DESC
                        LIMIT 15
                        """
                    )
                    rows = cursor.fetchall()
                    tables = [
                        {"table_name": r[0], "total_size": r[1], "total_bytes": r[2]}
                        for r in rows
                    ]

                    database_size = None
                    try:
                        cursor.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
                        database_size = cursor.fetchone()[0]
                    except Exception:
                        logger.exception("[DB_SIZE_CHECK] Could not fetch pg_database_size.")

                return Response({
                    "status": "Success",
                    "database_size": database_size,
                    "tables": tables,
                })
            except Exception as exc:
                logger.exception("[DB_SIZE_CHECK] Failed: %s", exc)
                return Response({"status": "Error", "message": str(exc)}, status=500)

        # One-off diagnostic, added 2026-07-29: how much calendar history is actually
        # sitting in candle_bars right now, per interval — answers "how many days of
        # data do we have" directly against retention-job cutoffs (run_candle_bars_cleanup:
        # FIVE_MINUTE 4d, FIFTEEN_MINUTE deleted unconditionally, ONE_DAY 400d).
        if action == "candle_bars_range_check":
            from stocks.models import CandleBar
            from django.db.models import Min, Max, Count

            rows = (
                CandleBar.objects.values("interval")
                .annotate(oldest=Min("ts"), newest=Max("ts"), row_count=Count("id"))
                .order_by("interval")
            )
            results = []
            for r in rows:
                span_days = None
                if r["oldest"] and r["newest"]:
                    span_days = (r["newest"] - r["oldest"]).days
                results.append({
                    "interval": r["interval"],
                    "oldest": r["oldest"].isoformat() if r["oldest"] else None,
                    "newest": r["newest"].isoformat() if r["newest"] else None,
                    "span_days": span_days,
                    "row_count": r["row_count"],
                })
            return Response({"status": "Success", "intervals": results})

        # One-off diagnostic, added 2026-07-29: what time did today's first intraday/
        # option_buying signal actually get generated, per symbol, so "did the morning
        # scan fire on schedule" can be answered directly from SignalHistory.generated_at
        # instead of grepping Render logs.
        if action == "todays_signal_times_check":
            from django.utils import timezone as dj_tz
            from stocks.models import SignalHistory

            today = dj_tz.localtime().date()
            results = {}
            for category in ("intraday", "option_buying"):
                qs = (
                    SignalHistory.objects.filter(category=category, generated_at__date=today)
                    .order_by("generated_at")
                )
                rows = [
                    {
                        "symbol": s.symbol,
                        "generated_at": dj_tz.localtime(s.generated_at).strftime("%Y-%m-%d %H:%M:%S IST"),
                        "status": s.status,
                    }
                    for s in qs
                ]
                results[category] = {
                    "count": len(rows),
                    "first_generated_at": rows[0]["generated_at"] if rows else None,
                    "signals": rows,
                }
            return Response({"status": "Success", "date": today.isoformat(), "results": results})

        # One-off diagnostic, added 2026-07-29: real count of Swing V2 shadow sessions
        # actually run, against the ~20-session requirement (doc/AUDIT_REMEDIATION_PLAN.md
        # Phase 5 item A). run_swing_v2_shadow's scan persists nothing by design, so
        # SwingV2ShadowRun (added alongside this action) is the only durable record.
        if action == "swing_v2_shadow_count_check":
            from stocks.models import SwingV2ShadowRun

            runs = list(
                SwingV2ShadowRun.objects.order_by("run_date").values_list("run_date", flat=True)
            )
            REQUIRED_SESSIONS = 20
            return Response({
                "status": "Success",
                "sessions_logged": len(runs),
                "sessions_required": REQUIRED_SESSIONS,
                "sessions_remaining": max(0, REQUIRED_SESSIONS - len(runs)),
                "first_session": runs[0].isoformat() if runs else None,
                "last_session": runs[-1].isoformat() if runs else None,
                "all_dates": [d.isoformat() for d in runs],
            })

        # Ultra-lightweight keep-alive ping for Render Free Tier spin-down prevention
        if action == "ping":
            logger.info("[CRON] Ping request received — returning Pong keep-alive.")
            return Response({"status": "Success", "message": "Pong"})

        # Quick Telegram connectivity test — does not generate signals
        if action == "test_telegram":
            from stocks.services.telegram_service import send_telegram_message, is_enabled, _config
            cfg = _config()
            if not is_enabled():
                return Response({
                    "telegram_enabled": False,
                    "config": {
                        "ENABLED": cfg["ENABLED"],
                        "BOT_TOKEN_SET": bool(cfg["BOT_TOKEN"]),
                        "CHAT_ID_SET": bool(cfg["CHAT_ID"]),
                    },
                    "error": "Telegram not enabled or credentials missing"
                })
            ok = send_telegram_message("🔔 <b>TradePulse Test</b>\n\nTelegram connectivity confirmed ✅")
            return Response({"telegram_test": "success" if ok else "failed", "chat_id": cfg["CHAT_ID"]})

        # One-off diagnostic: direct test send to the dedicated intraday/option-buying
        # chat, bypassing queue_telegram_message()/process_telegram_queue() entirely, plus
        # a read-only peek at the most recent queue rows for that chat. Isolates "queue not
        # draining" from "Telegram API itself is rejecting delivery to this chat" by
        # returning the raw Telegram API response body (not just True/False).
        if action == "test_intraday_chat":
            import requests as _requests
            from stocks.services.telegram_service import _config, get_intraday_chat_id
            from stocks.models import TelegramLog

            cfg = _config()
            target_chat_id = get_intraday_chat_id()

            if cfg["ENABLED"] not in ("true", "1", "yes") or not cfg["BOT_TOKEN"]:
                telegram_api_response = {
                    "ok": False,
                    "error": "Telegram not enabled or BOT_TOKEN missing",
                    "ENABLED": cfg["ENABLED"],
                    "BOT_TOKEN_SET": bool(cfg["BOT_TOKEN"]),
                }
            else:
                try:
                    url = f"https://api.telegram.org/bot{cfg['BOT_TOKEN']}/sendMessage"
                    resp = _requests.post(url, json={
                        "chat_id": target_chat_id,
                        "text": "🔔 Direct test to intraday chat",
                        "parse_mode": "HTML",
                    }, timeout=10)
                    telegram_api_response = resp.json()
                except Exception as exc:
                    telegram_api_response = {"ok": False, "exception": str(exc)}

            recent_logs = list(
                TelegramLog.objects.filter(
                    event_type__in=["INTRADAY_NEW_SIGNAL", "OPTION_BUYING_NEW_SIGNAL"]
                ).order_by("-sent_at")[:10].values(
                    "id", "event_type", "status", "delivery_status", "retry_count", "chat_id", "sent_at"
                )
            )

            return Response({
                "target_chat_id": target_chat_id,
                "telegram_api_response": telegram_api_response,
                "recent_intraday_telegram_logs": recent_logs,
            })

        def bg_scanner(scanner_action):
            import logging
            logger = logging.getLogger(__name__)
            try:
                close_old_connections()
                logger.info("[BACKGROUND CRON] Starting live scanners with action=%s...", scanner_action)
                run_periodic_scanners(action=scanner_action)
                logger.info("[BACKGROUND CRON] Live scanners completed successfully.")
            except Exception as exc:
                logger.exception("[BACKGROUND CRON] Exception during live scanning:")
            finally:
                close_old_connections()

        try:
            from django.utils import timezone
            from stocks.updater import _scheduler
            import logging
            logger = logging.getLogger(__name__)

            if _scheduler is not None:
                # Queue a one-off background task in APScheduler's thread pool immediately
                _scheduler.add_job(
                    run_periodic_scanners,
                    trigger='date',
                    run_date=timezone.now(),
                    kwargs={"action": action},
                    id=f"manual_trigger_{action}_{timezone.now().timestamp()}"
                )
                logger.info("[CRON TRIGGER] Successfully queued manual trigger job in APScheduler.")
                msg = "Live scanners queued in APScheduler background pool."
            else:
                # Fallback to standard background thread if scheduler is not running
                thread = threading.Thread(target=bg_scanner, args=(action,))
                thread.daemon = True
                thread.start()
                logger.info("[CRON TRIGGER] Scheduler not running, fell back to raw thread.")
                msg = "Live scanners triggered in background thread."
            
            return Response({
                "status": "Success",
                "message": msg
            })
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error("[CRON TRIGGER] Failed to queue background scanner: %s", e)
            return Response({"status": "Error", "message": str(e)}, status=500)




