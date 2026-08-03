"""
Statistical validation: walk-forward, purged CV, Monte Carlo, Deflated Sharpe.

See doc/INSTITUTIONAL_AUDIT_INTRADAY.md §8.2-8.3.

The governing constraint is sample size. The standard error of an estimated Sharpe over
T years is approximately sqrt((1 + SR^2/2)/T):

    T = 4 yrs  -> SE ~= 0.53   (cannot distinguish SR 0.5 from 0)
    T = 8 yrs  -> SE ~= 0.37
    T = 16 yrs -> SE ~= 0.26   (~2 SE — marginally conclusive)

So a weak edge cannot be confirmed by forward trading in any practical timeframe, which
is why a rigorous historical framework is mandatory rather than optional — and why the
Deflated Sharpe correction matters, since many variants inevitably get tested against
the same history.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# Normal CDF/inverse-CDF implemented directly rather than pulling in scipy — this module
# needs exactly two functions from it, and scipy is a heavy dependency to add to a
# deployed Django service for that.
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, |error| < 1.15e-9)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    p_low, p_high = 0.02425, 1 - 0.02425

    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p > p_high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
           (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)


@dataclass
class WalkForwardWindow:
    train_start: object
    train_end: object
    test_start: object
    test_end: object


def walk_forward_windows(index: pd.DatetimeIndex, train_days: int = 120,
                         test_days: int = 30, step_days: int | None = None) -> list:
    """Rolling train/test splits. Never a single in-sample fit."""
    if len(index) == 0:
        return []
    step = step_days or test_days
    start, end = index.min(), index.max()
    windows, cursor = [], start
    while True:
        tr_end = cursor + pd.Timedelta(days=train_days)
        te_end = tr_end + pd.Timedelta(days=test_days)
        if te_end > end:
            break
        windows.append(WalkForwardWindow(cursor, tr_end, tr_end, te_end))
        cursor += pd.Timedelta(days=step)
    return windows


def purged_kfold_indices(n: int, n_splits: int = 5, embargo_pct: float = 0.01) -> list:
    """Purged K-fold with embargo (Lopez de Prado).

    Plain K-fold leaks when labels span overlapping bars: a training sample immediately
    adjacent to a test sample shares outcome information with it. Purging drops the
    overlap and the embargo additionally removes a buffer after each test fold.
    """
    if n <= n_splits:
        return []
    indices = np.arange(n)
    fold_size = n // n_splits
    embargo = int(n * embargo_pct)
    out = []
    for k in range(n_splits):
        lo = k * fold_size
        hi = n if k == n_splits - 1 else (k + 1) * fold_size
        test = indices[lo:hi]
        train = np.concatenate([
            indices[: max(0, lo - embargo)],
            indices[min(n, hi + embargo):],
        ])
        if len(train) and len(test):
            out.append((train, test))
    return out


def monte_carlo_drawdown(trade_pnls, n_sims: int = 2000, initial_equity: float = 500_000.0,
                         seed: int = 42) -> dict:
    """Bootstrap the trade sequence to get a DISTRIBUTION of max drawdown.

    A single historical drawdown is one draw from that distribution. Reporting it as
    "the" max drawdown understates tail risk, because the realised ordering of wins and
    losses was luck.
    """
    pnls = np.asarray(list(trade_pnls), dtype=float)
    if pnls.size == 0:
        return {"error": "no_trades"}

    rng = np.random.default_rng(seed)
    dds, finals = np.empty(n_sims), np.empty(n_sims)
    for i in range(n_sims):
        path = initial_equity + np.cumsum(rng.choice(pnls, size=pnls.size, replace=True))
        peak = np.maximum.accumulate(path)
        dds[i] = float(((path - peak) / peak).min() * 100.0)
        finals[i] = path[-1]

    return {
        "n_sims": n_sims,
        "median_max_dd_pct": float(abs(np.median(dds))),
        "p95_max_dd_pct": float(abs(np.percentile(dds, 5))),
        "worst_max_dd_pct": float(abs(dds.min())),
        "median_final_equity": float(np.median(finals)),
        "p05_final_equity": float(np.percentile(finals, 5)),
        "prob_of_loss": float((finals < initial_equity).mean()),
    }


def deflated_sharpe_ratio(observed_sharpe: float, n_trials: int, n_obs: int,
                          skew: float = 0.0, kurtosis: float = 3.0) -> dict:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado).

    Testing many variants against one history guarantees some will look good by chance.
    DSR asks whether the best observed Sharpe exceeds what the best of `n_trials` random
    strategies would produce. Essential once parameters are tuned at all.
    """
    if n_obs < 3 or n_trials < 1:
        return {"error": "insufficient_data"}

    euler = 0.5772156649
    # Expected maximum Sharpe from n_trials independent noise strategies.
    if n_trials > 1:
        z1 = _norm_ppf(1 - 1.0 / n_trials)
        z2 = _norm_ppf(1 - 1.0 / (n_trials * math.e))
        sr0 = (1 - euler) * z1 + euler * z2
    else:
        sr0 = 0.0

    denom = np.sqrt(
        max(1e-12,
            1 - skew * observed_sharpe + ((kurtosis - 1) / 4.0) * observed_sharpe ** 2)
    )
    dsr = _norm_cdf(((observed_sharpe - sr0) * math.sqrt(n_obs - 1)) / denom)

    return {
        "observed_sharpe": round(float(observed_sharpe), 3),
        "expected_max_noise_sharpe": round(float(sr0), 3),
        "deflated_sharpe_probability": round(float(dsr), 4),
        "significant_at_95": bool(dsr > 0.95),
        "n_trials": n_trials,
        "n_observations": n_obs,
    }


def sharpe_standard_error(sharpe: float, n_years: float) -> float:
    """SE of an estimated Sharpe. Use to decide whether a result means anything."""
    if n_years <= 0:
        return float("inf")
    return float(np.sqrt((1 + (sharpe ** 2) / 2.0) / n_years))


def minimum_track_record_length(sharpe: float, target_sharpe: float = 0.0,
                                confidence: float = 0.95) -> float:
    """Years required to establish `sharpe` exceeds `target_sharpe` at `confidence`."""
    if sharpe <= target_sharpe:
        return float("inf")
    z = _norm_ppf(confidence)
    return float(((1 + (sharpe ** 2) / 2.0) * (z ** 2)) / ((sharpe - target_sharpe) ** 2))


def parameter_stability(results: dict) -> dict:
    """Prefer a plateau to a peak.

    An optimum surrounded by cliffs is overfit; a slightly worse optimum sitting on a
    broad shelf is more likely to survive out of sample.

    results: {param_value: metric}
    """
    if len(results) < 3:
        return {"error": "need >=3 parameter points"}
    keys = sorted(results.keys())
    vals = np.array([results[k] for k in keys], dtype=float)
    best_i = int(np.argmax(vals))

    neighbours = [vals[i] for i in (best_i - 1, best_i + 1) if 0 <= i < len(vals)]
    best = vals[best_i]
    degradation = (
        float(np.mean([(best - n) / abs(best) for n in neighbours])) if neighbours and abs(best) > 1e-9 else 1.0
    )

    return {
        "best_param": keys[best_i],
        "best_value": float(best),
        "neighbour_degradation": round(degradation, 4),
        "is_plateau": bool(degradation < 0.20),
        "verdict": "plateau (robust)" if degradation < 0.20 else "peak (likely overfit)",
        "coefficient_of_variation": round(float(vals.std() / abs(vals.mean())), 4)
        if abs(vals.mean()) > 1e-9 else None,
    }
