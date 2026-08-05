"""
TrueData REST + streaming service.

Replaces angel_one_service.py. Same public method surface (get_candle_data,
get_bulk_quotes, get_stock_quote, get_option_quote, get_token_map,
get_fo_stocks, get_nearest_strike, is_market_open, get_pulse_status,
disconnect) plus the same module-level singleton pattern
(get_truedata_instance / initialize_truedata / is_truedata_ready), so every
caller elsewhere in the codebase only needs its import line changed, not its
logic — see doc/TRUEDATA_MIGRATION_PLAN.md Phase 4.

Design decision this file depends on: "token" is now the TrueData symbol
string (e.g. "RELIANCE", "NIFTY-I", "CRUDEOIL-I"), not a numeric broker
token — TrueData's REST/WS APIs are symbol-name addressed, there is no
separate token to resolve first. get_token_map() below returns an identity
map for exactly this reason. Nothing downstream ever parses "token" as an
int (verified against the codebase before making this the design), so this
requires zero changes at any call site beyond the import swap.

Endpoints and formats are taken directly from TrueDataAPIDocument/ (the
Market Data API v2.6 PDF + the TD Postman collections), not guessed.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd
import requests

from .truedata_streamer import TrueDataStreamer, get_stream_price, get_stream_greeks, _STREAM_LOCK, _STREAM_CACHE

logger = logging.getLogger(__name__)

AUTH_URL = "https://auth.truedata.in/token"
HISTORY_BASE = "https://history.truedata.in"
MASTER_BASE = "https://api.truedata.in"
GREEKS_BASE = "https://greeks.truedata.in/api"

# Same choke-point pattern as angel_one_service._REST_CALL_LOCK: every REST
# call is paced + serialized through one lock, retried once on a transient
# connection error. TrueData documents 10 req/sec for bar data and 5 req/sec
# for tick data (Market Data API doc, section 4.c.i) — pacing at 1/sec stays
# well under both, matching this codebase's existing philosophy of never
# running close to a documented ceiling.
_REST_CALL_LOCK = threading.Lock()
_LAST_REST_CALL = 0.0
_REST_CIRCUIT_BREAKER_UNTIL: dict[str, float] = {"candle": 0.0, "quote": 0.0}

_AUTH_LOCK = threading.Lock()
AUTH_RETRY_COOLDOWN = 60.0


def _is_quota_exceeded(response: requests.Response) -> bool:
    """TrueData's documented rate-limit signal is plain text in a 200 response body
    ("API calls quota exceeded! maximum admitted 1 per Second." — Market Data API
    doc, tick/bar history error tables), not a distinct HTTP status. Checked
    alongside 403/429 so a real breach still trips the circuit breaker even when
    it doesn't come back as one of those codes."""
    return "quota exceeded" in response.text[:200].lower()

# Angel One interval names -> TrueData interval names, so every call site
# passing "FIVE_MINUTE" etc. (unchanged from the Angel One era) keeps working.
_INTERVAL_MAP = {
    "ONE_MINUTE": "1min",
    "THREE_MINUTE": "3min",
    "FIVE_MINUTE": "5min",
    "TEN_MINUTE": "10min",
    "FIFTEEN_MINUTE": "15min",
    "THIRTY_MINUTE": "30min",
    "SIXTY_MINUTE": "60min",
    "ONE_DAY": "eod",
}


