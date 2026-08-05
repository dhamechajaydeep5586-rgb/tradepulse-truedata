"""
TradePulse PRO SYSTEM Service
Implements rules for:
1. Market Direction (Nifty 50 above/below 20 & 50 EMA)
2. Short Term Watchlist (NIFTY50: Price > 50 EMA > 200 EMA + Volume + Proximity to 52w high)
3. Long Term Watchlist (Fundamentals: ROE, Growth, Debt - Top 5 Quality)
"""

import itertools
import logging
from typing import Any
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor

from stocks.models import SignalHistory, ShortTermSignal
from stocks.services.bhavcopy_service import _fetch_nifty500_symbols, _get_session

logger = logging.getLogger(__name__)

# Constants
NIFTY_INDEX = "NIFTYBEES.NS"  # Or ^NSEI
NIFTY_50_TOKEN = "NIFTY 50"   # TrueData symbol name for Nifty 50 Index
SHORT_TERM_TGT_MIN = 15.0
SHORT_TERM_TGT_MAX = 30.0
SHORT_TERM_SL_MIN = 5.0
SHORT_TERM_SL_MAX = 8.0

# ─── Nifty 50 Fallback List ───────────────────────────────────────────────────
# Narrowed from a Nifty 500 list to Nifty 50 only, 2026-07-28, matching the
# platform-wide narrowing to a single NIFTY50 universe (see shared/profiles.py).
# Used when the NSE CSV endpoint returns 403 and no cached symbols exist.
NIFTY50_FALLBACK = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "SBIN",
    "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE", "BAJAJFINSV",
    "MARUTI", "TITAN", "SUNPHARMA", "NESTLEIND", "TECHM", "WIPRO", "ULTRACEMCO",
    "HCLTECH", "NTPC", "POWERGRID", "ONGC", "BPCL", "COALINDIA", "TATAMOTORS",
    "TATASTEEL", "JSWSTEEL", "GRASIM", "EICHERMOT", "HEROMOTOCO", "DIVISLAB",
    "DRREDDY", "CIPLA", "APOLLOHOSP", "ADANIENT", "ADANIPORTS", "TRENT",
    "ASIANPAINT", "ITC", "INDUSINDBK", "M&M", "BRITANNIA", "LTIM", "BEL",
    "HDFCLIFE", "SBILIFE", "HINDALCO", "TATACONSUM",
]

def _get_ema(series: pd.Series, window: int) -> pd.Series:
    return series.ewm(span=window, adjust=False).mean()

def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df['High']
    low = df['Low']
    close = df['Close']
    prev_close = close.shift(1)
    
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    
    return tr.rolling(window=period).mean()

def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute standard Wilder's ADX (14) indicator."""
    high = df['High']
    low = df['Low']
    close = df['Close']
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)
    
    # 1. True Range
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    
    # 2. Directional Movement
    up_move = high - prev_high
    down_move = prev_low - low
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    # 3. Wilder's Smoothing
    tr_smooth = tr.ewm(alpha=1.0/period, adjust=False).mean()
    plus_dm_smooth = pd.Series(plus_dm, index=df.index).ewm(alpha=1.0/period, adjust=False).mean()
    minus_dm_smooth = pd.Series(minus_dm, index=df.index).ewm(alpha=1.0/period, adjust=False).mean()
    
    # 4. Directional Indicators
    plus_di = 100.0 * (plus_dm_smooth / tr_smooth.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm_smooth / tr_smooth.replace(0, np.nan))
    
    # 5. DX and ADX
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1.0/period, adjust=False).mean()
    return adx.fillna(0.0)

def get_market_direction() -> dict[str, Any]:
    """
    Step 1: Check Nifty 50 against its 20 & 50 EMA on daily timeframe via TrueData.
    Uses symbol "NIFTY 50" — avoids yfinance 401 errors.
    Returns bullish or bearish bias.
    """
    try:
        from stocks.services.truedata_service import get_truedata_instance
        from stocks.services import candle_store
        svc = get_truedata_instance()
        if svc:
            df = candle_store.get_candles(svc, "NIFTY50_INDEX", NIFTY_50_TOKEN, "ONE_DAY", lookback_days=120, exchange="NSE")
            if not df.empty and len(df) >= 51:
                close_price = df['Close'].iloc[-1]
                ema20 = _get_ema(df['Close'], 20).iloc[-1]
                ema50 = _get_ema(df['Close'], 50).iloc[-1]
                trend = "BULLISH" if close_price > ema20 and close_price > ema50 else "BEARISH"
                return {
                    "trend": trend,
                    "close": round(float(close_price), 2),
                    "ema20": round(float(ema20), 2),
                    "ema50": round(float(ema50), 2),
                    "rule": "Bullish → only BUY stocks | Bearish → avoid fresh buys" if trend == "BULLISH" else "Bearish → avoid fresh buys (or strict short term only)",
                    "source": "truedata"
                }
            logger.warning("TrueData Nifty data empty/insufficient (%d rows)", len(df) if not df.empty else 0)
    except Exception as ao_err:
        logger.error("TrueData market direction failed: %s", ao_err)

    return {"trend": "NEUTRAL", "close": 0, "ema20": 0, "ema50": 0, "rule": "Could not fetch market direction"}

