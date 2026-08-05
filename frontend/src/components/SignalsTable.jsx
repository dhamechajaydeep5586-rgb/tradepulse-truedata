import { useEffect, useState, useRef } from "react";
import API from "../api/axios";

const typeBadge = {
  BUY: "bg-emerald-500/15 text-emerald-400",
  SELL: "bg-red-500/15 text-red-400",
  HOLD: "bg-yellow-500/15 text-yellow-400",
};

export default function SignalsTable({ date }) {
  const [signals, setSignals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");
  const [currentPage, setCurrentPage] = useState(1);
  const itemsPerPage = 15;
  // Audit fix M17-style: no request-sequence guard existed here — a filter/date
  // change firing a new request while the 15-min poll's older request for a
  // DIFFERENT filter/date was still in flight could let that stale response land
  // last and overwrite the table with data for the wrong filter. Same pattern as
  // LiveSignalsTable.jsx / DeltaHedgePanel.jsx / OptionChainTable.jsx /
  // OptionBuyingTable.jsx.
  const requestSeqRef = useRef(0);

  useEffect(() => {
    const fetch = () => {
      setLoading(true);
      const mySeq = ++requestSeqRef.current;
      const params = {};
      if (filter) params.signal_type = filter;
      if (date) params.date = date;
      API.get("/api/stocks/intraday-signals/", { params })
        .then((r) => {
          if (mySeq !== requestSeqRef.current) return; // a newer request already superseded this one
          setSignals(r.data.results ?? r.data);
        })
        .catch(() => {
          if (mySeq !== requestSeqRef.current) return;
          setSignals([]);
        })
        .finally(() => {
          if (mySeq === requestSeqRef.current) setLoading(false);
        });
    };

    fetch();
    const id = setInterval(fetch, 900000); // 15 minutes
    return () => clearInterval(id);
  }, [filter, date]);

  // Reset page on filter change
  useEffect(() => {
    setCurrentPage(1);
  }, [filter, date]);

  // Pagination logic
  const filtered = filter ? signals.filter(s => s.signal_type === filter) : signals;
  const totalPages = Math.ceil(filtered.length / itemsPerPage);
  const indexOfLastItem = currentPage * itemsPerPage;
  const indexOfFirstItem = indexOfLastItem - itemsPerPage;
  const currentItems = filtered.slice(indexOfFirstItem, indexOfLastItem);

  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-900 p-6">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">Intraday Signals</h2>
        <div className="flex gap-1">
          {["", "BUY", "SELL", "HOLD"].map((f) => (
            <button
              key={f}
              onClick={() => { setLoading(true); setFilter(f); }}
              className={`rounded-lg px-3 py-1 text-xs font-medium transition ${
                filter === f
                  ? "bg-emerald-600 text-white"
                  : "bg-gray-800 text-gray-400 hover:text-white"
              }`}
            >
              {f || "All"}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <TableSkeleton cols={4} />
      ) : signals.length === 0 ? (
        <p className="py-8 text-center text-sm text-gray-500">No signals found.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-gray-800 text-xs uppercase text-gray-500">
                <th className="pb-3 pr-4">Symbol</th>
                <th className="pb-3 pr-4">Signal</th>
                <th className="pb-3 pr-4 text-right">Confidence</th>
                <th className="pb-3">Reasoning</th>
              </tr>
            </thead>
            <tbody>
              {currentItems.map((s) => (
                <tr
                  key={s.id}
                  className="border-b border-gray-800/50 last:border-0"
                >
                  <td className="py-3 pr-4 font-medium text-white">
                    {s.symbol}
                  </td>
                  <td className="py-3 pr-4">
                    <span
                      className={`rounded-md px-2.5 py-1 text-xs font-semibold ${typeBadge[s.signal_type] || ""}`}
                    >
                      {s.signal_type}
                    </span>
                  </td>
                  <td className="py-3 pr-4 text-right font-mono text-gray-300">
                    {parseFloat(s.confidence_score).toFixed(1)}%
                  </td>
                  <td className="py-3 text-gray-400 text-xs leading-relaxed">
                    {s.reasoning_summary}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* Pagination controls */}
          {totalPages > 1 && (
            <div className="mt-4 flex items-center justify-between border-t border-gray-800 pt-4">
              <span className="text-sm text-gray-400">
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
        </div>
      )}
    </div>
  );
}

function TableSkeleton({ cols }) {
  return (
    <div className="space-y-3">
      {[1, 2, 3, 4, 5].map((r) => (
        <div key={r} className="flex gap-4">
          {Array.from({ length: cols }).map((_, i) => (
            <div key={i} className="h-4 flex-1 animate-pulse rounded bg-gray-800" />
          ))}
        </div>
      ))}
    </div>
  );
}
