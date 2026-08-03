import { useState, useEffect, useCallback } from "react";
import { Link, useSearchParams } from "react-router-dom";
import API from "../api/axios";
import NotificationBell from "../components/NotificationBell";

/* ─── Shared Components ─── */
function TableSkeleton({ cols, rows = 5 }) {
  return (
    <div className="space-y-3 mt-4 p-4">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex gap-3">
          {Array.from({ length: cols }).map((_, i) => (
            <div key={i} className="h-6 flex-1 animate-pulse rounded bg-gray-800/70" />
          ))}
        </div>
      ))}
    </div>
  );
}

function StatusBadge({ status }) {
  const map = {
    ACTIVE:          { bg: "bg-emerald-500/10", text: "text-emerald-400", ring: "ring-emerald-500/20", label: "Active 📈", dot: "bg-emerald-400" },
    PENDING:         { bg: "bg-amber-500/10", text: "text-amber-400", ring: "ring-amber-500/20", label: "Pending ⏳", dot: "bg-amber-400" },
    TARGET1:         { bg: "bg-sky-500/10", text: "text-sky-400", ring: "ring-sky-500/20", label: "Target 1 🎯", dot: "bg-sky-400" },
    TARGET2:         { bg: "bg-teal-500/10", text: "text-teal-400", ring: "ring-teal-500/20", label: "Target 2 🎯", dot: "bg-teal-400" },
    REVIEW_REQUIRED: { bg: "bg-orange-500/10", text: "text-orange-400", ring: "ring-orange-500/20", label: "Review Req 🔔", dot: "bg-orange-400" },
    ARCHIVED:        { bg: "bg-gray-500/10", text: "text-gray-400", ring: "ring-gray-500/20", label: "Archived 📦", dot: "bg-gray-400" },
    EXPIRED:         { bg: "bg-violet-500/10", text: "text-violet-400", ring: "ring-violet-500/20", label: "Expired ⏰", dot: "bg-violet-400" },
    CANCELLED:       { bg: "bg-red-500/10", text: "text-red-400", ring: "ring-red-500/20", label: "Cancelled ❌", dot: "bg-red-400" },
  };
  const s = map[status] || { bg: "bg-gray-500/10", text: "text-gray-400", ring: "ring-gray-500/20", label: status, dot: "bg-gray-400" };
  return (
    <span className={`inline-flex items-center gap-1.5 ${s.bg} ${s.text} ring-1 ${s.ring} px-2.5 py-1 rounded-full text-[10px] font-bold uppercase tracking-wider`}>
      <span className={`w-1.5 h-1.5 rounded-full ${s.dot} animate-pulse`} />
      {s.label}
    </span>
  );
}

function PnlDisplay({ pnl }) {
  if (pnl === null || pnl === undefined) return <span className="text-gray-600">—</span>;
  const isPos = pnl >= 0;
  return (
    <span className={`font-mono font-bold ${isPos ? "text-emerald-400" : "text-red-400"}`}>
      {isPos ? "+" : ""}{pnl.toFixed(2)}%
    </span>
  );
}

/* ─── Tab Definitions ─── */
const TAB_CONFIG = [
  { key: "pending",         label: "Pending",     icon: "⏳", color: "amber" },
  { key: "active",          label: "Active",      icon: "📈", color: "emerald" },
  { key: "target1",         label: "Target 1",    icon: "🎯", color: "sky" },
  { key: "target2",         label: "Target 2",    icon: "🎯", color: "teal" },
  { key: "review_required", label: "Review Req",  icon: "🔔", color: "orange" },
  { key: "archived",        label: "Archived",    icon: "📦", color: "gray" },
  { key: "expired",         label: "Expired",     icon: "⏰", color: "violet" },
];

/* Long-Term signals use SignalHistory's Status vocabulary — a different, smaller set
   than ShortTermSignal's, so this is a separate tab config rather than reusing TAB_CONFIG. */