def _analyze_short_term(ticker: str, df: pd.DataFrame, min_vol_ratio: float = 1.5) -> dict | None:
    """
    Analyze a single stock for swing trade setup using institutional rules:
    - Price > 50 EMA > 200 EMA
    - ADX > 25
    - 5% of 52w High OR 20-day High Breakout
    - Volume > 1.5x Average
    - ATR-based Stop Loss
    - Risk Reward > 2.0
    """
    if df.empty or len(df) < 201: return None
    
    close = df['Close'].iloc[-1]
    ema50 = _get_ema(df['Close'], 50).iloc[-1]
    ema200 = _get_ema(df['Close'], 200).iloc[-1]
    ema20 = _get_ema(df['Close'], 20).iloc[-1]

    # 1. Trend Filter: Price > 50 EMA > 200 EMA
    if not (close > ema50 > ema200):
        return None

    # 2. ADX Filter: ADX > 25 (Wilder's 14 period)
    adx_series = _compute_adx(df)
    adx = adx_series.iloc[-1]
    if adx < 25.0:
        return None

    # 3. Breakout Filter: Within 5% of 52-week High OR 20-day High Breakout
    high_52w = df['High'].rolling(252, min_periods=100).max().iloc[-1]
    high_20d = df['High'].rolling(20).max().iloc[-2]  # previous 20-day high (excluding today)
    
    is_near_52w_high = (close >= high_52w * 0.95)
    is_20d_breakout = (close >= high_20d)
    
    if not (is_near_52w_high or is_20d_breakout):
        return None
        
    # 4. Volume Filter: Volume > 1.5x of 20-day Volume Average
    vol_5d = df['Volume'].iloc[-5:].mean()
    vol_20d = df['Volume'].iloc[-20:].mean()
    if vol_20d == 0 or vol_5d < (vol_20d * min_vol_ratio):
        return None
        
    # 5. Liquidity Filter: Minimum trading volume threshold
    if df['Volume'].iloc[-1] < 100000:  # Minimum 100k shares traded today
        return None

    # 6. Calculate Entry: Set entry to current close price for immediate buy today
    entry_price = close
    
    # 7. ATR-based Stop Loss: Stop Loss = Entry (Close) - 2 * ATR
    atr_series = _compute_atr(df, 14)
    atr = atr_series.iloc[-1]
    actual_sl = entry_price - (2 * atr)
    
    # Safety Floor: SL should not be wider than 10%
    max_sl_floor = entry_price * 0.90
    if actual_sl < max_sl_floor:
        actual_sl = max_sl_floor
        
    sl_points = entry_price - actual_sl
    if sl_points <= 0:
        return None

    # 8. Risk Reward > 2.0 (Calculate Target to yield 2.5x RR)
    target = entry_price + (2.5 * sl_points)
    
    rr_ratio = (target - entry_price) / sl_points
    if rr_ratio < 2.0:
        return None

    return {
        "symbol": ticker.replace(".NS", ""),
        "signal": "BUY",
        "close": round(float(close), 2),
        "ema20": round(float(ema20), 2),
        "ema50": round(float(ema50), 2),
        "ema200": round(float(ema200), 2),
        "high_52w": round(float(high_52w), 2),
        "vol_ratio": round(float(vol_5d / vol_20d), 1),
        "adx": round(float(adx), 1),
        "setup": "20d Breakout" if is_20d_breakout else "52w High Breakout",
        "entry": f"Buy at current market price ₹{round(entry_price, 2)}",
        "entry_price": round(float(entry_price), 2),
        "sl": round(actual_sl, 2),
        "sl_pct": round(((entry_price - actual_sl) / entry_price) * 100, 1),
        "target": round(target, 2),
        "target_pct": f"{(target - entry_price)/entry_price*100:.1f}%",
        "trailing_rule": "Trail SL strictly using 20 EMA closing basis"
    }

