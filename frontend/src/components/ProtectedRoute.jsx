import { Navigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

// Audit fix M15: this used to check only that a token value existed, never its
// validity or expiry — an expired/garbage token still rendered the protected
// page shell briefly before the first API call 401'd and redirected. Decodes
// the JWT's own `exp` claim (no signature verification needed — this is a UX
// guard only; the backend independently and authoritatively validates the
// token on every request) and treats expired the same as absent.
function isTokenExpired(token) {
  if (!token) return true;
  try {
    const base64Payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(base64Payload));
    if (!payload.exp) return false; // no exp claim -> can't determine, don't block
    return Date.now() >= payload.exp * 1000;
  } catch {
    return true; // unparseable token -> treat as invalid
  }
}

export default function ProtectedRoute({ children }) {
  const { token, isAuthenticated } = useAuth();
  if (!isAuthenticated || isTokenExpired(token)) return <Navigate to="/login" replace />;
  return children;
}
