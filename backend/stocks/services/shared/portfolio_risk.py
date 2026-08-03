"""
Portfolio-level risk controls: sector caps, correlation clustering, vol targeting.

See doc/INSTITUTIONAL_AUDIT_INTRADAY.md §5.3. Per-trade risk parity bounds each position
but says nothing about the book. With no correlation constraint and NIFTY100 at ~35%
financials, five simultaneous longs on a trend day are five versions of the same trade:

    N_eff = n / (1 + (n-1)·rho) = 5 / (1 + 4(0.6)) = 1.47
    sigma_p / sigma = sqrt((1 + (n-1)rho)/n) = sqrt(3.4/5) = 0.825

So a nominal 5-position book delivers a 17.5% volatility reduction where 5 uncorrelated
positions would deliver 55% — and its drawdowns arrive simultaneously. Since Sharpe
scales with sqrt(N_eff), decorrelation is a larger lever here than stronger signals.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from django.conf import settings
from django.core.cache import cache

from .. import market_data

# NIFTY 50 index token, used as the benchmark for beta computation. Same token/pseudo
# -symbol as shared/regime.py's NIFTY50_TOKEN — kept as a distinct constant here (rather
# than importing regime.py) to avoid a cross-import between sibling shared/ modules;
# candle_store keys on the symbol string, so using the identical pseudo-symbol
# "NIFTY50_INDEX" means both modules' NIFTY fetches share one cached row per interval.
NIFTY50_INDEX_SYMBOL = "NIFTY50_INDEX"

logger = logging.getLogger(__name__)

MAX_PER_SECTOR = int(getattr(settings, "INTRADAY_MAX_PER_SECTOR", 2))
MAX_PER_CLUSTER = int(getattr(settings, "INTRADAY_MAX_PER_CLUSTER", 2))
CORRELATION_THRESHOLD = float(getattr(settings, "INTRADAY_CORRELATION_THRESHOLD", 0.7))
TARGET_PORTFOLIO_VOL_PCT = float(getattr(settings, "INTRADAY_TARGET_VOL_PCT", 1.0))
# Beta-weighted net exposure band, as a multiple of equity. Keeps the book from becoming
# a leveraged directional bet on the index while nominally holding "stock signals".
MAX_NET_BETA = float(getattr(settings, "INTRADAY_MAX_NET_BETA", 1.5))

CORR_CACHE_KEY = "intraday_correlation_clusters_v1"
CORR_TTL = 60 * 60 * 12


def build_correlation_clusters(svc, symbols: list[str], lookback_days: int = 30,
                               profile=None) -> dict[str, int]:
    """Greedy single-linkage clustering on 20-day daily-return correlation.

    Greedy rather than hierarchical on purpose: the goal is only to stop two near-identical
    names entering the book together, which does not need a precise dendrogram, and the
    correlation estimate itself is noisy enough that extra sophistication would be false
    precision.

    Returns {symbol: cluster_id}.
    """
    # Per-profile lookback and threshold. A 30-day window is right for intraday, far too
    # short for a 1-2 year book where the correlation that matters is structural.
    threshold = CORRELATION_THRESHOLD
    cache_key = CORR_CACHE_KEY
    if profile is not None:
        lookback_days = profile.corr_lookback_days
        threshold = profile.corr_threshold
        cache_key = f"{CORR_CACHE_KEY}_{profile.name}"

    cached = cache.get(cache_key)
    if cached:
        return cached

    if svc is None or not symbols:
        return {}

    token_map = svc.get_token_map(symbols, exchange="NSE") or {}
    series: dict[str, pd.Series] = {}
    for sym, token in token_map.items():
        try:
            # +15 buffer preserved from the direct-call version (weekend/holiday slack).
            df = market_data.request_candles(
                svc, sym, token, "ONE_DAY", lookback_days=lookback_days + 15, exchange="NSE"
            )
            if df is None or df.empty or len(df) < 15:
                continue
            series[sym] = df["Close"].pct_change().dropna().tail(lookback_days)
        except Exception:
            continue

    if len(series) < 2:
        return {}

    returns = pd.DataFrame(series).dropna(how="all", axis=1)
    if returns.shape[1] < 2:
        return {}
    corr = returns.corr()

    clusters: dict[str, int] = {}
    next_id = 0
    for sym in corr.columns:
        if sym in clusters:
            continue
        clusters[sym] = next_id
        peers = corr[sym][corr[sym] >= threshold].index
        for peer in peers:
            clusters.setdefault(peer, next_id)
        next_id += 1

    cache.set(cache_key, clusters, timeout=CORR_TTL)
    logger.info("[PORTFOLIO] %d symbols grouped into %d correlation clusters "
                "(rho>=%.2f, %dd lookback).",
                len(clusters), next_id, threshold, lookback_days)
    return clusters


def effective_bets(n: int, avg_rho: float) -> float:
    """N_eff = n / (1 + (n-1)·rho). Reported so concentration stays visible."""
    if n <= 0:
        return 0.0
    return n / (1.0 + (n - 1) * max(0.0, min(1.0, avg_rho)))


def promoter_group_map(symbols: list[str]) -> dict[str, str]:
    """{symbol: promoter_group}. Unmapped symbols are treated as their own group.

    Sector caps alone cannot prevent single-group concentration: ADANIPORTS is
    Infrastructure and ADANIENT is Diversified, so a 2-per-sector rule permits both —
    which is exactly how the live long-term book became 100% one promoter group with
    two positions. This is the India-specific control that closes that hole.
    """
    from stocks.models import PromoterGroup
    try:
        rows = PromoterGroup.objects.filter(symbol__in=symbols).values("symbol", "group_name")
        return {r["symbol"]: r["group_name"] for r in rows}
    except Exception as exc:
        logger.warning("[PORTFOLIO] Promoter group lookup unavailable (%s) — "
                       "group cap NOT enforced this cycle.", exc)
        return {}


def apply_portfolio_constraints(
    candidates: list[dict],
    open_positions: list[dict],
    sectors: dict[str, str],
    clusters: dict[str, int],
    max_positions: int,
    profile=None,
    groups: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Filter candidates against sector, correlation-cluster and promoter-group caps.

    Returns (accepted, rejected_with_reason). Rejections carry a reason so the funnel
    stays measurable rather than silently discarding candidates.
    """
    if profile is None:
        from .profiles import INTRADAY
        profile = INTRADAY

    max_sector = profile.max_per_sector
    max_cluster = profile.max_per_cluster
    # Group cap is expressed as a % of the book; convert to a position count.
    max_group = max(1, int(profile.max_per_promoter_group_pct / 100.0 * max_positions))

    if groups is None:
        all_syms = [c["symbol"] for c in candidates] + [
            p.get("symbol") for p in open_positions if p.get("symbol")
        ]
        groups = promoter_group_map(all_syms)

    sector_counts: dict[str, int] = {}
    cluster_counts: dict[int, int] = {}
    group_counts: dict[str, int] = {}

    for pos in open_positions:
        sym = pos.get("symbol")
        sec = sectors.get(sym, "UNKNOWN")
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
        cid = clusters.get(sym)
        if cid is not None:
            cluster_counts[cid] = cluster_counts.get(cid, 0) + 1
        grp = groups.get(sym)
        if grp:
            group_counts[grp] = group_counts.get(grp, 0) + 1

    accepted: list[dict] = []
    rejected: list[dict] = []

    for cand in candidates:
        if len(accepted) + len(open_positions) >= max_positions:
            cand["reject_reason"] = "MAX_POSITIONS"
            rejected.append(cand)
            continue

        sym = cand["symbol"]
        sec = sectors.get(sym, "UNKNOWN")
        cid = clusters.get(sym)
        grp = groups.get(sym)

        if sector_counts.get(sec, 0) >= max_sector:
            cand["reject_reason"] = f"SECTOR_CAP:{sec}"
            rejected.append(cand)
            continue

        if cid is not None and cluster_counts.get(cid, 0) >= max_cluster:
            cand["reject_reason"] = f"CORRELATION_CAP:cluster{cid}"
            rejected.append(cand)
            continue

        if grp and group_counts.get(grp, 0) >= max_group:
            cand["reject_reason"] = f"PROMOTER_GROUP_CAP:{grp}"
            rejected.append(cand)
            continue

        accepted.append(cand)
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
        if cid is not None:
            cluster_counts[cid] = cluster_counts.get(cid, 0) + 1
        if grp:
            group_counts[grp] = group_counts.get(grp, 0) + 1

    if rejected:
        logger.info(
            "[PORTFOLIO] %d accepted, %d rejected by concentration caps (%s)",
            len(accepted), len(rejected),
            ", ".join(sorted({r["reject_reason"].split(":")[0] for r in rejected})),
        )
    return accepted, rejected


