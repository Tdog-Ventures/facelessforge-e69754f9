# FacelessForge — Product Requirements Document

## Tagline
Turn any idea into a YouTube-ready content package.

## Original Problem Statement
Build a production-ready SaaS web app **FacelessForge**: a creator-first faceless YouTube content generation platform. Users create a video project (niche, topic, audience, tone, duration, voice style, visual style) and generate hook/script/scene-by-scene storyboard/voiceover plan/caption file/thumbnail concepts/YouTube title options/description/tags/pinned comment. Track projects through a production workflow, quality score 0–100, and cost logging. Export TXT/CSV/JSON/ZIP. Dark mode, cyan+purple accents, Inter + JetBrains Mono. RBAC (admin/creator/editor/viewer).

## Architecture
- **Backend**: FastAPI + Motor (MongoDB) + JWT (bcrypt, httpOnly cookies) + emergentintegrations (GPT-5.2 via Emergent LLM key) with deterministic fallback.
- **Frontend**: React 19, React Router 7, shadcn/ui components, sonner toasts, recharts, lucide-react, Tailwind.
- **Data model**: users, projects, scripts, scenes, metadata_packages, assets, render_jobs, cost_logs, provider_settings, login_attempts.
- **File layout** (backend): `server.py` (entry + lifespan) → `app/db.py`, `app/auth.py`, `app/models.py`, `app/generation.py`, `app/scoring.py`, `app/routes.py`, `app/seed.py`.

## Core Requirements
1. Auth: register/login/logout/me/refresh. RBAC: admin, creator, editor, viewer.
2. Projects CRUD scoped by user (admin sees all).
3. Generation pipeline: script → scenes → metadata → thumbnail briefs.
4. Render simulation: validates artefacts, produces READY_TO_RENDER/COMPLETED/FAILED with error messages.
5. Quality score 0–100 recalculated on each read.
6. Exports: TXT (script), CSV (scenes), JSON (metadata), ZIP (full package).
7. Analytics overview for dashboard + analytics page.
8. Settings: default tone/visual style, cost limit, preferred provider.
9. Admin user console to view/change roles.
10. Seed admin + demo creator + 3 sample projects in DRAFT/SCRIPT/SCENES/COMPLETED distribution.

## User Personas
- **Admin** — platform operator, manages users, sees global analytics.
- **Creator** — primary customer; creates projects and runs full pipeline.
- **Editor** — can modify scripts/scenes/metadata but not billing/admin.
- **Viewer** — read-only access to completed projects.

## What's Been Implemented (2026-02)
- [x] Full backend with all endpoints + RBAC + validation
- [x] JWT auth with httpOnly cookies (access + refresh)
- [x] GPT-5.2 generation with deterministic fallback
- [x] Seed (admin + demo creator + 3 projects with real AI content)
- [x] Dashboard with stat cards + recharts bar/line
- [x] Projects list with search and status filter
- [x] Create Project form with client+server validation
- [x] Project Detail with 6 tabs (Overview/Script/Scenes/Metadata/Thumbnails/Render)
- [x] Script editor with hook picker + full-script textarea + save
- [x] Scene planner data table with timings, captions, search terms
- [x] Metadata panel with 10+ title options, description editor, tags, hashtags, chapters, pinned comment
- [x] Thumbnail briefs (3 concepts with composition, emotion, colour direction, click trigger, image prompt)
- [x] Render pipeline visualiser with 7-step progress and validation failure messages
- [x] Analytics page with bar/pie/line charts
- [x] Asset Library cross-project grid
- [x] Settings page (provider, cost limit, defaults)
- [x] Admin Users page (role management)
- [x] Copy-to-clipboard for title, description, tags, pinned comment, script, CTA
- [x] Exports TXT/CSV/JSON/ZIP
- [x] Landing page with dark-mode cinematic hero
- [x] 31/31 backend tests + 100% frontend E2E tests passing

