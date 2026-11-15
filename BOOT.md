# FACELESSFORGE BOOT FILE (Source of Truth + Agent Directions)

Owner: TDog (Troy), ETHINX Systems, Adelaide SA  
Last verified: 2026-10-04 11:05 UTC. Anything marked UNVERIFIED has not been seen with a tool.  
Canonical location: `/opt/facelessforge/deploy/BOOT.md`

Every agent reads this file first, does its work, then appends to the AGENT LOG at the bottom.
If this file and direct observation disagree, trust the observation, rerun the check, and correct this file.

---

## 1. HARD RULES (all agents, no exceptions)

1. Never print secret values. Print variable names only. Redact keys, tokens, passwords and signed URL parameters such as `X-Amz-*`.
2. Back up before every edit to `/root/ff_backups/<date>/`. Do not rely on `/tmp`, which is wiped on reboot.
3. No fake success. Do not create `yt_mock_*` IDs, mock Shorts rows, dummy MP4s or any mock fallback. Failures must be loud, stored in `forge_runs.error` and visible.
4. YouTube uploads stay private (`YOUTUBE_PRIVACY=private`) until Google clears the YouTube API audit.
5. Never recreate or rebuild `facelessforge-app` while a render is running. Check first using section 7.
6. Compile changed Python before rebuilding: `backend/venv/bin/python -m py_compile <file>`. Roll back on failure.
7. Do not touch the Caddyfile, cloudflared configuration, `.env.facelessforge` contents or Dodo/billing code without a backup and a stated reason.
8. Verify, do not assume. A task is done only when a command run by the acting agent proves the result.
9. Do not run `AUTONOMOUS-FINAL-BOOT.sh` or `preview.sh` as written. They identify port 8005, which is `faceless-teaser-api`, not FacelessForge on port 7001.
10. Do not make marketing claims that are not currently true; section 6 governs claims.
11. Hosted LLM calls cost money. Do not enable a paid provider or set a budget without the owner naming the provider and monthly ceiling.

---

## 2. INFRASTRUCTURE MAP (verified 2026-10-03/04)

