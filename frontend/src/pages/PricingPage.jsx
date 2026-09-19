import React, { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { Check, Loader2, Crown, Zap, Building } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "../lib/auth";
import { createDodoCheckout, DODO_PRODUCT_IDS } from "../lib/dodo";
import { api, BACKEND_URL } from "../lib/api";
import AppShell from "../components/AppShell";
import TopBar from "../components/TopBar";

const FALLBACK_PLANS = [
  {
    key: "basic",
    name: "Basic",
    priceMonthly: "$29",
    priceYearly: "$290",
    credits: 150,
    desc: "150 credits, 10 clips/30s scene, 3s each",
    icon: Zap,
    product_id: DODO_PRODUCT_IDS.basic,
    features: ["150 credits / mo", "10 clips per 30s scene", "3s each asset", "14d trial · monthly/yearly"],
  },
  {
    key: "pro",
    name: "Pro",
    priceMonthly: "$79",
    priceYearly: "$790",
    credits: 600,
    desc: "600 credits, 4K, all features",
    icon: Crown,
    hero: true,
    product_id: DODO_PRODUCT_IDS.pro,
    features: ["600 credits / mo", "4K assets", "Priority stock", "14d trial"],
  },
  {
    key: "advanced",
    name: "Advanced",
    priceMonthly: "$299",
    priceYearly: "$2990",
    credits: 2000,
    desc: "2000 credits + white-label",
    icon: Building,
    product_id: DODO_PRODUCT_IDS.advanced,
    features: ["2000 credits / mo", "White-label", "Team seats", "14d trial"],
  },
];

export default function PricingPage() {
  const [yearly, setYearly] = useState(false);
  const [loading, setLoading] = useState(null);
  const [plans, setPlans] = useState(FALLBACK_PLANS);
  const [providerInfo, setProviderInfo] = useState(null);
  const { user } = useAuth();

  useEffect(() => {
    let cancelled = false;
    async function loadRealProducts() {
      try {
        // Pull real Paddle products from backend — not hardcoded Stripe
        // Primary: /billing/status (Paddle PLANS) and /api/billing/provider (provider flag)
        const [statusRes, providerRes] = await Promise.allSettled([
          fetch(`${BACKEND_URL}/billing/status`, { credentials: "include" }).then(r => r.json()),
          api.get("/billing/provider").then(r => r.data).catch(() => null),
        ]);
        const statusData = statusRes.status === "fulfilled" ? statusRes.value : null;
        const providerData = providerRes.status === "fulfilled" ? providerRes.value : null;
        if (!cancelled && providerData) setProviderInfo(providerData);
        if (!cancelled && statusData && statusData.plans) {
          const iconMap = { basic: Zap, pro: Crown, advanced: Building };
          const realPlans = Object.entries(statusData.plans)
            .filter(([k]) => ["basic", "pro", "advanced"].includes(k))
            .map(([key, v]) => ({
              key,
              name: v.label || key,
              priceMonthly: `$${v.price}`,
              priceYearly: v.pri_yearly ? `$${Math.round(v.price * 10)}` : `$${v.price * 10}`,
              credits: v.credits,
              desc: v.desc || `${v.credits} credits`,
              icon: iconMap[key] || Zap,
              hero: key === "pro",
              product_id: v.price_id || v.product_id || FALLBACK_PLANS.find(f => f.key === key)?.product_id,
              features: v.desc ? [v.desc, `${v.credits} credits / mo`] : FALLBACK_PLANS.find(f => f.key === key)?.features || [],
            }));
          if (realPlans.length) setPlans(realPlans);
        }
      } catch {}
    }
    loadRealProducts();
    return () => { cancelled = true; };
  }, []);

  const handleCheckout = async (plan) => {
    setLoading(plan.key);
    try {
      const { checkout_url } = await createDodoCheckout(plan.product_id, 1);
      toast.success(`Redirecting to checkout for ${plan.name} ${yearly ? "Yearly" : "Monthly"}`);
      window.location.href = checkout_url;
    } catch (e) {
      const msg = e?.response?.data?.detail || e?.message || String(e);
      toast.error("Checkout failed", { description: msg });
    } finally {
      setLoading(null);
    }
  };

  return (
    <AppShell>
      <TopBar title="Pricing" subtitle={`${providerInfo?.provider || "Paddle"} · 3 plans · 14d trial`} />
      <div className="p-8 space-y-6">
        <div className="max-w-5xl mx-auto text-center space-y-3">
          <h1 className="text-3xl font-bold tracking-tight">Choose your plan</h1>
          <p className="text-zinc-400 text-sm">{providerInfo ? `${providerInfo.provider} · live` : "Paddle"} · 3 plans · 14d trial · credits for 3s assets {providerInfo?.provider ? `· ${providerInfo.provider}` : ""}</p>
          <div className="flex items-center justify-center gap-3 pt-2">
            <span className={`text-xs ${!yearly ? "text-white" : "text-zinc-500"}`}>Monthly</span>
            <button
              data-testid="billing-toggle"
              onClick={() => setYearly(!yearly)}
              className={`w-12 h-6 rounded-full p-1 transition-colors ${yearly ? "bg-[#00E5FF]" : "bg-zinc-800"}`}
            >
              <div className={`w-4 h-4 bg-white rounded-full transition-transform ${yearly ? "translate-x-6" : ""}`} />
            </button>
            <span className={`text-xs ${yearly ? "text-white" : "text-zinc-500"}`}>Yearly</span>
          </div>
        </div>

        <div className="max-w-5xl mx-auto grid grid-cols-1 md:grid-cols-3 gap-4">
          {plans.map((p) => (
            <div key={p.key} data-testid={`pricing-card-${p.key}`} className={`border rounded-sm p-6 space-y-4 flex flex-col ${p.hero ? "border-[#00E5FF] bg-[#00E5FF]/5 shadow-lg" : "border-zinc-800 bg-[#121212]"}`}>
              <div className="flex items-center gap-2">
                <p.icon size={18} className={p.hero ? "text-[#00E5FF]" : "text-zinc-400"} strokeWidth={1.5} />
                <h3 className="font-semibold">{p.name}</h3>
                {p.hero && <span className="ml-auto text-[9px] uppercase bg-[#00E5FF] text-black px-1.5 py-0.5 rounded-sm">Most Popular</span>}
              </div>
              <div className="flex items-baseline gap-1">
                <span className="text-3xl font-bold">{yearly ? p.priceYearly : p.priceMonthly}</span>
                <span className="text-xs text-zinc-500">/{yearly ? "yr" : "mo"}</span>
              </div>
              <div className="text-xs text-zinc-400">{p.desc}</div>
              <div className="font-mono text-[11px] text-[#00FF66]">{p.credits} credits</div>
              <ul className="space-y-2 pt-2 border-t border-zinc-800 flex-1">
                {p.features.map((f) => (
                  <li key={f} className="flex items-center gap-2 text-xs text-zinc-300">
                    <Check size={12} className="text-[#00FF66]" strokeWidth={2} /> {f}
                  </li>
                ))}
              </ul>
              <button
                data-testid={`pricing-choose-${p.key}`}
                onClick={() => handleCheckout(p)}
                disabled={!!loading}
                className={`w-full flex items-center justify-center gap-2 font-mono text-[11px] uppercase tracking-widest px-4 py-2 rounded-sm transition-colors disabled:opacity-60 ${p.hero ? "bg-[#00E5FF] text-black hover:bg-[#33EFFF]" : "border border-zinc-700 text-white hover:border-[#00E5FF] hover:text-[#00E5FF]"}`}
              >
                {loading === p.key ? <Loader2 size={12} className="animate-spin" /> : null}
                {yearly ? `Choose ${p.name} Yearly` : `Choose ${p.name}`}
              </button>
            </div>
          ))}
        </div>

        <div className="max-w-5xl mx-auto text-center">
          <Link to="/app/projects" className="text-xs text-zinc-500 hover:text-white">← Back to projects</Link>
        </div>
      </div>
    </AppShell>
  );
}