### Phase 2 — 2026-02 (shipping now)
- [x] **Public share links**: toggle per project, tokenised `/s/{token}` public page, read-only, view counter, last-viewed timestamp, title override, regenerate/disable actions, gated on METADATA_GENERATED+, private fields scrubbed. 44/44 backend + 100% frontend tests pass.
- [x] **Forgot-password flow**: POST `/api/auth/forgot-password` (rate-limited, no enumeration, TTL-indexed tokens, dev-mode reset link in response), POST `/api/auth/reset-password` (one-time use, expiry-safe). Dedicated `/forgot-password` + `/reset-password` pages. 54/54 backend tests pass including 10 new forgot-password cases.
- [x] **Deploy hardening**:
  - CORS: allow_origin from `FRONTEND_URL` env + regex fallback for preview; `*` only if no env set
  - Cookies: `SameSite=None; Secure` in prod, `Lax` in dev — auto-detected via `DEV_MODE`
  - Rate-limit now honours `X-Forwarded-For` first-hop (fixes k8s ingress pod IP rotation bypass)
  - Dev reset URL only logged when `DEV_MODE=true`
  - Dark themed `ConfirmDialog` (shadcn alert-dialog) replaces all `window.confirm` calls (regenerate + delete)
  - Recharts ResponsiveContainer width/height warnings eliminated
- [x] **Share page social unfurl**: client-side OG/Twitter meta tags (`og:title`, `og:description`, `og:url`, `og:image`, `twitter:card=summary_large_image`, canonical link) populated from the public share payload; `/og-share-default.svg` branded fallback image served from `public/`. Deferred: true server-side rendering for no-JS crawlers.

### Phase 3 — 2026-02 (shipping now)
- [x] **Pexels stock-footage fetcher** (mock-first, real-key-ready):
  - `app/stock.py` service: `search_stock(query, media_type, per_page)` normalises Pexels photos + videos into one shape; deterministic SHA1-seeded mock when `PEXELS_API_KEY` is missing or `USE_MOCK_PEXELS=true`; graceful fallback to mock on Pexels 429 / network errors.
  - New endpoints: `GET /api/stock/meta` (mock flag), `POST /api/projects/{pid}/stock-search`, `POST /api/projects/{pid}/scenes/{sid}/find-assets`, `POST /api/projects/{pid}/scenes/{sid}/attach-asset` (409 on duplicate by `external_id+source+scene`), `PATCH /api/projects/{pid}/assets/{aid}` status.
  - Asset model extended with: `scene_id`, `external_id`, `preview_url`, `source_url`, `download_url`, `attribution_name/url`, `width`, `height`, `duration`, `query`.
  - `StockAssetModal` (shadcn dialog): auto-seeds query from `scene.search_terms`, All/Videos/Photos tabs, amber "MOCK RESULTS" badge, green "Pexels · LIVE" badge, attach counter, duplicate-safe.
  - Scene Planner rows gain per-scene `Find Assets` button + inline thumbnail chips with hover-to-detach.
  - Asset Library upgraded with preview thumbnails, source badge, photographer attribution, dimensions, linked scene chip, remove action, filter tabs (All / Stock / Thumbnails).
  - 67/67 backend pytest (+13 new) + 100% Playwright passing. Zero critical issues.

### Phase 3.5 + Phase 4 — 2026-02 (shipping now)
- [x] **DB hardening**: compound partial unique index `assets(project_id, scene_id, external_id, source)` where `external_id` exists — closes the double-attach race at the database level without affecting briefs / generated thumbnails.
- [x] **Auto-attach Assets** (one-click bulk): `POST /api/projects/{pid}/auto-attach-assets {replace_existing, media_type}` iterates scenes (search_terms → visual_direction → topic fallback chain), attaches top stock result, returns `{total, attached, skipped, failed, details, mock}`. UI: green "Auto-attach Assets" button on Scenes tab; ConfirmDialog when scenes already have assets (Replace vs Fill-empty); summary toast.
- [x] **Phase 4 — Gemini Nano Banana thumbnail image generation** (mock-first):
  - `app/thumbnail_images.py`: builds rich 16:9 prompt from brief + project (no baked-in text, negative-space hint for title overlay); calls Gemini via `emergentintegrations` LlmChat with `gemini-3.1-flash-image-preview`; falls back to deterministic branded SVG when key missing or `USE_MOCK_THUMBNAIL_IMAGES=true`.
  - Generated images persist as files under `/app/backend/static/thumbs/{project_id}/{asset_id}.{png|svg}`, served via FastAPI `StaticFiles` mount at `/api/static/...` (routes through k8s ingress).
  - Endpoints: `GET /api/thumbnails/meta`, `POST /api/projects/{pid}/thumbnails/{brief_id}/generate {variants:1..3}`, `POST /api/projects/{pid}/thumbnails/{aid}/select` (exclusive per project — demotes prior selected, sets `project.selected_thumbnail_asset_id`), `POST /api/projects/{pid}/thumbnails/{aid}/reject`.
  - Asset extensions: `prompt`, `provider`, `model`, `mock`, `brief_asset_id`, `brief_snapshot`, `preview_path`, status `selected/rejected/generated`.
  - `ThumbnailPanel` upgrade: per-brief Generate Image / 3 Variants buttons; generated tiles in 2-col grid under each concept with hover-revealed Select / Reject / Open / Copy Prompt / Delete; SELECTED green ring + badge; amber MOCK / cyan GENERATED / red REJECTED state badges.
  - **Public share page**: when `selected_thumbnail_asset_id` is set, the public payload includes `selected_thumbnail_url`; the share page renders it as a 16:9 hero above the title and uses it as `og:image` / `twitter:image`. Falls back to `og-share-default.svg`.
  - **81/81 backend pytest** (+14 new across TestAutoAttach, TestDBIndex, TestThumbnailImages) + frontend Playwright verified. Zero issues found.

