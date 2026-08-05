import { createContext, useContext, useState, useCallback, useEffect } from "react";
import API from "../api/axios";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [token, setToken] = useState(() => localStorage.getItem("access"));
  const [user, setUser] = useState(null);

  const fetchProfile = useCallback(async () => {
    try {
      const { data } = await API.get("/api/auth/profile/");
      setUser(data);
    } catch (err) {
      logout();
    }
  }, []);

  const login = useCallback(async (username, password) => {
    const { data } = await API.post("/api/auth/login/", { username, password });
    localStorage.setItem("access", data.access);
    localStorage.setItem("refresh", data.refresh);
    setToken(data.access);
    // Profile is fetched in the useEffect below when token changes
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem("access");
    localStorage.removeItem("refresh");
    setToken(null);
    setUser(null);
  }, []);

  // Fetch profile when token changes
  useEffect(() => {
    if (token) {
      fetchProfile();
    } else {
      setUser(null);
    }
  }, [token, fetchProfile]);

  // Audit fix (found in a follow-up audit): axios.js's 401 interceptor silently
  // refreshes the access token and writes it to localStorage, but previously
  // never told this context — `token` state stayed pinned to whatever was set at
  // last login()/logout(), so any consumer of useAuth().token (rather than the
  // interceptor's own localStorage read) kept using a stale, expired value.
  // axios.js dispatches these two window events on refresh success/failure;
  // sync React state to match rather than letting it drift from localStorage.
  useEffect(() => {
    const handleRefreshed = (e) => setToken(e.detail.access);
    const handleLogout = () => {
      setToken(null);
      setUser(null);
    };
    window.addEventListener("auth:token-refreshed", handleRefreshed);
    window.addEventListener("auth:logout", handleLogout);
    return () => {
      window.removeEventListener("auth:token-refreshed", handleRefreshed);
      window.removeEventListener("auth:logout", handleLogout);
    };
  }, []);

  // AUTO-LOGOUT TIMER for guest users
  useEffect(() => {
    if (user && user.is_temporary && user.first_login_at) {
      const firstLoginTime = new Date(user.first_login_at).getTime();
      const expiryTime = firstLoginTime + 60 * 1000; // 1 minute
      const delay = expiryTime - Date.now();

      if (delay <= 0) {
        logout();
      } else {
        const timer = setTimeout(() => {
          logout();
        }, delay);
        return () => clearTimeout(timer);
      }
    }
  }, [user, logout]);

  return (
    <AuthContext.Provider value={{ token, user, isAuthenticated: !!token, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
