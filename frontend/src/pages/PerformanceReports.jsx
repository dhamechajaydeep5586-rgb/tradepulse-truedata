import { useEffect, useState } from "react";
import API from "../api/axios";
import { Link } from "react-router-dom";
import NotificationBell from "../components/NotificationBell";
import jsPDF from "jspdf";
import autoTable from "jspdf-autotable";

const TAB_LABELS = {
  all: "All",
  intraday: "Intraday Buy/Sell",
  short_term: "Short-Term Buy",
  long_term: "Long-Term Buy",
  specialist: "Option Selling",
  option_buying: "Option Buying",
};

const changeTypeLabels = {
  SIGNAL_FLIP: { label: "Signal Flip", icon: "🔄", color: "text-amber-400" },
  EXPIRY_ROLL: { label: "Expiry Roll", icon: "⟳", color: "text-blue-400" },
  ENTRY_UPDATED: { label: "Entry Updated", icon: "📝", color: "text-gray-400" },
  NEW_SIGNAL: { label: "New Signal", icon: "🆕", color: "text-emerald-400" },
  SIGNAL_REMOVED: { label: "Removed", icon: "❌", color: "text-red-400" },
};

export default function PerformanceReports() {
  const [report, setReport] = useState(null);
  const [proReport, setProReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState(null);
  const [activeTab, setActiveTab] = useState("all");
  const [selectedDate, setSelectedDate] = useState("");
  const [capital, setCapital] = useState(100000);
  const [leverage, setLeverage] = useState(1);

  const fetchReport = () => {
    setLoading(true);
    const dateQuery = selectedDate ? `?date=${selectedDate}` : "";
    // Short-Term Buy lives on its own model (ShortTermSignal), not SignalHistory,
    // so it's served by a separate endpoint — fetched alongside so every one of
    // the Dashboard's 6 categories has date-wise history on this one page.
    //
    // Settled independently (not Promise.all): a transient failure on one endpoint
    // must not wipe the other's already-fetched data. Failure branches below never
    // call setReport(null)/setProReport(null) — on a refresh failure the last-good
    // value simply stays in state; on an initial-load failure the state is still its
    // useState(null) default, so it reads as empty exactly as before. Either way,
    // `fetchError` drives a small inline indicator instead of blanking the page.
    // See AUDIT_REMEDIATION_PLAN.md 3.3.1.
    Promise.allSettled([
      API.get(`/api/stocks/performance-report/${dateQuery}`),
      API.get(`/api/stocks/pro-performance-report/${dateQuery}`),
    ]).then(([reportResult, proReportResult]) => {
      if (reportResult.status === "fulfilled") {
        setReport(reportResult.value.data);
      }
      if (proReportResult.status === "fulfilled") {
        setProReport(proReportResult.value.data);
      }
      const failed = reportResult.status === "rejected" || proReportResult.status === "rejected";
      setFetchError(failed ? "Couldn't refresh — showing last known data." : null);
      setLoading(false);
    });
  };

  // Dynamic Calculation Helper
  const getProcessedData = (historyArr) => {
    return historyArr.map(s => {
      // Prefer the size the engine actually generated the signal at. Intraday is
      // sized by risk (equity x risk% / stop distance), so recomputing an
      // equal-notional quantity here would report P&L on a position that was never
      // taken — and would understate risk dispersion between wide- and tight-stop
      // trades, which is exactly what risk-based sizing exists to remove.
      let qty = s.qty || 1;
      if (!s.qty && s.category !== 'specialist' && s.category !== 'short_term') {
        qty = Math.floor((capital * leverage) / s.entry);
      }

      if (["PENDING", "CANCELLED"].includes(s.status)) {
        return { ...s, dynamicQty: qty, dynamicPnL: 0, dynamicPnLPct: "0.00" };
      }

      // Target/SL not hit yet — show today's running (unrealized) P&L off the
      // live quote instead of just "—" until the position eventually closes.
      if (s.status === "ACTIVE") {
        if (s.current_price != null) {
          const multiplier = s.type === 'SELL' ? -1 : 1;
          const pnl = (s.current_price - s.entry) * qty * multiplier;
          const pnl_pct = (((s.current_price - s.entry) / s.entry) * 100 * multiplier).toFixed(2);
          return { ...s, dynamicQty: qty, dynamicPnL: pnl, dynamicPnLPct: pnl_pct, isRunning: true };
        }
        return { ...s, dynamicQty: qty, dynamicPnL: 0, dynamicPnLPct: "0.00" };
      }

      // Override for multi-leg strategies natively tracked by backend
      if (s.final_pnl !== undefined) {
        return { ...s, dynamicQty: 1, dynamicPnL: s.final_pnl, dynamicPnLPct: s.final_pnl_pct || "0.00" };
      }

      // Short-Term Buy already computes pnl/pnl_pct server-side (trailing-stop /
      // multi-target exits don't reduce to a simple entry-vs-exit delta the way
      // the generic fallback below assumes) — use it as-is rather than recompute.
      if (s.category === 'short_term' && s.pnl !== undefined) {
        return { ...s, dynamicQty: s.qty || 1, dynamicPnL: s.pnl, dynamicPnLPct: s.pnl_pct ?? "0.00" };
      }

      const exitPrice = s.exit_price || s.entry;
      const multiplier = s.type === 'SELL' ? -1 : 1;
      const pnl = (exitPrice - s.entry) * qty * multiplier;
      const pnl_pct = (((exitPrice - s.entry) / s.entry) * 100 * multiplier).toFixed(2);

      return { ...s, dynamicQty: qty, dynamicPnL: pnl, dynamicPnLPct: pnl_pct };
    });
  };

  const getSummary = (processedArr) => {
    const total = processedArr.length;
    const targets = processedArr.filter(
      (s) => s.status === 'HIT_TARGET' || (s.status === 'EXPIRED' && s.dynamicPnL > 0)
    ).length;
    const sls = processedArr.filter(
      (s) => s.status === 'HIT_SL' || (s.status === 'EXPIRED' && s.dynamicPnL < 0)
    ).length;
    const totalPnL = processedArr.reduce((sum, s) => sum + s.dynamicPnL, 0);
    const winRate = total > 0 ? Math.round(((targets) / (targets + sls || 1)) * 100) : 0;

    return { total, targets_hit: targets, sl_hits: sls, total_pnl: totalPnL, win_rate: winRate };
  };

  const downloadPDF = (category) => {
    if (!report) return;

    const doc = new jsPDF();
    const rawDataByCategory = {
      intraday: report.intraday_history,
      short_term: short_term_history,
      long_term: report.long_term_history,
      specialist: report.specialist_history,
      option_buying: report.option_buying_history,
    };
    const rawData = rawDataByCategory[category] || [];
    const data = getProcessedData(rawData);
    const summary = getSummary(data);

    const catLabel = TAB_LABELS[category] || category;
    const dateStr = report.date || "Today";

    // Header
    doc.setFontSize(20);
    doc.setTextColor(16, 185, 129); // emerald-500
    doc.text("TradePulse AI - Performance Report", 14, 20);
    
    doc.setFontSize(10);
    doc.setTextColor(100);
    doc.text(`Generated on: ${new Date().toLocaleString()}`, 14, 28);
    doc.text(`Report Subject: ${catLabel} signals on ${dateStr}`, 14, 33);
    doc.text(`Configuration: Capital Rs. ${capital.toLocaleString()}, Leverage ${leverage}x`, 14, 38);

    // Summary Section
    doc.setFontSize(14);
    doc.setTextColor(0);
    doc.text("Performance Summary", 14, 50);
    
    autoTable(doc, {
      startY: 53,
      head: [['Metric', 'Value']],
      body: [
        ['Total Signals', summary.total || 0],
        ['Targets Hit', summary.targets_hit || 0],
        ['Stop Losses', summary.sl_hits || 0],
        ['Win Rate', `${summary.win_rate || 0}%`],
        ['Total P&L', `Rs. ${summary.total_pnl?.toLocaleString("en-IN", { minimumFractionDigits: 2 })}`],
      ],
      theme: 'grid',
      headStyles: { fillColor: [16, 185, 129] },
    });

    // History Table
    doc.setFontSize(14);
    doc.text("Trade History", 14, doc.lastAutoTable.finalY + 15);

    const tableRows = data.map(s => [
      s.entry_time,
      s.symbol,
      s.type,
      s.dynamicQty,
      `Rs. ${s.entry?.toFixed(2)}`,
      `Rs. ${s.exit_price?.toFixed(2) || "-"}`,
      s.status,
      `${s.dynamicPnL >= 0 ? "+" : ""}Rs. ${s.dynamicPnL?.toFixed(2)} (${s.dynamicPnLPct}%)`
    ]);

    autoTable(doc, {
      startY: doc.lastAutoTable.finalY + 18,
      head: [['Time', 'Symbol', 'Type', 'Qty', 'Entry', 'Exit', 'Status', 'P&L']],
      body: tableRows,
      styles: { fontSize: 8 },
      headStyles: { fillColor: [31, 41, 55] }, // gray-800
      alternateRowStyles: { fillColor: [249, 250, 251] },
    });

    doc.save(`TradePulse_${category}_report_${dateStr}.pdf`);
  };

  useEffect(() => {
    fetchReport();
    const id = setInterval(fetchReport, 60000);
    return () => clearInterval(id);
  }, [selectedDate]);

  if (loading && !report) {
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center">
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-emerald-500"></div>
      </div>
    );
  }

  const {
    history = [],
    intraday_history = [],
    specialist_history = [],
    long_term_history = [],
    option_buying_history = [],
    signal_changes = [],
  } = report || {};

  // Short-Term isn't in SignalHistory at all, so it's never part of the flat
  // `history` list above — tag it with a matching `category` so it plugs into
  // the same badge/qty/pnl logic as everything else on this page.
  const short_term_history = (proReport?.short_term?.history || []).map((r) => ({
    ...r,
    category: "short_term",
  }));

  // The backend's "all categories for this date" list can't include Short-Term
  // (separate model, separate endpoint) — merge it in here so "Overview" is
  // actually all 6, not 5.
  const allHistory = [...history, ...short_term_history];

  const rawHistory =
    activeTab === "intraday" ? intraday_history :
    activeTab === "short_term" ? short_term_history :
    activeTab === "long_term" ? long_term_history :
    activeTab === "specialist" ? specialist_history :
    activeTab === "option_buying" ? option_buying_history :
    allHistory;

  const displayHistory = getProcessedData(rawHistory);
  const displaySummary = getSummary(displayHistory);

  return (
    <div className="min-h-screen bg-gray-950 p-6">
      <div className="mx-auto max-w-7xl space-y-8">
        {/* Header */}
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div>
            <Link to="/" className="text-emerald-400 hover:text-emerald-300 text-sm font-medium mb-2 inline-block">
              ← Back to Dashboard
            </Link>
            <h1 className="text-3xl font-bold text-white tracking-tight">Performance Reports</h1>
            <p className="text-gray-400 text-sm mt-1">
              Historical trading metrics • {report?.date || (selectedDate ? selectedDate : "Today")}
            </p>
          </div>
          <div className="flex items-center gap-3 w-full sm:w-auto">
            <div className="hidden sm:block">
              <NotificationBell />
            </div>
            <input
              type="date"
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
              className="px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-300 outline-none focus:border-emerald-500 transition cursor-pointer custom-date-input flex-1 sm:flex-none"
            />
            <button
              onClick={fetchReport}
              className="rounded-lg bg-gray-800 border border-gray-700 px-4 py-2 text-sm text-gray-300 hover:bg-gray-700 hover:text-white transition whitespace-nowrap"
            >
              Refresh
            </button>
          </div>
        </div>

        {/* Fetch error banner — last-good data stays on screen, this is just a nudge */}
        {fetchError && (
          <div className="rounded-xl border border-red-500/20 bg-red-500/5 px-4 py-3">
            <p className="text-xs text-red-300 flex items-center gap-1.5">
              <span className="font-semibold">⚠️ {fetchError}</span>
            </p>
          </div>
        )}

        {/* Trading Configuration */}
        <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3 bg-gray-900/50 p-6 rounded-2xl border border-gray-800/50 backdrop-blur-sm">
          <div className="space-y-2">
            <label className="text-xs font-bold text-emerald-400 uppercase tracking-widest">Initial Capital (₹)</label>
            <div className="relative">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500">₹</span>
              <input
                type="number"
                value={capital}
                onChange={(e) => setCapital(Number(e.target.value))}
                className="w-full pl-8 pr-4 py-2.5 bg-gray-800 border border-gray-700 rounded-xl text-white outline-none focus:border-emerald-500/50 transition font-mono"
                placeholder="Enter capital..."
              />
            </div>
          </div>
          <div className="space-y-2">
            <label className="text-xs font-bold text-blue-400 uppercase tracking-widest">Equity Leverage (Intraday)</label>
            <div className="flex bg-gray-800 p-1 rounded-xl border border-gray-700">
              {[1, 2, 5].map(lvl => (
                <button
                  key={lvl}
                  onClick={() => setLeverage(lvl)}
                  className={`flex-1 py-1.5 rounded-lg text-sm font-bold transition ${leverage === lvl ? 'bg-blue-500 text-white shadow-lg' : 'text-gray-400 hover:text-gray-200'}`}
                >
                  {lvl}x
                </button>
              ))}
            </div>
          </div>
          <div className="hidden lg:flex items-end pb-1 text-gray-500 text-[10px] italic">
            * Quantities for stocks are calculated as (Capital × Leverage) / Entry Price.
          </div>
        </div>

        {/* Tab Bar and Export */}
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-4">
          <div className="flex gap-2 flex-wrap">
            {[
              { key: "all", label: "Overview", count: allHistory.length },
              { key: "intraday", label: "Intraday Buy/Sell", count: intraday_history.length },
              { key: "short_term", label: "Short-Term Buy", count: short_term_history.length },
              { key: "long_term", label: "Long-Term Buy", count: long_term_history.length },
              { key: "specialist", label: "Option Selling", count: specialist_history.length },
              { key: "option_buying", label: "Option Buying", count: option_buying_history.length },
            ].map((tab) => (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                  activeTab === tab.key
                    ? "bg-emerald-500/20 text-emerald-400 ring-1 ring-emerald-500/30"
                    : "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white"
                }`}
              >
                {tab.label}
                {tab.count > 0 && (
                  <span className="ml-2 text-[10px] font-bold bg-gray-700 px-1.5 py-0.5 rounded-full">
                    {tab.count}
                  </span>
                )}
              </button>
            ))}
          </div>

          {activeTab !== "all" && (
            <button
              onClick={() => downloadPDF(activeTab)}
              className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 border border-gray-700 rounded-lg text-xs font-medium transition"
              title={`Download ${TAB_LABELS[activeTab]} PDF`}
            >
              📥 {TAB_LABELS[activeTab]} PDF
            </button>
          )}
        </div>

        {/* Stats Cards */}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
          <StatCard title="Total Signals" value={displaySummary.total || 0} icon="📊" />
          <StatCard title="Targets Hit" value={displaySummary.targets_hit || 0} icon="🎯" color="text-emerald-400" />
          <StatCard title="Stop Losses" value={displaySummary.sl_hits || 0} icon="🛑" color="text-red-400" />
          <StatCard
            title="Win Rate"
            value={`${displaySummary.win_rate || 0}%`}
            icon="🏆"
            color={displaySummary.win_rate >= 60 ? "text-emerald-400" : "text-amber-400"}
          />
          <StatCard
            title="Total P&L"
            value={`₹${(displaySummary.total_pnl || 0).toLocaleString("en-IN", { minimumFractionDigits: 2 })}`}
            icon={displaySummary.total_pnl >= 0 ? "📈" : "📉"}
            color={displaySummary.total_pnl >= 0 ? "text-emerald-400" : "text-red-400"}
          />
        </div>

        {/* Signal Changes Section */}
        {signal_changes.length > 0 && (
          <div className="rounded-2xl border border-gray-800 bg-gray-900 overflow-hidden">
            <div className="p-5 border-b border-gray-800 flex items-center gap-3">
              <span className="text-lg">🔔</span>
              <div>
                <h2 className="text-base font-semibold text-white">Signal Changes Today</h2>
                <p className="text-xs text-gray-500">Why signals changed during the session</p>
              </div>
            </div>
            <div className="divide-y divide-gray-800/50 max-h-80 overflow-y-auto">
              {signal_changes.filter(c => activeTab === 'all' || c.category === activeTab).map((c, idx) => {
                const ct = changeTypeLabels[c.change_type] || { label: c.change_type, icon: "•", color: "text-gray-400" };
                return (
                  <div key={idx} className="px-5 py-3 flex items-start gap-4 hover:bg-gray-800/20 transition-colors">
                    <span className="text-lg shrink-0 mt-0.5">{ct.icon}</span>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-bold text-white text-sm">{c.symbol}</span>
                        <span className={`text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full bg-gray-800 ${ct.color}`}>
                          {ct.label}
                        </span>
                        <span className="text-[10px] text-gray-500 font-mono ml-auto shrink-0">{c.time}</span>
                      </div>
                      <div className="mt-1 flex items-center gap-2 text-xs">
                        {c.old_value && (
                          <span className="text-gray-500 line-through">{c.old_value}</span>
                        )}
                        {c.old_value && c.new_value && <span className="text-gray-600">→</span>}
                        {c.new_value && (
                          <span className="text-gray-300">{c.new_value}</span>
                        )}
                      </div>
                      {c.reason && (
                        <p className="mt-1 text-[11px] text-gray-500 italic truncate">{c.reason}</p>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* History Table */}
        <div className="rounded-2xl border border-gray-800 bg-gray-900 overflow-hidden">
          <div className="p-6 border-b border-gray-800 flex items-center justify-between">
            <h2 className="text-lg font-semibold text-white">
              {TAB_LABELS[activeTab] || "All"} Signal History
            </h2>
            <span className="text-xs text-gray-500">
              {displayHistory.length} signal{displayHistory.length !== 1 ? "s" : ""}
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="bg-gray-800/50 text-xs uppercase text-gray-500 font-medium">
                  <th className="px-5 py-4">Signal</th>
                  <th className="px-5 py-4">Active</th>
                  <th className="px-5 py-4">Symbol</th>
                  <th className="px-5 py-4">Type</th>
                  <th className="px-5 py-4 text-right">Entry</th>
                  <th className="px-5 py-4 text-right">Target / SL</th>
                  <th className="px-5 py-4">Status</th>
                  <th className="px-5 py-4 text-right">P&L</th>
                  <th className="px-5 py-4">Exit</th>
                  <th className="px-5 py-4">Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800">
                {displayHistory.length > 0 ? (
                  displayHistory.map((s, idx) => (
                    <tr key={idx} className="hover:bg-gray-800/20 transition-colors">
                      <td className="px-5 py-4 font-mono text-gray-400 text-xs">{s.signal_time || s.entry_time || "—"}</td>
                      <td className="px-5 py-4 font-mono text-gray-400 text-xs">{s.active_time || "—"}</td>
                      <td className="px-5 py-4">
                        <span className="font-bold text-white">{s.symbol}</span>
                        <span className="ml-2 text-[9px] px-1.5 py-0.5 rounded uppercase font-bold tracking-wider bg-blue-500/10 text-blue-400">
                          NSE
                        </span>
                      </td>
                      <td className="px-5 py-4">
                        <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                          (s.type || s.strategy) === "SELL"
                            ? "bg-red-500/20 text-red-400"
                            : "bg-emerald-500/20 text-emerald-400"
                        }`}>
                          {s.type || s.strategy}
                        </span>
                      </td>
                      <td className="px-5 py-4 font-mono text-gray-300 text-right">
                        ₹{s.entry?.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        <div className="text-[10px] text-indigo-400 font-bold mt-1">Qty: {s.dynamicQty}</div>
                      </td>
                      <td className="px-5 py-4 font-mono text-xs text-right">
                        {s.category === "long_term" ? (
                          <div className="text-gray-400 italic max-w-[160px] ml-auto whitespace-normal break-words text-right">
                            {s.hold_rule || "Buy & hold — no fixed target/SL"}
                          </div>
                        ) : (
                          <>
                            <div className="text-emerald-400">T: {s.target != null ? `₹${s.target.toLocaleString("en-IN", { minimumFractionDigits: 2 })}` : "—"}</div>
                            <div className="text-red-400">S: {s.sl != null ? `₹${s.sl.toLocaleString("en-IN", { minimumFractionDigits: 2 })}` : "—"}</div>
                          </>
                        )}
                      </td>
                      <td className="px-5 py-4">
                        <StatusBadge status={s.status} />
                      </td>
                      <td className={`px-5 py-4 font-mono text-right font-bold ${
                        s.dynamicPnL > 0 ? "text-emerald-400" : s.dynamicPnL < 0 ? "text-red-400" : "text-gray-500"
                      }`}>
                        {s.status === "PENDING" || (s.status === "ACTIVE" && !s.isRunning) ? "—" : (
                          <>
                            {s.dynamicPnL > 0 ? "+" : ""}₹{s.dynamicPnL?.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
                            <span className="block text-[10px] font-normal">
                              {s.dynamicPnLPct > 0 ? "+" : ""}{s.dynamicPnLPct}%
                              {s.isRunning && <span className="text-amber-400 ml-1">(running)</span>}
                            </span>
                          </>
                        )}
                      </td>
                      <td className="px-5 py-4 font-mono text-gray-400 text-xs">
                        {s.exit_price != null ? `₹${s.exit_price.toLocaleString("en-IN", { minimumFractionDigits: 2 })}` : "—"}
                      </td>
                      <td className="px-5 py-4 text-gray-500 text-xs italic max-w-[280px] whitespace-normal break-words" title={s.exit_reason}>
                        {s.exit_reason || "—"}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan="9" className="px-6 py-12 text-center text-gray-500">
                      No signal history for today yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}

function StatCard({ title, value, icon, color = "text-white" }) {
  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-900 p-5 flex items-center gap-4 transition-transform hover:scale-[1.02]">
      <div className="text-2xl">{icon}</div>
      <div>
        <p className="text-[10px] text-gray-500 font-medium uppercase tracking-wider">{title}</p>
        <p className={`text-xl font-bold ${color}`}>{value}</p>
      </div>
    </div>
  );
}

function StatusBadge({ status }) {
  switch (status) {
    case "HIT_TARGET":
      return <span className="px-2 py-1 rounded-full bg-emerald-500/20 text-emerald-400 text-[10px] font-bold uppercase tracking-tight">🎯 Target Hit</span>;
    case "HIT_SL":
      return <span className="px-2 py-1 rounded-full bg-red-500/20 text-red-400 text-[10px] font-bold uppercase tracking-tight">🛑 SL Hit</span>;
    case "CANCELLED":
      return <span className="px-2 py-1 rounded-full bg-slate-500/20 text-slate-300 text-[10px] font-bold uppercase tracking-tight">🚫 Cancelled</span>;
    case "EXPIRED":
      return <span className="px-2 py-1 rounded-full bg-gray-500/20 text-gray-400 text-[10px] font-bold uppercase tracking-tight">⏱️ Auto-Closed</span>;
    case "ACTIVE":
      return (
        <span className="flex items-center gap-2 text-amber-500 text-[10px] font-bold uppercase tracking-tight">
          <span className="h-1.5 w-1.5 rounded-full bg-amber-500 animate-pulse"></span>
          Active
        </span>
      );
    default:
      return <span className="px-2 py-1 rounded-full bg-gray-800 text-gray-400 text-[10px] uppercase font-bold tracking-tight">{status}</span>;
  }
}
