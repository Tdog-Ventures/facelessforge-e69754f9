import React from "react";
import { Link } from "react-router-dom";

export default function LicensePage() {
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
        <div className="font-mono text-[11px] tracking-[0.2em] text-blue-400 uppercase">License & Attribution · 2026-09-09</div>
        <h1 className="text-3xl font-bold tracking-tight">License & Attribution</h1>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">1. Pexels / Pixabay / Unsplash (stock)</h2>
          <div className="border border-emerald-500/20 bg-emerald-500/5 rounded-md p-4 text-sm space-y-2">
            <p>Stock search via <code className="text-emerald-400">backend/app/stock.py</code> proxies <b>Pexels</b> (`https://api.pexels.com/v1/search`, `Authorization: PEXELS_API_KEY`), fallback <b>Pixabay</b> (`PIXABAY_API_KEY`) and <b>Unsplash</b> (`UNSPLASH_ACCESS_KEY`). Each result stores:</p>
            <ul className="list-disc list-inside ml-4 space-y-1">
              <li><code>source: "pexels" | "pixabay" | "unsplash"</code></li>
              <li><code>attribution_name</code> (photographer) + <code>attribution_url</code> (e.g. <code>https://www.pexels.com/@{`photographer`}</code>) + <code>source_url</code> (video/photo page)</li>
            </ul>
            <p><b>Pexels License disclosure:</b> All Pexels photos/videos are free to use on YouTube, no attribution required but appreciated. <b>Do not sell unaltered copies</b>, do not imply endorsement by the photographer, do not use identifiable people in a defamatory way. See <a href="https://www.pexels.com/license/" target="_blank" rel="noreferrer" className="text-emerald-400 hover:underline">pexels.com/license</a>. Pixabay: <a href="https://pixabay.com/service/license/" target="_blank" rel="noreferrer" className="text-emerald-400 hover:underline">pixabay.com/service/license</a>. Unsplash: <a href="https://unsplash.com/license" target="_blank" rel="noreferrer" className="text-emerald-400 hover:underline">unsplash.com/license</a>. FacelessForge shows attribution in asset panel but does not burn it into the MP4 — add credits in YouTube description if you want to exceed the requirement.</p>
            <p><b>Rate limit:</b> Pexels `429` → `pexels_rate_limited` (`stock.py:405`) → mixed fallback to Pixabay/Unsplash → Pillow caption frame. YouTube risk: using many generic `cinematic b-roll` without topic-specific `search_terms` (now fixed via `visual_query: derive_search_terms` + `is_garbage_token` + `truncate_words`) would previously cause copyright mismatch re-uploads.</p>
          </div>
        </section>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">2. YouTube Data API v3 — auto-publish compliance</h2>
          <div className="border border-blue-500/20 bg-blue-500/5 rounded-md p-4 text-sm space-y-2">
            <p>Auto-publish is optional (`AUTO_POST_ENABLED` env, `YOUTUBE_MOCK=false` by default). When you connect at <code>/app/settings</code>, we request OAuth scopes `https://www.googleapis.com/auth/youtube.upload` + `https://www.googleapis.com/auth/youtube` and store `refresh_token` encrypted (`YOUTUBE_ENCRYPTION_KEY`) in Mongo `youtube_tokens`. No other scopes.</p>
            <p><b>Compliance note:</b> You must comply with <a href="https://developers.google.com/youtube/terms/api-services-terms-of-service" target="_blank" rel="noreferrer" className="text-blue-400 hover:underline">YouTube API Services ToS</a>, handle quota (`quotaExceeded` 100 uploads/day, we throttle to 100/hour at `youtube_oauth.py`), and show YouTube attribution where required. You can disconnect anytime via `POST /api/youtube-oauth/disconnect`. Mock mode (`YOUTUBE_MOCK=true` or missing refresh token) still writes `forge_runs` with `is_mock:true` and `yt_mock_*` so billing is not blocked.</p>
            <p>FacelessForge is not affiliated with YouTube. FacelessForge respects <a href="https://www.youtube.com/t/terms" target="_blank" rel="noreferrer" className="text-blue-400 hover:underline">youtube.com/t/terms</a>.</p>
          </div>
        </section>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">3. Billing — Dodo</h2>
          <div className="border border-amber-500/20 bg-amber-500/5 rounded-md p-4 text-sm space-y-2">
            <p><b>Dodo Payments</b> (current, `DODO_PAYMENTS_ENVIRONMENT=test_mode`, products `pdt_starter` `pdt_creator` `pdt_pro` `pdt_scale` → test credits `100/500/2000/10000` via `billing_dodo.py`). Checkout `POST /api/billing/dodo/checkout` (JWT) → `DodoPayments.checkout_sessions.create` → webhook `POST /api/billing/dodo/webhook` verifies Standard Webhooks (`webhook-id/timestamp/signature` via `DODO_PAYMENTS_WEBHOOK_KEY`) and grants finite credits to Redis `tenant:id` plus the Mongo ledger (`subscriptions`/`ff_credits`). Empty `DODO_PAYMENTS_WEBHOOK_KEY` currently skips verify (dev) — must be set in prod.</p>
            <p><b>Dodo Payments</b> (retired) — Dodo Payments checkout and webhook surfaces are unmounted and no longer offered. Historical subscription rows may retain Dodo Payments id fields for audit only.</p>
            <p><b>PayPal shim</b> (`PAYMENT_PROVIDER=dodo` default, so `POST /api/billing/webhook/paypal` returns `dormant` unless `provider=paypal|both`). When active, verifies `paypal-transmission-sig` via `PAYPAL_WEBHOOK_ID/SECRET` and `hincrby tenant:tenant credits` (HMAC, idempotent `set nx`).</p>
            <p><b>Single source of truth concern:</b> 3 stores (`subscriptions` + `ff_credits` + Redis `tenant:id`) exist — status endpoint merges; audit recommends unifying to `subscriptions.findOneAndUpdate` atomic deduct.</p>
          </div>
        </section>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">4. Data retention & app license</h2>
          <ul className="list-disc list-inside text-sm text-slate-300 space-y-1">
            <li>Your prompts/scripts/renders are yours. App code, `hero-glitch.mp4`, `fallback-thumb.png` are © ETHINX/FacelessForge.</li>
            <li>Renders local `_work_*` cleaned after mux; final MP4 kept in R2 until delete or `RENDER_RETENTION_INTERVAL_SECONDS=21600` (6h) sweep.</li>
            <li>See <Link to="/privacy" className="text-blue-400 hover:underline">/privacy</Link> for 30d mongo/30d env/7d redis backup retention at `/var/backups/facelessforge` + `03:00` cron `facelessforge-backup.sh`.</li>
            <li>True cost per 20s video ≈ $0.06 (ElevenLabs $0.05 + LLM $0.001 + R2 $0 + Hetzner CPU $0.001) vs credit price $0.19/credit (Basic) → see BACKUP/RUNBOOK audit.</li>
          </ul>
        </section>

        <div className="pt-6 border-t border-[rgba(59,130,246,0.15)] flex gap-4 text-xs font-mono">
          <Link to="/privacy" className="text-blue-400 hover:underline">Privacy</Link>
          <Link to="/terms" className="text-blue-400 hover:underline">Terms</Link>
          <Link to="/" className="text-slate-500 hover:text-white">← Home</Link>
        </div>
      </main>
    </div>
  );
}
