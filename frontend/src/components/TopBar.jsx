import React, { useEffect, useState } from "react";
import { Activity, Clock, Sun, Moon } from "lucide-react";
import { useTheme } from "next-themes";
import { api } from "../lib/api";

export default function TopBar({ title, subtitle, right }) {
  const [now, setNow] = useState(new Date());
  const [online, setOnline] = useState(true);
  const { theme = "dark", setTheme, resolvedTheme = "dark" } = useTheme();

  const isLight = resolvedTheme === "light";

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        await api.get("/health");
        if (alive) setOnline(true);
      } catch {
        if (alive) setOnline(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  return (
    <header
      data-testid="topbar"
      className="sticky top-0 z-30 h-16 border-b border-[var(--ff-border)] bg-[var(--ff-surface)]/90 backdrop-blur-xl flex items-center justify-between px-8 pl-14 md:pl-8"
    >
      <div>
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
          {subtitle && (
            <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-widest">
              / {subtitle}
            </span>
          )}
        </div>
      </div>
      <div className="flex items-center gap-4">
        {right}
        {/* Theme toggle: sun (light) / moon (dark) with accessible labels and focus */}
        <div className="flex items-center" role="group" aria-label="Theme selection">
          <button
            data-testid="theme-light"
            aria-label="Select light mode"
            aria-pressed={isLight}
            onClick={() => { try { setTheme("light"); } catch (e) {} }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") { e.preventDefault(); try { setTheme("light"); } catch (e) {} }
              if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); try { setTheme("dark"); } catch (e) {} }
            }}
            tabIndex={0}
            className={`rounded-full p-2 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ff-primary)] focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--ff-bg)] ${isLight ? "bg-[var(--ff-primary)] text-white shadow-md" : "text-[var(--ff-text-muted)] hover:text-[var(--ff-text)] hover:bg-[var(--ff-surface-2)]"}`}
            title="Light mode"
          >
            <Sun size={18} strokeWidth={1.5} aria-hidden="true" />
          </button>
          <button
            data-testid="theme-dark"
            aria-label="Select dark mode"
            aria-pressed={!isLight}
            onClick={() => { try { setTheme("dark"); } catch (e) {} }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") { e.preventDefault(); try { setTheme("dark"); } catch (e) {} }
              if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); try { setTheme("light"); } catch (e) {} }
            }}
            tabIndex={0}
            className={`rounded-full p-2 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ff-primary)] focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--ff-bg)] ${!isLight ? "bg-[var(--ff-primary)] text-white shadow-md" : "text-[var(--ff-text-muted)] hover:text-[var(--ff-text)] hover:bg-[var(--ff-surface-2)]"}`}
            title="Dark mode"
          >
            <Moon size={18} strokeWidth={1.5} aria-hidden="true" />
          </button>
        </div>
        <div className="hidden md:flex items-center gap-2 font-mono text-[11px] text-zinc-500">
          <Activity size={12} strokeWidth={1.5} style={{ color: online ? "var(--ff-success)" : "var(--ff-error)" }} />
          <span>{online ? "SYSTEM · ONLINE" : "SYSTEM · DEGRADED"}</span>
        </div>
        <div className="hidden md:flex items-center gap-2 font-mono text-[11px] text-zinc-500">
          <Clock size={12} strokeWidth={1.5} />
          <span>{now.toUTCString().split(" ").slice(4, 5)[0]} UTC</span>
        </div>
      </div>
    </header>
  );
}
