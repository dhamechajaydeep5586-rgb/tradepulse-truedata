import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import API from "../api/axios";
import NotificationBell from "../components/NotificationBell";
import SignalPreviewCard from "../components/SignalPreviewCard";
import { signalBadge, statusPillClass } from "../components/signalDisplay";

function PreviewRowLine({ left, right, sub }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold text-white">{left}</div>
        {sub && <div className="truncate text-[11px] text-gray-500">{sub}</div>}
      </div>
      <div className="shrink-0">{right}</div>
    </div>
  );
}

export default function Dashboard() {
  const { logout } = useAuth();
  const [selectedDate, setSelectedDate] = useState("");
  const [summary, setSummary] = useState(null);
  const [summaryLoading, setSummaryLoading] = useState(true);

  useEffect(() => {
    API.get("/api/stocks/dashboard-summary/")
      .then((res) => setSummary(res.data))
      .catch((e) => console.error("Error fetching dashboard summary:", e))
      .finally(() => setSummaryLoading(false));
  }, []);

  const intraday = summary?.intraday || { count: 0, items: [] };
  const shortTerm = summary?.short_term || { count: 0, items: [] };
  const longTerm = summary?.long_term || { count: 0, items: [] };
  const optionSelling = summary?.option_selling || { count: 0, items: [] };
  const optionBuying = summary?.option_buying || { count: 0, items: [] };

  return (
    <div className="min-h-screen max-w-[100vw] overflow-x-hidden bg-gray-950">
      {/* Header */}
      <header className="sticky top-0 z-20 border-b border-gray-800 bg-gray-950/90 backdrop-blur-md">
        <div className="mx-auto max-w-7xl px-4 py-3 sm:px-6">
          <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            {/* Logo Row */}
            <div className="flex items-center justify-between">
              <h1 className="text-xl font-bold tracking-tight text-white">
                TradePulse <span className="text-emerald-400">AI</span>
              </h1>
              <div className="flex items-center gap-3 md:hidden">
                <NotificationBell />
                <button
                  onClick={logout}
                  className="rounded-lg bg-gray-800 p-2 text-gray-300 transition hover:bg-gray-700 hover:text-white border border-gray-700"
                  title="Logout"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M3 3a1 1 0 00-1 1v12a1 1 0 102 0V4a1 1 0 00-1-1zm10.293 9.293a1 1 0 001.414 1.414l3-3a1 1 0 000-1.414l-3-3a1 1 0 10-1.414 1.414L14.586 9H7a1 1 0 100 2h7.586l-1.293 1.293z" clipRule="evenodd" />
                  </svg>
                </button>
              </div>
            </div>

            {/* Actions Row */}
            <div className="flex flex-wrap items-center gap-2 sm:gap-3">
              <div className="flex flex-1 items-center gap-2 md:flex-none">
                <input
                  type="date"
                  value={selectedDate}
                  onChange={(e) => setSelectedDate(e.target.value)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-1.5 text-sm text-gray-200 outline-none transition focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500 md:w-auto"
                />
                {selectedDate && (
                  <button
                    onClick={() => setSelectedDate("")}
                    className="rounded-lg bg-gray-800 px-3 py-1.5 text-xs text-gray-400 transition hover:bg-gray-700 hover:text-white"
                  >
                    Clear
                  </button>
                )}
              </div>

              <div className="flex w-full items-center gap-2 sm:w-auto">
                <Link
                  to="/reports"
                  className="flex-1 rounded-lg bg-emerald-600/20 px-4 py-1.5 text-center text-xs font-medium text-emerald-400 transition hover:bg-emerald-600/30 border border-emerald-500/20 sm:text-sm"
                >
                  Reports
                </Link>
              </div>

              <div className="hidden items-center gap-3 md:flex">
                <NotificationBell />
                <button
                  onClick={logout}
                  className="rounded-lg bg-gray-800 px-4 py-1.5 text-sm text-gray-300 transition hover:bg-gray-700 hover:text-white border border-gray-700"
                >
                  Logout
                </button>
              </div>
            </div>
          </div>
        </div>
      </header>

      {/* Content */}
      <main className="mx-auto max-w-7xl space-y-8 px-4 py-6 sm:px-6">
        {/* ── Signal Category Previews ── */}
        <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3">
          <SignalPreviewCard
            title="Intraday Buy/Sell"
            icon="⚡"
            loading={summaryLoading}
            count={intraday.count}
            items={intraday.items}
            viewAllTo="/live-signals"
            emptyLabel="No active intraday signals"
            renderRow={(s) => (
              <PreviewRowLine
                left={s.symbol}
                sub={`Entry ₹${s.entry_price ?? "—"} · SL ₹${s.stop_loss ?? "—"} · T ₹${s.target ?? "—"}`}
                right={
                  <span className={`rounded-md px-2 py-0.5 text-[11px] font-bold ${signalBadge[s.signal] ?? "bg-gray-700 text-gray-300"}`}>
                    {s.signal}
                  </span>
                }
              />
            )}
          />

          <SignalPreviewCard
            title="Short-Term Buy"
            icon="📈"
            loading={summaryLoading}
            count={shortTerm.count}
            items={shortTerm.items}
            viewAllTo="/pro-system?view=short_term"
            emptyLabel="No active short-term signals"
            renderRow={(s) => (
              <PreviewRowLine
                left={s.symbol}
                sub={`${s.setup || "—"} · Entry ₹${s.entry_price ?? "—"}`}
                right={
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase ${statusPillClass(s.status)}`}>
                    {s.status}
                  </span>
                }
              />
            )}
          />

          <SignalPreviewCard
            title="Long-Term Buy"
            icon="🏛️"
            loading={summaryLoading}
            count={longTerm.count}
            items={longTerm.items}
            viewAllTo="/pro-system?view=long_term"
            emptyLabel="No active long-term signals"
            renderRow={(s) => (
              <PreviewRowLine
                left={s.symbol}
                sub={s.reason || "—"}
                right={
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase ${statusPillClass(s.status)}`}>
                    {s.status}
                  </span>
                }
              />
            )}
          />

          <SignalPreviewCard
            title="Option Selling"
            icon="📉"
            loading={summaryLoading}
            count={optionSelling.count}
            items={optionSelling.items}
            viewAllTo="/option-selling"
            emptyLabel="No active strangle positions"
            renderRow={(s) => (
              <PreviewRowLine
                left={s.symbol}
                sub={`${s.exchange || "—"} · ${s.legs_count} leg${s.legs_count === 1 ? "" : "s"} · ₹${s.entry_premium ?? "—"}`}
                right={
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase ${statusPillClass(s.status)}`}>
                    {s.status}
                  </span>
                }
              />
            )}
          />

          <SignalPreviewCard
            title="Option Buying"
            icon="🎯"
            loading={summaryLoading}
            count={optionBuying.count}
            items={optionBuying.items}
            viewAllTo="/option-buying"
            emptyLabel="No active option-buying signals"
            renderRow={(s) => (
              <PreviewRowLine
                left={`${s.symbol} ${s.option_type || ""}`}
                sub={`Strike ${s.strike_price ?? "—"} · Entry ₹${s.entry_price ?? "—"} · Now ₹${s.current_premium ?? "—"}`}
                right={
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase ${statusPillClass(s.status)}`}>
                    {s.status}
                  </span>
                }
              />
            )}
          />

        </div>
      </main>
    </div>
  );
}
