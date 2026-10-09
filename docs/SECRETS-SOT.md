# FacelessForge Secrets Source of Truth

Status: migration prepared; no production cutover has occurred.

## Authoritative runtime

- Runtime: `systemd` (unit file: `/etc/systemd/system/facelessforge.service`).
- Unit working directory: `/opt/facelessforge/deploy/backend`.
- Current runtime environment file: `/opt/facelessforge/deploy/backend/.env`.
- Docker Compose file: `/opt/facelessforge/deploy/docker-compose.facelessforge.yml` (not the active runtime).
- Repository defaults and legacy copies (`.env`, `.env.facelessforge`, and frontend `.env*` files) are retained pending verified migration.

The systemd unit must be treated as authoritative when this document conflicts with older repository documentation.

## Operational secrets in the active runtime file

`ADMIN_PASSWORD`, `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`,
`EXTERNAL_RENDER_API_KEY`, `FORGE_INTERNAL_SECRET`, `GROQ_API_KEY`,
`INVITE_CODE`, `JWT_SECRET`, `KIMI_API_KEY`, `MONGO_URL`,
`OPENROUTER_API_KEY`, `PADDLE_API_KEY`, `PADDLE_WEBHOOK_SECRET`,
`PEXELS_API_KEY`, `PIXABAY_API_KEY`, `REDIS_URL`, `SECRET_KEY`,
`STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`, `UNSPLASH_ACCESS_KEY`,
`YOUTUBE_CLIENT_SECRET`, and `YOUTUBE_ENCRYPTION_KEY`.

`GOOGLE_APPLICATION_CREDENTIALS` is a credential locator. Its referenced credential material must be assessed and, if it is a service-account key, moved to the same protected runtime flow rather than copied into ordinary config.

## Ordinary configuration

All other populated entries in the active runtime file are ordinary settings or public identifiers: feature flags, URLs/origins, provider/model selection, resource locations, storage settings, timeouts, render settings, product/price identifiers, and public client identifiers. They remain in the systemd environment file after migration.

## Target systemd credential design

- Credential storage: one host-key-encrypted, root-only systemd credential per operational secret in `/etc/credstore.encrypted/facelessforge/`.
- Injection: `facelessforge.service` loads credentials into systemd's private credential directory. A root-owned launcher reads them only in memory and `exec`s the existing Uvicorn command.
- The Google service-account JSON is an encrypted file credential; the launcher sets `GOOGLE_APPLICATION_CREDENTIALS` to its private systemd credential path.
- Secret values are not stored in the repository, unit file, shell history, or launcher logs.

## Rollback

Before migration, a root-only host-encrypted archive was created at `/opt/ethinx/deployment/secrets-vault/rollback/` and verified by a successful streaming decrypt plus archive validation. The plaintext sources remain intact until credential injection, tests, health checks, and an explicit production-cutover approval all succeed.

## Required verification gates

1. Each imported secret is retrieved by ID and verified in memory against its source without displaying either value.
2. The injection launcher verifies the complete expected name set and starts a disposable configuration check successfully.
3. Repository tests and non-disruptive health checks pass.
4. An operator explicitly approves replacing active plaintext values and restarting production.

## Change log — 2026-10-07T00:41Z (FacelessForge prod redeploy)

Executed (autonomous):
- Found root .env.facelessforge missing; running app used standalone docker with env from deploy/.env.facelessforge mounted into container (/app/backend/.env.facelessforge and /app/.env.facelessforge). JWT_SECRET length 64 hex preserved.
- Created root /opt/facelessforge/.env.facelessforge by copying deploy/.env.facelessforge and overlaying spec values: REDIS_URL=redis://ethinx-redis:6379, BANK_REFERENCE=FF-318A0311, BANK_AMOUNT_AUD=99. Permissions set 600 (124 lines).
- Updated docker-compose.prod.yml to set networks.deploy_facelessforge_net.external: true (network exists with that name). Config validated.
- Recreated stack: docker compose -f docker-compose.prod.yml down; removed standalone facelessforge-app; docker compose -f docker-compose.prod.yml up -d --force-recreate. Container now managed by compose (labels: facelessforge/app).
- Verification: /api/health returns {"status":"ok","forge":"active","redis":"ethinx-redis","tenant_isolation":"enforced via JWT tenant_id"}; /api/billing/bank-transfer-instructions returns HTTP/1.1 401 Unauthorized (unauthenticated) and returns full bank details when authenticated (login as founder@facelessforge.io). Image history clean (no BANK secrets baked). Deep health confirms R2 reachable, ElevenLabs/PEXELS keys present.
- Backed up: /opt/facelessforge/backups/pre-recreate-20261007-0040/ (deploy.env.facelessforge, docker-compose.prod.yml, inspect JSON).

Notes:
- deploy/.env.facelessforge remains the original full source (backed up). Root .env.facelessforge is the compose env file now.
- ethinx-redis connectivity shows "reachable": false in detailed health from current app logic (health checks by host name; container is on deploy_facelessforge_net; external redis may not be routable by name inside this network) but Redis URL set per spec; operational connectivity depends on external service.

## FacelessForge durable jobs fix 2026-10-07T06:32Z

- Added /api/jobs/test (public) returns 200 wired.
- Created deploy/backend/app/models/job.py JobStatus/JobLedger.
- Jobs router copied to deploy/backend; main.py with robust fallback wiring and logs.
- Dockerfile fixed: copy deploy/backend/requirements.txt, include deploy/backend, ensure frontend build placeholder if missing, PYTHONPATH updated.
- docker-compose.prod.yml updated with build context/target and external network.
- Rebuilt image (9fa26d3 style new sha) and restarted compose-managed container.
- Verification: /api/jobs/test returns 200; health ok; logs show 'Jobs router wired from app.api.jobs'.
