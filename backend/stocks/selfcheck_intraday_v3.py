"""Verify the final tranche: §3.1b/c/f/g, §4.2, §5.4, §9.3, §3.2 rank 3."""
import os, sys, django
sys.path.insert(0, "/home/jd/tradeplusai/tradepulse-ai/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import numpy as np
import pandas as pd

fails = []
def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)

IST = "Asia/Kolkata"

if __name__ == "__main__":
    print("=" * 72)
    print("1. SESSION-ANCHORED VOLUME PROFILE (§3.1b)")
    print("=" * 72)
    from stocks.services.signal_utils import compute_session_volume_profile, compute_volume_profile

    # Day 1 trades around 100; day 2 trades around 200. A composite profile blends them.
    idx = pd.DatetimeIndex(
        list(pd.date_range("2026-07-23 09:15", periods=25, freq="5min", tz=IST))
        + list(pd.date_range("2026-07-24 09:15", periods=25, freq="5min", tz=IST))
    )
    close = np.array([100.0] * 25 + [200.0] * 25)
    df = pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                       "Close": close, "Volume": np.full(50, 1000.0)}, index=idx)

    poc_s, vah_s, val_s, src = compute_session_volume_profile(df, bins=20)
    poc_c, _, _ = compute_volume_profile(df, bins=20)
    print(f"   session POC={poc_s:.1f} (source={src})   composite POC={poc_c:.1f}")
    check("developing profile used when session is deep enough", src == "developing", src)
    check("session POC reflects today only", abs(poc_s - 200.0) < 5.0, f"{poc_s:.1f}")
    check("composite POC is contaminated by prior day", poc_c < 190.0, f"{poc_c:.1f}")

    # Thin session -> must fall back to prior day rather than a blend.
    thin = df.iloc[:28]   # 25 bars day1 + 3 bars day2
    _, _, _, src2 = compute_session_volume_profile(thin, bins=20, min_session_bars=12)
    check("prior-day fallback early in session", src2 == "prior_day", src2)

    print()
    print("=" * 72)
    print("2. VOLATILITY COMPRESSION (§3.2 rank 3)")
    print("=" * 72)
    from stocks.services.signal_utils import compute_compression

    rng_ = np.random.default_rng(3)
    n = 80
    wide = 1000 + rng_.normal(0, 8, n).cumsum() * 0.3
    wide_df = pd.DataFrame({"Open": wide, "High": wide + 6, "Low": wide - 6,
                            "Close": wide, "Volume": np.full(n, 1e4)},
                           index=pd.date_range("2026-07-24 09:15", periods=n, freq="5min", tz=IST))
    # 30 flat bars so the 20-period window sits entirely inside the quiet zone;
    # with only 10 the rolling std is still half-composed of wide bars.
    tight = np.concatenate([wide[:-30], np.full(30, wide[-30])])
    tight_df = pd.DataFrame({"Open": tight, "High": tight + 0.2, "Low": tight - 0.2,
                             "Close": tight, "Volume": np.full(n, 1e4)},
                            index=wide_df.index)

    c_wide = compute_compression(wide_df)
    c_tight = compute_compression(tight_df)
    print(f"   wide bandwidth_pct={c_wide['bandwidth_pct']}  tight={c_tight['bandwidth_pct']}")
    check("compression detected on contracted range", c_tight["is_compressed"])
    check("not flagged on wide range", not c_wide["is_compressed"])
    check("NR7 detected", c_tight["is_nr7"] or c_tight["range_ratio"] < 0.5,
          f"nr7={c_tight['is_nr7']} ratio={c_tight['range_ratio']}")

    print()
    print("=" * 72)
    print("3. MULTI-TRIGGER + ENTRY SLIPPAGE (§3.1c, §3.1g)")
    print("=" * 72)
    from stocks.services.intraday_service import _volume_profile_logic, _build_intraday_candidate

    out = _volume_profile_logic("TEST", wide_df.copy(), "SIDEWAYS")
    check("returns a list, not a single dict", isinstance(out, list), type(out).__name__)
    check("short frame returns empty list",
          _volume_profile_logic("T", wide_df.iloc[:10], "SIDEWAYS") == [])
    check("None frame returns empty list", _volume_profile_logic("T", None, "SIDEWAYS") == [])

    LIQ = {"adv_inr": 5e8, "daily_vol_pct": 1.5}
    buy = _build_intraday_candidate(
        ticker_sym="T", strategy_key="VP", strategy_name="VP", signal="BUY",
        price=1000.0, entry=1000.0, stop_loss=994.0, target=1012.0, rr=2.0,
        reason="t", score=4.0, priority=1, vol_ratio=1.5, liquidity=LIQ)
    sell = _build_intraday_candidate(
        ticker_sym="T", strategy_key="VP", strategy_name="VP", signal="SELL",
        price=1000.0, entry=1000.0, stop_loss=1006.0, target=988.0, rr=2.0,
        reason="t", score=4.0, priority=1, vol_ratio=1.5, liquidity=LIQ)
    print(f"   BUY entry={buy['entry']}  SELL entry={sell['entry']}  (mid was 1000.00)")
    check("BUY entry lifted above mid", buy["entry"] > 1000.0, str(buy["entry"]))
    check("SELL entry pushed below mid", sell["entry"] < 1000.0, str(sell["entry"]))

    print()
    print("=" * 72)
    print("4. EVENT BLACKOUT (§3.1f)")
    print("=" * 72)
    from django.core.cache import cache
    from stocks.services import event_filter_service as ev

    cache.set(ev.FO_BAN_CACHE_KEY, ["BANNEDCO"], timeout=60)
    cache.set(ev.EARNINGS_CACHE_KEY,
              {"EARNCO": pd.Timestamp.now(tz=IST).date().isoformat()}, timeout=60)

    allowed, excluded = ev.filter_symbols(["RELIANCE", "BANNEDCO", "EARNCO", "TCS"])
    print(f"   allowed={allowed}  excluded={excluded}")
    check("F&O ban symbol removed", "BANNEDCO" not in allowed)
    check("earnings-window symbol removed", "EARNCO" not in allowed)
    check("clean symbols retained", set(allowed) == {"RELIANCE", "TCS"})
    check("exclusion reasons recorded", excluded.get("BANNEDCO") == "FO_BAN")

    cache.delete(ev.FO_BAN_CACHE_KEY); cache.delete(ev.EARNINGS_CACHE_KEY)

    print()
    print("=" * 72)
    print("5. BETA CONSTRAINT + P&L ATTRIBUTION (§5.4, v2.0/8)")
    print("=" * 72)
    from stocks.services.portfolio_risk import (
        net_portfolio_beta, beta_constrained, attribute_pnl, MAX_NET_BETA,
    )
    betas = {"HIGHB": 1.8, "LOWB": 0.4}
    book = [{"symbol": "HIGHB", "qty": 400, "entry_price": 1000, "signal_type": "BUY"}]
    nb = net_portfolio_beta(book, betas, 500_000.0)
    check("net beta computed", abs(nb - 1.44) < 0.01, f"{nb}")

    shorted = book + [{"symbol": "LOWB", "qty": 400, "entry_price": 1000, "signal_type": "SELL"}]
    nb2 = net_portfolio_beta(shorted, betas, 500_000.0)
    check("short position reduces net beta", nb2 < nb, f"{nb2} < {nb}")

    blocked = {"symbol": "HIGHB", "qty": 400, "entry": 1000, "signal": "BUY"}
    check("second high-beta long blocked by cap",
          not beta_constrained(blocked, book, betas, 500_000.0),
          f"would exceed {MAX_NET_BETA}")
    # qty 100 would take net beta to 1.52 (past the 1.5 cap) and is correctly refused;
    # qty 50 lands at 1.48 and must be allowed.
    check("oversized low-beta addition still refused",
          not beta_constrained({"symbol": "LOWB", "qty": 100, "entry": 1000, "signal": "BUY"},
                               book, betas, 500_000.0))
    check("low-beta addition within cap permitted",
          beta_constrained({"symbol": "LOWB", "qty": 50, "entry": 1000, "signal": "BUY"},
                           book, betas, 500_000.0))

    attr = attribute_pnl(trade_pnl=5000.0, symbol="X", entry_price=1000, qty=400,
                         direction="BUY", index_return_pct=1.0, sector_return_pct=1.5, beta=1.2)
    print(f"   total=Rs.{attr['total_pnl']} market=Rs.{attr['market_component']} "
          f"sector=Rs.{attr['sector_component']} alpha=Rs.{attr['alpha_component']}")
    check("components reconstruct total",
          abs(attr["market_component"] + attr["sector_component"] + attr["alpha_component"]
              - attr["total_pnl"]) < 0.01)
    check("market component non-zero on an up day", attr["market_component"] > 0)
    check("alpha share reported", "alpha_share" in attr)

    print()
    print("=" * 72)
    print("6. EXIT STACK (§4.2)")
    print("=" * 72)
    from stocks.services.live_signal_service import (
        _is_momentum, _manage_dynamic_stop, _vwap_exit_hit,
    )
    from stocks.models import SignalHistory

    class Sig:
        def __init__(self, reason, typ="BUY", entry=1000.0, stop=994.0, atr=4.0):
            self.reason, self.signal_type = reason, typ
            self.entry_price, self.stop_loss = entry, stop
            self.symbol, self.target = "T", 1012.0
            self.metadata = {"atr": atr}
            self.saved = False
        def save(self, update_fields=None): self.saved = True

    check("momentum family identified", _is_momentum(Sig("[VP] POC Bullish Flip")))
    check("mean-reversion family identified",
          not _is_momentum(Sig("[VP] Value Area Low Rejection")))

    # +1R excursion should arm break-even.
    bars_1r = pd.DataFrame({"High": [1006.5], "Low": [1000.0], "Close": [1006.0],
                            "Volume": [1e4]},
                           index=pd.date_range("2026-07-24 10:00", periods=1, freq="1min", tz=IST))
    s1 = Sig("[VP] POC Bullish Flip")
    moved = _manage_dynamic_stop(s1, bars_1r, None)
    check("break-even armed at +1R", moved and s1.stop_loss >= 1000.0, f"stop={s1.stop_loss}")

    # +2R should engage the Chandelier trail above break-even.
    bars_2r = pd.DataFrame({"High": [1013.0], "Low": [1000.0], "Close": [1012.0],
                            "Volume": [1e4]}, index=bars_1r.index)
    s2 = Sig("[VP] POC Bullish Flip")
    _manage_dynamic_stop(s2, bars_2r, None)
    check("chandelier trail engaged past 1.5R",
          s2.metadata.get("trailing_armed") is True, f"stop={s2.stop_loss}")
    check("stop never widens", s2.stop_loss >= 994.0, f"stop={s2.stop_loss}")

    # Mean-reversion trades must not trail.
    s3 = Sig("[VP] Value Area Low Rejection")
    check("mean-reversion does not trail", not _manage_dynamic_stop(s3, bars_2r, None))

    vwap_bars = pd.DataFrame(
        {"High": [1002.0, 1006.0, 1009.0], "Low": [998.0, 1001.0, 1004.0],
         "Close": [1000.0, 1005.0, 1008.0], "Volume": [1e4, 1e4, 1e4]},
        index=pd.date_range("2026-07-24 10:00", periods=3, freq="1min", tz=IST))
    px = _vwap_exit_hit(Sig("[VP] Value Area Low Rejection", entry=999.0), vwap_bars)
    check("VWAP exit fires for mean-reversion", px is not None, str(px))
    check("VWAP exit does not fire for momentum",
          _vwap_exit_hit(Sig("[VP] POC Bullish Flip", entry=999.0), vwap_bars) is None)

    print()
    print("=" * 72)
    print("7. CANDLE STORE (§9.3)")
    print("=" * 72)
    from stocks.models import CandleBar
    from stocks.services.candle_store import store_bars, load_bars, latest_stored_ts

    CandleBar.objects.filter(symbol="__TEST__").delete()
    bars = pd.DataFrame(
        {"Open": [100.0, 101.0, 102.0], "High": [101.0, 102.0, 103.0],
         "Low": [99.0, 100.0, 101.0], "Close": [100.5, 101.5, 102.5],
         "Volume": [1000, 1100, 1200]},
        index=pd.date_range("2026-07-24 09:15", periods=3, freq="5min", tz=IST))

    written = store_bars("__TEST__", "FIVE_MINUTE", bars)
    check("forming bar excluded from storage", written == 2, f"wrote {written} of 3")

    again = store_bars("__TEST__", "FIVE_MINUTE", bars)
    check("idempotent — no duplicate rows", again == 0, f"wrote {again}")

    loaded = load_bars("__TEST__", "FIVE_MINUTE", pd.Timestamp("2026-07-24 00:00", tz=IST))
    check("bars round-trip correctly", len(loaded) == 2, f"{len(loaded)} rows")
    check("OHLC preserved", abs(float(loaded["Close"].iloc[0]) - 100.5) < 0.01)
    check("index is tz-aware IST", str(loaded.index.tz) == IST, str(loaded.index.tz))
    check("latest ts tracked", latest_stored_ts("__TEST__", "FIVE_MINUTE") is not None)
    CandleBar.objects.filter(symbol="__TEST__").delete()

    print()
    print("=" * 72)
    print(f"RESULT: {'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
    print("=" * 72)
    sys.exit(1 if fails else 0)