| Item | Fact |
|---|---|
| Host | Hetzner, hostname `ubuntu-8gb-nbg1-1`, Ubuntu, 8 GB class; disk was about 89% used with 17 GB free when checked |
| App | `facelessforge-app` container, host port 7001 to container 8080, built from `/opt/facelessforge/deploy` |
| Compose file | `/opt/facelessforge/deploy/docker-compose.facelessforge.yml`, service `facelessforge-app` |
| Env file | `/opt/facelessforge/deploy/.env.facelessforge`, mode 600. Legacy `.env` hardening must be rechecked before relying on it |
| Database | `facelessforge-mongo` (Mongo 7, `127.0.0.1:27017`); database name comes from `DB_NAME`. Redis is `ethinx-redis` |
| Storage | Cloudflare R2 bucket `facelessforge-prod`, `STORAGE_MODE=object`. A null local `output_path` is intentional |
| Public path | Cloudflare to cloudflared tunnel. **`facelessforge.ethinx.solutions` -> `127.0.0.1:7001`** (container). **`videoforge`, `promo`, `revenue`, `optivid`, `content`, `studio` `.ethinx.solutions` -> `127.0.0.1:8081`** (the stale release deployment below). Verified 2026-10-04 by served bundle hash |
| Second deployment | `127.0.0.1:8081` is systemd unit **`facelessforge.service`** ("Restless Forge - FacelessForge FastAPI Production"), enabled, `Restart=always`, `User=www-data`, `ExecStart` uvicorn `--workers 2`. Drop-in `facelessforge.service.d/zz-clean-release.conf` (dated 2026-09-08) sets `WorkingDirectory=/opt/facelessforge/releases/ff-youtube-20260908/backend`, so it runs the **stale release tree** `/opt/facelessforge/releases/ff-youtube-20260908`, not `deploy/`. Its `forge_loop.py` md5 differs from `deploy/`, and it still contains 9 mock generators. It serves the six hostnames above. Its `Environment=AUTO_POST_ENABLED=false` in the drop-in was silently defeated by `EnvironmentFile=backend/.env`, so its scheduler ran daily from 2026-10-01 until 2026-10-04 15:51 UTC |
| Caddy | Also routes `facelessforge.ethinx.solutions` to `127.0.0.1:7001`; public traffic used the tunnel when last checked. nginx was not running |
| Scheduler | In-app APScheduler job `forge_daily_0900`, timezone Australia/Adelaide. **Live only in the container** since 2026-10-04 13:58 UTC. The 8081 deployment's scheduler was disabled 2026-10-04 15:51 UTC by `AUTO_POST_ENABLED=false` in `backend/.env` (see Second deployment). The systemd timer `faceless-forge-1200.timer` is disabled |
| TTS | `ELEVENLABS_API_KEY` is **invalid on both deployments** — `GET https://api.elevenlabs.io/v1/user` returns HTTP 401 for the `.env.facelessforge` copy and the `backend/.env` copy, and the two keys are **different** from each other. ElevenLabs 401s appear in both the container log and the `facelessforge.service` journal (39 in 72h in the journal), so `tts.py` falls back to a mock voiceover. No real render is possible until a working key is installed in both files |
| Other cron | `/root/run_pipeline.sh`, `pipeline-aitools.sh` and `pipeline-business.sh` call `/api/external/render-video` every two hours. They contain a hard-coded API-key fallback that must be rotated later |
| Backups | `/usr/local/bin/facelessforge-backup.sh` runs at 03:00. It ends steps with `|| true`, always prints success, keeps data on the same disk and only lists the R2 bucket. Restore remains untested |
| Monitoring | Gatus on `127.0.0.1:8092` and Gotify on 8091 exist. `monitor_facelessforge.sh` restarts a service but does not alert a human |
| Exposed ports | Docker-published ports 7001, 8005 and 9091 may bypass UFW. Ollama 11434 was externally blocked by a DOCKER-USER rule |
| Local LLM | `ethinx-ollama`, model `qwen2.5:7b-instruct`, CPU only. Metadata and thumbnail generation can take two to four minutes per call |

---

## 3. PIPELINE STATUS

| Stage | Status |
|---|---|
| Render (script, voice, footage, captions, R2 upload) | **PARTIAL** — among the five most recent runs checked, two failed Constitution check `a` (timing) and one timed out at 600 seconds |
| QA Constitution gate (checks a-h) | **FAILING** — check `a` (timing) is rejecting runs. The previously reported datetime JSON fix is UNVERIFIED in the current deployed path |
| QA retry in forge path | **APPLIED (verified 2026-10-04)** — `grep -c '_qa_retries' backend/app/workers/forge_loop.py` returns `1` (commit `efe80f3`, re-verified 14:45 UTC) |
| `forge_loop.py` wait loop | **UNVERIFIED / CONTRADICTED** — earlier notes claimed 1500 seconds, but a recent run hit a 600-second limit |
| YouTube upload (forge path) | **BLOCKED** — recent QA failures prevent the forge path reaching a proven real upload |
| YouTube upload proof | **MOCK ONLY IN RECENT EVIDENCE** — the latest `short_uploaded` record checked had `is_mock:true` and a `yt_mock_` ID. No scheduled real upload is proven |
| Remaining fake-ID generators | **NONE in `backend/app`** (verified 2026-10-04 14:45 UTC) — the seven listed locations were replaced with loud failures; `grep -rn 'yt_mock_\|mock_yt\|yt_shorts_mock' backend/app --include=*.py \| grep -v tests` returns nothing. Mock *result* generators still exist in `stock.py` and `tts.py`; see AGENT LOG |

Do not describe the pipeline as working end to end until the launch gates in sections 6 and 9 are proven again with fresh evidence.

---

## 4. GOOGLE / YOUTUBE (last known configuration; reverify before changes)

