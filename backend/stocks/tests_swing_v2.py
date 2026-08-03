"""Swing V2 + shared-layer regression suite.

Run:  python -m django test stocks.tests_swing_v2 --settings=config.settings

Style matches selfcheck_intraday_v2/v3 — plain asserts with printed PASS lines, so a failure
names the invariant that broke rather than a line number.

Covers the invariants that would silently corrupt trading behaviour if violated:
profile integrity, sizing arithmetic, cost-model product switching, portfolio caps
(including the promoter-group cap that the live book needed), and the closed-bar
discipline that the whole V2 design rests on.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from django.test import TestCase

from stocks.models import PromoterGroup
from stocks.services.shared import (
    INTRADAY, LONG_TERM, SWING, TOTAL_ACCOUNT_EQUITY, allocated_equity, get_profile,
)
from stocks.services.shared import portfolio_risk, ranking
from stocks.services.shared.regime import RegimeState, _resolve_permissions
from stocks.services.shared.risk_engine import (
    fits_gross_budget, gross_budget, inverse_vol_weights, position_size,
    volatility_scalar,
)
from stocks.services.trading_engine.cost_model import (
    DEFAULT_COST_MODEL, DELIVERY_COST_MODEL, cost_model_for,
)
from stocks.services import swing_signals


def ok(msg, detail=""):
    print(f"PASS  {msg}" + (f"  — {detail}" if detail else ""))


def _synthetic_uptrend(n=300, start=100.0, drift=0.004, vol=0.01, seed=7):
    """Deterministic rising series with a volume profile, long enough for EMA200."""
    rng = np.random.default_rng(seed)
    rets = drift + rng.normal(0, vol, n)
    close = start * np.exp(np.cumsum(rets))
    high = close * (1 + abs(rng.normal(0, 0.004, n)))
    low = close * (1 - abs(rng.normal(0, 0.004, n)))
    idx = pd.date_range("2025-01-01", periods=n, freq="D", tz="Asia/Kolkata")
    return pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close,
         "Volume": rng.integers(500_000, 1_500_000, n)},
        index=idx,
    )


class _Regime:
    directional_bias = "BULLISH"
    allow_momentum = True
    allow_mean_reversion = True
    size_multiplier = 1.0
    trend_state = "UP"
    vol_state = "NORMAL"


class SwingV2Tests(TestCase):

    # ── 1. Profiles ─────────────────────────────────────────────────────────────
    def test_profiles(self):
        print("\n" + "=" * 72 + "\n1. ENGINE PROFILES\n" + "=" * 72)

        shares = sum(p.equity_share_pct for p in (INTRADAY, SWING, LONG_TERM))
        assert shares <= 100.0 + 1e-9, f"equity shares sum to {shares}%"
        ok("engine equity shares do not exceed 100%", f"{shares}%")

        total = sum(allocated_equity(p) for p in (INTRADAY, SWING, LONG_TERM))
        assert total <= TOTAL_ACCOUNT_EQUITY + 1e-6
        ok("allocated equity never exceeds account equity", f"Rs.{total:,.0f}")

        for p in (INTRADAY, SWING, LONG_TERM):
            w = sum(p.factor_weights.values())
            assert abs(w - 100.0) < 1e-6, f"{p.name} weights sum to {w}"
        ok("factor weights sum to 100 for all profiles")

        assert get_profile("swing") is SWING
        try:
            get_profile("nonsense")
            raise AssertionError("unknown profile should raise")
        except ValueError:
            pass
        ok("unknown profile raises rather than defaulting silently")

        # A typo must never hand an engine intraday's 3x gross limit.
        assert SWING.max_gross_pct < INTRADAY.max_gross_pct
        ok("swing gross limit is tighter than intraday", f"{SWING.max_gross_pct}% vs {INTRADAY.max_gross_pct}%")

        assert LONG_TERM.sizing_mode == "inverse_vol"
        assert SWING.sizing_mode == "risk_parity"
        ok("sizing modes correct per horizon")

    # ── 2. Cost model ───────────────────────────────────────────────────────────
    def test_cost_model(self):
        print("\n" + "=" * 72 + "\n2. COST MODEL (§0.1)\n" + "=" * 72)

        intra = DEFAULT_COST_MODEL.round_trip_pct(price=1000, qty=100)
        deliv = DELIVERY_COST_MODEL.round_trip_pct(price=1000, qty=100)
        assert deliv > intra * 2, f"delivery {deliv} should far exceed intraday {intra}"
        ok("delivery friction far exceeds intraday", f"{deliv:.4f}% vs {intra:.4f}%")

        # STT is the dominant delivery term: 0.1% on both legs = 0.2% alone.
        assert deliv > 0.20, f"delivery {deliv} below two-sided STT floor"
        ok("delivery cost exceeds the two-sided STT floor", f"{deliv:.4f}%")

        small = DELIVERY_COST_MODEL.round_trip_pct(price=1000, qty=20)
        assert small > deliv, "flat fees must bite harder on small positions"
        ok("flat fees scale inversely with position size", f"Rs.20k {small:.4f}% > Rs.1L {deliv:.4f}%")

        assert cost_model_for(INTRADAY).product == "intraday"
        assert cost_model_for(SWING).product == "delivery"
        assert cost_model_for(LONG_TERM).product == "delivery"
        ok("cost_model_for routes each profile to the right product")

        # A multi-day hold priced at intraday STT understates friction ~3x.
        buy = DELIVERY_COST_MODEL.side_cost(symbol="X", price=1000, qty=100, is_buy=True)
        sell = DELIVERY_COST_MODEL.side_cost(symbol="X", price=1000, qty=100, is_buy=False)
        assert buy.stt > 0 and sell.stt > 0, "delivery STT applies to BOTH legs"
        assert buy.dp_charge == 0 and sell.dp_charge > 0, "DP charge is sell-side only"
        ok("delivery STT on both legs, DP charge on sell only")

        i_buy = DEFAULT_COST_MODEL.side_cost(symbol="X", price=1000, qty=100, is_buy=True)
        assert i_buy.stt == 0, "intraday STT must be sell-side only"
        ok("intraday STT remains sell-side only")

    # ── 3. Sizing ───────────────────────────────────────────────────────────────
    def test_sizing(self):
        print("\n" + "=" * 72 + "\n3. RISK ENGINE\n" + "=" * 72)

        # NOTE on fixture choice: SWING.max_position_pct is 15% of allocated equity
        # (Rs.30,000 notional at Rs.2,00,000 allocated). A stop as tight as 980 on a
        # Rs.1000 entry (2% risk) wants 75 shares to spend the full risk budget, which
        # is Rs.75,000 notional — 2.5x the cap. That is the cap correctly binding, not
        # a bug (it is exercised deliberately below). The "spends the risk budget"
        # check below therefore uses a wider stop where the cap has headroom, so it
        # tests risk-parity arithmetic in isolation from the notional cap.
        qty, risk = position_size(1000.0, 900.0, SWING)      # 10% stop, well under cap
        expected = allocated_equity(SWING) * (SWING.risk_per_trade_pct / 100.0)
        assert abs(risk - expected) <= 20.0, f"risk {risk} vs budget {expected}"
        ok("risk-parity sizing spends the risk budget", f"{qty} qty, Rs.{risk:.0f}")

        # Halving the stop distance must roughly double the size — this is the whole
        # point of risk parity versus equal-notional. Both ends chosen to stay at or
        # under the notional cap so the comparison isolates risk-parity, not the cap.
        q_wide, _ = position_size(1000.0, 900.0, SWING)   # risk/share=100 -> 15 shares
        q_tight, _ = position_size(1000.0, 950.0, SWING)  # risk/share=50  -> 30 shares
        assert q_tight > q_wide * 1.8, f"{q_tight} vs {q_wide}"
        ok("tighter stop yields proportionally larger size", f"{q_wide} -> {q_tight}")

        assert position_size(1000.0, 1000.0, SWING) == (0, 0.0)
        assert position_size(0.0, 10.0, SWING) == (0, 0.0)
        ok("degenerate inputs size to zero, not to a crash")

        # Regime scalar must scale the book down in poor conditions. Uses the same
        # uncapped stop as above so the multiplier's effect isn't masked by the cap.
        q_full, _ = position_size(1000.0, 900.0, SWING, size_multiplier=1.0)
        q_half, _ = position_size(1000.0, 900.0, SWING, size_multiplier=0.5)
        assert abs(q_half * 2 - q_full) <= 2
        ok("regime size multiplier scales position linearly", f"{q_full} -> {q_half}")

        # Notional cap must bind before an absurd size is produced. This is the ONE
        # case deliberately chosen to hit the cap — a near-zero stop distance would
        # otherwise imply an enormous share count.
        q_cap, _ = position_size(1000.0, 999.9, SWING)
        assert q_cap * 1000.0 <= allocated_equity(SWING) * (SWING.max_position_pct / 100.0) + 1000
        ok("per-name notional cap binds on very tight stops", f"{q_cap} qty")

    def test_inverse_vol(self):
        print("\n" + "=" * 72 + "\n4. INVERSE-VOL WEIGHTS (long-term)\n" + "=" * 72)

        w = inverse_vol_weights({"A": 1.0, "B": 2.0, "C": 4.0}, LONG_TERM)
        assert abs(sum(w.values()) - 1.0) < 1e-6, f"weights sum to {sum(w.values())}"
        ok("weights normalise to 1.0")

        assert w["A"] > w["B"] > w["C"], "lower vol must receive more weight"
        ok("lower volatility receives larger weight", str(w))

        cap = LONG_TERM.max_position_pct / 100.0
        skewed = inverse_vol_weights(
            {"A": 0.1, "B": 5.0, "C": 5.0, "D": 5.0, "E": 5.0,
             "F": 5.0, "G": 5.0, "H": 5.0, "I": 5.0, "J": 5.0,
             "K": 5.0, "L": 5.0, "M": 5.0, "N": 5.0, "O": 5.0}, LONG_TERM)
        assert max(skewed.values()) <= cap + 1e-6, f"cap breached: {max(skewed.values())}"
        ok("per-position cap holds after renormalisation", f"max {max(skewed.values()):.4f} <= {cap}")

        assert inverse_vol_weights({}, LONG_TERM) == {}
        assert inverse_vol_weights({"A": 0.0}, LONG_TERM) == {}
        ok("empty / zero-vol input returns empty rather than dividing by zero")

        assert volatility_scalar(2.0, 1.0) == 0.5
        assert volatility_scalar(0.01, 1.0) <= 2.0, "scalar must be clamped"
        ok("volatility targeting is clamped", f"quiet regime -> {volatility_scalar(0.01, 1.0)}")

    # ── 5. Gross exposure ───────────────────────────────────────────────────────
    def test_gross_exposure(self):
        print("\n" + "=" * 72 + "\n5. GROSS EXPOSURE\n" + "=" * 72)
        budget = gross_budget(SWING)
        assert fits_gross_budget(0.0, budget, SWING)
        assert not fits_gross_budget(budget, 1.0, SWING)
        ok("gross budget is a hard ceiling", f"Rs.{budget:,.0f}")

        # Five separately-reasonable positions can still exceed the book limit.
        each = budget / 4
        used = 0.0
        admitted = 0
        for _ in range(6):
            if fits_gross_budget(used, each, SWING):
                used += each
                admitted += 1
        assert admitted == 4, f"admitted {admitted}"
        ok("per-name reasonableness does not imply portfolio reasonableness", f"{admitted} admitted of 6")

    # ── 6. Ranking ──────────────────────────────────────────────────────────────
    def test_ranking(self):
        print("\n" + "=" * 72 + "\n6. CROSS-SECTIONAL RANKING (D3)\n" + "=" * 72)

        cands = [
            {"symbol": "A", "signal": "BUY", "relative_strength": 5.0, "vol_ratio": 2.0,
             "target_pct": 6.0, "cost_pct": 0.4, "trend_quality": 0.9,
             "momentum_persistence": 30.0, "breakout_quality": 0.9, "sector_strength": 0.9},
            {"symbol": "B", "signal": "BUY", "relative_strength": -3.0, "vol_ratio": 0.8,
             "target_pct": 2.5, "cost_pct": 0.4, "trend_quality": 0.2,
             "momentum_persistence": -5.0, "breakout_quality": 0.1, "sector_strength": 0.1},
        ]
        scored = ranking.score_candidates(list(cands), _Regime(), profile=SWING)
        assert scored[0]["symbol"] == "A", "stronger candidate must rank first"
        assert scored[0]["rank_score"] > scored[1]["rank_score"]
        ok("stronger candidate ranks first", f"A={scored[0]['rank_score']} B={scored[1]['rank_score']}")

        assert 0.0 <= scored[0]["rank_score"] <= 100.0
        ok("scores stay within 0-100 after renormalisation")

        # Only factors named in the profile may contribute.
        assert set(scored[0]["rank_factors"]) <= set(SWING.factor_weights)
        ok("only profile-declared factors contribute", str(sorted(scored[0]["rank_factors"])))

        # Ranking must not depend on input ordering.
        rev = ranking.score_candidates(list(reversed(cands)), _Regime(), profile=SWING)
        assert rev[0]["symbol"] == "A"
        ok("ranking is invariant to candidate ordering")

        # The threshold is ALLOWED to reject everything — this is the replacement for
        # the relaxed fallback, which lowered standards when conditions were worst.
        assert ranking.apply_threshold(scored, threshold=99.0) == []
        ok("threshold may legitimately return zero candidates (D2)")

        assert ranking.score_candidates([], _Regime(), profile=SWING) == []
        ok("empty candidate list is handled")

    # ── 7. Portfolio constraints ────────────────────────────────────────────────
    def test_portfolio_constraints(self):
        print("\n" + "=" * 72 + "\n7. PORTFOLIO CAPS (§9.1)\n" + "=" * 72)

        PromoterGroup.objects.bulk_create([
            PromoterGroup(symbol="ADANIENT", group_name="Adani"),
            PromoterGroup(symbol="ADANIPORTS", group_name="Adani"),
            PromoterGroup(symbol="ADANIPOWER", group_name="Adani"),
        ])

        cands = [{"symbol": s} for s in ("ADANIENT", "ADANIPORTS", "ADANIPOWER")]
        # Different sectors on purpose — this is exactly why a sector cap is not enough.
        sectors = {"ADANIENT": "Diversified", "ADANIPORTS": "Infrastructure",
                   "ADANIPOWER": "Power"}

        accepted, rejected = portfolio_risk.apply_portfolio_constraints(
            cands, [], sectors, {}, max_positions=10, profile=LONG_TERM,
        )
        group_cap = max(1, int(LONG_TERM.max_per_promoter_group_pct / 100.0 * 10))
        assert len(accepted) == group_cap, f"accepted {len(accepted)}, cap {group_cap}"
        assert any("PROMOTER_GROUP_CAP" in r["reject_reason"] for r in rejected)
        ok("promoter-group cap binds across DIFFERENT sectors",
           f"{len(accepted)} accepted, {len(rejected)} rejected")

        # Sector cap alone would have admitted all three — the live failure mode.
        acc_no_group, _ = portfolio_risk.apply_portfolio_constraints(
            cands, [], sectors, {}, max_positions=10, profile=LONG_TERM, groups={},
        )
        assert len(acc_no_group) == 3
        ok("without the group map, sector caps admit all three (the original bug)")

        # Sector cap still works on its own terms.
        same_sector = [{"symbol": f"S{i}"} for i in range(5)]
        secs = {f"S{i}": "Financial Services" for i in range(5)}
        acc, rej = portfolio_risk.apply_portfolio_constraints(
            same_sector, [], secs, {}, max_positions=10, profile=SWING, groups={},
        )
        assert len(acc) == SWING.max_per_sector
        ok("sector cap binds", f"{len(acc)} of 5 admitted (cap {SWING.max_per_sector})")

        # Correlation cluster cap.
        clustered = [{"symbol": f"C{i}"} for i in range(5)]
        clusters = {f"C{i}": 0 for i in range(5)}
        acc2, _ = portfolio_risk.apply_portfolio_constraints(
            clustered, [], {}, clusters, max_positions=10, profile=SWING, groups={},
        )
        assert len(acc2) == SWING.max_per_cluster
        ok("correlation-cluster cap binds", f"{len(acc2)} of 5 (cap {SWING.max_per_cluster})")

        # Open positions must count toward the caps, not just new candidates.
        # SWING.max_per_sector is 3, so 3 pre-existing open positions in the sector
        # already sit AT the cap — a 4th (new candidate) must be blocked. Two existing
        # positions would only bring the total to 3 (AT, not over, the cap) and would
        # be wrongly accepted, which is exactly the off-by-one this fixture avoids.
        open_in_sector = [{"symbol": f"S{i}"} for i in range(SWING.max_per_sector)]
        acc3, rej3 = portfolio_risk.apply_portfolio_constraints(
            [{"symbol": "S9"}], open_in_sector,
            {**secs, "S9": "Financial Services"}, {}, max_positions=10,
            profile=SWING, groups={},
        )
        assert len(acc3) == 0, "existing sector exposure at the cap must block a new one"
        ok("open positions count toward sector exposure")

    def test_effective_bets(self):
        print("\n" + "=" * 72 + "\n8. EFFECTIVE BETS\n" + "=" * 72)
        assert abs(portfolio_risk.effective_bets(5, 0.0) - 5.0) < 1e-9
        ok("uncorrelated book: N_eff == n")
        assert abs(portfolio_risk.effective_bets(5, 1.0) - 1.0) < 1e-9
        ok("perfectly correlated book: N_eff == 1")
        n_eff = portfolio_risk.effective_bets(5, 0.6)
        assert 1.4 < n_eff < 1.6
        ok("5 positions at rho=0.6 is ~1.5 real bets", f"N_eff={n_eff:.2f}")

    # ── 9. Closed-bar discipline ────────────────────────────────────────────────
    def test_closed_bar_discipline(self):
        print("\n" + "=" * 72 + "\n9. CLOSED BARS (D1)\n" + "=" * 72)
        df = _synthetic_uptrend(50)
        assert len(swing_signals.drop_forming_bar(df, session_complete=False)) == 49
        assert len(swing_signals.drop_forming_bar(df, session_complete=True)) == 50
        ok("forming bar dropped mid-session, retained after close")

        last_complete = swing_signals.drop_forming_bar(df, False).index[-1]
        assert last_complete == df.index[-2]
        ok("the retained final bar is the previous session's")

        # This is the measured failure: a partial bar collapses the volume RATIO because
        # vol_5d is dragged down ~5x harder than vol_20d.
        v = df["Volume"].astype(float)
        full = v.iloc[-5:].mean() / v.iloc[-20:].mean()
        vp = v.copy()
        vp.iloc[-1] = vp.iloc[-1] * 0.20
        partial = vp.iloc[-5:].mean() / vp.iloc[-20:].mean()
        assert partial < full, f"partial {partial} should be below full {full}"
        ok("in-progress bar depresses the volume ratio", f"{full:.2f} -> {partial:.2f}")

    # ── 10. Setup detection ─────────────────────────────────────────────────────
    def test_detect_setup(self):
        print("\n" + "=" * 72 + "\n10. SETUP DETECTION\n" + "=" * 72)

        df = _synthetic_uptrend(300)
        cand = swing_signals.detect_setup("TEST", df, _Regime(), SWING,
                                          benchmark_ret_pct=0.0, sector="IT")
        assert cand is not None, "a clean uptrend should produce a setup"
        ok("uptrend produces a candidate", f"{cand['setup_family']}: {cand['setup']}")

        # Level arithmetic.
        assert cand["stop_loss"] < cand["entry"] < cand["target"]
        assert cand["target"] < cand["target2"] < cand["target3"]
        ok("levels are correctly ordered")

        r = cand["entry"] - cand["stop_loss"]
        assert abs((cand["target"] - cand["entry"]) / r - 2.0) < 0.05
        assert abs((cand["target3"] - cand["entry"]) / r - 4.0) < 0.05
        ok("targets sit at 2R / 3R / 4R", f"RR={cand['rr']}")

        # Stop floor: never worse than -10%.
        assert cand["stop_loss"] >= cand["entry"] * 0.895
        ok("stop never exceeds the 10% floor",
           f"{(1 - cand['stop_loss'] / cand['entry']) * 100:.2f}%")

        assert cand["qty"] > 0 and cand["rupee_risk"] > 0
        ok("sizing attached to the candidate", f"{cand['qty']} qty, Rs.{cand['rupee_risk']:.0f}")

        for k in ("relative_strength", "momentum_persistence", "breakout_quality",
                  "trend_quality", "vol_ratio"):
            assert k in cand, f"missing ranker input {k}"
        ok("all ranker inputs present on the candidate")

        # Downtrend must be rejected by the structural gate.
        down = _synthetic_uptrend(300, drift=-0.004, seed=11)
        assert swing_signals.detect_setup("DOWN", down, _Regime(), SWING) is None
        ok("downtrend rejected by the trend-structure gate")

        # Too little history must be rejected, not extrapolated.
        assert swing_signals.detect_setup("SHORT", _synthetic_uptrend(100), _Regime(), SWING) is None
        ok("insufficient history rejected", "needs >= 220 closed bars")

    def test_long_only_momentum_direction(self):
        print("\n" + "=" * 72 + "\n12. LONG-ONLY MOMENTUM DIRECTION (live bug, 2026-07-26)\n" + "=" * 72)

        # The exact live case: DOWN/BEARISH regime still emitted 6 long momentum
        # signals for swing, because "momentum allowed" was defined as merely
        # TRENDING (true for both UP and DOWN) rather than specifically UP.
        down = RegimeState(trend_state="DOWN", vol_state="NORMAL")

        intraday_down = _resolve_permissions(RegimeState(**down.as_dict()), long_only=False)
        assert intraday_down.allow_momentum is True
        ok("intraday keeps momentum allowed in a DOWN trend (it can short)")

        swing_down = _resolve_permissions(RegimeState(**down.as_dict()), long_only=True)
        assert swing_down.allow_momentum is False
        ok("long-only momentum is now BLOCKED in a DOWN trend", "the fix")

        up = RegimeState(trend_state="UP", vol_state="NORMAL")
        swing_up = _resolve_permissions(RegimeState(**up.as_dict()), long_only=True)
        assert swing_up.allow_momentum is True
        ok("long-only momentum remains allowed in an UP trend")

        # Profiles must declare this correctly, or the wiring is inert.
        assert INTRADAY.long_only is False
        assert SWING.long_only is True
        assert LONG_TERM.long_only is True
        ok("long_only correctly declared per profile")

    def test_regime_gating(self):
        print("\n" + "=" * 72 + "\n11. REGIME GATING\n" + "=" * 72)

        class NoMomentum(_Regime):
            allow_momentum = False

        class NoMeanRev(_Regime):
            allow_mean_reversion = False

        assert not swing_signals.strategy_allowed(swing_signals.MOMENTUM_FAMILY, NoMomentum())
        assert swing_signals.strategy_allowed(swing_signals.PULLBACK_FAMILY, NoMomentum())
        ok("momentum blocked, pullback allowed when momentum is disallowed")

        assert swing_signals.strategy_allowed(swing_signals.MOMENTUM_FAMILY, NoMeanRev())
        assert not swing_signals.strategy_allowed(swing_signals.PULLBACK_FAMILY, NoMeanRev())
        ok("pullback blocked when mean reversion is disallowed")

        df = _synthetic_uptrend(300)
        c = swing_signals.detect_setup("T", df, NoMomentum(), SWING)
        assert c is None or c["setup_family"] != swing_signals.MOMENTUM_FAMILY
        ok("detection respects regime permission at source")

        print("\n" + "=" * 72 + "\nRESULT: ALL PASSED\n" + "=" * 72)