def scan_short_term_stocks() -> list[dict]:
    """Scan NIFTY50 for Top 5-10 Short Term Setups using TrueData API.

    Narrowed from Nifty 500 to NIFTY50 2026-07-28 — _fetch_nifty500_symbols() (name
    kept as-is to avoid touching every call site) now sources NIFTY50 constituents,
    see bhavcopy_service.py.
    """
    from stocks.services.truedata_service import get_truedata_instance
    svc = get_truedata_instance()
    if not svc:
        logger.error("TrueData instance not available for short term scan")
        return []

    # 1a. Try fetching live NIFTY50 list from NSE (403-prone from cloud IPs)
    try:
        session = _get_session()
        symbols = list(_fetch_nifty500_symbols(session))
    except Exception as fetch_err:
        logger.warning("NIFTY50 fetch failed (%s) — using fallback list", fetch_err)
        symbols = []

    # 1b. Fallback: use pre-defined list if NSE blocked or cache empty
    if not symbols:
        logger.warning("[SHORT_TERM] NIFTY50 symbols empty — using built-in NIFTY50_FALLBACK list (%d symbols)", len(NIFTY50_FALLBACK))
        symbols = NIFTY50_FALLBACK

    # 1. Resolve tokens
    token_map = svc.get_token_map(symbols, exchange="NSE")
    if not token_map:
        logger.error("Could not resolve tokens for NIFTY50")
        return []

    # 2. Batch fetch quotes to pre-filter (in chunks of 50 to avoid WAF limit blocks)
    tokens = list(token_map.values())
    quotes = {}
    chunk_size = 50
    for chunk in itertools.batched(tokens, chunk_size):
        try:
            chunk_quotes = svc.get_bulk_quotes({"NSE": chunk}, mode="FULL")
            quotes.update(chunk_quotes)
        except Exception as e:
            logger.warning(f"Failed to fetch quotes chunk: {e}")

    # 3. Pre-filter candidates (Price moving up + liquid)
    candidates = []
    for sym, token in token_map.items():
        key = f"NSE:{token}"
        q = quotes.get(key)
        if not q: continue
        
        ltp = q.get("ltp", 0)
        change_pct = q.get("change_percent", 0)
        vol = q.get("trade_volume", 0)
        
        # Candidate pre-filter: Positive daily change + minimum liquidity
        if change_pct > 1.0 and vol > 50000:
            candidates.append({
                "symbol": sym,
                "token": token,
                "change_pct": change_pct,
                "volume": vol
            })
            
    # Sort candidates by daily percentage change and take top 40 to scan historically
    candidates.sort(key=lambda x: x["change_pct"], reverse=True)
    top_candidates = candidates[:40]
    
    # 4. Fetch Nifty Index baseline from TrueData (symbol "NIFTY 50")
    from stocks.services import candle_store

    nifty_df = candle_store.get_candles(svc, "NIFTY50_INDEX", "NIFTY 50", "ONE_DAY", lookback_days=365, exchange="NSE")
    nifty_20d_ret = 0.0
    if not nifty_df.empty and len(nifty_df) > 20:
        nifty_20d_ret = (nifty_df['Close'].iloc[-1] - nifty_df['Close'].iloc[-20]) / nifty_df['Close'].iloc[-20]

    results = []
    for cand in top_candidates:
        sym = cand["symbol"]
        tok = cand["token"]

        # Fetch daily candle data from TrueData API
        df = candle_store.get_candles(svc, sym, tok, "ONE_DAY", lookback_days=365, exchange="NSE")
        if df.empty or len(df) < 100: continue
        
        # Calculate Relative Strength
        try:
            stock_20d_ret = (df['Close'].iloc[-1] - df['Close'].iloc[-20]) / df['Close'].iloc[-20]
            if stock_20d_ret < nifty_20d_ret:
                continue
        except:
            continue
            
        res = _analyze_short_term(sym, df)
        if res:
            results.append(res)
            
    # Sort by Volume explosion heavily to get the best 5-10
    results.sort(key=lambda x: x['vol_ratio'], reverse=True)
    return results[:10]

def _fetch_long_term_quality(symbol: str, svc) -> dict | None:
    try:
        token_map = svc.get_token_map([symbol], exchange="NSE")
        token = token_map.get(symbol)
        if not token: return None

        from stocks.services import candle_store
        df = candle_store.get_candles(svc, symbol, token, "ONE_DAY", lookback_days=200, exchange="NSE")
        if df.empty or len(df) < 100: return None

        close = df['Close'].iloc[-1]
        ema50 = _get_ema(df['Close'], 50).iloc[-1]
        ema200 = _get_ema(df['Close'], 200).iloc[-1]

        # Quality Rule: Must be in structural uptrend (Price > 50 EMA > 200 EMA)
        if not (close > ema50 > ema200):
            return None

        # Calculate momentum score based on 100-day outperformance and trend strength
        perf_100d = ((close - df['Close'].iloc[-100]) / df['Close'].iloc[-100]) * 100
        vol_20d = df['Volume'].iloc[-20:].mean()

        # SL/Target for the 1-2yr hold: wider than the short-term ATR stop since this
        # is a position trade, not an intraday one. 3xATR stop, capped at 15% max risk
        # (vs short-term's 10%) to give large-cap swings room; target at 2.5x the SL
        # distance, same R:R convention as the short-term engine.
        atr14 = _compute_atr(df, period=14).iloc[-1]
        stop_loss = close - (3 * atr14)
        max_sl_floor = close * 0.85
        if stop_loss < max_sl_floor:
            stop_loss = max_sl_floor
        sl_points = close - stop_loss
        target = close + (2.5 * sl_points)

        # NOTE (2026-07-26): the fabricated fundamentals that used to be returned here
        # — roe=22.5, debt_to_equity=0.2, profit_growth=15.0, sector="Large Cap", and
        # `revenue_growth` which was actually 100-day PRICE return — have been removed.
        # They were hardcoded literals that reached the UI and read as real data, which
        # is worse than absent data because absent data is visibly absent.
        # See doc/LONG_TERM_ENGINE_V2_ARCHITECTURE.md §0 and §7.
        #
        # Fields are now either real or omitted. `momentum_100d` is named for what it
        # actually measures. Genuine fundamentals arrive with the fundamentals/ port.
        return {
            "symbol": symbol,
            "sector": None,           # no sector taxonomy exists yet — do not invent one
            "mcap": None,             # close x volume is TURNOVER, not market cap
            "momentum_100d": round(float(perf_100d), 1),
            "adv_proxy_inr": int(close * vol_20d),
            "price": round(float(close), 2),
            "stop_loss": round(float(stop_loss), 2),
            "target": round(float(target), 2),
            "entry_plan": "Buy 30% Now, 30% on 10% dip, 40% on major correction",
            "hold_rule": "Hold for 1-2 years (max 2 years). Exit early if trend breaks below 200 EMA."
        }
    except Exception:
        return None

