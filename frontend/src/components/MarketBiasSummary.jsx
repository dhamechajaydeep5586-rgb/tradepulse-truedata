import { useEffect, useState } from "react";
import API from "../api/axios";

const biasConfig = {
  BULLISH: { color: "text-emerald-400", bg: "bg-emerald-500/10", icon: "\u25B2" },
  BEARISH: { color: "text-red-400", bg: "bg-red-500/10", icon: "\u25BC" },
  NEUTRAL: { color: "text-yellow-400", bg: "bg-yellow-500/10", icon: "\u25C6" },
};

export default function MarketBiasSummary({ date }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    const params = date ? { date } : {};
    API.get("/api/global-market/latest/", { params })
      .then((r) => setData(r.data))
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [date]);

  if (loading) {
    return (
      <div className="animate-pulse rounded-2xl border border-gray-800 bg-gray-900 p-6">
        <div className="h-5 w-36 rounded bg-gray-800" />
        <div className="mt-4 h-12 w-40 rounded bg-gray-800" />
      </div>
    );
  }

  const bias = data?.market_bias || "NEUTRAL";
  const cfg = biasConfig[bias] || biasConfig.NEUTRAL;

  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-900 p-4 sm:p-6">
      <h2 className="mb-3 text-base font-semibold text-white sm:text-lg">Market Bias</h2>
      <div
        className={`inline-flex items-center gap-2 rounded-xl px-4 py-2 text-xl font-bold sm:px-5 sm:py-3 sm:text-2xl ${cfg.bg} ${cfg.color}`}
      >
        <span>{cfg.icon}</span>
        <span>{bias}</span>
      </div>
      {data?.date && (
        <p className="mt-3 text-[10px] text-gray-500 sm:text-xs">As of {data.date}</p>
      )}
    </div>
  );
}
