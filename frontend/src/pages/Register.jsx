import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import API from "../api/axios";

export default function Register() {
  const navigate = useNavigate();
  const [form, setForm] = useState({
    username: "",
    email: "",
    password: "",
    password2: "",
  });
  const [success, setSuccess] = useState(false);
  const [errors, setErrors] = useState({});
  const [loading, setLoading] = useState(false);

  const handleChange = (e) =>
    setForm({ ...form, [e.target.name]: e.target.value });

  const handleSubmit = async (e) => {
    e.preventDefault();
    setErrors({});
    setSuccess(false);
    setLoading(true);
    try {
      await API.post("/api/auth/register/", form);
      setSuccess(true);
      // Clear form for potential next guest
      setForm({ username: "", email: "", password: "", password2: "" });
    } catch (err) {
      const data = err.response?.data;
      if (data && typeof data === "object") {
        setErrors(data);
      } else {
        setErrors({ non_field: "Registration failed. Please try again." });
      }
    } finally {
      setLoading(false);
    }
  };

  // Flatten field errors into a single string
  const fieldError = (field) => {
    const val = errors[field];
    if (!val) return null;
    return Array.isArray(val) ? val.join(" ") : val;
  };

  const inputClass =
    "w-full rounded-lg border border-gray-700 bg-gray-800 px-4 py-2.5 text-white placeholder-gray-500 outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500";

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-950 px-4">
      <div className="w-full max-w-md">
        {/* Brand */}
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
          <h2 className="mb-6 text-xl font-semibold text-white">
            Create account
          </h2>

          {errors.non_field && (
            <div className="mb-4 rounded-lg bg-red-900/40 px-4 py-3 text-sm text-red-300">
              {errors.non_field}
            </div>
          )}

          {success && (
            <div className="mb-4 rounded-lg bg-emerald-900/40 border border-emerald-500/30 px-4 py-3 text-sm text-emerald-300">
              Success! Guest account created. They can now log in for 1 minute.
            </div>
          )}

          {/* Username */}
          <label className="mb-1 block text-sm font-medium text-gray-300">
            Username
          </label>
          <input
            type="text"
            name="username"
            required
            value={form.username}
            onChange={handleChange}
            className={`mb-1 ${inputClass}`}
            placeholder="Choose a username"
          />
          {fieldError("username") && (
            <p className="mb-3 text-xs text-red-400">{fieldError("username")}</p>
          )}
          {!fieldError("username") && <div className="mb-4" />}

          {/* Email */}
          <label className="mb-1 block text-sm font-medium text-gray-300">
            Email
          </label>
          <input
            type="email"
            name="email"
            required
            value={form.email}
            onChange={handleChange}
            className={`mb-1 ${inputClass}`}
            placeholder="you@example.com"
          />
          {fieldError("email") && (
            <p className="mb-3 text-xs text-red-400">{fieldError("email")}</p>
          )}
          {!fieldError("email") && <div className="mb-4" />}

          {/* Password */}
          <label className="mb-1 block text-sm font-medium text-gray-300">
            Password
          </label>
          <input
            type="password"
            name="password"
            required
            value={form.password}
            onChange={handleChange}
            className={`mb-1 ${inputClass}`}
            placeholder="Create a password"
          />
          {fieldError("password") && (
            <p className="mb-3 text-xs text-red-400">{fieldError("password")}</p>
          )}
          {!fieldError("password") && <div className="mb-4" />}

          {/* Confirm Password */}
          <label className="mb-1 block text-sm font-medium text-gray-300">
            Confirm Password
          </label>
          <input
            type="password"
            name="password2"
            required
            value={form.password2}
            onChange={handleChange}
            className={`mb-1 ${inputClass}`}
            placeholder="Confirm your password"
          />
          {fieldError("password2") && (
            <p className="mb-3 text-xs text-red-400">{fieldError("password2")}</p>
          )}
          {!fieldError("password2") && <div className="mb-6" />}

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg bg-emerald-600 py-2.5 font-semibold text-white transition hover:bg-emerald-500 disabled:opacity-50"
          >
            {loading ? "Creating account..." : "Create account"}
          </button>

          <p className="mt-4 text-center text-sm text-gray-400">
            Finished creating guests?{" "}
            <Link to="/" className="text-emerald-400 hover:underline">
              Back to Dashboard
            </Link>
          </p>
        </form>
      </div>
    </div>
  );
}
