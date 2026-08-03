"""
Event-driven portfolio backtester with explicit costs and slippage.

See doc/INSTITUTIONAL_AUDIT_INTRADAY.md §8.2. The existing run_backtest_for_signal() is a
single-signal replayer: no shared capital, no concurrent positions, no costs, no
slippage. It cannot produce a Sharpe ratio or a drawdown, so it cannot validate anything.

Deliberate design choices, each corresponding to a way backtests usually flatter
themselves:

  * Pessimistic intrabar resolution — if a bar's range spans both stop and target, the
    stop is booked. Path is unknowable at bar resolution, and resolving ambiguity in the
    strategy's favour inflates win rate most on wide-range bars, which are exactly the
    bars where the outcome is least certain.
  * Fills are slipped by the half-spread, never taken at the touch price.
  * Costs are charged on both legs via the explicit cost model.
  * Capital is shared — a signal that cannot be funded is skipped, not silently taken.
  * Signals are consumed in bar order with no lookahead into future bars.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from .cost_model import CostModel, DEFAULT_COST_MODEL

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_time: object
    entry_price: float
    qty: int
    stop_loss: float
    target: float
    exit_time: object = None
    exit_price: float = 0.0
    status: str = "OPEN"
    gross_pnl: float = 0.0
    costs: float = 0.0
    net_pnl: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    # Diagnostics for "why did this time out" analysis — not used by the P&L math,
    # only to understand trade behaviour after the fact.
    strategy_reason: str = ""       # which trigger fired: POC Flip, VA Breakout, etc.
    best_favorable_price: float = 0.0   # closest price got to the profitable side
    mfe_pct_of_target: float = 0.0      # 0 = never moved, 1 = reached target exactly

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestConfig:
    initial_equity: float = 500_000.0
    risk_per_trade_pct: float = 0.30
    max_positions: int = 5
    max_gross_exposure_pct: float = 300.0
    max_position_pct: float = 100.0
    time_stop_bars: int = 8
    daily_loss_limit_pct: float = 2.0
    min_target_cost_multiple: float = 3.0
    cost_model: CostModel = field(default_factory=lambda: DEFAULT_COST_MODEL)


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    equity_curve: pd.Series = None
    metrics: dict = field(default_factory=dict)

    def summary(self) -> str:
        m = self.metrics
        return (
            f"trades={m.get('n_trades', 0)} win={m.get('win_rate', 0):.1%} "
            f"net=Rs.{m.get('net_pnl', 0):,.0f} sharpe={m.get('sharpe', 0):.2f} "
            f"maxDD={m.get('max_drawdown_pct', 0):.1f}% pf={m.get('profit_factor', 0):.2f}"
        )


def _mfe_pct(tr: "Trade", is_long: bool) -> float:
    """Max favorable excursion as a fraction of the entry-to-target distance.

    0.0 = price never moved in the trade's favor at all; 1.0 = it reached the target
    exactly (clipped so a trade that actually hit target reads as 1.0, not slightly
    over from the slipped fill price rounding).
    """
    target_dist = abs(tr.target - tr.entry_price)
    if target_dist <= 0:
        return 0.0
    moved = (tr.best_favorable_price - tr.entry_price) if is_long \
        else (tr.entry_price - tr.best_favorable_price)
    return round(max(0.0, min(1.0, moved / target_dist)), 3)


def _position_size(equity, entry, stop, cfg):
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0 or entry <= 0:
        return 0
    qty = int((equity * cfg.risk_per_trade_pct / 100.0) // risk_per_share)
    return max(0, min(qty, int((equity * cfg.max_position_pct / 100.0) // entry)))


def run_portfolio_backtest(
    signals: pd.DataFrame,
    bars: dict[str, pd.DataFrame],
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Replay a signal set against per-symbol OHLC bars as one shared-capital portfolio.

    signals: columns [timestamp, symbol, direction, entry, stop_loss, target]
    bars:    {symbol: DataFrame[Open, High, Low, Close] indexed by timestamp}
    """
    cfg = config or BacktestConfig()
    cm = cfg.cost_model

    if signals is None or signals.empty:
        return BacktestResult(metrics={"n_trades": 0, "error": "no_signals"})

    signals = signals.sort_values("timestamp").reset_index(drop=True)

    # Unified clock across every symbol, so concurrency and capital are shared correctly.
    all_ts = sorted({t for df in bars.values() for t in df.index})
    if not all_ts:
        return BacktestResult(metrics={"n_trades": 0, "error": "no_bars"})

    equity = cfg.initial_equity
    open_trades: list[Trade] = []
    closed: list[Trade] = []
    equity_points: list[tuple] = []
    day_start_equity = equity
    current_day = None
    halted_days: set = set()

    pending = signals.to_dict("records")
    pending_idx = 0

    for ts in all_ts:
        day = ts.date() if hasattr(ts, "date") else None
        if day != current_day:
            current_day = day
            day_start_equity = equity

        # ── Manage open positions on this bar ────────────────────────────────────
        for tr in list(open_trades):
            df = bars.get(tr.symbol)
            if df is None or ts not in df.index:
                continue
            bar = df.loc[ts]
            high, low = float(bar["High"]), float(bar["Low"])
            tr.bars_held += 1
            is_long = tr.direction == "BUY"

            # Track how far price got toward the target, even on bars that don't close
            # the trade — this is what separates "almost worked" from "never moved" once
            # a trade eventually times out.
            # Initialized to the entry fill, so "better" only trips on a genuine move
            # toward profit — a trade that never moves keeps its excursion at exactly 0.
            favorable_tick = high if is_long else low
            better = (favorable_tick > tr.best_favorable_price) if is_long \
                else (favorable_tick < tr.best_favorable_price)
            if better:
                tr.best_favorable_price = favorable_tick

            exit_px = exit_reason = None
            # Stop before target: pessimistic on ambiguous bars.
            if is_long:
                if low <= tr.stop_loss:
                    exit_px, exit_reason = tr.stop_loss, "STOP"
                elif high >= tr.target:
                    exit_px, exit_reason = tr.target, "TARGET"
            else:
                if high >= tr.stop_loss:
                    exit_px, exit_reason = tr.stop_loss, "STOP"
                elif low <= tr.target:
                    exit_px, exit_reason = tr.target, "TARGET"

            if exit_px is None and tr.bars_held >= cfg.time_stop_bars:
                exit_px, exit_reason = float(bar["Close"]), "TIME_STOP"

            if exit_px is None:
                continue

            fill = cm.slipped_fill(exit_px, is_buy=not is_long, symbol=tr.symbol)
            gross = (fill - tr.entry_price) * tr.qty * (1 if is_long else -1)
            costs = cm.round_trip(symbol=tr.symbol, entry_price=tr.entry_price,
                                  exit_price=fill, qty=tr.qty, is_long=is_long)
            tr.exit_time, tr.exit_price = ts, round(fill, 2)
            tr.status = "CLOSED"
            tr.exit_reason = exit_reason
            tr.gross_pnl = round(gross, 2)
            tr.costs = round(costs, 2)
            tr.net_pnl = round(gross - costs, 2)
            tr.mfe_pct_of_target = _mfe_pct(tr, is_long)
            equity += tr.net_pnl
            open_trades.remove(tr)
            closed.append(tr)

        # ── Daily loss limit ─────────────────────────────────────────────────────
        if day not in halted_days:
            dd = (equity - day_start_equity) / day_start_equity * 100.0
            if dd <= -cfg.daily_loss_limit_pct:
                halted_days.add(day)
                for tr in list(open_trades):
                    df = bars.get(tr.symbol)
                    if df is None or ts not in df.index:
                        continue
                    is_long = tr.direction == "BUY"
                    fill = cm.slipped_fill(float(df.loc[ts]["Close"]),
                                           is_buy=not is_long, symbol=tr.symbol)
                    gross = (fill - tr.entry_price) * tr.qty * (1 if is_long else -1)
                    costs = cm.round_trip(symbol=tr.symbol, entry_price=tr.entry_price,
                                          exit_price=fill, qty=tr.qty, is_long=is_long)
                    tr.exit_time, tr.exit_price = ts, round(fill, 2)
                    tr.status, tr.exit_reason = "CLOSED", "DAILY_LOSS_LIMIT"
                    tr.gross_pnl, tr.costs = round(gross, 2), round(costs, 2)
                    tr.net_pnl = round(gross - costs, 2)
                    equity += tr.net_pnl
                    open_trades.remove(tr)
                    closed.append(tr)

        # ── Open new positions from signals timestamped at this bar ──────────────
        while pending_idx < len(pending) and pending[pending_idx]["timestamp"] <= ts:
            sig = pending[pending_idx]
            pending_idx += 1

            if day in halted_days or len(open_trades) >= cfg.max_positions:
                continue
            sym = sig["symbol"]
            df = bars.get(sym)
            if df is None or ts not in df.index:
                continue
            if any(t.symbol == sym for t in open_trades):
                continue

            entry_ref = float(sig["entry"])
            stop, target = float(sig["stop_loss"]), float(sig["target"])
            is_long = sig["direction"] == "BUY"

            # Cost gate, same arithmetic the live engine applies.
            if entry_ref > 0:
                tgt_pct = abs(target - entry_ref) / entry_ref * 100.0
                if tgt_pct < cfg.min_target_cost_multiple * cm.round_trip_pct(
                    symbol=sym, price=entry_ref, qty=100
                ):
                    continue

            qty = _position_size(equity, entry_ref, stop, cfg)
            if qty <= 0:
                continue

            fill = cm.slipped_fill(entry_ref, is_buy=is_long, symbol=sym)
            gross_used = sum(t.entry_price * t.qty for t in open_trades)
            if gross_used + fill * qty > equity * cfg.max_gross_exposure_pct / 100.0:
                continue

            open_trades.append(Trade(
                symbol=sym, direction=sig["direction"], entry_time=ts,
                entry_price=round(fill, 2), qty=qty, stop_loss=stop, target=target,
                strategy_reason=str(sig.get("reason", "")),
                best_favorable_price=round(fill, 2),
            ))

        unrealised = 0.0
        for tr in open_trades:
            df = bars.get(tr.symbol)
            if df is not None and ts in df.index:
                px = float(df.loc[ts]["Close"])
                unrealised += (px - tr.entry_price) * tr.qty * (1 if tr.direction == "BUY" else -1)
        equity_points.append((ts, equity + unrealised))

    curve = pd.Series(dict(equity_points)).sort_index()
    return BacktestResult(trades=closed, equity_curve=curve,
                          metrics=compute_metrics(closed, curve, cfg.initial_equity))