def scan_long_term_stocks() -> list[dict]:
    """
    Scan NIFTY50 for Long Term quality stocks using TrueData API.

    Narrowed from Nifty 500 to NIFTY50 2026-07-28 (see shared/profiles.py for why).
    The bulk-quote liquidity prefilter below predates that narrowing — it was sized
    for 500 symbols and is now comfortably oversized for 50, left as-is since it's
    still correct, just no longer load-bearing at this universe size.
    """
    from stocks.services.truedata_service import get_truedata_instance
    svc = get_truedata_instance()
    if not svc:
        logger.error("TrueData instance not available for long term scan")
        return []

    try:
        session = _get_session()
        symbols = list(_fetch_nifty500_symbols(session))
    except Exception:
        symbols = []
    if not symbols:
        symbols = NIFTY50_FALLBACK
        logger.warning("[LONG_TERM] Using fallback NIFTY50 symbol list (%d)", len(symbols))

    token_map = svc.get_token_map(symbols, exchange="NSE")
    if not token_map:
        logger.error("[LONG_TERM] Token map empty")
        return []

    tokens = list(token_map.values())
    quotes = {}
    for chunk in itertools.batched(tokens, 50):
        try:
            chunk_q = svc.get_bulk_quotes({"NSE": chunk}, mode="FULL")
            quotes.update(chunk_q)
        except Exception as e:
            logger.warning("[LONG_TERM] Quote chunk failed: %s", e)

    candidates = []
    for sym, tok in token_map.items():
        q = quotes.get(f"NSE:{tok}")
        if not q:
            continue
        vol = q.get("trade_volume", 0)
        change_pct = q.get("change_percent", 0)
        if vol > 50000 and change_pct > -5.0:  # liquid, not in an acute single-day crash
            candidates.append({"symbol": sym, "volume": vol})

    candidates.sort(key=lambda x: x["volume"], reverse=True)
    top_candidates = [c["symbol"] for c in candidates[:75]]
    logger.info("[LONG_TERM] Bulk-quote prefiltered %d / %d Nifty 500 candidates for EMA check",
                len(top_candidates), len(candidates))

    results = []
    import time
    for sym in top_candidates:
        time.sleep(0.35)  # Rate limit safety: 3 req/sec limit on TrueData historical API
        res = _fetch_long_term_quality(sym, svc)
        if res:
            results.append(res)

    # Ranked by 100-day price momentum. Named honestly now — this was previously sorted
    # on a key called `revenue_growth` that had nothing to do with revenue.
    #
    # Ranking a LONG-TERM book on price momentum alone is what produced a 100%
    # single-promoter-group portfolio: correlated complexes move together, so they rank
    # together, every time. The replacement six-factor model is specified in
    # doc/LONG_TERM_ENGINE_V2_ARCHITECTURE.md §2 and is blocked on a fundamental feed.
    results.sort(key=lambda x: x['momentum_100d'], reverse=True)

    # ── Promoter-group concentration cap (audit remediation 2026-07-28) ──────────
    # Ranking on momentum alone is what produced a 100% single-promoter-group book
    # (ADANIPORTS + ADANIENT: different GICS sectors, same promoter group, both rank
    # together whenever that group has a correlated move). Reuses the same
    # apply_portfolio_constraints() machinery already proven in intraday_service.py,
    # weighed against whatever long_term positions are already open, checked against
    # the LONG_TERM profile's real max_positions (20) and max_per_promoter_group_pct
    # (12%) — not the old bare `[:5]` slice, which capped the *daily* candidate count
    # but enforced no concentration limit on the book itself.
    #
    # Sector cap is intentionally made a no-op here: no sector taxonomy exists for
    # long-term yet (sector=None above is real, not an oversight — see the honest-
    # fields note in _fetch_long_term_quality). Mapping every symbol to its own unique
    # placeholder "sector" prevents apply_portfolio_constraints' sector-cap component
    # from bucketing every candidate into one shared "UNKNOWN" group and silently
    # rejecting everything once more than max_per_sector positions are open. Only the
    # promoter-group cap — the one with real data (PromoterGroup model) and the one
    # actually responsible for the prior incident — is enforced today.
    from stocks.services.shared.portfolio_risk import apply_portfolio_constraints, record_candidates
    from stocks.services.shared.profiles import LONG_TERM

    open_positions = list(
        SignalHistory.objects.filter(
            category="long_term",
            status__in=[SignalHistory.Status.PENDING, SignalHistory.Status.ACTIVE],
        ).values("symbol")
    )
    # Widen the pool fed into the constraint filter beyond the final daily count of 5,
    # so a candidate rejected for concentration can be backfilled by the next-best
    # momentum pick instead of the day silently returning fewer than 5 candidates.
    candidate_pool = results[:15]
    all_syms = [c["symbol"] for c in candidate_pool] + [p["symbol"] for p in open_positions]
    sectors = {s: s for s in all_syms}

    accepted, rejected = apply_portfolio_constraints(
        candidate_pool, open_positions, sectors, clusters={},
        max_positions=LONG_TERM.max_positions, profile=LONG_TERM,
    )
    if rejected:
        logger.warning(
            "[LONG_TERM][CONCENTRATION] %d candidate(s) rejected by portfolio caps: %s",
            len(rejected), [(r["symbol"], r.get("reject_reason")) for r in rejected],
        )
    try:
        record_candidates("long_term", accepted, rejected)
    except Exception:
        logger.exception("[LONG_TERM] record_candidates failed — audit trail only, unaffected.")

    return accepted[:5]