### Phase 5 — 2026-02 (shipping now)
- [x] **Voiceover TTS** (OpenAI `tts-1` via Emergent LLM key, deterministic mock-WAV fallback):
  - `app/tts.py`: `generate_voiceover(text, voice_style, project_id, asset_id, scene_id)` returns normalised asset dict. 7 voice styles (narrator/energetic/documentary/calm/dramatic/corporate/mysterious) mapped to OpenAI voices; mock WAV = silent PCM at 22050Hz with realistic duration (~150 wpm). `USE_MOCK_TTS=true` in dev; drops to real API when flag flipped + key present. ElevenLabs drop-in ready via `VOICE_STYLE_MAP[...].eleven_voice_hint`.
  - Endpoints: `GET /api/tts/meta`, `POST /api/projects/{pid}/voiceover/generate-script`, `POST /api/projects/{pid}/scenes/{sid}/voiceover/generate`, `POST /api/projects/{pid}/voiceover/{aid}/select`, `POST .../reject`, `DELETE .../voiceover/{aid}` (scrubs file too).
  - Selection model: **both** library + select-one. Full-script exclusivity via `project.selected_voiceover_asset_id`; per-scene exclusivity by auto-demoting prior selected on new scene generation.
  - Audio served at `/api/static/audio/{project_id}/{asset_id}.{wav|mp3}` through k8s ingress.
  - `VoiceoverPanel`: voice-style picker (pill buttons), full-script section (generate/regenerate + card grid with audio player, select, reject, download, delete), per-scene section (per-scene generate + audio card grid). Mock/Live/Selected/Rejected badges. Responsive layout.
  - ScenePlanner: inline mini audio chip on rows where the scene has an active VO (`scene-voiceover-<id>`).
  - Project Overview: 5th tile for voiceover showing `X/Y scenes · full ready`.
  - AssetLibrary: new "Voiceover" filter + audio-card layout with inline `<audio controls>`; delete works for voiceovers.
  - Public share: `selected_voiceover` (preview_url, voice_style, duration only — no provider/cost/file_path leak) rendered as `<audio controls>` preview on `/s/{token}`.
  - ZIP export: new `voiceovers.json` listing all voiceovers with id, scene_id, scene_number, voice_style, duration, provider, mock, status, preview_url, text_excerpt, is_full_script, selected_for_project.
  - **96/96 backend pytest** (+15 new TestVoiceover: meta, auth, full-script mock, default voice fallback, scene generation, demote-prior, unknown-scene 404, cross-user 403, full-script select exclusivity, reject clears pointer, share surfacing + no-leak, ZIP voiceovers.json, delete-removes-file).
  - Frontend Playwright E2E verified: tab mounts, style picker, full/scene generation, select/reject/download/delete, asset library voiceover filter, overview tile, scene planner audio chip, public share player. Zero issues.