- Cloud project: ETHINX Prime, project ID `ethinx-prime`, number `369667172793`, owned by `tdogga14@gmail.com`.
- YouTube Data API v3 was enabled.
- OAuth consent screen was External and in production with `youtube.upload` scope only. Until the audit is complete, all uploads remain private.
- Target channel: `@Tdog-x5b`, channel ID `UCo4SpYyaayLKafva18PHTqA`, owned by `tdogga14@gmail.com`.
- `@Tdog-u8l` belongs to `tdogskernel@gmail.com` and is not the upload target.
- A token limited to `youtube.upload` cannot call `channels.list`; a 403 from that endpoint is expected.
- Pending: Search Console verification for `ethinx.solutions`, demo video, reviewer test login and verification submission.
- An earlier standalone token-test upload was reported, but the latest scheduled/forge-path evidence is mock-only. Do not use the standalone claim as proof of a working scheduled pipeline.

---

## 5. SITE, SIGNUPS AND BILLING

- Landing-page sources: `frontend/src/pages/LandingPage.jsx`, `LegalLayout.jsx`, `PrivacyPage.jsx` and `TermsPage.jsx`. Build with `yarn build` in `frontend/`, copy `frontend/build/.` to `backend/static/frontend/`, then rebuild only after the active-render check is zero.
- The public and local frontends both served `static/js/main.ad1bf6bb.js` during the 2026-10-04 verification.
- `frontend/public/hero-render.mp4` was previously judged weak and should be replaced only with a genuine captioned render.
- The YouTube section indentation remains a layout issue unless a fresh check proves otherwise.
- Signups were open. The invite code was cosmetic, `/register` accepted anyone and `ALLOW_INVITE_BYPASS=true`. Closing signups and creating a reviewer test login remain pending.
- Dodo was in `test_mode`. The FacelessForge receiver is `/api/billing/dodo/webhook`, but the registered URL `https://ethinx.solutions/webhooks/dodo` routes to Revenue Swarm on port 4600 and does not grant FacelessForge credits.
- Credit metering before `run-now` was not found in `forge_loop.py`; reverify elsewhere before changing billing.
- Privacy and Terms text were rewritten to remove internal details and include the Google Limited Use statement. Legal review remains advisable before accepting customers.

---

## 6. MARKETING DIRECTIONS

Customer: solo faceless-YouTube creators using self-serve checkout.

Accurate capability claim: FacelessForge is designed to turn a topic into a scripted, voiced and captioned vertical video and connect to a YouTube upload path. Do not state that the unattended production pipeline currently works.

Do not claim:

- Public auto-posting.
- Daily unattended posting as a proven feature.
- A successful scheduled real YouTube upload.
- Prices, free trials or credit amounts while Dodo remains in test mode.
- Shorts splitting or TikTok, Instagram or X posting unless freshly verified.
- Customer numbers, testimonials or earnings without real evidence.

Allowed preparation without traffic:

- Content calendars and scripts based only on genuine renders, labelled “made with FacelessForge”.
- Accurate landing-page, SEO, FAQ and comparison copy.
- Public-information creator outreach lists and draft messages held for owner approval.
- A demo-video script for Google review.
- Honest Reddit, Indie Hackers and X drafts that follow community rules and disclose AI-generated video and founder ownership.

Do not drive paid or large-scale traffic until all gates pass:

1. A scheduled run uploads a real private video and stores a real `watch?v=` URL in `forge_runs`.
2. Render plus QA success is at least 95% over 20 consecutive runs.
3. Dodo is live, the webhook routes correctly, one controlled purchase grants credits and rendering enforces credit balance.
4. Signups are controlled and a reviewer test login exists.
5. A backup restore has been demonstrated.
6. Alerts reach a human.

---

## 7. RUNBOOK (read-only checks first)

