import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Toaster } from "sonner";
import "@/App.css";

import { AuthProvider, useAuth } from "@/lib/auth";
import { ConfirmProvider } from "@/components/ConfirmDialog";
import LandingPage from "@/pages/LandingPage";
import LoginPage from "@/pages/LoginPage";
import RegisterPage from "@/pages/RegisterPage";
import DashboardPage from "@/pages/DashboardPage";
import ProjectsPage from "@/pages/ProjectsPage";
import CreateProjectPage from "@/pages/CreateProjectPage";
import ProjectDetailPage from "@/pages/ProjectDetailPage";
import AnalyticsPage from "@/pages/AnalyticsPage";
import SettingsPage from "@/pages/SettingsPage";
import AssetLibraryPage from "@/pages/AssetLibraryPage";
import AdminUsersPage from "@/pages/AdminUsersPage";
import PublicSharePage from "@/pages/PublicSharePage";
import ForgotPasswordPage from "@/pages/ForgotPasswordPage";
import ResetPasswordPage from "@/pages/ResetPasswordPage";

function Protected({ children, adminOnly = false }) {
  const { user } = useAuth();
  if (user === null) {
    return (
      <div className="min-h-screen bg-[#0A0A0A] text-zinc-500 font-mono text-sm flex items-center justify-center">
        Loading…
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  if (adminOnly && user.role !== "admin") return <Navigate to="/app" replace />;
  return children;
}

function GuestOnly({ children }) {
  const { user } = useAuth();
  if (user === null) {
    return (
      <div className="min-h-screen bg-[#0A0A0A] text-zinc-500 font-mono text-sm flex items-center justify-center">
        Loading…
      </div>
    );
  }
  if (user) return <Navigate to="/app" replace />;
  return children;
}

export default function App() {
  return (
    <div className="App">
      <AuthProvider>
        <ConfirmProvider>
          <BrowserRouter>
          <Routes>
            <Route path="/" element={<LandingPage />} />
            <Route path="/s/:token" element={<PublicSharePage />} />
            <Route path="/login" element={<GuestOnly><LoginPage /></GuestOnly>} />
            <Route path="/register" element={<GuestOnly><RegisterPage /></GuestOnly>} />
            <Route path="/forgot-password" element={<GuestOnly><ForgotPasswordPage /></GuestOnly>} />
            <Route path="/reset-password" element={<GuestOnly><ResetPasswordPage /></GuestOnly>} />

            <Route path="/app" element={<Protected><DashboardPage /></Protected>} />
            <Route path="/app/projects" element={<Protected><ProjectsPage /></Protected>} />
            <Route path="/app/projects/new" element={<Protected><CreateProjectPage /></Protected>} />
            <Route path="/app/projects/:id" element={<Protected><ProjectDetailPage /></Protected>} />
            <Route path="/app/analytics" element={<Protected><AnalyticsPage /></Protected>} />
            <Route path="/app/settings" element={<Protected><SettingsPage /></Protected>} />
            <Route path="/app/assets" element={<Protected><AssetLibraryPage /></Protected>} />
            <Route path="/app/admin/users" element={<Protected adminOnly><AdminUsersPage /></Protected>} />

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          <Toaster
            theme="dark"
            position="top-right"
            toastOptions={{
              style: {
                background: "#121212",
                border: "1px solid #27272A",
                color: "#fff",
                borderRadius: 2,
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: 12,
              },
            }}
          />
        </BrowserRouter>
        </ConfirmProvider>
      </AuthProvider>
    </div>
  );
}