### Phase 6 — 2026-02 (shipping now)
- [x] **Real ffmpeg render queue** — produces a downloadable `1920×1080 30fps H.264 + AAC` MP4 from the project package.
  - `app/render.py`: server-built ffmpeg pipeline. Steps: `validating → preparing_assets → rendering (per-scene encoding + concat + audio mux) → completed`. Async background task per project; `_LOCKS` per project_id prevents concurrent renders; `cancel_render` interrupts the task and marks `cancelled`. 10-minute hard timeout (`RENDER_TIMEOUT_SECONDS`).
  - Pipeline:
    1. **Intro**: selected thumbnail (PNG) → 1.5s clip. Mock SVG thumbs / unloadable images fall back to a Pillow-rendered PNG caption frame.
    2. **Scenes**: each scene becomes a normalised 1920×1080 30fps H.264 clip at `end_time - start_time` duration. Source = first attached stock asset (download via httpx with size cap; `image/*` and `video/*` content-types only). On any failure → Pillow caption frame.
    3. **Concat**: ffmpeg concat demuxer (`-c copy`) into a single silent video.
    4. **Audio**: prefer `project.selected_voiceover_asset_id` (full-script); else concat per-scene voiceovers in scene order; else silent AAC track from `anullsrc`.
    5. **Mux + faststart**: `-c:a aac -b:a 192k -movflags +faststart`.
  - Endpoints (all server-builds ffmpeg args; the only body field is `force` — extra fields ignored):
    - `GET /api/projects/{pid}/render/preflight` — checklist (script, scenes, metadata, thumbnail, voiceover, scene_assets) with hints; safe scene_assets coverage warning.
    - `POST /api/projects/{pid}/render/start` — 400 if prereqs unmet, 409 if a job is already active, 200 with `{id, status:queued, ...}` otherwise.
    - `GET /api/projects/{pid}/render/jobs` and `GET /...{job_id}` for polling.
    - `POST /api/projects/{pid}/render/jobs/{job_id}/cancel`.
  - MP4 saved to `/app/backend/static/renders/{project_id}/{job_id}.mp4`, served via existing `/api/static/...` ingress mount. Job persists `output_url`, `output_path` (internal only), `duration`, `file_size`, `progress`, `current_step`, `error_message`.
  - Project `status` flips to `COMPLETED` on success and `rendered_video_asset_id` is set to the latest job id.
  - Security: zero raw ffmpeg args from frontend; downloads capped at 60MB/asset and content-type-filtered; output paths sanitised to project workdir; cross-user `_ensure_project_access` on every endpoint; viewer role blocked at write endpoints; concurrent-render guard via DB + asyncio Lock.
  - `RenderPanel` (full rewrite): prerequisite checklist with green/red tiles + hints, intro thumbnail + voiceover audio preview cards, Start button (disabled until preflight ok), live progress bar with current step name and 2.5s polling, Cancel button while active, completed `<video>` player with `download` link, Retry button on failure, render history list.
  - Project Overview: 6th "Final video" tile showing render state; embedded `<video>` once completed.
  - Public share `/s/{token}`: when a completed render exists, the hero block renders the final MP4 (`share-final-video`) using the selected thumbnail as poster — replaces the static image hero. Public payload exposes `final_video.{url, duration, width, height}` only — no `file_path` / `output_path` / job ids leaked.
  - ZIP export adds `render.json` with codec/dimensions/url/duration (no internal paths).
  - **108/108 backend pytest** (+12 new `TestRenderQueue`: preflight ok / blocks empty / start blocked when unmet / cross-user 403 / extra-body fields ignored / full render completes + ffprobe-validated h264 1920×1080 / concurrent render 409 / jobs list+get / 404 / ZIP render.json / share final_video / viewer 403). Frontend Playwright E2E verified end-to-end: full ~177s render flow with progress polling, completed video player + download, overview embed, public share final video, and prerequisite gating on a fresh empty project. Zero issues.
  - **Deploy hardening**: ffmpeg binary resolved at module load — prefers system `ffmpeg` (apt) and falls back to the static binary shipped by the `imageio-ffmpeg` Python package, so renders survive a fresh container without apt packages. Duration probe uses ffprobe when available and falls back to scene-duration estimation otherwise. `imageio-ffmpeg==0.6.0` pinned in `requirements.txt`.