class TrueDataService:
    """Service for TrueData REST + WebSocket integration."""

    def __init__(self, username: str, password: str, ws_port: int = 8084):
        self.username = username
        self.password = password
        self.ws_port = ws_port
        self.access_token: str | None = None
        self.token_expires_at: float = 0.0
        self.is_authenticated = False
        self.session = requests.Session()
        self.streamer: TrueDataStreamer | None = None
        self._restart_lock = threading.Lock()

    # ------------------------------------------------------------------ auth

    def authenticate(self) -> bool:
        """OAuth2 password-grant login against auth.truedata.in — see Market
        Data API doc section 4.c.ii. Returns a bearer token valid for
        `expires_in` seconds (TrueData renews the underlying session daily
        around 4am regardless, so this is the effective ceiling, not just a
        local guess)."""
        try:
            response = self._rest_request(
                "POST", AUTH_URL,
                data={"username": self.username, "password": self.password, "grant_type": "password"},
                timeout=10,
            )
            if response.status_code != 200:
                logger.error("[TRUEDATA] Auth HTTP error: %s - %s", response.status_code, response.text[:200])
                return False

            data = response.json()
            if "error" in data:
                logger.error("[TRUEDATA] Auth error: %s - %s", data.get("error"), data.get("error_description"))
                return False

            self.access_token = data["access_token"]
            self.token_expires_at = time.time() + float(data.get("expires_in", 3600))
            self.is_authenticated = True
            self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})

            logger.info("[TRUEDATA] Authenticated for %s", self.username)

            from stocks.services.signal_utils import is_static_closed
            if not is_static_closed("NSE"):
                # authenticate() re-runs every ~3.9h on token near-expiry, on the one
                # long-lived worker process this app runs as. Without stopping the
                # previous streamer first, every refresh orphans its socket and both
                # background threads — a zombie whose health monitor keeps retrying
                # forever, contending for TrueData's single allowed WS session per
                # login. Mirrors the existing disconnect() cleanup below.
                if self.streamer:
                    try:
                        self.streamer.stop()
                    except Exception as e:
                        logger.warning("[TRUEDATA] Error stopping previous streamer before reconnect: %s", e)
                self.streamer = TrueDataStreamer(self.username, self.password, self.ws_port, restart_lock=self._restart_lock)
                self.streamer.start()

            return True
        except Exception as e:
            logger.error("[TRUEDATA] Auth exception: %s", e)
            return False

    def _ensure_fresh_token(self):
        """TrueData tokens are valid for `expires_in` seconds (~3.8h) and the
        backend renews the underlying session daily near 4am regardless — if
        we're within 5 minutes of expiry, just re-auth rather than risk a
        mid-request 401.

        Bug fix (found in a follow-up audit): this used to call
        self.authenticate() directly, unguarded — called from every REST method
        (get_candle_data, get_bulk_quotes, get_fo_stocks, etc.), so two threads
        both deciding the token is near-expiry could both race through
        authenticate()'s streamer stop-then-replace sequence concurrently,
        orphaning whichever streamer lost the race — the exact zombie-WebSocket
        bug the C6 fix was written to prevent, just reachable via a second,
        unguarded path. CLAUDE.md already states re-auth must go through the
        same guarded path as initialize_truedata() — this didn't. Now does,
        via the same module-level _AUTH_LOCK, with the freshness check
        re-verified inside the lock so a thread that lost the race to a
        concurrent re-auth doesn't redundantly authenticate a second time.
        """
        if self.is_authenticated and time.time() <= self.token_expires_at - 300:
            return
        with _AUTH_LOCK:
            if self.is_authenticated and time.time() <= self.token_expires_at - 300:
                return  # another thread already refreshed it while we waited
            self.authenticate()

    def _rest_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Paced, serialized, retry-once wrapper — see module docstring on
        _REST_CALL_LOCK. Mirrors angel_one_service._rest_request's reasoning
        for the SSLError/ConnectionError retry (long-lived pooled connection
        going stale between bursty ~15-min scan cycles)."""
        global _LAST_REST_CALL
        with _REST_CALL_LOCK:
            for attempt in (1, 2):
                elapsed = time.time() - _LAST_REST_CALL
                if elapsed < 1.0:
                    time.sleep(1.0 - elapsed)
                _LAST_REST_CALL = time.time()
                try:
                    return self.session.request(method, url, **kwargs)
                except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                    if attempt == 2:
                        raise
                    logger.warning("[TRUEDATA] Transient connection error on %s, retrying once: %s", url, e)

    # -------------------------------------------------------------- candles

    def get_candle_data(self, token: str | int, exchange: str, interval: str, from_date: str, to_date: str) -> pd.DataFrame:
        """Historical OHLCV via TrueData's getbars. `token` is the TrueData
        symbol string (see module docstring); `exchange` is accepted but
        unused — TrueData symbols are already segment-qualified
        (e.g. "CRUDEOIL-I" vs "RELIANCE"), matching how every call site
        already threads `exchange` through without this service needing it.

        `from_date`/`to_date` arrive in Angel One's "YYYY-MM-DD HH:MM" format
        (candle_store.py's convention) — converted here to TrueData's
        "yymmddTHH:MM:SS".
        """
        from django.core.cache import cache as _dj_cache

        symbol = str(token)
        td_interval = _INTERVAL_MAP.get(interval, interval)

        cache_key = None
        try:
            fmt = "%Y-%m-%d %H:%M" if len(from_date) > 10 else "%Y-%m-%d"
            lookback_days = round((datetime.strptime(to_date, fmt) - datetime.strptime(from_date, fmt)).total_seconds() / 86400)
            cache_key = f"td_candle_cache_{symbol}_{td_interval}_{lookback_days}"
            cached_df = _dj_cache.get(cache_key)
            if cached_df is not None:
                return cached_df.copy()
        except Exception:
            fmt = "%Y-%m-%d %H:%M" if len(from_date) > 10 else "%Y-%m-%d"
            cache_key = None

        now = time.time()
        if now < _REST_CIRCUIT_BREAKER_UNTIL["candle"]:
            logger.debug("[TRUEDATA] CIRCUIT BREAKER ACTIVE - skipping candle REST for symbol=%s", symbol)
            return pd.DataFrame()

        self._ensure_fresh_token()
        try:
            td_from = datetime.strptime(from_date, fmt).strftime("%y%m%dT%H:%M:%S")
            td_to = datetime.strptime(to_date, fmt).strftime("%y%m%dT%H:%M:%S")
            url = f"{HISTORY_BASE}/getbars"
            params = {"symbol": symbol, "from": td_from, "to": td_to, "response": "csv", "interval": td_interval}
            response = self._rest_request("GET", url, params=params, timeout=15)

            if response.status_code in (403, 429) or (response.status_code == 200 and _is_quota_exceeded(response)):
                _REST_CIRCUIT_BREAKER_UNTIL["candle"] = time.time() + 300
                logger.error("[TRUEDATA] Rate limit detected during candle fetch. Disabled for 5 minutes.")
                return pd.DataFrame()
            if response.status_code != 200:
                logger.error("[TRUEDATA] CANDLE_HTTP_ERROR symbol=%s status=%s", symbol, response.status_code)
                return pd.DataFrame()

            text = response.text.strip()
            if not text or text.startswith("No Data exists") or text.startswith("{"):
                return pd.DataFrame()

            df = pd.read_csv(io.StringIO(text))
            if df.empty or "timestamp" not in df.columns:
                return pd.DataFrame()

            df = df.rename(columns={
                "timestamp": "Datetime", "open": "Open", "high": "High",
                "low": "Low", "close": "Close", "volume": "Volume",
            })
            df["Datetime"] = pd.to_datetime(df["Datetime"])
            df.set_index("Datetime", inplace=True)
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            # A malformed source row (e.g. a non-numeric field) coerces to NaN above
            # rather than raising — Postgres accepts NaN silently, so an un-dropped row
            # here permanently corrupts the candle store. Drop before caching/returning.
            ohlcv_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
            if ohlcv_cols:
                df = df.dropna(subset=ohlcv_cols)

            if cache_key:
                _dj_cache.set(cache_key, df, timeout=240)
            return df
        except Exception as e:
            logger.error("[TRUEDATA] CANDLE_EXCEPTION symbol=%s: %s", symbol, e)
            return pd.DataFrame()

    # --------------------------------------------------------------- quotes

    def get_bulk_quotes(self, exchange_token_map: Dict[str, List[str]], mode: str = "FULL") -> Dict[str, Any]:
        """Fetch quotes for multiple symbols in one call via getLTPBulk.

        `exchange_token_map` keeps Angel One's `{"NSE": [...], "MCX": [...]}`
        shape for call-site compatibility, but TrueData's bulk endpoint takes
        one flat symbol list with no exchange grouping — flattened here.
        Returns `{"EXCHANGE:symbol": {...}}` to match the existing key format
        every caller (candle_store, gateway, intraday_service) already parses.
        """
        results: dict[str, Any] = {}
        to_fetch: list[tuple[str, str]] = []  # (exchange, symbol)

        for exchange, symbols in exchange_token_map.items():
            for s in symbols:
                symbol = str(s)
                tick = get_stream_price(symbol)
                is_fresh = False
                if tick and tick.get("ltp", 0) > 0:
                    age = time.time() - tick.get("fetch_time", 0)
                    is_ws = tick.get("source") == "websocket"
                    has_ohlc = tick.get("high", 0) > 0 and tick.get("low", 0) > 0
                    # A tick's age is checked regardless of source — a WS-sourced cache
                    # entry from a silently-dead socket must expire and fall through to
                    # the REST path below, not be trusted forever just because it once
                    # came from the WebSocket. WS gets a longer bound (streaming ticks
                    # are pushed continuously so a fresh one is rarely far behind) than a
                    # REST-fetched one (fetched on-demand, so staleness there means the
                    # fetch itself is old).
                    fresh_bound = 60 if is_ws else 30
                    if age < fresh_bound and (mode != "FULL" or has_ohlc):
                        is_fresh = True
                if is_fresh:
                    results[f"{exchange}:{symbol}"] = tick
                else:
                    to_fetch.append((exchange, symbol))

        if not to_fetch:
            return results

        now = time.time()
        if now < _REST_CIRCUIT_BREAKER_UNTIL["quote"]:
            logger.warning("[TRUEDATA] CIRCUIT BREAKER ACTIVE - skipping bulk quote REST")
            return results

        # Warm the WebSocket cache so the next call is served from streaming data.
        if self.streamer:
            self.streamer.subscribe(0, [s for _, s in to_fetch])

        self._ensure_fresh_token()
        try:
            url = f"{HISTORY_BASE}/getLTPBulk"
            symbols_param = ",".join(s for _, s in to_fetch)
            response = self._rest_request("GET", url, params={"symbols": symbols_param, "response": "csv"}, timeout=15)

            if response.status_code in (403, 429) or (response.status_code == 200 and _is_quota_exceeded(response)):
                _REST_CIRCUIT_BREAKER_UNTIL["quote"] = time.time() + 300
                logger.error("[TRUEDATA] Rate limit detected during bulk quote fetch. Disabled for 5 minutes.")
                return results

            if response.status_code == 200:
                # getLTPBulk's CSV has no symbol column — rows come back in request
                # order only, so they must be paired to `to_fetch` by position. A
                # short or reordered response with no length check silently shifts
                # every subsequent pairing, misattributing prices for the rest of the
                # batch with nothing logged. Validate row count first; abort (and log)
                # the whole batch on any mismatch instead of guessing a partial pairing.
                rows = list(csv.DictReader(io.StringIO(response.text.strip())))
                if len(rows) != len(to_fetch):
                    logger.error(
                        "[TRUEDATA] BULK_QUOTE_ROW_MISMATCH requested=%d got=%d — "
                        "aborting batch, refusing to pair by guesswork.",
                        len(to_fetch), len(rows),
                    )
                else:
                    for (exchange, symbol), row in zip(to_fetch, rows):
                        ltp = float(row.get("ltp") or row.get("LTP") or 0)
                        if ltp <= 0:
                            continue
                        tick = {
                            "ltp": ltp,
                            "high": float(row.get("high", 0) or 0),
                            "low": float(row.get("low", 0) or 0),
                            "close": float(row.get("close", 0) or row.get("prevclose", 0) or 0),
                            "change": float(row.get("change", 0) or 0),
                            "change_percent": float(row.get("changeper", 0) or row.get("change_percent", 0) or 0),
                            "trade_volume": float(row.get("volume", 0) or 0),
                            "open_interest": float(row.get("oi", 0) or 0),
                            "source": "rest_fallback",
                            "fetch_time": time.time(),
                        }
                        results[f"{exchange}:{symbol}"] = tick
                        with _STREAM_LOCK:
                            _STREAM_CACHE[symbol] = tick
            else:
                logger.warning("[TRUEDATA] BULK_QUOTE_HTTP_ERROR status=%s", response.status_code)
        except Exception as e:
            logger.error("[TRUEDATA] Bulk quote fetch failed: %s", e)

        return results

    def get_stock_quote(self, symbol: str) -> dict[str, Any] | None:
        """Live quote for one equity symbol — delegates to get_bulk_quotes so
        there's exactly one REST quote implementation, not two to keep in sync."""
        quotes = self.get_bulk_quotes({"NSE": [symbol]})
        quote = quotes.get(f"NSE:{symbol}")
        if quote:
            quote = dict(quote)
            quote["symbol"] = symbol
            quote["token"] = symbol
            quote["exchange"] = "NSE"
            quote["trading_symbol"] = symbol
        return quote

    def get_live_price_by_token(self, token: str | int, exchange: str = "NSE") -> dict[str, Any] | None:
        """Kept for call-site compatibility with the few places that call this
        directly rather than get_stock_quote/get_bulk_quotes.

        Audit fix L9: this used to return any non-zero cached WS tick regardless
        of age, bypassing the same staleness bound get_bulk_quotes() enforces
        (audit fix C5) — a docstring elsewhere (market_data/ws_read.py) claimed
        this function already did staleness checking, which wasn't true. Same
        60s bound as get_bulk_quotes's WS path, for consistency.
        """
        symbol = str(token)
        tick = get_stream_price(symbol)
        if tick and tick.get("ltp", 0) > 0:
            age = time.time() - tick.get("fetch_time", 0)
            if age < 60:
                return tick
        return self.get_bulk_quotes({exchange: [symbol]}).get(f"{exchange}:{symbol}")

    def get_live_greeks_by_token(self, token: str | int) -> dict[str, Any] | None:
        """WS-only, unlike get_live_price_by_token above — TrueData's Greeks feed
        is an account-level backend toggle with no REST equivalent for a single
        live contract's streaming greeks (see truedata_streamer._GREEKS_CACHE),
        so there is nothing to fall back to here. Returns None if this account's
        feed isn't enabled or the last tick is stale; callers (delta_hedge_service)
        are expected to fall back to their own local Black-Scholes calculation in
        that case. Same 60s freshness bound as get_live_price_by_token.
        """
        greeks = get_stream_greeks(str(token))
        if greeks and (time.time() - greeks.get("fetch_time", 0)) < 60:
            return greeks
        return None

    # -------------------------------------------------------------- symbols

    def get_token_map(self, symbols: list[str], exchange: str = "NSE") -> dict[str, str]:
        """Identity map — see module docstring. TrueData needs no separate
        token resolution step; the symbol name IS the identifier used by
        every other method in this class."""
        return {s: s for s in symbols}

    def get_fo_stocks(self) -> list[str]:
        """F&O-eligible underlyings via getunderlyinglist?segment=fo."""
        self._ensure_fresh_token()
        try:
            url = f"{MASTER_BASE}/getunderlyinglist"
            params = {"segment": "fo", "user": self.username, "password": self.password}
            response = self._rest_request("GET", url, params=params, timeout=15)
            if response.status_code != 200:
                logger.error("[TRUEDATA] getunderlyinglist HTTP %s", response.status_code)
                return []
            text = response.text.strip()
            # Documented as csv/plain list of names, one per line or comma-separated.
            if "," in text and "\n" not in text:
                names = [n.strip() for n in text.split(",")]
            else:
                names = [line.strip() for line in text.splitlines() if line.strip()]
            return sorted({n for n in names if n and not n.lower().startswith("symbol")})
        except Exception as e:
            logger.error("[TRUEDATA] get_fo_stocks failed: %s", e)
            return []

    def get_expiry_list(self, symbol: str) -> list[str]:
        """All available expiries for `symbol`, normalized to Angel One's old
        '%d%b%Y' format (e.g. "25JUN2026") — delta_hedge_service.py's
        resolve_target_expiry()/expiry_str_to_date() parse exactly that format,
        and both are shared rollover logic used by more than one call site, so
        normalizing here at the boundary is a smaller/safer change than touching
        that shared function's format assumption. TrueData's getSymbolExpiryList
        (Market Data API doc doesn't cover it; confirmed via the TD Postman
        collection) returns yyyymmdd.

        Cached 15 min: expiry lists are static within a trading day, but
        get_lot_size/get_nse_option_strikes/get_nse_option_quote each call this
        independently for the same symbol — uncached, one specialist scan
        (~28 candidates) fanned this out into hundreds of redundant REST calls
        and tripped the quote circuit breaker for the whole scan, not just one
        stock. Same fix pattern as get_candle_data's cache below.
        """
        from django.core.cache import cache as _dj_cache
        cache_key = f"td_expiry_list_{symbol}"
        cached = _dj_cache.get(cache_key)
        if cached is not None:
            return cached

        self._ensure_fresh_token()
        try:
            url = f"{HISTORY_BASE}/getSymbolExpiryList"
            response = self._rest_request("GET", url, params={"symbol": symbol, "response": "csv"}, timeout=15)
            if response.status_code != 200:
                return []
            out = []
            for line in response.text.strip().splitlines():
                raw = line.split(",")[0].strip()
                for fmt in ("%Y%m%d", "%y%m%d", "%Y-%m-%d"):
                    try:
                        out.append(datetime.strptime(raw, fmt).strftime("%d%b%Y").upper())
                        break
                    except ValueError:
                        continue
            if out:
                _dj_cache.set(cache_key, out, timeout=900)
            return out
        except Exception as e:
            logger.error("[TRUEDATA] get_expiry_list failed for %s: %s", symbol, e)
            return []

    def get_option_chain(self, symbol: str, expiry: str) -> list[dict]:
        """Strikes for one symbol/expiry, shaped like Angel One's old NFO
        instrument-master rows (token/symbol/expiry/strike/lotsize/
        instrumenttype) so the three functions in delta_hedge_service.py that
        used to read `_INSTRUMENT_MASTER_CACHE["nfo"]` directly (get_lot_size,
        get_nse_option_strikes, get_nse_option_quote) need only their data
        source swapped, not their filtering/sorting/rollover logic. `expiry`
        in is Angel One '%d%b%Y' format (matches get_expiry_list's output and
        every existing caller); converted to TrueData's yyyymmdd for the
        request, kept in '%d%b%Y' form on the returned rows.

        VERIFIED against a live TrueData account (2026-08-05): the CSV response
        is headerless positional data, not the named-header format originally
        guessed — see the column comment inline below.

        Cached 15 min: strikes/lot sizes are static intraday, but get_lot_size,
        get_nse_option_strikes, and get_nse_option_quote (called separately per
        CE/PE leg, per retry distance, per rebalance/audit pass) each refetch
        the full chain for the same symbol/expiry — see get_expiry_list's cache
        note for the production impact (circuit-breaker trips scan-wide).
        """
        from django.core.cache import cache as _dj_cache
        cache_key = f"td_option_chain_{symbol}_{expiry}"
        cached = _dj_cache.get(cache_key)
        if cached is not None:
            return cached

        self._ensure_fresh_token()
        try:
            td_expiry = datetime.strptime(expiry, "%d%b%Y").strftime("%Y%m%d")
        except ValueError:
            td_expiry = expiry
        try:
            url = f"{MASTER_BASE}/getoptionchain"
            params = {"user": self.username, "password": self.password, "symbol": symbol, "expiry": td_expiry, "csv": "true"}
            response = self._rest_request("GET", url, params=params, timeout=15)
            if response.status_code != 200:
                return []
            # CONFIRMED (live account, 2026-08-05): getoptionchain returns headerless
            # positional CSV, NOT a DictReader-style header + rows. Columns are:
            # token, tradingsymbol, option_type, "", exchange, lotsize, strike,
            # expiry(DD-MM-YYYY), short_code, tradingsymbol_again. csv.DictReader used
            # to treat the first data row as fabricated header names, so every row.get()
            # lookup silently returned None for every symbol — see get_lot_size's H18
            # note; this was the actual cause, not a broker-connection/entitlement issue.
            index_underlyings = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}
            rows = []
            for cols in csv.reader(io.StringIO(response.text.strip())):
                if len(cols) < 8:
                    continue
                trading_symbol = cols[1].strip()
                option_type = cols[2].strip().upper()
                lotsize = cols[5].strip()
                strike = cols[6].strip()
                if not strike or not trading_symbol:
                    continue
                try:
                    strike_val = float(strike)
                except ValueError:
                    continue
                if option_type not in ("CE", "PE") and trading_symbol[-2:] in ("CE", "PE"):
                    option_type = trading_symbol[-2:]
                rows.append({
                    "token": trading_symbol,  # TrueData is symbol-addressed: the display symbol IS the identifier
                    "symbol": trading_symbol if trading_symbol[-2:] in ("CE", "PE") else f"{trading_symbol}{option_type}",
                    "expiry": expiry,
                    "strike": str(strike_val * 100),  # matches Angel One master's strike*100 convention callers already divide out
                    "lotsize": lotsize or 0,
                    "instrumenttype": "OPTIDX" if symbol.upper() in index_underlyings else "OPTSTK",
                })
            if rows:
                _dj_cache.set(cache_key, rows, timeout=900)
            return rows
        except Exception as e:
            logger.error("[TRUEDATA] get_option_chain failed for %s/%s: %s", symbol, expiry, e)
            return []

    def get_nearest_strike(self, symbol: str, target_price: float) -> float | None:
        """Nearest strike from the live option chain (getoptionchain), using
        the nearest available expiry for `symbol`."""
        expiry = self._nearest_expiry(symbol)
        if not expiry:
            return None
        strikes = self._option_chain_strikes(symbol, expiry)
        if not strikes:
            return None
        return min(strikes, key=lambda x: abs(x - target_price))

    def _nearest_expiry(self, symbol: str) -> str | None:
        self._ensure_fresh_token()
        try:
            url = f"{HISTORY_BASE}/getSymbolExpiryList"
            response = self._rest_request("GET", url, params={"symbol": symbol, "response": "csv"}, timeout=15)
            if response.status_code != 200:
                return None
            lines = [l.strip() for l in response.text.strip().splitlines() if l.strip()]
            return lines[1] if len(lines) > 1 else (lines[0] if lines else None)
        except Exception as e:
            logger.error("[TRUEDATA] _nearest_expiry failed for %s: %s", symbol, e)
            return None

    def _option_chain_strikes(self, symbol: str, expiry: str) -> list[float]:
        self._ensure_fresh_token()
        try:
            url = f"{MASTER_BASE}/getoptionchain"
            params = {"user": self.username, "password": self.password, "symbol": symbol, "expiry": expiry, "csv": "true"}
            response = self._rest_request("GET", url, params=params, timeout=15)
            if response.status_code != 200:
                return []
            # Headerless positional CSV — see get_option_chain's column comment.
            strikes = set()
            for cols in csv.reader(io.StringIO(response.text.strip())):
                if len(cols) < 8:
                    continue
                try:
                    strikes.add(float(cols[6].strip()))
                except ValueError:
                    continue
            return sorted(strikes)
        except Exception as e:
            logger.error("[TRUEDATA] option chain fetch failed for %s/%s: %s", symbol, expiry, e)
            return []

    def get_option_quote(self, symbol: str, target_strike: float, option_type: str = "CE", expiry: str | None = None) -> dict[str, Any] | None:
        """LTP + delta for one option contract via getLTPwithGreeks — a single
        call that gives both, unlike Angel One where strike-resolution and the
        quote were two separate steps. `expiry` here, when given, is expected
        in "DD-MM-YYYY" (TrueData's format for this endpoint) — callers pinning
        an already-open position's exact contract need to pass it in that
        format, not Angel One's "DDMonYYYY".
        """
        if not expiry:
            expiry = self._nearest_expiry(symbol)
            if not expiry:
                return None
            expiry = self._to_ddmmyyyy(expiry)

        self._ensure_fresh_token()
        try:
            url = f"{GREEKS_BASE}/getLTPwithGreeks"
            params = {"symbol": symbol, "expiry": expiry, "strike": target_strike, "series": option_type, "response": "json"}
            response = self._rest_request("GET", url, params=params, timeout=15)
            if response.status_code != 200:
                logger.error("[TRUEDATA] getLTPwithGreeks HTTP %s for %s", response.status_code, symbol)
                return None
            data = response.json()
            row = data.get("Records", data) if isinstance(data, dict) else data
            if isinstance(row, list):
                row = row[0] if row else {}
            if not row:
                return None
            return {
                "ltp": float(row.get("ltp") or row.get("LTP") or 0),
                "delta": float(row.get("delta") or row.get("Delta") or 0),
                "strike": target_strike,
                "type": option_type,
                "expiry": expiry,
                "trading_symbol": f"{symbol}{expiry}{int(target_strike)}{option_type}",
            }
        except Exception as e:
            logger.error("[TRUEDATA] get_option_quote failed for %s: %s", symbol, e)
            return None

    @staticmethod
    def _to_ddmmyyyy(expiry: str) -> str:
        """getSymbolExpiryList returns yyyymmdd; getLTPwithGreeks wants dd-mm-yyyy."""
        try:
            return datetime.strptime(expiry, "%Y%m%d").strftime("%d-%m-%Y")
        except ValueError:
            return expiry

    # ------------------------------------------------------------- lifecycle

    def is_market_open(self) -> bool:
        """CLAUDE.md: NSE API is the sole market-status source, never a
        broker feed. Straight passthrough, unchanged from Angel One."""
        from .signal_utils import is_market_open
        return is_market_open()

    def get_pulse_status(self, symbol_or_token: str | int) -> datetime | None:
        tick = get_stream_price(str(symbol_or_token))
        if tick and tick.get("fetch_time"):
            return datetime.fromtimestamp(tick["fetch_time"], tz=timezone.utc)
        return None

    def disconnect(self) -> None:
        global _service_instance, _IS_AUTHENTICATED
        try:
            if self.streamer:
                self.streamer.stop()
                self.streamer = None
        except Exception as e:
            logger.error("[TRUEDATA] Error stopping streamer during disconnect: %s", e)
        try:
            self.session.close()
        except Exception:
            pass
        _IS_AUTHENTICATED = False
        _service_instance = None
        logger.info("[TRUEDATA] Session disconnected")


