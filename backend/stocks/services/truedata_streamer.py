"""
TrueData WebSocket client for real-time market data.

Replaces angel_one_streamer.py's AngelOneStreamer. Same public surface
(start/stop/subscribe, module-level get_stream_price() + _STREAM_CACHE) so
every caller of the old streamer keeps working unchanged — only the import
line at each call site needs to change.

Protocol differences from Angel One that shaped this file (see
TrueDataAPIDocument/TrueData Market Data API Documentation v 2.6.pdf):
  - TrueData speaks plain JSON over the WebSocket (no binary tick format to
    decode), so there's no equivalent of Angel One's _process_tick_dict field
    hunting across SDK versions.
  - Auth is query-string user/password on the WS URL itself, not a
    separate token exchange.
  - Subscriptions are by symbol NAME ("addsymbol": ["RELIANCE", ...]), not by
    a pre-resolved numeric token — matches this migration's design decision
    (see truedata_service.get_token_map) to make "token" == TrueData symbol
    string everywhere in this codebase, so callers pass the same value they
    always did.
  - The wire "trade" message is still keyed by TrueData's own numeric Symbol
    ID (assigned per subscription, not by us), not the symbol name — so this
    class keeps a symbolid -> symbol_name map (populated from the touchline/
    addsymbol response, which returns both) to translate incoming ticks back
    onto the symbol-string keys the rest of the app expects in _STREAM_CACHE.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from threading import Lock
from typing import Any, Dict, List

import websocket

logger = logging.getLogger(__name__)

# Thread-safe cache for ticks, keyed by symbol name (see module docstring).
_STREAM_CACHE: Dict[str, Dict[str, Any]] = {}
_STREAM_LOCK = Lock()

# Thread-safe cache for streamed option Greeks, keyed by symbol name — same
# pattern/lock as _STREAM_CACHE (no separate lock: the two caches are written
# from the same single queue-worker thread, a second lock would just be more
# to deadlock on for no real concurrency benefit).
#
# Populated ONLY if TrueData has enabled the Greeks feed for this account —
# per the Market Data API doc's "Realtime Greeks Response" section: "Greeks
# in websocket needs to be enabled from backend". That's an account-level
# toggle TrueData support sets, not something any WS request message can turn
# on client-side (confirmed: the doc's only WS methods are addsymbol/
# removesymbol/touchline/logout — no "enable greeks" request exists).
#
# VERIFIED LIVE (Trial127 sandbox account, 2026-08-05, market open): streamed
# 40s on 8 actively-traded ATM NIFTY + RELIANCE option contracts (96 trade
# ticks arrived in that window) and got zero "greeks" messages — only
# HeartBeat / "symbols added" / "trade". This account does not have the feed
# enabled. get_stream_greeks() will return None for everyone until TrueData
# turns it on; callers must fall back to local computation, not treat an
# empty result as a bug.
_GREEKS_CACHE: Dict[str, Dict[str, Any]] = {}


class TrueDataStreamer:
    """Manages a persistent WebSocket connection to TrueData with async tick processing."""

    def __init__(self, username: str, password: str, port: int, restart_lock: Lock | None = None):
        self.username = username
        self.password = password
        self.port = port
        self.ws: websocket.WebSocketApp | None = None
        self.is_connected = False
        self._stop_requested = False
        self.thread: threading.Thread | None = None

        # Shared with truedata_service's self-heal path so the two independent
        # reconnect paths can't both call start() at once and orphan a socket —
        # same contract as angel_one_streamer's _restart_lock.
        self._restart_lock = restart_lock if restart_lock is not None else Lock()

        # symbolid (as sent by TrueData) -> symbol name we subscribed with.
        self._id_to_symbol: Dict[str, str] = {}
        self._sub_lock = threading.Lock()
        self._subscribed_symbols: set[str] = set()
        # Symbols requested before the socket was open — flushed in _on_open.
        self._pending_symbols: List[str] = []

        self.tick_queue: queue.Queue = queue.Queue()
        self.queue_worker_thread: threading.Thread | None = None
        self.last_tick_time = 0.0
        self.health_monitor_thread: threading.Thread | None = None

    def start(self):
        if self.is_connected:
            return

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("[TD_STREAMER] Previous connect thread still alive after 5s wait; starting new connection anyway.")

        self._stop_requested = False
        self.last_tick_time = time.time()

        if not self.queue_worker_thread or not self.queue_worker_thread.is_alive():
            self.queue_worker_thread = threading.Thread(target=self._queue_worker, daemon=True)
            self.queue_worker_thread.start()

        if not self.health_monitor_thread or not self.health_monitor_thread.is_alive():
            self.health_monitor_thread = threading.Thread(target=self._health_monitor_worker, daemon=True)
            self.health_monitor_thread.start()

        url = f"wss://push.truedata.in:{self.port}?user={self.username}&password={self.password}"
        self.ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self.thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        self.thread.start()
        logger.info("[TD_STREAMER] WebSocket thread started for %s", self.username)

    def stop(self):
        self._stop_requested = True
        self.is_connected = False
        if self.ws:
            try:
                self.ws.close()
            except Exception as e:
                logger.error("[TD_STREAMER] Error closing WebSocket: %s", e)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)

    def subscribe(self, exchange_type: int, tokens: List[str], mode: int = 2):
        """Subscribe to a list of symbols.

        `exchange_type` and `mode` are accepted-but-ignored — kept so every
        existing Angel One call site (which passes NSE/NFO exchange-type ints
        and a quote mode) keeps working unchanged. TrueData needs neither: the
        symbol string itself already identifies its segment, and TrueData
        streams full touchline fields (OHLC, bid/ask) unconditionally, there's
        no separate "mode" to request.
        """
        symbols = [str(t) for t in tokens]
        with self._sub_lock:
            new_symbols = [s for s in symbols if s not in self._subscribed_symbols]
            if not new_symbols:
                return
            self._subscribed_symbols.update(new_symbols)
            if not self.ws or not self.is_connected:
                self._pending_symbols.extend(new_symbols)
                return

        self._send_addsymbol(new_symbols)

    def _send_addsymbol(self, symbols: List[str]):
        if not symbols or not self.ws:
            return
        try:
            self.ws.send(json.dumps({"method": "addsymbol", "symbols": symbols}))
            logger.info("[TD_STREAMER] Subscribed to %d symbols", len(symbols))
        except Exception as e:
            logger.error("[TD_STREAMER] addsymbol send failed: %s", e)

    def _on_open(self, ws):
        logger.info("[TD_STREAMER] Connection established")
        self.is_connected = True
        self.last_tick_time = time.time()

        with self._sub_lock:
            queued = list(self._pending_symbols)
            self._pending_symbols = []
            all_symbols = list(self._subscribed_symbols)

        if queued:
            self._send_addsymbol(queued)
        # Re-subscribe everything from a previous connection (reconnect case).
        remaining = [s for s in all_symbols if s not in queued]
        if remaining:
            self._send_addsymbol(remaining)

    def _on_message(self, ws, message):
        self.tick_queue.put(message)

    def _queue_worker(self):
        while not self._stop_requested:
            try:
                message = self.tick_queue.get(timeout=1.0)
                self.last_tick_time = time.time()
                self._process_message(message)
                self.tick_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error("[TD_STREAMER] Queue worker tick processing failed: %s", e)

    def _process_message(self, raw: str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return

        if "trade" in data:
            self._process_trade(data["trade"])
        elif "greeks" in data:
            self._process_greeks(data["greeks"])
        elif data.get("message") in ("touchline", "symbols added") and data.get("symbollist"):
            for row in data["symbollist"]:
                self._process_touchline_row(row)
        # heartbeat / marketstatus / bidask: nothing downstream reads these yet.

    def _process_trade(self, fields: List[str]):
        """['trade': [SymbolID, Timestamp, LTP, LTQ, ATP, TTQ, Open, High, Low, ...]]

        Field count distinguishes the with-bid/ask (19 fields) vs without
        (13 fields) format — see Market Data API doc section 4.b.viii. Prev
        Close only exists in the 19-field form; the 13-field form has OI at
        that index instead, so we don't misread it as a price there.
        """
        if len(fields) < 9:
            return
        symbol_id = str(fields[0])
        symbol = self._id_to_symbol.get(symbol_id)
        if not symbol:
            return  # tick for a symbol we haven't mapped an id for yet

        try:
            ltp = float(fields[2])
            volume = float(fields[5])
            open_ = float(fields[6])
            high = float(fields[7])
            low = float(fields[8])
            close = float(fields[9]) if len(fields) >= 19 else 0.0
        except (ValueError, IndexError):
            return

        with _STREAM_LOCK:
            existing = _STREAM_CACHE.get(symbol, {})
            _STREAM_CACHE[symbol] = {
                "ltp": ltp,
                "high": high or existing.get("high", 0.0),
                "low": low or existing.get("low", 0.0),
                "close": close or existing.get("close", 0.0),
                "open": open_ or existing.get("open", 0.0),
                "change": ltp - (close or existing.get("close", 0.0)),
                "change_percent": 0.0 if not (close or existing.get("close")) else
                    round((ltp - (close or existing.get("close"))) / (close or existing.get("close")) * 100, 2),
                "trade_volume": volume,
                "open_interest": existing.get("open_interest", 0.0),
                "fetch_time": time.time(),
                "source": "websocket",
            }

    def _process_greeks(self, fields: List[str]):
        """['greeks': [SymbolID, Timestamp, Delta, Gamma, Theta, Vega, Rho, IV]]

        Field order and the 4-decimal precision are exactly as documented
        (Market Data API doc v2.6, "Realtime Greeks Response") — not guessed.
        Only ever arrives if the account has the feed enabled; see
        _GREEKS_CACHE's comment for the live check confirming this one doesn't.
        """
        if len(fields) < 8:
            return
        symbol_id = str(fields[0])
        symbol = self._id_to_symbol.get(symbol_id)
        if not symbol:
            return  # greeks tick for a symbol we haven't mapped an id for yet

        try:
            delta, gamma, theta, vega, rho, iv = (float(f) for f in fields[2:8])
        except (ValueError, IndexError):
            return

        with _STREAM_LOCK:
            _GREEKS_CACHE[symbol] = {
                "delta": delta,
                "gamma": gamma,
                "theta": theta,
                "vega": vega,
                "rho": rho,
                "iv": iv,
                "fetch_time": time.time(),
                "source": "websocket",
            }

    def _process_touchline_row(self, row: List[str]):
        """[Symbol, SymbolID, LastUpdateTime, LTP, TickVolume, ATP, TotalVolume,
        Open, High, Low, PrevClose, TodayOI, PrevOIClose, Turnover, Bid, BidQty, Ask, AskQty]"""
        if len(row) < 11:
            return
        symbol, symbol_id = str(row[0]), str(row[1])
        self._id_to_symbol[symbol_id] = symbol
        try:
            tick = {
                "ltp": float(row[3]),
                "high": float(row[8]),
                "low": float(row[9]),
                "close": float(row[10]),
                "open": float(row[7]),
                "change": 0.0,
                "change_percent": 0.0,
                "trade_volume": float(row[6]),
                "open_interest": float(row[11]) if len(row) > 11 else 0.0,
                "fetch_time": time.time(),
                "source": "websocket",
            }
        except (ValueError, IndexError):
            return
        with _STREAM_LOCK:
            _STREAM_CACHE[symbol] = tick

    def _on_error(self, ws, error):
        if self._stop_requested:
            logger.debug("[TD_STREAMER] Post-stop WebSocket error suppressed: %s", error)
            return
        logger.error("[TD_STREAMER] WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code=None, close_msg=None):
        self.is_connected = False
        if self._stop_requested:
            logger.debug("[TD_STREAMER] Connection closed (intentional stop).")
            return
        logger.warning("[TD_STREAMER] Connection closed: %s %s", close_status_code, close_msg)

    def _health_monitor_worker(self):
        """Same silence-detection + proactive-restart pattern as
        angel_one_streamer._health_monitor_worker — see that method's docstring
        for why a passive reconnect isn't enough."""
        logger.info("[TD_STREAMER] Health monitor thread started.")
        last_reconnect_attempt = 0.0
        while not self._stop_requested:
            time.sleep(15.0)

            from stocks.services.signal_utils import is_market_open
            if not is_market_open():
                continue

            if self.is_connected:
                age = time.time() - self.last_tick_time
                if age > 45.0:
                    logger.warning("[TD_STREAMER] HEARTBEAT SILENCE DETECTED: no messages in %.1fs. Forcing close...", age)
                    self.is_connected = False
                    if self.ws:
                        try:
                            self.ws.close()
                        except Exception as close_err:
                            logger.error("[TD_STREAMER] Error forcing close on heartbeat failure: %s", close_err)
            elif not self._stop_requested:
                now = time.time()
                if now - last_reconnect_attempt > 30.0:
                    last_reconnect_attempt = now
                    if self._restart_lock.acquire(blocking=False):
                        try:
                            logger.warning("[TD_STREAMER] Disconnected — proactively restarting WebSocket connection...")
                            self.start()
                        except Exception as restart_err:
                            logger.error("[TD_STREAMER] Proactive reconnect failed: %s", restart_err)
                        finally:
                            self._restart_lock.release()
                    else:
                        logger.info("[TD_STREAMER] Restart already in progress via another path — skipping.")