def record_candidates(engine: str, accepted: list[dict], rejected: list[dict],
                      regime=None, dry_run: bool = False) -> None:
    """Persist every candidate an engine produced this scan — accepted AND rejected —
    to the `SignalCandidate` audit table (Signal Queue, see
    doc/MARKET_DATA_ENGINE_ARCHITECTURE.md §9). Today a candidate rejected by
    `apply_portfolio_constraints` (sector cap / cluster cap / promoter-group cap /
    gross exposure / cost gate) simply never becomes a `SignalHistory` row and the
    reason only ever existed in logs; this makes that reconstructable after the fact.

    Pure observability — no behavior change to what gets persisted. Candidate dicts are
    not identical across engines (e.g. `stop_loss` vs `stop`, swing has no explicit
    `direction` key since it is long-only), so every field is read defensively. Any
    failure here is logged and swallowed, never raised — an audit-trail bug must never
    block or alter signal generation, persistence or notification.

    `dry_run=True` (used by Swing V2's shadow scan) skips the `bulk_create` entirely —
    a shadow evaluation must have zero DB footprint, not just skip `SignalHistory`.
    """
    from stocks.models import SignalCandidate

    try:
        snapshot = regime.as_dict() if hasattr(regime, "as_dict") else {}

        def _row(cand: dict, status: str) -> SignalCandidate:
            return SignalCandidate(
                engine=engine,
                symbol=cand.get("symbol", ""),
                direction=cand.get("direction") or cand.get("signal") or "BUY",
                entry=cand.get("entry") or 0,
                stop=cand.get("stop_loss", cand.get("stop", 0)) or 0,
                target=cand.get("target") or 0,
                rank_score=cand.get("rank_score"),
                rank_factors=cand.get("rank_factors") or {},
                regime_snapshot=snapshot,
                status=status,
                reject_reason=(
                    cand.get("reject_reason", "") if status == SignalCandidate.Status.REJECTED else ""
                ),
            )

        rows = [_row(c, SignalCandidate.Status.ACCEPTED) for c in accepted]
        rows += [_row(c, SignalCandidate.Status.REJECTED) for c in rejected]
        if rows and not dry_run:
            SignalCandidate.objects.bulk_create(rows)
    except Exception:
        logger.exception(
            "[PORTFOLIO] record_candidates failed for engine=%s (%d accepted, %d "
            "rejected) — audit trail only, signal flow unaffected.",
            engine, len(accepted), len(rejected),
        )


