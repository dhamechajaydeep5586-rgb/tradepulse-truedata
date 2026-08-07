from typing import List, Dict, Any
import itertools
import logging
import threading
from django.utils import timezone
from datetime import timedelta, datetime, time
import pandas as pd
import math

from stocks.services.truedata_service import get_truedata_instance
from django.db.models import Q
from stocks.models import SignalHistory, SignalChangeLog
from stocks.services.signal_utils import is_market_open, IST, round_to_tick, fetch_nifty_symbols_live
from django.core.cache import cache
from django.conf import settings

from stocks.services.market_data_orchestrator import get_orchestrator
from stocks.services.market_intelligence_service import (
    is_sideways_market, is_trading_window_active, get_standard_market_state, get_symbol_market_state
)
from stocks.services.option_greeks_service import calculate_greeks, estimate_iv

from stocks.services.config_vol import (
    COMBINED_SL_MULTIPLIER, PROFIT_CAPTURE_PCT, SHORT_DELTA_DANGER,
    AUTO_EXIT_ON_DELTA_BREACH, FORCE_EXIT_DTE, MAX_SPREAD_PCT,
    # Intraday engine parameters
    INTRADAY_DELTA_MORNING, INTRADAY_DELTA_MIDDAY, INTRADAY_DELTA_AFTERNOON,
    INTRADAY_MAX_DELTA, INTRADAY_EM_CAP_MULT, INTRADAY_EM_HIGH_IV_CAP_MULT,
    INTRADAY_PROFIT_CAPTURE_PCT, INTRADAY_PROFIT_CAPTURE_EARLY,
    INTRADAY_COMBINED_SL_MULT,
    INTRADAY_MIN_PREMIUM_MORNING, INTRADAY_MIN_PREMIUM_MIDDAY,
    INTRADAY_MIN_PREMIUM_AFTERNOON,
    INTRADAY_PREMIUM_ASYMMETRY_TOLERANCE, MIN_INTRADAY_THETA_VELOCITY,
    ASSIGNMENT_RISK_DELTA, INDEX_UNDERLYINGS, MAX_ROLLS_PER_DAY,
)
from stocks.services.risk_state_engine import classify_risk_state, RiskState
from stocks.services.vol_telegram_formatter import format_entry_signal, format_live_update

logger = logging.getLogger(__name__)

# Audit fix (race condition found in a follow-up audit): process_legs() does a
# classic read-modify-write against a SignalHistory row — fetch `sig`, mutate its
# status/metadata/legs across ~400 lines of exit/rebalance logic, then sig.save() —
# with no locking. Force Scan (bypasses the panel's 2s/5s caches, runs process_legs
# inline on the request thread) can overlap with the background scanner's own
# periodic monitor pass for the SAME signal. This app runs as a single gunicorn
# worker with N gthreads (see stocks/apps.py's single-worker guard), so all real
# concurrency here is in-process threads — a plain per-signal threading.Lock fully
# serializes it. Without this, whichever thread's sig.save() lands last silently
# discards the other thread's changes (a delta rebalance roll, a target/SL exit,
# updated P&L) — a lost-update bug. Same double-checked-lock-per-key idiom already
# used for TrueData auth (_AUTH_LOCK / _ensure_fresh_token in truedata_service.py).
_SIGNAL_LOCKS: Dict[int, threading.Lock] = {}
_SIGNAL_LOCKS_GUARD = threading.Lock()


def _get_signal_lock(sig_id) -> threading.Lock:
    lock = _SIGNAL_LOCKS.get(sig_id)
    if lock is None:
        with _SIGNAL_LOCKS_GUARD:
            lock = _SIGNAL_LOCKS.get(sig_id)
            if lock is None:
                lock = threading.Lock()
                _SIGNAL_LOCKS[sig_id] = lock
    return lock


# Volatility (Sigma) Parameters
DEFAULT_STOCK_SIGMA = 0.25
HIGH_BETA_SIGMA = 0.25
FALLBACK_SIGMA = 0.30
_HIGH_BETA_SYMBOLS = {"BAJAJ-AUTO", "ADANIENT", "BEL"}
# ----------------------------------------

# Audit fix (H3): the strangle scanner had no correlation or sector concentration
# control — apply_portfolio_constraints()/build_correlation_clusters() are already
# proven in intraday_service.py/pro_system_service.py but were never imported here.
# Rather than build a whole new EngineProfile (most of its fields — factor_weights,
# sizing_mode, cost model — are meaningless for a premium-selling scanner that ranks
# candidates on VWAP/VA/range metrics, not the equity factor model), this derives
# only the concentration-cap fields from INTRADAY via with_overrides(): a 10-position
# book (HEDGE_MAX_SIGNALS) sits between intraday's 5-position and swing's ~10-15
# position books, so max_per_sector/max_per_cluster are set a notch looser than
# intraday's but the promoter-group % and correlation lookback stay short-dated
# (30d), matching this engine's short (weekly-expiry) holding period rather than
# swing's 90d or long-term's 250d.
def _specialist_portfolio_profile():
    from stocks.services.shared.profiles import INTRADAY
    return INTRADAY.with_overrides(
        name="specialist",
        max_per_sector=int(getattr(settings, "HEDGE_MAX_PER_SECTOR", 3)),
        max_per_cluster=int(getattr(settings, "HEDGE_MAX_PER_CLUSTER", 2)),
        max_per_promoter_group_pct=float(getattr(settings, "HEDGE_MAX_PROMOTER_GROUP_PCT", 20.0)),
        corr_lookback_days=30,
        corr_threshold=float(getattr(settings, "HEDGE_CORRELATION_THRESHOLD", 0.65)),
    )

# --- Portfolio-heat risk gate (Audit Remediation Plan Phase 2 #2.8) ---
# HEDGE_ACCOUNT_CAPITAL: denominator for portfolio_heat_pct — total open SELL-leg notional
# (see _compute_portfolio_heat) as a percentage of this figure. Same override pattern as
# INTRADAY_ACCOUNT_EQUITY (intraday_service.py): a getattr(settings, ...) default that can be
# overridden via Django settings/env once real account/margin capital is confirmed with
# whoever owns account-sizing assumptions. Placeholder default is the same ₹1,00,00,000
# (10,000,000) that was already implicit in the pre-existing hardcoded denominator, so
# behavior does not silently change until this is set deliberately.
HEDGE_ACCOUNT_CAPITAL = float(getattr(settings, "HEDGE_ACCOUNT_CAPITAL", 10_000_000.0))

# MAX_PORTFOLIO_HEAT_PCT: additive gate alongside the existing flat position-count cap
# (HEDGE_MAX_SIGNALS below) — once portfolio_heat_pct reaches this
# threshold, the background scanner skips opening any NEW strangle position for the scan
# cycle, even if count-based slots remain open. Does not touch existing open positions and
# does not affect the dashboard display path. Settings-overridable, same pattern as
# HEDGE_MAX_SIGNALS.
MAX_PORTFOLIO_HEAT_PCT = float(getattr(settings, "MAX_PORTFOLIO_HEAT_PCT", 80.0))


def _fallback_sigma(symbol: str, meta_iv: float | None) -> float:
    """Sigma to use when a leg's live_iv metadata is missing: stored IV if present,
    else a higher default for known high-beta names, else the standard fallback."""
    if meta_iv:
        return float(meta_iv)
    return HIGH_BETA_SIGMA if symbol in _HIGH_BETA_SYMBOLS else FALLBACK_SIGMA

# --- 90% WIN-RATE SPECIALIST CONFIGS ---
# MCX commodity specialists (NATURALGAS/CRUDEOIL) removed — a rate-limit hit on
# any MCX request tripped the single shared Angel One REST circuit breaker
# (truedata_service._REST_CIRCUIT_BREAKER_UNTIL) and blocked NSE intraday/
# option-buying scans for 5 minutes, so MCX was dropped platform-wide.
SPECIALISTS = []

# Static NIFTY50-sized snapshot — last-resort fallback only if the live NSE fetch
# below (NIFTY50) fails. Deliberately not widened to a full 100-name list: this
# only fires if both the live fetch AND the DB fallback in fetch_nifty_symbols_live
# are down, and a smaller-but-correct candidate set beats hand-typing 100 tickers
# with no live source to verify them against.
# Do NOT read this directly; call NIFTY_50_STOCKS() instead, which auto-picks up
# index reconstitutions (e.g. a TCS-in/INFY-out swap) within 24h via
# signal_utils.fetch_nifty_symbols_live's cache TTL.
_NIFTY_50_FALLBACK = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BPCL",
    "BHARTIARTL", "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB",
    "DRREDDY", "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK",
    "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK",
    "INDUSINDBK", "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
    "LTIM", "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SUNPHARMA",
    "TATAMOTORS", "TATASTEEL", "TCS", "TATACONSUM", "TECHM",
    "TITAN", "TRENT", "ULTRACEMCO", "WIPRO"
]


def NIFTY_50_STOCKS() -> list[str]:
    return fetch_nifty_symbols_live("NIFTY50") or _NIFTY_50_FALLBACK

ENTRY_WINDOW_START = time(10, 45)   # Intraday: capture premiums starting from 10:45 AM (Theta optimization after volatility settles)
ENTRY_WINDOW_END = time(15, 30)    # Hard stop for Equity at 3:30 PM
EXIT_TIME = time(15, 25)

# --- STRATEGY PARAMETERS (tune here) ---
SHORT_DELTA = 0.25          # Target sold-option delta (0.25 = safer OTM, 0.30 = closer ATM with higher premium)
FALLBACK_OTM_PCT = 0.025    # 2.5% OTM distance fallback when delta selection fails
MIN_STOCK_PRICE = 150       # Structural floor, not an IV/range call — sub-Rs.150 names (IDEA, SUZLON,
                            # YESBANK, NHPC, NMDC, GMRAIRPORT, IREDA, VMM, PNB, IRFC as of 2026-07-31)
                            # have option strikes spaced too coarsely for the equidistant search and
                            # combined premiums that routinely miss the Rs.3 floor — wastes a full
                            # scan slot (IV fetch + option chain probe) on a candidate that was going
                            # to get rejected anyway. Skipped before any REST calls for that symbol.
MIN_DAYS_TO_EXPIRY = 3      # Roll to next contract once 3 or fewer trading days remain — avoids
                            # opening new short strangles in the compressed final days of an
                            # expiry, when most stocks' OTM premiums decay below the ₹10 floor
                            # (min_individual_premium in the equidistant strike search) and the
                            # few that still qualify carry more gamma risk near expiry.

def get_trading_days_remaining(expiry_date) -> int:
    """Calculate exact working days between today and expiry, excluding weekends and NSE holidays."""
    from stocks.services.signal_utils import NSE_HOLIDAYS, IST
    import numpy as np
    from django.utils import timezone
    
    today = timezone.now().astimezone(IST).date()
    if expiry_date <= today:
        return 0
    holidays_list = [str(h) for h in NSE_HOLIDAYS]
    try:
        return int(np.busday_count(str(today), str(expiry_date), holidays=holidays_list))
    except Exception:
        # Fallback to calendar days if numpy calculation fails
        return (expiry_date - today).days


def expiry_str_to_date(exp_str: str):
    """Parse an NSE option expiry string (format '%d%b%Y', e.g. '25JUN2026') into a date.

    Returns datetime.max.date() on a malformed string so `sorted(..., key=expiry_str_to_date)`
    pushes unparseable entries to the end instead of raising.
    """
    try:
        return datetime.strptime(exp_str, "%d%b%Y").date()
    except (ValueError, TypeError):
        return datetime.max.date()


def resolve_target_expiry(expiries: List[str], min_days: int = MIN_DAYS_TO_EXPIRY):
    """
    Single source of truth for expiry-rollover selection (Audit Remediation Plan Phase 3
    #3.2.4). Previously duplicated as a local `parse_expiry` closure + valid_expiries/
    target_expiry filter independently in both get_nse_option_strikes() and
    build_specialist_hedge() — a future change to the rollover condition in one would not
    have propagated to the other. Both call sites now delegate here.

    Rollover Rule: use the nearest expiry that has MORE than `min_days` trading days
    remaining (i.e. roll to the next contract once `min_days` or fewer trading days remain).

    Args:
        expiries: raw (possibly duplicated/unsorted) list of expiry strings, format '%d%b%Y'.
        min_days: rollover threshold — an expiry qualifies only if trading days remaining is
            strictly greater than this.

    Returns:
        The nearest qualifying expiry string, or the nearest expiry overall if none qualify
        (e.g. every available expiry is already inside the rollover window), or None if
        `expiries` is empty.
    """
    if not expiries:
        return None
    sorted_expiries = sorted(set(expiries), key=expiry_str_to_date)
    valid_expiries = [
        e for e in sorted_expiries
        if get_trading_days_remaining(expiry_str_to_date(e)) > min_days
    ]
    return valid_expiries[0] if valid_expiries else sorted_expiries[0]


MIN_PREMIUM_PER_LEG = 2.0   # Minimum ₹2 premium per leg for NSE equities
MIN_NOTIONAL_PER_LEG = 500  # Minimum ₹500 notional per leg for NSE equities
MIN_LIVE_PREMIUM = 0.30     # Cancel if premium decays below ₹0.30 on a PENDING signal
PENDING_GRACE_SECONDS = 120 # 2-minute grace period: signal stays PENDING so user can review before auto-activation
MAX_PREMIUM_DIFF_WARN = 2.0  # Log warning if best CE/PE pair still differs by more than this (₹) after balancing
PREMIUM_BALANCE_PROBE = 25   # OTM candidates to probe per side when searching for equal-premium pair

# --- INSTITUTIONAL UPGRADES ---
MIN_DTE = 1                      # Minimum days to expiry required to open strangles (blocks unmanaged final-day gamma)
CAP_DELTA_NEAR_EXPIRY = 0.20     # Max delta when DTE <= 3 (gamma risk mitigation)
EXPECTED_MOVE_FLOOR_MULT = 1.0   # Physical strike floor based on expected move (1.0x standard deviation)



# ─────────────────────────────────────────────────────────────────────────────
# INTRADAY ENGINE: Helper Functions
# ─────────────────────────────────────────────────────────────────────────────

def get_intraday_target_delta() -> float:
    """
    Return the target short delta based on current IST time.
    Strikes compress toward ATM as the day progresses to maximise
    same-day theta decay.

    Session windows (IST):
      Morning   09:20 – 11:00  →  0.25  (safer opening strikes)
      Midday    11:00 – 13:00  →  0.28  (moderate compression)
      Afternoon 13:00 – 15:20  →  0.32  (max decay capture)

    NOTE: these three numbers are sourced from INTRADAY_DELTA_MORNING/MIDDAY/AFTERNOON in
    config_vol.py — re-check this docstring whenever those constants change (this is the
    second time the two have drifted out of sync).
    """
    now_ist = timezone.now().astimezone(IST).time()

    if now_ist < time(11, 0):
        delta = INTRADAY_DELTA_MORNING    # 0.25
        regime = "MORNING"
    elif now_ist < time(13, 0):
        delta = INTRADAY_DELTA_MIDDAY     # 0.28
        regime = "MIDDAY"
    else:
        delta = INTRADAY_DELTA_AFTERNOON  # 0.32
        regime = "AFTERNOON"

    logger.debug(
        "[INTRADAY_DELTA] Time regime: %s → target delta: %.2f", regime, delta
    )
    return delta


def get_intraday_time_regime() -> str:
    """Return a human-readable label for the current intraday time session."""
    now_ist = timezone.now().astimezone(IST).time()
    if now_ist < time(11, 0):
        return "Morning"
    elif now_ist < time(13, 0):
        return "Midday"
    return "Afternoon"


def get_intraday_min_premium(symbol: str) -> float:
    """
    Return minimum combined premium floor for the current time session.
    Morning is slightly lower because strikes are safer and fewer takers;
    afternoon is higher because tighter strikes should collect more.
    """
    now_ist = timezone.now().astimezone(IST).time()
    if now_ist < time(11, 0):
        return INTRADAY_MIN_PREMIUM_MORNING    # ₹8
    elif now_ist < time(13, 0):
        return INTRADAY_MIN_PREMIUM_MIDDAY     # ₹10
    return INTRADAY_MIN_PREMIUM_AFTERNOON      # ₹12


def score_intraday_theta_velocity(theta: float, spot: float, strike: float) -> float:
    """
    Intraday Theta Velocity Score.

    Measures how much theta (premium decay) you receive per unit of
    OTM distance risk taken.

        score = |theta| / (|strike - spot| / spot)

    Higher score = faster premium decay per % of OTM distance.
    This is the PRIMARY ranking signal for intraday strangle selection.

    Returns 0.0 if inputs are invalid.
    """
    if spot <= 0 or abs(strike - spot) < 0.01:
        return 0.0
    otm_pct = abs(strike - spot) / spot
    if otm_pct <= 0:
        return 0.0
    return round(abs(theta) / otm_pct, 6)

def is_physical_settlement_risk(symbol: str, max_short_delta: float) -> bool:
    """True if `symbol` is a single-stock underlying (American-style, physically
    settled per SEBI mandate since 2019, unlike cash-settled index options) AND a
    short leg's delta has climbed to ASSIGNMENT_RISK_DELTA — an early, informational
    warning distinct from (and below) SHORT_DELTA_DANGER's auto-exit threshold.
    Audit fix C4: nothing previously distinguished stock-option assignment risk from
    index-option risk anywhere in this engine."""
    return symbol not in INDEX_UNDERLYINGS and max_short_delta >= ASSIGNMENT_RISK_DELTA


def get_lot_size(symbol: str, exchange: str) -> int:
    """Fetch lot size dynamically from the option chain (nearest expiry).

    Returns 0 — not a fabricated real-looking value — if the live lookup and the
    small index-only fallback dict both miss. Audit fix H18: this used to silently
    return 1 on any failure, and 1 is wrong by a factor of 50-1000x+ for almost
    every real NSE lot size, corrupting target/SL premium math and per-lot credit
    figures without anything logging it. Callers must treat 0 as "unknown" and
    either abort (new-signal generation, no prior value exists) or keep whatever
    lot_size they already had (existing-position P&L refresh) — never trade or
    price against a bare 0 or a silently substituted 1.
    """
    svc = get_truedata_instance()
    if svc:
        expiries = svc.get_expiry_list(symbol)
        if expiries:
            nearest = sorted(set(expiries), key=expiry_str_to_date)[0]
            for contract in svc.get_option_chain(symbol, nearest):
                try:
                    ls = int(float(contract.get('lotsize', 0)))
                    if ls > 0: return ls
                except (ValueError, TypeError):
                    continue

    # Robust Lot Size Fallbacks (indices only — real, not guessed)
    fallbacks = {
        'NIFTY': 50, 'BANKNIFTY': 15, 'FINNIFTY': 40, 'MIDCPNIFTY': 75,
    }
    if symbol in fallbacks: return fallbacks[symbol]

    logger.error(
        "[LOT_SIZE] Could not resolve a real lot size for %s/%s (no broker connection "
        "or empty option chain, and not an index with a known fallback) — returning 0.",
        symbol, exchange,
    )
    return 0

def get_nse_option_strikes(symbol: str, spot_price: float) -> List[Dict[str, Any]]:
    """
    Get NSE option strikes for a stock
    """
    svc = get_truedata_instance()
    if not svc:
        return []
    try:
        # Strict Near-Month Filtering: Use calendar date logic, not string comparison
        today = timezone.now().astimezone(IST).date()

        expiries = [e for e in svc.get_expiry_list(symbol) if expiry_str_to_date(e) >= today]
        nse_options = []
        for expiry in expiries:
            nse_options.extend(
                row for row in svc.get_option_chain(symbol, expiry)
                if row['instrumenttype'] in ['OPTSTK', 'OPTIDX']
            )

        if not nse_options:
            return []

        # Process and normalize strikes (NSE strikes are * 100 in master)
        normalized_options = []
        for row in nse_options:
            try:
                row_copy = row.copy()
                row_copy['strike_price'] = float(row['strike']) / 100.0
                normalized_options.append(row_copy)
            except:
                continue

        # Strict Near-Month Filtering: Focus on the earliest available expiry (Chronological).
        # Rollover Rule: Use current month's contract until its expiry day. On expiry day
        # (days_remaining == 0), skip it and move to the next month. Shared with
        # build_specialist_hedge() via resolve_target_expiry() (Audit Remediation Plan
        # Phase 3 #3.2.4) so the rollover threshold can't drift between the two call sites.
        target_expiry = resolve_target_expiry(
            [row['expiry'] for row in normalized_options], MIN_DAYS_TO_EXPIRY
        )
        if target_expiry:
            logger.info("[STRATEGIC] Locking to Sector Expiry: %s (days_remaining=%d)", target_expiry, (expiry_str_to_date(target_expiry) - today).days)
            normalized_options = [row for row in normalized_options if row['expiry'] == target_expiry]

        # Sort by proximity to spot price
        normalized_options.sort(key=lambda x: abs(x['strike_price'] - spot_price))

        return normalized_options
    except Exception as e:
        logger.error(f"Error getting NSE option strikes: {e}")
        return []

