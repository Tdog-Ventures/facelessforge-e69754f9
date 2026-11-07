import React from "react";
import { NavLink, useNavigate } from "react-router-dom";
import {
  LayoutDashboard, Plus, FolderKanban, Settings as SettingsIcon,
  LogOut, Users, BarChart3, Boxes, ShieldCheck, Video, CreditCard, Zap,
} from "lucide-react";
import { useAuth } from "../lib/auth";

const NAV_ITEMS = [
  { to: "/app", icon: LayoutDashboard, label: "Dashboard", end: true, testId: "nav-dashboard" },
  { to: "/app/client-dashboard", icon: Video, label: "Client Dashboard", testId: "nav-client-dashboard" },
  { to: "/app/projects", icon: FolderKanban, label: "Projects", testId: "nav-projects" },
  { to: "/app/projects/new", icon: Plus, label: "Create Project", testId: "nav-create" },
  { to: "/app/analytics", icon: BarChart3, label: "Analytics", testId: "nav-analytics" },
  { to: "/pricing", icon: CreditCard, label: "Billing", testId: "nav-billing", badge: "LIVE" },
  { to: "/app/assets", icon: Boxes, label: "Asset Library", testId: "nav-assets" },
  { to: "/app/automation", icon: Zap, label: "Automation", testId: "nav-automation" },
  { to: "/app/settings", icon: SettingsIcon, label: "Settings", testId: "nav-settings" },
];

export default function Sidebar({ open = true, onClose }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  const items = [...NAV_ITEMS];
  if (user && user.role === "admin") {
    items.push({ to: "/app/admin/users", icon: Users, label: "Users", testId: "nav-users" });
    items.push({ to: "/app/admin/diagnostics", icon: ShieldCheck, label: "Diagnostics", testId: "nav-diagnostics" });
  }

  return (
    <aside
      data-testid="sidebar"
      className={`fixed left-0 top-0 bottom-0 w-60 border-r border-[var(--ff-border)] bg-[var(--ff-surface)] flex flex-col z-40 transform transition-transform duration-200
        ${open ? "translate-x-0" : "-translate-x-full"} md:translate-x-0`}
      style={{ boxShadow: "0 0 20px var(--ff-glow)" }}
    >
      <div className="h-16 flex items-center gap-3 px-5 border-b border-[var(--ff-border)]">
        <div
          className="w-8 h-8 flex items-center justify-center bg-[var(--ff-primary)] text-white font-black font-mono text-lg rounded-md"
        >
          F
        </div>
        <div className="flex flex-col leading-none">
          <span className="font-semibold tracking-tight text-[var(--ff-text)]">FacelessForge</span>
          <span className="font-mono text-[9px] text-[var(--ff-text-dim)] tracking-widest uppercase mt-1">
            Creator OS
          </span>
        </div>
      </div>

      <nav className="flex-1 py-6 px-3 space-y-1 overflow-y-auto">
        {items.map(({ to, icon: Icon, label, end, testId, badge }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            data-testid={testId}
            onClick={() => onClose && onClose()}
            className={({ isActive }) => {
              const isBilling = label === "Billing" && typeof window !== "undefined" && (window.location.pathname.startsWith("/pricing") || window.location.pathname.startsWith("/billing") || window.location.pathname.startsWith("/app/pricing"));
              const active = isActive || isBilling;
              return `flex items-center gap-3 px-3 py-2.5 text-sm rounded-md border transition-colors ${
                active
                  ? "bg-[var(--ff-bg)] border-[var(--ff-border-strong)] text-[var(--ff-text)]"
                  : "border-transparent text-[var(--ff-text-muted)] hover:text-[var(--ff-text)] hover:bg-[var(--ff-bg)]"
              }`;
            }}
          >
            {({ isActive }) => {
              const isBilling = label === "Billing" && typeof window !== "undefined" && (window.location.pathname.startsWith("/pricing") || window.location.pathname.startsWith("/billing") || window.location.pathname.startsWith("/app/pricing"));
              const active = isActive || isBilling;
              return (
                <>
                  <Icon
                    size={16}
                    strokeWidth={1.5}
                    className={active ? "text-[var(--ff-primary)]" : ""}
                  />
                  <span className="flex-1">{label}</span>
                  {badge && <span className="font-mono text-[8px] bg-[var(--ff-primary)] text-white px-1.5 py-0.5 rounded tracking-widest">{badge}</span>}
                </>
              );
            }}
          </NavLink>
        ))}
      </nav>

      <div className="border-t border-[var(--ff-border)] p-3 space-y-3">
        {user && (
          <div className="px-2">
            <div className="text-sm text-[var(--ff-text)] truncate">{user.name}</div>
            <div className="font-mono text-[10px] text-[var(--ff-text-dim)] uppercase tracking-widest">
              {user.role}
            </div>
          </div>
        )}
        <button
          data-testid="logout-btn"
          onClick={handleLogout}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm text-[var(--ff-text-muted)] hover:text-[var(--ff-text)] hover:bg-[var(--ff-bg)] border border-transparent hover:border-[var(--ff-border)] rounded-md transition-colors"
        >
          <LogOut size={14} strokeWidth={1.5} /> Sign out
        </button>
      </div>
    </aside>
  );
}
