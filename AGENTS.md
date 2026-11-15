# FacelessForge Agent Guide

Deployable FacelessForge package. Runtime is Docker Compose.

## Layout

- `backend/` — FastAPI app (`backend/server.py`, loaded by `main.py`)
- `frontend/` — React dashboard

## Deploy

`docker-compose.facelessforge.yml` publishes host `127.0.0.1:7001` to container `8080`. The image entrypoint is `uvicorn main:app` (`Dockerfile`). Compose `env_file` is `/opt/facelessforge/deploy/.env.facelessforge`.

Recreate the app container only:

```bash
docker compose -f docker-compose.facelessforge.yml up -d --build --no-deps facelessforge-app
curl -s http://127.0.0.1:7001/api/health
```

`/api/health` is defined in `backend/server.py`.