def get_nse_option_quote(symbol: str, strike: float, option_type: str, expiry: str = None) -> Dict[str, Any]:
    """
    Get live quote for NSE option with specific expiry alignment
    """
    svc = get_truedata_instance()
    if not svc:
        return {}
    try:
        # NSE strikes in master are multiplied by 100 (e.g. 820.0 is stored as 82000.0)
        raw_strike = float(strike) * 100.0

        expiries_to_check = [expiry] if expiry else svc.get_expiry_list(symbol)
        instruments = []
        for exp in expiries_to_check:
            instruments.extend(svc.get_option_chain(symbol, exp))

        token_data = next(
            (row for row in instruments
              if row.get('instrumenttype') in ['OPTSTK', 'OPTIDX'] and
                 abs(float(row.get('strike', 0)) - raw_strike) < 1.0 and # More resilient matching
                 row.get('symbol', '').endswith(option_type) and
                 (not expiry or row.get('expiry') == expiry)),
            None
        )
        
        if not token_data:
            logger.warning(f"[STRIKE_MISS] Could not find {symbol} {strike} {option_type} {expiry} in master")
            return {}, None

        token = token_data['token']
        logger.info(f"[TOKEN_FOUND] {symbol} {strike} {option_type} -> {token} ({token_data.get('symbol')})")
        
        return svc.get_live_price_by_token(token, exchange='NFO'), token
    except Exception as e:
        logger.error(f"Error getting NSE option quote for {symbol} {strike}: {e}")
        return {}, None

def calculate_pnl(sell_price: float, cmp: float, lot_size: int, lots: int = 1) -> Dict[str, Any]:
    """
    Calculate P&L for an option leg
    """
    if sell_price <= 0:
        return {'pnl': 0, 'pnl_pct': 0, 'status': 'PENDING'}
    
    premium_difference = sell_price - cmp
    pnl = premium_difference * lot_size * lots
    pnl_pct = (premium_difference / sell_price) * 100 if sell_price > 0 else 0

    return {
        'pnl': round(pnl, 2),
        'pnl_pct': round(pnl_pct, 2),
        'status': 'PROFIT' if pnl > 0 else 'LOSS' if pnl < 0 else 'NEUTRAL'
    }

def find_strike_by_delta(strikes: List[Dict], spot: float, target_delta: float, option_type: str, t_days: int, sigma: float, symbol: str) -> Dict:
    """Institutional Strike Selection: Picks the contract closest to the target Delta + STRICT OTM."""
    best_match = None
    min_delta_diff = 1.0
    
    for s_row in strikes:
        try:
            # INTEGRITY GUARD 1: Symbol row must belong to the correct underlying
            # The master uses a 'name' field for MCX but a 'symbol' prefix for NFO/NSE
            row_name = s_row.get('name') or s_row.get('symbol', '')
            if row_name and symbol and not (row_name == symbol or row_name.startswith(symbol)): continue

            # INTEGRITY GUARD 2: Must explicitly match the requested option type (CE/PE)
            if not s_row.get('symbol', '').endswith(option_type): continue

            # --- STEP 1: UNIFIED NORMALIZATION (Rupee Scale Sync) ---
            # We must normalize BEFORE the OTM check to handle mixed-scale data (24500 vs 245)
            strike_val = float(s_row.get('strike', 0))
            # Scale correction for NFO: master stores NSE option strikes ×100
            if s_row.get('exch_seg') == 'NFO': strike_val /= 100.0

            # --- STEP 2: STRICT OTM HARD-LOCK ---
            # Selling ITM is forbidden. We strictly target Delta ≈ 0.20 OTM
            if option_type == 'CE' and strike_val <= spot: continue
            if option_type == 'PE' and strike_val >= spot: continue

            # --- STEP 3: GREEK SELECTION ---
            # Calculate Delta using the unified strike_val and spot
            greeks = calculate_greeks(spot, strike_val, t_days, sigma=sigma, option_type=option_type)
            delta = abs(greeks.get('delta', 0))
            
            diff = abs(delta - target_delta)
            if diff < min_delta_diff:
                min_delta_diff = diff
                best_match = s_row
        except Exception as e:
            logger.debug("find_strike_by_delta: skipping bad strike row: %s", e)
            continue
    return best_match

def find_strike_by_distance(strikes: List[Dict], spot: float, distance_pct: float, option_type: str = 'CE', symbol: str = None) -> Dict:
    """Find the strike closest to the target distance (e.g. 2.5% OTM) for safety."""
    target_price = spot * (1 + distance_pct) if option_type == 'CE' else spot * (1 - distance_pct)
    best_match = None
    min_diff = 999999
    
    for s_row in strikes:
        try:
            # INTEGRITY GUARD: Symbol must match
            row_name = s_row.get('name') or s_row.get('symbol', '')
            if symbol and row_name and not (row_name == symbol or row_name.startswith(symbol)):
                continue

            # INTEGRITY GUARD 2: Must explicitly match the requested option type (CE/PE)
            if not s_row.get('symbol', '').endswith(option_type): continue

            # UNIFIED NORMALIZATION
            strike_val = float(s_row.get('strike', 0))
            if s_row.get('exch_seg') == 'NFO': strike_val /= 100.0
            
            diff = abs(strike_val - target_price)
            if diff < min_diff:
                min_diff = diff
                best_match = s_row
        except Exception as e:
            logger.debug("find_strike_by_distance: skipping bad strike row: %s", e)
            continue
    return best_match


def find_equal_premium_pair(
    ce_candidates: List[Dict],
    pe_candidates: List[Dict],
    ce_ltp_map: dict,
    pe_ltp_map: dict,
    fallback_ce: Dict,
    fallback_pe: Dict,
    spot: float = 0.0,
    exchange: str = "NFO",
    symbol: str = None,
    ce_bid_map: dict = None,
    ce_ask_map: dict = None,
    pe_bid_map: dict = None,
    pe_ask_map: dict = None,
    min_individual_premium: float = 0.0
) -> tuple:
    """
    Select the (CE, PE) strike pair where a multi-factor score is minimized.
    The score weights execution quality (spread), premium balance, symmetry,
    and underlying liquidity. Supports strict backward-compatible legacy mode
    if bid/ask maps are omitted.
    """
    def _normalize(row):
        sv = float(row.get('strike', 0))
        if exchange == 'NFO': sv /= 100.0
        return sv

    valid_pairs = []

    for ce_row in ce_candidates:
        ce_ltp = ce_ltp_map.get(id(ce_row), 0.0)
        # A 0 (or negative) LTP means no live quote was available, not a legitimately
        # free option — always reject it, independent of min_individual_premium (which
        # defaults to 0.0 and would otherwise let a missing quote through unfiltered).
        if ce_ltp <= 0 or ce_ltp < min_individual_premium:
            continue
        for pe_row in pe_candidates:
            pe_ltp = pe_ltp_map.get(id(pe_row), 0.0)
            if pe_ltp <= 0 or pe_ltp < min_individual_premium:
                continue
            
            valid_pairs.append((ce_row, pe_row, ce_ltp, pe_ltp))

    if not valid_pairs:
        return fallback_ce, fallback_pe, float('inf')

    def sort_key(item):
        ce_row, pe_row, ce_ltp, pe_ltp = item
        
        diff = abs(ce_ltp - pe_ltp)
        ce_oi = float(ce_row.get('open_interest', 0) or ce_row.get('oi', 0) or 0)
        pe_oi = float(pe_row.get('open_interest', 0) or pe_row.get('oi', 0) or 0)
        ce_vol = float(ce_row.get('trade_volume', 0) or ce_row.get('volume', 0) or 0)
        pe_vol = float(pe_row.get('trade_volume', 0) or pe_row.get('volume', 0) or 0)
        liquidity = ce_oi + pe_oi + ce_vol + pe_vol
        
        ce_strike = _normalize(ce_row)
        pe_strike = _normalize(pe_row)
        symmetry = abs(abs(ce_strike - spot) - abs(pe_strike - spot))
        combined_premium = ce_ltp + pe_ltp
        
        # 1. Backward-compatible legacy mode (Lexicographical)
        if ce_bid_map is None or pe_bid_map is None:
            return (round(diff, 4), -liquidity, round(symmetry, 4), -combined_premium)
            
        # 2. Advanced Multi-Factor Lexicographical Mode (Execution quality focus)
        ce_bid = ce_bid_map.get(id(ce_row), ce_ltp * 0.998)
        ce_ask = ce_ask_map.get(id(ce_row), ce_ltp * 1.002)
        pe_bid = pe_bid_map.get(id(pe_row), pe_ltp * 0.998)
        pe_ask = pe_ask_map.get(id(pe_row), pe_ltp * 1.002)
        
        ce_spread = (ce_ask - ce_bid) / max(0.01, ce_ltp)
        pe_spread = (pe_ask - pe_bid) / max(0.01, pe_ltp)
        spread_total = ce_spread + pe_spread
        
        # Bucket very close premiums (within 0.10) to let spread quality choose the best fill
        rounded_diff = round(diff, 1)
        
        return (rounded_diff, round(spread_total, 4), -liquidity, round(symmetry, 4), -combined_premium)

    valid_pairs.sort(key=sort_key)
    best_ce, best_pe, ce_best_ltp, pe_best_ltp = valid_pairs[0]
    best_diff = abs(ce_best_ltp - pe_best_ltp)

    return best_ce, best_pe, best_diff