const LT_TAB_CONFIG = [
  { key: "pending",    label: "Pending",    icon: "⏳", color: "amber" },
  { key: "active",     label: "Active",     icon: "📈", color: "emerald" },
  { key: "hit_target", label: "Hit Target", icon: "🎯", color: "sky" },
  { key: "hit_sl",     label: "Hit SL",     icon: "🛑", color: "red" },
  { key: "expired",    label: "Expired",    icon: "⏰", color: "violet" },
  { key: "cancelled",  label: "Cancelled",  icon: "❌", color: "gray" },
];

/* ─── Main Page ─── */
export default function ProSystem() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [searchParams, setSearchParams] = useSearchParams();
  const viewMode = searchParams.get("view") === "long_term" ? "long_term" : "short_term";
  const [activeTab, setActiveTab] = useState("pending");
  const [capital, setCapital] = useState(500000);

  const fetchData = useCallback(() => {
    setLoading(true);
    API.get("/api/stocks/pro-system/")
      .then((res) => setData(res.data))
      .catch((e) => console.error("Error fetching pro system:", e))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  // Reset to a valid default tab whenever the view mode switches, since the two
  // modes use different tab vocabularies (e.g. "target1" doesn't exist for long-term).
  useEffect(() => { setActiveTab("pending"); }, [viewMode]);

  const setViewMode = (mode) => {
    setSearchParams(mode === "long_term" ? { view: "long_term" } : {});
  };

  const md = data?.market_direction;
  const isBullish = md?.trend === "BULLISH";
  const isLongTerm = viewMode === "long_term";
  const analytics = isLongTerm ? (data?.long_term?.analytics || {}) : (data?.analytics || {});
  const tabs = isLongTerm ? (data?.long_term?.tabs || {}) : (data?.tabs || {});
  const activeTabConfig = isLongTerm ? LT_TAB_CONFIG : TAB_CONFIG;
  const maxLossRs = Math.round((capital * 1.5) / 100);
  // Long-term has no SL to size risk against (buy-and-hold), so position size for the
  // ₹ P&L figure is an equal-weight split of Total Capital across active LT positions.
  const ltActiveCount = data?.long_term?.analytics?.active_count || 0;
  const ltCapitalPerStock = ltActiveCount > 0 ? capital / ltActiveCount : 0;

  const currentTabData = tabs[activeTab] || [];

  return (
    <div className="min-h-screen bg-gray-950 font-sans text-gray-200">
      {/* ── Header ── */}
      <header className="sticky top-0 z-10 border-b border-gray-800 bg-gray-950/80 backdrop-blur">
        <div className="mx-auto flex max-w-[90rem] items-center justify-between px-4 py-3 sm:px-6">
          <div className="flex items-center gap-4">
            <Link to="/" className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
              🏆 <span className="bg-gradient-to-r from-indigo-400 to-violet-400 bg-clip-text text-transparent">TRADE ENGINE</span>
            </Link>
            <span className="hidden sm:inline-flex items-center gap-1.5 bg-gray-800 text-gray-400 text-[10px] font-bold uppercase tracking-wider px-2.5 py-1 rounded-full">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
              AI Powered
            </span>
          </div>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={fetchData}
              disabled={loading}
              className="flex items-center gap-1.5 rounded-lg bg-gray-800 px-4 py-1.5 text-sm font-medium text-gray-300 transition hover:bg-gray-700 hover:text-white disabled:opacity-50"
            >
              <span className={loading ? "animate-spin" : ""}>🔄</span>
              Refresh
            </button>
            <NotificationBell />
            <Link
              to="/"
              className="rounded-lg bg-gray-800 px-4 py-1.5 text-sm font-medium text-gray-300 transition hover:bg-gray-700 hover:text-white"
            >
              Dashboard
            </Link>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[90rem] space-y-6 px-4 py-6 sm:px-6">

        {/* ── Strategy Toggle ── */}
        <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-xl p-1 w-fit">
          <button
            type="button"
            onClick={() => setViewMode("short_term")}
            className={`px-4 py-2 rounded-lg text-xs font-bold transition-all ${
              !isLongTerm ? "bg-gray-800 text-white shadow-lg" : "text-gray-500 hover:text-gray-300 hover:bg-gray-800/50"
            }`}
          >
            Short-Term
          </button>
          <button
            type="button"
            onClick={() => setViewMode("long_term")}
            className={`px-4 py-2 rounded-lg text-xs font-bold transition-all ${
              isLongTerm ? "bg-gray-800 text-white shadow-lg" : "text-gray-500 hover:text-gray-300 hover:bg-gray-800/50"
            }`}
          >
            Long-Term
          </button>
        </div>

        {/* ── Analytics Summary Cards ── */}
        {isLongTerm ? (
          <section className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <AnalyticsCard label="Total Signals" value={analytics.total || 0} icon="📊" />
            <AnalyticsCard label="Active" value={analytics.active_count || 0} icon="📈" color="emerald" />
            <AnalyticsCard label="Pending" value={analytics.pending_count || 0} icon="⏳" color="amber" />
            <AnalyticsCard
              label="Win Rate"
              value={`${analytics.win_rate || 0}%`}
              icon="🏆"
              color={(analytics.win_rate || 0) >= 50 ? "emerald" : "red"}
            />
          </section>
        ) : (
          <section className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3">
            <AnalyticsCard label="Total Trades" value={analytics.total_trades || 0} icon="📊" />
            <AnalyticsCard label="Active" value={analytics.active_count || 0} icon="📈" color="emerald" />
            <AnalyticsCard label="Pending" value={analytics.pending_count || 0} icon="⏳" color="amber" />
            <AnalyticsCard label="Wins" value={analytics.total_wins || 0} icon="🎯" color="sky" />
            <AnalyticsCard label="Losses" value={analytics.total_losses || 0} icon="🛑" color="red" />
            <AnalyticsCard
              label="Win Rate"
              value={`${analytics.win_rate || 0}%`}
              icon="🏆"
              color={(analytics.win_rate || 0) >= 50 ? "emerald" : "red"}
            />
            <AnalyticsCard
              label="Unrealized"
              value={`${(analytics.unrealized_pnl || 0) >= 0 ? "+" : ""}${(analytics.unrealized_pnl || 0).toFixed(1)}%`}
              icon="💰"
              color={(analytics.unrealized_pnl || 0) >= 0 ? "emerald" : "red"}
            />
          </section>
        )}

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-4">
          {/* Main Workspace (Left 3 cols) */}
          <div className="lg:col-span-3 space-y-6">

            {/* ── Market Direction ── */}
            <section>
              <h2 className="text-sm font-bold text-gray-400 uppercase tracking-wider mb-3 flex items-center gap-2">
                <span className="bg-gray-800 text-gray-400 rounded w-5 h-5 inline-flex items-center justify-center text-[10px]">1</span>
                MARKET DIRECTION
              </h2>
              {loading ? (
                <div className="h-20 animate-pulse rounded-xl bg-gray-800 px-6 py-4" />
              ) : (
                <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
                  <div className={`px-6 py-4 flex flex-col md:flex-row md:items-center justify-between gap-4 border-l-4 ${isBullish ? 'border-emerald-500' : 'border-red-500'}`}>
                    <div>
                      <h3 className="text-white text-xl font-bold flex items-center gap-2">
                        {isBullish ? '🟢 BULLISH BIAS' : '🔴 BEARISH BIAS'}
                      </h3>
                      <p className="text-gray-400 text-sm mt-1">{md?.rule}</p>
                    </div>
                    <div className="flex gap-3">
                      <MetricChip label="NIFTY 50" value={md?.close} />
                      <MetricChip label="20 EMA" value={md?.ema20} isAbove={md?.close > md?.ema20} />
                      <MetricChip label="50 EMA" value={md?.ema50} isAbove={md?.close > md?.ema50} />
                    </div>
                  </div>
                </div>
              )}
            </section>

            {/* ── Tabs ── */}
            <section>
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-bold text-gray-400 uppercase tracking-wider flex items-center gap-2">
                  <span className="bg-gray-800 text-gray-400 rounded w-5 h-5 inline-flex items-center justify-center text-[10px]">2</span>
                  TRADE SIGNALS
                </h2>
              </div>

              {/* Tab Bar */}
              <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-xl p-1 mb-4 overflow-x-auto">
                {activeTabConfig.map((tab) => {
                  const count = (tabs[tab.key] || []).length;
                  const isActive = activeTab === tab.key;
                  return (
                    <button
                      key={tab.key}
                      onClick={() => setActiveTab(tab.key)}
                      className={`flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-bold transition-all whitespace-nowrap ${
                        isActive
                          ? "bg-gray-800 text-white shadow-lg"
                          : "text-gray-500 hover:text-gray-300 hover:bg-gray-800/50"
                      }`}
                    >
                      <span>{tab.icon}</span>
                      {tab.label}
                      {count > 0 && (
                        <span className={`ml-1 px-1.5 py-0.5 rounded-full text-[10px] font-bold ${
                          isActive
                            ? `bg-${tab.color}-500/20 text-${tab.color}-400`
                            : "bg-gray-700 text-gray-400"
                        }`}>
                          {count}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>

              {/* Tab Content Table */}
              <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
                {loading ? (
                  <TableSkeleton cols={7} />
                ) : currentTabData.length === 0 ? (
                  <div className="p-12 text-center">
                    <div className="text-3xl mb-3 opacity-50">
                      {activeTabConfig.find((t) => t.key === activeTab)?.icon || "📊"}
                    </div>
                    <p className="text-gray-500 text-sm">
                      No signals in <span className="font-bold text-gray-400">{activeTabConfig.find((t) => t.key === activeTab)?.label}</span> tab
                    </p>
                  </div>
                ) : isLongTerm ? (
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-sm whitespace-nowrap">
                      <thead>
                        <tr className="border-b border-gray-800 text-xs text-gray-500 bg-gray-900/50">
                          <th className="font-semibold p-4">Stock</th>
                          <th className="font-semibold p-4">Reason</th>
                          <th className="font-semibold p-4">Status</th>
                          <th className="font-semibold p-4 text-right">Entry</th>
                          <th className="font-semibold p-4 text-right">Current</th>
                          <th className="font-semibold p-4 text-right">P&L</th>
                          <th className="font-semibold p-4 text-right">SL / Target</th>
                          <th className="font-semibold p-4">Generated</th>
                        </tr>
                      </thead>
                      <tbody>
                        {currentTabData.map((s, i) => {
                          const genDate = s.generated_at ? new Date(s.generated_at).toLocaleDateString() : "";
                          const ltQty = (s.status === "ACTIVE" && s.entry_price > 0 && ltCapitalPerStock > 0)
                            ? Math.floor(ltCapitalPerStock / s.entry_price)
                            : 0;
                          const ltPnlRupees = (ltQty > 0 && s.pnl_pct != null)
                            ? (s.pnl_pct / 100) * s.entry_price * ltQty
                            : null;
                          return (
                            <tr key={s.id || i} className="border-b border-gray-800/50 hover:bg-gray-800/20 transition-colors">
                              <td className="p-4">
                                <span className="font-bold text-indigo-300 text-sm">{s.symbol}</span>
                              </td>
                              <td className="p-4 text-xs text-gray-400 max-w-xs">{s.reason || "—"}</td>
                              <td className="p-4"><StatusBadge status={s.status} /></td>
                              <td className="p-4 text-right font-mono text-gray-300">
                                {s.entry_price != null ? `₹${s.entry_price.toFixed(2)}` : "—"}
                              </td>
                              <td className="p-4 text-right font-mono text-gray-300">
                                {s.current_price != null ? `₹${s.current_price.toFixed(2)}` : "—"}
                              </td>
                              <td className="p-4 text-right">
                                <PnlDisplay pnl={s.pnl_pct} />
                                {ltPnlRupees != null && (
                                  <div className={`text-[11px] font-mono mt-0.5 ${ltPnlRupees >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                                    {ltPnlRupees >= 0 ? "+" : "-"}₹{Math.abs(ltPnlRupees).toFixed(0)}
                                  </div>
                                )}
                              </td>
                              <td className="p-4 text-right font-mono text-xs">
                                <div className="text-red-400 font-semibold">SL: {s.stop_loss != null ? `₹${s.stop_loss.toFixed(2)}` : "—"}</div>
                                <div className="text-emerald-400 mt-0.5">T: {s.target != null ? `₹${s.target.toFixed(2)}` : "—"}</div>
                              </td>
                              <td className="p-4 text-xs text-gray-500 font-mono">{genDate}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-sm whitespace-nowrap">
                      <thead>
                        <tr className="border-b border-gray-800 text-xs text-gray-500 bg-gray-900/50">
                          <th className="font-semibold p-4">Stock</th>
                          <th className="font-semibold p-4">Setup</th>
                          <th className="font-semibold p-4">Status</th>
                          <th className="font-semibold p-4">Duration</th>
                          <th className="font-semibold p-4 text-right">Entry</th>
                          <th className="font-semibold p-4 text-right">Current</th>
                          <th className="font-semibold p-4 text-right">SL / Targets</th>
                          <th className="font-semibold p-4 text-right">Peak / DD</th>
                          <th className="font-semibold p-4 text-right">P&L</th>
                          {["active", "target1", "target2"].includes(activeTab) && <th className="font-semibold p-4 text-right">Qty</th>}
                        </tr>
                      </thead>
                      <tbody>
                        {currentTabData.map((s, i) => {
                          const slAmt = s.entry_price - s.stop_loss;
                          const posSizeShares = slAmt > 0 ? Math.floor(maxLossRs / slAmt) : 0;
                          const posSizeValue = posSizeShares * s.entry_price;
                          const pnlRupees = (posSizeShares > 0 && s.pnl_pct != null)
                            ? (s.pnl_pct / 100) * s.entry_price * posSizeShares
                            : null;

                          // Format Dates
                          const genDate = s.generated_at ? new Date(s.generated_at).toLocaleDateString() : "";
                          const actDate = s.activated_at ? new Date(s.activated_at).toLocaleDateString() : "";
                          const extDate = s.exited_at ? new Date(s.exited_at).toLocaleDateString() : "";

                          return (
                            <tr key={s.id || i} className="border-b border-gray-800/50 hover:bg-gray-800/20 transition-colors">
                              {/* Stock Column */}
                              <td className="p-4">
                                <div className="flex items-center gap-2">
                                  <span className="font-bold text-indigo-300 text-sm">{s.symbol}</span>
                                  <span className="bg-indigo-500/10 text-indigo-300 ring-1 ring-indigo-500/20 px-1.5 py-0.5 rounded text-[9px] font-mono font-bold">
                                    AI: {s.ai_score?.toFixed(1)}
                                  </span>
                                </div>
                                <div className="text-[10px] text-gray-500 mt-0.5 font-mono">
                                  Gen: {genDate}
                                </div>
                              </td>

                              {/* Setup Column */}
                              <td className="p-4">
                                <div className="flex flex-col gap-1">
                                  <span className="bg-emerald-500/10 text-emerald-400 ring-1 ring-emerald-500/20 px-2 py-0.5 rounded text-[10px] font-bold w-fit">
                                    🟢 BUY
                                  </span>
                                  <span className="bg-indigo-500/10 text-indigo-400 ring-1 ring-indigo-500/20 px-2 py-0.5 rounded text-[10px] font-semibold w-fit">
                                    {s.setup}
                                  </span>
                                </div>
                              </td>

                              {/* Status Column */}
                              <td className="p-4">
                                <div className="flex flex-col gap-1">
                                  <StatusBadge status={s.status} />
                                  {s.exit_reason && (
                                    <span className="text-[10px] text-gray-400 italic">
                                      {s.exit_reason}
                                    </span>
                                  )}
                                  {s.cooldown_until && (
                                    <span className="text-[9px] text-rose-400 font-semibold font-mono">
                                      Lock till: {new Date(s.cooldown_until).toLocaleDateString()}
                                    </span>
                                  )}
                                </div>
                              </td>

                              {/* Duration Column */}
                              <td className="p-4 text-xs">
                                {s.status === "PENDING" ? (
                                  <span className="text-gray-500 font-mono">Setup Pending</span>
                                ) : (
                                  <div className="flex flex-col gap-0.5 font-mono">
                                    <span className="text-gray-300 font-semibold">Held {s.holding_days} Days</span>
                                    {s.activated_at && <span className="text-[9px] text-gray-500">In: {actDate}</span>}
                                    {s.exited_at && <span className="text-[9px] text-gray-500">Out: {extDate}</span>}
                                  </div>
                                )}
                              </td>

                              {/* Entry Price */}
                              <td className="p-4 text-right">
                                <div className="font-mono text-gray-300">₹{s.entry_price?.toFixed(2)}</div>
                              </td>

                              {/* Current Price */}
                              <td className="p-4 text-right">
                                <div className="font-mono text-white font-bold">
                                  ₹{s.current_price?.toFixed(2) || "—"}
                                </div>
                              </td>

                              {/* SL / Targets */}
                              <td className="p-4 text-right font-mono text-xs">
                                <div className="text-red-400 font-semibold">SL: ₹{s.stop_loss?.toFixed(2)}</div>
                                <div className="text-emerald-400 mt-0.5">T1: ₹{s.target?.toFixed(2)}</div>
                                {s.target2 && <div className="text-teal-400 mt-0.5">T2: ₹{s.target2?.toFixed(2)}</div>}
                                {s.target3 && <div className="text-indigo-400 mt-0.5">T3: ₹{s.target3?.toFixed(2)}</div>}
                              </td>

                              {/* Peak / Drawdown Metrics */}
                              <td className="p-4 text-right font-mono text-xs">
                                <div className="text-emerald-400 font-bold">Peak: +{s.highest_profit?.toFixed(1)}%</div>
                                <div className="text-red-400 font-semibold mt-0.5">DD: {s.max_drawdown?.toFixed(1)}%</div>
                              </td>

                              {/* P&L */}
                              <td className="p-4 text-right">
                                <PnlDisplay pnl={s.pnl_pct} />
                                {pnlRupees != null && (
                                  <div className={`text-[11px] font-mono mt-0.5 ${pnlRupees >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                                    {pnlRupees >= 0 ? "+" : "-"}₹{Math.abs(pnlRupees).toFixed(0)}
                                  </div>
                                )}
                              </td>

                              {/* Sizing Qty */}
                              {["active", "target1", "target2"].includes(activeTab) && (
                                <td className="p-4 text-right text-xs">
                                  <div className="font-mono text-gray-300">{posSizeShares} qty</div>
                                  <div className="text-gray-500 mt-0.5">₹{posSizeValue.toLocaleString("en-IN")}</div>
                                </td>
                              )}
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </section>
          </div>

          {/* Right Sidebar */}
          <div className="lg:col-span-1 space-y-5">

            {/* Risk Calculator */}
            <div className="rounded-xl border border-indigo-500/30 bg-indigo-500/5 p-5">
              <h3 className="text-sm font-bold text-indigo-400 uppercase tracking-wider mb-4 flex items-center gap-2">
                💰 Risk Management
              </h3>
              <div className="space-y-3">
                <label className="block text-xs text-gray-400">Total Capital (₹)</label>
                <input
                  type="number"
                  value={capital}
                  onChange={(e) => setCapital(Number(e.target.value))}
                  className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:ring-1 focus:ring-indigo-500 outline-none"
                />
                <div className="pt-2 flex justify-between items-center border-t border-gray-800/50">
                  <span className="text-sm text-gray-300">Max Risk (1.5%)</span>
                  <span className="text-lg font-bold text-red-400">₹{maxLossRs.toLocaleString()}</span>
                </div>
                <p className="text-[10px] text-gray-500 leading-tight">
                  Applied to calculate Position Size (qty) in the Active tab.
                </p>
              </div>
            </div>

            {/* Pipeline Flow */}
            <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
              <h3 className="text-xs font-bold text-gray-400 uppercase tracking-wider mb-4">🧠 AI Pipeline</h3>
              <div className="flex flex-col gap-1 text-xs font-medium text-gray-300">
                {[
                  { time: "9:05 AM", label: "Pre-market Trend", color: "text-amber-400" },
                  { time: "10:00 AM", label: "AI Scanner", color: "text-indigo-400" },
                  { time: "10min", label: "Intraday Check", color: "text-sky-400" },
                  { time: "3:25 PM", label: "EOD Evaluation", color: "text-violet-400" },
                  { time: "Saturday", label: "Cleanup", color: "text-gray-500" },
                ].map((step, i) => (
                  <div key={i} className="flex items-center gap-3 py-1.5">
                    <span className={`text-[10px] font-mono ${step.color} w-16 text-right`}>{step.time}</span>
                    <span className="w-1.5 h-1.5 rounded-full bg-gray-600" />
                    <span className="bg-gray-800 px-2.5 py-1 rounded text-gray-300">{step.label}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Strict Rules */}
            <div className="rounded-xl border border-red-500/20 bg-red-500/5 p-5">
              <h3 className="text-sm font-bold text-red-400 uppercase tracking-wider mb-4">⚠️ Strict Rules</h3>
              <ul className="text-xs text-gray-300 space-y-2.5">
                <li className="flex gap-2"><span>❌</span> No random trades</li>
                <li className="flex gap-2"><span>❌</span> No overtrading</li>
                <li className="flex gap-2"><span>❌</span> No revenge trading</li>
                <li className="flex gap-2"><span>❌</span> No skipping stop loss</li>
                <li className="flex gap-2"><span>✅</span> Trail using 20 EMA only</li>
              </ul>
            </div>

            {/* Daily Routine */}
            <div className="space-y-3">
              <div>
                <h4 className="text-sm font-bold text-white flex items-center gap-2">⏱️ Daily Routine</h4>
                <ul className="text-xs text-gray-400 mt-2 space-y-1.5 list-disc pl-6">
                  <li>Check Telegram for new setups at 10:05 AM</li>
                  <li>Monitor active holdings during market hours</li>
                  <li>EOD review at 3:30 PM for trailing exits</li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

/* ─── Utility Components ─── */
function AnalyticsCard({ label, value, icon, color = "gray" }) {
  const colorMap = {
    emerald: "border-emerald-500/20 bg-emerald-500/5",
    amber: "border-amber-500/20 bg-amber-500/5",
    sky: "border-sky-500/20 bg-sky-500/5",
    red: "border-red-500/20 bg-red-500/5",
    gray: "border-gray-800 bg-gray-900",
  };
  return (
    <div className={`rounded-xl border p-3.5 ${colorMap[color] || colorMap.gray}`}>
      <div className="text-[10px] font-bold text-gray-500 uppercase tracking-wider flex items-center gap-1">
        <span>{icon}</span> {label}
      </div>
      <div className="text-xl font-bold text-white mt-1 font-mono">{value}</div>
    </div>
  );
}

function MetricChip({ label, value, isAbove }) {
  const baseClass = "rounded px-3 py-2 text-center min-w-24";
  const colorClass = isAbove === undefined
    ? "bg-gray-800"
    : isAbove
    ? "bg-emerald-500/10 text-emerald-400 ring-1 ring-emerald-500/20"
    : "bg-red-500/10 text-red-400 ring-1 ring-red-500/20";
  return (
    <div className={`${baseClass} ${colorClass}`}>
      <div className="text-xs font-semibold mb-0.5">{label}</div>
      <div className="font-mono text-white">{value}</div>
    </div>
  );
}