def run_long_term_scan() -> dict[str, Any]:
    """Standalone long-term scan + persist, on its own daily schedule.

    Previously this only ran as a side effect of the short-term Telegram formatter
    (_send_telegram_scanner_summary in trade_engine.py) — and only on days the short-term
    strict pass came up empty. Deliberately given its own scheduler entry instead, so it
    runs every day regardless of the short-term outcome, and at its own time slot rather
    than piggybacking on whatever the short-term scan happened to do.

    Gated by LONG_TERM_GENERATION_ENABLED (settings). Re-enabled 2026-07-28 at the account
    owner's explicit request. scan_long_term_stocks() now applies the promoter-group
    concentration cap (see that function's docstring) before returning candidates, so the
    100%-single-promoter-group failure mode this engine previously hit is closed — momentum
    ranking is still the sole quality signal (the six-factor replacement in
    doc/LONG_TERM_ENGINE_V2_ARCHITECTURE.md §2 is still blocked on a fundamentals feed), but
    a correlated complex can no longer occupy the whole book.
    """
    from django.conf import settings as _settings
    if not getattr(_settings, "LONG_TERM_GENERATION_ENABLED", False):
        logger.info("[LONG_TERM] Generation disabled (LONG_TERM_GENERATION_ENABLED=False) — skipping scan.")
        return {"generated": 0, "screened": 0, "reason": "disabled"}

    lt_signals = scan_long_term_stocks()
    generated = 0
    for l in lt_signals:
        _, created = SignalHistory.objects.update_or_create(
            symbol=l["symbol"],
            category="long_term",
            status__in=[SignalHistory.Status.PENDING, SignalHistory.Status.ACTIVE],
            defaults={
                "signal_type": "BUY PIP",
                "entry_price": l["price"],
                "stop_loss": l.get("stop_loss"),
                "target": l.get("target"),
                "reason": "Long-term momentum screen (fundamentals unavailable)",
                "status": SignalHistory.Status.ACTIVE,
            },
        )
        if created:
            generated += 1
    logger.info("[LONG_TERM] Scan complete: %d candidate(s) screened, %d new position(s) opened.",
                len(lt_signals), generated)
    return {"generated": generated, "screened": len(lt_signals)}