def build_specialist_hedge(symbol: str, exchange: str, spot_price: float, orch, sigma: float = 0.20) -> List[Dict[str, Any]]:
    """
    90%+ Win-Rate Specialist Strategy:
    Sell a 2-leg OTM Strangle (Delta ~0.15-0.20) for premium collection.

    NAKED position (confirmed product decision — see doc/AUDIT_REMEDIATION_PLAN.md,
    Phase 1 item #8): this strangle carries NO purchased long option legs, so max
    theoretical loss on either side is unbounded — a large enough adverse move in
    the underlying is not capped by any owned option. An earlier version of this
    docstring claimed a "Buy Far OTM Protection" long leg capped max loss; that leg
    was computed but never attached to the persisted position, the claim was false,
    and it has been corrected here rather than the code changed to match it.

    Real risk controls that DO apply to this naked position (enforced elsewhere in
    this file / config_vol.py — none of them are an owned option, so none give a
    hard, gap-proof cap on loss the way a long leg would):
    - Systematic combined-premium stop loss: auto-exit when combined CE+PE premium
      expands COMBINED_SL_MULTIPLIER (+30% monthly) or INTRADAY_COMBINED_SL_MULT
      (+25% intraday) above entry.
    - Delta-danger auto-exit: forced close if either short leg's |delta| reaches
      SHORT_DELTA_DANGER (0.55) while AUTO_EXIT_ON_DELTA_BREACH is True.
    - Position-count cap: at most HEDGE_MAX_SIGNALS (default 10) concurrent
      strangle positions open at once.
    - Premium/notional viability floor (MIN_PREMIUM_PER_LEG / MIN_NOTIONAL_PER_LEG)
      rejects entries too small to be worth the margin — this bounds trade
      selection, not loss.
    Each of the above relies on a price/delta check firing and an exit order
    executing before the underlying gaps further, so slippage/gap risk on the exit
    itself is real and unbounded loss is not merely theoretical on a large enough move.

    ASSIGNMENT / PHYSICAL-SETTLEMENT RISK (audit fix C4): for a single-stock
    underlying (`symbol` not in config_vol.INDEX_UNDERLYINGS), the resulting short
    legs are NSE stock options — American-style and physically settled (SEBI mandate
    since 2019), unlike index options. A short leg that drifts or gaps ITM can be
    assigned before expiry, obligating physical delivery of the underlying rather
    than a cash settlement — a materially different (and potentially much larger)
    obligation than the premium collected. Nothing in this file distinguishes
    stock-option risk from index-option risk beyond the informational
    `assignment_risk` flag set on each leg during the audit loop
    (ASSIGNMENT_RISK_DELTA in config_vol.py) — that flag does NOT auto-exit the
    position, it only surfaces the warning. Trading this strategy on single-stock
    underlyings carries real assignment risk that the account owner must manage
    manually; this is disclosed here and in DeltaHedgePanel's UI copy rather than
    silently handled.
    """
    legs = []
    lot_size = get_lot_size(symbol, exchange)
    if lot_size <= 0:
        # No prior leg exists yet to fall back to (this is new-signal generation) —
        # abort rather than build a strangle whose target/SL and credit math would
        # be silently wrong (audit fix H18).
        logger.error("[SPECIALIST] %s: cannot resolve a real lot size — skipping strangle build.", symbol)
        return []
    calc_spot = spot_price
    _svc = get_truedata_instance()  # Local reference to avoid NameError

    strikes = get_nse_option_strikes(symbol, spot_price)
    if not strikes:
        return []

    # Expiry calculation for Greeks
    # Skip same-day expiry (t_days=0 or 1) — on expiry day all OTM options have
    # near-zero delta, so find_strike_by_delta fails. Use next-week expiry instead.
    try:
        today = timezone.now().astimezone(IST).date()

        # Rollover Rule: Roll to next month contract when current month has <= MIN_DAYS_TO_EXPIRY
        # trading days remaining. Shared with get_nse_option_strikes() via resolve_target_expiry()
        # (Audit Remediation Plan Phase 3 #3.2.4) so the rollover threshold can't drift between
        # the two call sites.
        target_expiry = resolve_target_expiry(
            [s.get('expiry', '') for s in strikes], MIN_DAYS_TO_EXPIRY
        )

        if target_expiry:
            strikes = [s for s in strikes if s.get('expiry') == target_expiry]
            expiry_dt = expiry_str_to_date(target_expiry)
            t_days = max(1, (expiry_dt - today).days)  # Minimum 1 day for Greek calculations
        else:
            t_days = 1

        logger.info("[SPECIALIST] %s using expiry %s (t_days=%d)", symbol, target_expiry, t_days)
    except Exception as exp_err:
        logger.warning("[SPECIALIST] Expiry parse error for %s: %s", symbol, exp_err)
        t_days = 5

    # 1. Gamma Risk Guard: Reject fresh entry generation if DTE is less than or equal to FORCE_EXIT_DTE
    if t_days <= FORCE_EXIT_DTE:
        logger.warning("[GAMMA_GUARD] Skipping strangle for %s: Days to expiry %d <= FORCE_EXIT_DTE %d (unmanaged final-day gamma risk)", symbol, t_days, FORCE_EXIT_DTE)
        return []

    if not strikes:
        return []

    # 2. Estimate Live IV and dynamic delta targets
    live_iv = sigma
    try:
        # Find ATM candidate to estimate live Implied Volatility
        def _get_strike(row):
            sv = float(row.get('strike', 0))
            if exchange == 'NFO': sv /= 100.0
            return sv

        atm_ce = min(strikes, key=lambda s: abs(_get_strike(s) - calc_spot))
        atm_strike_val = _get_strike(atm_ce)
        atm_res = orch.get_option_data(symbol, atm_strike_val, 'CE', atm_ce['expiry'], exchange)
        atm_quote = atm_res[0] if isinstance(atm_res, tuple) else atm_res
        atm_ltp = float(atm_quote.get('ltp', 0))
        if atm_ltp > 0:
            live_iv = estimate_iv(calc_spot, atm_strike_val, t_days, atm_ltp, 'CE')
            logger.info("[IV_ESTIMATE] Resolved live IV for %s: %.2f%% (baseline sigma %.2f%%)", symbol, live_iv * 100, sigma * 100)
    except Exception as iv_err:
        logger.warning("[IV_ESTIMATE] Failed to estimate live IV for %s: %s", symbol, iv_err)

    # ── 2a. INTRADAY IV REJECTION GUARD ──────────────────────────────────
    # Reject stocks with IV > 25% — selling strangles into high IV causes
    # both-sides premium expansion (as seen with ASIANPAINT -43% session loss).
    if live_iv > 0.25 and exchange == 'NFO':
        logger.warning(
            "[IV_GUARD] Rejecting %s: live IV %.2f%% exceeds 25%% safety cap "
            "(high IV = premium will expand against short strangle)",
            symbol, live_iv * 100
        )
        return []

    # ── 2b. INTRADAY RANGE REJECTION GUARD ───────────────────────────────
    # Reject stocks that have already moved > 1.5% intraday — they are
    # trending and will likely continue, blowing through strangle strikes.
    try:
        _price_data = orch.get_price(symbol, exchange=('NSE' if exchange == 'NFO' else exchange))
        if _price_data:
            _range_ltp = float(_price_data.get('ltp', 0) or 0)
            _range_high = float(_price_data.get('high', 0) or 0)
            _range_low = float(_price_data.get('low', 0) or 0)
            if _range_ltp > 0 and _range_high > _range_low > 0:
                _intraday_range_pct = (_range_high - _range_low) / _range_ltp * 100.0
                if _intraday_range_pct > 1.5:
                    logger.warning(
                        "[RANGE_GUARD] Rejecting %s: intraday range %.2f%% exceeds 1.5%% safety cap "
                        "(stock already moving too much for safe strangle)",
                        symbol, _intraday_range_pct
                    )
                    return []
                logger.debug("[RANGE_GUARD] %s intraday range: %.2f%% — OK", symbol, _intraday_range_pct)
    except Exception as _range_err:
        logger.warning("[RANGE_GUARD] Failed to check intraday range for %s: %s", symbol, _range_err)

    # 3. INTRADAY TIME-BASED ADAPTIVE DELTA (replaces fixed SHORT_DELTA)
    # Delta ramps up as the day progresses to capture faster same-day decay.
    target_short_delta = get_intraday_target_delta()
    time_regime = get_intraday_time_regime()

    # Override with IV-regime adjustment on top of time-based delta:
    # Low IV: even conservative strikes are fine (market not moving)
    # Panic IV: stay conservative — gamma risk very high
    if live_iv < 0.15:
        target_short_delta = max(INTRADAY_DELTA_MORNING, target_short_delta - 0.05)
        logger.info("[IV_REGIME] Low IV (%.2f%%) -> Using conservative delta: %.2f", live_iv * 100, target_short_delta)
    elif live_iv > 0.40:
        target_short_delta = min(INTRADAY_DELTA_MORNING, target_short_delta)  # panic IV: cap at 0.35
        logger.info("[IV_REGIME] Panic IV (%.2f%%) -> Capping delta at: %.2f to control gamma", live_iv * 100, target_short_delta)
    elif live_iv > 0.25:
        target_short_delta = min(INTRADAY_MAX_DELTA, target_short_delta + 0.03)  # High IV: slight bump, still capped
        logger.info("[IV_REGIME] High IV (%.2f%%) -> Bumped delta to: %.2f", live_iv * 100, target_short_delta)

    logger.info(
        "[INTRADAY] %s | Regime: %s | Base delta: %.2f | IV: %.2f%%",
        symbol, time_regime, target_short_delta, live_iv * 100
    )

    # 4. Gamma Guard: Cap delta near expiry (DTE <= 3)
    if t_days <= 3:
        old_delta = target_short_delta
        target_short_delta = min(target_short_delta, CAP_DELTA_NEAR_EXPIRY)
        logger.info("[GAMMA_GUARD] DTE %d <= 3 -> Delta capped %.2f -> %.2f to mitigate gamma explosion", t_days, old_delta, target_short_delta)

    if exchange == 'NFO':
        # Intraday expected move for same-day holding (1-day standard deviation).
        # Since positions are squared off today, we do not need to hold strikes outside the monthly EM.
        # This keeps the strikes much closer and ensures rich premium collections.
        expected_move = calc_spot * live_iv * math.sqrt(1.0 / 365.0)
    else:
        expected_move = calc_spot * live_iv * math.sqrt(t_days / 365.0)
        
    logger.info(
        "[EXPECTED_MOVE] %s: EM=₹%.2f (%.2f%%) | t_days=%d | IV=%.2f%%",
        symbol, expected_move, (expected_move / calc_spot) * 100, t_days, live_iv * 100
    )

    # --- 6. INTRADAY ADAPTIVE DELTA LOOP ---
    # Starts at time-based delta; compresses strikes toward ATM in steps
    # of 0.03 until combined premium floor is satisfied or hard cap reached.
    from stocks.services.config_vol import (
        MIN_COMBINED_PREMIUM_LARGECAP, MIN_COMBINED_PREMIUM_MIDCAP,
        MIN_COMBINED_PREMIUM_HIGHIV, MAX_TARGET_DELTA, MIN_DAILY_THETA_YIELD
    )

    # Intraday premium floor: session-aware (morning < midday < afternoon)
    min_combined_premium = get_intraday_min_premium(symbol)
    # Fall back to institutional floor if intraday floor is lower
    if live_iv > 0.25:
        min_combined_premium = max(min_combined_premium, MIN_COMBINED_PREMIUM_HIGHIV * 0.6)
    elif symbol in NIFTY_50_STOCKS() or symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
        min_combined_premium = max(min_combined_premium, MIN_COMBINED_PREMIUM_LARGECAP * 0.5)

    max_loop_delta = min(INTRADAY_MAX_DELTA, MAX_TARGET_DELTA)  # hard cap: 0.48
    sell_ce = None
    sell_pe = None

    # ── EQUIDISTANT STRIKE SELECTION ─────────────────────────────────────────
    # Pick CE and PE strikes at the SAME % distance from spot so premiums are
    # naturally balanced. We scan distance from 3% to 10% in 0.5% steps and
    # pick the closest equidistant pair whose LIVE premiums meet the floor.
    # Live premiums are fetched once per candidate; theoretical used as fallback.

    # Bulk warm-up option quotes to prevent WAF block on individual queries.
    # Chunked to <=50 tokens/request — Angel One's bulk quote endpoint caps there;
    # an unchunked call above ~50 (routine for wider option chains, e.g. BEL/CIPLA/
    # COALINDIA) silently under-fills the cache, so most strikes then fall through
    # to the slow single-token REST path in get_live_price_by_token (1.5s each).
    _svc_inst = get_truedata_instance()
    if _svc_inst and strikes:
        tokens_to_warm = [str(row["token"]) for row in strikes if row.get("token")]
        if tokens_to_warm:
            logger.info("[WARM-UP] Bulk pre-fetching %d option quotes for %s", len(tokens_to_warm), symbol)
            try:
                if _svc_inst.streamer:
                    _svc_inst.streamer.subscribe(2, tokens_to_warm)
                for chunk in itertools.batched(tokens_to_warm, 50):
                    _svc_inst.get_bulk_quotes({"NFO": chunk})
            except Exception as w_err:
                logger.warning("[WARM-UP] Bulk pre-fetch failed: %s", w_err)

    # Build a lookup: strike_value -> (row, live_ltp) for CE and PE
    ce_strike_map: dict[float, tuple] = {}  # strike_val -> (row, ltp)
    pe_strike_map: dict[float, tuple] = {}

    for row in strikes:
        sym_str = row.get('symbol', '')
        sv = _get_strike(row)
        if sv <= 0:
            continue
        if sym_str.endswith('CE') and sv > calc_spot:
            res = orch.get_option_data(symbol, sv, 'CE', row['expiry'], exchange)
            q = res[0] if isinstance(res, tuple) else res
            ltp = float(q.get('ltp', 0)) if q else 0.0
            if ltp > 0:
                ce_strike_map[sv] = (row, ltp)
        elif sym_str.endswith('PE') and sv < calc_spot:
            res = orch.get_option_data(symbol, sv, 'PE', row['expiry'], exchange)
            q = res[0] if isinstance(res, tuple) else res
            ltp = float(q.get('ltp', 0)) if q else 0.0
            if ltp > 0:
                pe_strike_map[sv] = (row, ltp)

    # Dynamic Scan Range based on Beta/Volatility profile
    # CRITICAL FIX: Raised minimum OTM distance from 1.5-2% to 4-5% to prevent
    # strikes from landing dangerously close to ATM (BAJAJFINSV at 1.4% OTM caused -34% loss).
    # IT and Low-Beta stocks scan 3.5% to 8% OTM.
    # High-Beta stocks scan 4.5% to 10% OTM for extra safety margin.
    is_low_beta = symbol in [
        "INFY", "TECHM", "TCS", "HCLTECH", "LTIM", "WIPRO",
        "HINDUNILVR", "ITC", "NESTLEIND",
        "BEL", "POWERGRID", "NTPC", "COALINDIA",  # PSU/Defence — low intraday vol
    ]
    min_dist_pct = 0.020 if is_low_beta else 0.025
    max_dist_pct = 0.050 if is_low_beta else 0.060
    
    # Expiry Proximity Compression: If DTE <= 5, allow slightly closer but NEVER below 1.5% OTM.
    if t_days <= 5:
        min_dist_pct = 0.015  # 1.5% floor near expiry
        
    dist_steps = [d / 1000.0 for d in range(int(min_dist_pct * 1000), int(max_dist_pct * 1000) + 5, 5)]

    best_pair_ce = None
    best_pair_pe = None
    best_pair_diff = float('inf')
    best_pair_combined = 0.0
    best_pair_dist = float('inf')

    ce_strikes_avail = sorted(ce_strike_map.keys())   # ascending
    pe_strikes_avail = sorted(pe_strike_map.keys())   # ascending

    if not ce_strikes_avail or not pe_strikes_avail:
        logger.warning("[EQUIDISTANT] No OTM strikes available for %s — rejecting", symbol)
        return []

    MIN_INDIVIDUAL_PREMIUM = 10.00 if t_days <= 5 else 5.00  # ₹5 floor per leg (was ₹3 — too thin, BEL had ₹4.85 PE)
    for dist_pct in dist_steps:
        ce_target = calc_spot * (1 + dist_pct)
        pe_target = calc_spot * (1 - dist_pct)

        # Snap to INNER strike: largest CE ≤ ce_target (closer to spot)
        # If none exists below target, take the smallest available CE
        inner_ce = [x for x in ce_strikes_avail if x <= ce_target]
        ce_sv = inner_ce[-1] if inner_ce else ce_strikes_avail[0]

        # Snap to INNER strike: smallest PE ≥ pe_target (closer to spot)
        # If none exists above target, take the largest available PE
        inner_pe = [x for x in pe_strikes_avail if x >= pe_target]
        pe_sv = inner_pe[0] if inner_pe else pe_strikes_avail[-1]

        ce_row, ce_ltp = ce_strike_map[ce_sv]
        pe_row, pe_ltp = pe_strike_map[pe_sv]
        combined = ce_ltp + pe_ltp
        diff = abs(ce_ltp - pe_ltp)
        max_ltp = max(ce_ltp, pe_ltp)
        imbalance = diff / max_ltp if max_ltp > 0 else 0.0

        # Check if this pair meets basic quality and premium floors
        has_min_individual = (ce_ltp >= MIN_INDIVIDUAL_PREMIUM and pe_ltp >= MIN_INDIVIDUAL_PREMIUM)
        has_min_combined = (combined >= min_combined_premium)
        is_balanced = (imbalance <= INTRADAY_PREMIUM_ASYMMETRY_TOLERANCE or diff <= 0.50)

        if has_min_individual and has_min_combined and is_balanced:
            best_pair_ce = ce_row
            best_pair_pe = pe_row
            best_pair_diff = diff
            best_pair_combined = combined
            best_pair_dist = dist_pct
            best_ce_ltp = ce_ltp
            best_pe_ltp = pe_ltp
            logger.info("[EQUIDISTANT] Found optimal close-balanced pair at dist=%.1f%%", dist_pct * 100)
            break

        # Fallback tracking
        if diff < best_pair_diff or (diff == best_pair_diff and dist_pct < best_pair_dist):
            best_pair_ce = ce_row
            best_pair_pe = pe_row
            best_pair_diff = diff
            best_pair_combined = combined
            best_pair_dist = dist_pct
            best_ce_ltp = ce_ltp
            best_pe_ltp = pe_ltp

    if best_pair_ce and best_pair_pe:
        sell_ce = best_pair_ce
        sell_pe = best_pair_pe
        logger.info(
            "[EQUIDISTANT] %s: dist=%.1f%% | CE ₹%.2f / PE ₹%.2f | diff=₹%.2f | combined=₹%.2f",
            symbol, best_pair_dist * 100,
            best_ce_ltp, best_pe_ltp,
            best_pair_diff, best_pair_combined
        )
    else:
        logger.warning(
            "[EQUIDISTANT] No equidistant pair found for %s — no strikes available, rejecting",
            symbol
        )
        return []

    # If equidistant search failed to find any strikes, reject
    if not sell_ce or not sell_pe:
        logger.warning("[BUILD_HEDGE] No viable CE/PE strikes found for %s (spot=%.2f) — rejecting", symbol, calc_spot)
        return []

    # Re-verify premium target viability with live prices
    ce_strike_check = _get_strike(sell_ce)
    pe_strike_check = _get_strike(sell_pe)
    check_ce_res = orch.get_option_data(symbol, ce_strike_check, 'CE', sell_ce['expiry'] if sell_ce else target_expiry, exchange)
    check_pe_res = orch.get_option_data(symbol, pe_strike_check, 'PE', sell_pe['expiry'] if sell_pe else target_expiry, exchange)
    quote_ce = check_ce_res[0] if isinstance(check_ce_res, tuple) else check_ce_res
    quote_pe = check_pe_res[0] if isinstance(check_pe_res, tuple) else check_pe_res
    premium_ce = float(quote_ce.get('ltp', 0))
    premium_pe = float(quote_pe.get('ltp', 0))

    # ── PER-LEG MINIMUM PREMIUM CHECK (₹5.00 FLOOR) ──────────────────────────
    # For cheaper stocks, the equidistant search can pick strikes with individual
    # premiums below ₹5.00 (e.g. HDFCBANK ₹4.45 / PE ₹4.75). Options under ₹5.00
    # have very poor decay metrics. If either CE or PE premium is below ₹5.00
    # and it is not a commodity, we attempt to step strikes CLOSER to ATM
    # (down to a minimum of 1.5% OTM distance) to pull both legs above ₹5.00.
    MIN_INDIVIDUAL_PREMIUM = 10.00 if t_days <= 5 else 3.00  # ₹3.00 floor per leg, raised to ₹10.00 near expiry for higher premium/profit
    if exchange == 'NFO' and (premium_ce < MIN_INDIVIDUAL_PREMIUM or premium_pe < MIN_INDIVIDUAL_PREMIUM):
        logger.info(
            "[LEG_PREMIUM] %s: CE=₹%.2f, PE=₹%.2f below ₹%.2f floor — trying closer strikes",
            symbol, premium_ce, premium_pe, MIN_INDIVIDUAL_PREMIUM
        )
        # Search for tighter strikes inward to satisfy the ₹5.00 per leg target
        closer_sell_ce, closer_sell_pe = None, None
        closer_ce_ltp, closer_pe_ltp = 0.0, 0.0
        
        # Scan OTM distances starting just inside best_pair_dist down to 3% (or 2.5% near expiry)
        # CRITICAL FIX: was 0.4%/1.4% — allowed near-ATM strikes causing massive losses
        start_scan = int((best_pair_dist - 0.005) * 1000)
        min_retry_limit = 25 if t_days <= 5 else 30
        for retry_dist_pct in [d / 1000.0 for d in range(start_scan, min_retry_limit, -5)]:
            ce_t = calc_spot * (1 + retry_dist_pct)
            pe_t = calc_spot * (1 - retry_dist_pct)
            inner_c = [x for x in ce_strikes_avail if x <= ce_t]
            inner_p = [x for x in pe_strikes_avail if x >= pe_t]
            if not inner_c or not inner_p:
                continue
            c_sv = inner_c[-1]
            p_sv = inner_p[0]
            c_row, c_ltp = ce_strike_map.get(c_sv, (None, 0))
            p_row, p_ltp = pe_strike_map.get(p_sv, (None, 0))
            if not c_row or not p_row:
                continue
            
            logger.info(
                "[LEG_PREMIUM] %s retry dist=%.1f%% → CE ₹%.2f, PE ₹%.2f",
                symbol, retry_dist_pct * 100, c_ltp, p_ltp
            )
            if c_ltp >= MIN_INDIVIDUAL_PREMIUM and p_ltp >= MIN_INDIVIDUAL_PREMIUM:
                closer_sell_ce, closer_sell_pe = c_row, p_row
                closer_ce_ltp, closer_pe_ltp = c_ltp, p_ltp
                sell_ce, sell_pe = closer_sell_ce, closer_sell_pe
                premium_ce, premium_pe = closer_ce_ltp, closer_pe_ltp
                logger.info(
                    "[LEG_PREMIUM] %s: Successfully adjusted strikes inward to %.1f%% OTM — CE ₹%.2f / PE ₹%.2f",
                    symbol, retry_dist_pct * 100, premium_ce, premium_pe
                )
                break
        else:
            # All tighter retries exhausted — premiums still under ₹5.00, reject
            logger.warning(
                "[LEG_PREMIUM] %s: Rejecting. Cannot pull both legs above ₹%.2f even at closest strikes",
                symbol, MIN_INDIVIDUAL_PREMIUM
            )
            return []

    if premium_ce + premium_pe < min_combined_premium:
        logger.warning("[PREMIUM_FILTER] Rejecting Strangle: combined premium ₹%.2f below institutional minimum target ₹%.2f", premium_ce + premium_pe, min_combined_premium)
        return []

    # ── LOT-WEIGHTED NET CREDIT CHECK ────────────────────────────────────────
    # For low-spot stocks (BPCL ₹298, BAJFINANCE ₹897) the per-unit premium
    # can look adequate but the total credit collected (premium × lot_size) is
    # too small to be meaningful. If net_credit × lot_size < ₹5,000 we
    # automatically step the strikes CLOSER to ATM (reduce OTM distance by 0.5%
    # per attempt, up to 3 retries) to get more premium.  If we still can't
    # reach the minimum after retries, we reject the strangle.
    MIN_LOT_WEIGHTED_CREDIT = 2500.0   # Minimum total credit ₹ per strangle (was ₹5,000 — blocked low-premium/small-lot stocks)
    net_credit_total = (premium_ce + premium_pe) * lot_size
    if net_credit_total < MIN_LOT_WEIGHTED_CREDIT:
        logger.info(
            "[LOT_CREDIT] %s: net_credit=₹%.2f × lot=%d = ₹%.0f < ₹%.0f minimum — trying closer strikes",
            symbol, premium_ce + premium_pe, lot_size, net_credit_total, MIN_LOT_WEIGHTED_CREDIT
        )
        # Re-run equidistant search from the next tighter distance inward
        closer_sell_ce, closer_sell_pe = None, None
        closer_ce_ltp, closer_pe_ltp = 0.0, 0.0
        start_scan = int((best_pair_dist - 0.005) * 1000)
        min_retry_limit = 25 if t_days <= 5 else 30
        min_retry_dist = 0.025 if t_days <= 5 else 0.03  # Never retry below 2.5-3% OTM (was 0.5-1% — too aggressive)
        for retry_dist_pct in [d / 1000.0 for d in range(start_scan, min_retry_limit, -5)]:
            if retry_dist_pct <= min_retry_dist:
                break
            ce_t = calc_spot * (1 + retry_dist_pct)
            pe_t = calc_spot * (1 - retry_dist_pct)
            inner_c = [x for x in ce_strikes_avail if x <= ce_t]
            inner_p = [x for x in pe_strikes_avail if x >= pe_t]
            if not inner_c or not inner_p:
                continue
            c_sv = inner_c[-1]
            p_sv = inner_p[0]
            c_row, c_ltp = ce_strike_map.get(c_sv, (None, 0))
            p_row, p_ltp = pe_strike_map.get(p_sv, (None, 0))
            if not c_row or not p_row:
                continue
            retry_credit_total = (c_ltp + p_ltp) * lot_size
            logger.info(
                "[LOT_CREDIT] %s retry dist=%.1f%% → CE₹%.2f + PE₹%.2f × %d = ₹%.0f",
                symbol, retry_dist_pct * 100, c_ltp, p_ltp, lot_size, retry_credit_total
            )
            if retry_credit_total >= MIN_LOT_WEIGHTED_CREDIT:
                closer_sell_ce, closer_sell_pe = c_row, p_row
                closer_ce_ltp, closer_pe_ltp = c_ltp, p_ltp
                sell_ce, sell_pe = closer_sell_ce, closer_sell_pe
                premium_ce, premium_pe = closer_ce_ltp, closer_pe_ltp
                logger.info(
                    "[LOT_CREDIT] %s: Using closer strikes at %.1f%% OTM — new credit ₹%.0f",
                    symbol, retry_dist_pct * 100, retry_credit_total
                )
                break
        else:
            # All retries exhausted — credit still too low, reject
            logger.warning(
                "[LOT_CREDIT] %s: Cannot achieve ₹%.0f minimum credit even at closest strikes — rejecting",
                symbol, MIN_LOT_WEIGHTED_CREDIT
            )
            return []
    # ─────────────────────────────────────────────────────────────────────────

    # --- 7. INTRADAY EXPECTED MOVE STRIKE CAPS ---
    # Intraday: reject ultra-far OTM beyond 1.5x EM (default)
    # Panic IV (>35%): allow up to 2.0x EM — IV is high enough to justify wider strikes
    em_cap_mult = INTRADAY_EM_HIGH_IV_CAP_MULT if live_iv > 0.35 else INTRADAY_EM_CAP_MULT

    max_ce_strike = calc_spot + em_cap_mult * expected_move
    min_pe_strike = calc_spot - em_cap_mult * expected_move

    ce_val = _get_strike(sell_ce)
    pe_val = _get_strike(sell_pe)

    if ce_val > max_ce_strike:
        valid_ce = [s for s in strikes if s.get('symbol', '').endswith('CE') and calc_spot < _get_strike(s) <= max_ce_strike]
        if valid_ce:
            sell_ce = max(valid_ce, key=lambda s: _get_strike(s))
            logger.info("[EM_CAP] Capped CE %.2f -> %.2f (limit=%.2f @ %.1fx EM)", ce_val, _get_strike(sell_ce), max_ce_strike, em_cap_mult)
    if pe_val < min_pe_strike:
        valid_pe = [s for s in strikes if s.get('symbol', '').endswith('PE') and min_pe_strike <= _get_strike(s) < calc_spot]
        if valid_pe:
            sell_pe = min(valid_pe, key=lambda s: _get_strike(s))
            logger.info("[EM_CAP] Capped PE %.2f -> %.2f (limit=%.2f @ %.1fx EM)", pe_val, _get_strike(sell_pe), min_pe_strike, em_cap_mult)

    # --- 8. PREMIUM VELOCITY FILTER (Daily Theta Yield) ---
    # Audit finding M5: margin_required is a flat 10%-of-notional HEURISTIC, not
    # real SPAN/exposure margin (which is volatility- and moneyness-dependent) — no
    # broker margin API is integrated here. This can screen trades wrong in both
    # directions: a low-vol strike's real margin is likely well under 10% of
    # notional (yield understated -> good trades wrongly rejected below), while a
    # high-vol/near-the-money strike's real margin can exceed 10% (yield
    # overstated -> a trade that wouldn't actually clear the real yield bar gets
    # accepted). Left as a reject-only heuristic deliberately: erring toward
    # rejecting more candidates is the safer failure direction for a risk-sensitive
    # gate than loosening it without a real margin source to replace it.
    margin_required = 0.10 * calc_spot * lot_size
    ce_gr = calculate_greeks(calc_spot, _get_strike(sell_ce), t_days, sigma=live_iv, option_type='CE')
    pe_gr = calculate_greeks(calc_spot, _get_strike(sell_pe), t_days, sigma=live_iv, option_type='PE')
    total_theta_sold = -float(ce_gr.get('theta', 0.0)) - float(pe_gr.get('theta', 0.0))

    if margin_required > 0:
        daily_theta_yield = (total_theta_sold * lot_size) / margin_required
        if daily_theta_yield < MIN_DAILY_THETA_YIELD:
            logger.warning("[VELOCITY_FILTER] Rejecting Strangle: Strangle Daily Theta Yield %.6f < min %.6f (low premium velocity)", daily_theta_yield, MIN_DAILY_THETA_YIELD)
            return []


    # Fallback: If delta selection fails, use distance-based
    if not sell_ce:
        logger.warning("[SPECIALIST] Delta selection failed for CE, falling back to %.1f%% OTM", FALLBACK_OTM_PCT * 100)
        sell_ce = find_strike_by_distance(strikes, calc_spot, FALLBACK_OTM_PCT, 'CE', symbol)
    if not sell_pe:
        logger.warning("[SPECIALIST] Delta selection failed for PE, falling back to %.1f%% OTM", FALLBACK_OTM_PCT * 100)
        sell_pe = find_strike_by_distance(strikes, calc_spot, FALLBACK_OTM_PCT, 'PE', symbol)

    if not sell_ce or not sell_pe:
        logger.warning("[SPECIALIST] Could not find valid CE/PE strikes for %s — skipping", symbol)
        return []

    # --- 1.5. OPTIMAL MULTI-FACTOR EQUAL-PREMIUM & SPREAD PAIR SEARCH ---
    try:
        # OTM-only candidates, nearest-to-spot first
        # Expanded candidate pool: search wider than baseline strikes to ensure premium balancing can find match
        baseline_ce_strike = _get_strike(sell_ce) if sell_ce else (calc_spot * 1.05)
        baseline_pe_strike = _get_strike(sell_pe) if sell_pe else (calc_spot * 0.95)

        max_ce_limit = max(baseline_ce_strike, calc_spot + (baseline_ce_strike - calc_spot) * 1.5, calc_spot * 1.05)
        min_pe_limit = min(baseline_pe_strike, calc_spot - (calc_spot - baseline_pe_strike) * 1.5, calc_spot * 0.95)

        ce_candidates = sorted(
            [s for s in strikes if s.get('symbol', '').endswith('CE') and calc_spot < _get_strike(s) <= max_ce_limit],
            key=lambda s: _get_strike(s) - calc_spot
        )[:PREMIUM_BALANCE_PROBE]

        pe_candidates = sorted(
            [s for s in strikes if s.get('symbol', '').endswith('PE') and min_pe_limit <= _get_strike(s) < calc_spot],
            key=lambda s: calc_spot - _get_strike(s)
        )[:PREMIUM_BALANCE_PROBE]

        # Dynamic Bulk Pre-Fetch to populate the service stream cache
        try:
            tokens_to_prefetch = []
            for s in ce_candidates + pe_candidates:
                if s.get("token"):
                    tokens_to_prefetch.append(str(s["token"]))
            if tokens_to_prefetch and _svc:
                bulk_map = {exchange: tokens_to_prefetch}
                _svc.get_bulk_quotes(bulk_map)
        except Exception as bulk_err:
            logger.warning("[BULK_PREFETCH] Bulk pre-fetch failed for %s: %s", symbol, bulk_err)

        def _fetch_rounded_option(s_row, side):
            """One API call per candidate; fetch complete quote data robustly."""
            try:
                val = _get_strike(s_row)
                res = orch.get_option_data(symbol, val, side, s_row['expiry'], exchange)
                quote = res[0] if isinstance(res, tuple) else res
                ltp = float(quote.get('ltp', 0))
                bid = float(quote.get('bid') or ltp * 0.998)
                ask = float(quote.get('ask') or ltp * 1.002)

                return {
                    'ltp': round_to_tick(ltp, 0.05),
                    'bid': round_to_tick(bid, 0.05),
                    'ask': round_to_tick(ask, 0.05)
                }
            except Exception:
                return {'ltp': 0.0, 'bid': 0.0, 'ask': 0.0}

        # Build maps: n + n API calls
        ce_quotes = {id(s): _fetch_rounded_option(s, 'CE') for s in ce_candidates}
        pe_quotes = {id(s): _fetch_rounded_option(s, 'PE') for s in pe_candidates}

        ce_ltp_map = {k: v['ltp'] for k, v in ce_quotes.items()}
        pe_ltp_map = {k: v['ltp'] for k, v in pe_quotes.items()}
        ce_bid_map = {k: v['bid'] for k, v in ce_quotes.items()}
        ce_ask_map = {k: v['ask'] for k, v in ce_quotes.items()}
        pe_bid_map = {k: v['bid'] for k, v in pe_quotes.items()}
        pe_ask_map = {k: v['ask'] for k, v in pe_quotes.items()}

        # Filter out candidates with bad execution spreads (MAX_SPREAD_PCT)
        filtered_ce_candidates = []
        for s in ce_candidates:
            q = ce_quotes.get(id(s), {})
            ltp = q.get('ltp', 0)
            bid = q.get('bid', 0)
            ask = q.get('ask', 0)
            if ltp > 0:
                spread = (ask - bid) / ltp
                if spread <= MAX_SPREAD_PCT:
                    filtered_ce_candidates.append(s)
                else:
                    logger.info("[SPREAD_GUARD] Filtering out CE strike %.2f due to wide spread: %.2f%%", _get_strike(s), spread * 100)
        
        filtered_pe_candidates = []
        for s in pe_candidates:
            q = pe_quotes.get(id(s), {})
            ltp = q.get('ltp', 0)
            bid = q.get('bid', 0)
            ask = q.get('ask', 0)
            if ltp > 0:
                spread = (ask - bid) / ltp
                if spread <= MAX_SPREAD_PCT:
                    filtered_pe_candidates.append(s)
                else:
                    logger.info("[SPREAD_GUARD] Filtering out PE strike %.2f due to wide spread: %.2f%%", _get_strike(s), spread * 100)

        # Fallback to unfiltered lists if spread filtering was too restrictive
        final_ce_pool = filtered_ce_candidates if filtered_ce_candidates else ce_candidates
        final_pe_pool = filtered_pe_candidates if filtered_pe_candidates else pe_candidates

        best_ce, best_pe, min_diff = find_equal_premium_pair(
            final_ce_pool, final_pe_pool, ce_ltp_map, pe_ltp_map, sell_ce, sell_pe,
            spot=calc_spot, exchange=exchange, symbol=symbol,
            ce_bid_map=ce_bid_map, ce_ask_map=ce_ask_map, pe_bid_map=pe_bid_map, pe_ask_map=pe_ask_map,
            min_individual_premium=MIN_INDIVIDUAL_PREMIUM if exchange == 'NFO' else 0.0
        )

        if min_diff < float('inf'):
            sell_ce, sell_pe = best_ce, best_pe
            ce_best = ce_ltp_map.get(id(sell_ce), 0.0)
            pe_best = pe_ltp_map.get(id(sell_pe), 0.0)
            
            ce_strike = _get_strike(sell_ce)
            pe_strike = _get_strike(sell_pe)
            
            logger.info(
                "[STRANGLE_SELECTION] Multi-Factor Optimal Pair resolved for %s | Selected CE: %.2f @ ₹%.2f | Selected PE: %.2f @ ₹%.2f | Premium Diff: ₹%.2f | Live IV: %.2f%%",
                symbol, ce_strike, ce_best, pe_strike, pe_best, min_diff, live_iv * 100
            )

            if min_diff > MAX_PREMIUM_DIFF_WARN:
                logger.warning(
                    "[BALANCE] %s: Best CE/PE premium diff ₹%.2f exceeds limit ₹%.2f",
                    symbol, min_diff, MAX_PREMIUM_DIFF_WARN
                )
        else:
            logger.warning("[BALANCE] Multi-Factor pair search failed; using fallback strikes.")

    except Exception as bal_err:
        logger.warning("[BALANCE] Optimal pair search exception: %s", bal_err)

    # Post-Optimizer Risk Controls: Expected Move Floor & Symmetric Normalization
    try:
        ce_val = _get_strike(sell_ce)
        pe_val = _get_strike(sell_pe)
        adjusted = False
        if ce_val < calc_spot + expected_move:
            valid_ce_strikes = [s for s in strikes if s.get('symbol', '').endswith('CE') and _get_strike(s) >= calc_spot + expected_move]
            if valid_ce_strikes:
                new_ce = min(valid_ce_strikes, key=lambda s: _get_strike(s))
                logger.info("[EXPECTED_MOVE_FLOOR] Adjusted CE strike %.2f -> %.2f to stand outside Expected Move floor (Rs. %.2f)", ce_val, _get_strike(new_ce), expected_move)
                sell_ce = new_ce
                adjusted = True
        if pe_val > calc_spot - expected_move:
            valid_pe_strikes = [s for s in strikes if s.get('symbol', '').endswith('PE') and _get_strike(s) <= calc_spot - expected_move]
            if valid_pe_strikes:
                new_pe = max(valid_pe_strikes, key=lambda s: _get_strike(s))
                logger.info("[EXPECTED_MOVE_FLOOR] Adjusted PE strike %.2f -> %.2f to stand outside Expected Move floor (Rs. %.2f)", pe_val, _get_strike(new_pe), expected_move)
                sell_pe = new_pe
                adjusted = True

        # Always check and balance premiums if the imbalance exceeds tolerance (10%)
        # or if the absolute difference is significant (e.g. > 0.50).
        ce_val = _get_strike(sell_ce)
        pe_val = _get_strike(sell_pe)
        
        # Fetch current live premiums for these baseline adjusted strikes
        check_ce_res = orch.get_option_data(symbol, ce_val, 'CE', sell_ce['expiry'] if sell_ce else target_expiry, exchange)
        check_pe_res = orch.get_option_data(symbol, pe_val, 'PE', sell_pe['expiry'] if sell_pe else target_expiry, exchange)
        q_ce = check_ce_res[0] if isinstance(check_ce_res, tuple) else check_ce_res
        q_pe = check_pe_res[0] if isinstance(check_pe_res, tuple) else check_pe_res
        prem_ce = float(q_ce.get('ltp', 0))
        prem_pe = float(q_pe.get('ltp', 0))
        
        max_prem = max(prem_ce, prem_pe)
        imbalance_ratio = abs(prem_ce - prem_pe) / max_prem if max_prem > 0 else 0.0
        
        # Symmetrically evaluate both PE and CE adjustments for optimal premium match
        should_rebalance = adjusted or (imbalance_ratio > 0.10 and abs(prem_ce - prem_pe) > 0.50)
        
        if should_rebalance:
            logger.info("[FLOOR_REBALANCE] Asymmetry detected (CE: %.2f @ ₹%.2f vs PE: %.2f @ ₹%.2f, ratio: %.1f%%). Re-balancing...", ce_val, prem_ce, pe_val, prem_pe, imbalance_ratio * 100)
            
            # Option 1: Adjust the PE side (CE stays fixed at sell_ce)
            valid_pes = [s for s in strikes if s.get('symbol', '').endswith('PE') and _get_strike(s) <= calc_spot - expected_move]
            # Option 2: Adjust the CE side (PE stays fixed at sell_pe)
            valid_ces = [s for s in strikes if s.get('symbol', '').endswith('CE') and _get_strike(s) >= calc_spot + expected_move]
            
            # Bulk pre-fetch for rebalancing candidates to avoid WAF rate limits on individual quote calls
            try:
                rebalance_tokens = []
                for s in valid_pes + valid_ces:
                    if s.get("token"):
                        rebalance_tokens.append(str(s["token"]))
                if rebalance_tokens and _svc:
                    logger.info("[REBALANCE_WARMUP] Bulk pre-fetching %d rebalancing options for %s", len(rebalance_tokens), symbol)
                    for chunk in itertools.batched(rebalance_tokens, 50):
                        _svc.get_bulk_quotes({exchange: chunk})
            except Exception as rebal_prefetch_err:
                logger.warning("[REBALANCE_PREFETCH] Bulk pre-fetch failed: %s", rebal_prefetch_err)

            best_pe_match = sell_pe
            min_pe_diff = abs(prem_ce - prem_pe)
            for pe_cand in valid_pes:
                pe_cand_strike = _get_strike(pe_cand)
                pe_cand_res = orch.get_option_data(symbol, pe_cand_strike, 'PE', pe_cand['expiry'], exchange)
                pe_cand_q = pe_cand_res[0] if isinstance(pe_cand_res, tuple) else pe_cand_res
                pe_cand_ltp = float(pe_cand_q.get('ltp', 0))
                if pe_cand_ltp <= 0:
                    continue

                diff = abs(prem_ce - pe_cand_ltp)
                if diff < min_pe_diff:
                    min_pe_diff = diff
                    best_pe_match = pe_cand

            best_ce_match = sell_ce
            min_ce_diff = abs(prem_ce - prem_pe)
            for ce_cand in valid_ces:
                ce_cand_strike = _get_strike(ce_cand)
                ce_cand_res = orch.get_option_data(symbol, ce_cand_strike, 'CE', ce_cand['expiry'], exchange)
                ce_cand_q = ce_cand_res[0] if isinstance(ce_cand_res, tuple) else ce_cand_res
                ce_cand_ltp = float(ce_cand_q.get('ltp', 0))
                if ce_cand_ltp <= 0:
                    continue

                diff = abs(prem_pe - ce_cand_ltp)
                if diff < min_ce_diff:
                    min_ce_diff = diff
                    best_ce_match = ce_cand

            # Choose the adjustment direction that yields the absolute minimum premium difference
            if min_pe_diff < min_ce_diff:
                if best_pe_match != sell_pe:
                    logger.info("[FLOOR_REBALANCE] Selected PE adjustment: PE %.2f -> %.2f to match CE premium (₹%.2f)", pe_val, _get_strike(best_pe_match), prem_ce)
                    sell_pe = best_pe_match
            else:
                if best_ce_match != sell_ce:
                    logger.info("[FLOOR_REBALANCE] Selected CE adjustment: CE %.2f -> %.2f to match PE premium (₹%.2f)", ce_val, _get_strike(best_ce_match), prem_pe)
                    sell_ce = best_ce_match
        
    except Exception as floor_err:
        logger.warning("[EXPECTED_MOVE_FLOOR] Floor adjustment failed: %s", floor_err)

    # Final guard: Reject the strangle if either final adjusted leg falls below the MIN_INDIVIDUAL_PREMIUM (₹3.00)
    final_ce_val = _get_strike(sell_ce)
    final_pe_val = _get_strike(sell_pe)
    check_ce_res = orch.get_option_data(symbol, final_ce_val, 'CE', sell_ce['expiry'] if sell_ce else target_expiry, exchange)
    check_pe_res = orch.get_option_data(symbol, final_pe_val, 'PE', sell_pe['expiry'] if sell_pe else target_expiry, exchange)
    q_ce = check_ce_res[0] if isinstance(check_ce_res, tuple) else check_ce_res
    q_pe = check_pe_res[0] if isinstance(check_pe_res, tuple) else check_pe_res
    final_prem_ce = float(q_ce.get('ltp', 0)) if q_ce else 0.0
    final_prem_pe = float(q_pe.get('ltp', 0)) if q_pe else 0.0

    if final_prem_ce <= 0 or final_prem_pe <= 0:
        logger.warning(
            "[EXPECTED_MOVE_FLOOR] Rejecting %s: no live quote for final adjusted legs (CE=%.2f, PE=%.2f)",
            symbol, final_prem_ce, final_prem_pe
        )
        return []

    if exchange == 'NFO' and (final_prem_ce < MIN_INDIVIDUAL_PREMIUM or final_prem_pe < MIN_INDIVIDUAL_PREMIUM):
        logger.warning(
            "[EXPECTED_MOVE_FLOOR] Rejecting %s: final adjusted premiums (CE: %.2f @ ₹%.2f, PE: %.2f @ ₹%.2f) fell below individual leg floor ₹%.2f",
            symbol, final_ce_val, final_prem_ce, final_pe_val, final_prem_pe, MIN_INDIVIDUAL_PREMIUM
        )
        return []

    # Symmetric Normalization Disabled: Prioritize premium-balanced/delta-neutral strangles
    # over equidistant-strike strangles to keep CE and PE prices closely matched.
    # try:
    #     ce_val = _get_strike(sell_ce)
    #     pe_val = _get_strike(sell_pe)
    # 
    #     if calc_spot > 0:
    #         ce_dist = (ce_val - calc_spot) / calc_spot
    #         pe_dist = (calc_spot - pe_val) / calc_spot
    #         min_dist = min(ce_dist, pe_dist)
    # 
    #         if ce_dist > pe_dist * 1.20 and min_dist > 0:
    #             new_ce = find_strike_by_distance(strikes, calc_spot, min_dist, 'CE', symbol)
    #             if new_ce:
    #                 logger.info("[SYMMETRIC] CE re-selected %.2f→%.2f to match PE (%.1f%% OTM)", ce_val, _get_strike(new_ce), pe_dist * 100)
    #                 sell_ce = new_ce
    #         elif pe_dist > ce_dist * 1.20 and min_dist > 0:
    #             new_pe = find_strike_by_distance(strikes, calc_spot, min_dist, 'PE', symbol)
    #             if new_pe:
    #                 logger.info("[SYMMETRIC] PE re-selected %.2f→%.2f to match CE (%.1f%% OTM)", pe_val, _get_strike(new_pe), ce_dist * 100)
    #                 sell_pe = new_pe
    # except Exception as sym_err:
    #     logger.warning("[SYMMETRIC] Normalization failed for %s: %s", symbol, sym_err)

    def make_leg_entry(row, side, action):
        s_val = float(row['strike'])
        if exchange == 'NFO': s_val /= 100.0
        greeks = calculate_greeks(spot_price, s_val, t_days, sigma=sigma, option_type=side)
        
        # Fetch current premium to use as the baseline sell_price
        opt_res_raw = orch.get_option_data(symbol, s_val, side, row['expiry'], exchange)
        opt_res = opt_res_raw[0] if isinstance(opt_res_raw, tuple) else opt_res_raw
        token = opt_res_raw[1] if isinstance(opt_res_raw, tuple) else None
        premium = float(opt_res.get('ltp', 0))

        # Apply ₹0.05 tick rounding to all stored prices so display, P&L, and trading values
        # are always valid NSE option tick-size multiples (₹0.05 increments).
        premium = round_to_tick(premium, 0.05)

        return {
            'symbol': symbol,
            'exchange': exchange,
            'expiry': row['expiry'],
            'strike': s_val,
            'option_type': side,
            'token': token,
            'action': action,
            # Audit fix (C4): NSE stock options are American-style / physically
            # settled, unlike index options — see ASSIGNMENT_RISK_DELTA in
            # config_vol.py. Recorded at leg build time since it only depends on
            # the underlying, not price.
            'instrument_type': 'OPTIDX' if symbol in INDEX_UNDERLYINGS else 'OPTSTK',
            'lots': -1 if action == 'SELL' else 1,
            'lot_size': lot_size,
            'delta': greeks['delta'],
            'theta': greeks['theta'],
            'sell_price': premium,
            # original_sell_price is the immutable entry premium shown in the signal notification.
            # It must NEVER be overwritten after signal creation, even during grace-window floating.
            'original_sell_price': premium,
            'cmp': premium,
            'change_pct': 0,
            'pnl': 0,
            'pnl_pct': 0,
            'status': 'WAITING',
            'target_price': float(premium) * 0.5 if premium > 0 else 0,
            'stop_loss_price': float(premium) * 2.0 if premium > 0 else 0,
            'live_iv': live_iv
        }

    # Bulk pre-fetch final selected (sell-only) legs to warm up cache before make_leg_entry calls
    try:
        final_tokens = []
        for row in [sell_ce, sell_pe]:
            if row and row.get("token"):
                final_tokens.append(str(row["token"]))
        if final_tokens and _svc:
            logger.info("[FINAL_LEGS_WARMUP] Bulk pre-fetching %d final option legs for %s", len(final_tokens), symbol)
            _svc.get_bulk_quotes({exchange: final_tokens})
    except Exception as final_prefetch_err:
        logger.warning("[FINAL_PREFETCH] Bulk pre-fetch failed: %s", final_prefetch_err)

    # Build 2-leg Short Strangle (Sell Only)
    legs.append(make_leg_entry(sell_ce, 'CE', 'SELL'))
    legs.append(make_leg_entry(sell_pe, 'PE', 'SELL'))
    
    # PREMIUM VIABILITY CHECK: Reject positions with premiums too low to trade profitably
    # NSE: ≥₹2 per leg, ≥₹500 notional
    effective_min_premium = MIN_PREMIUM_PER_LEG
    effective_min_notional = MIN_NOTIONAL_PER_LEG
    
    for leg in legs:
        leg_premium = float(leg.get('sell_price', 0))
        leg_lot = int(leg.get('lot_size', 1))
        leg_notional = leg_premium * leg_lot
        
        if leg_premium < effective_min_premium:
            logger.info(
                "[PREMIUM_FILTER] Skipping %s — %s %s premium ₹%.2f < min ₹%.2f (not worth trading)",
                symbol, leg['strike'], leg['option_type'], leg_premium, effective_min_premium
            )
            return []  # Reject entire strangle — both legs must be viable
        
        if leg_notional < effective_min_notional:
            logger.info(
                "[NOTIONAL_FILTER] Skipping %s — %s %s notional ₹%.0f (₹%.2f × %d) < min ₹%.0f",
                symbol, leg['strike'], leg['option_type'], leg_notional, leg_premium, leg_lot, effective_min_notional
            )
            return []  # Reject — not enough absolute profit to justify margin

    # Inject dynamic quantitative metrics into leg metadata for downstream tracking and display
    ce_strike_final = _get_strike(sell_ce)
    pe_strike_final = _get_strike(sell_pe)
    ce_dist_pct = ((ce_strike_final - calc_spot) / calc_spot * 100.0) if calc_spot > 0 else 0.0
    pe_dist_pct = ((calc_spot - pe_strike_final) / calc_spot * 100.0) if calc_spot > 0 else 0.0
    total_otm_dist = ce_dist_pct + pe_dist_pct
    combined_premium_val = float(legs[0].get('sell_price', 0)) + float(legs[1].get('sell_price', 0))
    theta_eff = (combined_premium_val / total_otm_dist) if total_otm_dist > 0 else 0.0

    # ── Compute per-leg intraday theta velocity scores ────────────────────────
    ce_strike_final = _get_strike(sell_ce)
    pe_strike_final = _get_strike(sell_pe)
    ce_dist_pct = ((ce_strike_final - calc_spot) / calc_spot * 100.0) if calc_spot > 0 else 0.0
    pe_dist_pct = ((calc_spot - pe_strike_final) / calc_spot * 100.0) if calc_spot > 0 else 0.0
    total_otm_dist = ce_dist_pct + pe_dist_pct

    combined_premium_val = float(legs[0].get('sell_price', 0)) + float(legs[1].get('sell_price', 0))
    theta_eff = (combined_premium_val / total_otm_dist) if total_otm_dist > 0 else 0.0

    ce_gr_final = calculate_greeks(calc_spot, ce_strike_final, t_days, sigma=live_iv, option_type='CE')
    pe_gr_final = calculate_greeks(calc_spot, pe_strike_final, t_days, sigma=live_iv, option_type='PE')

    ce_theta_score = score_intraday_theta_velocity(ce_gr_final.get('theta', 0), calc_spot, ce_strike_final)
    pe_theta_score = score_intraday_theta_velocity(pe_gr_final.get('theta', 0), calc_spot, pe_strike_final)

    for leg in legs:
        leg['theta_efficiency'] = round(theta_eff, 4)
        leg['expected_move'] = round(expected_move, 2)
        leg['premium_velocity'] = round(daily_theta_yield * 100.0, 4)   # Daily Theta Yield in % per day
        leg['time_regime'] = time_regime                                  # "Morning" | "Midday" | "Afternoon"
        leg['is_intraday'] = True                                         # Flag for exit engine
        # Per-leg theta velocity score (primary intraday ranking metric)
        leg['intraday_theta_score'] = ce_theta_score if leg.get('option_type') == 'CE' else pe_theta_score

    logger.info(
        "[INTRADAY_SCORE] %s | CE theta_score=%.6f | PE theta_score=%.6f | "
        "Combined=\u20b9%.2f | EM=\u20b9%.2f | Regime=%s",
        symbol, ce_theta_score, pe_theta_score, combined_premium_val, expected_move, time_regime
    )

    return legs


