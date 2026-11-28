# FACELESSFORGE BOOT FILE (Source of Truth + Agent Directions)

Owner: TDog (Troy), ETHINX Systems, Adelaide SA
Canonical location: `/opt/facelessforge/deploy/BOOT.md`

Deploy is `docker-compose.facelessforge.yml`: host `127.0.0.1:7001` to container `8080`, entrypoint `uvicorn main:app` (`Dockerfile`). Compose `env_file` is `/opt/facelessforge/deploy/.env.facelessforge`.

If this file and direct observation disagree, trust the observation, rerun the check, and correct this file.

---

## 1. HARD RULES (all agents, no exceptions)

1. Never print secret values. Print variable names only. Redact keys, tokens, passwords and signed URL parameters such as `X-Amz-*`.
3. No fake success. Do not create `yt_mock_*` IDs, mock Shorts rows, dummy MP4s or any mock fallback. Failures must be loud, stored in `forge_runs.error` and visible.
4. YouTube uploads stay private (`YOUTUBE_PRIVACY=private`) until Google clears the YouTube API audit.
5. Never recreate or rebuild `facelessforge-app` while a render is running.
7. Do not edit `backend/app/billing_dodo.py` or the compose env file `/opt/facelessforge/deploy/.env.facelessforge` without a stated reason.
8. Verify, do not assume. A task is done only when a command run by the acting agent proves the result.
10. Do not make marketing claims that are not currently true; section 6 governs claims.
11. Hosted LLM calls cost money. Do not enable a paid provider or set a budget without the owner naming the provider and monthly ceiling.

---

## 2. INFRASTRUCTURE

| Item | Fact |
|---|---|
| App | `facelessforge-app`, host `127.0.0.1:7001` to container `8080`, entrypoint `uvicorn main:app` (`Dockerfile`) |
| Compose file | `docker-compose.facelessforge.yml`, services `facelessforge-app` and `facelessforge-mongo` |
| Env file | Compose `env_file`: `/opt/facelessforge/deploy/.env.facelessforge` |
| Database | `facelessforge-mongo` (mongo:7) on host `127.0.0.1:27017`. The database name is `DB_NAME` (`backend/app/db.py`) |
| Scheduler | APScheduler job `forge_daily_0900`, 09:00 Australia/Adelaide, in `backend/app/workers/forge_loop.py`. `start_scheduler()` registers it only when `AUTO_POST_ENABLED` is true |
| Health | `GET /api/health` in `backend/server.py`. Compose healthcheck calls it on container port `8080` |

---

## 3. PIPELINE

- `backend/app` has no `yt_mock_`, `mock_yt`, or `yt_shorts_mock` generators.
- `FORGE_QA_RETRIES` defaults to 1 in `backend/app/workers/forge_loop.py` (`_qa_retries`).
- `RENDER_TIMEOUT_SECONDS` defaults to 1500 in `backend/app/workers/forge_loop.py` and to 600 in `backend/app/render.py`.
- `backend/app/stock.py` `_use_mock()` is true when `USE_MOCK_PEXELS` is set or `PEXELS_API_KEY` is empty. `backend/app/tts.py` `_use_mock()` is true when `USE_MOCK_TTS` is set.

Do not describe the pipeline as working end to end until the gates in sections 6 and 9 are met.

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

- Landing-page sources: `frontend/src/pages/LandingPage.jsx`, `frontend/src/pages/LegalLayout.jsx`, `frontend/src/pages/PrivacyPage.jsx`, and `frontend/src/pages/TermsPage.jsx`.
- `frontend/public/hero-render.mp4` was previously judged weak and should be replaced only with a genuine captioned render.
- Dodo defaults to `test_mode` unless `DODO_PAYMENTS_ENVIRONMENT=live_mode` (`backend/app/billing_dodo.py`). The receiver is `POST /api/billing/dodo/webhook`.

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

Run from the deploy checkout (the directory that contains `docker-compose.facelessforge.yml`).

```bash
curl -s http://127.0.0.1:7001/api/health

# expected 1
grep -c '_qa_retries' backend/app/workers/forge_loop.py

# must return nothing outside tests before launch
grep -rn 'yt_mock_\|mock_yt\|yt_shorts_mock' backend/app --include=*.py | grep -v tests
```

Rebuild the app container only after the active-render count is zero:

```bash
docker compose -f docker-compose.facelessforge.yml up -d --build --no-deps facelessforge-app
curl -s http://127.0.0.1:7001/api/health
```

---

## 8. WORK QUEUE (priority order)

3. **CRITICAL:** Fix Constitution check `a` timing failures and investigate whether datetime serialization still affects the deployed QA path. Do not weaken the gate without evidence.
4. **CRITICAL:** Identify and raise the effective render timeout from 600 to 1500 seconds, then prove the running container uses it. `backend/app/render.py` still defaults `RENDER_TIMEOUT_SECONDS` to 600; `backend/app/workers/forge_loop.py` defaults it to 1500.
5. **HIGH:** Complete the first real scheduled private YouTube upload and record a non-mock URL.
6. **HIGH:** Correct the Dodo webhook, implement credit metering and only then move billing toward live mode.
7. **MEDIUM:** Close signups and create the reviewer test login.
8. **MEDIUM:** Complete Search Console verification, demo video and Google audit submission.
9. **LOW:** Harden backups and connect alerts to a human.
10. **LOW / OWNER DECISION:** Choose a hosted LLM provider and monthly spending ceiling.

The production changes above require explicit execution approval. This file does not authorize them.

---

## 9. DEFINITION OF “FULLY FUNCTIONAL”

Fully functional means a scheduled 09:00 Adelaide run completes unattended and records a real YouTube URL with no mock rows or mock generators; a paying user can check out, receive credits, render and be blocked when credits are exhausted; failures alert a human; a backup restore has been demonstrated; and marketing claims match observed reality.
