import { Link } from "react-router-dom";
import OptionBuyingTable from "../components/OptionBuyingTable";

export default function OptionBuying() {
  return (
    <div className="min-h-screen bg-gray-950 font-sans text-gray-200">
      <header className="sticky top-0 z-10 border-b border-gray-800 bg-gray-950/80 backdrop-blur">
        <div className="mx-auto flex max-w-[90rem] items-center justify-between px-4 py-3 sm:px-6">
          <div className="flex items-center gap-4">
            <Link to="/" className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
              🎯 <span className="bg-gradient-to-r from-emerald-400 to-sky-400 bg-clip-text text-transparent">OPTION BUYING</span>
            </Link>
            <span className="hidden sm:inline-flex items-center gap-1.5 bg-gray-800 text-gray-400 text-[10px] font-bold uppercase tracking-wider px-2.5 py-1 rounded-full">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
              CE/PE Breakout
            </span>
          </div>
          <Link
            to="/"
            className="rounded-lg bg-gray-800 px-4 py-1.5 text-sm font-medium text-gray-300 transition hover:bg-gray-700 hover:text-white"
          >
            Dashboard
          </Link>
        </div>
      </header>

      <main className="mx-auto max-w-[90rem] px-4 py-6 sm:px-6">
        <OptionBuyingTable />
      </main>
    </div>
  );
}
