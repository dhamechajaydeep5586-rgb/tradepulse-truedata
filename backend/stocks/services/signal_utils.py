from __future__ import annotations
import time as time_module
import numpy as np
import pandas as pd
import logging
import requests
from datetime import datetime, time, date, timezone
from zoneinfo import ZoneInfo
from django.utils import timezone as dj_timezone
from django.core.cache import cache
import pandas as pd
import io
from stocks.models import IndexConstituent

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
INTRADAY_SIGNAL_CUTOFF = time(15, 20)
OPTION_SELLING_SIGNAL_CUTOFF = time(15, 15)
# Earlier than every other cutoff above on purpose: option BUYING loses to theta decay
# the longer a position sits open, unlike option-selling (which wants time to pass) or
# equity signals (which don't decay). Forces a hard exit well before EOD regardless of P&L.
OPTION_BUYING_TIME_STOP = time(14, 30)

# Stop OPENING brand-new positions well before the force-close cutoffs above, so every
# signal gets real runway to reach target/SL organically instead of syncing on the
# forced-exit timestamp. Fixed 2026-07-28: both generation and force-close used to share
# the SAME cutoff (INTRADAY_SIGNAL_CUTOFF / OPTION_BUYING_TIME_STOP), so a signal minted
# minutes before the cutoff had only minutes to live — e.g. three option-buying signals
# generated ~2:15 PM on 2026-07-28 all got force-closed at the 2:30 PM hard stop 15
# minutes later, which looked (and was reported by the user) as "why did everything
# exit at the exact same time" even though each trade's own math was correct.
# Existing ACTIVE/PENDING positions are unaffected — only new-signal generation stops
# earlier; INTRADAY_SIGNAL_CUTOFF and OPTION_BUYING_TIME_STOP still govern force-close.
INTRADAY_GENERATION_CUTOFF = time(14, 30)      # 50 min of runway before the 3:20 PM force-close
OPTION_BUYING_GENERATION_CUTOFF = time(13, 0)  # 90 min of runway before the 2:30 PM force-close
                                                # (wider buffer than intraday's since options
                                                # need a bigger move, faster, to clear entry
                                                # cost + reach a 1.6x-2.0x premium target)

# Skip the noisy opening range before allowing the FIRST generation attempt of the day.
# 9:15-9:45 AM is dominated by gap-driven price discovery — VAH/VAL/POC crosses in this
# window are disproportionately false breakouts that reverse once real direction sets in.
# Deliberately NOT pushed later than this: the schedule was already tuned once (see
# updater.py / CLAUDE.md) specifically to avoid missing 9:15-11:00 IST, the highest-volume
# part of the session — this only trims the first ~30 noisy minutes off that window, it
# doesn't abandon it. Only gates the FIRST scan of the day; once a signal exists,
# subsequent scans behave as before (5-min cooldown, update-only, etc).
INTRADAY_GENERATION_START = time(9, 45)
# 15 min later than intraday: option buying's ADX>20 trend-strength gate is more sensitive
# to immature opening-range data than intraday's volume-profile trigger is — firing it on
# the same early bars intraday tolerates risks a false-ADX entry into a decaying, leveraged
# position, which is a costlier mistake here than for equity.
OPTION_BUYING_GENERATION_START = time(10, 0)

# Intraday scanner's stock universe. NIFTY100 by default (down from NIFTY500) to bound
# Angel One REST call volume per scan cycle — drop to "NIFTY50" here if rate-limit
# pressure persists after the bulk-quote batching fix in intraday_service.py.
INTRADAY_UNIVERSE = "NIFTY100"

# ---------------------------------------------------------------------------
# NSE Market Status API  (primary source — overrides static calendar)
# ---------------------------------------------------------------------------
_NSE_STATUS_CACHE: dict = {"data": None, "fetched_at": 0.0}
_NSE_STATUS_TTL = 60  # re-fetch at most once per minute