def compute_betas(svc, symbols: list[str], benchmark_token: str = "NIFTY 50",
                  benchmark_symbol: str = NIFTY50_INDEX_SYMBOL,
                  lookback_days: int = 60) -> dict[str, float]:
    """Per-symbol beta vs NIFTY 50 from daily returns. Cached 12h."""
    cache_key = "intraday_symbol_betas_v1"
    cached = cache.get(cache_key)
    if cached:
        return cached
    if svc is None or not symbols:
        return {}

    try:
        # +20 buffer preserved from the direct-call version (weekend/holiday slack).
        bench = market_data.request_candles(
            svc, benchmark_symbol, benchmark_token, "ONE_DAY",
            lookback_days=lookback_days + 20, exchange="NSE",
        )
        if bench is None or bench.empty or len(bench) < 20:
            return {}
        bench_ret = bench["Close"].pct_change().dropna()
    except Exception:
        return {}

    betas: dict[str, float] = {}
    token_map = svc.get_token_map(symbols, exchange="NSE") or {}
    var_b = float(bench_ret.var())
    if var_b <= 1e-12:
        return {}

    for sym, token in token_map.items():
        try:
            df = market_data.request_candles(
                svc, sym, token, "ONE_DAY", lookback_days=lookback_days + 20, exchange="NSE"
            )
            if df is None or df.empty or len(df) < 20:
                continue
            r = df["Close"].pct_change().dropna()
            joined = pd.concat([r, bench_ret], axis=1, join="inner").dropna()
            if len(joined) < 20:
                continue
            betas[sym] = round(float(joined.cov().iloc[0, 1] / var_b), 3)
        except Exception:
            continue

    if betas:
        cache.set(cache_key, betas, timeout=CORR_TTL)
    return betas


