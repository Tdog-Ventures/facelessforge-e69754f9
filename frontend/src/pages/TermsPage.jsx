import React from "react";
import { Link } from "react-router-dom";

export default function TermsPage() {
  return (
    <div className="min-h-screen bg-[#0f141f] text-slate-100">
      <header className="flex items-center justify-between px-8 py-5 border-b border-[rgba(59,130,246,0.15)] bg-[#0f141f]/80 backdrop-blur">
        <Link to="/" className="flex items-center gap-3">
          <div className="w-8 h-8 flex items-center justify-center bg-blue-600 text-white font-black font-mono text-lg rounded-md">F</div>
          <span className="font-semibold tracking-tight">FacelessForge</span>
        </Link>
        <Link to="/app" className="text-sm text-slate-400 hover:text-white">Open App</Link>
      </header>
      <main className="max-w-3xl mx-auto px-8 py-12 space-y-8">
        <div className="font-mono text-[11px] tracking-[0.2em] text-blue-400 uppercase">Terms of Service · 2026-09-09</div>
        <h1 className="text-3xl font-bold tracking-tight">Terms of Service</h1>
        <p className="text-sm text-slate-400">By using <code className="text-blue-400">facelessforge.ethinx.solutions</code> you agree to these terms. If you do not agree, do not use the service.</p>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">1. Service</h2>
          <p className="text-sm text-slate-300 leading-relaxed">FacelessForge generates faceless YouTube drafts (hooks, scripts, scenes, metadata, thumbnails) and optional renders (ffmpeg 1080×1920, ElevenLabs voiceover, Pexels stock, captions burned). Drafts are provided as-is for your review before you publish to YouTube. We do not auto-publish unless you enable <code className="text-blue-400">AUTO_POST_ENABLED=true</code> and connect YouTube OAuth (`YOUTUBE_CLIENT_ID`).</p>
        </section>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">2. Accounts & invite</h2>
          <p className="text-sm text-slate-300 leading-relaxed">Registration requires email + password (bcrypt). Roles: <code>creator</code> (create/render), <code>viewer</code> (read-only), <code>admin</code> (diagnostics). Current invite is <code>FORGE</code> (`ALLOW_INVITE_BYPASS=true`, `REACT_APP_DISABLE_WAITLIST=true`). We may close the bypass without notice.</p>
        </section>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">3. Billing — Dodo disclosure</h2>
          <div className="border border-amber-500/20 bg-amber-500/5 rounded-md p-4 text-sm space-y-2">
            <p><b>Provider:</b> Dodo Payments (current, `DODO_PAYMENTS_ENVIRONMENT=test_mode`, products `pdt_starter`/`pdt_creator`/`pdt_pro`/`pdt_scale` → test credits `100/500/2000/10000`). Dodo Payments checkout has been retired and is no longer offered. Finite credits are granted via webhook `POST /api/billing/dodo/webhook` → Redis `tenant:id` hash plus the Mongo ledger (`subscriptions`/`ff_credits`).</p>
            <p><b>No auto-charge without checkout:</b> You are charged only after you complete Dodo (`PAYMENT_PROVIDER=dodo`) checkout and redirect to `/billing/success`. Credits are finite and gated per render.</p>
            <p><b>Refunds:</b> Contact via billing portal or <code>tdogga14@gmail.com</code> within 14 days for unused credits. Rendered minutes consumed are non-refundable.</p>
            <p><b>Tax:</b> Dodo handles VAT/GST; invoices available in the provider dashboard.</p>
          </div>
        </section>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">4. Acceptable use & content safety</h2>
          <ul className="list-disc list-inside text-sm text-slate-300 space-y-1">
            <li>Do not generate NSFW, hate, self-harm, or disallowed content. Current `generate-script` has <b>no automated NSFW filter</b> — you are responsible for prompt and review. A future `strip_beat_markers` + keyword blocklist is planned; until then, violates may be removed and account suspended.</li>
            <li>Do not publish copyrighted text verbatim via script. Stock via Pexels/Pixabay/Unsplash is licensed for YouTube (see <Link to="/license" className="text-blue-400 hover:underline">/license</Link>), but you must verify attribution and that the clip is not trademarked.</li>
            <li>YouTube compliance: auto-publish requires you to comply with <a href="https://www.youtube.com/t/terms" target="_blank" rel="noreferrer" className="text-blue-400 hover:underline">YouTube Terms</a>, <a href="https://developers.google.com/youtube/terms/api-services-terms-of-service" target="_blank" rel="noreferrer" className="text-blue-400 hover:underline">API Services ToS</a>, and handle `quotaExceeded` (100 uploads/day throttle at `backend/app/routes/youtube_oauth.py` `100/hour`). We throttle and mock when `YOUTUBE_MOCK=true`.</li>
          </ul>
        </section>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">5. IP & license</h2>
          <p className="text-sm text-slate-300 leading-relaxed">You retain rights to prompts, scripts, and renders you create. FacelessForge retains rights to the app, `hero-glitch.mp4`, `fallback-thumb.png`, and pipeline code. Pexels assets remain under Pexels License (see `/license`). Generated thumbnails via Gemini Nano Banana (`THUMBNAIL_IMAGE_PROVIDER=gemini_nano_banana`) are mock in current build (`USE_MOCK_THUMBNAIL_IMAGES=false` but `emergentintegrations` missing → fallback `mock: true`).</p>
        </section>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">6. Disclaimer & liability</h2>
          <p className="text-sm text-slate-300 leading-relaxed">Service is provided as-is without warranty. Not liable for YouTube demonetization, copyright strikes, or Pexels attribution errors. Max liability is fees paid in last 30 days. Australian law (South Australia, Adelaide) applies.</p>
        </section>

        <div className="pt-6 border-t border-[rgba(59,130,246,0.15)] flex gap-4 text-xs font-mono">
          <Link to="/privacy" className="text-blue-400 hover:underline">Privacy</Link>
          <Link to="/license" className="text-blue-400 hover:underline">License</Link>
          <Link to="/" className="text-slate-500 hover:text-white">← Home</Link>
        </div>
      </main>
    </div>
  );
}
