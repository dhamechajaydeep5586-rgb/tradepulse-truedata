import { useEffect, useState } from "react";
import API from "../api/axios";

const indicators = [
  { key: "gift_nifty", label: "GIFT Nifty" },
];

function formatLTP(val) {
  if (val == null) return "--";
  const num = parseFloat(val);
  return num.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function GlobalMarketCard({ date }) {
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

  if (loading) return <CardSkeleton />;
  if (!data) return <EmptyCard />;

  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-900 p-4 sm:p-6">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-base font-semibold text-white sm:text-lg">Global Markets</h2>
        <span className="text-[10px] text-gray-500 sm:text-xs">{data.date}</span>
      </div>

      <div className="grid grid-cols-1 gap-2 sm:gap-4">
        {indicators.map(({ key, label }) => {
          const ltp = data[`${key}_ltp`];
          const change = data[`${key}_change`] != null ? parseFloat(data[`${key}_change`]) : null;
          const isPositive = change != null && change >= 0;

          return (
            <div key={key} className="text-center">
              <p className="text-[10px] text-gray-400 sm:text-xs">{label}</p>
              <p className="mt-1 text-sm font-bold text-white sm:text-lg">
                {formatLTP(ltp)}
              </p>
              {change != null ? (
                <p
                  className={`text-xs font-semibold sm:text-sm ${isPositive ? "text-emerald-400" : "text-red-400"}`}
                >
                  {isPositive ? "+" : ""}
                  {change.toFixed(2)}%
                </p>
              ) : (
                <p className="text-xs font-semibold text-gray-600 sm:text-sm">--</p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function CardSkeleton() {
  return (
    <div className="animate-pulse rounded-2xl border border-gray-800 bg-gray-900 p-6">
      <div className="mb-4 h-5 w-32 rounded bg-gray-800" />
      <div className="grid grid-cols-1 gap-4">
        {[1].map((i) => (
          <div key={i} className="flex flex-col items-center gap-2">
            <div className="h-3 w-16 rounded bg-gray-800" />
            <div className="h-6 w-20 rounded bg-gray-800" />
            <div className="h-4 w-14 rounded bg-gray-800" />
          </div>
        ))}
      </div>
    </div>
  );
}

function EmptyCard() {
  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-900 p-6 text-center text-sm text-gray-500">
      No global market data available.
    </div>
  );
}
