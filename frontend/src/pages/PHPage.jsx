import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Play, Sparkles, Clock, Video } from "lucide-react";
import { api } from "../lib/api";

export default function PHPage() {
  const [demoVideo, setDemoVideo] = useState(null);
  const [loadingVideo, setLoadingVideo] = useState(true);

  useEffect(() => {
    // Try to fetch latest PETS FIX YOUR BRAIN render via public share or fallback to hero
    // For Product Hunt, show golden demo project autoplay muted
    const fallback = "/hero-glitch.mp4";
    let cancelled = false;
    async function fetchDemo() {
      try {
        // Try to find a recent project with PETS in title via public API if available
        // Fallback to known R2 URL from last successful E2E (presigned but we use static)
        // We attempt to fetch via backend health then projects (requires auth, so will fail for anon -> use fallback)
        // Instead, try to fetch via /api/static not needed - just use fallback hero which is golden demo
        // If you have a specific R2 URL, replace here
        const knownGolden = "/hero-glitch.mp4";
        // Attempt to load a real render via unauthenticated public share if any exists
        // We do not have public token, so use hero
        if (!cancelled) {
          setDemoVideo(knownGolden);
        }
      } catch {
        if (!cancelled) setDemoVideo(fallback);
      } finally {
        if (!cancelled) setLoadingVideo(false);
      }
    }
    fetchDemo();
    return () => { cancelled = true; };
  }, []);

  return (
    <div className="min-h-screen bg-[var(--ff-bg)] text-[var(--ff-text)]">
      <header className="flex items-center justify-between px-8 py-5 border-b border-[rgba(59,130,246,0.15)] bg-[var(--ff-bg)]/80 backdrop-blur">
        <Link to="/" className="flex items-center gap-3">
          <div className="w-8 h-8 flex items-center justify-center bg-blue-600 text-white font-black font-mono text-lg rounded-md">F</div>
          <span className="font-semibold tracking-tight">FacelessForge</span>
          <span className="ml-2 text-[10px] tracking-[0.2em] text-amber-400 uppercase border border-amber-500/30 px-2 py-0.5 rounded-full">Product Hunt</span>
        </Link>
        <div className="flex items-center gap-3">
          <Link to="/login" className="text-sm text-[var(--ff-text-muted)] hover:text-[var(--ff-text)]">Sign in</Link>
          <Link to="/app/projects/new" data-testid="ph-cta-header" className="bg-blue-600 text-white font-semibold text-sm px-4 py-2 rounded-md hover:bg-blue-500 transition-colors flex items-center gap-2">
            Try Live Demo <ArrowRight size={14} strokeWidth={2} />
          </Link>
        </div>
      </header>

      <section className="max-w-5xl mx-auto px-8 py-16 md:py-20 text-center">
        <div className="font-mono text-[11px] tracking-[0.2em] text-amber-400 uppercase mb-6">
          ▌ Product Hunt Launch · Golden Demo
        </div>
        <h1 className="text-5xl md:text-6xl font-bold tracking-tight leading-[1.05] max-w-3xl mx-auto">
          Create a 30-60s faceless draft in 15 min
        </h1>
        <p className="mt-6 max-w-2xl mx-auto text-[var(--ff-text-muted)] text-base md:text-lg leading-relaxed">
          FacelessForge turns one prompt into hook, script, scenes, metadata and thumbnails — scored and exportable. No templates, no upsells. Just your idea to draft.
        </p>
        <div className="mt-8 flex items-center justify-center gap-3">
          <Link to="/app/projects/new" data-testid="ph-cta-hero" className="bg-blue-600 text-white font-semibold text-sm px-6 py-3 rounded-md hover:bg-blue-500 flex items-center gap-2">
            Try Live Demo <ArrowRight size={14} strokeWidth={2} />
          </Link>
          <span className="text-xs text-[var(--ff-text-muted)] flex items-center gap-2"><Clock size={14} /> 15 min workflow</span>
        </div>

        <div className="mt-10 rounded-xl overflow-hidden border border-amber-500/20 bg-[var(--ff-surface)] aspect-video flex items-center justify-center relative" style={{ boxShadow: "0 0 40px rgba(245,158,11,0.12)" }}>
          {!loadingVideo && demoVideo ? (
            <video
              data-testid="ph-golden-demo"
              autoPlay
              muted
              loop
              playsInline
              poster="/fallback-thumb.png"
              className="w-full h-full object-cover"
              src={demoVideo}
              onError={(e) => { e.currentTarget.poster = "/fallback-thumb.png"; }}
            >
              <source src={demoVideo} type="video/mp4" />
            </video>
          ) : (
            <div className="flex flex-col items-center gap-3 text-[var(--ff-text-muted)]">
              <Video size={32} className="text-amber-400" />
              <span className="text-xs font-mono">Loading golden demo…</span>
            </div>
          )}
          <div className="absolute inset-0 bg-gradient-to-t from-[var(--ff-bg)]/60 to-transparent pointer-events-none" />
          <div className="absolute bottom-6 left-6 right-6 flex items-center justify-between">
            <div className="flex gap-2">
              <span className="bg-amber-500 text-black text-xs px-3 py-1 rounded-full font-mono flex items-center gap-1"><Play size={12} /> PETS FIX YOUR BRAIN</span>
              <span className="bg-[var(--ff-surface)] text-[var(--ff-text)] text-xs px-3 py-1 rounded-full font-mono border border-[rgba(59,130,246,0.15)]">30s · 2 scenes · auto-captions</span>
            </div>
            <span className="bg-blue-600 text-white text-xs px-3 py-1 rounded-full font-mono">Golden Demo</span>
          </div>
        </div>

        <div className="mt-6 grid grid-cols-1 md:grid-cols-3 gap-4 text-left">
          <div className="border border-[rgba(59,130,246,0.15)] bg-[var(--ff-surface)] rounded-md p-4">
            <div className="flex items-center gap-2 text-blue-400 mb-2"><Sparkles size={16} /><span className="text-xs font-mono uppercase tracking-widest">Create</span></div>
            <p className="text-sm text-[var(--ff-text)]">Hook + script + scenes from one topic. Topic-specific search_terms, not generic business template.</p>
          </div>
          <div className="border border-[rgba(59,130,246,0.15)] bg-[var(--ff-surface)] rounded-md p-4">
            <div className="flex items-center gap-2 text-amber-400 mb-2"><Clock size={16} /><span className="text-xs font-mono uppercase tracking-widest">15 min</span></div>
            <p className="text-sm text-[var(--ff-text)]">Voiceover (ElevenLabs), stock (Pexels), thumbnails, captions burned — MP4 with audio ready to download.</p>
          </div>
          <div className="border border-[rgba(59,130,246,0.15)] bg-[var(--ff-surface)] rounded-md p-4">
            <div className="flex items-center gap-2 text-emerald-400 mb-2"><Video size={16} /><span className="text-xs font-mono uppercase tracking-widest">Draft, not publish</span></div>
            <p className="text-sm text-[var(--ff-text)]">Draft for review. You choose thumbnail, voice, assets. No auto-publish, no lock-in.</p>
          </div>
        </div>

        <div className="mt-8 flex items-center justify-center">
          <Link to="/app/projects/new" data-testid="ph-cta-bottom" className="bg-amber-500 text-black font-semibold text-sm px-6 py-3 rounded-md hover:bg-amber-400 flex items-center gap-2">
            Try Live Demo -&gt; /app/projects/new <ArrowRight size={14} strokeWidth={2} />
          </Link>
        </div>
        <p className="mt-4 text-[11px] text-[var(--ff-text-muted)] font-mono">No credit card required for demo. Free credits on sign-up.</p>
      </section>
    </div>
  );
}
