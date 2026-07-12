import React from "react";
import { NavLink, useNavigate } from "react-router-dom";
import {
  LayoutDashboard, Plus, FolderKanban, Settings as SettingsIcon,
  LogOut, Users, BarChart3, Boxes, ShieldCheck, Video,
} from "lucide-react";
import { useAuth } from "../lib/auth";

const NAV_ITEMS = [
  { to: "/app", icon: LayoutDashboard, label: "Dashboard", end: true, testId: "nav-dashboard" },
  { to: "/app/client-dashboard", icon: Video, label: "Client Dashboard", testId: "nav-client-dashboard" },
  { to: "/app/projects", icon: FolderKanban, label: "Projects", testId: "nav-projects" },
  { to: "/app/projects/new", icon: Plus, label: "Create Project", testId: "nav-create" },
  { to: "/app/analytics", icon: BarChart3, label: "Analytics", testId: "nav-analytics" },
  { to: "/app/assets", icon: Boxes, label: "Asset Library", testId: "nav-assets" },
  { to: "/app/settings", icon: SettingsIcon, label: "Settings", testId: "nav-settings" },
];

export default function Sidebar() {
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
      className="fixed left-0 top-0 bottom-0 w-60 border-r border-zinc-800 bg-[#0A0A0A] flex flex-col z-40"
    >
      <div className="h-16 flex items-center gap-3 px-5 border-b border-zinc-800">
        <div
          className="w-8 h-8 flex items-center justify-center bg-[#00E5FF] text-black font-black font-mono text-lg"
          style={{ clipPath: "polygon(0 0, 100% 0, 100% 70%, 85% 100%, 0 100%)" }}
        >
          F
        </div>
        <div className="flex flex-col leading-none">
          <span className="font-semibold tracking-tight">FacelessForge</span>
          <span className="font-mono text-[9px] text-zinc-500 tracking-widest uppercase mt-1">
            Creator OS
          </span>
        </div>
      </div>

      <nav className="flex-1 py-6 px-3 space-y-1">
        {items.map(({ to, icon: Icon, label, end, testId }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            data-testid={testId}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 text-sm rounded-sm border transition-colors ${
                isActive
                  ? "bg-[#121212] border-zinc-800 text-white"
                  : "border-transparent text-zinc-400 hover:text-white hover:bg-[#121212]"
              }`
            }
          >
            {({ isActive }) => (
              <>
                <Icon
                  size={16}
                  strokeWidth={1.5}
                  style={{ color: isActive ? "#00E5FF" : undefined }}
                />
                <span>{label}</span>
              </>
            )}
          </NavLink>
        ))}
      </nav>

      <div className="border-t border-zinc-800 p-3 space-y-3">
        {user && (
          <div className="px-2">
            <div className="text-sm text-white truncate">{user.name}</div>
            <div className="font-mono text-[10px] text-zinc-500 uppercase tracking-widest">
              {user.role}
            </div>
          </div>
        )}
        <button
          data-testid="logout-btn"
          onClick={handleLogout}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm text-zinc-400 hover:text-white hover:bg-[#121212] border border-transparent hover:border-zinc-800 rounded-sm transition-colors"
        >
          <LogOut size={14} strokeWidth={1.5} /> Sign out
        </button>
      </div>
    </aside>
  );
}