### Phase 7 — Production hardening (2026-02)
- [x] **Boot-time ffmpeg/ffprobe install**: `app/system.py` runs in FastAPI `lifespan` and idempotently `apt-get install -y ffmpeg` if missing; cached system status exposed via diagnostics. `imageio-ffmpeg` static binary remains as a permanent pip-level fallback. Disabled via `DISABLE_APT_BOOT_INSTALL=true`.
- [x] **Render artifact retention**: `app/retention.py` runs every 6h (configurable via `RENDER_RETENTION_INTERVAL_SECONDS`) and:
  - removes MP4 files older than `RENDER_RETENTION_DAYS` (default 7) and marks the corresponding `render_jobs` row as `expired_artifact` with `output_url=None`
  - purges `_work_*` dirs older than 1h (always)
  - drops orphan project dirs in `static/{renders,thumbs,audio}/` for projects that no longer exist in DB
  - marks "stuck" active render jobs (>1h) as failed with explanatory error
  - never touches DB-tracked files for live projects unless past retention.
- [x] **CORS lock-down**: production CORS is now strictly `FRONTEND_URL` only. Wildcard `*` is filtered out when `DEV_MODE=false`. Preview regex (`*.preview.emergentagent.com`) only enabled when `DEV_MODE=true`.
- [x] **Cookie config**: production = `SameSite=None; Secure; HttpOnly`; dev = `SameSite=Lax; HttpOnly`. Already conditioned on `DEV_MODE` — verified.
- [x] **Admin diagnostics**: `GET /api/admin/diagnostics` (admin-only) returns provider modes, ffmpeg/ffprobe paths + sources, CORS origins + wildcard flag, cookie mode, DEV_MODE, render queue concurrency/timeout, on-disk usage per category, retention policy, data counts. New `/admin/diagnostics` page in the sidebar (admin only) with green/red status banner, cleanup-sweep button, and full breakdown.
- [x] **Manual retention trigger**: `POST /api/admin/retention/run` (admin-only) returns the cleanup report.
- [x] **Tests**: +7 `TestHardening` (admin RBAC on diagnostics + retention, payload shape, CORS no-wildcard when FRONTEND_URL set, HttpOnly+SameSite on auth cookies). **115/115 backend tests pass** in 264s.

### Phase 8 — Object-storage abstraction (2026-02)
- [x] **Pluggable storage backend** (`app/storage.py`): two adapters, controlled by `STORAGE_MODE`:
  - `local` (default — preserves current behaviour byte-for-byte): writes under `/app/backend/static/<key>` and returns `/api/static/<key>` URLs.
  - `object` (S3-compatible — AWS S3, Cloudflare R2, MinIO, Backblaze, Wasabi): lazy `boto3` client; uploads with proper `Content-Type` + `Cache-Control`; returns public URL when `STORAGE_PUBLIC_BASE_URL` set, else falls back to a presigned URL with `STORAGE_SIGNED_URL_TTL` (default 24h). Local file removed after successful upload.