```bash
# Hetzner / root / /opt/facelessforge/deploy

# Health
curl -s http://127.0.0.1:7001/api/health

# Recent real-or-fake status
DB=$(grep -m1 '^DB_NAME=' .env.facelessforge | cut -d= -f2)
docker exec facelessforge-mongo mongosh --quiet "$DB" --eval 'db.forge_runs.find({}, {_id:0,is_mock:1,youtube_url:1,status:1,error:1,created_at:1}).sort({created_at:-1}).limit(5).forEach(d=>print(JSON.stringify(d)))' | cut -c1-240

# Active render count: must print 0 before any rebuild
docker exec facelessforge-mongo mongosh --quiet "$DB" --eval 'print(db.render_jobs.countDocuments({status:{$nin:["completed","failed","cancelled","expired_artifact"]}, created_at:{$gt:new Date(Date.now()-90*60000)}}))' | tail -1

# QA retry patch status: expected 1 after a correct patch; last verified value was 0
grep -c '_qa_retries' backend/app/workers/forge_loop.py

# Mock-ID locations: must return nothing outside tests before launch
grep -rn 'yt_mock_\|mock_yt\|yt_shorts_mock' backend/app --include=*.py | grep -v tests

# Frontend bundle parity
curl -s "https://facelessforge.ethinx.solutions/?x=$RANDOM" | grep -oE 'static/js/main\.[a-z0-9]+\.js' | head -1
curl -s http://127.0.0.1:7001/ | grep -oE 'static/js/main\.[a-z0-9]+\.js' | head -1
```

Safe rebuild: back up every changed file, compile Python, confirm the active-render count is zero, then run `docker compose -f docker-compose.facelessforge.yml up -d --build --no-deps facelessforge-app`. Wait for health. If verification fails, restore the backup and rebuild.

Move any important rollback material from `/tmp/ff_bak/` or `/tmp/ff_bak2/` into `/root/ff_backups/`; `/tmp` is volatile.

---

## 8. WORK QUEUE (priority order)

1. **CRITICAL:** Remove all seven mock-ID generators from `routes/youtube.py`, `workers/forge_loop.py`, `adapters/posters/youtube.py` and `external_api.py`.
2. **CRITICAL:** Apply the QA retry patch. `grep -c '_qa_retries' backend/app/workers/forge_loop.py` must become `1` after verification.
3. **CRITICAL:** Fix Constitution check `a` timing failures and investigate whether datetime serialization still affects the deployed QA path. Do not weaken the gate without evidence.
4. **CRITICAL:** Identify and raise the effective render timeout from 600 to 1500 seconds, then prove the running container uses it.
5. **HIGH:** Complete the first real scheduled private YouTube upload and record a non-mock URL.
6. **HIGH:** Correct the Dodo webhook, implement credit metering and only then move billing toward live mode.
7. **MEDIUM:** Close signups and create the reviewer test login.
8. **MEDIUM:** Complete Search Console verification, demo video and Google audit submission.
9. **LOW:** Harden backups and connect Gatus to Gotify or another human alert channel.
10. **LOW / OWNER DECISION:** Choose a hosted LLM provider and monthly spending ceiling.

The first four production changes require explicit execution approval. This BOOT restoration does not authorize them.

---

## 9. DEFINITION OF “FULLY FUNCTIONAL”

Current status: **NOT FUNCTIONAL**.

- No scheduled run has proven a real YouTube upload; the latest checked upload evidence was mock.
- QA is failing on Constitution check `a` timing.
- The seven mock-ID generators were removed on 2026-10-04 and verified gone; mock result generators remain in `stock.py` and `tts.py`.
- The QA retry patch is applied and verified (`grep -c '_qa_retries'` = 1).
- A recent render timed out at 600 seconds despite earlier 1500-second claims.

Fully functional means a scheduled 09:00 Adelaide run completes unattended and records a real YouTube URL with no mock rows or mock generators; a paying user can check out, receive credits, render and be blocked when credits are exhausted; failures alert a human; a backup restore has been demonstrated; and marketing claims match observed reality.

---

## AGENT LOG