def update_long_term_outcomes() -> dict[str, Any]:
    """Enforce the long-term hold rule: exit early if the 200 EMA trend breaks.

    Added 2026-07-28 (audit remediation). Previously "Exit early if trend breaks below
    200 EMA" (see the hold_rule string in _fetch_long_term_quality) was documentation
    only — no code anywhere ever checked it. update_pro_system_outcomes(), the function
    that once audited both short- and long-term positions, was removed platform-wide
    to fix a duplicate-alert defect on the short-term side (trade_engine.py is now the
    sole short-term auditor); nothing replaced the long-term half of what it used to do.
    Self-contained by design, same precedent as delta_hedge_service.py and
    option_buying_service.py's own auditors — long-term's exit condition (a 200-day
    EMA trend break) doesn't fit update_signal_outcomes()'s equity/commodity-oriented
    branches, which is exactly why that function explicitly excludes category="long_term".

    Deliberately does NOT touch target/stop_loss — those exist on the row but are not
    the exit mechanism for this engine (see LONG_TERM profile: sizing_mode="inverse_vol",
    "no meaningful entry stop to size against"). Only a close below the 200 EMA closes
    a position here.
    """
    from django.utils import timezone
    from stocks.services.truedata_service import get_truedata_instance
    from stocks.services import candle_store

    open_rows = list(SignalHistory.objects.filter(
        category="long_term",
        status__in=[SignalHistory.Status.PENDING, SignalHistory.Status.ACTIVE],
    ))
    if not open_rows:
        return {"checked": 0, "closed": 0}

    svc = get_truedata_instance()
    if not svc:
        logger.warning("[LONG_TERM][AUDIT] TrueData instance unavailable — skipping this cycle.")
        return {"checked": 0, "closed": 0, "reason": "service_unavailable"}

    closed = 0
    for sig in open_rows:
        try:
            token_map = svc.get_token_map([sig.symbol], exchange="NSE") or {}
            token = token_map.get(sig.symbol)
            if not token:
                continue

            # 200-day EMA needs a real runway of history to be meaningful; 250 calendar
            # days gives ~170 trading days of margin over the 200-session EMA itself.
            df = candle_store.get_candles(svc, sig.symbol, token, "ONE_DAY", lookback_days=250, exchange="NSE")
            if df is None or df.empty or len(df) < 200:
                continue

            ema200 = float(df['Close'].ewm(span=200, adjust=False).mean().iloc[-1])
            latest_close = float(df['Close'].iloc[-1])
            if latest_close >= ema200:
                continue

            entry = float(sig.entry_price)
            new_status = (
                SignalHistory.Status.HIT_TARGET if latest_close > entry
                else SignalHistory.Status.HIT_SL
            )
            sig.status = new_status
            sig.exit_price = round(latest_close, 2)
            sig.exit_time = timezone.now()
            meta = sig.metadata or {}
            meta["exit_reason"] = "TREND_BREAK_200EMA"
            sig.metadata = meta
            sig.save()
            closed += 1
            logger.info(
                "[LONG_TERM][EXIT] %s closed (%s): close ₹%.2f below 200 EMA ₹%.2f",
                sig.symbol, new_status, latest_close, ema200,
            )
            # Instant exit alert — long-term never had ANY Telegram notification before
            # (no dedicated channel of its own), added 2026-08-05 at the account owner's
            # explicit request alongside every other engine. No stored qty for this
            # engine (sizing_mode="inverse_vol", resolved at presentation time, not
            # persisted), so this reports the % move only rather than a fabricated
            # rupee P&L. Routed to the short-term channel — the closest existing
            # precedent in this file (see the "new active setups" send above).
            try:
                from stocks.services.telegram_service import send_instant_exit_alert, get_short_term_chat_id
                send_instant_exit_alert(
                    symbol=sig.symbol, category_label="🏛️ Long-Term", signal_type="BUY",
                    entry=entry, exit_price=float(sig.exit_price), qty=0,
                    reason="TREND_BREAK_200EMA", chat_id=get_short_term_chat_id(),
                )
            except Exception as tg_err:
                logger.error("[LONG_TERM][EXIT_ALERT] Failed for %s: %s", sig.symbol, tg_err)
        except Exception as e:
            logger.error("[LONG_TERM][AUDIT] %s failed: %s", sig.symbol, e)
            continue

    logger.info("[LONG_TERM][AUDIT] Checked %d position(s), closed %d on trend break.",
                len(open_rows), closed)
    return {"checked": len(open_rows), "closed": closed}


