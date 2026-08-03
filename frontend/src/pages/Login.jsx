import { useState } from "react";
import { Link, useNavigate, useLocation } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const isExpired = new URLSearchParams(location.search).get("expired") === "true";
  const [form, setForm] = useState({ username: "", password: "" });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(form.username, form.password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(
        err.response?.data?.detail || "Invalid credentials. Please try again.",
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-950 px-4">
      <div className="w-full max-w-md">
        {/* Logo / Brand */}
        <div className="mb-8 text-center">
          <h1 className="text-3xl font-bold tracking-tight text-white">
            TradePulse <span className="text-emerald-400">AI</span>
          </h1>
          <p className="mt-2 text-sm text-gray-400">
            Indian Stock Market Intelligence
          </p>
        </div>

        {/* Card */}
        <form
          onSubmit={handleSubmit}
          className="rounded-2xl border border-gray-800 bg-gray-900 p-8 shadow-xl"
        >
          <h2 className="mb-6 text-xl font-semibold text-white">Sign in</h2>

          {error && (
            <div className="mb-4 rounded-lg bg-red-900/40 px-4 py-3 text-sm text-red-300">
              {error}
            </div>
          )}

          {isExpired && !error && (
            <div className="mb-4 rounded-lg bg-emerald-900/40 border border-emerald-500/30 px-4 py-3 text-sm text-emerald-300">
              Your guest session has expired. Thank you for trying TradePulse AI!
            </div>
          )}

          <label className="mb-1 block text-sm font-medium text-gray-300">
            Username
          </label>
          <input
            type="text"
            required
            value={form.username}
            onChange={(e) => setForm({ ...form, username: e.target.value })}
            className="mb-4 w-full rounded-lg border border-gray-700 bg-gray-800 px-4 py-2.5 text-white placeholder-gray-500 outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
            placeholder="Enter username"
          />

          <label className="mb-1 block text-sm font-medium text-gray-300">
            Password
          </label>
          <input
            type="password"
            required
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
            className="mb-6 w-full rounded-lg border border-gray-700 bg-gray-800 px-4 py-2.5 text-white placeholder-gray-500 outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500"
            placeholder="Enter password"
          />

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg bg-emerald-600 py-2.5 font-semibold text-white transition hover:bg-emerald-500 disabled:opacity-50"
          >
            {loading ? "Signing in..." : "Sign in"}
          </button>

          <p className="mt-4 text-center text-sm text-gray-400">
            Private Terminal — Authorized Access Only
          </p>
        </form>
      </div>
    </div>
  );
}
