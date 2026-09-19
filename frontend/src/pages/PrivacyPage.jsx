import React from "react";
import { Link } from "react-router-dom";

export default function PrivacyPage() {
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
        <div className="font-mono text-[11px] tracking-[0.2em] text-blue-400 uppercase">Privacy Policy · Last updated 2026-09-09</div>
        <h1 className="text-3xl font-bold tracking-tight">Privacy Policy</h1>
        <p className="text-sm text-slate-400 leading-relaxed">FacelessForge ("we", "us") operates at <code className="text-blue-400">facelessforge.ethinx.solutions</code> and is hosted on Hetzner (Germany) with Cloudflare R2 (auto) and MongoDB on localhost. This policy explains what we collect, why, and how we handle third-party services you authorize.</p>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">1. Data we collect</h2>
          <ul className="list-disc list-inside text-sm text-slate-300 space-y-1">
            <li><b>Account:</b> name, email, password hash (bcrypt), role (creator/viewer/admin) — stored in Mongo `users`.</li>
            <li><b>Projects:</b> topics, scripts, scenes, metadata, thumbnail briefs — you own this content.</li>
            <li><b>Renders:</b> MP4s stored in R2 `facelessforge-prod` bucket (`https://videos.ethinx.solutions`) and locally under `backend/static/renders/` (retention 6h via `RENDER_RETENTION_INTERVAL_SECONDS=21600`).</li>
            <li><b>Billing:</b> email + plan via Paddle (`pdl_live_...`) or Dodo Payments (`pdt_*`) — we store only `tenant:id` credits in Redis `127.0.0.1:6379` and `ff_credits`/`subscriptions` in Mongo; card data stays with the provider.</li>
            <li><b>Usage:</b> waitlist emails, `provider_settings` (tone, white-label), `analytics/overview` aggregates.</li>
            <li><b>YouTube (if you connect):</b> OAuth refresh token encrypted (`YOUTUBE_ENCRYPTION_KEY`) in Mongo `youtube_tokens`; we request `youtube.upload` scope only when you enable auto-publish.</li>
          </ul>
        </section>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">2. Third parties & disclosure</h2>
          <div className="border border-[rgba(59,130,246,0.15)] bg-[#1c2333] rounded-md p-4 space-y-2 text-sm">
            <p><b>Pexels / Pixabay / Unsplash</b> — stock search proxies via `backend/app/stock.py` using your query + `PEXELS_API_KEY`. Results include `photographer` + `attribution_url` (e.g. `https://www.pexels.com/@photographer`). See <Link to="/license" className="text-blue-400 hover:underline">/license</Link> for license terms.</p>
            <p><b>YouTube Data API v3</b> — auto-publish uses `youtube.upload` + `youtube` scope; we never read your watch history. You can disconnect at `/app/settings` → `POST /api/youtube-oauth/disconnect`. YouTube’s Terms apply: <a href="https://www.youtube.com/t/terms" className="text-blue-400 hover:underline" target="_blank" rel="noreferrer">youtube.com/t/terms</a> and Google Privacy: <a href="https://policies.google.com/privacy" className="text-blue-400 hover:underline" target="_blank" rel="noreferrer">policies.google.com/privacy</a>.</p>
            <p><b>ElevenLabs</b> — TTS via `https://api.elevenlabs.io/v1/text-to-speech/voice_id` with `ELEVENLABS_API_KEY`; text is sent for synthesis, audio returned as MP3 stored in R2 `audio/project/*.mp3`.</p>
            <p><b>Dodo Payments</b> (`DODO_PAYMENTS_ENVIRONMENT=test_mode` with `REACT_APP_DODO_PRODUCT_*=pdt_*`) and <b>Paddle</b> (`pdl_live_...` prices `pri_01...`) — they handle checkout; we receive only `email`, `plan`, `transaction_id` via webhook and grant finite credits (`tenant:id` Redis hash).</p>
            <p><b>Ollama</b> — local LLM at `http://localhost:11434` (`llama3:latest`, `qwen2.5:7b-instruct`) — prompts never leave Hetzner when Ollama succeeds; fallback DeepSeek/Kimi/Claude only if Ollama times out (20s).</p>
          </div>
        </section>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">3. Data retention</h2>
          <ul className="list-disc list-inside text-sm text-slate-300 space-y-1">
            <li>Projects/scripts/scenes/metadata: until you delete project (`DELETE /api/projects/id`) — we hard-delete from Mongo.</li>
            <li>Renders: local `_work_job` cleaned after mux; final MP4 kept in R2 until you delete or 6h retention sweep (`backend/app/retention.py` via `RENDER_RETENTION_INTERVAL_SECONDS`).</li>
            <li>Voiceovers/thumbnails: same as renders.</li>
            <li>Billing audit: `audit:dodo:webhook_id` 30d (`2592000s`) + `tenant:id:billing_audit` capped 999.</li>
            <li>Logs: `journalctl -u facelessforge` retained by systemd journal (default ~4G).</li>
            <li>Backups: daily at 03:00 via /usr/local/bin/facelessforge-backup.sh → /var/backups/facelessforge (env, mongo.tgz, redis) — 30d env/mongo, 7d redis, R2 versioned.</li>
          </ul>
        </section>

        <section className="space-y-3">
          <h2 className="text-lg font-semibold">4. Your rights & contact</h2>
          <p className="text-sm text-slate-300 leading-relaxed">You can export (`/api/projects/id/export/package.zip`) or delete your data. For access/deletion requests contact <code className="text-blue-400">tdogga14@gmail.com</code> (ADMIN_EMAIL) or use `/app/settings`. Host location: Adelaide, South Australia (company) — servers in Hetzner Germany (GDPR-aware DPA on request).</p>
        </section>

        <div className="pt-6 border-t border-[rgba(59,130,246,0.15)] flex gap-4 text-xs font-mono">
          <Link to="/terms" className="text-blue-400 hover:underline">Terms</Link>
          <Link to="/license" className="text-blue-400 hover:underline">License</Link>
          <Link to="/" className="text-slate-500 hover:text-white">← Home</Link>
        </div>
      </main>
    </div>
  );
}