def _fetch_nse_market_status() -> list[dict] | None:
    """
    Fetches live market status from NSE's own API.
    Returns the `marketState` list or None on failure.
    Cached for 60 seconds to avoid hammering NSE.
    """
    now = time_module.time()
    if _NSE_STATUS_CACHE["data"] and (now - _NSE_STATUS_CACHE["fetched_at"]) < _NSE_STATUS_TTL:
        return _NSE_STATUS_CACHE["data"]

    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
        })
        # Bootstrap cookies — NSE requires visiting the homepage first
        session.get("https://www.nseindia.com", timeout=8)
        resp = session.get("https://www.nseindia.com/api/marketStatus", timeout=8)
        resp.raise_for_status()
        market_state = resp.json().get("marketState", [])
        if market_state:
            _NSE_STATUS_CACHE["data"] = market_state
            _NSE_STATUS_CACHE["fetched_at"] = now
            logger.debug("[NSE_STATUS] Fetched %d segment(s) from NSE API", len(market_state))
            return market_state
    except Exception as exc:
        logger.debug("[NSE_STATUS] API fetch failed, will use static fallback: %s", exc)

    return None

# NSE Holidays (2025–2026)
NSE_HOLIDAYS: set[date] = {
    # ── 2025 ──
    date(2025, 2, 26), date(2025, 3, 14), date(2025, 3, 31),
    date(2025, 4, 10), date(2025, 4, 14), date(2025, 4, 18),
    date(2025, 5, 1), date(2025, 8, 15), date(2025, 8, 27),
    date(2025, 10, 2), date(2025, 10, 21), date(2025, 10, 22),
    date(2025, 11, 5), date(2025, 12, 25),
    # ── 2026 ──
    date(2026, 1, 15), date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26),
    date(2026, 3, 31), date(2026, 4, 3), date(2026, 4, 14), date(2026, 5, 1),
    date(2026, 5, 28), date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2),
    date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25),
    # Weekend holidays in 2026 (for completeness/guard fallback)
    date(2026, 2, 15), date(2026, 3, 21), date(2026, 8, 15), date(2026, 11, 8),
    # ── 2027 (Provisional/Fixed National Holidays) ──
    date(2027, 1, 26), date(2027, 4, 14), date(2027, 5, 1),
    date(2027, 8, 15), date(2027, 10, 2), date(2027, 12, 25),
}

def sync_nse_holidays_from_api() -> bool:
    """
    Fetches the holiday list from NSE API dynamically and seeds/updates MarketHoliday table.
    Ensures that we cache the success of this check for 24 hours.
    Additionally, deactivates holidays in the database that are no longer returned by the API.
    """
    cache_key = "nse_holidays_sync_success"
    cooldown_key = "nse_holidays_sync_failed_cooldown"
    if cache.get(cache_key):
        return True
    if cache.get(cooldown_key):
        return False

    logger.info("[HOLIDAY_SYNC] Initiating dynamic NSE holidays sync...")
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
        })
        session.get("https://www.nseindia.com", timeout=8)
        resp = session.get("https://www.nseindia.com/api/holiday-master?type=trading", timeout=8)
        resp.raise_for_status()
        data = resp.json()

        from stocks.models import MarketHoliday
        
        updated_count = 0
        active_dates = set()
        for segment in ['CM', 'FO']:
            segment_holidays = data.get(segment, [])
            for h in segment_holidays:
                trading_date_str = h.get('tradingDate')
                description = h.get('description', 'NSE Holiday')
                if not trading_date_str:
                    continue
                try:
                    h_date = datetime.strptime(trading_date_str, "%d-%b-%Y").date()
                    active_dates.add(h_date)
                    _, created = MarketHoliday.objects.update_or_create(
                        holiday_date=h_date,
                        defaults={
                            "market": "NSE",
                            "name": description,
                            "is_active": True
                        }
                    )
                    if created:
                        updated_count += 1
                except Exception as parse_err:
                    logger.debug(f"[HOLIDAY_SYNC] Failed to parse holiday date '{trading_date_str}': {parse_err}")
        
        # Deactivate any obsolete holidays in the database for the years retrieved
        if active_dates:
            years = {d.year for d in active_dates}
            obsolete_holidays = MarketHoliday.objects.filter(
                market="NSE",
                holiday_date__year__in=years,
                is_active=True
            ).exclude(holiday_date__in=active_dates)
            
            if obsolete_holidays.exists():
                count = obsolete_holidays.count()
                logger.info(f"[HOLIDAY_SYNC] Deactivating {count} obsolete database holidays: {[h.holiday_date for h in obsolete_holidays]}")
                obsolete_holidays.update(is_active=False)

        logger.info(f"[HOLIDAY_SYNC] Successfully synced. {updated_count} new holidays added.")
        cache.set(cache_key, True, timeout=86400)
        return True
    except Exception as exc:
        logger.error(f"[HOLIDAY_SYNC] Dynamic sync failed: {exc}")
        cache.set(cooldown_key, True, timeout=21600)  # back off 6h — NSE blocks Render's IP; retrying every minute is pointless
        return False

