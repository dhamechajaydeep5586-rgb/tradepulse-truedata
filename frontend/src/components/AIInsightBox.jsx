import { useEffect, useState } from "react";
import API from "../api/axios";

export default function AIInsightBox({ date }) {
  const [insight, setInsight] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    const params = date ? { date } : {};
    API.get("/api/insights/daily/", { params })
      .then((r) => setInsight(r.data))
      .catch(() => setInsight(null))
      .finally(() => setLoading(false));
  }, [date]);

  if (loading) {
    return (
      <div className="animate-pulse rounded-2xl border border-gray-800 bg-gray-900 p-6">
        <div className="mb-3 h-5 w-28 rounded bg-gray-800" />
        <div className="space-y-2">
          <div className="h-4 w-full rounded bg-gray-800" />
          <div className="h-4 w-5/6 rounded bg-gray-800" />
          <div className="h-4 w-4/6 rounded bg-gray-800" />
        </div>
      </div>
    );
  }

  if (!insight) {
    return (
      <div className="rounded-2xl border border-gray-800 bg-gray-900 p-6 text-sm text-gray-500">
        No AI insight available for today.
      </div>
    );
  }

  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-900 p-6">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-white">
          AI Pre-Market Insight
        </h2>
        <span className="text-xs text-gray-500">
          {date ? insight.date : new Date().toISOString().split('T')[0]} 
          {!date && <span className="ml-1 opacity-60">(Ref: {insight.date})</span>}
        </span>
      </div>
      <div className="whitespace-pre-line text-sm leading-relaxed text-gray-300">
        {insight.ai_summary}
      </div>
    </div>
  );
}
