"""
TradePulse AI — Trade Engine
============================
Implements the full event-driven trade lifecycle:

    Market Data → Daily Scanner → AI Scoring → Trade Creation →
    Trade Lifecycle → Telegram → Dashboard → Analytics

Schedule (per stocks/updater.py — see that file for the authoritative CronTrigger config):
    9:05 AM        — Pre-market: Update Nifty/Sector trends
    11:30 AM       — Scanner: Scan NIFTY50, AI score, create Trades
                     (moved from 10:00 AM on 2026-07-28 to avoid colliding with
                     intraday's/option-buying's own first generation attempts)
    10:15 AM–3:15 PM, every 30 min — Intraday: Check entry activation (Low ≤ Entry)
                     (moved from a 10-minute cadence on 2026-07-28)
    3:25 PM        — EOD: Trail stops, check SL/Target, update P&L
    Weekend        — Cleanup: Expire old setups

All data flows through the new models:
    StockDailyData → TradeScanner → Trade → TelegramLog
"""

import logging
from datetime import datetime, timedelta, date
from decimal import Decimal
from typing import Optional
import json
import os

import pandas as pd
import numpy as np

from django.utils import timezone
from django.db import transaction, IntegrityError

from stocks.models import (
    Stock, StockDailyData, TradeScanner, TelegramLog, ShortTermSignal, SignalHistory,
)
from stocks.services import candle_store

# ── Cost / slippage model (audit remediation Phase 1 item 6) ────────────────────
# This engine holds 15-90 days on delivery/CNC — same holding-period economics as (and
# literally the same `ShortTermSignal` table as) the swing V2 engine, so it reuses that
# engine's profile rather than inventing a new one. See shared/profiles.py's SWING
# profile and swing_service.py's `check_swing_activations`/`_close_position` for
# precedent. `cost_model_for(PROFILE)` resolves to DELIVERY_COST_MODEL (STT on both
# legs, wider spread) rather than the intraday model.
from stocks.services.trading_engine.cost_model import cost_model_for
from stocks.services.shared import SWING as PROFILE

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY CONFIGURATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
DEFAULT_CONFIG = {
    "scanner": {
        "nifty_500_prefilter_pct": 0.5,
        "nifty_500_prefilter_volume": 50000,
        "top_candidates_count": 50,
        "max_telegram_alerts": 10
    },
    "indicators": {
        "ema_trend_short": 50,
        "ema_trend_long": 200,
        "ema_trailing": 20,
        "adx_period": 14,
        "rsi_period": 14,
        "atr_period": 14
    },
    "strategy": {
        "min_ai_score": 25.0,
        "min_adx_threshold": 25.0,
        "adx_relaxed_threshold": 15.0,
        "min_volume_multiplier": 1.5,
        "volume_relaxed_multiplier": 1.0,
        "near_52w_high_percent": 5.0,
        "near_52w_relaxed_percent": 10.0,
        "minimum_liquidity_floor": 100000,
        "min_risk_reward_ratio": 2.0
    },
    "trade_lifecycle": {
        "pending_expiry_trading_days": 30,
        "active_review_calendar_days": 90,
        "archived_cooldown_trading_days": 20,
        "atr_stop_loss_multiplier": 2.0,
        "max_stop_loss_floor_pct": 10.0,
        "target1_risk_reward": 2.0,
        "target2_risk_reward": 3.0,
        "target3_risk_reward": 4.0
    },
    "telegram": {
        "alerts_enabled": True,
        "async_execution": True,
        "max_retries": 3,
        "retry_delay_seconds": 10
    }
}

def load_strategy_config() -> dict:
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'strategy_config.json')
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                loaded = json.load(f)
                # Merge loaded keys over default layout to guarantee fallback keys exist
                final_config = {}
                for key, default_sub in DEFAULT_CONFIG.items():
                    loaded_sub = loaded.get(key, {})
                    final_config[key] = {**default_sub, **loaded_sub}
                return final_config
    except Exception as e:
        logger.error("[STRATEGY_CONFIG] Failed to load config, using default configuration: %s", e)
    return DEFAULT_CONFIG

STRATEGY_CONFIG = load_strategy_config()

# Long-term BUY signals are scan-only picks with no recorded quantity/capital, so an
# equal notional per position is assumed to surface an absolute ₹ P&L in the Telegram
# report alongside the percentage (agreed basis: ₹1,00,000 per position).
LONG_TERM_ASSUMED_CAPITAL_PER_POSITION = 100_000

