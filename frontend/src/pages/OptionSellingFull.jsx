import { Link } from "react-router-dom";
import DeltaHedgePanel from "../components/DeltaHedgePanel";

export default function OptionSellingFull() {
  return (
    <div className="min-h-screen bg-gray-950 font-sans text-gray-200">
      <header className="sticky top-0 z-10 border-b border-gray-800 bg-gray-950/80 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3 sm:px-6">
          <h1 className="text-xl font-bold tracking-tight text-white">
            📉 Option Selling (Strangle)
          </h1>
          <Link
            to="/"
            className="rounded-lg bg-gray-800 px-4 py-1.5 text-sm font-medium text-gray-300 transition hover:bg-gray-700 hover:text-white"
          >
            Dashboard
          </Link>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6">
        <DeltaHedgePanel />
      </main>
    </div>
  );
}