def _background_scan(tracked):
    try:
        logger.info("[SCANNER] Background thread started")
        from stocks.services.market_data_orchestrator import get_orchestrator
        bg_orch = get_orchestrator()
        bg_svc = get_truedata_instance() # Global singleton, thread-safe for basic REST calls
        
        target_count = getattr(settings, "HEDGE_MAX_SIGNALS", 10) # 10 NSE stocks

        candidate_equities = []

        now_ist_dt = timezone.now().astimezone(IST)
        now_ist_time = now_ist_dt.time()

        # 3.2 Nifty 50 Complete Universe — TWO-PASS RELATIVE RANKING
        # ─────────────────────────────────────────────────────────────
        # Why two passes?
        # All Nifty-50 large-caps tend to be "near VWAP" and "in their VA"
        # most sessions. A fixed threshold treats them as equal. Instead we
        # collect RAW metrics for EVERY stock, then rank them relative to
        # each other. The stock that is MOST tightly hugging its VWAP today
        # scores rank-1 on that dimension — even if yesterday a different
        # stock held rank-1. This guarantees genuine variety each session.
        # ─────────────────────────────────────────────────────────────
        if ENTRY_WINDOW_START <= now_ist_time <= ENTRY_WINDOW_END:

            # ── PASS 1: Collect raw metrics for all qualifying stocks ──
            raw_candidates = []   # list of dicts with symbol + raw metric values

            # Same earnings/F&O-ban exclusion intraday_service.py and
            # option_buying_service.py already apply — this scanner never did, despite
            # a short strangle being the single worst position to hold into an earnings
            # surprise (both legs exposed, no directional hedge). Reuses the existing
            # filter, doesn't reinvent it.
            from stocks.services.event_filter_service import filter_symbols
            scan_universe, _excluded_today = filter_symbols(NIFTY_50_STOCKS())

            for symbol in scan_universe:
                if symbol in ['NIFTY', 'BANKNIFTY', 'FINNIFTY']: continue
                if symbol in tracked: continue

                stock_intel = get_symbol_market_state(symbol, exchange='NSE', svc=bg_svc)
                if not stock_intel:
                    stock_intel = {"is_within_va": True, "vah": 0, "val": 0, "confidence": 60}

                vah          = stock_intel.get('vah', 0)
                val          = stock_intel.get('val', 0)
                poc          = stock_intel.get('poc', 0)
                current_price = stock_intel.get('current_price', 0)
                metrics      = stock_intel.get('metrics', {})

                # Structural price floor — see MIN_STOCK_PRICE. 0 means the lookup itself
                # failed (data unavailable), not a genuinely cheap stock, so don't skip on it.
                if 0 < current_price < MIN_STOCK_PRICE:
                    logger.debug("[SCANNER] %s below Rs.%d price floor (₹%.2f) — skipped", symbol, MIN_STOCK_PRICE, current_price)
                    continue

                # Hard filter: must pass the sideways OR value-area condition.
                # Using both conditions (OR) keeps the universe wide for ranking.
                # Stocks outside BOTH conditions are genuinely trending — skip them.
                is_sideways_ok  = stock_intel.get('is_sideways', False)
                is_within_va_ok = stock_intel.get('is_within_va', True)   # default True when VA unavailable
                has_no_va_data  = vah == 0 and val == 0
                if not (is_sideways_ok or is_within_va_ok or has_no_va_data):
                    logger.debug("[SCANNER] %s trending + outside VA — skipped", symbol)
                    continue

                # Raw metric 1: VWAP distance % (lower = better)
                vwap_dist_pct = float(metrics.get('vwap_dist_pct', 9999))

                # Raw metric 2: VA width % of spot (lower = tighter consolidation = better)
                va_width_pct = 9999.0
                if vah > val > 0 and current_price > 0:
                    va_width_pct = (vah - val) / current_price * 100.0

                # Raw metric 3: POC distance % (how far price is from POC as % of VA half-width)
                poc_dist_norm = 1.0   # worst = 1.0, best = 0.0
                if poc > 0 and vah > val > 0 and current_price > 0:
                    va_half = (vah - val) / 2.0
                    if va_half > 0:
                        poc_dist_norm = min(1.0, abs(current_price - poc) / va_half)

                # Raw metric 4: Intraday range % (lower = flatter day = better)
                intraday_range_pct = 9999.0
                try:
                    price_res = bg_orch.get_price(symbol, exchange='NSE') or {}
                    ltp  = float(price_res.get('ltp', 0) or 0)
                    high = float(price_res.get('high', 0) or 0)
                    low  = float(price_res.get('low', 0) or 0)
                    if ltp > 0 and high > low > 0:
                        intraday_range_pct = (high - low) / ltp * 100.0
                except Exception:
                    pass

                raw_candidates.append({
                    'symbol':             symbol,
                    'exchange':           'NFO',
                    'sigma':              DEFAULT_STOCK_SIGMA,
                    'vwap_dist_pct':      vwap_dist_pct,
                    'va_width_pct':       va_width_pct,
                    'poc_dist_norm':      poc_dist_norm,
                    'intraday_range_pct': intraday_range_pct,
                })

            logger.info("[SCANNER] Pass-1 complete: %d stocks qualify for ranking", len(raw_candidates))

            # ── PASS 2: Rank-normalise each metric, compute composite score ──
            # For each metric (lower raw value = better), assign a rank from
            # 0.0 (worst) to 1.0 (best) based on position among all candidates.
            # Final score = weighted sum of normalised ranks (0–100 scale).
            # Weights:
            #   VWAP proximity  50% — most important for theta/premium quality
            #   VA tightness    25% — consolidation quality
            #   POC centrality  15% — strike balance
            #   Intraday range  10% — day's actual movement

            def rank_normalise(candidates, key, reverse=False):
                """Add a '_rank_{key}' (0.0–1.0) field to each candidate dict."""
                n = len(candidates)
                if n <= 1:
                    for c in candidates:
                        c[f'_rank_{key}'] = 1.0
                    return
                sorted_vals = sorted(candidates, key=lambda x: x[key], reverse=reverse)
                for rank_idx, cand in enumerate(sorted_vals):
                    # rank_idx 0 = best; normalise to 1.0 → 0.0
                    cand[f'_rank_{key}'] = 1.0 - (rank_idx / (n - 1))

            if raw_candidates:
                rank_normalise(raw_candidates, 'vwap_dist_pct',      reverse=False)  # lower dist = rank 1.0
                rank_normalise(raw_candidates, 'va_width_pct',        reverse=False)  # tighter VA = rank 1.0
                rank_normalise(raw_candidates, 'poc_dist_norm',        reverse=False)  # closer POC = rank 1.0
                rank_normalise(raw_candidates, 'intraday_range_pct',  reverse=False)  # flatter day = rank 1.0

                for cand in raw_candidates:
                    composite = (
                        cand['_rank_vwap_dist_pct']      * 50.0 +
                        cand['_rank_va_width_pct']        * 25.0 +
                        cand['_rank_poc_dist_norm']        * 15.0 +
                        cand['_rank_intraday_range_pct']  * 10.0
                    )
                    cand['confidence'] = round(composite, 2)
                    logger.info(
                        "[RANK] %s | vwap_dist=%.3f%% va=%.2f%% poc_dist=%.2f intra=%.2f%% "
                        "→ ranks(vwap=%.2f va=%.2f poc=%.2f rng=%.2f) → score=%.1f",
                        cand['symbol'],
                        cand['vwap_dist_pct'], cand['va_width_pct'],
                        cand['poc_dist_norm'], cand['intraday_range_pct'],
                        cand['_rank_vwap_dist_pct'], cand['_rank_va_width_pct'],
                        cand['_rank_poc_dist_norm'], cand['_rank_intraday_range_pct'],
                        composite
                    )

                candidate_equities = raw_candidates

        # 3.3 RANKING AND EXECUTION
        # Final sort: highest composite rank-score first.
        # Because ranks are RELATIVE across today's universe, the winner
        # is whichever stock genuinely has the best combination of conditions
        # RIGHT NOW — not just "passes a threshold". Different stocks win
        # on different days as VWAP distances, VA widths, and intraday ranges
        # shift across the 50-stock universe.
        candidate_equities.sort(key=lambda x: x['confidence'], reverse=True)

        # Audit fix (H3): apply sector/correlation/promoter-group concentration caps
        # before candidates ever reach the per-symbol creation loop below — without
        # this, candidates are ranked purely on today's consolidation profile with
        # nothing stopping a "safe" 10-position book from being ten correlated names
        # that all breach on the same macro trigger. Same machinery already proven in
        # intraday_service.py / pro_system_service.py.
        if candidate_equities:
            try:
                from stocks.services.shared.portfolio_risk import (
                    apply_portfolio_constraints, build_correlation_clusters,
                )
                from stocks.services.shared.universe import get_sector_map

                specialist_profile = _specialist_portfolio_profile()
                open_positions = list(
                    SignalHistory.objects.filter(
                        category='specialist',
                        status__in=[SignalHistory.Status.PENDING, SignalHistory.Status.ACTIVE],
                    ).values("symbol")
                )
                sector_map = get_sector_map()
                clusters = build_correlation_clusters(
                    bg_svc, [c["symbol"] for c in candidate_equities], profile=specialist_profile,
                )
                accepted, rejected = apply_portfolio_constraints(
                    candidate_equities, open_positions, sector_map, clusters,
                    target_count, profile=specialist_profile,
                )
                if rejected:
                    logger.info(
                        "[SCANNER][H3] Concentration caps: %d accepted, %d rejected of %d candidates.",
                        len(accepted), len(rejected), len(candidate_equities),
                    )
                candidate_equities = accepted
            except Exception as pc_err:
                # Fail open on the concentration-cap machinery itself (e.g. a sector
                # CSV fetch failure) rather than blocking the whole scan — losing this
                # control for one cycle is far less bad than losing the entire day's
                # scan to an unrelated data-source hiccup.
                logger.error("[SCANNER][H3] Portfolio constraint check failed, proceeding unfiltered: %s", pc_err)

        current_total_active = len(tracked)
        slots_remaining = max(0, target_count - current_total_active)

        if slots_remaining > 0:
            # Solution A: Bulk Spot Price Pre-Fetching (reduces spot REST spams to near-zero)
            # Fetch for all ranked candidates to make sure we have data ready as we dynamically scan down the list
            all_equity_symbols = [c["symbol"] for c in candidate_equities]

            bulk_spots = {}
            if all_equity_symbols:
                try:
                    bulk_spots.update(bg_orch.get_prices_bulk(all_equity_symbols, exchange="NSE"))
                except Exception as e:
                    logger.warning("[SCANNER] Bulk equity spot fetch failed: %s", e)

            created_signals = []

            # 3.3a Select & Process Equities dynamically until we fill all slots.
            # Audit fix M7: this used to be a hardcoded `MAX_EQUITY_SIGNALS = 10`,
            # silently overriding target_count (which IS settings-driven, via
            # HEDGE_MAX_SIGNALS above) — raising HEDGE_MAX_SIGNALS in settings didn't
            # actually raise the real cap. Use target_count directly.
            equity_count = len(tracked)

            for cand in candidate_equities:
                if equity_count >= target_count or slots_remaining <= 0:
                    break  # Cap fully achieved!

                spot_price = bulk_spots.get(cand["symbol"], 0)
                if spot_price <= 0:
                    price_res = bg_orch.get_price(cand["symbol"], exchange='NSE')
                    spot_price = price_res.get('ltp', 0)
                if spot_price <= 0:
                    continue

                legs = build_specialist_hedge(cand["symbol"], cand["exchange"], spot_price, bg_orch, sigma=cand["sigma"])
                if not legs:
                    # Automatically rejected (e.g. total premium < ₹5000, BPCL low premium, etc.)
                    # We just skip it and let the loop scan the NEXT candidate to fill the slot!
                    logger.info("[SCANNER] Stock %s rejected (no viable strikes or premiums too low) — scanning next candidate", cand["symbol"])
                    continue

                # Ensure signal doesn't already exist
                already_exists = SignalHistory.objects.filter(
                    symbol=cand["symbol"],
                    category='specialist',
                    status__in=[SignalHistory.Status.PENDING, SignalHistory.Status.ACTIVE],
                    generated_at__gte=timezone.now().astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
                ).exists()
                if already_exists:
                    logger.info("[SCANNER] Stock %s signal already exists — skipping", cand["symbol"])
                    tracked.add(cand["symbol"])
                    continue

                try:
                    new_sig = SignalHistory.objects.create(
                        symbol=cand["symbol"],
                        signal_type='STRANGLE',
                        entry_price=spot_price,
                        status=SignalHistory.Status.PENDING,
                        category='specialist',
                        metadata={
                            'legs': legs,
                            'confidence': cand.get('confidence', 0),
                            'rank': len(created_signals) + 1
                        }
                    )
                    tracked.add(cand["symbol"])
                    created_signals.append(new_sig)
                    equity_count += 1
                    slots_remaining -= 1
                    logger.info("[SCANNER] ✅ Created stock signal id=%d for %s spot=%.2f legs=%d",
                                new_sig.id, cand["symbol"], spot_price, len(legs))
                except Exception as ie:
                    logger.error("[SCANNER] ❌ Failed to create stock signal for %s: %s", cand["symbol"], ie)
            
            # Send consolidated new signals alert at the end of the scan cycle
            if created_signals:
                try:
                    from stocks.services.telegram_service import send_consolidated_new_signals
                    send_consolidated_new_signals(created_signals)
                except Exception as tg_err:
                    logger.warning("[TELEGRAM] Failed to send consolidated new signal alert: %s", tg_err)
                        
        logger.info("[SCANNER] Background scan cycle complete. Created %d signals during this scan.", len(created_signals))
    except Exception as bge:
        logger.error("Background scanner exception: %s", bge, exc_info=True)

