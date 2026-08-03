// Shared display helpers for signal badges/colors — used by LiveSignalsTable and the
// Dashboard preview cards so both stay visually consistent without duplicating styles.

export const signalBadge = {
  BUY:  "bg-emerald-500/15 text-emerald-400 ring-1 ring-emerald-500/30",
  SELL: "bg-red-500/15 text-red-400 ring-1 ring-red-500/30",
};

export const sentimentConfig = {
  BULLISH:  { cls: "bg-emerald-500/15 text-emerald-400 ring-1 ring-emerald-500/30" },
  BEARISH:  { cls: "bg-red-500/15 text-red-400 ring-1 ring-red-500/30" },
  SIDEWAYS: { cls: "bg-yellow-500/15 text-yellow-400 ring-1 ring-yellow-500/30" },
};

export const rsiColor = (rsi) => {
  if (rsi == null) return "text-gray-400";
  if (rsi >= 70)   return "text-red-400";
  if (rsi <= 30)   return "text-emerald-400";
  return "text-gray-300";
};

// Generic status pill classes — covers every status value used across SignalHistory
// and ShortTermSignal (PENDING/ACTIVE/HIT_TARGET/TARGET1/TARGET2/HIT_SL/EXPIRED/
// CANCELLED/etc.), for compact preview cards that don't need the full StatusBadge
// treatment ProSystem.jsx uses for its full table.
export const statusPillClass = (status) => {
  const s = (status || "").toUpperCase();
  if (s === "ACTIVE" || s === "TARGET1" || s === "TARGET2") return "bg-emerald-500/15 text-emerald-400 ring-1 ring-emerald-500/30";
  if (s === "PENDING") return "bg-amber-500/15 text-amber-400 ring-1 ring-amber-500/30";
  if (s === "HIT_TARGET") return "bg-sky-500/15 text-sky-400 ring-1 ring-sky-500/30";
  if (s === "HIT_SL") return "bg-red-500/15 text-red-400 ring-1 ring-red-500/30";
  return "bg-gray-500/15 text-gray-400 ring-1 ring-gray-500/30"; // EXPIRED, CANCELLED, etc.
};