def net_portfolio_beta(positions: list[dict], betas: dict[str, float],
                       equity: float) -> float:
    """Beta-weighted net exposure as a multiple of equity.

    Signed by direction: a short position contributes negative beta, so a balanced
    long/short book can be near beta-neutral even while gross exposure is large.
    """
    if equity <= 0:
        return 0.0
    net = 0.0
    for p in positions:
        beta = betas.get(p.get("symbol"), 1.0)
        notional = float(p.get("qty", 0)) * float(p.get("entry_price", 0) or 0)
        direction = 1 if str(p.get("signal_type", "BUY")).upper() == "BUY" else -1
        net += beta * notional * direction
    return round(net / equity, 3)


def beta_constrained(candidate: dict, open_positions: list[dict], betas: dict[str, float],
                     equity: float, max_abs_beta: float | None = None) -> bool:
    """Would adding this candidate push net portfolio beta outside the band?"""
    limit = MAX_NET_BETA if max_abs_beta is None else max_abs_beta
    prospective = open_positions + [{
        "symbol": candidate["symbol"], "qty": candidate.get("qty", 0),
        "entry_price": candidate.get("entry", 0),
        "signal_type": candidate.get("signal", "BUY"),
    }]
    return abs(net_portfolio_beta(prospective, betas, equity)) <= limit


def attribute_pnl(trade_pnl: float, symbol: str, entry_price: float, qty: int,
                  direction: str, index_return_pct: float, sector_return_pct: float,
                  beta: float = 1.0) -> dict:
    """Decompose realised P&L into market, sector and residual alpha components.

    Without this the engine cannot answer the question that actually matters — is this
    edge, or is it just long exposure on an up day? On a trend day five correlated longs
    look brilliant for reasons that have nothing to do with volume profile.
    """
    notional = float(entry_price) * int(qty)
    sign = 1 if str(direction).upper() == "BUY" else -1

    market = notional * (index_return_pct / 100.0) * beta * sign
    sector = notional * ((sector_return_pct - index_return_pct) / 100.0) * sign
    alpha = float(trade_pnl) - market - sector

    return {
        "total_pnl": round(float(trade_pnl), 2),
        "market_component": round(market, 2),
        "sector_component": round(sector, 2),
        "alpha_component": round(alpha, 2),
        "alpha_share": round(alpha / trade_pnl, 3) if abs(trade_pnl) > 1e-9 else 0.0,
        "beta_used": beta,
    }


def volatility_scalar(realized_vol_pct: float, target_vol_pct: float | None = None) -> float:
    """Scale sizing so portfolio vol tracks a constant target.

    Preferred over Kelly for live sizing: it needs only a volatility estimate, which is
    forecastable, rather than an edge estimate, which is not (see kelly_fraction).
    """
    target = TARGET_PORTFOLIO_VOL_PCT if target_vol_pct is None else target_vol_pct
    if realized_vol_pct <= 1e-6:
        return 1.0
    return float(np.clip(target / realized_vol_pct, 0.25, 2.0))


def kelly_fraction(win_rate: float, payoff_ratio: float, fraction: float = 0.25,
                   n_trades: int = 0, min_trades: int = 300) -> float:
    """Fractional Kelly, gated on sample size.

        f* = (p(b+1) - 1) / b

    Returns 0.0 until `min_trades` observations exist. That gate is the point of this
    function, not a formality: with n=100 and p=0.45, SE(p) ~= 0.05, and

        p=0.40 -> f*=0.10      p=0.45 -> f*=0.175      p=0.50 -> f*=0.25

    a +/-1 SE error moves optimal leverage by 2.5x. Kelly's growth curve is steeply
    asymmetric, so overbetting past 2f* produces NEGATIVE log growth even with a real
    edge. With no track record, p has effectively infinite standard error.
    """
    if n_trades < min_trades:
        return 0.0
    if payoff_ratio <= 0:
        return 0.0
    f_star = (win_rate * (payoff_ratio + 1) - 1) / payoff_ratio
    return float(np.clip(f_star * fraction, 0.0, 0.25))
