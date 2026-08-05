import { useEffect, useState, useCallback, useRef } from "react";
import API from "../api/axios";
import { statusPillClass } from "./signalDisplay";

const optionTypeBadge = {
  CE: "bg-emerald-500/15 text-emerald-400 ring-1 ring-emerald-500/30",
  PE: "bg-red-500/15 text-red-400 ring-1 ring-red-500/30",
};

function TableSkeleton({ rows = 5 }) {
  return (
    <div className="space-y-3 p-4">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex gap-3">
          {Array.from({ length: 8 }).map((_, i) => (
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

function StatusPill({ status }) {
  const map = {
    ACTIVE:  { label: "Active 📈", dot: "bg-emerald-400" },
    PENDING: { label: "Pending ⏳", dot: "bg-amber-400" },
  };
  const s = map[status] || { label: status || "—", dot: "bg-gray-400" };
  return (
    <span className={`inline-flex items-center gap-1.5 ring-1 ring-inset px-2.5 py-1 rounded-full text-[10px] font-bold uppercase tracking-wider ${statusPillClass(status)}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${s.dot} ${status === "ACTIVE" ? "animate-pulse" : ""}`} />
      {s.label}
    </span>
  );
}

/* /api/stocks/option-buying/ only ever returns ACTIVE/PENDING rows (same as
   intraday's live endpoint) — no closed-signal history is served here. */
const STATUS_TABS = [
  { key: "ALL",     label: "All",     icon: "📊", color: "gray" },
  { key: "PENDING", label: "Pending", icon: "⏳", color: "amber" },
  { key: "ACTIVE",  label: "Active",  icon: "📈", color: "emerald" },
];

const AI_PIPELINE = [
  { time: "9:00 AM",     label: "Scan Window Opens",   color: "text-amber-400" },
  { time: "Every 15min", label: "Signal Scan + Update", color: "text-indigo-400" },
  { time: "2:30 PM",     label: "Hard Time-Stop",       color: "text-red-400" },
  { time: "After 2:30",  label: "Audit-Only (no new signals)", color: "text-gray-500" },
];

export default function OptionBuyingTable() {
  const [data, setData]           = useState(null);
  const [loading, setLoading]     = useState(true);
  const [filter, setFilter]       = useState("ALL");
  const [statusTab, setStatusTab] = useState("ALL");
  const [error, setError]         = useState(null);
  // Read inside the interval tick handler (not the effect) so a tab left open
  // across the 3:30 PM close always sees the current market state instead of
  // the value captured when the effect first ran.
  const isOpenRef = useRef(false);
  const lastFetchAtRef = useRef(0);
  // Audit fix M17: no request-sequence guard existed on this poller — an
  // out-of-order response under variable latency (e.g. a slow Force Scan
  // response arriving after a subsequent plain poll's fast response) could
  // overwrite fresher state with stale data. requestSeqRef increments per
  // issued request; a response only applies if it's still the latest one.
  // Same pattern as LiveSignalsTable.jsx / DeltaHedgePanel.jsx / OptionChainTable.jsx.
  const requestSeqRef = useRef(0);

  const marketStatus = data?.market_status || "CLOSED";
  const isOpen = marketStatus === "OPEN";
  const signals = data?.signals ?? [];
  const filtered = signals.filter((s) => {
    const matchesFilter = filter === "ALL" || s.option_type === filter;
    const matchesStatus = statusTab === "ALL" || s.status === statusTab;
    return matchesFilter && matchesStatus;
  });

  const fetchData = useCallback((force = false) => {
    setError(null);
    if (force) setLoading(true);
    const mySeq = ++requestSeqRef.current;
    const url = force ? "/api/stocks/option-buying/?force=true" : "/api/stocks/option-buying/";
    API.get(url)
      .then((res) => {
        if (mySeq !== requestSeqRef.current) return; // a newer request already superseded this one
        setData(res.data);
        setError(null);
        isOpenRef.current = res.data?.market_status === "OPEN";
      })
      .catch((e) => {
        if (mySeq !== requestSeqRef.current) return;
        console.error("Error fetching option-buying signals:", e);
        setError("Failed to refresh option-buying signals. Showing last known data.");
      })
      .finally(() => {
        if (mySeq === requestSeqRef.current) setLoading(false);
      });
  }, []);

  useEffect(() => {
    // Single fixed-cadence (5 min) poll, always running — no more picking an
    // interval once at mount based on isOpen, which used to freeze a tab left
    // open across the 3:30 PM close at the 5-min "open" cadence forever. The
    // tick handler below re-checks live market state (via isOpenRef, updated
    // on every successful fetch) every time it fires instead.
    const CHECK_MS = 5 * 60 * 1000;
    const CLOSED_POLL_MS = 30 * 60 * 1000;
    lastFetchAtRef.current = Date.now();
    fetchData();
    const interval = setInterval(() => {
      const now = Date.now();
      if (isOpenRef.current || now - lastFetchAtRef.current >= CLOSED_POLL_MS) {
        lastFetchAtRef.current = now;
        fetchData();
      }
    }, CHECK_MS);
    return () => clearInterval(interval);
  }, [fetchData]);

  const fmtIST = (ts) => {
    if (!ts) return "—";
    try {
      return new Intl.DateTimeFormat("en-IN", {
        timeZone: "Asia/Kolkata",
        hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short", year: "numeric", hour12: true,
      }).format(new Date(ts));
    } catch {
      return ts;
    }
  };

  const totalCount = signals.length;
  const ceCount     = signals.filter((s) => s.option_type === "CE").length;
  const peCount     = signals.filter((s) => s.option_type === "PE").length;
  const activeCount = signals.filter((s) => s.status === "ACTIVE").length;
  const pendingCount = signals.filter((s) => s.status === "PENDING").length;

  const now = new Date();
  const istOffset = 5.5 * 60 * 60 * 1000;
  const istHM = new Date(now.getTime() + istOffset);
  const pastTimeStop = (istHM.getUTCHours() * 60 + istHM.getUTCMinutes()) >= (14 * 60 + 30);

  return (
    <div className="space-y-6">

      {/* ── Analytics Summary Cards ── */}
      <section className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        <AnalyticsCard label="Total Signals" value={loading && !data ? "—" : totalCount} icon="📊" />
        <AnalyticsCard label="CE (Call)" value={loading && !data ? "—" : ceCount} icon="🟢" color="emerald" />
        <AnalyticsCard label="PE (Put)" value={loading && !data ? "—" : peCount} icon="🔴" color="red" />
        <AnalyticsCard label="Active" value={loading && !data ? "—" : activeCount} icon="📈" color="emerald" />
        <AnalyticsCard label="Pending" value={loading && !data ? "—" : pendingCount} icon="⏳" color="amber" />
      </section>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-4">
        {/* Main Workspace (Left 3 cols) */}
        <div className="lg:col-span-3 space-y-6">

          {/* ── Time Window Status ── */}
          <section>
            <h2 className="text-sm font-bold text-gray-400 uppercase tracking-wider mb-3 flex items-center gap-2">
              <span className="bg-gray-800 text-gray-400 rounded w-5 h-5 inline-flex items-center justify-center text-[10px]">1</span>
              GENERATION WINDOW
            </h2>
            {loading && !data ? (
              <div className="h-20 animate-pulse rounded-xl bg-gray-800 px-6 py-4" />
            ) : (
              <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden">
                <div className={`px-6 py-4 flex flex-col md:flex-row md:items-center justify-between gap-4 border-l-4 ${
                  !isOpen ? "border-yellow-500" : pastTimeStop ? "border-red-500" : "border-emerald-500"
                }`}>
                  <div>
                    <h3 className="text-white text-xl font-bold flex items-center gap-2">
                      {!isOpen ? "🟡 MARKET CLOSED" : pastTimeStop ? "🔴 PAST TIME-STOP — AUDIT ONLY" : "🟢 GENERATING"}
                    </h3>
                    <p className="text-gray-400 text-sm mt-1">
                      Buys near-ATM CE/PE (delta 0.40–0.60) on confirmed VA breakout + VWAP + ADX&gt;20.
                      New signals stop at 2:30 PM IST — decaying premium leaves no time to work later than that.
                    </p>
                  </div>
                  <div className="flex gap-3">
                    <div className="rounded px-3 py-2 text-center min-w-24 bg-gray-800">
                      <div className="text-xs font-semibold mb-0.5 text-gray-300">CE Signals</div>
                      <div className="font-mono text-white">{ceCount}</div>
                    </div>
                    <div className="rounded px-3 py-2 text-center min-w-24 bg-gray-800">
                      <div className="text-xs font-semibold mb-0.5 text-gray-300">PE Signals</div>
                      <div className="font-mono text-white">{peCount}</div>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </section>

          {/* ── Tabs + Table ── */}
          <section>
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-bold text-gray-400 uppercase tracking-wider flex items-center gap-2">
                <span className="bg-gray-800 text-gray-400 rounded w-5 h-5 inline-flex items-center justify-center text-[10px]">2</span>
                OPTION SIGNALS
              </h2>
              <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-xl p-1 w-fit">
                {["ALL", "CE", "PE"].map((f) => (
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
                <TableSkeleton />
              ) : filtered.length === 0 ? (
                <div className="p-12 text-center">
                  <div className="text-3xl mb-3 opacity-50">🎯</div>
                  <p className="text-gray-500 text-sm">No option-buying signals found for current filter.</p>
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm whitespace-nowrap">
                    <thead>
                      <tr className="border-b border-gray-800 text-xs text-gray-500 bg-gray-900/50">
                        <th className="font-semibold p-4">Stock</th>
                        <th className="font-semibold p-4">Type</th>
                        <th className="font-semibold p-4 text-right">Strike</th>
                        <th className="font-semibold p-4">Status</th>
                        <th className="font-semibold p-4 text-right">Entry</th>
                        <th className="font-semibold p-4 text-right">Current</th>
                        <th className="font-semibold p-4 text-right">SL / Target</th>
                        <th className="font-semibold p-4">Expiry</th>
                        <th className="font-semibold p-4">Generated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filtered.map((s, i) => (
                        <tr key={s.id || i} className="border-b border-gray-800/50 hover:bg-gray-800/20 transition-colors">
                          <td className="p-4 font-bold text-indigo-300">{s.symbol}</td>
                          <td className="p-4">
                            <span className={`rounded-md px-2 py-0.5 text-[11px] font-bold ${optionTypeBadge[s.option_type] ?? "bg-gray-700 text-gray-300"}`}>
                              {s.option_type}
                            </span>
                          </td>
                          <td className="p-4 text-right font-mono text-gray-300">{s.strike_price}</td>
                          <td className="p-4"><StatusPill status={s.status} /></td>
                          <td className="p-4 text-right font-mono text-gray-300">₹{s.entry}</td>
                          <td className="p-4 text-right font-mono text-white font-bold">₹{s.current_premium ?? s.entry}</td>
                          <td className="p-4 text-right font-mono text-xs">
                            <div className="text-red-400 font-semibold">SL: ₹{s.stop_loss}</div>
                            <div className="text-emerald-400 mt-0.5">T: ₹{s.target}</div>
                          </td>
                          <td className="p-4 text-xs text-gray-400 font-mono">{s.expiry ?? "—"}</td>
                          <td className="p-4 text-xs text-gray-500 font-mono">{fmtIST(s.generated_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
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
                  <span className="font-mono text-gray-200">{fmtIST(data.timestamp)}</span>
                </div>
              )}
              <p className="pt-3 border-t border-gray-800/50 text-[10px] text-gray-500 leading-tight">
                Generation normally only happens on the backend's own 15-min scheduler.
                Force Scan bypasses that for testing (still subject to market hours, the
                2:30 PM time-stop, and a rate limit).
              </p>
              <button
                onClick={() => fetchData(true)}
                disabled={loading}
                className="w-full flex items-center justify-center gap-1.5 rounded-lg bg-gray-800 px-3 py-2 text-xs font-semibold text-gray-300 transition hover:bg-gray-700 hover:text-white disabled:opacity-50"
                title="Force scan option-buying signals"
              >
                <span className={loading ? "animate-spin" : ""}>⚡</span> Force Scan
              </button>
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
              <li className="flex gap-2"><span>🎯</span> BUY_CE — VAH breakout, VWAP+ADX&gt;20 confirmed</li>
              <li className="flex gap-2"><span>🎯</span> BUY_PE — VAL breakdown, VWAP+ADX&gt;20 confirmed</li>
              <li className="flex gap-2"><span>📐</span> Strike must land in 0.40–0.60 delta band</li>
              <li className="flex gap-2"><span>💰</span> Target 1.6×–2.0× premium (scales with ADX), SL 0.625×</li>
              <li className="flex gap-2"><span>❌</span> Max 3 signals per scan</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
