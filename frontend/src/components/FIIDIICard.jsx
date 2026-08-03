import { useEffect, useState } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Legend,
  CartesianGrid,
} from "recharts";
import API from "../api/axios";

const fmtCr = (val) => {
  if (val == null) return "—";
  return `₹${Math.abs(val).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} Cr`;
};

const netColor = (val) =>
  val == null ? "text-gray-300" : val >= 0 ? "text-emerald-400" : "text-red-400";

function StatRow({ label, value, valueClass }) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-gray-800/60 last:border-0">
      <span className="text-xs text-gray-500">{label}</span>
      <span className={`text-sm font-semibold font-mono ${valueClass}`}>{value}</span>
    </div>
  );
}

function ActivityCard({ title, buy, sell, net, accentColor }) {
  return (
    <div
      className={`flex-1 rounded-xl border bg-gray-800/40 p-4 ${
        accentColor === "emerald"
          ? "border-emerald-800/40"
          : "border-blue-800/40"
      }`}
    >
      <h3
        className={`mb-3 text-sm font-semibold ${
          accentColor === "emerald" ? "text-emerald-400" : "text-blue-400"
        }`}
      >
        {title}
      </h3>
      <StatRow
        label="Buy"
        value={fmtCr(buy)}
        valueClass="text-emerald-400"
      />
      <StatRow
        label="Sell"
        value={fmtCr(sell)}
        valueClass="text-red-400"
      />
      <StatRow
        label="Net"
        value={
          net != null
            ? `${net >= 0 ? "+" : ""}${fmtCr(net)}`
            : "—"
        }
        valueClass={netColor(net)}
      />
    </div>
  );
}

function CardSkeleton() {
  return (
    <div className="flex gap-4">
      {[1, 2].map((i) => (
        <div
          key={i}
          className="flex-1 rounded-xl border border-gray-800 bg-gray-800/40 p-4 space-y-3"
        >
          <div className="h-4 w-16 animate-pulse rounded bg-gray-700" />
          {[1, 2, 3].map((j) => (
            <div key={j} className="flex justify-between">
              <div className="h-3.5 w-12 animate-pulse rounded bg-gray-700" />
              <div className="h-3.5 w-28 animate-pulse rounded bg-gray-700" />
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function ChartSkeleton() {
  return (
    <div className="mt-5 flex h-48 items-center justify-center rounded-xl border border-gray-800 bg-gray-800/30">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-700 border-t-emerald-400" />
    </div>
  );
}

const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-xs">
      <p className="mb-1.5 font-semibold text-gray-200">{label}</p>
      {payload.map((p) => (
        <p key={p.dataKey} style={{ color: p.color }}>
          {p.name}:{" "}
          <span className="font-mono">
            {p.value >= 0 ? "+" : ""}
            {p.value?.toFixed(2)} Cr
          </span>
        </p>
      ))}
    </div>
  );
};

export default function FIIDIICard() {
  const [records, setRecords] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    API.get("/api/stocks/fii-dii/")
      .then((r) => {
        const raw = Array.isArray(r.data) ? r.data : r.data.results ?? [];
        // Latest 5 trading days, sorted ascending for chart
        const sorted = [...raw]
          .sort((a, b) => {
            // Try date string comparison (dd-Mon-yyyy)
            const toDate = (s) => {
              if (!s) return 0;
              const parts = s.split("-");
              if (parts.length === 3) {
                return new Date(`${parts[1]} ${parts[0]} ${parts[2]}`).getTime();
              }
              return new Date(s).getTime();
            };
            return toDate(a.date) - toDate(b.date);
          })
          .slice(-5);
        setRecords(sorted);
      })
      .catch(() => setRecords([]))
      .finally(() => setLoading(false));
  }, []);

  const latest = records[records.length - 1] ?? null;

  const chartData = records.map((r) => ({
    date: r.date,
    "FII Net": parseFloat(r.fii_net ?? 0),
    "DII Net": parseFloat(r.dii_net ?? 0),
  }));

  return (
    <div className="rounded-2xl border border-gray-800 bg-gray-900 p-6">
      {/* Header */}
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-white">FII / DII Activity</h2>
          {latest?.date && (
            <p className="mt-0.5 text-xs text-gray-500">
              Latest:{" "}
              <span className="text-gray-300 font-medium">{latest.date}</span>
            </p>
          )}
        </div>
        {latest && (
          <div className="flex gap-2">
            <NetPill label="FII" net={latest.fii_net} />
            <NetPill label="DII" net={latest.dii_net} />
          </div>
        )}
      </div>

      {/* Cards */}
      {loading ? (
        <CardSkeleton />
      ) : latest ? (
        <div className="flex flex-col gap-4 sm:flex-row">
          <ActivityCard
            title="FII / FPI"
            buy={latest.fii_buy}
            sell={latest.fii_sell}
            net={latest.fii_net}
            accentColor="emerald"
          />
          <ActivityCard
            title="DII"
            buy={latest.dii_buy}
            sell={latest.dii_sell}
            net={latest.dii_net}
            accentColor="blue"
          />
        </div>
      ) : (
        <p className="py-8 text-center text-sm text-gray-500">
          No FII/DII data available.
        </p>
      )}

      {/* Bar Chart */}
      {loading ? (
        <ChartSkeleton />
      ) : chartData.length > 0 ? (
        <div className="mt-5">
          <p className="mb-2 text-xs uppercase tracking-wide text-gray-500">
            Net Flow — Last {chartData.length} Sessions (₹ Cr)
          </p>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart
              data={chartData}
              margin={{ top: 5, right: 5, bottom: 5, left: 10 }}
              barCategoryGap="30%"
              barGap={4}
            >
              <CartesianGrid
                strokeDasharray="3 3"
                vertical={false}
                stroke="#374151"
              />
              <XAxis
                dataKey="date"
                tick={{ fill: "#9ca3af", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: "#9ca3af", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                tickFormatter={(v) =>
                  v >= 0 ? `+${v.toLocaleString("en-IN")}` : v.toLocaleString("en-IN")
                }
              />
              <Tooltip content={<CustomTooltip />} />
              <Legend
                wrapperStyle={{ fontSize: "12px", color: "#9ca3af" }}
              />
              <Bar
                dataKey="FII Net"
                fill="#34d399"
                fillOpacity={0.8}
                radius={[4, 4, 0, 0]}
              />
              <Bar
                dataKey="DII Net"
                fill="#60a5fa"
                fillOpacity={0.8}
                radius={[4, 4, 0, 0]}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      ) : null}
    </div>
  );
}

function NetPill({ label, net }) {
  const positive = net != null && net >= 0;
  return (
    <span
      className={`rounded-md px-2.5 py-1 text-xs font-semibold ring-1 ${
        positive
          ? "bg-emerald-500/15 text-emerald-400 ring-emerald-500/30"
          : "bg-red-500/15 text-red-400 ring-red-500/30"
      }`}
    >
      {label}{" "}
      {net != null
        ? `${positive ? "+" : ""}${parseFloat(net).toFixed(0)} Cr`
        : "—"}
    </span>
  );
}