def get_pro_system_data(trigger_scan: bool = False) -> dict:
    """Master controller that returns everything (cached outside if possible)."""
    dt_today = datetime.now().date()
    
    if trigger_scan:
        st_signals = scan_short_term_stocks()
        lt_signals = scan_long_term_stocks()
        
        # Save to database for tracking (preventing duplicate rows via status-aware upsert)
        from django.utils import timezone
        new_signals = []
        for s in st_signals:
            # Check if there is already an ACTIVE signal for this symbol
            active_exists = ShortTermSignal.objects.filter(
                symbol=s["symbol"],
                status=ShortTermSignal.Status.ACTIVE
            ).exists()
            
            if not active_exists:
                # Create a new ACTIVE signal for immediate buy today
                try:
                    ShortTermSignal.objects.create(
                        symbol=s["symbol"],
                        entry_price=s["entry_price"],
                        stop_loss=s["sl"],
                        target=s["target"],
                        vol_ratio=s["vol_ratio"],
                        setup=s["setup"],
                        status=ShortTermSignal.Status.ACTIVE,
                        activated_at=timezone.now(),
                        current_price=s["close"]
                    )
                    new_signals.append(s)
                except Exception as err:
                    logger.error(f"Failed to create swing setup: {err}")
        
        # Instantly send Telegram alert for the new BUY TODAY swing selections
        if new_signals:
            logger.info(f"[SHORT_TERM] Found {len(new_signals)} new active swing setups: {[s['symbol'] for s in new_signals]}")
            try:
                from stocks.services.telegram_service import send_telegram_message, get_short_term_chat_id
                chat_id = get_short_term_chat_id()
                
                lines = []
                for idx, s in enumerate(new_signals, 1):
                    lines.append(
                        f" {idx}. <b>{s['symbol']}</b> (ADX: {s.get('adx', 'N/A')})\n"
                        f"    ▸ Buy Today: <b>₹{s['entry_price']}</b> (Current Price)\n"
                        f"    ▸ Target: <b>₹{s['target']}</b>\n"
                        f"    ▸ Stop Loss: <b>₹{s['sl']}</b> ({s['sl_pct']}%)\n"
                    )
                
                header = "🚀 <b>NEW SHORT-TERM SWING SETUPS (BUY TODAY)</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                footer = "\nAction: <b>Execute at current market price before market close</b> ⚡\n━━━━━━━━━━━━━━━━━━━━"
                msg = header + "\n".join(lines) + footer
                send_telegram_message(msg, chat_id=chat_id)
            except Exception as tg_err:
                logger.error(f"Failed to send new active setups Telegram message: {tg_err}")
            
        # ── LONG-TERM GENERATION IS DISABLED ─────────────────────────────────────
        # Ranking a 1-2 year book on 100-day price momentum, with no fundamental input
        # and no working exit path, is what produced a 100% single-promoter-group
        # portfolio. Emitting nothing is strictly better than emitting that.
        #
        # Re-enable only when the fundamentals port is live and the six-factor model in
        # doc/LONG_TERM_ENGINE_V2_ARCHITECTURE.md §2 is in place. The gate is a setting
        # so it can be flipped without a code change once that lands.
        #
        # Also fixed here: this update_or_create passed `generated_at__date` as a lookup
        # against an auto_now_add field. Django silently drops `__` keys before create,
        # so it did not raise — but the date-scoped .get() meant a second run on a later
        # day tried to CREATE a duplicate ACTIVE row, violating the unique_live_signal
        # partial index with an uncaught IntegrityError.
        from django.conf import settings as _settings
        if getattr(_settings, "LONG_TERM_GENERATION_ENABLED", False):
            for l in lt_signals:
                SignalHistory.objects.update_or_create(
                    symbol=l["symbol"],
                    category="long_term",
                    status__in=[SignalHistory.Status.PENDING, SignalHistory.Status.ACTIVE],
                    defaults={
                        "signal_type": "BUY PIP",
                        "entry_price": l["price"],
                        "stop_loss": l.get("stop_loss"),
                        "target": l.get("target"),
                        "reason": "Long-term momentum screen (fundamentals unavailable)",
                        "status": SignalHistory.Status.ACTIVE,
                    },
                )
        elif lt_signals:
            logger.info(
                "[LONG_TERM] Generation disabled — %d candidate(s) screened but NOT "
                "persisted. Set LONG_TERM_GENERATION_ENABLED=True once the fundamentals "
                "port is live. Candidates: %s",
                len(lt_signals), [s["symbol"] for s in lt_signals],
            )

    # Always fetch current active/pending signals from DB for the UI response
    active_st_db = ShortTermSignal.objects.exclude(
        status__in=[
            ShortTermSignal.Status.HIT_TARGET,
            ShortTermSignal.Status.HIT_SL,
        ]
    )
    
    # Fetch active Long Term signals from SignalHistory
    active_lt_db = SignalHistory.objects.filter(
        category="long_term",
        status=SignalHistory.Status.ACTIVE
    )
    formatted_lt = []
    for l in active_lt_db:
        # NOTE (2026-07-26): a SECOND set of fabricated fundamentals lived here —
        # roe=25.0, debt_to_equity=0.1, revenue_growth=15.0, profit_growth=12.0 — with
        # values that did not even agree with the first set in _fetch_long_term_quality
        # (22.5 / 0.2 / 15.0). Both are removed. The `hold_rule` string also cited an
        # "exit if ROE drops below 10%" condition that could never be evaluated because
        # ROE was never fetched.
        formatted_lt.append({
            "symbol": l.symbol,
            "sector": None,
            "mcap": None,
            "price": float(l.entry_price),
            "entry_plan": "Buy 30% Now, 30% on 10% dip, 40% on major correction",
            "hold_rule": "Hold 1-2 years. Exit on trend break below 200 EMA. "
                         "NOTE: not currently enforced in code — manual discipline.",
            "fundamentals_available": False,
        })
    
    formatted_st = []
    for s in active_st_db:
        formatted_st.append({
            "symbol": s.symbol,
            "signal": "BUY",
            "close": float(s.current_price or s.entry_price),
            "vol_ratio": float(s.vol_ratio or 0.0),
            "setup": s.setup,
            "entry": f"Near ₹{round(s.entry_price, 2)} (Pullback)",
            "entry_price": float(s.entry_price),
            "sl": float(s.stop_loss),
            "sl_pct": round(((float(s.entry_price) - float(s.stop_loss)) / float(s.entry_price)) * 100, 1) if s.entry_price > 0 else 5.0,
            "target": float(s.target) if s.target else 0.0,
            "target_pct": f"{((float(s.target) - float(s.entry_price)) / float(s.entry_price) * 100):.1f}%" if (s.target and s.entry_price) else "20.0%",
            "status": s.status
        })

    return {
        "market_direction": get_market_direction(),
        "short_term": formatted_st,
        "long_term": formatted_lt,
        "timestamp": datetime.utcnow().isoformat()
    }


