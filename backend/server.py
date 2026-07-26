"""FacelessForge backend entrypoint."""
from dotenv import load_dotenv
load_dotenv()

# Load user-managed secret blocks from /app/secrets/*.env (gitignored).
# These OVERRIDE values from backend/.env so users can drop in real API keys
# without touching the tracked .env. Order is deterministic (sorted).
from pathlib import Path as _SecretsPath
_secrets_dir = _SecretsPath(__file__).resolve().parent.parent / "secrets"
if _secrets_dir.is_dir():
    for _f in sorted(_secrets_dir.glob("*.env")):
        load_dotenv(_f, override=True)

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

from app.db import init_db, ensure_indexes, close_db
from app.routes import router as api_router
from app.external_api import router as external_router
from app.seed import run_seed
from app.system import ensure_ffmpeg_available

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger("facelessforge")


# Cached system status set at boot, exposed via /api/admin/diagnostics.
SYSTEM_STATUS: dict = {}


async def _seed_task():
    try:
        await run_seed()
        logger.info("Seed complete")
    except Exception as e:  # noqa: BLE001
        logger.exception("Seed failed: %s", e)


async def _retention_loop():
    """Periodic background cleanup of old render artifacts."""
    from app.retention import run_cleanup_once
    interval = int(os.environ.get("RENDER_RETENTION_INTERVAL_SECONDS", str(60 * 60 * 6)))
    while True:
        try:
            await run_cleanup_once()
        except Exception as e:  # noqa: BLE001
            logger.exception("Retention cleanup failed: %s", e)
        await asyncio.sleep(max(60, interval))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await ensure_indexes()
    # System checks (ffmpeg, ffprobe) — never block startup
    SYSTEM_STATUS.update(ensure_ffmpeg_available())
    # Run seed in background so server starts immediately even if LLM is slow
    asyncio.create_task(_seed_task())
    asyncio.create_task(_retention_loop())
    yield
    close_db()


app = FastAPI(title="FacelessForge API", lifespan=lifespan)


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "facelessforge"}


@app.get("/api/health/deep")
async def health_deep():
    """Deep health check — runs a live storage round-trip probe and a Mongo
    ping. Slower than /api/health (do NOT use for k8s liveness/readiness).
    Use for monitoring, smoke checks, and admin diagnostics. Always returns
    200 so monitoring tools can read the JSON; inspect `ok` and per-section
    flags for actual status."""
    import asyncio as _aio
    from datetime import datetime as _dt, timezone as _tz
    from app.storage import get_storage
    from app.db import get_db

    out = {
        "service": "facelessforge",
        "checked_at": _dt.now(_tz.utc).isoformat(),
        "mongo": {"ok": False, "latency_ms": None, "error": None},
        "storage": {"ok": False, "mode": None, "latency_ms": None, "error": None},
    }
    # Mongo ping
    try:
        import time as _t
        t0 = _t.time()
        await _aio.wait_for(get_db().command("ping"), timeout=3.0)
        out["mongo"] = {"ok": True, "latency_ms": int((_t.time() - t0) * 1000), "error": None}
    except _aio.TimeoutError:
        out["mongo"] = {"ok": False, "latency_ms": 3000, "error": "ping timed out"}
    except Exception as e:  # noqa: BLE001
        out["mongo"] = {"ok": False, "latency_ms": None, "error": f"{type(e).__name__}"}

    # Storage probe (off-thread; bounded)
    try:
        store = get_storage()
        result = await _aio.wait_for(_aio.to_thread(store.probe), timeout=10.0)
        out["storage"] = result
    except _aio.TimeoutError:
        out["storage"] = {"ok": False, "mode": getattr(store, "mode", None) if 'store' in locals() else None,
                          "latency_ms": 10000, "error": "probe timed out",
                          "probed_at": _dt.now(_tz.utc).isoformat()}
    except Exception as e:  # noqa: BLE001
        out["storage"] = {"ok": False, "mode": None, "latency_ms": None,
                          "error": f"{type(e).__name__}",
                          "probed_at": _dt.now(_tz.utc).isoformat()}

    out["ok"] = bool(out["mongo"]["ok"] and out["storage"]["ok"])

    # TTS provider snapshot (non-blocking — informational only)
    try:
        from app.tts import provider_info as _tts_info
        out["tts"] = _tts_info()
    except Exception as e:  # noqa: BLE001
        out["tts"] = {"error": f"{type(e).__name__}"}

    return out


app.include_router(api_router)
# External API: thin wrapper exposed at /api/external/...
app.include_router(external_router, prefix="/api")

# Static mount for generated images. Served via /api/static/thumbs/{project_id}/{asset_id}.{ext}
# so it routes through the k8s ingress /api/* rule.
from pathlib import Path as _Path
_STATIC = _Path(__file__).parent / "static"
_STATIC.mkdir(parents=True, exist_ok=True)
app.mount("/api/static", StaticFiles(directory=str(_STATIC)), name="static")

# CORS: production locks to FRONTEND_URL; dev permits localhost + emergent preview regex.
# Wildcard "*" with credentials is unsafe AND violates the CORS spec, so we never use it.
_dev = os.environ.get("DEV_MODE", "false").lower() in ("1", "true", "yes")
_frontend = os.environ.get("FRONTEND_URL")
if _frontend:
    _allow_origins = [o.strip() for o in _frontend.split(",") if o.strip()]
elif _dev:
    # Dev fallback: allow local dev origins
    _allow_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
else:
    _allow_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]

# Belt-and-braces: strip wildcard if anyone set it via env in prod
if not _dev:
    _allow_origins = [o for o in _allow_origins if o != "*"]

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=_allow_origins,
    allow_origin_regex=r"https://.*\.preview\.emergentagent\.com" if _dev else None,
    allow_methods=["*"],
    allow_headers=["*"],
)