def get_hedge_panel_summary(limit: int = 3) -> Dict[str, Any]:
    """
    Lightweight, DB-only preview of open strangle positions for the Dashboard's preview
    card — deliberately NOT calling get_hedge_panel_data(), which does live scanning/
    quote-fetch work. Reads already-persisted SignalHistory(category='specialist') rows
    and their stored metadata['legs'] (entry premiums captured at signal creation), so
    this can be hit on every dashboard load with zero Angel One REST calls.
    """
    live_rows = SignalHistory.objects.filter(
        category='specialist',
        status__in=[SignalHistory.Status.PENDING, SignalHistory.Status.ACTIVE],
    ).order_by('-generated_at')

    items = []
    for sig in live_rows[:limit]:
        legs = (sig.metadata or {}).get('legs', [])
        entry_premium = round(sum(float(leg.get('original_sell_price', 0)) for leg in legs), 2)
        items.append({
            'id': sig.id,
            'symbol': sig.symbol,
            'exchange': legs[0].get('exchange') if legs else None,
            'status': sig.status,
            'legs_count': len(legs),
            'entry_premium': entry_premium,
            'generated_at': sig.generated_at.isoformat() if sig.generated_at else None,
        })

    return {'count': live_rows.count(), 'items': items}


def _compute_portfolio_heat(unique_signals) -> float:
    """
    Portfolio 'heat' = total notional of all ACTIVE strangle SELL legs, expressed as a
    percentage of HEDGE_ACCOUNT_CAPITAL (capped at 100%).

    Single source of truth for portfolio_heat_pct, used by BOTH:
      - the dashboard display path (panel_data['portfolio_metrics']['portfolio_heat_pct']
        in get_hedge_panel_data), and
      - the pre-scan risk gate (see the "SCAN FOR NEW SIGNALS" section of
        get_hedge_panel_data) that skips opening new positions once heat crosses
        MAX_PORTFOLIO_HEAT_PCT.

    Extracted out of what used to be an inline loop so both callers can never see a
    different number. Same filtering rules as before extraction: only ACTIVE signals count,
    only SELL legs count, and a leg marked EXPIRED (rolled away during a mid-day delta
    rebalance) is skipped so a rebalanced symbol doesn't double-count notional across both
    its old and new leg.
    """
    total_notional = 0.0
    for sig in unique_signals:
        if sig.status != SignalHistory.Status.ACTIVE:
            continue
        meta = sig.metadata or {}
        legs = meta.get("legs", [])
        spot_val = float(sig.entry_price or 0.0)

        for leg in legs:
            if leg.get("action") == "SELL" and leg.get("status") != "EXPIRED":
                ls = float(leg.get("lot_size", 1.0))
                strike = float(leg.get("strike", 0.0))
                leg_spot = spot_val if spot_val > 0 else strike
                total_notional += leg_spot * ls

    if total_notional <= 0:
        return 0.0
    return min(100.0, (total_notional / HEDGE_ACCOUNT_CAPITAL) * 100.0)