def get_stream_price(token: str | int) -> Dict[str, Any] | None:
    """Read latest price from the stream cache. `token` is a TrueData symbol string."""
    with _STREAM_LOCK:
        return _STREAM_CACHE.get(str(token))


def get_stream_greeks(token: str | int) -> Dict[str, Any] | None:
    """Read latest streamed Greeks from the cache. Stays empty unless TrueData
    has enabled the Greeks WS feed for this account — see _GREEKS_CACHE."""
    with _STREAM_LOCK:
        return _GREEKS_CACHE.get(str(token))


def _demo():
    """No-network self-check for the pure parsing logic — the part that's
    actually easy to get subtly wrong (field offsets, id/symbol translation)."""
    s = TrueDataStreamer("u", "p", 8084)
    s._id_to_symbol["950000114"] = "CRUDEOIL-I"

    # 19-field trade (with bid/ask)
    s._process_trade(["950000114", "2020-05-06T09:06:00", "45691", "45", "45690", "7602",
                       "45680", "45698", "45670", "45650", "0", "0", "0", "", "1", "45690", "10", "45692", "10"])
    tick = get_stream_price("CRUDEOIL-I")
    assert tick["ltp"] == 45691.0 and tick["high"] == 45698.0 and tick["close"] == 45650.0, tick

    # touchline row builds the id map and seeds the cache in one shot
    s._process_touchline_row(["RELIANCE", "100000995", "2020-12-17T09:17:52", "1996",
                               "12625", "1991.2", "448945", "1996", "1996.2", "1985.3", "1984.6"])
    assert s._id_to_symbol["100000995"] == "RELIANCE"
    tick2 = get_stream_price("RELIANCE")
    assert tick2["ltp"] == 1996.0 and tick2["close"] == 1984.6, tick2

    # 13-field trade (no bid/ask) — index 9 is OI here, must NOT be read as close
    s._id_to_symbol["100000995"] = "RELIANCE"
    s._process_trade(["100000995", "2020-12-16T14:02:32", "1472.8", "635", "1475.83",
                       "680949", "1475.05", "1484", "1463", "0", "0", "", "4775"])
    tick3 = get_stream_price("RELIANCE")
    assert tick3["ltp"] == 1472.8 and tick3["close"] == 1984.6, tick3  # close carried over, not misread as 0/OI

    # Greeks — exact sample from the Market Data API doc's "Realtime Greeks
    # Response" section (symbolid, timestamp, delta, gamma, theta, vega, rho,
    # iv), not a guessed shape. Live capture (2026-08-05) showed this trial
    # account never actually emits this message type, so this is the only way
    # to exercise the parser at all — see _GREEKS_CACHE's comment.
    s._id_to_symbol["301680343"] = "NIFTY26081124500CE"
    s._process_greeks(["301680343", "2024-02-14T09:42:02", "0.2015", "0.0331",
                        "-6.0417", "0.0005", "0.8335", "0.0198"])
    greeks = get_stream_greeks("NIFTY26081124500CE")
    assert greeks["delta"] == 0.2015 and greeks["theta"] == -6.0417 and greeks["iv"] == 0.0198, greeks

    print("truedata_streamer self-check OK")


if __name__ == "__main__":
    _demo()
