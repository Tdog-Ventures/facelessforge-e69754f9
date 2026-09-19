import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Coins, Zap } from "lucide-react";
import { api } from "../lib/api";

export default function BillingBar() {
  const [credits, setCredits] = useState({ remaining: 0, quota: 150, credits: 0, reset_at: null, can_generate: false, monthly: 150, plan: "free" });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    const fetchBilling = async () => {
      try {
        // Primary: /api/billing/balance (real Dodo per task spec), fallback to status/credits/dodo
        let data = null;
        const endpoints = ["/billing/balance", "/billing/dodo/credits", "/billing/status", "/billing/credits"];
        for (const ep of endpoints) {
          try {
            const res = await api.get(ep);
            if (res?.data && typeof res.data === "object") { data = res.data; break; }
          } catch (_e) { continue; }
        }
        // Guard null / malformed
        if (!data || typeof data !== "object") {
          if (alive) setCredits({ remaining: 0, quota: 150, credits: 0, reset_at: null, can_generate: false, monthly: 150, plan: "free" });
          return;
        }
        // Normalize with optional chaining, never throw
        const normalized = {
          remaining: data?.remaining ?? data?.credits ?? 0,
          quota: data?.quota ?? data?.monthly ?? 150,
          credits: data?.credits ?? data?.remaining ?? 0,
          reset_at: data?.reset_at ?? data?.current_period_end ?? null,
          can_generate: data?.can_generate ?? (data?.remaining ?? data?.credits ?? 0) > 0,
          monthly: data?.monthly ?? data?.quota ?? 150,
          plan: data?.plan ?? "free",
          // keep raw for compat
          ...data,
        };
        if (alive) setCredits(normalized);
      } catch (e) {
        // Never throw — default safe state prevents white screen
        console.warn("BillingBar fetch failed", e?.message || e);
        if (alive) setCredits({ remaining: 0, quota: 150, credits: 0, reset_at: null, can_generate: false, monthly: 150, plan: "free" });
      } finally {
        if (alive) setLoading(false);
      }
    };
    fetchBilling();
    const id = setInterval(async () => {
      try {
        let data = null;
        for (const ep of ["/billing/balance", "/billing/dodo/credits", "/billing/status", "/billing/credits"]) {
          try { const r = await api.get(ep); if (r?.data) { data = r.data; break; } } catch {}
        }
        if (!alive || !data) return;
        const normalized = {
          remaining: data?.remaining ?? data?.credits ?? 0,
          quota: data?.quota ?? data?.monthly ?? 150,
          credits: data?.credits ?? data?.remaining ?? 0,
          reset_at: data?.reset_at ?? data?.current_period_end ?? null,
          can_generate: data?.can_generate ?? (data?.remaining ?? data?.credits ?? 0) > 0,
          monthly: data?.monthly ?? data?.quota ?? 150,
          plan: data?.plan ?? "free",
          ...data,
        };
        setCredits(normalized);
      } catch {}
    }, 30000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (loading) {
    return <div className="font-mono text-[10px] text-zinc-500">credits…</div>;
  }
  const remaining = credits?.remaining ?? credits?.credits ?? 0;
  const monthly = credits?.monthly ?? credits?.quota ?? 150;
  const plan = credits?.plan ?? "free";
  const resetAt = credits?.reset_at ?? credits?.current_period_end ?? null;
  const pct = monthly > 0 ? Math.round((remaining / monthly) * 100) : 0;
  const exhausted = remaining <= 0;
  const low = exhausted || pct < 20;
  const barColor = exhausted ? "bg-red-500" : low ? "bg-amber-500" : pct < 50 ? "bg-blue-500" : "bg-emerald-500";
  const resetLabel = (() => {
    if (!resetAt) return "";
    try {
      const d = new Date(resetAt);
      return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    } catch { return String(resetAt).slice(0,10); }
  })();

  return (
    <div className="flex items-center gap-3 font-mono text-[11px] w-full">
      <div className={`flex items-center gap-2 px-2 py-1 rounded-md border flex-1 max-w-[420px] ${exhausted ? "border-red-500/40 bg-red-500/10 text-red-400" : low ? "border-amber-500/40 bg-amber-500/10 text-amber-400" : "border-[rgba(59,130,246,0.15)] bg-[#1c2333] text-slate-300"}`}>
        <Coins size={12} strokeWidth={1.5} />
        <span data-testid="billing-credits">{remaining}/{monthly} credits</span>
        <span className="text-[9px] uppercase tracking-widest px-1 py-0.5 bg-zinc-800 text-zinc-400 rounded-sm">{plan}</span>
        <div className="flex-1 mx-2 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
          <div data-testid="billing-progress" className={`h-full ${barColor} transition-all`} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
        </div>
        <span className="text-[10px] text-zinc-500">{pct}%</span>
        {exhausted && resetLabel && <span data-testid="billing-reset" className="text-[9px] text-red-400 ml-1">resets {resetLabel}</span>}
      </div>
      {exhausted ? (
        <Link to="/pricing" data-testid="billing-upgrade" title={`0 left, resets ${resetLabel}`} className="flex items-center gap-1 text-red-400 hover:text-white border border-red-500/30 hover:border-red-500 px-2 py-1 rounded-md transition-colors">
          <Zap size={12} /> 0 left, resets {resetLabel || "next cycle"} →
        </Link>
      ) : low ? (
        <Link to="/pricing" data-testid="billing-upgrade" className="flex items-center gap-1 text-blue-400 hover:text-white border border-blue-500/30 hover:border-blue-500 px-2 py-1 rounded-md transition-colors">
          <Zap size={12} /> Upgrade
        </Link>
      ) : (
        <Link to="/pricing" data-testid="billing-manage" className="text-slate-500 hover:text-white text-[10px]">Manage →</Link>
      )}
    </div>
  );
}
