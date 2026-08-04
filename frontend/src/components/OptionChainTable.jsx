import { useEffect, useRef, useState } from "react";
import API from "../api/axios";

const fmtL = (val) => {
  if (val == null) return "—";
  return (val / 100000).toFixed(1) + "L";
};

const fmtOIChange = (val) => {
  if (val == null) return "—";
  const abs = (Math.abs(val) / 100000).toFixed(1) + "L";
  return val >= 0 ? `+${abs}` : `-${abs}`;
};

function SkeletonRow() {
  return (
    <tr>
      {Array.from({ length: 7 }).map((_, i) => (
        <td key={i} className="px-3 py-3">
          <div className="h-4 animate-pulse rounded bg-gray-800" />
        </td>
      ))}
    </tr>
  );
}

function SummarySkeleton() {
  return (
    <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-5">
      {[1, 2, 3, 4, 5].map((i) => (
        <div key={i} className="rounded-xl border border-gray-800 bg-gray-800/50 p-3">
          <div className="mb-1.5 h-3 w-16 animate-pulse rounded bg-gray-700" />
          <div className="h-5 w-20 animate-pulse rounded bg-gray-700" />
        </div>
      ))}
    </div>
  );
}

export default function OptionChainTable() {
  const [symbol, setSymbol] = useState("NIFTY");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const hasChainRef = useRef(false);
  // Audit fix M20: trust the backend's NSE-API-backed market_status (see
  // OptionChainView) instead of a local clock-only check — that check had no
  // concept of NSE holidays, so a holiday within trading hours still polled
  // every second, and it duplicated logic the backend already gets right.
  // Updated on every successful fetch, read from the poller between fetches.
  const isOpenRef = useRef(false);

  useEffect(() => {
    let isSubscribed = true;
    // Audit fix M17: no request-sequence guard existed on this 1s poller — an
    // out-of-order response under variable latency could overwrite fresher state
    // with stale data. requestSeq increments per issued request; a response only
    // applies if it's still the latest one.
    let requestSeq = 0;

    const fetchChain = (showLoading = false) => {
      if (showLoading) setLoading(true);
      setError(null);
      const mySeq = ++requestSeq;
      API.get("/api/stocks/option-chain/", { params: { symbol } })
        .then((r) => {
          if (!isSubscribed || mySeq !== requestSeq) return;
          if (r.data.error) {
            setError(r.data.error);
            setData(r.data);
          } else {
            setData(r.data);
          }
          hasChainRef.current = !!(r.data?.chain?.length > 0);
          isOpenRef.current = r.data?.market_status === "OPEN";
        })
        .catch((err) => {
          if (!isSubscribed || mySeq !== requestSeq) return;
          setError(
             err?.response?.data?.detail ?? "Failed to load option chain data."
          );
        })
        .finally(() => {
          if (isSubscribed && showLoading) setLoading(false);
        });
    };

    // Initial explicit load
    fetchChain(true);

    // High frequency 1-second auto-poll only if market is open and data is available
    const pollerId = setInterval(() => {
      if (isOpenRef.current && hasChainRef.current) {
        fetchChain(false);
      }
    }, 1000);
    
    return () => {
      isSubscribed = false;
      clearInterval(pollerId);
    };
  }, [symbol]);

  const chain = data?.chain ?? [];
  const spot = data?.spot_price;

  // Find ATM strike — nearest to spot
  const atmStrike =
    spot && chain.length
      ? chain.reduce((prev, cur) =>
          Math.abs(cur.strike - spot) < Math.abs(prev.strike - spot)
            ? cur
            : prev
        ).strike
      : null;

  const pcrColor = (pcr) => {
    if (pcr == null) return "text-gray-300";
    if (pcr > 1) return "text-emerald-400";
    if (pcr < 0.7) return "text-red-400";
    return "text-yellow-400";
  };

  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-900 p-4 sm:p-6">
      {/* Header */}
      <div className="mb-5 flex items-center justify-between gap-3">
        <h2 className="text-base font-semibold text-white sm:text-lg">Option Chain</h2>
        <div className="flex gap-1.5">
          {["NIFTY", "BANKNIFTY"].map((sym) => (
            <button
              key={sym}
              onClick={() => setSymbol(sym)}
              className={`rounded-lg px-2 py-1 text-[10px] font-medium transition sm:px-3 sm:py-1.5 sm:text-xs ${
                symbol === sym
                  ? "bg-emerald-600 text-white"
                  : "bg-gray-800 text-gray-400 hover:text-white"
              }`}
            >
              {sym}
            </button>
          ))}
        </div>
      </div>

      {/* Summary */}
      {loading ? (
        <SummarySkeleton />
      ) : data && !error ? (
        <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-5">
          <SummaryCard label="Spot Price" value={spot?.toFixed(2) ?? "—"} valueClass="text-white" />
          <SummaryCard
            label="PCR"
            value={data.pcr?.toFixed(2) ?? "—"}
            valueClass={pcrColor(data.pcr)}
          />
          <SummaryCard
            label="Max Pain"
            value={data.max_pain?.toLocaleString("en-IN") ?? "—"}
            valueClass="text-yellow-400"
            badge="★"
          />
          <SummaryCard
            label="Total CE OI"
            value={fmtL(data.total_ce_oi)}
            valueClass="text-red-400"
          />
          <SummaryCard
            label="Total PE OI"
            value={fmtL(data.total_pe_oi)}
            valueClass="text-emerald-400"
          />
        </div>
      ) : null}

      {/* Error state */}
      {!loading && error && (
        <div className="mb-4 rounded-xl border border-red-800/50 bg-red-900/20 px-4 py-3">
          <p className="text-sm text-red-400">{error}</p>
        </div>
      )}

      {/* Expiry info */}
      {!loading && data?.expiry && (
        <p className="mb-3 text-xs text-gray-500">
          Expiry:{" "}
          <span className="text-gray-300 font-medium">{data.expiry}</span>
        </p>
      )}

      {/* Table */}
      {loading ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-800 text-xs uppercase text-gray-500">
                <th className="pb-3 px-3 text-right">CE OI Chg</th>
                <th className="pb-3 px-3 text-right">CE OI</th>
                <th className="pb-3 px-3 text-right">CE LTP</th>
                <th className="pb-3 px-3 text-center font-bold">Strike</th>
                <th className="pb-3 px-3 text-right">PE LTP</th>
                <th className="pb-3 px-3 text-right">PE OI</th>
                <th className="pb-3 px-3 text-right">PE OI Chg</th>
              </tr>
            </thead>
            <tbody>
              {[1, 2, 3, 4, 5, 6, 7, 8].map((i) => (
                <SkeletonRow key={i} />
              ))}
            </tbody>
          </table>
        </div>
      ) : chain.length === 0 ? (
        <p className="py-10 text-center text-sm text-gray-500">
          No chain data available.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-gray-800 text-xs uppercase text-gray-500">
                <th className="pb-3 px-3 text-right whitespace-nowrap">CE OI Chg</th>
                <th className="pb-3 px-3 text-right whitespace-nowrap">CE OI</th>
                <th className="pb-3 px-3 text-right whitespace-nowrap">CE LTP</th>
                <th className="pb-3 px-3 text-center font-bold whitespace-nowrap">Strike</th>
                <th className="pb-3 px-3 text-right whitespace-nowrap">PE LTP</th>
                <th className="pb-3 px-3 text-right whitespace-nowrap">PE OI</th>
                <th className="pb-3 px-3 text-right whitespace-nowrap">PE OI Chg</th>
              </tr>
            </thead>
            <tbody>
              {chain.map((row) => {
                const isATM = row.strike === atmStrike;
                const isMaxPain = row.strike === data?.max_pain;
                return (
                  <tr
                    key={row.strike}
                    className={`border-b border-gray-800/50 last:border-0 transition-colors ${
                      isATM
                        ? "bg-emerald-900/20 hover:bg-emerald-900/30"
                        : "hover:bg-gray-800/30"
                    }`}
                  >
                    {/* CE OI Change */}
                    <td className="px-3 py-2.5 text-right font-mono text-xs whitespace-nowrap">
                      <span
                        className={
                          row.ce_oi_change >= 0
                            ? "text-emerald-400"
                            : "text-red-400"
                        }
                      >
                        {fmtOIChange(row.ce_oi_change)}
                      </span>
                    </td>
                    {/* CE OI */}
                    <td className="px-3 py-2.5 text-right font-mono text-xs text-red-300 whitespace-nowrap">
                      {fmtL(row.ce_oi)}
                    </td>
                    {/* CE LTP */}
                    <td className="px-3 py-2.5 text-right font-mono text-xs text-red-300 whitespace-nowrap">
                      {row.ce_ltp?.toFixed(2) ?? "—"}
                    </td>
                    {/* Strike */}
                    <td className="px-3 py-2.5 text-center font-bold whitespace-nowrap">
                      <span
                        className={
                          isATM
                            ? "text-emerald-400"
                            : isMaxPain
                            ? "text-yellow-400"
                            : "text-gray-100"
                        }
                      >
                        {row.strike?.toLocaleString("en-IN")}
                        {isATM && (
                          <span className="ml-1 text-[10px] font-normal text-emerald-500">
                            ATM
                          </span>
                        )}
                        {isMaxPain && !isATM && (
                          <span className="ml-1 text-[10px] font-normal text-yellow-500">
                            ★
                          </span>
                        )}
                      </span>
                    </td>
                    {/* PE LTP */}
                    <td className="px-3 py-2.5 text-right font-mono text-xs text-emerald-300 whitespace-nowrap">
                      {row.pe_ltp?.toFixed(2) ?? "—"}
                    </td>
                    {/* PE OI */}
                    <td className="px-3 py-2.5 text-right font-mono text-xs text-emerald-300 whitespace-nowrap">
                      {fmtL(row.pe_oi)}
                    </td>
                    {/* PE OI Change */}
                    <td className="px-3 py-2.5 text-right font-mono text-xs whitespace-nowrap">
                      <span
                        className={
                          row.pe_oi_change >= 0
                            ? "text-emerald-400"
                            : "text-red-400"
                        }
                      >
                        {fmtOIChange(row.pe_oi_change)}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Legend */}
      {!loading && chain.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-4 text-[11px] text-gray-600">
          <span>
            <span className="text-emerald-500">ATM</span> — At-the-money strike
          </span>
          <span>
            <span className="text-yellow-500">★</span> — Max pain strike
          </span>
          <span>
            <span className="text-red-400">Red</span> — CE (Call) side
          </span>
          <span>
            <span className="text-emerald-400">Green</span> — PE (Put) side
          </span>
        </div>
      )}
    </div>
  );
}

function SummaryCard({ label, value, valueClass, badge }) {
  return (
    <div className="rounded-xl border border-gray-800 bg-gray-800/40 p-3">
      <p className="mb-1 text-[11px] uppercase tracking-wide text-gray-500">
        {label}
      </p>
      <p className={`text-base font-semibold ${valueClass}`}>
        {badge && <span className="mr-1">{badge}</span>}
        {value}
      </p>
    </div>
  );
}
