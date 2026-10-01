import React, { useState, useEffect } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowRight, Sparkles, Zap, Download, Layers } from "lucide-react";
import { toast } from "sonner";
import { api } from "../lib/api";

export default function LandingPage() {
  const [email, setEmail] = useState("");
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();

  // Handle ?invite=FORGE from any landing link (e.g. /?invite=FORGE or /app?invite=FORGE redirected)
  useEffect(() => {
    const invite = searchParams.get("invite");
    if (invite && invite.toUpperCase() === "FORGE") {
      localStorage.setItem("forge_invite", "FORGE");
      toast.success("Invite unlocked — welcome!");
      // clean url but keep landing
      searchParams.delete("invite");
      setSearchParams(searchParams, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  const handleWaitlist = async (e) => {
    e.preventDefault();
    if (!email || !email.includes("@")) {
      toast.error("Enter a valid email");
      return;
    }
    // Try Supabase if configured, fallback to backend /api/waitlist
    const supabaseUrl = process.env.REACT_APP_SUPABASE_URL;
    const supabaseKey = process.env.REACT_APP_SUPABASE_ANON_KEY;
    let saved = false;
    if (supabaseUrl && supabaseKey) {
      try {
        const res = await fetch(`${supabaseUrl}/rest/v1/waitlist`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "apikey": supabaseKey,
            "Authorization": `Bearer ${supabaseKey}`,
            "Prefer": "return=minimal",
          },
          body: JSON.stringify({ email }),
        });
        if (res.ok || res.status === 409) saved = true;
      } catch {}
    }
    if (!saved) {
      try {
        await api.post("/waitlist", { email });
        saved = true;
      } catch (err) {
        // 409 = already exists, treat as success
        if (err?.response?.status === 409) saved = true;
        else {
          // still allow invite even if backend down
          console.warn("waitlist backend failed", err?.message);
        }
      }
    }
    localStorage.setItem("forge_invite", email);
    toast.success(saved ? "You're on the waitlist! Redirecting to app…" : "Invite set — redirecting to app…");
    setTimeout(() => navigate("/app"), 600);
  };

  const hasInvite = typeof window !== "undefined" && !!localStorage.getItem("forge_invite");

  return (
    <div className="min-h-screen bg-[#0f141f] text-slate-100">
      <div className="ff-grid min-h-screen">
        <header className="flex items-center justify-between px-8 py-5 border-b border-[rgba(59,130,246,0.15)] bg-[#0f141f]/80 backdrop-blur">
          <div className="flex items-center gap-3">
            <div
              className="w-8 h-8 flex items-center justify-center bg-blue-600 text-white font-black font-mono text-lg rounded-md"
            >
              F
            </div>
            <span className="font-semibold tracking-tight">FacelessForge</span>
          </div>
          <div className="flex items-center gap-3">
            <Link to="/login" data-testid="landing-login" className="text-sm text-slate-400 hover:text-white">Sign in</Link>
            {hasInvite ? (
              <Link to="/app" data-testid="landing-app" className="bg-blue-600 text-white font-semibold text-sm px-4 py-2 rounded-md hover:bg-blue-500 transition-colors">Open App</Link>
            ) : (
              <Link to="/register" data-testid="landing-register" className="bg-blue-600 text-white font-semibold text-sm px-4 py-2 rounded-md hover:bg-blue-500 transition-colors flex items-center gap-2">
                Get started <ArrowRight size={14} strokeWidth={2} />
              </Link>
            )}
          </div>
        </header>

        {/* Video hero — Create Automate Monetise */}
        <section className="max-w-5xl mx-auto px-8 py-16 md:py-20">
          <div className="font-mono text-[11px] tracking-[0.2em] text-blue-400 uppercase mb-6 ff-rise">
            ▌ Creator Operations · v1 — Create · Automate · Monetise
          </div>
          <h1 className="text-5xl md:text-7xl font-bold tracking-tight leading-[1.05] max-w-4xl ff-rise ff-rise-1">
            Turn any idea into a
            <span className="block text-blue-400">YouTube-ready content package.</span>
          </h1>
          <p className="mt-8 max-w-2xl text-slate-400 text-base md:text-lg leading-relaxed ff-rise ff-rise-2">
            FacelessForge is the control room for faceless YouTube creators.
            One prompt becomes your hook, script, scene plan, metadata, and thumbnail concepts — scored, tracked,
            and exportable in seconds.
          </p>

          {/* Video hero — local glitch mp4 */}
          <div className="mt-10 rounded-xl overflow-hidden border border-[rgba(59,130,246,0.15)] bg-[#1c2333] aspect-video flex items-center justify-center relative" style={{ boxShadow: "0 0 40px rgba(59,130,246,0.08)" }}>
            <video
              data-testid="hero-video"
              autoPlay
              muted
              loop
              playsInline
              poster="/fallback-thumb.png"
              className="w-full h-full object-cover"
              src="/hero-glitch.mp4"
              onError={(e)=>{ e.currentTarget.poster="/fallback-thumb.png"; }}
            >
              <source src="/hero-glitch.mp4" type="video/mp4" />
            </video>
            <div className="absolute inset-0 bg-gradient-to-t from-[#0f141f]/60 to-transparent pointer-events-none" />
            <div className="absolute bottom-6 left-6 flex gap-2">
              <span className="bg-blue-600 text-white text-xs px-3 py-1 rounded-full font-mono">Create</span>
              <span className="bg-[#1c2333] text-slate-200 text-xs px-3 py-1 rounded-full font-mono border border-[rgba(59,130,246,0.15)]">Automate</span>
              <span className="bg-amber-500 text-black text-xs px-3 py-1 rounded-full font-mono">Monetise</span>
            </div>
          </div>

          {/* Waitlist gate */}
          <div className="mt-10 ff-rise ff-rise-3">
            {!hasInvite ? (
              <form onSubmit={handleWaitlist} className="flex flex-col sm:flex-row gap-3 max-w-md">
                <input
                  data-testid="waitlist-email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="Enter email for early access"
                  className="flex-1 bg-[#1c2333] border border-[rgba(59,130,246,0.15)] rounded-md px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500 focus:border-blue-500"
                />
                <button data-testid="waitlist-join" type="submit" className="bg-blue-600 text-white font-semibold text-sm px-6 py-3 rounded-md hover:bg-blue-500 flex items-center gap-2 whitespace-nowrap">
                  Join waitlist <ArrowRight size={14} />
                </button>
              </form>
            ) : (
              <div className="flex items-center gap-3">
                <Link to="/app" data-testid="hero-app" className="bg-blue-600 text-white font-semibold text-sm px-5 py-3 rounded-md hover:bg-blue-500 flex items-center gap-2">
                  Enter App <ArrowRight size={14} strokeWidth={2} />
                </Link>
                <span className="text-xs text-emerald-400 font-mono">✓ Invite active</span>
              </div>
            )}
            <p className="mt-3 text-xs text-slate-500 font-mono">Have an invite? Go to <code className="text-blue-400">/app?invite=FORGE</code></p>
          </div>

          <div className="mt-8 flex items-center gap-3 ff-rise ff-rise-3">
            <Link to="/register" data-testid="hero-register" className="border border-[rgba(59,130,246,0.15)] text-slate-200 text-sm px-5 py-3 rounded-md hover:border-blue-500 hover:text-blue-400 transition-colors">
              Start forging
            </Link>
            <Link to="/login" data-testid="hero-demo" className="text-slate-400 text-sm px-5 py-3 hover:text-white transition-colors">
              Try demo creator
            </Link>
          </div>

          <div className="mt-20 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            {[
              { icon: Sparkles, title: "Create", body: "DeepSeek writes hooks, scripts, scenes, and metadata tuned to your niche." },
              { icon: Layers, title: "Automate", body: "Every project moves through a visible 8-step production pipeline." },
              { icon: Zap, title: "Score", body: "0–100 quality score shows exactly what's missing before publish." },
              { icon: Download, title: "Monetise", body: "Download TXT, CSV, JSON, or a full ZIP package. Your content stays yours." },
            ].map((f, i) => (
              <div
                key={f.title}
                className="border border-[rgba(59,130,246,0.15)] bg-[#1c2333] p-5 rounded-lg ff-rise"
                style={{ animationDelay: `${200 + i * 60}ms`, boxShadow: "0 0 20px rgba(59,130,246,0.05)" }}
              >
                <f.icon size={18} strokeWidth={1.5} className="text-blue-400" />
                <div className="mt-4 font-semibold text-sm text-slate-100">{f.title}</div>
                <div className="mt-2 text-xs text-slate-400 leading-relaxed">{f.body}</div>
              </div>
            ))}
          </div>
        </section>

        <footer className="border-t border-[rgba(59,130,246,0.15)] px-8 py-6 font-mono text-[11px] text-slate-500 uppercase tracking-widest">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <span>FacelessForge · creator operations · navy theme · ethinx.solutions</span>
            <div className="flex gap-4 normal-case tracking-normal">
              <Link to="/privacy" className="hover:text-blue-400 normal-case">Privacy</Link>
              <Link to="/terms" className="hover:text-blue-400 normal-case">Terms</Link>
              <Link to="/license" className="hover:text-blue-400 normal-case">License</Link>
            </div>
          </div>
        </footer>
      </div>
    </div>
  );
}