def compute_metrics(trades: list, curve: pd.Series, initial_equity: float) -> dict:
    """Performance statistics, all net of costs."""
    if not trades:
        return {"n_trades": 0, "win_rate": 0.0, "net_pnl": 0.0, "sharpe": 0.0,
                "max_drawdown_pct": 0.0, "profit_factor": 0.0}

    pnls = np.array([t.net_pnl for t in trades], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]

    gross_profit, gross_loss = wins.sum(), abs(losses.sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-9 else float("inf")

    max_dd = 0.0
    if curve is not None and len(curve) > 1:
        running_max = curve.cummax()
        max_dd = float(((curve - running_max) / running_max).min() * 100.0)

    sharpe = 0.0
    if curve is not None and len(curve) > 2:
        daily = curve.resample("1D").last().dropna() if hasattr(curve.index, "freq") or True else curve
        rets = daily.pct_change().dropna()
        if len(rets) > 2 and rets.std() > 1e-9:
            sharpe = float(rets.mean() / rets.std() * np.sqrt(252))

    return {
        "n_trades": len(trades),
        "win_rate": float((pnls > 0).mean()),
        "net_pnl": float(pnls.sum()),
        "gross_pnl": float(sum(t.gross_pnl for t in trades)),
        "total_costs": float(sum(t.costs for t in trades)),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(profit_factor) if np.isfinite(profit_factor) else 999.0,
        "expectancy": float(pnls.mean()),
        "max_drawdown_pct": abs(max_dd),
        "sharpe": sharpe,
        "return_pct": float(pnls.sum() / initial_equity * 100.0),
        "cost_drag_pct": float(sum(t.costs for t in trades) / initial_equity * 100.0),
        "exit_breakdown": {r: sum(1 for t in trades if t.exit_reason == r)
                           for r in {t.exit_reason for t in trades}},
    }
