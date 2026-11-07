import React, { useState } from "react";
import Sidebar from "./Sidebar";
import { Menu, X } from "lucide-react";

export default function AppShell({ children }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="min-h-screen bg-[var(--ff-bg)] text-[var(--ff-text)]">
      <Sidebar open={open} onClose={() => setOpen(false)} />
      {/* overlay for mobile when open */}
      {open && (
        <div
          className="fixed inset-0 bg-black/50 z-30 md:hidden"
          onClick={() => setOpen(false)}
          aria-hidden
        />
      )}
      {/* hamburger button - visible on mobile only */}
      <button
        data-testid="sidebar-toggle"
        onClick={() => setOpen(!open)}
        className="fixed top-3 left-3 z-40 md:hidden bg-[var(--ff-surface)] border border-[var(--ff-border)] p-2 rounded-md text-[var(--ff-text)]"
        aria-label="Toggle sidebar"
      >
        {open ? <X size={18} /> : <Menu size={18} />}
      </button>
      <div className={`min-h-screen flex flex-col transition-all duration-200 ${open ? "ml-0 md:ml-60" : "ml-0 md:ml-60"} w-full md:w-auto`}>
        {/* when sidebar closed on mobile, main takes w-full; sidebar uses translateX(-100%) so no overlap */}
        <div className="md:ml-0 ml-0 w-full">
          {children}
        </div>
      </div>
    </div>
  );
}