def get_hedge_panel_data(action: str | None = None, sync_scan: bool = False) -> Dict[str, Any]:
    """
    Get hedge panel data for all active signals
    """
    svc = get_truedata_instance()
    if not svc:
        return {
            'timestamp': timezone.now().isoformat(),
            'market_status': 'ERROR',
            'error': 'AngelOne Service not available',
            'sections': []
        }

    try:
        # Throttled Cache: Reduced to 5 seconds for LIVE sniper accuracy.
        # This ensures the CMP and P&L update on every dashboard poll.
        cached_panel = cache.get("delta_hedge_panel_live_5s")
        if cached_panel and action != "generate" and not sync_scan: return cached_panel

        # Determine resolved action with database-backed idempotency for NSE Specialist Equities.
        # Was `.exists()` (any signal today, of any status) -- flipped to "update" forever
        # after the first position, so _background_scan()'s own target_count/slots_remaining
        # logic (already built to fill up to HEDGE_MAX_SIGNALS concurrent positions per run)
        # only ever got invoked once a day. Gate on the actual live count vs the cap instead,
        # so later periodic ticks keep generating until the day's slots are actually full.
        target_count = getattr(settings, "HEDGE_MAX_SIGNALS", 10)
        today_spec_count = SignalHistory.objects.filter(
            category='specialist',
            status__in=[SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING],
            generated_at__date=timezone.now().astimezone(IST).date()
        ).count()
        today_spec_exists = today_spec_count >= target_count

        resolved_action = action
        if resolved_action is None:
            if today_spec_exists:
                resolved_action = "update"
            else:
                resolved_action = "generate"

        orch = get_orchestrator()
        svc = get_truedata_instance()
        
        # Streamer Status Monitoring
        streamer_active = False
        if svc and svc.streamer and svc.streamer.is_connected:
            streamer_active = True

        nse_open = is_market_open()

        panel_data = {
            'timestamp': timezone.now().isoformat(),
            'market_status': 'OPEN' if nse_open else 'CLOSED',
            'nse_open': nse_open,
            'streamer_active': streamer_active,
            'is_sideways': True, # Default, refined per symbol in loop
            'trading_window': True,
            'is_degraded': False,
            'server_time': timezone.now().astimezone(IST).strftime("%H:%M:%S"),
            'diagnostics': {
                'ist_time': timezone.now().astimezone(IST).strftime("%H:%M:%S"),
                'ist_date': str(timezone.now().astimezone(IST).date()),
                'nse_open': nse_open,
            },
            'total_pnl': 0,
            'sections': [],
            # Exposes what this call actually resolved to (vs. the possibly-None `action`
            # argument) so callers like run_periodic_scanners can tell whether a fresh
            # generation pass ran this time, without re-deriving today_spec_exists themselves.
            'resolved_action': resolved_action,
        }

        # 0. AUTO-CLEANUP: Expire stale signals from previous days (runs once per hour)
        # This prevents the unique constraint (symbol, category, status) from blocking
        # new signal creation each trading day.
        if not cache.get("stale_signal_cleanup_done"):
            try:
                today_start_cleanup = timezone.now().astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
                stale_count = SignalHistory.objects.filter(
                    category='specialist',
                    status__in=[SignalHistory.Status.PENDING, SignalHistory.Status.ACTIVE],
                    generated_at__lt=today_start_cleanup
                ).update(status=SignalHistory.Status.EXPIRED)
                if stale_count > 0:
                    logger.info("[CLEANUP] Auto-expired %d stale signals from previous sessions", stale_count)
                cache.set("stale_signal_cleanup_done", True, timeout=3600)  # Re-check every hour
            except Exception as cleanup_err:
                logger.warning("[CLEANUP] Stale signal cleanup failed: %s", cleanup_err)

        # 1. Fetch Existing Specialist Signals (Including today's closed ones)
        today_start = timezone.now().astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        existing_signals = list(SignalHistory.objects.filter(
            category='specialist',
            generated_at__gte=today_start
        ))

        # UI List Sorting: Prioritize ACTIVE first, then PENDING, then sort by Rank (metadata__rank)
        # We manually sort here because rank is inside the JSON metadata field.
        def sort_key(s):
            # Status Priority: ACTIVE (0), PENDING (1), then others (2)
            if s.status == SignalHistory.Status.ACTIVE: status_priority = 0
            elif s.status == SignalHistory.Status.PENDING: status_priority = 1
            else: status_priority = 2
            
            # Rank Priority (if metadata exists and has rank)
            rank = 999
            if isinstance(s.metadata, dict):
                try: rank = int(s.metadata.get('rank', 999))
                except: rank = 999
            
            return (status_priority, rank, -s.generated_at.timestamp())

        existing_signals.sort(key=sort_key)

        # 1.1 Symbols to skip in NEW scanner (to prevent double entry)
        scanner_skip_symbols = set()
        try:
            # Also skip symbols that are already active/pending in the scanner
            for sig in existing_signals:
                if sig.status in [SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING]:
                    scanner_skip_symbols.add(sig.symbol)
        except Exception as query_err:
            logger.error(f"Critical Database Error: Missing columns in SignalHistory: {query_err}")
            # Degrade gracefully: return the panel with an error message but no sections
            panel_data['error'] = "Market data system is undergoing maintenance (Migration Pending)."
            panel_data['is_degraded'] = True
            return panel_data
        
        unique_signals = []
        processed_symbols = set()
        
        # UI List Deduplication: Show every symbol ONLY ONCE (prioritize Active ones)
        for sig in existing_signals:
            if sig.symbol in processed_symbols:
                # If we have a PENDING signal but already have an ACTIVE/CLOSED one, cancel it
                if sig.status == SignalHistory.Status.PENDING:
                    try:
                        sig.status = SignalHistory.Status.CANCELLED
                        sig.save()
                    except: pass
                continue
            
            processed_symbols.add(sig.symbol)
            unique_signals.append(sig)

        # 2. PRE-PROCESS & GATHER TOKENS
        bulk_token_map = {"NFO": []}
        for sig in unique_signals:
            # Self-Healing Metadata Check
            if not hasattr(sig, 'metadata') or sig.metadata is None:
                continue

            # Auto-Correction: If strikes are stored in Paise (master-scale), normalize them to Rupees
            needs_save = False
            try:
                legs = sig.metadata.get('legs', [])
            except (AttributeError, TypeError):
                continue

            for leg in legs:
                try:
                    curr_strike = float(leg.get('strike', 0))
                    if curr_strike > 100000:
                        leg['strike'] = curr_strike / 100.0
                        needs_save = True
                    if sig.symbol in ["NIFTY", "BANKNIFTY", "MARUTI"] and curr_strike < 1000 and curr_strike > 0:
                        leg['strike'] = curr_strike * 100.0
                        needs_save = True
                    
                    # Gather for bulk quote
                    l_exch = leg.get('exchange', 'NSE')
                    l_token = leg.get('token')
                    if l_token and l_exch in bulk_token_map:
                        bulk_token_map[l_exch].append(str(l_token))
                except:
                    continue
            
            if sig.status in ['TARGET HIT', 'SL HIT']:
                sig.status = SignalHistory.Status.ACTIVE
                needs_save = True

            if needs_save:
                sig.save()

        # 2.1 FETCH BULK QUOTES ONCE
        bulk_quotes = {}
        if bulk_token_map["NFO"] and svc:
            # Ensure all option tokens are subscribed to WebSocket for real-time ticks
            if svc.streamer and svc.streamer.is_connected:
                svc.streamer.subscribe(2, bulk_token_map["NFO"], mode=1)
            bulk_quotes = svc.get_bulk_quotes(bulk_token_map)

        # 2.2 PROCESS EXISTING SIGNALS (Persistent Monitor)
        for sig in unique_signals:
            try:
                if not hasattr(sig, 'metadata') or sig.metadata is None:
                    continue

                sig_exchange = 'NSE'

                # Spot Price Retrieval - Handle None response gracefully
                price_res = orch.get_price(sig.symbol, exchange=sig_exchange) or {}
                spot_price = price_res.get('ltp', 0)

                # STALE SIGNAL CHECK
                sig_date = sig.generated_at.astimezone(IST).date()
                today_date = datetime.now(tz=IST).date()
                if sig_date < today_date:
                    sig.status = 'EXPIRED' if sig.status == 'ACTIVE' else 'CANCELLED'
                    sig.save()
                    continue

                display_spot = spot_price

                # Extract metadata metrics
                meta = sig.metadata or {}
                sig_conf = round(meta.get('confidence', 90))

                # Calculate grace remaining for PENDING signals
                age_secs = (timezone.now() - sig.generated_at).total_seconds()
                grace_left = max(0, int(PENDING_GRACE_SECONDS - age_secs)) if sig.status == SignalHistory.Status.PENDING else 0

                section = {
                    'id': sig.id,
                    'title': f"{sig.symbol} — {sig_conf}% CONFIDENCE SPECIALIST SETUP",
                    'exchange': 'NSE',
                    'underlying': sig.symbol,
                    'spot_price': display_spot,
                    'signal': 'SIDEWAYS',
                    'signal_time': sig.generated_at.astimezone(IST).strftime("%H:%M"),
                    'entry_time': sig.active_time.astimezone(IST).strftime("%H:%M") if sig.active_time else "PENDING",
                    'grace_remaining': grace_left,
                    'was_activated': sig.active_time is not None,
                    'legs': [],
                    'section_pnl': 0,
                    'status': sig.status,
                    'exit_reason': None
                }
                
                legs = sig.metadata.get('legs', [])
                
                # LIVE PREMIUM VIABILITY: Auto-cancel PENDING signals whose premiums have decayed
                # below tradeable levels
                if sig.status == SignalHistory.Status.PENDING:
                    should_cancel = False
                    for leg in legs:
                        leg_cmp = float(leg.get('cmp', 0) or leg.get('sell_price', 0))
                        if 0 < leg_cmp < MIN_LIVE_PREMIUM:
                            logger.info(
                                "[LIVE_FILTER] Auto-cancelling %s — %s %s CMP ₹%.2f < min live ₹%.2f",
                                sig.symbol, leg.get('strike'), leg.get('option_type'), leg_cmp, MIN_LIVE_PREMIUM
                            )
                            should_cancel = True
                            break
                    if should_cancel:
                        sig.status = SignalHistory.Status.CANCELLED
                        sig.save()
                        continue
                
                process_legs(section, legs, orch, panel_data, persist_updates=True, sig_id=sig.id, bulk_quotes=bulk_quotes, underlying_spot=spot_price, sig_exchange=sig_exchange)
            except Exception as sig_err:
                logger.error(f"Error processing signal {sig.id} for {sig.symbol}: {sig_err}", exc_info=True)
                continue

        # --- PORTFOLIO METRICS CALCULATION ---
        import math
        total_theta = 0.0
        total_vega = 0.0
        total_premium_sold = 0.0
        active_positions_count = 0

        for sig in unique_signals:
            if sig.status == SignalHistory.Status.ACTIVE:
                active_positions_count += 1
                meta = sig.metadata or {}
                legs = meta.get("legs", [])

                spot_val = float(sig.entry_price or 0.0)

                for leg in legs:
                    # Skip EXPIRED (rolled-away) legs — otherwise a rebalanced symbol double-counts
                    # premium/theta/vega across both the old and new leg. (Notional/heat is
                    # computed separately by _compute_portfolio_heat with the same rule.)
                    if leg.get("action") == "SELL" and leg.get("status") != "EXPIRED":
                        sell_pr = float(leg.get("original_sell_price") or leg.get("sell_price", 0.0))
                        ls = float(leg.get("lot_size", 1.0))
                        total_premium_sold += sell_pr * ls

                        strike = float(leg.get("strike", 0.0))
                        opt_type = leg.get("option_type", "CE")
                        
                        try:
                            today_date = timezone.now().astimezone(IST).date()
                            exp_date = datetime.strptime(leg.get("expiry"), "%d%b%Y").date()
                            t_days = max(1.0, float((exp_date - today_date).days))
                        except:
                            t_days = 5.0
                        
                        leg_spot = spot_val if spot_val > 0 else strike
                        
                        from stocks.services.option_greeks_service import calculate_greeks
                        gr = calculate_greeks(leg_spot, strike, t_days, 0.07, 0.20, opt_type)
                        leg_theta = -float(gr.get("theta", 0.0)) * ls
                        total_theta += leg_theta
                        
                        t_years = max(0.0001, t_days / 365.0)
                        try:
                            d1 = (math.log(leg_spot / strike) + (0.07 + 0.5 * 0.20**2) * t_years) / (0.20 * math.sqrt(t_years))
                            leg_vega = leg_spot * math.sqrt(t_years) * (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * d1**2)
                        except:
                            leg_vega = 0.0
                        
                        total_vega += -leg_vega * ls

        # portfolio_heat_pct is the single source of truth for BOTH the display metric below
        # AND the pre-scan risk gate right after it — always the same computed value (see
        # _compute_portfolio_heat docstring).
        portfolio_heat = _compute_portfolio_heat(unique_signals)

        panel_data['portfolio_metrics'] = {
            'total_theta': round(total_theta, 2),
            'total_vega': round(total_vega, 2),
            'portfolio_heat_pct': round(portfolio_heat, 2),
            'active_positions': active_positions_count,
            'total_premium_sold': round(total_premium_sold, 2)
        }

        # 3. SCAN FOR NEW SIGNALS (Self-Aware Scanner)

        now_ist = timezone.now().astimezone(IST).time()
        scanner_throttled = cache.get("delta_hedge_scanner_throttle_5m")
        # Scan strictly runs when resolved_action == "generate"
        if resolved_action == "generate" and not scanner_throttled and now_ist >= ENTRY_WINDOW_START:
            # PORTFOLIO-HEAT GATE (Audit Remediation Plan Phase 2 #2.8) — additive to the
            # flat position-count cap (HEDGE_MAX_SIGNALS / target_count inside
            # _background_scan), not a replacement. Reuses the same portfolio_heat value just
            # computed for the display metric above, so the gate and the dashboard can never
            # disagree. Only blocks opening NEW positions — existing open positions, exits,
            # and rebalancing are untouched.
            if portfolio_heat >= MAX_PORTFOLIO_HEAT_PCT:
                logger.warning(
                    "[PORTFOLIO_HEAT] Skipping new position scan — heat %.2f%% >= threshold %.2f%%",
                    portfolio_heat, MAX_PORTFOLIO_HEAT_PCT
                )
            else:
                logger.info("[DELTA_HEDGE] Triggering daily specialist signal generation scan...")
                # Set throttle before thread creation to prevent multiple submissions
                cache.set("delta_hedge_scanner_throttle_5m", True, timeout=120)  # 2-min throttle (was 5)

                if sync_scan:
                    logger.info("[DELTA_HEDGE] Running daily specialist scan synchronously...")
                    _background_scan(scanner_skip_symbols.copy())
                else:
                    logger.info("[DELTA_HEDGE] Spawning background thread for daily specialist scan...")
                    import threading
                    threading.Thread(target=_background_scan, args=(scanner_skip_symbols.copy(),), daemon=True).start()

        cache.set("delta_hedge_panel_live_5s", panel_data, timeout=5) # 5-second live refresh
        return panel_data
    except Exception as e:
        logger.error(f"Error getting hedge panel data: {e}", exc_info=True)
        return {
            'timestamp': timezone.now().isoformat(),
            'market_status': 'ERROR',
            'error': str(e),
            'sections': []
        }

