import axios from "axios";

const API = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "",
});

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
          
          processQueue(null, data.access);
          originalRequest.headers.Authorization = `Bearer ${data.access}`;
          return API(originalRequest);
        } catch (refreshError) {
          processQueue(refreshError, null);
          localStorage.removeItem("access");
          localStorage.removeItem("refresh");
          // Redirect with expired flag to show the popup on Login page
          window.location.href = "/login?expired=true";
        } finally {
          isRefreshing = false;
        }
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
