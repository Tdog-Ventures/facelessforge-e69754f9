import React, { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Coins, Zap } from "lucide-react";
import { api } from "../lib/api";

const BILLING_ENDPOINTS = ["/billing/credits", "/billing/balance"];

export function authoritativeBilling(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  // Dodo's raw credit hash has neither remaining nor operator_unmetered.
  // It must not replace the account balance, especially for an unmetered admin.
  if (data.remaining == null && data.operator_unmetered !== true && data.quota == null && data.monthly == null) {
    return null;
  }
  return {
    ...data,
    remaining: data.remaining ?? data.credits ?? 0,
    quota: data.quota ?? data.monthly ?? 150,
    credits: data.credits ?? data.remaining ?? 0,
    reset_at: data.reset_at ?? data.current_period_end ?? null,
    can_generate: data.can_generate ?? (data.remaining ?? data.credits ?? 0) > 0,
    monthly: data.monthly ?? data.quota ?? 150,
    plan: data.plan ?? "free",
    operator_unmetered: data.operator_unmetered === true,
  };
}

export default function BillingBar() {
  const [credits, setCredits] = useState(null);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState(false);
  const requestSeq = useRef(0);

  useEffect(() => {
    let alive = true;
    const fetchBilling = async () => {
      const seq = ++requestSeq.current;
      let normalized = null;
      for (const ep of BILLING_ENDPOINTS) {
        try {
          const res = await api.get(ep, { timeout: 15000 });
          normalized = authoritativeBilling(res?.data);
          if (normalized) break;
        } catch { /* try the next authoritative endpoint */ }
      }
      if (!alive || seq !== requestSeq.current) return;
      if (!normalized) {
        setUnavailable(true);
        setLoading(false);
        return;
      }
      setCredits(normalized);
      setUnavailable(false);
      setLoading(false);
    };
    fetchBilling();
    const id = setInterval(fetchBilling, 30000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (loading) {
    return <div className="font-mono text-[10px] text-[var(--ff-text-muted)]">credits…</div>;
  }
  if (unavailable || !credits) {
    return <div className="font-mono text-[10px] text-amber-300" data-testid="billing-unavailable">Balance unavailable</div>;
  }
  const remaining = credits?.remaining ?? credits?.credits ?? 0;
  const monthly = credits?.monthly ?? credits?.quota ?? 150;
  const plan = credits?.plan ?? "free";
  const resetAt = credits?.reset_at ?? credits?.current_period_end ?? null;
  const pct = monthly > 0 ? Math.round((remaining / monthly) * 100) : 0;
  const exhausted = !credits?.operator_unmetered && remaining <= 0;
  const low = !credits?.operator_unmetered && (exhausted || pct < 20);
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
      <div className={`flex items-center gap-2 px-2 py-1 rounded-md border flex-1 max-w-[420px] ${exhausted ? "border-red-500/40 bg-red-500/10 text-red-400" : low ? "border-amber-500/40 bg-amber-500/10 text-amber-400" : "border-[rgba(59,130,246,0.15)] bg-[var(--ff-surface)] text-[var(--ff-text)]"}`}>
        <Coins size={12} strokeWidth={1.5} />
        <span data-testid="billing-credits">{credits?.operator_unmetered ? "Admin production access" : `${remaining}/${monthly} credits`}</span>
        <span className="text-[9px] uppercase tracking-widest px-1 py-0.5 bg-[var(--ff-surface-2)] text-[var(--ff-text-muted)] rounded-sm">{plan}</span>
        <div className="flex-1 mx-2 h-1.5 bg-[var(--ff-surface-2)] rounded-full overflow-hidden">
          <div data-testid="billing-progress" className={`h-full ${barColor} transition-all`} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
        </div>
        <span className="text-[10px] text-[var(--ff-text-muted)]">{pct}%</span>
        {exhausted && resetLabel && <span data-testid="billing-reset" className="text-[9px] text-red-400 ml-1">resets {resetLabel}</span>}
      </div>
      {exhausted ? (
        <Link to="/pricing" data-testid="billing-upgrade" title={`0 left, resets ${resetLabel}`} className="flex items-center gap-1 text-red-400 hover:text-[var(--ff-text)] border border-red-500/30 hover:border-red-500 px-2 py-1 rounded-md transition-colors">
          <Zap size={12} /> 0 left, resets {resetLabel || "next cycle"} →
        </Link>
      ) : low ? (
        <Link to="/pricing" data-testid="billing-upgrade" className="flex items-center gap-1 text-blue-400 hover:text-[var(--ff-text)] border border-blue-500/30 hover:border-blue-500 px-2 py-1 rounded-md transition-colors">
          <Zap size={12} /> Upgrade
        </Link>
      ) : (
        <Link to="/pricing" data-testid="billing-manage" className="text-[var(--ff-text-muted)] hover:text-[var(--ff-text)] text-[10px]">Manage →</Link>
      )}
    </div>
  );
}