- 2026-10-04 Claude: wrote the original boot file from session evidence; no server commands were run by that agent.
- 2026-10-04 11:05 UTC verification, Hetzner `ubuntu-8gb-nbg1-1`: seven mock-ID locations remained; `_qa_retries` count was 0; two of five recent runs failed Constitution check `a`, one timed out at 600 seconds and one Shorts upload was mock; frontend bundles matched `main.ad1bf6bb.js`; env permissions were 600; container was healthy on port 7001. Required before any launch claim: remove mocks, apply QA retry, fix timing and prove the 1500-second timeout.
- 2026-10-04 13:14 UTC, prior agent (commit `efe80f3`): QA retry patch applied; `grep -c '_qa_retries'` = 1. Section 3 and section 9 statements "QA retry NOT APPLIED" are therefore stale.
- 2026-10-04 ~13:58 UTC, prior agent: container `facelessforge-app` rebuilt from host code. Root cause of the dead pipeline was `Forge mount failed: unexpected indent (forge_loop.py, line 444)` — a `SyntaxError` that silently skipped `include_router(forge_router)` and killed the APScheduler, so `/api/forge/*` returned 404 on 7001 and no 09:00 job existed. After rebuild: `Forge loop mounted`, `Forge scheduler started daily 09:00 Australia/Adelaide`, forge paths 404 -> 401, path count 110 -> 121.
- 2026-10-04 14:45 UTC, mock-fallback removal under HARD RULE 3, approved by owner. Backups: `/root/ff_backups/2026-10-05/`. Pre-check `grep -c '_qa_retries'` = 1, patch present, not reapplied. All edits `py_compile` clean.
  - `forge_loop.py`: removed the `FORGE_MOCK_STOCK` fast path (its default was `"true"`, so mock stock was the production default) and made the flag raise; removed the stock-timeout mock dict; replaced the two swallowing `logger.debug`/`logger.warning` handlers with `raise RuntimeError(...)` so stock failures are loud; replaced the voiceover timeout `Exception("voiceover_timeout_mock")` and deleted the `mock_tts` asset insert with its `/api/static/audio/{pid}_mock.mp3` preview; removed the `shorts/mock_{base}_{i}` generator in `_videoforge_clip`. Dropped the redundant `startswith("yt_mock_")` clause from the upload guard because the existing `"mock" in ...` substring test already covers it.
  - `server.py`: clip shim now raises `HTTPException(501)` (local import; `HTTPException` was not imported) instead of returning three fabricated `shorts/mock_` URLs; `start_scheduler()` wrapped in `FORGE_SCHEDULER_ENABLED` (default `"true"`, container behaviour unchanged).
  - `external_api.py`: auto-upload failure now writes `status:"failed"` plus the real exception text to `forge_runs.error` via upsert on `job_id`, instead of only `logger.warning`.
  - Verified: active-render count 0; `docker compose -f /opt/facelessforge/deploy/docker-compose.facelessforge.yml up -d --build --no-deps facelessforge-app`; container healthy; local and public `/api/health` both `{"status":"ok",...}`; `Forge scheduler started daily 09:00 Australia/Adelaide` in logs; clip shim returns HTTP 501; `POST /api/forge/run-now` 401; required grep `yt_mock_|mock_yt|yt_shorts_mock` in `backend/app` returns nothing. Port 8081 untouched and still listening (pid 3834433); `videoforge`, `promo`, `revenue`, `optivid`, `content`, `studio` `.ethinx.solutions` all HTTP 200. Nothing was sent to nginx, cloudflared, Caddy, `.env.facelessforge`, billing/Dodo or any other compose directory.
  - Left alone: 8081 needs `FORGE_SCHEDULER_ENABLED=false` plus an owner-scheduled restart to stop its duplicate 09:00 job, but it serves six live hostnames so only the owner can restart it. `stock.py:182 _use_mock()` still defaults to true (`USE_MOCK_PEXELS`, also true when `PEXELS_API_KEY` is absent), so the forge path's real Pexels search can still receive mock results — a separate file and blast radius, and flipping it touches `.env.facelessforge` (HARD RULE 7). `tts.py:147` can still return `provider:"mock"` when `USE_MOCK_TTS` is on; `render.py:918` already skips mock voiceover when muxing. Thumbnail fallbacks in `forge_loop.py` ~291-315 remain `logger.debug`.
