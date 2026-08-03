import { useEffect, useState } from "react";
import API from "../api/axios";

export default function DomesticMarketCard() {
  const [data, setData] = useState({
    NIFTY: { ltp: 0, change: 0, prev: 0 },
    BANKNIFTY: { ltp: 0, change: 0, prev: 0 },
  });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchPrices = () => {
      API.get("/api/stocks/live-price-updates/?symbols=NIFTY,BANKNIFTY")
        .then((r) => {
          if (Object.keys(r.data).length > 0) {
            setData((prevData) => {
              const newData = { ...prevData };
              if (r.data.NIFTY) {
                newData.NIFTY = {
                  ltp: r.data.NIFTY,
                  // Logic for change calculation if needed, or backend can provide it
                  prev: prevData.NIFTY.ltp || r.data.NIFTY,
                };
              }
              if (r.data.BANKNIFTY) {
                newData.BANKNIFTY = {
                  ltp: r.data.BANKNIFTY,
                  prev: prevData.BANKNIFTY.ltp || r.data.BANKNIFTY,
                };
              }
              return newData;
            });
            setLoading(false);
          }
        })
        .catch((err) => console.debug("Domestic Market Card Poll Error:", err));
    };

    fetchPrices();
    const id = setInterval(fetchPrices, 30000); // 30-second pulse for general market indices
    return () => clearInterval(id);
  }, []);

  if (loading) return <DomesticSkeleton />;

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      <IndexCard title="NIFTY 50" value={data.NIFTY.ltp} prev={data.NIFTY.prev} />
      <IndexCard title="BANK NIFTY" value={data.BANKNIFTY.ltp} prev={data.BANKNIFTY.prev} />
    </div>
  );
}

function IndexCard({ title, value, prev }) {
  const isUp = value >= prev;
  const priceColor = isUp ? "text-emerald-400" : "text-red-400";

  return (
    <div className="relative overflow-hidden rounded-2xl border border-gray-800 bg-gray-900 p-5 transition-all hover:border-gray-700">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wider">{title}</p>
          <h3 className={`mt-1 text-2xl font-black tracking-tight sm:text-3xl ${priceColor}`}>
            {value.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
          </h3>
        </div>
        <div className={`flex h-10 w-10 items-center justify-center rounded-full ${isUp ? "bg-emerald-500/10 text-emerald-400" : "bg-red-500/10 text-red-400"}`}>
          {isUp ? (
            <svg xmlns="http://www.w3.org/2000/svg" className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-9 9-4-4-6 6" />
            </svg>
          ) : (
            <svg xmlns="http://www.w3.org/2000/svg" className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 17h8m0 0V9m0 8l-9-9-4 4-6-6" />
            </svg>
          )}
        </div>
      </div>
      
      {/* Subtle background glow */}
      <div className={`absolute -right-4 -top-4 h-24 w-24 rounded-full blur-3xl opacity-10 ${isUp ? "bg-emerald-500" : "bg-red-500"}`} />
    </div>
  );
}

function DomesticSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      {[1, 2].map((i) => (
        <div key={i} className="animate-pulse rounded-2xl border border-gray-800 bg-gray-900 p-5 h-[100px]" />
      ))}
    </div>
  );
}
