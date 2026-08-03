import { useEffect, useState, useCallback } from "react";
import API from "../api/axios";
import useSignalNotification from "../hooks/useSignalNotification";
import { signalBadge, statusPillClass } from "./signalDisplay";

function TableSkeleton({ cols, rows = 5 }) {
  return (
    <div className="space-y-3 p-4">
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

function AnalyticsCard({ label, value, icon, color = "gray" }) {
  const colorMap = {
    emerald: "border-emerald-500/20 bg-emerald-500/5",
    amber: "border-amber-500/20 bg-amber-500/5",
    sky: "border-sky-500/20 bg-sky-500/5",
    red: "border-red-500/20 bg-red-500/5",
    violet: "border-violet-500/20 bg-violet-500/5",
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

function MetricChip({ label, value }) {
  return (
    <div className="rounded px-3 py-2 text-center min-w-24 bg-gray-800">
      <div className="text-xs font-semibold mb-0.5 text-gray-300">{label}</div>
      <div className="font-mono text-white">{value}</div>
    </div>
  );
}

function StatusPill({ status }) {
  const map = {
    ACTIVE:     { label: "Active 📈", dot: "bg-emerald-400" },
    PENDING:    { label: "Pending ⏳", dot: "bg-amber-400" },
    HIT_TARGET: { label: "Hit Target 🎯", dot: "bg-sky-400" },
    HIT_SL:     { label: "Hit SL 🛑", dot: "bg-red-400" },
    EXPIRED:    { label: "Expired ⏰", dot: "bg-violet-400" },
    CANCELLED:  { label: "Cancelled ❌", dot: "bg-gray-400" },
  };
  const s = map[status] || { label: status || "—", dot: "bg-gray-400" };
  return (
    <span className={`inline-flex items-center gap-1.5 ring-1 ring-inset px-2.5 py-1 rounded-full text-[10px] font-bold uppercase tracking-wider ${statusPillClass(status)}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${s.dot} ${status === "ACTIVE" ? "animate-pulse" : ""}`} />
      {s.label}
    </span>
  );
}

/* Status tabs. /api/stocks/live-signals/ only ever returns ACTIVE/PENDING rows
   (_live_intraday_payload() filters status__in=[ACTIVE, PENDING] — closed signals
   aren't served here at all), so HIT_TARGET/HIT_SL/EXPIRED/CANCELLED tabs would
   always show 0 and an empty state. Only include statuses this endpoint can
   actually return; add the rest back if the endpoint ever starts including history. */
const STATUS_TABS = [
  { key: "ALL",     label: "All",     icon: "📊", color: "gray" },
  { key: "PENDING", label: "Pending", icon: "⏳", color: "amber" },
  { key: "ACTIVE",  label: "Active",  icon: "📈", color: "emerald" },
];

const AI_PIPELINE = [
  { time: "9:15 AM",     label: "Market Opens",             color: "text-amber-400" },
  { time: "9:30 AM",     label: "First Scan (needs bars)",  color: "text-sky-400" },
  { time: "Every 15min", label: "Signal Scan + Update",     color: "text-indigo-400" },
  { time: "3:20 PM",     label: "Signal Cutoff",            color: "text-orange-400" },
  { time: "3:30 PM",     label: "Market Close",             color: "text-gray-500" },
];

export default function LiveSignalsTable() {
  const [data, setData]               = useState(null);
  const [loading, setLoading]         = useState(true);
  const [filter, setFilter]           = useState("ALL");
  const [statusTab, setStatusTab]     = useState("ALL");
  const [lastRefreshed, setLastRefreshed] = useState(null);
  const [error, setError]             = useState(null);
  const [livePrices, setLivePrices]   = useState({});
  const [prevPrices, setPrevPrices]   = useState({});
  const [currentPage, setCurrentPage] = useState(1);
  const itemsPerPage = 15;
  const { permission, requestPermission, notifyNewSignals } = useSignalNotification("intraday");

  const isOpen      = data?.market_status === "OPEN";
  const signals     = data?.signals ?? [];
  const filtered    = signals.filter((s) => {
    const matchesFilter = filter === "ALL" || s.signal === filter;
    const matchesStatus = statusTab === "ALL" || s.status === statusTab;
    return matchesFilter && matchesStatus;
  });

  const isMarketOpen = () => {
    const now = new Date();
    const istOffset = 5.5 * 60 * 60 * 1000; // IST is UTC+5:30
    const istTime = new Date(now.getTime() + istOffset);
    const day = istTime.getUTCDay(); // 0=Sun, 1=Mon, ..., 6=Sat
    if (day === 0 || day === 6) return false; // Weekend
    const hours = istTime.getUTCHours();
    const minutes = istTime.getUTCMinutes();
    const totalMinutes = hours * 60 + minutes;
    const openMinutes = 9 * 60 + 15; // 9:15
    const closeMinutes = 15 * 60 + 30; // 15:30
    return totalMinutes >= openMinutes && totalMinutes <= closeMinutes;
  };

  const fetchSignals = useCallback((force = false) => {
    // Only show skeleton on first load, use a more subtle indicator for background refreshes
    if (!data) setLoading(true);
    setError(null);

    const url = force === true ? "/api/stocks/live-signals/?force=true" : "/api/stocks/live-signals/";
    API.get(url)
      .then((r) => {
        setData(r.data);
        setLastRefreshed(new Date());
        setError(null);
        // Reset prices when main data changes
        setLivePrices({});
        setPrevPrices({});

        // Notify if there are signals
        if (r.data?.signals?.length > 0) {
          notifyNewSignals(r.data.signals);
        }
      })
      .catch((err) => {
        console.error("Failed to fetch signals:", err);
        // Do NOT set data to null; keep existing data if available
        setError("Failed to refresh signals. Showing last known data.");
      })
      .finally(() => setLoading(false));
  }, [notifyNewSignals]);

  useEffect(() => {
    // On dashboard load, fetch current state (not force scan - backend runs on its own 5-min cycle)
    if (isMarketOpen()) {
      fetchSignals(false);
    } else {
      setLoading(false);
    }

    // Auto-refresh: 5 minutes when market is open, 30 minutes when closed
    const intervalMs = isMarketOpen() ? 5 * 60 * 1000 : 30 * 60 * 1000;

    const id = setInterval(() => {
      fetchSignals(false);
    }, intervalMs);

    // Auto-refresh when user returns to the tab
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        fetchSignals(false);
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [fetchSignals]);

  // Reset page to 1 when filter or data changes
  useEffect(() => {
    setCurrentPage(1);
  }, [filter, statusTab, data?.market_status]);

  // High-frequency polling for prices of currently visible signals
  useEffect(() => {
    if (data?.market_status !== "OPEN" || !data?.signals || data.signals.length === 0) return;

    const pollLivePrices = () => {
      const activeSymbols = data.signals
        .filter(s => ["ACTIVE", "PENDING"].includes(s.status))
        .map(s => s.symbol)
        .join(",");
      if (!activeSymbols) return;

      const encodedSymbols = encodeURIComponent(activeSymbols);
      API.get(`/api/stocks/live-price-updates/?symbols=${encodedSymbols}`)
        .then(r => {
          if (Object.keys(r.data).length > 0) {
            setLivePrices(prev => {
              setPrevPrices(prevPricesState => ({ ...prevPricesState, ...prev }));
              return { ...prev, ...r.data };
            });
          }
        })
        .catch(err => console.debug("Live price poll error:", err));
    };

    pollLivePrices();
    const pollerId = setInterval(pollLivePrices, 1000);
    return () => clearInterval(pollerId);
  }, [data?.market_status, data?.signals]);

  // Pagination logic
  const totalPages = Math.ceil(filtered.length / itemsPerPage);
  const indexOfLastItem = currentPage * itemsPerPage;
  const indexOfFirstItem = indexOfLastItem - itemsPerPage;
  const currentItems = filtered.slice(indexOfFirstItem, indexOfLastItem);

  const fmtIST = (ts) => {
    if (!ts) return "—";
    try {
      return new Intl.DateTimeFormat("en-IN", {
        timeZone: "Asia/Kolkata",
        hour: "2-digit",
        minute: "2-digit",
        day: "2-digit",
        month: "short",
        year: "numeric",
        hour12: true,
      }).format(new Date(ts));
    } catch {
      return ts;
    }
  };

  const fmtTime = fmtIST;

  const getReason = (s) => s.reasoning_summary || s.condition || s.reason || "—";
  const getStrategy = (s) => {
    if (s.strategy_name) return s.strategy_name;
    if (typeof s.reason === "string" && s.reason.startsWith("[")) {
      const end = s.reason.indexOf("]");
      if (end > 1) return s.reason.slice(1, end);
    }
    return "Volume Profile";
  };
  const getEntryTolerance = (s) => {
    const n = Number(s.entry_tolerance);
    if (Number.isFinite(n) && n > 0) return n;
    const entry = Number(s.entry);
    return Number.isFinite(entry) ? Math.max(0.5, Math.abs(entry) * 0.0015) : 0.5;
  };

  const getTouchState = (s) => {
    const live = Number(livePrices[s.symbol]);
    const entry = Number(s.entry);
    const tolerance = getEntryTolerance(s);
    if (!Number.isFinite(entry)) return { label: "—", cls: "bg-gray-800 text-gray-400" };
    if (["HIT_TARGET", "HIT_SL", "CANCELLED", "EXPIRED"].includes(s.status)) {
      return { label: "CLOSED", cls: "bg-slate-500/10 border border-slate-500/20 text-slate-300" };
    }
    if (!Number.isFinite(live)) {
      return s.status === "ACTIVE"
        ? { label: "ACTIVE", cls: "bg-emerald-500/10 border border-emerald-500/20 text-emerald-400" }
        : { label: "WAITING", cls: "bg-yellow-500/10 border border-yellow-500/20 text-yellow-400" };
    }
    if (Math.abs(live - entry) <= tolerance) {
      return { label: "TOUCHED", cls: "bg-emerald-500/15 border border-emerald-500/30 text-emerald-400" };
    }
    return s.status === "ACTIVE"
      ? { label: "ACTIVE", cls: "bg-emerald-500/10 border border-emerald-500/20 text-emerald-400" }
      : { label: "WAITING", cls: "bg-yellow-500/10 border border-yellow-500/20 text-yellow-400" };
  };

  // Next-day mode shows volume-profile debug columns; live mode shows VWAP / VolRatio
  const isNextDay = !isOpen;

  // ── Analytics (client-side, computed from the currently loaded signal set) ──
  // NOTE: this endpoint only ever returns ACTIVE/PENDING rows (closed signals aren't
  // served here), so there's no HIT_TARGET/HIT_SL data available to compute a real
  // win rate from — avg R:R of currently live setups is shown instead.
  const totalCount   = signals.length;
  const buyCount     = signals.filter((s) => s.signal === "BUY").length;
  const sellCount    = signals.filter((s) => s.signal === "SELL").length;
  const activeCount  = signals.filter((s) => s.status === "ACTIVE").length;
  const pendingCount = signals.filter((s) => s.status === "PENDING").length;
  const rrValues     = signals.map((s) => Number(s.rr)).filter((n) => Number.isFinite(n));
  const avgRR        = rrValues.length > 0 ? (rrValues.reduce((a, b) => a + b, 0) / rrValues.length).toFixed(1) : "—";

  const sentiment = data?.sentiment ?? "SIDEWAYS";
  const sentimentCfg = {
    BULLISH:  { border: "border-emerald-500", title: "🟢 BULLISH SENTIMENT" },
    BEARISH:  { border: "border-red-500", title: "🔴 BEARISH SENTIMENT" },
    SIDEWAYS: { border: "border-yellow-500", title: "🟡 SIDEWAYS / NEUTRAL" },
  }[sentiment] ?? { border: "border-yellow-500", title: sentiment };

  return (
    <div className="space-y-6">

      {/* ── Analytics Summary Cards ── */}
      <section className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        <AnalyticsCard label="Total Signals" value={loading && !data ? "—" : totalCount} icon="📊" />
        <AnalyticsCard label="Buy" value={loading && !data ? "—" : buyCount} icon="🟢" color="emerald" />
        <AnalyticsCard label="Sell" value={loading && !data ? "—" : sellCount} icon="🔴" color="red" />
        <AnalyticsCard label="Active" value={loading && !data ? "—" : activeCount} icon="📈" color="emerald" />
        <AnalyticsCard label="Pending" value={loading && !data ? "—" : pendingCount} icon="⏳" color="amber" />
        <AnalyticsCard label="Avg R:R" value={loading && !data ? "—" : (avgRR === "—" ? "—" : `1:${avgRR}`)} icon="⚖️" color="sky" />
      </section>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-4">
        {/* Main Workspace (Left 3 cols) */}
        <div className="lg:col-span-3 space-y-6">

          {/* ── Sentiment / Market Direction ── */}
          <section>
            <h2 className="text-sm font-bold text-gray-400 uppercase tracking-wider mb-3 flex items-center gap-2">
              <span className="bg-gray-800 text-gray-400 rounded w-5 h-5 inline-flex items-center justify-center text-[10px]">1</span>
              NIFTY 50 SENTIMENT
            </h2>
            {loading && !data ? (
              <div className="h-20 animate-pulse rounded-xl bg-gray-800 px-6 py-4" />
            ) : (
              <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
                <div className={`px-6 py-4 flex flex-col md:flex-row md:items-center justify-between gap-4 border-l-4 ${sentimentCfg.border}`}>
                  <div>
                    <h3 className="text-white text-xl font-bold flex items-center gap-2">
                      {sentimentCfg.title}
                    </h3>
                    <p className="text-gray-400 text-sm mt-1">
                      Computed from NIFTY 50 15-min candles — gates the mean-reversion
                      (Value Area Rejection) trigger only; POC Flip and VA Breakout trade both directions regardless.
                    </p>
                  </div>
                  <div className="flex gap-3">
                    <MetricChip label="Buy Signals" value={buyCount} />
                    <MetricChip label="Sell Signals" value={sellCount} />
                    <MetricChip label="Universe" value="Nifty 100" />
                  </div>
                </div>
              </div>
            )}
          </section>

          {/* ── Info banner when market is closed ── */}
          {!loading && !isOpen && (
            <div className="rounded-xl border border-yellow-500/20 bg-yellow-500/5 px-4 py-3">
              <p className="text-xs text-yellow-300">
                <span className="font-semibold">Market is closed.</span> Showing next-session outlook
                based on the volume-profile engine: POC, VAH, VAL, VWAP, ATR, plus volume and momentum
                filters across the Nifty 100 universe. Signals are indicative — confirm at market open.
              </p>
            </div>
          )}

          {/* ── Tabs + Table ── */}
          <section>
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-bold text-gray-400 uppercase tracking-wider flex items-center gap-2">
                <span className="bg-gray-800 text-gray-400 rounded w-5 h-5 inline-flex items-center justify-center text-[10px]">2</span>
                TRADE SIGNALS
              </h2>
              <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-xl p-1 w-fit">
                {["ALL", "BUY", "SELL"].map((f) => (
                  <button
                    key={f}
                    onClick={() => setFilter(f)}
                    className={`px-3 py-1.5 rounded-lg text-xs font-bold transition-all ${
                      filter === f ? "bg-gray-800 text-white shadow-lg" : "text-gray-500 hover:text-gray-300 hover:bg-gray-800/50"
                    }`}
                  >
                    {f}
                  </button>
                ))}
              </div>
            </div>

            {/* ── Fetch error banner ── */}
            {error && (
              <div className="rounded-xl border border-red-500/20 bg-red-500/5 px-4 py-3 mb-4">
                <p className="text-xs text-red-300 flex items-center gap-1.5">
                  <span className="font-semibold">⚠️ {error}</span>
                </p>
              </div>
            )}

            {/* Status Tab Bar */}
            <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-xl p-1 mb-4 overflow-x-auto">
              {STATUS_TABS.map((tab) => {
                const count = tab.key === "ALL" ? signals.length : signals.filter((s) => s.status === tab.key).length;
                const isActive = statusTab === tab.key;
                return (
                  <button
                    key={tab.key}
                    onClick={() => setStatusTab(tab.key)}
                    className={`flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-bold transition-all whitespace-nowrap ${
                      isActive ? "bg-gray-800 text-white shadow-lg" : "text-gray-500 hover:text-gray-300 hover:bg-gray-800/50"
                    }`}
                  >
                    <span>{tab.icon}</span>
                    {tab.label}
                    {count > 0 && (
                      <span className={`ml-1 px-1.5 py-0.5 rounded-full text-[10px] font-bold ${
                        isActive ? `bg-${tab.color}-500/20 text-${tab.color}-400` : "bg-gray-700 text-gray-400"
                      }`}>
                        {count}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>

            {/* Table Card */}
            <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
              {loading ? (
                <TableSkeleton cols={9} />
              ) : filtered.length === 0 ? (
                <div className="p-12 text-center">
                  <div className="text-3xl mb-3 opacity-50">📊</div>
                  <p className="text-gray-500 text-sm">No signals found for current filter.</p>
                  <p className="mt-1 text-xs text-gray-600">
                    {isOpen
                      ? "Low volatility or no breakout confirmed right now."
                      : "No clear trend setups in Nifty 100 for next session."}
                  </p>
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm whitespace-nowrap">
                    <thead>
                      <tr className="border-b border-gray-800 text-xs text-gray-500 bg-gray-900/50">
                        <th className="font-semibold p-4">Stock</th>
                        <th className="font-semibold p-4">Strategy</th>
                        <th className="font-semibold p-4">Status</th>
                        <th className="font-semibold p-4">Touch</th>
                        <th className="font-semibold p-4 text-right">{isNextDay ? "Last Close" : "Price"}</th>
                        <th className="font-semibold p-4 text-right">Entry</th>
                        <th className="font-semibold p-4 text-right">SL / Target</th>
                        <th className="font-semibold p-4 text-right">R:R</th>
                        <th className="font-semibold p-4 text-right">Vol / VWAP / ATR</th>
                        <th className="font-semibold p-4">Reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {currentItems.map((s, idx) => {
                        const touch = getTouchState(s);
                        return (
                          <tr key={`${s.symbol}-${idx}`} className="border-b border-gray-800/50 last:border-0 hover:bg-gray-800/20 transition-colors">
                            <td className="p-4">
                              <div className="flex items-center gap-2">
                                <span className="font-bold text-indigo-300 text-sm">{s.symbol}</span>
                                <span className={`rounded-md px-2 py-0.5 text-[10px] font-bold ${signalBadge[s.signal] ?? "bg-gray-700 text-gray-300"}`}>
                                  {s.signal === "BUY" ? "▲ BUY" : "▼ SELL"}
                                </span>
                              </div>
                              {s.signal_time && (
                                <div className="text-[10px] text-gray-500 mt-0.5 font-mono">Gen: {fmtIST(s.signal_time)}</div>
                              )}
                            </td>
                            <td className="p-4">
                              <span className="inline-flex rounded-md bg-indigo-500/10 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-indigo-300 ring-1 ring-indigo-500/20">
                                {getStrategy(s)}
                              </span>
                            </td>
                            <td className="p-4"><StatusPill status={s.status} /></td>
                            <td className="p-4">
                              <div className={`inline-flex items-center rounded-md px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ${touch.cls}`}>
                                {touch.label}
                              </div>
                            </td>
                            <td className={`p-4 text-right font-mono transition-all duration-300 ${
                              isNextDay
                                ? "text-gray-200"
                                : livePrices[s.symbol] == null
                                  ? "text-gray-400"
                                  : livePrices[s.symbol] > (prevPrices[s.symbol] ?? livePrices[s.symbol]) ? "text-emerald-400 font-bold"
                                  : livePrices[s.symbol] < (prevPrices[s.symbol] ?? livePrices[s.symbol]) ? "text-red-400 font-bold"
                                  : "text-gray-200 font-bold"
                            }`}>
                              {isNextDay
                                ? `₹${(s.price ?? 0).toFixed(2)}`
                                : (livePrices[s.symbol] != null ? `₹${livePrices[s.symbol].toFixed(2)}` : "—")}
                            </td>
                            <td className="p-4 text-right font-mono text-gray-300">₹{s.entry?.toFixed(2)}</td>
                            <td className="p-4 text-right font-mono text-xs">
                              <div className="text-red-400 font-semibold">SL: ₹{s.stop_loss?.toFixed(2)}</div>
                              <div className="text-emerald-400 mt-0.5">T: ₹{s.target?.toFixed(2)}</div>
                            </td>
                            <td className="p-4 text-right font-mono text-gray-300">1:{s.rr?.toFixed(1)}</td>
                            <td className="p-4 text-right font-mono text-xs">
                              <div className={s.vol_ratio >= 2 ? "text-emerald-400" : "text-gray-300"}>
                                {s.vol_ratio != null ? `${s.vol_ratio.toFixed(1)}x` : "—"}
                              </div>
                              <div className="text-gray-400 mt-0.5">{s.vwap != null ? `₹${s.vwap.toFixed(2)}` : "—"}</div>
                              <div className="text-gray-500 mt-0.5">{s.atr != null ? `₹${s.atr.toFixed(2)}` : "—"}</div>
                            </td>
                            <td className="p-4 max-w-[220px] truncate text-gray-400 text-[11px] group relative">
                              <span className="truncate block pr-6" title={s.reason}>{getReason(s)}</span>
                              <div className="mt-1 text-[10px] text-gray-500">
                                Entry band: {s.entry_band ?? `±₹${getEntryTolerance(s).toFixed(2)}`}
                              </div>
                              {s.active_time && (
                                <div className="text-[10px] text-gray-500">Active: {fmtIST(s.active_time)}</div>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>

                  {/* Pagination controls */}
                  {totalPages > 1 && (
                    <div className="flex items-center justify-between border-t border-gray-800 px-4 py-3">
                      <span className="text-xs text-gray-400">
                        Showing {indexOfFirstItem + 1} to {Math.min(indexOfLastItem, filtered.length)} of {filtered.length} entries
                      </span>
                      <div className="flex gap-2">
                        <button
                          onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
                          disabled={currentPage === 1}
                          className="rounded-lg bg-gray-800 px-3 py-1.5 text-xs font-medium text-gray-300 transition hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          Previous
                        </button>
                        <div className="flex items-center gap-1">
                          {Array.from({ length: totalPages }, (_, i) => i + 1)
                            .filter(page => page === 1 || page === totalPages || (page >= currentPage - 1 && page <= currentPage + 1))
                            .map((page, i, arr) => {
                              const isGap = i > 0 && page - arr[i - 1] > 1;
                              const btn = (
                                <button
                                  key={page}
                                  onClick={() => setCurrentPage(page)}
                                  className={`h-7 w-7 rounded-lg text-xs font-medium transition ${
                                    currentPage === page
                                      ? "bg-emerald-600 text-white"
                                      : "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white"
                                  }`}
                                >
                                  {page}
                                </button>
                              );
                              return isGap ? [
                                <span key={`gap-${page}`} className="text-gray-500 text-xs px-1">...</span>,
                                btn
                              ] : btn;
                            })}
                        </div>
                        <button
                          onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
                          disabled={currentPage === totalPages}
                          className="rounded-lg bg-gray-800 px-3 py-1.5 text-xs font-medium text-gray-300 transition hover:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          Next
                        </button>
                      </div>
                    </div>
                  )}

                  {/* Legend for next-day mode */}
                  {isNextDay && (
                    <div className="flex flex-wrap gap-4 border-t border-gray-800 px-4 py-3 text-xs text-gray-500">
                      <span><span className="text-emerald-300 font-mono">Vol</span> — volume surge ratio</span>
                      <span><span className="text-blue-300 font-mono">VWAP</span> — volume weighted average price</span>
                      <span><span className="text-purple-300 font-mono">ATR</span> — average true range</span>
                      <span>Setup is based on POC / VAH / VAL volume-profile logic</span>
                    </div>
                  )}
                </div>
              )}
            </div>
          </section>
        </div>

        {/* Right Sidebar */}
        <div className="lg:col-span-1 space-y-5">

          {/* Scan Info */}
          <div className="rounded-xl border border-indigo-500/30 bg-indigo-500/5 p-5">
            <h3 className="text-sm font-bold text-indigo-400 uppercase tracking-wider mb-4 flex items-center gap-2">
              📡 Scan Info
            </h3>
            <div className="space-y-3 text-xs">
              <div className="flex justify-between items-center">
                <span className="text-gray-400">Market</span>
                <span className={`font-semibold ${isOpen ? "text-emerald-400" : "text-yellow-400"}`}>
                  {loading && !data ? "Checking..." : (isOpen ? "🟢 Open" : "🟡 Closed")}
                </span>
              </div>
              {data?.scanned != null && (
                <div className="flex justify-between items-center">
                  <span className="text-gray-400">Scanned</span>
                  <span className="font-mono text-gray-200">{data.scanned} stocks</span>
                </div>
              )}
              {data?.timestamp && (
                <div className="flex justify-between items-center">
                  <span className="text-gray-400">Updated</span>
                  <span className="font-mono text-gray-200">{fmtTime(data.timestamp)}</span>
                </div>
              )}
              {lastRefreshed && (
                <div className="flex justify-between items-center">
                  <span className="text-gray-400">Last Refreshed</span>
                  <span className="font-mono text-gray-200">{fmtTime(lastRefreshed)}</span>
                </div>
              )}
              <div className="pt-3 border-t border-gray-800/50 flex flex-col gap-2">
                <button
                  onClick={requestPermission}
                  className={`flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-xs font-semibold transition ${
                    permission === "granted"
                      ? "bg-emerald-500/20 text-emerald-400"
                      : "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white"
                  }`}
                >
                  🔔 {permission === "granted" ? "Alerts On" : "Alerts Off"}
                </button>
                <button
                  onClick={() => fetchSignals(true)}
                  disabled={loading}
                  className="flex items-center justify-center gap-1.5 rounded-lg bg-gray-800 px-3 py-2 text-xs font-semibold text-gray-300 transition hover:bg-gray-700 hover:text-white disabled:opacity-50"
                  title="Force scan live intraday signals"
                >
                  <span className={loading ? "animate-spin" : ""}>⚡</span> Force Scan
                </button>
              </div>
            </div>
          </div>

          {/* Pipeline Flow */}
          <div className="rounded-xl border border-gray-800 bg-gray-900 p-5">
            <h3 className="text-xs font-bold text-gray-400 uppercase tracking-wider mb-4">🧠 Scan Schedule</h3>
            <div className="flex flex-col gap-1 text-xs font-medium text-gray-300">
              {AI_PIPELINE.map((step, i) => (
                <div key={i} className="flex items-center gap-3 py-1.5">
                  <span className={`text-[10px] font-mono ${step.color} w-20 text-right`}>{step.time}</span>
                  <span className="w-1.5 h-1.5 rounded-full bg-gray-600" />
                  <span className="bg-gray-800 px-2.5 py-1 rounded text-gray-300">{step.label}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Strategy Rules */}
          <div className="rounded-xl border border-red-500/20 bg-red-500/5 p-5">
            <h3 className="text-sm font-bold text-red-400 uppercase tracking-wider mb-4">⚠️ Strategy Rules</h3>
            <ul className="text-xs text-gray-300 space-y-2.5">
              <li className="flex gap-2"><span>🎯</span> POC Flip — vol_ratio &gt; 1.2</li>
              <li className="flex gap-2"><span>🎯</span> VA Breakout — vol_ratio &gt; 1.5</li>
              <li className="flex gap-2"><span>🎯</span> VA Rejection — mean-reversion, gated by sentiment</li>
              <li className="flex gap-2"><span>💰</span> Cost gate — target &gt; 3× round-trip cost</li>
              <li className="flex gap-2"><span>❌</span> Max 5 signals per scan</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
