# FacelessForge Secrets Source of Truth

Runtime is Docker Compose, `docker-compose.facelessforge.yml`.

- Publish: host `127.0.0.1:7001` to container `8080`
- Entrypoint: `uvicorn main:app` (`Dockerfile`)
- Env file: `/opt/facelessforge/deploy/.env.facelessforge` (compose `env_file`)
- Health: `GET /api/health` (`backend/server.py`)

Do not print secret values. Print variable names only.

## Names the app reads

`ADMIN_PASSWORD`, `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`,
`EXTERNAL_RENDER_API_KEY`, `FORGE_INTERNAL_SECRET`, `INVITE_CODE`,
`JWT_SECRET`, `KIMI_API_KEY`, `MONGO_URL`, `OPENROUTER_API_KEY`,
`PADDLE_API_KEY`, `PADDLE_WEBHOOK_SECRET`, `PEXELS_API_KEY`,
`PIXABAY_API_KEY`, `REDIS_URL`, `STORAGE_ACCESS_KEY`,
`STORAGE_SECRET_KEY`, `UNSPLASH_ACCESS_KEY`, `YOUTUBE_CLIENT_SECRET`,
and `YOUTUBE_ENCRYPTION_KEY`.