- [x] **Render integration**: ffmpeg writes to a temp local workdir, then `storage.save_file(...)` is called for the final MP4. Job persists `output_url` (always remote-safe), `output_path` (None when remote), `output_storage_mode`, `output_storage_key`. Public share `final_video.url` and ZIP `render.json` use the URL only — `file_path` and `storage_key` are stripped from public payloads.
- [x] **TTS / thumbnail integration**: `tts.py` and `thumbnail_images.py` write to a temp path then route through the same abstraction. Asset documents now carry `storage_mode` + `storage_key`. Voiceover delete handler routes through `store.delete(...)` so object-mode files are removed from the bucket too.
- [x] **Retention update**: existing local sweep unchanged; in object mode the sweep additionally deletes expired remote MP4s via `storage.delete(key=...)` and marks the row `expired_artifact`. **Never** touches user/project/script/scene/asset rows for live data — only artifact rows whose physical files have been removed.
- [x] **Diagnostics**: `/api/admin/diagnostics.storage` now exposes `mode`, `bucket`, `region`, `endpoint_url`, `public_url_strategy`, `public_base_url`, `credentials_present`, `ok`, and a `warning` string set when `STORAGE_MODE=local` AND `DEV_MODE=false` (production with local disk) OR object mode is misconfigured. Diagnostics page renders the storage block with a green/amber state pill + warning banner.
- [x] **No-leak guarantees**: the public share payload, ZIP exports, and frontend asset cards never render `/app/backend/...`, `file_path`, `output_path`, or `storage_key`.
- [x] **Env vars** (object mode): `STORAGE_MODE`, `STORAGE_BUCKET`, `STORAGE_REGION`, `STORAGE_ENDPOINT_URL`, `STORAGE_PUBLIC_BASE_URL`, `STORAGE_SIGNED_URL_TTL`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`, `STORAGE_RETENTION_DAYS`. `boto3==1.42.86` pinned.
### Phase 8b — Deep health probe (2026-02)
- [x] **`/api/health/deep`**: new endpoint that runs a Mongo `ping` + a live storage round-trip probe (upload → verify → delete). Always returns 200 so monitoring tools can read JSON; inspect `ok`/`mongo.ok`/`storage.ok` per-section.
  - Local probe writes a 10-byte file to `static/health/probe-{ts}-{rand}.txt`, verifies size, deletes — leaves no artifacts behind.
  - Object probe (S3-compatible) does `put_object` → `head_object` → `delete_object` against `health/probe-{ts}-{rand}.txt`. Bounded with a 10s timeout. Pre-flights `STORAGE_BUCKET` to give a clean error if missing.
  - Errors are passed through `_safe_error()` which redacts known credential patterns (`AKIA`, `AWS_SECRET`, `STORAGE_SECRET`, `X-Amz-Signature`, presigned `Signature=` chunks) and caps length at 240 chars.
  - Cheap `/api/health` remains unchanged for k8s liveness/readiness; deep check is for monitoring/admin only.
- [x] **Tests**: +8 `TestHealthDeep` (endpoint shape + ok=true on local, local unit, object success with mocked client, upload-failure clean message, delete-failure clean message, missing bucket clean error, redaction unit, cheap-health untouched). **132/132 backend pytest** pass in 193s.

### Files changed (Phase 8b)
- Modified: `/app/backend/app/storage.py` (added `probe()` to backend interface + Local/S3 impls + `_safe_error()` + redaction patterns), `/app/backend/server.py` (added `/api/health/deep`), `/app/backend/tests/backend_test.py` (+`TestHealthDeep`)

### Deep health response shape
```json
{
  "service": "facelessforge",
  "checked_at": "2026-04-25T06:37:16.086060+00:00",
  "mongo":   { "ok": true, "latency_ms": 0, "error": null },
  "storage": { "ok": true, "mode": "local", "latency_ms": 0,
               "error": null, "probed_at": "..." },
  "ok": true
}
```

## Seeded Content
- `admin@facelessforge.io` / `admin123`
- `creator@facelessforge.io` / `creator123`
- 3 demo projects (AI trading — COMPLETED, Dark mode psychology — SCENES_GENERATED, Ancient Rome productivity — SCRIPT_GENERATED)

## Prioritised Backlog
### P1 (next phase)
- Real video rendering pipeline (ffmpeg / local render queue) to replace simulation
- Image generation for thumbnails (Gemini Nano Banana) producing actual JPEG/PNG
- Voiceover audio generation (ElevenLabs or OpenAI TTS) from scripts
- Stock footage / B-roll fetcher (Pexels/Storyblocks) from scene search terms
- Real-time generation progress (streaming LLM tokens) via SSE
### P2
- Team workspaces (multi-creator shared projects)
- Public share links for completed projects
- Version history for scripts/metadata
- Keyword research integration (trends, search volume)
- Email notifications when long-running jobs complete
- Stripe billing + tiered plans + cost-limit enforcement
- Password reset flow + forgot-password
- Advanced provider settings (per-step model selection)

## Known Limitations
- Thumbnails are text briefs only (no image generation yet — P1).
- Render is simulated (no actual video output — P1).
- First cold start: seed runs in background, so first request may briefly see 0 projects.
- CORS uses `*` + credentials (works because frontend/API are same-origin in preview; restrict for non-preview prod).

## Next Tasks List
1. Refactor `routes.py` into domain routers (`auth`, `projects`, `assets`, `tts`, `thumbnails`, `render`, `share`, `admin`) — non-behavioural pass to keep growth sustainable.
2. Admin Thumbnail/Render Gallery `/admin/thumbnails` (deferred from Phase 5/6).
3. ElevenLabs TTS as premium provider (drop-in via `tts.py` VOICE_STYLE_MAP).
4. Real-time SSE streaming for script generation.
5. Stripe billing + cost-limit enforcement (currently cost_estimate tracked but not enforced).
6. Render polish — Ken-Burns zoom on stills, simple per-scene crossfades, captions burn-in option.
