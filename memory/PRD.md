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
1. Ship image generation for thumbnails (Gemini Nano Banana) — adds immediate visual wow.
2. Add voiceover TTS (ElevenLabs/OpenAI) so scripts can be heard, not just read.
3. Wire forgot-password + reset-password flow.
4. Add public shareable link for COMPLETED projects (lead magnet / showcase).
5. Add billing & cost-limit enforcement.