def is_market_open() -> bool:
    """NSE/NFO Status with Heartbeat Pulse."""
    return get_market_status("NSE") == "OPEN"

# In-memory cache for is_market_open_today to prevent repeated DB/API calls
# from the WebSocket health monitor (fires every 15 seconds).
_MARKET_OPEN_CACHE: dict = {"result": None, "date": None, "ts": 0.0}
_MARKET_OPEN_CACHE_TTL = 60  # seconds

def is_market_open_today(target_date=None, timezone="Asia/Kolkata") -> bool:
    """
    Core production weekend & holiday guard.
    Returns False on Saturdays, Sundays, and NSE holidays (from DB or fallback).
    Results are cached for 60 seconds to prevent log spam from high-frequency callers.
    """
    import zoneinfo
    from datetime import date
    from django.utils import timezone as dj_timezone
    from stocks.models import MarketHoliday

    tz = zoneinfo.ZoneInfo(timezone)
    now_ist = dj_timezone.now().astimezone(tz)
    eval_date = target_date if target_date else now_ist.date()

    # Fast-path: return cached result if same date and within TTL
    now_ts = time_module.time()
    if (
        _MARKET_OPEN_CACHE["date"] == eval_date
        and _MARKET_OPEN_CACHE["result"] is not None
        and (now_ts - _MARKET_OPEN_CACHE["ts"]) < _MARKET_OPEN_CACHE_TTL
    ):
        return _MARKET_OPEN_CACHE["result"]

    # 0. Sync holidays dynamically from NSE API
    try:
        sync_nse_holidays_from_api()
    except Exception as sync_err:
        logger.warning(f"[HOLIDAY GUARD] Dynamic holiday sync failed: {sync_err}")

    # 1. Weekend Check (0 = Monday, ..., 5 = Saturday, 6 = Sunday)
    if eval_date.weekday() >= 5:
        logger.info(f"[HOLIDAY GUARD] Evaluated {eval_date}: CLOSED (Weekend)")
        _MARKET_OPEN_CACHE.update({"result": False, "date": eval_date, "ts": now_ts})
        return False

    # 2. Database Holiday Check
    try:
        is_db_holiday = MarketHoliday.objects.filter(
            holiday_date=eval_date,
            is_active=True
        ).exists()
        if is_db_holiday:
            logger.info(f"[HOLIDAY GUARD] Evaluated {eval_date}: CLOSED (DB Holiday)")
            _MARKET_OPEN_CACHE.update({"result": False, "date": eval_date, "ts": now_ts})
            return False
    except Exception as db_err:
        logger.warning(f"[HOLIDAY GUARD] DB holiday query failed, falling back to static config: {db_err}")

    # 3. Static Fallback Check (Using existing NSE_HOLIDAYS list)
    if eval_date in NSE_HOLIDAYS:
        logger.info(f"[HOLIDAY GUARD] Evaluated {eval_date}: CLOSED (Static Config Holiday)")
        _MARKET_OPEN_CACHE.update({"result": False, "date": eval_date, "ts": now_ts})
        return False

    # OPEN case: log at DEBUG to prevent flooding (this fires every 15s from health monitor)
    logger.debug(f"[HOLIDAY GUARD] Evaluated {eval_date}: OPEN (Weekday, not holiday)")
    _MARKET_OPEN_CACHE.update({"result": True, "date": eval_date, "ts": now_ts})
    return True

