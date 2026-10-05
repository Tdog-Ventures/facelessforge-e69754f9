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
