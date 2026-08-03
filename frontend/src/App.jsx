import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import ProtectedRoute from "./components/ProtectedRoute";
import Login from "./pages/Login";
import Register from "./pages/Register";
import Dashboard from "./pages/Dashboard";
import PerformanceReports from "./pages/PerformanceReports";
import ProSystem from "./pages/ProSystem";
import LiveSignalsFull from "./pages/LiveSignalsFull";
import OptionSellingFull from "./pages/OptionSellingFull";
import OptionBuying from "./pages/OptionBuying";

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/register"
            element={
              <ProtectedRoute>
                <Register />
              </ProtectedRoute>
            }
          />
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <Dashboard />
              </ProtectedRoute>
            }
          />
          <Route
            path="/reports"
            element={
              <ProtectedRoute>
                <PerformanceReports />
              </ProtectedRoute>
            }
          />
          <Route
            path="/pro-system"
            element={
              <ProtectedRoute>
                <ProSystem />
              </ProtectedRoute>
            }
          />
          <Route
            path="/live-signals"
            element={
              <ProtectedRoute>
                <LiveSignalsFull />
              </ProtectedRoute>
            }
          />
          <Route
            path="/option-selling"
            element={
              <ProtectedRoute>
                <OptionSellingFull />
              </ProtectedRoute>
            }
          />
          <Route
            path="/option-buying"
            element={
              <ProtectedRoute>
                <OptionBuying />
              </ProtectedRoute>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
