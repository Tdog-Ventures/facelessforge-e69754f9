import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Toaster } from "sonner";
import "@/App.css";

import { AuthProvider, useAuth } from "@/lib/auth";
import { ConfirmProvider } from "@/components/ConfirmDialog";
import BillingBankTransfer from "@/components/BillingBankTransfer";
import GuestOnly from "@/components/GuestOnly";

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }
  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }
  componentDidCatch(error, info) {
    console.error("FacelessForge ErrorBoundary caught", error, info);
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-[#0A0A0A] text-zinc-300 flex flex-col items-center justify-center p-8 font-mono text-sm">
          <h2 className="text-red-400 text-lg mb-2">Something went wrong</h2>
          <p className="text-zinc-500 text-xs max-w-md text-center mb-4">{String(this.state.error?.message || this.state.error || "Unknown error")}</p>
          <button onClick={() => this.setState({ hasError: false, error: null })} className="border border-zinc-700 text-white px-4 py-1 text-xs hover:border-[#00E5FF] hover:text-[#00E5FF]">Try again</button>
        </div>
      );
    }
    return this.props.children;
  }
}
import LandingPage from "@/pages/LandingPage";
import LoginPage from "@/pages/LoginPage";
import RegisterPage from "@/pages/RegisterPage";
import DashboardPage from "@/pages/DashboardPage";
import AppShell from "@/components/AppShell";
import ClientDashboard from "@/components/ClientDashboard";
import ProjectsPage from "@/pages/ProjectsPage";
import CreateProjectPage from "@/pages/CreateProjectPage";
import ProjectDetailPage from "@/pages/ProjectDetailPage";
import AnalyticsPage from "@/pages/AnalyticsPage";
import SettingsPage from "@/pages/SettingsPage";
import AssetLibraryPage from "@/pages/AssetLibraryPage";
import AdminUsersPage from "@/pages/AdminUsersPage";
import AdminDiagnosticsPage from "@/pages/AdminDiagnosticsPage";
import PublicSharePage from "@/pages/PublicSharePage";
import ForgotPasswordPage from "@/pages/ForgotPasswordPage";
import ResetPasswordPage from "@/pages/ResetPasswordPage";
import PricingPage from "@/pages/PricingPage";
import AutomationPage from "@/pages/AutomationPage";
import PHPage from "@/pages/PHPage";
import PrivacyPage from "@/pages/PrivacyPage";
import TermsPage from "@/pages/TermsPage";
import LicensePage from "@/pages/LicensePage";

function WaitlistGate({ children }) {
  const hasInvite = typeof window !== "undefined" && !!localStorage.getItem("forge_invite");
  if (!hasInvite) return <Navigate to="/" replace />;
  return children;
}

function Protected({ children, adminOnly = false }) {
  const { user } = useAuth();
  // handle ?invite=FORGE on any /app route — sets localStorage before gate check
  if (typeof window !== "undefined") {
    const params = new URLSearchParams(window.location.search);
    const invite = params.get("invite");
    if (invite && invite.toUpperCase() === "FORGE") {
      localStorage.setItem("forge_invite", "FORGE");
      // clean url
      params.delete("invite");
      const clean = window.location.pathname + (params.toString() ? `?${params.toString()}` : "") + window.location.hash;
      window.history.replaceState({}, "", clean);
    }
  }
  if (user === null) {
    return (
      <div className="min-h-screen bg-[#0f141f] text-slate-500 font-mono text-sm flex items-center justify-center">
        Loading…
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  if (adminOnly && user.role !== "admin") return <Navigate to="/app" replace />;
  // waitlist gate for /app — strict per task, but allow bypass if REACT_APP_DISABLE_WAITLIST=true
  const disableGate = process.env.REACT_APP_DISABLE_WAITLIST === "true";
  const hasInvite = typeof window !== "undefined" && !!localStorage.getItem("forge_invite");
  if (!disableGate && !hasInvite && typeof window !== "undefined" && window.location.pathname.startsWith("/app")) {
    return <Navigate to="/" replace />;
  }
  return children;
}

function ProductHuntRedirect() {
  // Global redirect: if utm_source=producthunt or referrer producthunt.com -> /ph
  React.useEffect(() => {
    try {
      const params = new URLSearchParams(window.location.search);
      const utm = (params.get("utm_source") || params.get("utm_medium") || "").toLowerCase();
      const isPHParam = utm.includes("producthunt") || params.get("ref") === "producthunt" || params.has("ph");
      const isPHReferrer = typeof document !== "undefined" && document.referrer && document.referrer.toLowerCase().includes("producthunt.com");
      const alreadyPH = window.location.pathname === "/ph";
      if (!alreadyPH && (isPHParam || isPHReferrer)) {
        window.history.replaceState({}, "", "/ph" + window.location.search + window.location.hash);
        window.location.replace("/ph");
      }
    } catch {}
  }, []);
  return null;
}

export default function App() {
  return (
    <div className="App">
      <AuthProvider>
        <ConfirmProvider>
          <BrowserRouter>
          <ProductHuntRedirect />
          <ErrorBoundary>
          <Routes>
            <Route path="/" element={<LandingPage />} />
            <Route path="/ph" element={<PHPage />} />
            <Route path="/privacy" element={<PrivacyPage />} />
            <Route path="/terms" element={<TermsPage />} />
            <Route path="/license" element={<LicensePage />} />
            <Route path="/s/:token" element={<PublicSharePage />} />
            <Route path="/login" element={<GuestOnly><LoginPage /></GuestOnly>} />
            <Route path="/register" element={<GuestOnly><RegisterPage /></GuestOnly>} />
            <Route path="/forgot-password" element={<GuestOnly><ForgotPasswordPage /></GuestOnly>} />
            <Route path="/reset-password" element={<GuestOnly><ResetPasswordPage /></GuestOnly>} />

            <Route path="/app" element={<Protected><DashboardPage /></Protected>} />
            <Route path="/app/client-dashboard" element={<Protected><AppShell><ClientDashboard /></AppShell></Protected>} />
            <Route path="/app/projects" element={<Protected><ProjectsPage /></Protected>} />
            <Route path="/app/projects/new" element={<Protected><CreateProjectPage /></Protected>} />
            <Route path="/app/projects/:id" element={<Protected><ProjectDetailPage /></Protected>} />
            <Route path="/app/analytics" element={<Protected><AnalyticsPage /></Protected>} />
            <Route path="/app/settings" element={<Protected><SettingsPage /></Protected>} />
            <Route path="/app/assets" element={<Protected><AssetLibraryPage /></Protected>} />
            <Route path="/app/admin/users" element={<Protected adminOnly><AdminUsersPage /></Protected>} />
            <Route path="/app/admin/diagnostics" element={<Protected adminOnly><AdminDiagnosticsPage /></Protected>} />
            <Route path="/pricing" element={<PricingPage />} />
            <Route path="/app/pricing" element={<Protected><PricingPage /></Protected>} />
            <Route path="/app/automation" element={<Protected><AutomationPage /></Protected>} />
            <Route path="/billing" element={<Navigate to="/app/billing" replace />} />
            <Route path="/app/billing" element={<Protected><BillingBankTransfer /></Protected>} />

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          </ErrorBoundary>
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
