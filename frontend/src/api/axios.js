import axios from "axios";

// Audit finding H13 (assessed, deliberately deferred — not fixed in this pass):
// both JWTs live in localStorage, fully exposed to any future XSS. The real fix is
// httpOnly cookies, which is a coordinated backend+frontend change (cookie-setting
// login/refresh endpoints, a DRF auth class reading the cookie instead of the
// Authorization header, CSRF token plumbing since cookies are sent automatically,
// CORS credentials, and an AuthContext that can no longer read its own "am I logged
// in" state synchronously from localStorage). That's a full session-architecture
// migration touching every authenticated request in the app — a bad edge case there
// risks locking out every user, and it cannot be fully verified without a live
// cross-origin browser test this environment can't run. Rather than ship that blind,
// this was assessed and intentionally left for a supervised deploy with browser
// testing. Partial, already-shipped mitigation: refresh-token rotation +
// blacklist-on-rotation is enabled (SIMPLE_JWT / token_blacklist in settings.py), so
// a stolen refresh token is invalidated the next time the legitimate client rotates,
// and the access token itself is short-lived (30 min).
// Audit fix M18: no request timeout existed at all — a hung backend/TrueData call
// left an action (e.g. Force Scan) spinning forever with no user-facing recovery.
const API = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "",
  timeout: 20000,
});

// Distinct, easy-to-check timeout flag — axios already sets error.code ===
// "ECONNABORTED" on a timeout, but callers shouldn't need to know that constant.
API.interceptors.response.use(
  (res) => res,
  (error) => {
    if (error.code === "ECONNABORTED" && /timeout/i.test(error.message || "")) {
      error.isTimeout = true;
    }
    return Promise.reject(error);
  },
);

// Attach JWT access token to every request
API.interceptors.request.use((config) => {
  const token = localStorage.getItem("access");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

let isRefreshing = false;
let failedQueue = [];

const processQueue = (error, token = null) => {
  failedQueue.forEach((prom) => {
    if (error) {
      prom.reject(error);
    } else {
      prom.resolve(token);
    }
  });
  failedQueue = [];
};

// On 401, try refreshing the token once
API.interceptors.response.use(
  (res) => res,
  async (error) => {
    const originalRequest = error.config;

    if (error.response?.status === 401 && !originalRequest._retry) {
      if (isRefreshing) {
        // Audit fix M19: this used to leave _retry unset on queued requests — only
        // the request that actually triggered the refresh got marked. If a queued
        // request's retry independently 401'd again (e.g. the refreshed token is
        // itself rejected for that resource), it re-entered this same interceptor
        // with _retry still falsy and could loop back into another refresh attempt
        // instead of failing cleanly.
        originalRequest._retry = true;
        return new Promise((resolve, reject) => {
          failedQueue.push({ resolve, reject });
        })
          .then((token) => {
            originalRequest.headers.Authorization = `Bearer ${token}`;
            return API(originalRequest);
          })
          .catch((err) => Promise.reject(err));
      }

      originalRequest._retry = true;
      isRefreshing = true;

      const refreshToken = localStorage.getItem("refresh");
      if (refreshToken) {
        try {
          const { data } = await axios.post(
            `${API.defaults.baseURL}/api/auth/token/refresh/`,
            { refresh: refreshToken }
          );

          localStorage.setItem("access", data.access);
          if (data.refresh) localStorage.setItem("refresh", data.refresh);
          // Audit fix (found in a follow-up audit): this silent refresh only ever
          // wrote the new access token to localStorage — AuthContext's `token`
          // React state (read by every component via useAuth()) kept holding the
          // OLD, now-expired value, since nothing re-synced it outside of an
          // explicit login()/logout() call. Any code path reading `token` from
          // context instead of localStorage (rather than relying on this
          // interceptor's own localStorage read on the next request) would keep
          // using a dead token indefinitely. AuthContext listens for this event
          // and calls setToken() to bring React state back in sync.
          window.dispatchEvent(
            new CustomEvent("auth:token-refreshed", { detail: { access: data.access } })
          );

          processQueue(null, data.access);
          originalRequest.headers.Authorization = `Bearer ${data.access}`;
          return API(originalRequest);
        } catch (refreshError) {
          processQueue(refreshError, null);
          localStorage.removeItem("access");
          localStorage.removeItem("refresh");
          // Same fix, failure side: clear AuthContext's `token`/`user` state
          // immediately rather than leaving it stale until the redirect below
          // completes (or forever, if something ever intercepts/blocks it).
          window.dispatchEvent(new CustomEvent("auth:logout"));
          // Redirect with expired flag to show the popup on Login page
          window.location.href = "/login?expired=true";
        } finally {
          isRefreshing = false;
        }
      } else {
        // Audit fix M20 (bug found in a follow-up audit): `isRefreshing` was set
        // true above, but with no refreshToken this whole try/catch/finally block
        // was skipped entirely — `isRefreshing` never got reset back to false, so
        // every 401 for the rest of the session queued into failedQueue and never
        // resolved (permanently "stuck", no network activity, no error surfaced).
        isRefreshing = false;
        window.dispatchEvent(new CustomEvent("auth:logout"));
        window.location.href = "/login?expired=true";
      }
    }
    return Promise.reject(error);
  },
);

// --- In-flight GET request de-dup (AUDIT_REMEDIATION_PLAN.md #3.3.3) ---
// Multiple components (e.g. GlobalMarketCard + MarketBiasSummary) independently
// fetch the same endpoint+params on mount. Rather than lifting them into a shared
// hook, we collapse concurrent identical GETs into a single network call here:
// every caller gets the same in-flight promise, keyed by URL+params. Calls with
// different params (e.g. a different `date`) are NOT deduped against each other.
const _inFlightGetRequests = new Map();

// Stable stringify so key order in `params` doesn't create spurious cache misses.
function _stableStringify(value) {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(_stableStringify).join(",")}]`;
  }
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${_stableStringify(value[k])}`).join(",")}}`;
}

function _buildDedupKey(url, config) {
  return `${url}?${_stableStringify(config?.params ?? {})}`;
}

const _rawGet = API.get.bind(API);

API.get = (url, config) => {
  const key = _buildDedupKey(url, config);
  const inFlight = _inFlightGetRequests.get(key);
  if (inFlight) {
    return inFlight;
  }

  const promise = _rawGet(url, config).finally(() => {
    // Clean up regardless of success/failure so future calls (even with the
    // same params) hit the network again rather than serving a stale promise.
    _inFlightGetRequests.delete(key);
  });

  _inFlightGetRequests.set(key, promise);
  return promise;
};

export default API;