def rebalance_delta_neutral_strangle(sig, updated_legs, underlying_spot, sig_exchange, orch):
    """
    Dynamic Delta Hedging: rolls the decayed leg closer to spot to balance delta
    when the absolute delta imbalance exceeds 0.15.
    """
    from stocks.services.option_greeks_service import calculate_greeks
    from stocks.services.telegram_service import send_telegram_message
    from django.core.cache import cache
    
    symbol = sig.symbol
    # 1. Identify active legs (status == 'WAITING')
    active_legs = [l for l in updated_legs if l.get('status') == 'WAITING']
    if len(active_legs) != 2:
        return
        
    ce_leg = next((l for l in active_legs if l.get('option_type') == 'CE'), None)
    pe_leg = next((l for l in active_legs if l.get('option_type') == 'PE'), None)
    if not ce_leg or not pe_leg:
        return

    # A quote-fetch failure (e.g. TrueData rate-limit circuit breaker) leaves
    # underlying_spot at its 0 default — feeding that into calculate_greeks()
    # below raises "math domain error" (log(spot/strike) with spot=0) and
    # silently produces a fake Delta: 0.00 / Imbalance: 0.00 result, which
    # masks a real delta breach instead of skipping the check. Skip this cycle
    # and retry on the next tick once a real quote is available.
    if underlying_spot <= 0:
        logger.warning(f"[REBALANCE_CHECK] {symbol}: skipping — invalid spot price ({underlying_spot})")
        return

    # Calculate days to expiry (t_days)
    try:
        today_date = timezone.now().astimezone(IST).date()
        exp_date = datetime.strptime(ce_leg.get('expiry'), "%d%b%Y").date()
        t_days = max(1.0, float((exp_date - today_date).days))
    except Exception as e:
        logger.error(f"[REBALANCE] Failed to parse expiry date: {e}")
        return

    # Volatility / Sigma config
    sigma = _fallback_sigma(symbol, ce_leg.get('live_iv'))

    # 2. Calculate current deltas
    ce_g = calculate_greeks(underlying_spot, ce_leg['strike'], t_days, sigma=sigma, option_type='CE')
    pe_g = calculate_greeks(underlying_spot, pe_leg['strike'], t_days, sigma=sigma, option_type='PE')
    ce_delta = abs(ce_g.get('delta', 0))
    pe_delta = abs(pe_g.get('delta', 0))
    
    delta_imbalance = abs(ce_delta - pe_delta)
    logger.info(f"[REBALANCE_CHECK] {symbol} | Spot: {underlying_spot} | CE {ce_leg['strike']} Delta: {ce_delta:.2f} | PE {pe_leg['strike']} Delta: {pe_delta:.2f} | Imbalance: {delta_imbalance:.2f}")
    
    if delta_imbalance < 0.15:
        return # Delta is balanced, no action needed

    logger.warning(f"[REBALANCE_TRIGGER] Delta imbalance {delta_imbalance:.2f} >= 0.15 for {symbol}. Triggering roll...")
    
    # 3. Determine which leg is challenged and roll THAT leg further OTM to bring
    # its delta back down — not the safe leg inward to match the challenged one's
    # delta up. The old logic did the latter: it left the threatened leg exactly
    # where it was (still under directional pressure) and dragged the safe leg
    # almost ATM to "match" it, which doesn't reduce the risk that triggered the
    # rebalance at all — it just adds equivalent risk on the other side too, so a
    # continued adverse move now hurts both legs instead of just the one in trouble.
    if ce_delta > pe_delta:
        # Call challenged — roll the CALL further out to bring its delta back down
        target_delta = pe_delta
        rolled_leg = ce_leg
        op_type = 'CE'
    else:
        # Put challenged — roll the PUT further out to bring its delta back down
        target_delta = ce_delta
        rolled_leg = pe_leg
        op_type = 'PE'

    # Audit fix H2: cap rolls per day on this leg. Without this, a trending/whipsawing
    # underlying can roll the same leg 5-6 times in one session, each roll realizing a
    # small loss chasing price — a cost that never shows up in the headline SL%. Once
    # the cap is hit for today, skip the roll and fall back to the existing SL/
    # delta-danger auto-exit instead of continuing to chase price indefinitely.
    today_iso = today_date.isoformat()
    roll_count_today = rolled_leg.get('roll_count', 0) if rolled_leg.get('last_roll_date') == today_iso else 0
    if roll_count_today >= MAX_ROLLS_PER_DAY:
        logger.warning(
            "[REBALANCE_CAP] %s %s leg has already rolled %d time(s) today (cap %d) — "
            "skipping further rolls; SL/delta-danger will handle it from here.",
            symbol, op_type, roll_count_today, MAX_ROLLS_PER_DAY,
        )
        return

    # Get option strikes
    strikes = get_nse_option_strikes(symbol, underlying_spot)

    if not strikes:
        logger.error(f"[REBALANCE] No strikes found to rebalance {symbol}")
        return

    # Filter strikes for correct option type and expiry
    target_expiry = rolled_leg.get('expiry')
    strike_vals = []
    for s in strikes:
        if s.get('expiry') == target_expiry:
            try:
                if 'strike_price' in s:
                    st_val = float(s['strike_price'])
                else:
                    st_val = float(s.get('strike', 0))
                strike_vals.append(st_val)
            except Exception:
                pass
                
    strike_vals = sorted(list(set(strike_vals)))
    if not strike_vals:
        return

    # Calculate delta for each candidate strike
    candidates = []
    for st in strike_vals:
        # For Put: only look at strikes below spot. For Call: only look at strikes above spot.
        if op_type == 'PE' and st > underlying_spot:
            continue
        if op_type == 'CE' and st < underlying_spot:
            continue
            
        g = calculate_greeks(underlying_spot, st, t_days, sigma=sigma, option_type=op_type)
        d = abs(g.get('delta', 0))
        candidates.append((st, d))
        
    if not candidates:
        return
        
    # Select strike closest to target_delta
    best_strike, best_delta = min(candidates, key=lambda x: abs(x[1] - target_delta))
    
    if best_strike == rolled_leg['strike']:
        logger.info(f"[REBALANCE] Best strike is already the current strike {best_strike}. Skipping.")
        return

    # 4. Fetch quote/CMP for new strike
    try:
        q_res = get_nse_option_quote(symbol, best_strike, op_type, target_expiry)
        q = q_res[0] if isinstance(q_res, tuple) else q_res
        new_token = q_res[1] if isinstance(q_res, tuple) else None
        new_cmp = float(q.get('ltp', 0)) if q else 0.0
    except Exception as exc:
        logger.error(f"[REBALANCE] Failed to get quote for new strike {best_strike}: {exc}")
        return

    # Subscribe the newly-rolled-to contract on the WS immediately — without this,
    # the rolled leg only starts streaming on the NEXT full panel refresh (when
    # get_hedge_panel_data re-gathers bulk_token_map from the just-saved metadata),
    # leaving it on the slower single-token REST path for one whole scan cycle.
    if new_token:
        svc = get_truedata_instance()
        if svc and svc.streamer:
            svc.streamer.subscribe(2, [str(new_token)])
        
    if new_cmp <= 0:
        logger.error(f"[REBALANCE] Invalid CMP {new_cmp} for new strike {best_strike}")
        return

    # 5. Execute the Roll
    # Freeze the old leg
    old_strike = rolled_leg['strike']
    old_cmp = rolled_leg['cmp']
    rolled_leg['status'] = 'EXPIRED'
    rolled_leg['exit_reason'] = f"Rolled to {best_strike} (Delta Neutral Adjustment)"
    
    # Add new leg to updated_legs
    # NOTE: must carry every field make_leg_entry() sets (symbol/action/lots in particular) —
    # downstream code (telegram summary, portfolio Greeks, per-leg exit checks) filters legs by
    # `action == 'SELL'`, and a leg missing that key silently drops out of all of them.
    new_leg = {
        'symbol': rolled_leg.get('symbol', symbol),
        'strike': best_strike,
        'option_type': op_type,
        'exchange': rolled_leg.get('exchange', 'NSE'),
        'expiry': target_expiry,
        'action': rolled_leg.get('action', 'SELL'),
        'lots': rolled_leg.get('lots', -1),
        'delta': best_delta,
        'sell_price': new_cmp,
        'original_sell_price': new_cmp,
        'cmp': new_cmp,
        'status': 'WAITING',
        'token': new_token,
        'live_iv': sigma,
        # Audit fix H2: carry the per-day roll count forward onto the new leg so the
        # cap above sees the running total, not a reset-to-zero count every roll.
        'roll_count': roll_count_today + 1,
        'last_roll_date': today_iso,
    }

    # Initialize zones for the new leg
    new_leg['target_price'] = round_to_tick(new_cmp * 0.70, 0.05)
    new_leg['stop_loss_price'] = round_to_tick(new_cmp * 1.15, 0.05)

    # Audit fix H18: a failed fresh lookup must not overwrite the rolled leg's
    # already-known-good lot_size with a fabricated value — fall back to it instead.
    ls = get_lot_size(symbol, new_leg['exchange']) or rolled_leg.get('lot_size', 0)
    calculated = calculate_pnl(new_cmp, new_cmp, ls, 1)
    calculated['lot_size'] = ls
    # calculate_pnl() always returns PROFIT/LOSS/NEUTRAL for its 'status' key — never apply
    # that to a just-opened leg, or it clobbers the 'WAITING' status set above.
    calculated.pop('status', None)
    new_leg.update(calculated)
    
    # Cache the baseline price suffix for the new leg
    cache_suffix = f"_{sig.id}"
    new_cache_key = f"specialist_baseline_{symbol}_{best_strike}_{op_type}{cache_suffix}"
    cache.set(new_cache_key, new_cmp, 60 * 60 * 24)
    
    updated_legs.append(new_leg)
    
    # 6. Send Telegram Notification
    tg_msg = (
        f"🔄 <b>TradePulse Greeks Rebalance</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Asset</b>: {symbol}\n"
        f"<b>Spot Price</b>: ₹{underlying_spot:,.2f}\n"
        f"<b>Reason</b>: Delta Imbalance too high ({delta_imbalance:.2f} &ge; 0.15)\n\n"
        f"🔴 <b>Closed</b>: {op_type} {old_strike:,.1f} @ ₹{old_cmp:.2f}\n"
        f"🟢 <b>Opened</b>: {op_type} {best_strike:,.1f} @ ₹{new_cmp:.2f} (Delta: {best_delta:.2f})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Strangle net delta balanced back to neutral."
    )
    try:
        send_telegram_message(tg_msg)
        logger.info(f"[REBALANCE] Successfully executed leg roll for {symbol}: {old_strike} -> {best_strike}")
    except Exception as e:
        logger.error(f"[REBALANCE] Telegram failed: {e}")