# ═══════════════════════════════════════════════════════════════════════════════
# INDICATOR HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df['High'], df['Low'], df['Close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low = df['High'], df['Low']
    prev_high, prev_low = high.shift(1), low.shift(1)
    prev_close = df['Close'].shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    alpha = 1.0 / period
    tr_s = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_dm_s = pd.Series(plus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean()
    minus_dm_s = pd.Series(minus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean()

    plus_di = 100.0 * (plus_dm_s / tr_s.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm_s / tr_s.replace(0, np.nan))
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=alpha, adjust=False).mean().fillna(0.0)


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


# ═══════════════════════════════════════════════════════════════════════════════
# AI SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_ai_score(df: pd.DataFrame, nifty_20d_ret: float, relaxed: bool = False, symbol: str = "") -> dict | None:
    """
    Multi-factor AI scoring engine.
    Returns a dict with individual scores and the composite ai_score,
    plus trade parameters (entry, SL, targets), or None if filtered out.
    """
    cfg_ind = STRATEGY_CONFIG["indicators"]
    cfg_strat = STRATEGY_CONFIG["strategy"]
    cfg_lifecycle = STRATEGY_CONFIG["trade_lifecycle"]

    # Resolve dynamic periods
    short_ema_period = cfg_ind["ema_trend_short"]
    long_ema_period = cfg_ind["ema_trend_long"]
    adx_period = cfg_ind["adx_period"]
    rsi_period = cfg_ind["rsi_period"]
    atr_period = cfg_ind["atr_period"]

    if df.empty or len(df) < max(201, long_ema_period + 1):
        return None

    close = df['Close'].iloc[-1]
    ema20 = _ema(df['Close'], cfg_ind["ema_trailing"]).iloc[-1]
    ema50 = _ema(df['Close'], short_ema_period).iloc[-1]
    ema200 = _ema(df['Close'], long_ema_period).iloc[-1]

    # ── Filter 1: Trend (Price > 50 EMA > 200 EMA) ──
    if not (close > ema50 > ema200):
        return None

    # ── Filter 2: ADX ──
    adx_val = _adx(df, period=adx_period).iloc[-1]
    adx_limit = cfg_strat["adx_relaxed_threshold"] if relaxed else cfg_strat["min_adx_threshold"]
    if adx_val < adx_limit:
        return None

    # ── Filter 3: Relative Strength > Nifty (Bypassed if relaxed) ──
    stock_20d_ret = 0.0
    if len(df) >= 20:
        stock_20d_ret = (df['Close'].iloc[-1] - df['Close'].iloc[-20]) / df['Close'].iloc[-20]
    if not relaxed and stock_20d_ret < nifty_20d_ret:
        return None

    # ── Filter 4: 52-week High proximity OR 20-day Breakout ──
    high_52w = df['High'].rolling(252, min_periods=100).max().iloc[-1]
    high_20d = df['High'].rolling(20).max().iloc[-2]  # previous 20-day high
    prox_pct = cfg_strat["near_52w_relaxed_percent"] if relaxed else cfg_strat["near_52w_high_percent"]
    prox_limit = (100.0 - prox_pct) / 100.0
    is_near_52w = close >= high_52w * prox_limit
    is_20d_break = close >= high_20d
    if not (is_near_52w or is_20d_break):
        return None

    # ── Filter 5: Volume ──
    vol_5d = df['Volume'].iloc[-5:].mean()
    vol_20d = df['Volume'].iloc[-20:].mean()
    vol_multiplier = cfg_strat["volume_relaxed_multiplier"] if relaxed else cfg_strat["min_volume_multiplier"]
    if vol_20d == 0 or vol_5d < vol_20d * vol_multiplier:
        return None

    # ── Filter 6: Liquidity Floor ──
    if df['Volume'].iloc[-1] < cfg_strat["minimum_liquidity_floor"]:
        return None

    # ── Calculate Trade Parameters ──
    entry_price = close
    atr_val = _atr(df, period=atr_period).iloc[-1]
    rsi_val = _rsi(df['Close'], period=rsi_period).iloc[-1]

    stop_loss = entry_price - (float(cfg_lifecycle["atr_stop_loss_multiplier"]) * float(atr_val))
    max_sl_floor = entry_price * ((100.0 - float(cfg_lifecycle["max_stop_loss_floor_pct"])) / 100.0)
    if stop_loss < max_sl_floor:
        stop_loss = max_sl_floor

    sl_points = entry_price - stop_loss
    if sl_points <= 0:
        return None

    target1 = entry_price + (cfg_lifecycle["target1_risk_reward"] * sl_points)
    target2 = entry_price + (cfg_lifecycle["target2_risk_reward"] * sl_points)
    target3 = entry_price + (cfg_lifecycle["target3_risk_reward"] * sl_points)

    rr_ratio = (target1 - entry_price) / sl_points
    if rr_ratio < cfg_strat["min_risk_reward_ratio"]:
        return None

    # ── Cost-viability gate (audit remediation Phase 1 item 6) ──────────────────
    # Arithmetic, not prediction — same rule intraday enforces at 3x round-trip cost
    # and swing enforces at `profile.min_target_cost_multiple` (6x for delivery). This
    # engine has no qty/notional model (no position sizing exists anywhere in it), so
    # cost is priced at the shared cost model's reference notional rather than a
    # fabricated position size — same fallback swing_signals.py uses when per-trade
    # liquidity stats aren't available.
    cost_pct = cost_model_for(PROFILE).round_trip_pct(symbol=symbol, price=entry_price)
    required_target_pct = cost_pct * PROFILE.min_target_cost_multiple
    target1_pct = (target1 - entry_price) / entry_price * 100.0
    if target1_pct < required_target_pct:
        logger.debug(
            "[TRADE_ENGINE][COST_GATE] %s rejected: target1 %.3f%% < required %.3f%% (cost %.4f%%)",
            symbol, target1_pct, required_target_pct, cost_pct,
        )
        return None

    # ── AI Score Calculation (0-100 scale) ──
    # Trend Score (0-25): How far above 200 EMA and EMA alignment
    ema_spread = ((close - ema200) / ema200) * 100
    trend_score = min(25.0, max(0.0, ema_spread * 1.5))

    # Momentum Score (0-25): ADX strength + RSI position
    adx_contribution = min(12.5, (adx_val - adx_limit) * 0.5) if adx_val > adx_limit else 0.0
    rsi_contribution = min(12.5, max(0.0, (rsi_val - 40) * 0.25))
    momentum_score = adx_contribution + rsi_contribution

    # Volume Score (0-20): Volume explosion intensity
    vol_ratio = vol_5d / vol_20d if vol_20d > 0 else 0.0
    volume_score = min(20.0, max(0.0, (vol_ratio - 1.0) * 10.0))

    # Sector/RS Score (0-15): Relative outperformance
    rs_spread = (stock_20d_ret - nifty_20d_ret) * 100
    sector_score = min(15.0, max(0.0, rs_spread * 3.0))

    # Risk Score (0-15): Proximity to 52-week high + tight SL
    pct_from_52w = ((high_52w - close) / high_52w) * 100
    risk_score = min(15.0, max(0.0, 15.0 - pct_from_52w * 3.0))

    # Fundamental Score: placeholder (would need external fundamental data)
    fundamental_score = 10.0  # Default neutral

    ai_score = trend_score + momentum_score + volume_score + sector_score + risk_score

    # Check minimum AI Score threshold
    if ai_score < cfg_strat["min_ai_score"]:
        return None

    # Estimate holding days based on ATR (higher ATR = faster move)
    atr_pct = (atr_val / close) * 100 if close > 0 else 2.0
    holding_days = max(15, min(90, int(60 / max(atr_pct, 0.5))))

    return {
        'ai_score': round(ai_score, 2),
        'trend_score': round(trend_score, 2),
        'momentum_score': round(momentum_score, 2),
        'volume_score': round(volume_score, 2),
        'sector_score': round(sector_score, 2),
        'fundamental_score': round(fundamental_score, 2),
        'risk_score': round(risk_score, 2),
        'entry_price': round(entry_price, 2),
        'stop_loss': round(stop_loss, 2),
        'target1': round(target1, 2),
        'target2': round(target2, 2),
        'target3': round(target3, 2),
        'holding_days': holding_days,
        'expiry_days': holding_days + 10,
        'vol_ratio': round(vol_ratio, 2),
        'adx': round(adx_val, 1),
        'rsi': round(rsi_val, 1),
        'ema20': round(ema20, 2),
        'ema50': round(ema50, 2),
        'ema200': round(ema200, 2),
        'atr': round(atr_val, 2),
        'high_52w': round(high_52w, 2),
        'setup': '20d Breakout' if is_20d_break else '52w High Breakout',
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 1. PRE-MARKET UPDATE (9:05 AM)
# ═══════════════════════════════════════════════════════════════════════════════

def run_premarket_update():
    """
    9:05 AM: Fetch Nifty 50 trend (close vs 20/50 EMA) and store result.
    Returns market direction dict.
    """
    from stocks.services.pro_system_service import get_market_direction
    direction = get_market_direction()
    logger.info("[TRADE_ENGINE] Pre-market update: Nifty trend = %s", direction.get('trend'))
    return direction


# ═══════════════════════════════════════════════════════════════════════════════
# 2. DAILY SCANNER (10:00 AM)
# ═══════════════════════════════════════════════════════════════════════════════

def run_daily_scanner(relaxed: bool = False):
    """
    10:00 AM daily scanner. Guards against overlapping runs — two scans firing at once
    (e.g. two manual cron-trigger hits close together) share the same TrueDataService
    singleton and its one requests.Session, and running it concurrently from two threads
    has been observed to corrupt the shared TLS connection (SSL "decryption failed or bad
    record mac" errors) and starve the second scan's own quote fetch (0/0 candidates).
    """
    from django.core.cache import cache
    lock_key = "trade_engine_scanner_running"
    if not cache.add(lock_key, True, timeout=600):
        logger.warning("[TRADE_ENGINE] Scanner already running (overlapping trigger) — skipping this run")
        return []
    try:
        if relaxed:
            return _run_daily_scanner_impl(relaxed=True)

        # Strict pass first, telegram held back — if it finds nothing (including the
        # BEARISH skip), fall back to the existing relaxed thresholds so the report
        # doesn't come up empty every day. Relaxed pass sends whatever it is asked to send.
        result = _run_daily_scanner_impl(relaxed=False, send_telegram=False)
        if result:
            return result
        logger.info("[TRADE_ENGINE] Strict scan found no setups — falling back to relaxed mode")
        return _run_daily_scanner_impl(relaxed=True)
    finally:
        cache.delete(lock_key)


def _run_daily_scanner_impl(relaxed: bool = False, send_telegram: bool = True):
    """
    1. Check market direction — skip if BEARISH
    2. Fetch NIFTY50 universe
    3. Pre-filter via bulk quotes (gainers with volume)
    4. Run AI scoring on top candidates
    5. Create Trade records as PENDING for qualifying setups
    6. Send Top N configured Telegram alerts
    """
    from stocks.services.truedata_service import get_truedata_instance
    from stocks.services.pro_system_service import (
        get_market_direction, NIFTY50_FALLBACK,
    )
    from stocks.services.bhavcopy_service import _fetch_nifty500_symbols, _get_session
    from stocks.models import TradeHistory

    logger.info("[TRADE_ENGINE] ═══ Starting Daily Scanner (Relaxed=%s) ═══", relaxed)

    # ── Step 1: Market Direction ──
    direction = get_market_direction()
    if direction.get('trend') == 'BEARISH' and not relaxed:
        logger.info("[TRADE_ENGINE] Market BEARISH — skipping fresh scanner")
        if send_telegram:
            _send_telegram_scanner_summary([], direction)
        return []

    # ── Step 2: Get Angel One Instance ──
    svc = get_truedata_instance()
    if not svc:
        logger.error("[TRADE_ENGINE] Angel One instance not available")
        return []

    # ── Step 3: Fetch NIFTY50 universe ──
    try:
        session = _get_session()
        symbols = list(_fetch_nifty500_symbols(session))
    except Exception:
        symbols = []
    if not symbols:
        symbols = NIFTY50_FALLBACK
        logger.warning("[TRADE_ENGINE] Using fallback symbol list (%d)", len(symbols))

    # ── Step 4: Resolve tokens & pre-filter ──
    token_map = svc.get_token_map(symbols, exchange="NSE")
    if not token_map:
        logger.error("[TRADE_ENGINE] Token map empty")
        return []

    tokens = list(token_map.values())
    quotes = {}
    for i in range(0, len(tokens), 50):
        try:
            chunk_q = svc.get_bulk_quotes({"NSE": tokens[i:i+50]}, mode="FULL")
            quotes.update(chunk_q)
        except Exception as e:
            logger.warning("[TRADE_ENGINE] Quote chunk failed: %s", e)

    candidates = []
    for sym, tok in token_map.items():
        q = quotes.get(f"NSE:{tok}")
        if not q:
            continue
        change_pct = q.get("change_percent", 0)
        vol = q.get("trade_volume", 0)
        min_change = -2.0 if relaxed else 0.5
        min_vol = 10000 if relaxed else 50000
        if change_pct > min_change and vol > min_vol:
            candidates.append({
                "symbol": sym, "token": tok,
                "change_pct": change_pct, "volume": vol,
                "ltp": q.get("ltp", 0),
            })

    candidates.sort(key=lambda x: x["change_pct"], reverse=True)
    top_candidates = candidates[:50]
    logger.info("[TRADE_ENGINE] Pre-filtered %d / %d candidates", len(top_candidates), len(candidates))

    # ── Step 5: Fetch Nifty index baseline ──
    nifty_df = candle_store.get_candles(svc, "NIFTY50_INDEX", "NIFTY 50", "ONE_DAY", lookback_days=365, exchange="NSE")
    nifty_20d_ret = 0.0
    if not nifty_df.empty and len(nifty_df) > 20:
        nifty_20d_ret = (nifty_df['Close'].iloc[-1] - nifty_df['Close'].iloc[-20]) / nifty_df['Close'].iloc[-20]

    # ── Step 6: AI Scoring on each candidate ──
    scored_results = []
    for cand in top_candidates:
        try:
            df = candle_store.get_candles(svc, cand["symbol"], cand["token"], "ONE_DAY", lookback_days=365, exchange="NSE")
            if df.empty or len(df) < 100:
                continue
            result = _compute_ai_score(df, nifty_20d_ret, relaxed=relaxed, symbol=cand["symbol"])
            if result:
                result['symbol'] = cand['symbol']
                result['current_ltp'] = cand['ltp']
                scored_results.append(result)
        except Exception as e:
            logger.warning("[TRADE_ENGINE] AI score failed for %s: %s", cand['symbol'], e)

    # Sort by AI score descending, keep all matching results
    scored_results.sort(key=lambda x: x['ai_score'], reverse=True)
    top_picks = scored_results
    logger.info("[TRADE_ENGINE] AI scored %d results, matching picks: %s",
                len(scored_results), [r['symbol'] for r in top_picks])

    # ── Step 7: Create PENDING Trade records ──
    new_trades = []
    for pick in top_picks:
        try:
            # 1. Check duplicate matching lifecycle (PENDING, ACTIVE, TARGET1, TARGET2, REVIEW_REQUIRED)
            existing = ShortTermSignal.objects.filter(
                symbol=pick['symbol'],
                status__in=[
                    ShortTermSignal.Status.PENDING,
                    ShortTermSignal.Status.ACTIVE,
                    ShortTermSignal.Status.TARGET1,
                    ShortTermSignal.Status.TARGET2,
                    ShortTermSignal.Status.REVIEW_REQUIRED,
                ]
            ).exists()
            if existing:
                logger.info("[TRADE_ENGINE] Skipping %s — already tracked in active lifecycle", pick['symbol'])
                continue

            # 2. Check cooldown lock
            cooldown_active = ShortTermSignal.objects.filter(
                symbol=pick['symbol'],
                status=ShortTermSignal.Status.ARCHIVED,
                cooldown_until__gt=timezone.now()
            ).exists()
            if cooldown_active:
                logger.info("[TRADE_ENGINE] Skipping %s — cooldown lock active", pick['symbol'])
                continue

            try:
                with transaction.atomic():
                    sig = ShortTermSignal.objects.create(
                        symbol=pick['symbol'],
                        entry_price=Decimal(str(pick['entry_price'])),
                        stop_loss=Decimal(str(pick['stop_loss'])),
                        target=Decimal(str(pick['target1'])),
                        target2=Decimal(str(pick['target2'])),
                        target3=Decimal(str(pick['target3'])),
                        current_price=Decimal(str(pick['current_ltp'])),
                        vol_ratio=Decimal(str(pick['vol_ratio'])),
                        setup=pick['setup'],
                        status=ShortTermSignal.Status.PENDING,
                        expected_holding_days=pick['holding_days'],
                        ai_score=Decimal(str(pick['ai_score'])),
                    )

                    # Add creation event to TradeHistory audit log
                    TradeHistory.objects.create(
                        trade=sig,
                        old_status='NONE',
                        new_status=ShortTermSignal.Status.PENDING,
                        price=Decimal(str(pick['entry_price'])),
                        reason='Scanner Discovery',
                        triggered_by='SYSTEM'
                    )

                    new_trades.append({**pick, 'signal_id': sig.id})
                    logger.info("[TRADE_ENGINE] Created PENDING opportunity for %s (AI: %.1f)",
                                pick['symbol'], pick['ai_score'])
            except IntegrityError:
                # The .exists() pre-check above (step 1) is not locked, so two
                # overlapping scanner runs can both pass it before either inserts.
                # unique_live_short_term_signal (partial unique index, models.py)
                # is the real dedup guarantee; this is the race it's meant to
                # catch. Same "already tracked" outcome as the pre-check branch,
                # just caught one step later. Mirrors persist_live_signal_history's
                # IntegrityError handling for SignalHistory in
                # trading_engine/state_engine.py.
                logger.info(
                    "[TRADE_ENGINE] Skipping %s — concurrent insert already created a live signal (unique constraint)",
                    pick['symbol'],
                )
        except Exception as e:
            logger.error("[TRADE_ENGINE] Failed to create signal for %s: %s", pick['symbol'], e)

    # ── Step 8: Telegram Alert — always sent (even 0 new picks) so ACTIVE HOLDINGS /
    # long-term sections show up daily regardless of whether new setups were found ──
    top_n = STRATEGY_CONFIG["scanner"]["max_telegram_alerts"]
    top_alerts = new_trades[:top_n]
    if send_telegram:
        _send_telegram_scanner_summary(top_alerts, direction)

    logger.info("[TRADE_ENGINE] ═══ Scanner Complete: %d new pending trades created ═══", len(new_trades))
    return new_trades


# ═══════════════════════════════════════════════════════════════════════════════
# 3. INTRADAY CHECKER (Every 10 minutes)
# ═══════════════════════════════════════════════════════════════════════════════

def run_intraday_check():
    """
    Every 10 min during market hours (10:10 AM – 3:15 PM):
    - Update current price for all ACTIVE/PENDING signals
    - Check if any ACTIVE signal hit SL or Target intraday
    - Real-time trailing stop check on 20 EMA
    """
    from stocks.services.truedata_service import get_truedata_instance

    svc = get_truedata_instance()
    if not svc:
        return

    active_signals = list(ShortTermSignal.objects.filter(
        status__in=[ShortTermSignal.Status.ACTIVE, ShortTermSignal.Status.PENDING]
    ))

    if not active_signals:
        logger.info("[TRADE_ENGINE] No active/pending signals for intraday check")
        return

    symbols = [s.symbol for s in active_signals]
    try:
        token_map = svc.get_token_map(symbols, exchange="NSE")
    except Exception as e:
        logger.error("[TRADE_ENGINE] Token map fetch failed: %s", e)
        return

    if not token_map:
        return

    tokens = list(token_map.values())
    quotes = {}
    for i in range(0, len(tokens), 50):
        try:
            chunk_q = svc.get_bulk_quotes({"NSE": tokens[i:i+50]}, mode="FULL")
            quotes.update(chunk_q)
        except Exception as e:
            logger.warning("[TRADE_ENGINE] Intraday quote chunk failed: %s", e)

    for sig in active_signals:
        tok = token_map.get(sig.symbol)
        if not tok:
            continue
        q = quotes.get(f"NSE:{tok}")
        if not q:
            continue

        ltp = float(q.get("ltp", 0))
        high = float(q.get("high", ltp))
        low = float(q.get("low", ltp))

        if ltp <= 0:
            continue

        sig.current_price = Decimal(str(ltp))

        if sig.status == ShortTermSignal.Status.ACTIVE:
            # Check stop loss hit first — matches run_eod_evaluation's SL-first
            # ordering, so a bar where both SL and target are touched resolves as a
            # loss, not a win (AUDIT_REMEDIATION_PLAN.md item 2.4).
            if low <= float(sig.stop_loss):
                _exit_signal(sig, float(sig.stop_loss), "HIT_SL", "Stop loss hit intraday")
                continue

            # Check target hit
            if high >= float(sig.target):
                _exit_signal(sig, float(sig.target), "HIT_TARGET", "Target hit intraday")
                continue

        sig.save(update_fields=['current_price'])


# ═══════════════════════════════════════════════════════════════════════════════
# 4. EOD EVALUATOR (3:25 PM)
# ═══════════════════════════════════════════════════════════════════════════════

def run_eod_evaluation():
    """
    3:25 PM End-of-Day evaluation:
    - Update current prices to closing values
    - Check target milestones, SL levels, and closing 20 EMA exits
    - Track highest profit levels and max drawdowns
    - Log transition audits to TradeHistory
    """
    from stocks.services.truedata_service import get_truedata_instance
    from stocks.models import TradeHistory

    logger.info("[TRADE_ENGINE] ═══ Starting EOD Evaluation ═══")

    svc = get_truedata_instance()
    if not svc:
        logger.error("[TRADE_ENGINE] Angel One not available for EOD")
        return

    # Check active/tracking signals (ACTIVE, TARGET1, TARGET2)
    active_signals = list(ShortTermSignal.objects.filter(
        status__in=[
            ShortTermSignal.Status.ACTIVE,
            ShortTermSignal.Status.TARGET1,
            ShortTermSignal.Status.TARGET2,
        ]
    ))

    if not active_signals:
        logger.info("[TRADE_ENGINE] No active holdings for EOD evaluation")
        return

    symbols = [s.symbol for s in active_signals]
    token_map = svc.get_token_map(symbols, exchange="NSE")
    if not token_map:
        logger.warning("[TRADE_ENGINE] Token map empty for EOD symbols")
        return

    for sig in active_signals:
        tok = token_map.get(sig.symbol)
        if not tok:
            continue

        try:
            # Fetch daily candles
            df = candle_store.get_candles(svc, sig.symbol, tok, "ONE_DAY", lookback_days=120, exchange="NSE")
            if df.empty or len(df) < 20:
                continue

            latest_close = float(df['Close'].iloc[-1])
            latest_high = float(df['High'].iloc[-1])
            latest_low = float(df['Low'].iloc[-1])

            sig.current_price = Decimal(str(latest_close))

            # Update holding days
            if sig.activated_at:
                sig.holding_days = (timezone.now().date() - sig.activated_at.date()).days
            else:
                sig.holding_days = getattr(sig, 'holding_days', 0) + 1

            # Update Peak metrics (compared to entry price)
            entry_val = float(sig.entry_price)
            if entry_val > 0:
                # Highest realized/unrealized profit peak (%)
                current_high_pnl = ((latest_high - entry_val) / entry_val) * 100.0
                if current_high_pnl > float(sig.highest_profit):
                    sig.highest_profit = Decimal(str(round(current_high_pnl, 2)))

                # Maximum drawdown experienced (%)
                current_drawdown = ((latest_low - entry_val) / entry_val) * 100.0
                if current_drawdown < float(sig.max_drawdown):
                    sig.max_drawdown = Decimal(str(round(current_drawdown, 2)))

                # Current closing P&L (%)
                sig.pnl_pct = Decimal(str(round(((latest_close - entry_val) / entry_val) * 100, 2)))

            # Resolve strategy targets
            t3_val = float(sig.target3) if sig.target3 else float(sig.target) * 2.0
            t2_val = float(sig.target2) if sig.target2 else float(sig.target) * 1.5
            t1_val = float(sig.target)
            sl_val = float(sig.stop_loss)

            # ── Check 1: Hard Stop Loss Hit (Low <= SL) ──
            if latest_low <= sl_val:
                _exit_signal(sig, sl_val, ShortTermSignal.Status.HIT_SL, "Stop Loss Triggered", "EOD low crossed Stop Loss floor")
                continue

            # ── Check 2: Target 3 Hit (Final target achieved) ──
            # Intentionally checked before the trailing-EMA close check below: on a
            # spike-and-fade day the daily high can clear target3 while the close still
            # finishes under the 20 EMA. Checking the close-based trailing exit first
            # would book the worse TRAILING_EXIT price even though target3 was actually
            # touched intraday and is the better, already-available fill.
            if latest_high >= t3_val:
                _exit_signal(sig, t3_val, ShortTermSignal.Status.HIT_TARGET, "Final Target Hit", f"Target 3 reached (₹{t3_val:.2f})")
                continue

            # ── Check 3: Closing Trailing Stop (Close < 20 EMA) ──
            ema20_val = float(_ema(df['Close'], 20).iloc[-1])
            if latest_close < ema20_val:
                _exit_signal(sig, latest_close, ShortTermSignal.Status.TRAILING_EXIT, "Trailing Exit", f"EOD close ₹{latest_close:.2f} below daily 20 EMA (₹{ema20_val:.2f})")
                continue

            # ── Check 3.5: Time-Stop (estimated holding timeframe elapsed, no target/SL hit) ──
            if sig.expected_holding_days and sig.activated_at:
                elapsed_days = (timezone.now().date() - sig.activated_at.date()).days
                if elapsed_days >= sig.expected_holding_days:
                    _exit_signal(
                        sig, latest_close, ShortTermSignal.Status.TIME_STOP, "Time-Stop Exit",
                        f"Held {elapsed_days}d >= estimated {sig.expected_holding_days}d timeframe without hitting target/SL — closed at market (₹{latest_close:.2f})"
                    )
                    continue

            # ── Check 4: Target 2 Hit (Trail Stop Loss) ──
            if latest_high >= t2_val and sig.status in [ShortTermSignal.Status.ACTIVE, ShortTermSignal.Status.TARGET1]:
                old_status = sig.status
                sig.status = ShortTermSignal.Status.TARGET2
                # Trail Stop Loss to Entry Price
                sig.stop_loss = sig.entry_price
                sig.save()
                
                # Audit log
                TradeHistory.objects.create(
                    trade=sig,
                    old_status=old_status,
                    new_status=ShortTermSignal.Status.TARGET2,
                    price=Decimal(str(latest_close)),
                    reason=f"Target 2 reached (₹{t2_val:.2f}), stop loss trailed to entry ₹{sig.entry_price}",
                    triggered_by='SYSTEM'
                )
                
                _send_telegram_event_alert(
                    sig, 
                    "TARGET2_HIT", 
                    f"🎯 <b>TARGET 2 HIT</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Stock: <b>{sig.symbol}</b>\n"
                    f"▸ Target 2: ₹{t2_val:.2f}\n"
                    f"▸ Close: ₹{latest_close:.2f}\n"
                    f"▸ Action: Stop loss trailed to Entry (₹{sig.entry_price})\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )

            # ── Check 5: Target 1 Hit (Lock Stop Loss to Entry) ──
            elif latest_high >= t1_val and sig.status == ShortTermSignal.Status.ACTIVE:
                old_status = sig.status
                sig.status = ShortTermSignal.Status.TARGET1
                # Lock Stop Loss to Entry Price
                sig.stop_loss = sig.entry_price
                sig.save()

                # Audit log
                TradeHistory.objects.create(
                    trade=sig,
                    old_status=old_status,
                    new_status=ShortTermSignal.Status.TARGET1,
                    price=Decimal(str(latest_close)),
                    reason=f"Target 1 reached (₹{t1_val:.2f}), stop loss locked to entry ₹{sig.entry_price}",
                    triggered_by='SYSTEM'
                )

                _send_telegram_event_alert(
                    sig, 
                    "TARGET1_HIT", 
                    f"🎯 <b>TARGET 1 HIT</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Stock: <b>{sig.symbol}</b>\n"
                    f"▸ Target 1: ₹{t1_val:.2f}\n"
                    f"▸ Close: ₹{latest_close:.2f}\n"
                    f"▸ Action: Stop loss locked to Entry (₹{sig.entry_price}) to secure break-even\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )

            # If no transitions, simply save updated closing price / P&L
            sig.save()

        except Exception as e:
            logger.error("[TRADE_ENGINE] EOD eval failed for %s: %s", sig.symbol, e)

    # ── Send EOD Portfolio Status Alert ──
    _send_telegram_eod_status()

    logger.info("[TRADE_ENGINE] ═══ EOD Evaluation Complete ═══")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. EXPIRY CLEANUP
# ═══════════════════════════════════════════════════════════════════════════════

def run_expiry_cleanup():
    """
    Weekly Expiries & Review Handler:
    - Expire stale PENDING setups (>30 trading days / 42 calendar days)
    - Transition long-running holdings (>90 calendar days) to REVIEW_REQUIRED
    - Log audit entries to TradeHistory and notify Telegram
    """
    from stocks.models import TradeHistory, TelegramLog
    from stocks.services.telegram_service import queue_telegram_message, get_short_term_chat_id
    
    logger.info("[TRADE_ENGINE] ═══ Starting Expiries & Review Handler ═══")

    # ── Step 1: Expire Pending Setups ──
    pending_days = STRATEGY_CONFIG["trade_lifecycle"]["pending_expiry_trading_days"]
    calendar_pending_limit = int(pending_days * 7 / 5)
    pending_cutoff = timezone.now() - timedelta(days=calendar_pending_limit)

    expired_pending = list(ShortTermSignal.objects.filter(
        status=ShortTermSignal.Status.PENDING,
        generated_at__lt=pending_cutoff,
    ))

    for sig in expired_pending:
        try:
            with transaction.atomic():
                db_trade = ShortTermSignal.objects.select_for_update().get(id=sig.id)
                if db_trade.status != ShortTermSignal.Status.PENDING:
                    continue

                db_trade.status = ShortTermSignal.Status.EXPIRED
                db_trade.save(update_fields=['status'])

                # Log history
                TradeHistory.objects.create(
                    trade=db_trade,
                    old_status=ShortTermSignal.Status.PENDING,
                    new_status=ShortTermSignal.Status.EXPIRED,
                    price=db_trade.entry_price,
                    reason=f"Pending setup expired after {pending_days} trading days ({calendar_pending_limit} calendar days)",
                    triggered_by='SYSTEM'
                )

                # Send Telegram alert
                chat_id = get_short_term_chat_id()
                msg = (
                    f"⏰ <b>TRADE SETUP EXPIRED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Stock: <b>{db_trade.symbol}</b>\n"
                    f"▸ Entry: ₹{db_trade.entry_price}\n"
                    f"▸ Stop Loss: ₹{db_trade.stop_loss}\n"
                    f"▸ Target 1: ₹{db_trade.target}\n"
                    f"▸ Status: EXPIRED\n"
                    f"▸ Reason: Pending setup expired after {pending_days} trading days\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )
                
                already_logged = TelegramLog.objects.filter(
                    short_term_signal=db_trade,
                    event_type='SETUP_EXPIRED'
                ).exists()
                if not already_logged:
                    TelegramLog.objects.create(
                        short_term_signal=db_trade,
                        type='EXPIRED',
                        event_type='SETUP_EXPIRED',
                        message=msg,
                        status='PENDING',
                        delivery_status='PENDING',
                        retry_count=0
                    )
                    queue_telegram_message(msg, chat_id=chat_id, short_term_signal=db_trade, event_type='SETUP_EXPIRED_QUEUED')

                logger.info("[TRADE_ENGINE] Setup %s expired successfully", db_trade.symbol)
        except Exception as e:
            logger.error("[TRADE_ENGINE] Failed to expire setup %s: %s", sig.symbol, e)

    # ── Step 2: Flag Active Review Handlers ──
    review_days = STRATEGY_CONFIG["trade_lifecycle"]["active_review_calendar_days"]
    active_cutoff = timezone.now() - timedelta(days=review_days)

    stale_active = list(ShortTermSignal.objects.filter(
        status__in=[
            ShortTermSignal.Status.ACTIVE,
            ShortTermSignal.Status.TARGET1,
            ShortTermSignal.Status.TARGET2,
        ],
        activated_at__lt=active_cutoff
    ))

    for sig in stale_active:
        try:
            with transaction.atomic():
                db_trade = ShortTermSignal.objects.select_for_update().get(id=sig.id)
                if db_trade.status not in [
                    ShortTermSignal.Status.ACTIVE,
                    ShortTermSignal.Status.TARGET1,
                    ShortTermSignal.Status.TARGET2,
                ]:
                    continue

                old_status = db_trade.status
                db_trade.status = ShortTermSignal.Status.REVIEW_REQUIRED
                db_trade.review_required = True
                db_trade.save(update_fields=['status', 'review_required'])

                # Log history
                TradeHistory.objects.create(
                    trade=db_trade,
                    old_status=old_status,
                    new_status=ShortTermSignal.Status.REVIEW_REQUIRED,
                    price=db_trade.current_price or db_trade.entry_price,
                    reason=f"Active holding period exceeded {review_days} calendar days limit",
                    triggered_by='SYSTEM'
                )

                # Send Telegram alert
                chat_id = get_short_term_chat_id()
                msg = (
                    f"🔔 <b>REVIEW REQUIRED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Stock: <b>{db_trade.symbol}</b>\n"
                    f"▸ Entry: ₹{db_trade.entry_price}\n"
                    f"▸ Current Close: ₹{db_trade.current_price or db_trade.entry_price}\n"
                    f"▸ P&L: {db_trade.pnl_pct or 0.0:+.2f}%\n"
                    f"▸ Status: REVIEW_REQUIRED\n"
                    f"▸ Reason: Active trade held for over {review_days} calendar days\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )

                already_logged = TelegramLog.objects.filter(
                    short_term_signal=db_trade,
                    event_type='REVIEW_REQUIRED'
                ).exists()
                if not already_logged:
                    TelegramLog.objects.create(
                        short_term_signal=db_trade,
                        type='REVIEW_REQUIRED',
                        event_type='REVIEW_REQUIRED',
                        message=msg,
                        status='PENDING',
                        delivery_status='PENDING',
                        retry_count=0
                    )
                    queue_telegram_message(msg, chat_id=chat_id, short_term_signal=db_trade, event_type='REVIEW_REQUIRED_QUEUED')

                logger.info("[TRADE_ENGINE] Trade %s flagged for review", db_trade.symbol)
        except Exception as e:
            logger.error("[TRADE_ENGINE] Failed to review trade %s: %s", sig.symbol, e)

    logger.info("[TRADE_ENGINE] ═══ Expiries & Review Handler Complete ═══")


def _send_telegram_event_alert(trade: ShortTermSignal, event_type: str, message: str):
    """Queue a Telegram alert via TelegramLog to prevent double-sends and blocking."""
    from stocks.models import TelegramLog
    try:
        from stocks.services.telegram_service import queue_telegram_message
        
        # Check if already queued
        already_queued = TelegramLog.objects.filter(
            short_term_signal=trade,
            event_type=event_type
        ).exists()
        if already_queued:
            return

        # Write PENDING record — the 1-min queue dispatcher will deliver it
        TelegramLog.objects.create(
            short_term_signal=trade,
            type='EVENT',
            event_type=event_type,
            message=message,
            status='PENDING',
            delivery_status='PENDING',
            retry_count=0
        )
        # Also call queue_telegram_message for idempotency guard
        queue_telegram_message(message, short_term_signal=trade, event_type=f"{event_type}_QUEUED")
    except Exception as e:
        logger.error("[TRADE_ENGINE] Event Telegram queue failed for %s (%s): %s", trade.symbol, event_type, e)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER: Exit a signal
# ═══════════════════════════════════════════════════════════════════════════════

def _exit_signal(sig: ShortTermSignal, exit_price: float, status: str, exit_reason: str, details: str):
    """Transition a signal to its exit state, archive it with cooldown, log history, and alert Telegram."""
    from stocks.models import TradeHistory, TelegramLog
    from stocks.services.telegram_service import queue_telegram_message, get_short_term_chat_id
    import pytz

    entry_f = float(sig.entry_price)

    # ── Cost / slippage model (audit remediation Phase 1 item 6) ────────────────
    # A live exit is never obtained at the raw trigger/bar price: closing a long
    # crosses the bid (same `slipped_fill` treatment used on the entry side, mirrored
    # for a sell), and the round trip carries real STT/brokerage/stamp-duty/DP-charge
    # friction that `entry_f` alone doesn't capture. Same shared model intraday/swing
    # price signals with — see trading_engine/cost_model.py.
    cm = cost_model_for(PROFILE)
    raw_exit_price = exit_price
    exit_price = round(cm.slipped_fill(raw_exit_price, is_buy=False, symbol=sig.symbol), 2)
    cost_pct = cm.round_trip_pct(symbol=sig.symbol, price=exit_price)

    gross_pnl_pct = ((exit_price - entry_f) / entry_f) * 100 if entry_f > 0 else 0.0
    pnl_pct = gross_pnl_pct - cost_pct

    # Date math for holding days
    holding_days = 0
    if sig.activated_at:
        holding_days = (timezone.now().date() - sig.activated_at.date()).days
    else:
        holding_days = getattr(sig, 'holding_days', 0)

    old_status = sig.status
    sig.exit_price = Decimal(str(round(exit_price, 2)))
    sig.exited_at = timezone.now()
    sig.exit_reason = exit_reason
    sig.pnl_pct = Decimal(str(round(pnl_pct, 2)))
    sig.status = ShortTermSignal.Status.ARCHIVED

    cooldown_days = STRATEGY_CONFIG["trade_lifecycle"]["archived_cooldown_trading_days"]
    calendar_cooldown = int(cooldown_days * 7 / 5)
    sig.cooldown_until = timezone.now() + timedelta(days=calendar_cooldown)
    sig.save()

    # 1. Create TradeHistory records
    TradeHistory.objects.create(
        trade=sig,
        old_status=old_status,
        new_status=status,  # HIT_TARGET, HIT_SL, TRAILING_EXIT
        price=Decimal(str(exit_price)),
        reason=details,
        triggered_by='SYSTEM'
    )
    TradeHistory.objects.create(
        trade=sig,
        old_status=status,
        new_status=ShortTermSignal.Status.ARCHIVED,
        price=Decimal(str(exit_price)),
        reason='Position Archived, Cooldown Active',
        triggered_by='SYSTEM'
    )

    logger.info(
        "[TRADE_ENGINE] EXIT %s: %s trigger=%.2f filled=%.2f (P&L net: %+.2f%%, gross: %+.2f%%, cost: %.3f%%, Days: %d) — %s",
        sig.symbol, status, raw_exit_price, exit_price, pnl_pct, gross_pnl_pct, cost_pct, holding_days, details,
    )

    # 2. Dispatch Telegram alert
    try:
        chat_id = get_short_term_chat_id()
        if status == ShortTermSignal.Status.HIT_TARGET:
            emoji = "🎯"
            title = "FINAL TARGET HIT"
        elif status == ShortTermSignal.Status.TRAILING_EXIT:
            emoji = "📉"
            title = "TRAILING EXIT"
        elif status == ShortTermSignal.Status.TIME_STOP:
            emoji = "⏱️"
            title = "TIME-STOP EXIT"
        else:
            emoji = "🛑"
            title = "STOP LOSS HIT"

        msg = (
            f"{emoji} <b>{title}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Stock: <b>{sig.symbol}</b>\n"
            f"▸ Entry Price: ₹{entry_f:.2f}\n"
            f"▸ Exit Price: <b>₹{exit_price:.2f}</b>\n"
            f"▸ Final Profit: <b>{pnl_pct:+.2f}%</b>\n"
            f"▸ Holding Duration: {holding_days} Days\n"
            f"▸ Reason: {details}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        
        # Queue Telegram delivery — prevents blocking and duplicate sends
        already_logged = TelegramLog.objects.filter(
            short_term_signal=sig,
            event_type=f"EXIT_{status}"
        ).exists()
        if not already_logged:
            TelegramLog.objects.create(
                short_term_signal=sig,
                type='EXIT',
                event_type=f"EXIT_{status}",
                message=msg,
                status='PENDING',
                delivery_status='PENDING',
                retry_count=0
            )
            queue_telegram_message(msg, chat_id=chat_id, short_term_signal=sig, event_type=f"EXIT_{status}_QUEUED")
    except Exception as e:
        logger.error("[TRADE_ENGINE] Telegram exit msg failed for %s: %s", sig.symbol, e)


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM FORMATTERS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_active_holdings_lines() -> list[str]:
    """Live CMP/P&L lines for all ACTIVE short-term holdings, for embedding in the scanner report."""
    active_signals = list(ShortTermSignal.objects.filter(status=ShortTermSignal.Status.ACTIVE))
    if not active_signals:
        return []

    from stocks.services.market_data_orchestrator import get_orchestrator
    orch = get_orchestrator()

    lines = ["🟢 <b>ACTIVE HOLDINGS</b>"]
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
    logger.info("[TRADE_ENGINE] Scanner report: %d active holdings included", len(active_signals))
    return lines


def _get_active_long_term_holdings_lines() -> list[str]:
    """Live CMP/P&L lines for all ACTIVE long-term holdings, for embedding in the scanner report."""
    active_signals = list(
        SignalHistory.objects.filter(category="long_term", status=SignalHistory.Status.ACTIVE)
    )
    if not active_signals:
        return []

    from stocks.services.market_data_orchestrator import get_orchestrator
    orch = get_orchestrator()

    lines = ["🏦 <b>ACTIVE LONG-TERM HOLDINGS</b>"]
    for sig in active_signals:
        try:
            price_res = orch.get_price(sig.symbol, exchange="NSE") or {}
            ltp = float(price_res.get("ltp", 0) or 0)
        except Exception:
            ltp = 0

        entry = float(sig.entry_price)

        if ltp > 0:
            pnl_pct = ((ltp - entry) / entry) * 100
            pnl_emoji = "📈" if pnl_pct >= 0 else "📉"
            # No quantity/capital is recorded against long-term BUY signals — assume an
            # equal notional per position so the report can also show an absolute ₹ figure.
            qty = int(LONG_TERM_ASSUMED_CAPITAL_PER_POSITION // entry) if entry > 0 else 0
            pnl_rupees = (ltp - entry) * qty
            pnl_str = f"{pnl_pct:+.2f}% ({'+' if pnl_rupees >= 0 else '-'}₹{abs(pnl_rupees):,.0f})"
            ltp_str = f"₹{ltp:.2f}"
        else:
            pnl_emoji = "📊"
            pnl_str = "Fetching..."
            ltp_str = "—"

        lines.append(
            f"\n  <b>{sig.symbol}</b> {pnl_emoji}\n"
            f"  ▸ Entry: ₹{entry:.2f} | CMP: {ltp_str}\n"
            f"  ▸ P&L: <b>{pnl_str}</b>"
        )
    logger.info("[TRADE_ENGINE] Scanner report: %d active long-term holdings included", len(active_signals))
    return lines


def _scan_new_long_term_setups() -> list[dict]:
    """DISABLED (2026-07-26). Returns [] without scanning or persisting.

    Three defects, all removed by disabling rather than patching:

    1. **A Telegram formatter ran a scan and wrote to the database.** This function was
       called from `_send_telegram_scanner_summary`, so the entire long-term engine
       executed as a side effect of building a message — and only on days the SHORT-TERM
       strict pass came up empty, which in a BEARISH market is every day.
    2. **It discarded the ATR-derived levels** computed by `_fetch_long_term_quality`
       and substituted `price x 0.5` / `price x 2.0` — a -50% stop and a +100% target
       that no monitoring job ever evaluated anyway.
    3. **`update_or_create(symbol, category)` was not status-scoped**, so a symbol with
       any prior long-term row could match multiple rows and raise
       MultipleObjectsReturned into a blanket except.

    The replacement is specified in doc/LONG_TERM_ENGINE_V2_ARCHITECTURE.md: a scheduled
    monthly job (not a formatter side effect), a six-factor fundamental model, and the
    eight exit rules in §8. It is blocked on a fundamental data feed.
    """
    logger.info("[TRADE_ENGINE] Long-term scan skipped — engine disabled pending "
                "fundamentals (see LONG_TERM_ENGINE_V2_ARCHITECTURE.md §0).")
    return []


def _send_telegram_scanner_summary(trades: list, direction: dict):
    """Queue daily scanner results for Telegram delivery — includes live ACTIVE holdings CMP and new long-term setups."""
    try:
        from stocks.services.telegram_service import queue_telegram_message, get_short_term_chat_id
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now_str = datetime.now(tz=ist).strftime("%d %b %Y, %I:%M %p IST")

        chat_id = get_short_term_chat_id()
        trend = direction.get('trend', 'UNKNOWN')
        trend_emoji = "🟢" if trend == "BULLISH" else "🔴"

        holdings_lines = _get_active_holdings_lines()
        lt_holdings_lines = _get_active_long_term_holdings_lines()
        lt_picks = _scan_new_long_term_setups()

        extra_blocks = ""
        if holdings_lines:
            extra_blocks += "\n\n" + "\n".join(holdings_lines)

        if lt_holdings_lines:
            extra_blocks += "\n\n" + "\n".join(lt_holdings_lines)

        if lt_picks:
            lt_lines = ["📈 <b>NEW LONG-TERM SETUPS</b>"]
            for idx, p in enumerate(lt_picks, 1):
                lt_lines.append(
                    f"\n  {idx}. <b>{p['symbol']}</b> ({p['sector']})\n"
                    f"  ▸ Price: ₹{p['price']}\n"
                    f"  ▸ Plan: {p['entry_plan']}\n"
                    f"  ▸ {p['hold_rule']}"
                )
            extra_blocks += "\n\n" + "\n".join(lt_lines)
        else:
            extra_blocks += "\n\n📈 <b>LONG-TERM</b>\nNo new long-term setups found today."

        if not trades:
            msg = (
                f"📊 <b>DAILY SCANNER REPORT</b>\n"
                f"🕒 {now_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Market: {trend_emoji} {trend}\n\n"
                f"No new setups found matching all criteria today."
                + extra_blocks +
                f"\n━━━━━━━━━━━━━━━━━━━━"
            )
            queue_telegram_message(msg, chat_id=chat_id, event_type='SCANNER_NO_SETUPS')
            logger.info("[TRADE_ENGINE] Scanner report queued (SCANNER_NO_SETUPS, holdings_shown=%s, %d new LT setups)",
                        bool(holdings_lines), len(lt_picks))
            return

        lines = []
        for idx, t in enumerate(trades, 1):
            lines.append(
                f"{idx}. <b>{t['symbol']}</b> (AI: {t['ai_score']:.0f}/100)\n"
                f"   Buy: ₹{t['entry_price']} | SL: ₹{t['stop_loss']}\n"
                f"   T1: ₹{t['target1']} | T2: ₹{t['target2']}\n"
                f"   Timeframe: ~{t['holding_days']} days\n"
            )

        msg = (
            f"🚀 <b>DAILY SCANNER — NEW BUY TODAY SETUPS</b>\n"
            f"🕒 {now_str}\n"
            f"Market: {trend_emoji} {trend} | Nifty: {direction.get('close', 'N/A')}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            + "\n".join(lines) +
            f"\n⚡ <b>Action: Buy at current market price</b>"
            + extra_blocks +
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Powered by TradePulse AI Engine</i>"
        )
        queue_telegram_message(msg, chat_id=chat_id, event_type='DAILY_SCANNER_SUMMARY')
        logger.info("[TRADE_ENGINE] Scanner report queued (DAILY_SCANNER_SUMMARY, holdings_shown=%s, %d new LT setups)",
                    bool(holdings_lines), len(lt_picks))
    except Exception as e:
        logger.error("[TRADE_ENGINE] Telegram scanner summary failed: %s", e)


def _send_telegram_eod_status():
    """Queue EOD portfolio status for Telegram delivery."""
    try:
        from stocks.services.telegram_service import queue_telegram_message, get_short_term_chat_id
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now_str = datetime.now(tz=ist).strftime("%d %b %Y, %I:%M %p IST")

        chat_id = get_short_term_chat_id()

        active = list(ShortTermSignal.objects.filter(status=ShortTermSignal.Status.ACTIVE))
        if not active:
            return

        lines = []
        total_pnl = 0.0
        for sig in active:
            entry_f = float(sig.entry_price)
            current_f = float(sig.current_price or sig.entry_price)
            pnl = ((current_f - entry_f) / entry_f) * 100 if entry_f > 0 else 0.0
            total_pnl += pnl
            emoji = "📈" if pnl >= 0 else "📉"

            lines.append(
                f"\n  <b>{sig.symbol}</b> {emoji}\n"
                f"  ▸ Entry: ₹{entry_f:.2f} | CMP: ₹{current_f:.2f}\n"
                f"  ▸ P&L: <b>{pnl:+.2f}%</b>\n"
                f"  ▸ SL: ₹{float(sig.stop_loss):.2f} | Target: ₹{float(sig.target):.2f}"
            )

        avg_pnl = total_pnl / len(active) if active else 0.0
        portfolio_emoji = "🟢" if avg_pnl >= 0 else "🔴"

        msg = (
            f"📊 <b>EOD PORTFOLIO STATUS</b>\n"
            f"🕒 {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{portfolio_emoji} Portfolio Avg: <b>{avg_pnl:+.2f}%</b> ({len(active)} holdings)\n"
            + "\n".join(lines) +
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Powered by TradePulse AI</i>"
        )
        queue_telegram_message(msg, chat_id=chat_id, event_type='EOD_PORTFOLIO_STATUS')
    except Exception as e:
        logger.error("[TRADE_ENGINE] Telegram EOD status queue failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD API HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_dashboard_data() -> dict:
    """
    Returns the complete dashboard payload for the upgraded ProSystem page.
    Groups signals by status tab with extended metrics.
    """
    from stocks.services.pro_system_service import get_market_direction

    all_signals = ShortTermSignal.objects.all().order_by('-generated_at')

    def _fmt(sig: ShortTermSignal) -> dict:
        entry_f = float(sig.entry_price)
        current_f = float(sig.current_price or sig.entry_price)
        sl_f = float(sig.stop_loss)
        target_f = float(sig.target)
        pnl_pct = float(sig.pnl_pct) if sig.pnl_pct else (
            ((current_f - entry_f) / entry_f * 100) if entry_f > 0 else 0.0
        )

        return {
            'id': sig.id,
            'symbol': sig.symbol,
            'signal': 'BUY',
            'entry_price': entry_f,
            'current_price': current_f,
            'stop_loss': sl_f,
            'sl_pct': round(((entry_f - sl_f) / entry_f) * 100, 1) if entry_f > 0 else 0,
            'target': target_f,
            'target_pct': round(((target_f - entry_f) / entry_f) * 100, 1) if entry_f > 0 else 0,
            'target2': float(sig.target2) if sig.target2 else None,
            'target3': float(sig.target3) if sig.target3 else None,
            'vol_ratio': float(sig.vol_ratio or 0),
            'setup': sig.setup,
            'status': sig.status,
            'pnl_pct': round(pnl_pct, 2),
            'highest_profit': float(sig.highest_profit or 0.0),
            'max_drawdown': float(sig.max_drawdown or 0.0),
            'cooldown_until': sig.cooldown_until.isoformat() if sig.cooldown_until else None,
            'ai_score': float(sig.ai_score or 0.0),
            'exit_reason': sig.exit_reason,
            'review_required': sig.review_required,
            'holding_days': (timezone.now().date() - sig.activated_at.date()).days if sig.activated_at else 0,
            'generated_at': sig.generated_at.isoformat() if sig.generated_at else None,
            'activated_at': sig.activated_at.isoformat() if sig.activated_at else None,
            'exited_at': sig.exited_at.isoformat() if sig.exited_at else None,
            'exit_price': float(sig.exit_price) if sig.exit_price else None,
        }

    # Format all database entries
    formatted_signals = [_fmt(s) for s in all_signals]

    # Upgraded lifecycle status groupings
    pending_list = [f for f in formatted_signals if f['status'] == ShortTermSignal.Status.PENDING]
    active_list = [f for f in formatted_signals if f['status'] == ShortTermSignal.Status.ACTIVE]
    target1_list = [f for f in formatted_signals if f['status'] == ShortTermSignal.Status.TARGET1]
    target2_list = [f for f in formatted_signals if f['status'] == ShortTermSignal.Status.TARGET2]
    review_list = [f for f in formatted_signals if f['status'] == ShortTermSignal.Status.REVIEW_REQUIRED]
    archived_list = [f for f in formatted_signals if f['status'] == ShortTermSignal.Status.ARCHIVED]
    expired_list = [f for f in formatted_signals if f['status'] == ShortTermSignal.Status.EXPIRED]

    # Legacy groups (backward compatibility)
    active_legacy = active_list + target1_list + target2_list + review_list
    hit_target_legacy = [f for f in archived_list if 'Target' in (f['exit_reason'] or '')]
    hit_sl_legacy = [f for f in archived_list if 'Stop' in (f['exit_reason'] or '') or 'Trailing' in (f['exit_reason'] or '')]
    closed_legacy = expired_list

    # Analytics calculation
    total_trades = all_signals.count()
    total_wins = all_signals.filter(status=ShortTermSignal.Status.HIT_TARGET).count()
    total_losses = all_signals.filter(status=ShortTermSignal.Status.HIT_SL).count()
    total_closed = total_wins + total_losses
    win_rate = round((total_wins / total_closed * 100), 1) if total_closed > 0 else 0.0

    total_pnl = 0.0
    for s in all_signals.filter(status__in=[
        ShortTermSignal.Status.HIT_TARGET, ShortTermSignal.Status.HIT_SL,
    ]):
        if s.pnl_pct:
            total_pnl += float(s.pnl_pct)

    unrealized_pnl = sum(t['pnl_pct'] for t in active_legacy)

    # ── Long-Term signals (separate model/status vocabulary — kept parallel, not merged) ──
    # SignalHistory has a 6-state Status vs ShortTermSignal's 14-state Status, and no
    # target2/ai_score/highest_profit/etc. fields, so these are formatted independently
    # rather than forced into the tabs/_fmt shape above.
    from stocks.models import SignalHistory
    lt_signals = SignalHistory.objects.filter(category="long_term").order_by('-generated_at')

    # Live P&L for ACTIVE holdings — buy-and-hold longs have no target/SL, so this is
    # the only way to see how a position is running. Reuses the same WebSocket-backed
    # bulk price lookup as the live price ticker, so it doesn't add REST pressure.
    active_lt_symbols = [s.symbol for s in lt_signals if s.status == SignalHistory.Status.ACTIVE]
    lt_price_map = {}
    if active_lt_symbols:
        try:
            from stocks.services.live_signal_service import get_latest_prices
            lt_price_map = get_latest_prices(active_lt_symbols)
        except Exception as e:
            logger.warning(f"Failed to fetch live prices for long-term holdings: {e}")

    def _fmt_lt(sig: SignalHistory) -> dict:
        entry_f = float(sig.entry_price) if sig.entry_price else None
        current_f = None
        pnl_pct = None
        if sig.status == SignalHistory.Status.ACTIVE:
            live_price = lt_price_map.get(sig.symbol)
            if live_price:
                current_f = float(live_price)
                if entry_f:
                    pnl_pct = round(((current_f - entry_f) / entry_f) * 100, 2)

        return {
            'id': sig.id,
            'symbol': sig.symbol,
            'entry_price': entry_f,
            'current_price': current_f,
            'pnl_pct': pnl_pct,
            'stop_loss': float(sig.stop_loss) if sig.stop_loss else None,
            'target': float(sig.target) if sig.target else None,
            'status': sig.status,
            'reason': sig.reason,
            'exit_price': float(sig.exit_price) if sig.exit_price else None,
            'exit_time': sig.exit_time.isoformat() if sig.exit_time else None,
            'generated_at': sig.generated_at.isoformat() if sig.generated_at else None,
        }

    formatted_lt = [_fmt_lt(s) for s in lt_signals]
    lt_tabs = {
        'pending': [f for f in formatted_lt if f['status'] == SignalHistory.Status.PENDING],
        'active': [f for f in formatted_lt if f['status'] == SignalHistory.Status.ACTIVE],
        'hit_target': [f for f in formatted_lt if f['status'] == SignalHistory.Status.HIT_TARGET],
        'hit_sl': [f for f in formatted_lt if f['status'] == SignalHistory.Status.HIT_SL],
        'cancelled': [f for f in formatted_lt if f['status'] == SignalHistory.Status.CANCELLED],
        'expired': [f for f in formatted_lt if f['status'] == SignalHistory.Status.EXPIRED],
    }
    lt_hit_target_count = len(lt_tabs['hit_target'])
    lt_hit_sl_count = len(lt_tabs['hit_sl'])
    lt_closed = lt_hit_target_count + lt_hit_sl_count

    return {
        'market_direction': get_market_direction(),
        'long_term': {
            'tabs': lt_tabs,
            'analytics': {
                'total': len(formatted_lt),
                'active_count': len(lt_tabs['active']),
                'pending_count': len(lt_tabs['pending']),
                'win_rate': round(lt_hit_target_count / lt_closed * 100, 1) if lt_closed > 0 else 0.0,
            },
        },
        'tabs': {
            # Upgraded tabs
            'pending': pending_list,
            'active': active_list,
            'target1': target1_list,
            'target2': target2_list,
            'review_required': review_list,
            'archived': archived_list,
            'expired': expired_list,
            # Legacy fallback tabs
            'hit_target': hit_target_legacy,
            'hit_sl': hit_sl_legacy,
            'closed': closed_legacy,
        },
        'analytics': {
            'total_trades': total_trades,
            'active_count': len(active_legacy),
            'pending_count': len(pending_list),
            'total_wins': total_wins,
            'total_losses': total_losses,
            'win_rate': win_rate,
            'realized_pnl': round(total_pnl, 2),
            'unrealized_pnl': round(unrealized_pnl, 2),
        },
        'timestamp': timezone.now().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 6. TRADE ENTRY ACTIVATION MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

def check_pending_activations():
    """
    Check all PENDING trades during market hours.
    If LTP <= entry_price, activate the trade:
    - PENDING -> ACTIVE
    - activated_at = timezone.now()
    - Create TradeHistory log entry
    - Send Telegram alert
    """
    from stocks.services.truedata_service import get_truedata_instance
    from stocks.models import TradeHistory, ShortTermSignal, TelegramLog
    from stocks.services.telegram_service import queue_telegram_message, get_short_term_chat_id
    import pytz

    pending_trades = list(ShortTermSignal.objects.filter(status=ShortTermSignal.Status.PENDING))
    if not pending_trades:
        logger.info("[TRADE_ENGINE] No PENDING trades to evaluate for activation.")
        return

    svc = get_truedata_instance()
    if not svc:
        logger.error("[TRADE_ENGINE] Angel One instance not available for activation checker")
        return

    symbols = [s.symbol for s in pending_trades]
    token_map = svc.get_token_map(symbols, exchange="NSE")
    if not token_map:
        logger.warning("[TRADE_ENGINE] Token map empty for pending activation check")
        return

    tokens = list(token_map.values())
    quotes = {}
    for i in range(0, len(tokens), 50):
        try:
            chunk_q = svc.get_bulk_quotes({"NSE": tokens[i:i+50]}, mode="FULL")
            quotes.update(chunk_q)
        except Exception as e:
            logger.warning("[TRADE_ENGINE] Bulk quote fetch failed for activation: %s", e)

    for trade in pending_trades:
        tok = token_map.get(trade.symbol)
        if not tok:
            continue
        q = quotes.get(f"NSE:{tok}")
        if not q:
            continue

        ltp = float(q.get("ltp", 0))
        if ltp <= 0:
            continue

        # Update current price
        trade.current_price = Decimal(str(ltp))
        trade.save(update_fields=['current_price'])

        # Check entry condition: LTP <= Entry Price
        if ltp <= float(trade.entry_price):
            # Double check status to prevent race conditions / duplicate runs
            try:
                with transaction.atomic():
                    # Reload with select_for_update to prevent concurrent activation
                    db_trade = ShortTermSignal.objects.select_for_update().get(id=trade.id)
                    if db_trade.status != ShortTermSignal.Status.PENDING:
                        logger.info("[TRADE_ENGINE] Signal %s already activated in another thread", db_trade.symbol)
                        continue

                    # ── Cost / slippage model (audit remediation Phase 1 item 6) ──
                    # The activation poll observes LTP, not an obtainable fill: a real
                    # buy crosses the offer, so the booked entry must be worse than the
                    # touched price, never equal to or better than it.
                    trigger_price = float(db_trade.entry_price)
                    fill_price = round(
                        cost_model_for(PROFILE).slipped_fill(ltp, is_buy=True, symbol=db_trade.symbol),
                        2,
                    )
                    # Audit remediation 3.1.5: entry_price must be rewritten to the observed
                    # fill (not left at the stale scan-time trigger), so SL/target/RR computed
                    # earlier are auditable against what was actually paid. Deliberately NOT
                    # rescaling stop_loss/target/target2/target3 here — proportionally
                    # rescaling them to preserve the original R-multiple is a product decision
                    # (see AUDIT_REMEDIATION_PLAN.md 3.1.5) left for a separate item; this fix
                    # only records the slippage delta for audit.
                    slippage_delta = round(fill_price - trigger_price, 2)

                    db_trade.status = ShortTermSignal.Status.ACTIVE
                    db_trade.activated_at = timezone.now()
                    db_trade.entry_price = Decimal(str(fill_price))
                    db_trade.save(update_fields=['status', 'activated_at', 'entry_price'])

                    # 1. Create TradeHistory audit record
                    TradeHistory.objects.create(
                        trade=db_trade,
                        old_status=ShortTermSignal.Status.PENDING,
                        new_status=ShortTermSignal.Status.ACTIVE,
                        price=Decimal(str(fill_price)),
                        reason=(
                            f'Entry Trigger Hit (trigger ₹{trigger_price:.2f}, LTP ₹{ltp:.2f}, '
                            f'filled ₹{fill_price:.2f} incl. slippage, slippage_delta '
                            f'{slippage_delta:+.2f} vs original entry; SL/target left unchanged '
                            f'at pre-fill levels)'
                        ),
                        triggered_by='SYSTEM'
                    )

                    # 2. Dispatch Telegram alert
                    chat_id = get_short_term_chat_id()
                    msg = (
                        f"✅ <b>BUY ACTIVATED</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"Stock: <b>{db_trade.symbol}</b>\n"
                        f"▸ Entry Trigger: <b>₹{trigger_price:.2f}</b>\n"
                        f"▸ Filled Price: <b>₹{fill_price:.2f}</b> (incl. slippage)\n"
                        f"▸ Current Price: ₹{ltp:.2f}\n"
                        f"▸ Stop Loss: ₹{db_trade.stop_loss}\n"
                        f"▸ Target 1: ₹{db_trade.target}\n"
                        f"▸ Target 2: ₹{db_trade.target2}\n"
                        f"▸ Target 3: ₹{db_trade.target3}\n"
                        f"▸ Activation: {timezone.now().astimezone(pytz.timezone('Asia/Kolkata')).strftime('%d %b %Y, %I:%M %p')}\n"
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )
                    # Queue alert — prevents blocking and duplicate sends
                    already_logged = TelegramLog.objects.filter(
                        short_term_signal=db_trade,
                        event_type='BUY_ACTIVATED'
                    ).exists()
                    if not already_logged:
                        # Write PENDING record — queue dispatcher delivers within 1 min
                        TelegramLog.objects.create(
                            short_term_signal=db_trade,
                            type='ACTIVATED',
                            event_type='BUY_ACTIVATED',
                            message=msg,
                            status='PENDING',
                            delivery_status='PENDING',
                            retry_count=0
                        )
                        queue_telegram_message(msg, chat_id=chat_id, short_term_signal=db_trade, event_type='BUY_ACTIVATED_QUEUED')

                    logger.info(
                        "[TRADE_ENGINE] Activated PENDING trade for %s: trigger=%.2f ltp=%.2f filled=%.2f",
                        db_trade.symbol, trigger_price, ltp, fill_price,
                    )

            except Exception as e:
                logger.error("[TRADE_ENGINE] Failed to activate trade for %s: %s", trade.symbol, e)