# update_pro_system_outcomes() was REMOVED here (audit remediation Phase 2 #2.6,
# 2026-07-29). It was a full second PENDING→ACTIVE→HIT_TARGET/HIT_SL implementation
# for both SignalHistory(category="long_term") and ShortTermSignal, including its own
# Telegram sends, that had been unreachable (zero call sites) since it was pulled from
# the scheduler to fix a duplicate-alert defect — see the removal note in
# live_signal_service.py and update_long_term_outcomes()'s docstring above for what
# replaced its long-term half. It stayed importable as dead code; deleted outright
# rather than left as an attractive nuisance for a future accidental re-wire.

def get_pro_performance_report(target_date=None) -> dict:
    # NOTE: update_pro_system_outcomes() was REMOVED from this file entirely (audit
    # remediation Phase 2 #2.6) — it used to duplicate trade_engine.py's exit/alert
    # logic on the same ShortTermSignal rows, and calling it from a GET-triggered
    # report view meant simply opening the Reports page could silently fire duplicate
    # Telegram alerts. This view is now read-only; trade_engine.py's scheduled jobs are
    # the sole source of truth for updating outcomes.
    #
    # target_date: Short-Term Buy is the one Dashboard category that lives on its own
    # model (ShortTermSignal, not SignalHistory) — get_performance_report() in
    # live_signal_service.py already handles date-filtered history for the other five
    # categories natively; this optional filter brings Short-Term to parity with those
    # for the unified date-wise Reports page, without changing default (all-time,
    # last-50) behavior for any existing caller that doesn't pass a date.

    st_signals = ShortTermSignal.objects.all().order_by('-generated_at')
    if target_date:
        st_signals = st_signals.filter(generated_at__date=target_date)
    lt_signals = SignalHistory.objects.filter(category="long_term").order_by('-generated_at')
    
    def _aggregate_st(qs) -> dict:
        total = qs.count()
        wins = qs.filter(status=ShortTermSignal.Status.HIT_TARGET).count()
        losses = qs.filter(status=ShortTermSignal.Status.HIT_SL).count()
        closed = wins + losses
        win_rate = round((wins / closed * 100), 1) if closed > 0 else 0
        
        # Format history
        history = []
        for s in qs[:50]: # Last 50 entries
            # --- Smart Qty logic ---
            max_risk_inr = 5000
            sl_points = abs(float(s.entry_price) - float(s.stop_loss))
            qty = max(1, int(max_risk_inr / sl_points)) if sl_points > 0 else 100
            
            pnl_amt = 0
            pnl_pct = float(s.pnl_pct) if s.pnl_pct is not None else 0
            if pnl_pct != 0:
                pnl_amt = (pnl_pct / 100) * float(s.entry_price) * qty
            
            history.append({
                "symbol": s.symbol,
                "strategy": "short_term",
                "entry": float(s.entry_price),
                "sl": float(s.stop_loss),
                "target": float(s.target),
                "status": s.status,
                "generated_at": s.generated_at.isoformat(),
                "exit_time": s.exited_at.isoformat() if s.exited_at else None,
                "exit_price": float(s.exit_price) if s.exit_price else None,
                "qty": qty,
                "pnl": round(pnl_amt, 2),
                "pnl_pct": round(pnl_pct, 2)
            })
            
        return {
            "total_signals": total,
            "closed_trades": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "history": history
        }

    def _aggregate_lt(qs) -> dict:
        total = qs.count()
        wins = qs.filter(status=SignalHistory.Status.HIT_TARGET).count()
        losses = qs.filter(status=SignalHistory.Status.HIT_SL).count()
        closed = wins + losses
        win_rate = round((wins / closed * 100), 1) if closed > 0 else 0
        
        # Format history
        history = []
        for s in qs[:50]: # Last 50 entries
            # --- Smart Qty logic ---
            # Long-term has no target/SL (buy-and-hold) — fall back to a flat qty.
            if s.stop_loss is not None:
                max_risk_inr = 5000
                sl_points = abs(float(s.entry_price) - float(s.stop_loss))
                qty = max(1, int(max_risk_inr / sl_points)) if sl_points > 0 else 100
            else:
                qty = 100

            pnl_amt = 0
            pnl_pct = 0
            if s.status in [SignalHistory.Status.HIT_TARGET, SignalHistory.Status.HIT_SL, SignalHistory.Status.EXPIRED] and s.exit_price:
                diff = float(s.exit_price) - float(s.entry_price)
                if s.signal_type == "SELL": diff = -diff
                pnl_amt = diff * qty
                pnl_pct = (diff / float(s.entry_price)) * 100

            history.append({
                "symbol": s.symbol,
                "strategy": s.category,
                "entry": float(s.entry_price),
                "sl": float(s.stop_loss) if s.stop_loss is not None else None,
                "target": float(s.target) if s.target is not None else None,
                "status": s.status,
                "generated_at": s.generated_at.isoformat(),
                "exit_time": s.exit_time.isoformat() if s.exit_time else None,
                "exit_price": float(s.exit_price) if s.exit_price else None,
                "qty": qty,
                "pnl": round(pnl_amt, 2),
                "pnl_pct": round(pnl_pct, 2)
            })
            
        return {
            "total_signals": total,
            "closed_trades": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "history": history
        }
        
    return {
        "short_term": _aggregate_st(st_signals),
        "long_term": _aggregate_lt(lt_signals)
    }

