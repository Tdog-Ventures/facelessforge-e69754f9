# SWARM_STATE.md — FacelessForge delivery state (verified by command, not assumed)

## HEAD / deployment
- Branch: `fix/p0-revenue-blockers` (verify: `git branch --show-current`)
- Last commit: see `git log --oneline -3`
- Running image: `deploy-facelessforge-app` (verify: `docker inspect facelessforge-app`)
- Container serves 127.0.0.1:7001; public https://facelessforge.ethinx.solutions serves same bundle
- Untracked file to preserve: `FACELESSFORGE_AUDIT.md` (never commit)

## Standing rules (every session)
- Rebuild ONLY `facelessforge-app` (`docker compose -f docker-compose.facelessforge.yml up -d --build --no-deps facelessforge-app`). Never ports 8000/8005, Caddy, billing config, provider selection, YouTube.
- Automation/scheduling/publishing stay OFF for testing. No YouTube uploads/quota.
- No secrets printed/committed. No mock fallbacks. No weakened QA. No fabricated outputs.
- Backups: timestamped `/root/ffbackups/<ts>-<slice>/` with priors + rollback notes. No routine `git reset --hard`.
- Zero active renders in last 90 min before any rebuild (mongo `render_jobs` active-status query).
- Paid calls: bounded, counted, existing metering untouched. No unlimited retries.

## Verified findings
- ElevenLabs key valid (voices endpoint ok). TTS fail-closed (no mock fallback).
- Thumbnails live via pexels_stock compositor (no Gemini/API keys configured).
- Scheduler real (APScheduler `forge_daily_0900`); automation OFF persisted across restarts.
- Render-job status normalized both cases in backend + frontend.
- QA check_c skips sub-second trailing slivers; check_f requires paired dark edges.
- VO asset duration is ffprobe-MEASURED (tts `_measure_audio_duration`), not byte estimate.
- Requested target_duration is authoritative; only explicit user actions move it.
- Gotify/Gatus run on `ethinx-operations_default`; facelessforge-app is on `deploy_facelessforge_net` and published socket is 127.0.0.1-bound, so the app CANNOT reach Gotify directly. Alerting path: app exposes `/api/alerts/evaluate` (Gatus polls the public URL) + optional direct dispatch via env-configured URL. ALERTS_ENABLED defaults false.

## Changes (this delivery)
- P1 manual script: PATCH upsert + source + validation + draft-only invalidation; dual-option Script tab; 7 backend + 10 frontend tests; 16/16 browser checks.
- P2 voice fit: measured VO duration; silent target overwrite removed; bounded AI correction (initial + 2); manual never rewritten; explicit autofit endpoint; voice_fit provenance persisted.
- P3 alerting: `backend/app/alerts.py` (disabled-by-default Gotify dispatch, cooldowns, fail-silent, pollable evaluate); hooks on render + forge failures; `/api/alerts/evaluate` + `/api/alerts/test`.

## Test evidence
- See commit messages + `/root/ffbackups/` dirs. Full backend suite + frontend suite green before each deploy.

## Remaining blockers
- Scheduler: no verified successful real run.
- Disk: high usage; retention/leak reconciliation ongoing (orphan compose volumes exist).
- Monitoring: alert wiring in progress (P3).
- Voice↔duration fitting still needs explicit user action when drift exceeds tolerance (by design).