def round_to_tick(val: float, tick: float = 0.05) -> float:
    if pd.isna(val): return 0.0
    return round(round(val / tick) * tick, 2)

def gap_adjusted_stop_price(stop: float, touched_extreme: float, is_buy: bool) -> float:
    """Recorded stop-exit price for a bar that crossed the stop.

    A bar merely grazing the stop fills close to the stop level; a genuine gap-through
    (news, illiquid stock, circuit-triggered reopen) can leave the bar's low/high well
    past it, and booking the stop level in that case understates the real loss. 0.05%
    tolerance distinguishes an ordinary graze (recorded at the stop, as before) from a
    real gap (recorded at the actual touched extreme) — wide enough to ignore tick/float
    noise, tight enough to catch a real gap.
    """
    tol = stop * 0.0005
    breach = (stop - touched_extreme) if is_buy else (touched_extreme - stop)
    return touched_extreme if breach > tol else stop

def compute_atr(df: pd.DataFrame, period: int) -> float:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    val = tr.rolling(window=period).mean().iloc[-1]
    return float(val) if pd.notna(val) else 0.0

def compute_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    return (typical * df["Volume"]).cumsum() / df["Volume"].cumsum()

def compute_session_vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP — the cumulative sums reset at each trading day.

    compute_vwap() runs one cumsum across the whole frame, so on a multi-day lookback
    it returns a multi-day cumulative average that is anchored by earlier sessions and
    barely moves intraday. "Distance from VWAP" against that number is close to
    meaningless for an intraday decision. Intraday VWAP must anchor to the session open,
    which is also what institutional execution benchmarks against.

    Falls back to an unweighted running average of typical price within the session
    when volume is zero. This matters beyond edge-case safety: INDEX candles (e.g. the
    NIFTY 50 series the regime model reads) report Volume=0 on every bar, since an
    index isn't itself traded — only its constituents are. Without this fallback,
    every call on index data returns NaN, which then propagates through np.sign() and
    np.clip() in the regime trend-score math and makes every ">" / "<" threshold check
    silently evaluate False — collapsing the directional read to SIDEWAYS regardless of
    the actual market. That is a real, currently-live bug this fixes, not just a
    backtest concern: get_standard_market_state()/get_regime() call this same function
    on the same zero-volume NIFTY feed.
    """
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    session = pd.Series(df.index.date, index=df.index)
    pv = (typical * df["Volume"]).groupby(session).cumsum()
    vol = df["Volume"].groupby(session).cumsum()
    vwap = (pv / vol.replace(0, np.nan)).astype(float)

    unweighted = typical.groupby(session).cumsum() / (typical.groupby(session).cumcount() + 1)
    return vwap.fillna(unweighted.astype(float))

def compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    """
    Trend-strength filter for option buying — a VA breakout on a choppy/rangebound
    day isn't enough conviction to pay premium for, so callers additionally require
    adx > 20. Rolling-mean smoothing (not Wilder's), matching compute_atr()'s style
    already used in this codebase, rather than mixing two different smoothing methods.
    """
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_high, prev_low, prev_close = high.shift(1), low.shift(1), close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr_smooth = tr.rolling(window=period).mean()
    plus_dm_smooth = plus_dm.rolling(window=period).mean()
    minus_dm_smooth = minus_dm.rolling(window=period).mean()

    plus_di = 100 * (plus_dm_smooth / tr_smooth.replace(0, np.nan))
    minus_di = 100 * (minus_dm_smooth / tr_smooth.replace(0, np.nan))
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx = dx.rolling(window=period).mean().iloc[-1]
    return float(adx) if pd.notna(adx) else 0.0

def compute_triple_ema(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """JD BUY/SELL Logic: 9 EMA, 20 SMA, 51 SMA"""
    ema9 = ta_ema(df["Close"], 9)
    sma20 = ta_sma(df["Close"], 20)
    sma51 = ta_sma(df["Close"], 51)
    return ema9, sma20, sma51

def get_market_status(segment: str = "NSE", static_only: bool = False) -> str:
    """
    Market status check — 3-layer architecture:
      Layer 1 (always): Weekend guard
      Layer 2 (primary): Live NSE API  →  authoritative, no hardcoded holidays needed
      Layer 3 (fallback): Static calendar + time window  (if NSE API is unreachable)
    Returns: 'OPEN' or 'CLOSED'
    """
    # Use Django-aware timezone for consistency
    from django.utils import timezone as dj_timezone
    now_ist = dj_timezone.now().astimezone(IST)

    # Layer 1 — Weekend is always closed regardless of API
    if now_ist.weekday() >= 5:
        return "CLOSED"

    if segment == "NSE":
        if not static_only:
            # Layer 2 — NSE Live API (authoritative)
            market_state = _fetch_nse_market_status()
            
            # Layer 3 — Date Guards (Highest priority after weekend)
            is_holiday = not is_market_open_today(target_date=now_ist.date())
            is_within_time = MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE
            
            if market_state:
                # Primary Segment Match: Capital Market (NSE Equity)
                api_status = "CLOSED"
                for seg in market_state:
                    is_equity = seg.get("market") in ["Capital Market", "Equity", "NSE"]
                    if is_equity:
                        api_status = "OPEN" if seg.get("marketStatus") == "Open" else "CLOSED"
                        logger.debug("[NSE_STATUS] API reported %s: %s", seg.get("market"), api_status)
                        break
                
                if api_status == "OPEN":
                    return "OPEN"
                
                # Resilient Override: If API says CLOSED but it's during hours and not a holiday, trust the time.
                if is_within_time and not is_holiday:
                    logger.info("[NSE_STATUS] API reported CLOSED during hours. Overriding using Failsafe Logic.")
                    return "OPEN"
                
                return "CLOSED"
            
            # Layer 3 — Last Resort Fallback (API unreachable)
            if is_holiday: return "CLOSED"
            return "OPEN" if is_within_time else "CLOSED"

        # Layer 3 — fallback (calendar + time window)
        # This is the "JD Truth" fallback if NSE API is blocked (common on Render)
        if not is_market_open_today(target_date=now_ist.date()):
            logger.debug("[NSE_STATUS] Fallback: Holiday detected")
            return "CLOSED"
        
        # Check time window
        current_time = now_ist.time()
        if MARKET_OPEN <= current_time <= MARKET_CLOSE:
            logger.debug("[NSE_STATUS] Fallback: Within trading hours → OPEN")
            return "OPEN"
        
        logger.debug("[NSE_STATUS] Fallback: Outside trading hours → CLOSED")
        return "CLOSED"

    return "CLOSED"

def is_static_closed(segment: str = "NSE") -> bool:
    """Fast check: is market closed by calendar or time?"""
    return get_market_status(segment, static_only=True) == "CLOSED"

def ta_ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()

def ta_sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length).mean()

def compute_atr_zones(open_price: float, atr: float, level1: float = 0.5, level2: float = 1.0) -> dict[str, float]:
    """JD BUY/SELL Logic: ATR-based Reference Levels from daily open."""
    return {
        "R1": open_price + (atr * level1),
        "S1": open_price - (atr * level1),
        "R2": open_price + (atr * level2),
        "S2": open_price - (atr * level2),
    }

def compute_volume_profile(df: pd.DataFrame, bins: int = 50) -> tuple[float, float, float]:
    if len(df) == 0: return 0.0, 0.0, 0.0
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    hist, bin_edges = np.histogram(tp, bins=bins, weights=df['Volume'])
    total_vol = np.sum(hist)
    if total_vol == 0:
        poc = float((df['High'].max() + df['Low'].min()) / 2)
        return poc, poc, poc
    poc_idx = np.argmax(hist)
    poc = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2.0
    target_vol, current_vol = total_vol * 0.70, hist[poc_idx]
    low_idx = upp_idx = poc_idx

    def _clamp_bin_index(idx: int) -> int:
        return max(0, min(int(idx), len(bin_edges) - 2))

    while current_vol < target_vol and (low_idx > 0 or upp_idx < bins - 1):
        v_low = hist[low_idx - 1] if low_idx > 0 else 0
        v_upp = hist[upp_idx + 1] if upp_idx < bins - 1 else 0
        if v_low > v_upp:
            current_vol += v_low
            low_idx = _clamp_bin_index(low_idx - 1)
        elif v_upp > v_low:
            current_vol += v_upp
            upp_idx = _clamp_bin_index(upp_idx + 1)
        else:
            # If both sides are equal, expand symmetrically but keep the
            # indices bounded to the histogram edges so we never overrun
            # bin_edges[0..len(bin_edges)-1].
            current_vol += v_low + v_upp
            low_idx = _clamp_bin_index(low_idx - 1)
            upp_idx = _clamp_bin_index(upp_idx + 1)

    low_idx = _clamp_bin_index(low_idx)
    upp_idx = _clamp_bin_index(upp_idx)
    return float(poc), float(bin_edges[upp_idx + 1]), float(bin_edges[low_idx])

def compute_session_volume_profile(
    df: pd.DataFrame, bins: int = 40, min_session_bars: int = 12
) -> tuple[float, float, float, str]:
    """Session-anchored volume profile — returns (poc, vah, val, source).

    A profile built across a multi-day frame blends yesterday's and today's volume
    distributions, so the VAH/VAL it produces sit at levels that had meaning in a
    session that has already closed. Standard construction is either today's DEVELOPING
    profile or the PRIOR DAY's profile used as fixed support/resistance — not a blend of
    both.

    Uses today's developing profile once the session has `min_session_bars` of data;
    before that (the first hour, when a developing profile is too thin to be meaningful)
    it falls back to the prior session's completed profile, which is the conventional
    reference for early-session trade.
    """
    if df is None or len(df) == 0:
        return 0.0, 0.0, 0.0, "empty"

    try:
        sessions = pd.Series(df.index.date, index=df.index)
    except Exception:
        poc, vah, val = compute_volume_profile(df, bins=bins)
        return poc, vah, val, "composite_fallback"

    today = sessions.iloc[-1]
    today_df = df[sessions == today]

    if len(today_df) >= min_session_bars:
        poc, vah, val = compute_volume_profile(today_df, bins=bins)
        return poc, vah, val, "developing"

    prior_dates = [d for d in sessions.unique() if d != today]
    if prior_dates:
        prior_df = df[sessions == prior_dates[-1]]
        if len(prior_df) >= 5:
            poc, vah, val = compute_volume_profile(prior_df, bins=bins)
            return poc, vah, val, "prior_day"

    poc, vah, val = compute_volume_profile(df, bins=bins)
    return poc, vah, val, "composite_fallback"


def compute_compression(df: pd.DataFrame, lookback: int = 7) -> dict[str, float | bool]:
    """Volatility-compression detection (NR7 / Bollinger squeeze).

    Volatility clustering is the most robust stylised fact in finance (the whole basis of
    GARCH): contraction is followed by expansion. The practical value here is asymmetry —
    entering while range is compressed gives a small stop against a large potential move,
    which is a direct structural attack on the cost-gate problem, where a thin R is what
    makes trades unprofitable.

    Returns range_ratio (current bar range vs lookback average), bandwidth percentile,
    and an `is_compressed` flag.
    """
    out: dict[str, float | bool] = {"range_ratio": 1.0, "bandwidth_pct": 1.0,
                                    "is_compressed": False, "is_nr7": False}
    try:
        if df is None or len(df) < lookback + 20:
            return out

        rng = (df["High"] - df["Low"]).astype(float)
        cur = float(rng.iloc[-1])
        avg = float(rng.tail(lookback).mean())
        out["range_ratio"] = round(cur / avg, 3) if avg > 0 else 1.0
        # NR7: narrowest range of the last `lookback` bars.
        out["is_nr7"] = bool(cur <= rng.tail(lookback).min() + 1e-9)

        close = df["Close"].astype(float)
        ma = close.rolling(20).mean()
        sd = close.rolling(20).std()
        bandwidth = ((ma + 2 * sd) - (ma - 2 * sd)) / ma.replace(0, pd.NA)
        bw = bandwidth.dropna()
        if len(bw) >= 20:
            cur_bw = float(bw.iloc[-1])
            out["bandwidth_pct"] = round(float((bw.tail(60) <= cur_bw).mean()), 3)
            # Squeeze = bandwidth in the bottom quartile of its recent range.
            out["is_compressed"] = bool(out["bandwidth_pct"] <= 0.25)
    except Exception:
        pass
    return out


def load_cached_index_symbols(index_name: str = "NIFTY500") -> list[str]:
    return list(
        IndexConstituent.objects.filter(index_name=index_name, is_active=True)
        .order_by("symbol")
        .values_list("symbol", flat=True)
    )

# NSE archive CSV filename per index tier — all three share the same
# "Company Name,Industry,Symbol,Series,ISIN Code" column shape, confirmed live.
_NIFTY_INDEX_CSV = {
    "NIFTY50": "ind_nifty50list.csv",
    "NIFTY100": "ind_nifty100list.csv",
    "NIFTY500": "ind_nifty500list.csv",
}


def fetch_nifty_symbols_live(index_name: str = "NIFTY500") -> list[str]:
    """
    Fetch Nifty {50,100,500} constituents directly from NSE Archives to avoid local DB
    reliance. Cached for 24 hours, keyed per index tier so switching tiers doesn't serve
    a stale list cached under a different tier.
    """
    cache_key = f"nifty_symbols_live_{index_name}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    csv_name = _NIFTY_INDEX_CSV.get(index_name, _NIFTY_INDEX_CSV["NIFTY500"])
    url = f'https://nsearchives.nseindia.com/content/indices/{csv_name}'
    logger.info("[SYMBOLS] Fetching live %s list from NSE...", index_name)
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Referer": "https://www.nseindia.com/",
        })
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        df.columns = df.columns.str.strip()
        symbols = [s.strip() for s in df['Symbol'].tolist() if s.strip()]

        if symbols:
            cache.set(cache_key, symbols, timeout=60 * 60 * 24)
            return symbols
    except Exception as exc:
        logger.error("[SYMBOLS] Live fetch failed for %s: %s", index_name, exc)

    # Final fallback to IndexConstituent table if NSE is down
    return load_cached_index_symbols(index_name)


def fetch_nifty500_symbols_live() -> list[str]:
    """Kept for existing callers that explicitly want the full Nifty 500 list
    (e.g. diagnostic_nextday_signals.py) — intraday's own scan universe now goes
    through load_symbols_from_stock_table() / INTRADAY_UNIVERSE instead."""
    return fetch_nifty_symbols_live("NIFTY500")

def load_symbols_from_stock_table() -> list[str]:
    """Overridden to use live fetching first, bypassing heavy 'Stock' table.
    Uses INTRADAY_UNIVERSE (NIFTY100 by default) to bound Angel One REST call volume —
    drop INTRADAY_UNIVERSE to "NIFTY50" if rate-limit pressure persists."""
    return fetch_nifty_symbols_live(INTRADAY_UNIVERSE)