- 2026-10-04 14:45 UTC, owner action still required: (a) restart 8081 with `FORGE_SCHEDULER_ENABLED=false` to remove the duplicate 09:00 scheduler; (b) decide `USE_MOCK_PEXELS` / `USE_MOCK_TTS`; (c) work-queue items 3 and 4 (Constitution check `a` timing, 600 -> 1500 second render timeout) are untouched, so the pipeline still cannot complete a run unattended.
- 2026-10-04 15:48-15:52 UTC, TASK A, owner-approved: neutralised the duplicate 09:00 scheduler on port 8081. Baseline captured first: all seven hostnames HTTP 200, `facelessforge` serving bundle `main.ad1bf6bb.js` and the other six serving `main.161e0c73.js` (copies in `/root/ff_backups/2026-10-05/baseline/`). Active-render count was **1** on the first check (job `471db6d3`, `preparing_assets`, created 15:45:24Z), so waited per instruction; it reached `completed` and the count fell to 0 after 2 minutes. Backup `/root/ff_backups/2026-10-05/backend.env.taskA` (mode 640 preserved, not 600 — the file is 640 root:www-data and the 8081 unit runs as `www-data`). Edited exactly one line, `AUTO_POST_ENABLED=true` -> `false`; `grep -c '^AUTO_POST_ENABLED=false'` = 1, `=true` = 0, 138 lines before and after. Proved the container cannot be affected: its compose `env_file` is `.env.facelessforge`, and 45 keys that exist only in `backend/.env` are absent from the container. Restarted `facelessforge.service` only (pid 3834493 -> 2615645, `NRestarts=0`), waited 15s. Verified: 8081 `/api/health` HTTP 200; `/proc/<pid>/environ` shows `AUTO_POST_ENABLED=false`; **no** apscheduler, `Added job`, `Scheduler started` or `Forge scheduler started` lines in the journal after 15:51:00Z (release code `start_scheduler()` returns before importing APScheduler when the flag is false); all seven hostnames returned identical status, title and bundle hash to the baseline. No rollback needed.
- 2026-10-04 15:55 UTC, TASK B, read-only: ElevenLabs failures come from **both** instances — 2 lines in the container log (window only from its 14:41 UTC recreation) and 8 `ElevenLabs failed` / 39 `401 Unauthorized` lines in the `facelessforge.service` journal across Oct 01-03. `GET https://api.elevenlabs.io/v1/user` with the `xi-api-key` header returns **HTTP 401** for the `.env.facelessforge` key (sha256 prefix `f46bb6b1`) and **HTTP 401** for the `backend/.env` key (sha256 prefix `351b89d6`); the two keys are **not identical**. Two separate broken credentials. Nothing was changed.
- 2026-10-04 15:57 UTC, TASK C, read-only: origin 8081 serves `main.161e0c73.js` for every hostname and origin 7001 serves `main.ad1bf6bb.js` for every hostname, so there is no Host-based routing — the public split is purely which origin the tunnel targets. Public mapping: `facelessforge` -> 7001 container, the other six -> 8081 stale release. Probed `/api/health`, `/api/auth/login`, `/api/forge/status`, `/api/videoforge/clip`, `/pricing`, `/privacy` on both origins: identical status codes, so no route-parity blocker was found in that sample. Recommendation only, not executed: repoint the six hostnames to 7001 (that is the build that carries the HARD RULE 3 fixes and the live 09:00 scheduler), then retire `facelessforge.service`; do it only after a working ElevenLabs key exists, and validate the marketing pages on 7001 first because those six hostnames are public surface.
- 2026-10-04 15:57 UTC, incident note: while diffing `backend/.env` to prove a one-line change, `diff -u` printed adjacent context that included a `YOUTUBE_CLIENT_SECRET` value in the agent transcript. No key was intentionally read or reported, but that value should be treated as disclosed and rotated by the owner. Subsequent verification used name-only and hash-only output.