# ------------------------------------------------------------------ singleton

_service_instance: TrueDataService | None = None
_IS_AUTHENTICATED = False
_LAST_AUTH_ATTEMPT = 0.0
_LAST_AUTH_FAILURE = 0.0


def get_truedata_instance() -> TrueDataService | None:
    global _service_instance, _IS_AUTHENTICATED
    if _service_instance and _IS_AUTHENTICATED:
        return _service_instance
    if initialize_truedata():
        return _service_instance
    return None


def initialize_truedata(username: str | None = None, password: str | None = None) -> bool:
    with _AUTH_LOCK:
        return _initialize_truedata_locked(username, password)


def _initialize_truedata_locked(username: str | None, password: str | None) -> bool:
    global _service_instance, _IS_AUTHENTICATED, _LAST_AUTH_ATTEMPT, _LAST_AUTH_FAILURE

    try:
        from django.conf import settings

        now = time.time()
        if _service_instance and _IS_AUTHENTICATED:
            # Reuse while the access token itself is still valid, not an
            # arbitrary wall-clock window — TrueData's expiry is authoritative.
            if now < _service_instance.token_expires_at - 300:
                return True
            logger.info("[TRUEDATA] Token nearing expiry. Forcing disconnect and fresh authentication...")
            try:
                _service_instance.disconnect()
            except Exception as de:
                logger.warning("Error disconnecting old session during refresh: %s", de)
            _IS_AUTHENTICATED = False

        if _LAST_AUTH_FAILURE and (now - _LAST_AUTH_FAILURE) < AUTH_RETRY_COOLDOWN:
            logger.warning("[TRUEDATA] Throttling login attempts. Please wait.")
            return False

        if not username:
            config = getattr(settings, "TRUEDATA", {})
            username = config.get("USERNAME")
            password = config.get("PASSWORD")
            ws_port = int(config.get("WS_PORT", 8084))
        else:
            ws_port = int(os.getenv("TRUEDATA_WS_PORT", "8084"))

        if not all([username, password]):
            logger.error("[TRUEDATA] Credentials missing.")
            return False

        _service_instance = TrueDataService(username, password, ws_port)
        _LAST_AUTH_ATTEMPT = now

        if _service_instance.authenticate():
            _IS_AUTHENTICATED = True
            _LAST_AUTH_FAILURE = 0.0
            logger.info("[TRUEDATA] MASTER SESSION started successfully")
            return True

        _LAST_AUTH_FAILURE = time.time()
        logger.error("[TRUEDATA] MASTER SESSION failed to start")
        return False
    except Exception as e:
        _LAST_AUTH_FAILURE = time.time()
        logger.error("[TRUEDATA] Critical error in init: %s", e)
        return False


def is_truedata_ready() -> bool:
    return _service_instance is not None and _IS_AUTHENTICATED


def _demo():
    """No-network self-check for the pure logic (interval mapping, expiry
    format conversion) — the parts most likely to silently drift wrong."""
    assert _INTERVAL_MAP["FIVE_MINUTE"] == "5min"
    assert _INTERVAL_MAP["ONE_DAY"] == "eod"
    assert TrueDataService._to_ddmmyyyy("20230427") == "27-04-2023"
    assert TrueDataService._to_ddmmyyyy("garbage") == "garbage"  # falls back, doesn't crash

    svc = TrueDataService.__new__(TrueDataService)
    assert svc.get_token_map(["RELIANCE", "TCS"]) == {"RELIANCE": "RELIANCE", "TCS": "TCS"}

    print("truedata_service self-check OK")


if __name__ == "__main__":
    _demo()