def process_legs(section, legs, orch, panel_data, persist_updates=False, sig_id=None, bulk_quotes=None, underlying_spot=0, sig_exchange='NSE'):
    """Helper to process option legs and calculate P&L"""
    from django.core.cache import cache
    updated_legs = []
    symbol = section['underlying']
    bulk_quotes = bulk_quotes or {}
    svc = get_truedata_instance()  # used below to prefer WS-streamed Greeks over local Black-Scholes

    for leg in legs:
        # OPTION B: Let Winners Run. If this specific leg has already hit SL/Target, or was
        # rolled away by rebalance_delta_neutral_strangle() (status EXPIRED), freeze it —
        # otherwise a rolled-off leg keeps getting re-quoted every scan and its cmp keeps
        # drifting, making a dead leg look live in P&L/Telegram summaries.
        prev_status = leg.get('status', 'WAITING')
        if prev_status in ['HIT_SL', 'HIT_TARGET', 'EXPIRED']:
            section['legs'].append(leg)
            updated_legs.append(leg)
            section['section_pnl'] += leg.get('pnl', 0)
            continue
            
        try:
            # 1. Fetch Fresh Quote (Prioritize Bulk results)
            l_exch = leg.get('exchange', 'NSE')
            l_token = leg.get('token')
            bulk_key = f"{l_exch}:{l_token}" if l_token else None
            
            if bulk_key and bulk_key in bulk_quotes:
                q = bulk_quotes[bulk_key]
            else:
                q_res = get_nse_option_quote(symbol, leg['strike'], leg['option_type'], leg.get('expiry'))
                q = q_res[0] if isinstance(q_res, tuple) else q_res
                if isinstance(q_res, tuple): leg['token'] = q_res[1]

            if not q: q = {}
        except Exception as qe:
            logger.warning(f"Failed to fetch quote for {symbol} {leg['strike']} {leg['option_type']}: {qe}")
            q = {}
            
        cmp = float(q.get('ltp', 0))

        # Audit fix M6: previously only checked "is it non-zero" — on a
        # circuit-frozen underlying, a stale last-print looks like a live,
        # actionable price to the exit/rebalance logic below. Flag a leg suspect
        # if its LTP hasn't moved across 3 consecutive polls while the underlying
        # spot has moved meaningfully (>0.1%) — reuses the existing
        # `is_theoretical` flag, already respected by the exit-check logic
        # elsewhere in this file, so a suspect leg is excluded from auto-exit
        # without inventing a second flag/code path.
        if cmp > 0:
            prev_cmp = leg.get('_stale_check_cmp')
            prev_spot = leg.get('_stale_check_spot')
            cur_spot = float(underlying_spot or 0)
            if (prev_cmp is not None and prev_spot and cur_spot > 0
                    and cmp == prev_cmp
                    and abs(cur_spot - prev_spot) / prev_spot > 0.001):
                leg['_stale_quote_polls'] = leg.get('_stale_quote_polls', 0) + 1
                if leg['_stale_quote_polls'] >= 3 and not leg.get('is_theoretical'):
                    leg['is_theoretical'] = True
                    logger.warning(
                        "[STALE_QUOTE_SUSPECT] %s %s %s: LTP frozen at %.2f across %d polls "
                        "while spot moved %.2f -> %.2f — flagging theoretical (excluded from auto-exit).",
                        symbol, leg.get('strike'), leg.get('option_type'), cmp,
                        leg['_stale_quote_polls'], prev_spot, cur_spot,
                    )
            else:
                leg['_stale_quote_polls'] = 0
            leg['_stale_check_cmp'] = cmp
            if cur_spot > 0:
                leg['_stale_check_spot'] = cur_spot

        # SELF-HEALING: Theoretical fallback to avoid UI showing "--" un-usable data
        if cmp <= 0:
            # We must calculate t_days for theoretical fallback!
            try:
                today_date = timezone.now().astimezone(IST).date()
                exp_date = datetime.strptime(leg.get('expiry'), "%d%b%Y").date()
                t_days = max(1.0, float((exp_date - today_date).days))
                
                # Use passed underlying spot or fetch if missing (Optimization)
                spot_price = float(underlying_spot or 0)
                if spot_price <= 0:
                    price_res = orch.get_price(symbol, exchange='NSE') or {}
                    spot_price = float(price_res.get('ltp', 0))
                
                # Use stored live_iv from leg metadata if available, else fall back to default stock sigma configs
                sigma = _fallback_sigma(symbol, leg.get('live_iv'))
                if spot_price > 0:
                    from stocks.services.option_greeks_service import calculate_theoretical_premium
                    cmp = calculate_theoretical_premium(spot_price, leg['strike'], t_days, sigma=sigma, option_type=leg['option_type'])
                    leg['is_theoretical'] = True
                    logger.info(f"[TS_FALLBACK] {symbol} WAF Blocked. Theoretical {leg['option_type']} premium={cmp} (Sigma={sigma})")
            except Exception as ts_err:
                pass

            # Last-known-good fallback (found 2026-08-07, EOD square-off incident):
            # both the live quote AND the theoretical calc above can fail in the same
            # call — most commonly a rate-limit burst right at EOD square-off, when
            # several positions close within the same scan cycle. Previously this fell
            # straight through to the hard-zero "prevent ghost profit" guard below,
            # which is correct when NO real price is known, but wrong when one already
            # is: _stale_check_cmp holds this leg's last successfully fetched premium
            # (set every poll below once cmp>0), so a leg that had genuinely decayed
            # toward zero (a real, favorable outcome for a short seller) got its actual
            # profit thrown away and reported as a flat Rs.0 instead of survived.
            if cmp <= 0:
                last_known = leg.get('_stale_check_cmp')
                if last_known and float(last_known) > 0:
                    cmp = float(last_known)
                    leg['is_theoretical'] = True
                    logger.warning(
                        "[LKG_FALLBACK] %s %s %s: quote+theoretical both failed, using last known cmp=%.2f",
                        symbol, leg.get('strike'), leg.get('option_type'), cmp,
                    )

        # 1b. Refresh live Delta for risk monitoring. make_leg_entry() only sets
        # 'delta' once, at entry/roll time — every panel refresh after that was
        # comparing SHORT_DELTA_DANGER against that frozen snapshot instead of the
        # position's actual current delta. Pure local math, no Angel One call: only
        # uses underlying_spot already passed in by the caller (no orch.get_price()
        # fallback here, so this never adds REST/WS load).
        # Prefer TrueData's own streamed Greeks over our local Black-Scholes estimate
        # when this leg's option contract is actually subscribed on the WS and has
        # a fresh (<60s) tick — a broker-computed live Greek beats a model estimate
        # any day it's available. Falls straight through to the existing local-calc
        # path otherwise (unsubscribed leg, feed not enabled for this account, or a
        # stale/missing tick) so nothing breaks for the common case.
        ws_greeks = svc.get_live_greeks_by_token(leg['token']) if svc and leg.get('token') else None
        if ws_greeks:
            leg['delta'] = ws_greeks.get('delta', leg.get('delta', 0.0))
            leg['gamma'] = ws_greeks.get('gamma', leg.get('gamma'))
            leg['theta'] = ws_greeks.get('theta', leg.get('theta'))
            leg['vega'] = ws_greeks.get('vega', leg.get('vega'))
            leg['live_iv'] = ws_greeks.get('iv', leg.get('live_iv'))
            leg['greeks_source'] = 'websocket'
        else:
            spot_for_delta = float(underlying_spot or 0)
            if spot_for_delta > 0:
                try:
                    today_date = timezone.now().astimezone(IST).date()
                    exp_date = datetime.strptime(leg.get('expiry'), "%d%b%Y").date()
                    t_days_live = max(1.0, float((exp_date - today_date).days))
                    sigma_live = _fallback_sigma(symbol, leg.get('live_iv'))
                    fresh_greeks = calculate_greeks(spot_for_delta, leg['strike'], t_days_live, sigma=sigma_live, option_type=leg['option_type'])
                    leg['delta'] = fresh_greeks.get('delta', leg.get('delta', 0.0))
                    leg['greeks_source'] = 'black_scholes'
                except Exception as delta_err:
                    logger.warning(f"[DELTA_REFRESH] Failed to refresh live delta for {symbol} {leg.get('strike')}: {delta_err}")

        # 2. Baseline Price Logic (Entry Persistence)
        # Unique cache key per Signal ID to prevent cross-trade price contamination
        cache_suffix = f"_{sig_id}" if sig_id else ""
        cache_key = f"specialist_baseline_{symbol}_{leg['strike']}_{leg['option_type']}{cache_suffix}"
        
        baseline_price = cache.get(cache_key)
        
        if not baseline_price or float(baseline_price) <= 0:
            # Recovery: Use existing metadata premium if available, else current CMP
            baseline_price = float(leg.get('sell_price', 0))
            if baseline_price <= 0:
                baseline_price = float(cmp)
            
            # Persist for 24h to lock entry
            if baseline_price > 0:
                cache.set(cache_key, baseline_price, 60 * 60 * 24)
        
        leg['sell_price'] = baseline_price
        leg['cmp'] = cmp
        
        # 3. Calculations with Integrity Guard
        # 30% TGT / 30% SL logic
        if float(cmp) <= 0:
             # If price is not yet available, suppress P&L to prevent 100% ghost profit distortion
             leg.update({'cmp': 0, 'pnl': 0, 'pnl_pct': 0, 'status': 'WAITING'})
        else:
            # Baseline Protection: Initialize baseline if not already set.
            # Do not reset on premium fluctuations (>80%) since option prices are highly volatile
            # and resetting would overwrite the locked entry price with CMP, zeroing out P&L.
            if baseline_price <= 0:
                baseline_price = float(cmp)
                cache.set(cache_key, baseline_price, 60 * 60 * 24)
                leg['sell_price'] = baseline_price
                logger.info("[STRATEGY] Initialized baseline for %s to %s", symbol, baseline_price)

            # Re-calculate zones based on confirmed baseline (1:2 Risk-to-Reward)
            # Reward: 30% Target | Risk: 15% SL | Round to 0.05 Tick Size
            leg['target_price'] = round_to_tick(baseline_price * 0.70, 0.05)
            leg['stop_loss_price'] = round_to_tick(baseline_price * 1.15, 0.05)

            # Lot size detection — fall back to this leg's own known-good value on a
            # failed fresh lookup instead of overwriting it (audit fix H18).
            ls = get_lot_size(symbol, leg.get('exchange', 'NSE')) or leg.get('lot_size', 0)
            calculated = calculate_pnl(leg['sell_price'], leg['cmp'], ls, 1)
            calculated['lot_size'] = ls
            leg.update(calculated)
            section['section_pnl'] += leg['pnl']
            
        section['legs'].append(leg)
        updated_legs.append(leg)

    # 4. Status State Machine & Persistence
    if sig_id:
        # Bug fix (found while testing M6): SignalHistory is already imported at
        # module level (top of file) — this redundant local import shadowed it for
        # the ENTIRE function scope (Python's scoping rules make a name local to a
        # function if it's assigned anywhere in the function body, regardless of
        # whether that assignment actually executes on a given call). That made
        # `SignalHistory.Status.CANCELLED` at the P&L-suppression check further
        # down raise UnboundLocalError on any call with persist_updates=False (or
        # sig_id=None) that reached that check — dormant in production today only
        # because the single live call site always passes persist_updates=True.
        with _get_signal_lock(sig_id):
            try:
                sig = SignalHistory.objects.get(id=sig_id)
                current_metadata = sig.metadata or {}
                current_metadata['legs'] = updated_legs
            
                # Transition overall signal status based on legs
                new_status = sig.status
            
                # --- C. TIME-BASED EXIT (EXIT_TIME / 3:25 PM NSE Auto-Square-Off) ---
                # Re-enabled — previously disabled per an earlier user request, which left
                # equity strangles sitting ACTIVE indefinitely past market close instead of
                # resolving same-day like every other category.
                # Just sets new_status here — the "AGGREGATE NET P&L RECORDING" block further
                # down (triggered by is_closing and was_active) locks final_pnl/final_pnl_pct
                # from these same mark-to-market updated_legs once new_status is EXPIRED, so
                # that computation doesn't need to be duplicated here.
                now_ist_time = timezone.now().astimezone(IST).time()
                if sig.status in [SignalHistory.Status.ACTIVE, SignalHistory.Status.PENDING]:
                    if now_ist_time >= EXIT_TIME:
                        if sig.status == SignalHistory.Status.ACTIVE:
                            for l in updated_legs:
                                l['status'] = 'EXPIRED'
                            new_status = SignalHistory.Status.EXPIRED
                            section['status'] = 'EXPIRED'
                            logger.info("[EOD_SQUARE_OFF] Auto-closing %s at %s IST (mark-to-market)", symbol, EXIT_TIME)
                        else:  # PENDING — never activated, no P&L
                            new_status = SignalHistory.Status.CANCELLED
                            section['status'] = 'CANCELLED'
                            logger.info("[EOD_SQUARE_OFF] Auto-cancelled PENDING %s at %s IST (never activated)", symbol, EXIT_TIME)

            
                # A. Entry: PENDING → ACTIVE with Grace Window
                # Signals stay PENDING for PENDING_GRACE_SECONDS after creation so users
                # have time to see and enter the trade. During grace, entry price floats
                # to CMP. After grace, auto-activate with current CMP as the real entry.
                # just_activated tracks a same-tick PENDING -> ACTIVE -> HIT_TARGET/HIT_SL
                # transition (fast-moving stock breaches target/SL right as grace expires,
                # before ever being recorded ACTIVE in a prior tick) — see its use in the
                # "was_active" check below, which otherwise reads sig.status from BEFORE
                # this call and would wrongly treat the position as never having been open.
                just_activated = False
                if sig.status == SignalHistory.Status.PENDING and all(l.get('cmp', 0) > 0 for l in updated_legs):
                    age_seconds = (timezone.now() - sig.generated_at).total_seconds()
                    grace_remaining = max(0, PENDING_GRACE_SECONDS - age_seconds)
                
                    if grace_remaining > 0:
                        # Still in grace window: float entry price to live CMP
                        # so the user sees what they would actually pay right now.
                        # NOTE: original_sell_price is intentionally preserved — it reflects
                        # the price shown in the initial signal notification and must never change.
                        for l in updated_legs:
                            curr_cmp = float(l.get('cmp', 0))
                            if curr_cmp > 0:
                                l['sell_price'] = curr_cmp
                                # Preserve original_sell_price (immutable entry for Telegram updates)
                                if not l.get('original_sell_price'):
                                    l['original_sell_price'] = curr_cmp
                                # Re-calculate target, stop loss, and PNL with new sell_price
                                l['target_price'] = round_to_tick(curr_cmp * 0.70, 0.05)
                                l['stop_loss_price'] = round_to_tick(curr_cmp * 1.15, 0.05)
                                # Fall back to this leg's own known-good lot_size on a failed
                                # fresh lookup instead of overwriting it (audit fix H18).
                                ls = get_lot_size(symbol, l.get('exchange', 'NSE')) or l.get('lot_size', 0)
                                calculated = calculate_pnl(l['sell_price'], l['cmp'], ls, 1)
                                calculated['lot_size'] = ls
                                l.update(calculated)
                                # Restore original_sell_price in case calculate/update clobbered it
                                if 'original_sell_price' not in l or not l['original_sell_price']:
                                    l['original_sell_price'] = l['sell_price']

                                # Reset cached baseline so it picks up the new price on activation
                                cache_suffix = f"_{sig_id}" if sig_id else ""
                                float_cache_key = f"specialist_baseline_{symbol}_{l['strike']}_{l['option_type']}{cache_suffix}"
                                cache.set(float_cache_key, curr_cmp, 60 * 60 * 24)
                    
                        section['status'] = 'PENDING'
                        # Recalculate section_pnl from updated leg pnls to prevent ghost P&L leaking
                        section['section_pnl'] = sum(float(l.get('pnl', 0)) for l in updated_legs)
                    
                        grace_min = int(grace_remaining // 60)
                        grace_sec = int(grace_remaining % 60)
                        logger.debug("[STRATEGY] Signal %s in grace period (%dm %ds remaining)", sig_id, grace_min, grace_sec)
                    else:
                        # Grace expired: lock current CMP as the real entry price and activate.
                        # NOTE: original_sell_price is intentionally preserved — it must stay as
                        # the price originally shown in the signal notification (pre-grace).
                        for l in updated_legs:
                            curr_cmp = float(l.get('cmp', 0))
                            if curr_cmp > 0:
                                # Snapshot original_sell_price before overwriting sell_price
                                if not l.get('original_sell_price'):
                                    l['original_sell_price'] = float(l.get('sell_price', curr_cmp))
                                l['sell_price'] = curr_cmp
                                # Re-calculate target, stop loss, and PNL with new sell_price
                                l['target_price'] = round_to_tick(curr_cmp * 0.70, 0.05)
                                l['stop_loss_price'] = round_to_tick(curr_cmp * 1.15, 0.05)
                                # Fall back to this leg's own known-good lot_size on a failed
                                # fresh lookup instead of overwriting it (audit fix H18).
                                ls = get_lot_size(symbol, l.get('exchange', 'NSE')) or l.get('lot_size', 0)
                                calculated = calculate_pnl(l['sell_price'], l['cmp'], ls, 1)
                                calculated['lot_size'] = ls
                                l.update(calculated)
                                # Restore original_sell_price after dict update
                                if not l.get('original_sell_price'):
                                    l['original_sell_price'] = l['sell_price']

                                cache_suffix = f"_{sig_id}" if sig_id else ""
                                lock_cache_key = f"specialist_baseline_{symbol}_{l['strike']}_{l['option_type']}{cache_suffix}"
                                cache.set(lock_cache_key, curr_cmp, 60 * 60 * 24)
                    
                        new_status = SignalHistory.Status.ACTIVE
                        just_activated = True
                        section['status'] = 'ACTIVE'
                        # Recalculate section_pnl from updated leg pnls to prevent ghost P&L leaking
                        section['section_pnl'] = sum(float(l.get('pnl', 0)) for l in updated_legs)
                    
                        logger.info("[STRATEGY] Signal %s moved PENDING -> ACTIVE (Grace expired, entry locked at live CMP)", sig_id)
                        # Telegram: Alert for signal activation (now disabled per user request, but keeping the try block structure)
                        try:
                            from stocks.services.telegram_service import maybe_send_telegram_activation
                            maybe_send_telegram_activation(sig)
                        except Exception as tg_err:
                            logger.warning("[TELEGRAM] Failed to send activation alert: %s", tg_err)
            
                # B. Exits: Check for TGT/SL Hits (ONLY for ACTIVE orders)
                any_sl = False
                all_tgt = False
                is_equity = True  # MCX removed platform-wide — every specialist signal is now NSE equity
            
                if sig.status == SignalHistory.Status.ACTIVE or new_status == SignalHistory.Status.ACTIVE:
                    # Per-leg status logic refinement (Check against TGT/SL)
                    for l in updated_legs:
                        entry = float(l.get('sell_price', 0))
                        cmp_now = float(l.get('cmp', 0))
                        tgt = float(l.get('target_price', 0))
                        sl = float(l.get('stop_loss_price', 0))
                    
                        # Prevent re-evaluating already completed legs. EXPIRED (rolled-away by
                        # rebalance_delta_neutral_strangle) must stay terminal too — otherwise the
                        # unconditional `is_equity` branch below resets it back to WAITING every
                        # cycle, which both re-enables quote drift on a dead leg and makes
                        # rebalance's `len(active_legs) != 2` check see 3 WAITING legs and bail out.
                        if l.get('status') in ['HIT_TARGET', 'HIT_SL', 'EXPIRED']:
                            continue

                        # SCALE MISMATCH GUARD: entry and cmp are both sourced from the same
                        # normalized quote pipeline (truedata_streamer divides WS ticks by 100
                        # paise->Rupees; truedata_service's REST /quote path is already Rupees,
                        # for every exchange including NFO) — see investigation notes in
                        # AUDIT_REMEDIATION_PLAN.md item 7. Root cause traced to the now-deleted
                        # MCX quote path (removed platform-wide 2026-07-24); no remaining code path
                        # was found that double-scales a live NFO option premium. Given that, a
                        # >2.5x jump here is far more likely to be a genuine large adverse move
                        # (real loss for the seller) than a leftover unit bug — auto-dividing it by
                        # 100 would silently turn a real loss into a false "win". Previously this
                        # block did exactly that (divided cmp_now by a factor of 100); it now fails
                        # safe instead: flag the leg theoretical/suspect and skip its exit check. Both
                        # the per-leg elif below and the combined-premium systematic check further
                        # down respect is_theoretical.
                        if entry < 200 and cmp_now > 500:
                            logger.error(
                                "[SCALE_MISMATCH] %s %s %s entry=%.2f cmp=%.2f — flagging theoretical, no auto-exit.",
                                sig.symbol, l.get('strike'), l.get('option_type'), entry, cmp_now
                            )
                            l['is_theoretical'] = True
                            alert_key = f"scale_mismatch_alert_{sig_id}_{l.get('strike')}_{l.get('option_type')}"
                            if not cache.get(alert_key):
                                cache.set(alert_key, True, 60 * 15)  # throttle: once per 15 min per leg
                                try:
                                    from stocks.services.telegram_service import send_telegram_message
                                    send_telegram_message(
                                        f"⚠️ <b>SCALE MISMATCH — {sig.symbol}</b>\n"
                                        f"Leg {l.get('option_type')} {l.get('strike')}: entry=₹{entry:.2f} cmp=₹{cmp_now:.2f}\n"
                                        f"Auto-exit suppressed pending manual review."
                                    )
                                except Exception as tg_err:
                                    logger.warning("[SCALE_MISMATCH] Telegram alert failed: %s", tg_err)
                            continue
                    
                        # Equities bypass single-leg exits, but still check systematic strangle
                        # overlays below (per-leg TGT/SL branch removed post-MCX removal — every
                        # specialist signal is NSE equity now, so this was the only reachable path).
                        l['status'] = 'WAITING'
                
                    # --- INSTITUTIONAL SYSTEMATIC RISK ENGINE OVERLAYS ---
                    # Exclude EXPIRED (rolled-away) legs: a symbol that has been through a delta
                    # rebalance has 3+ SELL-action legs in metadata (old rolled leg + live one),
                    # and this overlay is only meaningful across the two currently-live legs.
                    sell_legs = [l for l in updated_legs if l.get('action') == 'SELL' and l.get('status') != 'EXPIRED']
                    if len(sell_legs) == 2:
                        ce_leg = next((l for l in sell_legs if l.get('option_type') == 'CE'), None)
                        pe_leg = next((l for l in sell_legs if l.get('option_type') == 'PE'), None)

                        # GHOST/SCALE PROTECTION: this combined-premium block is the *actual* live
                        # exit path for every current specialist signal (all NSE equity — see
                        # `is_equity = True` above), since that hardcode makes the single-leg
                        # `elif ... not l.get('is_theoretical')` guard just above unreachable. Skip
                        # the systematic SL/target/profit-capture math entirely if either leg is
                        # flagged theoretical (self-healing WAF fallback, or SCALE_MISMATCH above)
                        # — otherwise a suspect price still reaches HIT_TARGET/HIT_SL undetected.
                        if (ce_leg and pe_leg and ce_leg.get('cmp', 0) > 0 and pe_leg.get('cmp', 0) > 0
                                and not ce_leg.get('is_theoretical', False) and not pe_leg.get('is_theoretical', False)):
                            ce_entry = float(ce_leg.get('original_sell_price', 0) or ce_leg.get('sell_price', 0))
                            pe_entry = float(pe_leg.get('original_sell_price', 0) or pe_leg.get('sell_price', 0))
                        
                            entry_combined = ce_entry + pe_entry
                            current_combined = float(ce_leg.get('cmp', 0)) + float(pe_leg.get('cmp', 0))
                        
                            # Theta Capture Percentage
                            theta_capture_pct = 0.0
                            if entry_combined > 0:
                                theta_capture_pct = ((entry_combined - current_combined) / entry_combined) * 100.0
                        
                            # Premium Expansion Percentage
                            premium_expansion_pct = 0.0
                            if entry_combined > 0:
                                premium_expansion_pct = ((current_combined - entry_combined) / entry_combined) * 100.0
                            
                            ce_delta = abs(float(ce_leg.get('delta', 0.0) or 0.0))
                            pe_delta = abs(float(pe_leg.get('delta', 0.0) or 0.0))
                            max_short_delta = max(ce_delta, pe_delta)

                            # Audit fix (C4): physical-settlement/assignment-risk warning,
                            # distinct from and earlier than the delta-danger auto-exit
                            # below — informational only, does not itself close the
                            # position. Index legs (cash-settled) never carry this risk.
                            assignment_risk = is_physical_settlement_risk(sig.symbol, max_short_delta)
                            section['assignment_risk'] = assignment_risk
                            sig.metadata['assignment_risk'] = assignment_risk
                            if assignment_risk:
                                logger.warning(
                                    "[ASSIGNMENT_RISK] %s is a physically-settled stock option nearing ITM "
                                    "(delta=%.2f) — a short leg that drifts/gaps ITM before expiry can be "
                                    "assigned, obligating physical delivery rather than cash settlement.",
                                    sig.symbol, max_short_delta,
                                )

                            # Classify dynamic risk state
                            risk_state = classify_risk_state(premium_expansion_pct, max_short_delta)
                            section['risk_state'] = risk_state.value
                            sig.metadata['risk_state'] = risk_state.value
                            sig.metadata['theta_capture_pct'] = round(theta_capture_pct, 2)

                            # Detect whether this is an intraday-flagged signal
                            _is_intraday = any(l.get('is_intraday') for l in sell_legs)

                            # Choose exit thresholds: intraday (fast) vs monthly (conservative)
                            if _is_intraday:
                                _sl_mult = INTRADAY_COMBINED_SL_MULT           # 1.25 (+25%)
                                # Use early fast-capture if near expiry (DTE ≤ 3)
                                try:
                                    _exp_str = ce_leg.get('expiry', '')
                                    _today_dt = timezone.now().astimezone(IST).date()
                                    _exp_dt = datetime.strptime(_exp_str, "%d%b%Y").date()
                                    _dte_now = (_exp_dt - _today_dt).days
                                except Exception:
                                    _dte_now = 99
                                _capture_pct = INTRADAY_PROFIT_CAPTURE_EARLY if _dte_now <= 3 else INTRADAY_PROFIT_CAPTURE_PCT
                            else:
                                _sl_mult = COMBINED_SL_MULTIPLIER               # 1.30 (+30%)
                                _capture_pct = PROFIT_CAPTURE_PCT               # 0.80 (80% decay)

                            # A. Combined Premium Stop Loss Check
                            if current_combined >= entry_combined * _sl_mult:
                                logger.warning(
                                    "[SYSTEMATIC_SL] %s SL: \u20b9%.2f >= \u20b9%.2f (%.0f%% expansion) | mode=%s",
                                    sig.symbol, current_combined, entry_combined * _sl_mult,
                                    premium_expansion_pct, "INTRADAY" if _is_intraday else "MONTHLY"
                                )
                                new_status = SignalHistory.Status.HIT_SL
                                section['status'] = 'SL HIT'
                                section['exit_reason'] = f"Combined Premium SL ({'+' if _is_intraday else '+'}{ int((_sl_mult-1)*100)}%)"

                            # B. Profit Capture / Theta Decay Booking Check
                            elif current_combined <= entry_combined * (1.0 - _capture_pct):
                                logger.info(
                                    "[SYSTEMATIC_TP] %s Target: \u20b9%.2f <= \u20b9%.2f (%.0f%% decay) | mode=%s | capture=%.0f%%",
                                    sig.symbol, current_combined, entry_combined * (1.0 - _capture_pct),
                                    theta_capture_pct, "INTRADAY" if _is_intraday else "MONTHLY",
                                    _capture_pct * 100
                                )
                                new_status = SignalHistory.Status.HIT_TARGET
                                section['status'] = 'TARGET HIT'
                                section['exit_reason'] = "Systematic Profit Capture"
                            
                            # C. Delta Danger auto-exit Check
                            elif max_short_delta >= SHORT_DELTA_DANGER and AUTO_EXIT_ON_DELTA_BREACH:
                                logger.warning("[DELTA_BREACH_EXIT] Auto-exiting %s: Delta %.2f exceeds danger threshold %.2f", sig.symbol, max_short_delta, SHORT_DELTA_DANGER)
                                new_status = SignalHistory.Status.HIT_SL
                                section['status'] = 'SL HIT'
                                section['exit_reason'] = f"Delta Breach (Delta={max_short_delta:.2f})"
                            
                            # D. Expiry Force-Exit DTE Guard
                            today_dt = timezone.now().astimezone(IST).date()
                            exp_date_str = ce_leg.get('expiry')
                            if exp_date_str:
                                try:
                                    exp_dt = datetime.strptime(exp_date_str, "%d%b%Y").date()
                                    dte = (exp_dt - today_dt).days
                                    if dte <= FORCE_EXIT_DTE:
                                        logger.warning("[EXPIRY_CLOSEOUT] Force-closing %s strangle: DTE is %d <= %d day threshold", sig.symbol, dte, FORCE_EXIT_DTE)
                                        new_status = SignalHistory.Status.EXPIRED
                                        section['status'] = 'EXPIRED'
                                        section['exit_reason'] = f"Expiry Force-Exit (DTE={dte})"
                                except Exception as dte_err:
                                    logger.error(f"[DTE_ERROR] Failed to parse expiry date: {dte_err}")

                    # Check for overall signal completion (equities only — MCX's "Option B" all-legs
                    # branch removed platform-wide; every specialist signal is NSE equity now).
                    if new_status in [SignalHistory.Status.HIT_SL, SignalHistory.Status.HIT_TARGET, SignalHistory.Status.EXPIRED]:
                        # Direct closure logic for equities
                        final_pnl = sum([float(lf.get('pnl', 0)) for lf in updated_legs])
                        # Audit fix M9: only the currently-live legs' entry value belongs in the
                        # % denominator — a rolled-away (EXPIRED) leg's original sell_price
                        # inflates it, understating the true % return on a rolled position (the
                        # rupee final_pnl above is already correct, since a frozen leg's pnl was
                        # locked at its actual realized value when it was rolled).
                        total_entry_val = sum([
                            float(lf.get('sell_price', 0)) * int(lf.get('lot_size', 1))
                            for lf in updated_legs if lf.get('status') != 'EXPIRED'
                        ])
                        final_pnl_pct = (final_pnl / total_entry_val * 100) if total_entry_val > 0 else 0

                        sig.metadata['final_pnl'] = round(final_pnl, 2)
                        sig.metadata['final_pnl_pct'] = round(final_pnl_pct, 2)

                        try:
                            from stocks.services.telegram_service import maybe_send_telegram_exit
                            maybe_send_telegram_exit(sig, new_status, exit_reason=section.get('exit_reason'))
                        except Exception as tg_err:
                            logger.warning("[TELEGRAM] Failed to send exit alert: %s", tg_err)
                else:
                    # Still PENDING: Keep legs in WAITING status until touched
                    for l in updated_legs:
                        l['status'] = 'WAITING'
            
                # Sync section status with final confirmed status
                if new_status == SignalHistory.Status.ACTIVE:
                    section['status'] = 'ACTIVE'
            
                # Commit Updates
                if persist_updates:
                    # --- RUN DELTA NEUTRAL REBALANCING (ROLLING) ---
                    if sig.status == SignalHistory.Status.ACTIVE:
                        rebalance_delta_neutral_strangle(sig, updated_legs, underlying_spot, sig_exchange, orch)
                    
                    current_metadata['legs'] = updated_legs
                    sig.metadata = current_metadata
                
                    # --- AGGREGATE NET P&L RECORDING ---
                    # Only lock in the P&L if we are transitioning from ACTIVE to a closed state.
                    # was_active also covers a same-tick PENDING -> ACTIVE -> closed transition
                    # (just_activated) — without it, a fast move that breaches target/SL right as
                    # grace expires would skip this block (sig.status here still reads the PENDING
                    # value from before this call) and permanently lock final_pnl at 0.0.
                    is_closing = new_status in [SignalHistory.Status.HIT_SL, SignalHistory.Status.HIT_TARGET, SignalHistory.Status.EXPIRED, SignalHistory.Status.CANCELLED]
                    was_active = (sig.status == SignalHistory.Status.ACTIVE) or just_activated
                
                    if is_closing and was_active:
                        final_pnl = sum([float(l.get('pnl', 0)) for l in updated_legs])
                        # Audit fix M9: exclude rolled-away (EXPIRED) legs from the % denominator
                        # — see the matching comment on the equities-only branch above.
                        total_entry_val = sum([
                            float(l.get('sell_price', 0)) * int(l.get('lot_size', 1))
                            for l in updated_legs if l.get('status') != 'EXPIRED'
                        ])
                        final_pnl_pct = (final_pnl / total_entry_val * 100) if total_entry_val > 0 else 0

                        sig.metadata['final_pnl'] = round(final_pnl, 2)
                        sig.metadata['final_pnl_pct'] = round(final_pnl_pct, 2)
                        logger.info("[STRATEGY] Locked Net P&L for %s: Rs.%.2f (%.2f%%)", sig.symbol, final_pnl, final_pnl_pct)

                    # For active NSE specialist equities, always update and record the live P&L in metadata
                    elif is_equity and sig.status == SignalHistory.Status.ACTIVE:
                        final_pnl = sum([float(l.get('pnl', 0)) for l in updated_legs])
                        total_entry_val = sum([
                            float(l.get('sell_price', 0)) * int(l.get('lot_size', 1))
                            for l in updated_legs if l.get('status') != 'EXPIRED'
                        ])
                        final_pnl_pct = (final_pnl / total_entry_val * 100) if total_entry_val > 0 else 0
                    
                        sig.metadata['final_pnl'] = round(final_pnl, 2)
                        sig.metadata['final_pnl_pct'] = round(final_pnl_pct, 2)

                    # If moving to ACTIVE for the first time, record the timestamp
                    if new_status == SignalHistory.Status.ACTIVE and not sig.active_time:
                        sig.active_time = timezone.now()
                        section['entry_time'] = sig.active_time.astimezone(IST).strftime("%H:%M")
                
                    sig.status = new_status
                    sig.save()
                
            except SignalHistory.DoesNotExist:
                logger.warning("[STRATEGY] Attempted to persist to non-existent signal %s", sig_id)

    # 5. P&L Suppression for Non-Entered Trades
    # Zero out P&L if the trade was never actually entered:
    #   - PENDING signals (grace window still running)
    #   - CANCELLED/EXPIRED signals that were never activated (no active_time)
    never_entered = section.get('status') == 'PENDING' or (
        section.get('status') in ['CANCELLED', 'EXPIRED', SignalHistory.Status.CANCELLED, SignalHistory.Status.EXPIRED]
        and not section.get('was_activated', False)
    )
    if never_entered:
        section['section_pnl'] = 0
        for leg in updated_legs:
            leg['pnl'] = 0
            leg['pnl_pct'] = 0

    if section['legs']:
        panel_data['sections'].append(section)
        panel_data['total_pnl'] += section['section_pnl']

    return updated_legs
