import { Link } from "react-router-dom";

// Generic dashboard preview card — shows up to 3 rows of a signal category with a
// "View All" link out to the category's dedicated full screen. Row rendering is a
// render-prop rather than a fixed column set, since intraday/short-term/long-term/
// option-selling all have genuinely different fields — forcing one shared row shape
// would either lose information or need per-field branching inside this component.
export default function SignalPreviewCard({
  title,
  icon,
  loading,
  count,
  items = [],
  viewAllTo,
  renderRow,
  emptyLabel = "No active signals",
}) {
  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900 overflow-hidden flex flex-col">
      <div className="flex items-center justify-between border-b border-gray-800 px-4 py-3">
        <h3 className="flex items-center gap-2 text-sm font-bold text-white">
          <span>{icon}</span> {title}
          {count > 0 && (
            <span className="rounded-full bg-gray-800 px-2 py-0.5 text-[10px] font-bold text-gray-400">
              {count}
            </span>
          )}
        </h3>
        <Link
          to={viewAllTo}
          className="text-xs font-semibold text-indigo-400 transition hover:text-indigo-300"
        >
          View All →
        </Link>
      </div>

      <div className="flex-1 divide-y divide-gray-800/50">
        {loading ? (
          <div className="space-y-3 p-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-10 animate-pulse rounded bg-gray-800/70" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <div className="p-6 text-center text-xs text-gray-500">{emptyLabel}</div>
        ) : (
          items.slice(0, 3).map((item, i) => (
            <div key={item.id ?? i} className="px-4 py-2.5">
              {renderRow(item)}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
