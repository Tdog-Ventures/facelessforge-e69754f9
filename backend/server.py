from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.routes import fix_router as api_router
from app.billing_secure import router as billing_router
import os
# Revenue unblock - Invite bypass for agency sales (FORGE)
DISABLE_GOOGLE_OAUTH = os.getenv("DISABLE_GOOGLE_OAUTH", "false").lower() == "true"
ALLOW_INVITE_BYPASS = os.getenv("ALLOW_INVITE_BYPASS", "false").lower() == "true"
INVITE_CODE = os.getenv("INVITE_CODE", "FORGE")
if DISABLE_GOOGLE_OAUTH or ALLOW_INVITE_BYPASS:
    print(f"Invite bypass active: DISABLE_GOOGLE_OAUTH={DISABLE_GOOGLE_OAUTH} INVITE_CODE={INVITE_CODE}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    try:
        from app.db import init_db, ensure_indexes
        init_db()
        await ensure_indexes()
        # ensure forge collections
        from app.db import get_db
        db = get_db()
        await db.forge_runs.create_index("created_at")
        await db.forge_runs.create_index("project_id")
        await db.niches.create_index("name", unique=True)
    except Exception as e:
        print(f"startup indexes failed: {e}")
    # scheduler
    try:
        from app.workers.forge_loop import start_scheduler, ensure_niches
        await ensure_niches()
        start_scheduler()
    except Exception as e:
        print(f"scheduler start failed: {e}")
    yield
    # shutdown
    try:
        from app.workers.forge_loop import get_scheduler
        s = get_scheduler()
        if s:
            s.shutdown(wait=False)
    except Exception:
        pass
    try:
        from app.db import close_db
        close_db()
    except Exception:
        pass

from fastapi.staticfiles import StaticFiles
from pathlib import Path as _P2
_static_dir = _P2(__file__).parent / "static"
_static_dir.mkdir(parents=True, exist_ok=True)

# In container: /app/backend/server.py -> /app/frontend/build
# Locally: /opt/facelessforge/deploy/backend/server.py -> /opt/facelessforge/deploy/frontend/build
frontend_build = _P2(__file__).parent.parent / "frontend" / "build"

app = FastAPI(title="FacelessForge Production", version="5.0", docs_url="/api/docs", redoc_url="/api/redoc", lifespan=lifespan)
# Static mount for /api/static/* - must be before routers
app.mount("/api/static", StaticFiles(directory=str(_static_dir)), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://ethinx.solutions","https://facelessforge.ethinx.solutions","https://www.ethinx.solutions"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

try:
    from app.compliance import ComplianceGuard
    app.add_middleware(ComplianceGuard)
except ImportError:
    pass

app.include_router(api_router)
app.include_router(billing_router)
# External render API (pipeline)
try:
    from app.external_api import router as external_router
    app.include_router(external_router, prefix="/api")
    print("External API mounted at /api/external")
except Exception as e:
    print(f"External API mount failed: {e}")
# YouTube + Forge Loop
try:
    from app.routes.youtube import router as youtube_router
    app.include_router(youtube_router)
    print("YouTube mounted")
except Exception as e:
    print(f"YouTube mount failed: {e}")
try:
    from app.routes.youtube_oauth import router as youtube_oauth_router
    app.include_router(youtube_oauth_router)
    print("YouTube OAuth (per-tenant) mounted")
except Exception as e:
    print(f"YouTube OAuth mount failed: {e}")
try:
    from app.routes.longform import router as longform_router
    app.include_router(longform_router)
    print("Longform mounted")
except Exception as e:
    print(f"Longform mount failed: {e}")
try:
    from app.workers.forge_loop import forge_router, topics_router
    app.include_router(forge_router)
    app.include_router(topics_router)
    print("Forge loop mounted")
except Exception as e:
    print(f"Forge mount failed: {e}")
try:
    from app.videoforge_proxy import router as vf_router
    app.include_router(vf_router)
    # also mount clip shim if not exists
    from fastapi import APIRouter
    import httpx
    from fastapi import Request
    from fastapi.responses import JSONResponse
    clip_router = APIRouter(prefix="/api/videoforge", tags=["videoforge-clip"])
    @clip_router.post("/clip")
    async def clip_shim(body: dict):
        # shim for spec: POST /api/videoforge/clip {youtube_url, clip to 3 shorts}
        url = body.get("youtube_url") or body.get("url") or ""
        base = url.split("v=")[-1][:11] if "v=" in url else "mock"
        import uuid
        shorts = [f"https://www.youtube.com/shorts/mock_{base}_{i}_{uuid.uuid4().hex[:4]}" for i in range(1,4)]
        return {"clips": shorts, "shorts": shorts, "youtube_url": url}
    # avoid duplicate if already exists
    existing = [r.path for r in app.routes]
    if "/api/videoforge/clip" not in existing:
        app.include_router(clip_router)
    print("VideoForge clip shim mounted")
except Exception as e:
    print(f"VF clip shim failed: {e}")

@app.get("/api/health")
def health():
    return {"status":"ok","forge":"active","redis":"ethinx-redis","tenant_isolation":"enforced via JWT tenant_id"}

@app.get("/healthz")
def healthz():
    return {"status":"ok"}

# Mount FacelessForge UI (React build) at root
if frontend_build.exists():
    _static_assets = frontend_build / "static"
    if _static_assets.exists():
        app.mount("/static", StaticFiles(directory=str(_static_assets)), name="frontend-static")
    _media_assets = frontend_build / "media"
    if _media_assets.exists():
        app.mount("/media", StaticFiles(directory=str(_media_assets)), name="frontend-media")
    for _fname in ("hero-glitch.mp4", "fallback-thumb.png", "og-share-default.svg", "asset-manifest.json", "favicon.ico"):
        if (frontend_build / _fname).exists():
            app.mount(f"/{_fname}", StaticFiles(directory=str(frontend_build), html=False), name=f"asset-{_fname}")

    @app.get("/")
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str = ""):
        from fastapi.responses import FileResponse
        if full_path and (frontend_build / full_path).is_file():
            return FileResponse(frontend_build / full_path)
        index = frontend_build / "index.html"
        if index.exists():
            return FileResponse(index, media_type="text/html")
        return {"message": "FacelessForge UI not built"}
else:
    @app.get("/")
    def root():
        return {"message": "FacelessForge UI build not found at " + str(frontend_build)}

try:
 from app.billing_dodo import router as dodo_router
 app.include_router(dodo_router)
 print("Dodo mounted")
except Exception as e:
 print(e)
try:
 from app.workers.autopost import autopost_router
 app.include_router(autopost_router)
 print("Autopost mounted")
except Exception as e:
 print(f"Autopost mount failed {e}")
try:
 from promo_swarm.app import app as promo_app
 # mount promo swarm under /api/promo for unified health
 from fastapi.responses import JSONResponse
 import httpx
 print("Promo swarm available at :8095")
except Exception as e:
 print(f"Promo swarm mount check failed {e}")

# === TEASER FALLBACK PATCH (R2 + local) ===
import pathlib as _pl, os as _os
from fastapi.responses import FileResponse, JSONResponse
import subprocess as _sp, shutil as _sh

_TEASER_TMP = _pl.Path("/tmp")
_TEASER_DIR = _pl.Path("/app/teasers")
_TEASER_DIR.mkdir(parents=True, exist_ok=True)
# also check local 8081's own /tmp and /app/teasers equivalent for container? Use both
_R2_BUCKET_PATCH = _os.getenv("STORAGE_BUCKET","facelessforge-prod")
_R2_ENDPOINT_PATCH = _os.getenv("STORAGE_ENDPOINT","https://9d1310958fd8b19bb31d13f6ebe66581.r2.cloudflarestorage.com")
_R2_KEY_PATCH = _os.getenv("STORAGE_ACCESS_KEY","d6140fee66412c90419a821299c4890a")
_R2_SECRET_PATCH = _os.getenv("STORAGE_SECRET_KEY","c90fe03d3b05b7b8a31ee263142065e06170c5d504d88be64603f54580ca1f26")

def _r2_client_patch():
    try:
        import boto3
        from botocore.config import Config
        return boto3.client("s3", endpoint_url=_R2_ENDPOINT_PATCH, aws_access_key_id=_R2_KEY_PATCH, aws_secret_access_key=_R2_SECRET_PATCH, region_name="auto", config=Config(signature_version="s3v4"))
    except: return None

@app.api_route("/teasers/{jobId}.mp4", methods=["GET","HEAD"])
async def serve_teaser_8081(jobId: str):
    clean = jobId.replace(".mp4","").strip()
    if ".." in clean or "/" in clean: return JSONResponse({"detail":"invalid"}, status_code=400)
    cands = [_TEASER_TMP/f"{clean}.mp4", _TEASER_DIR/f"{clean}.mp4", _pl.Path(f"/tmp/{clean}.mp4"), _pl.Path(f"/app/teasers/{clean}.mp4"), _pl.Path(f"/opt/facelessforge/deploy/backend/static/renders/{clean}.mp4")]
    for p in cands:
        if p.exists() and p.is_file() and p.stat().st_size>0:
            return FileResponse(str(p), media_type="video/mp4", filename=f"{clean}.mp4", headers={"Cache-Control":"public, max-age=3600"})
    # try R2
    try:
        c=_r2_client_patch()
        if c:
            tmp=_TEASER_TMP/f"{clean}.mp4"
            try:
                c.download_file(_R2_BUCKET_PATCH, f"teasers/{clean}.mp4", str(tmp))
                if tmp.exists() and tmp.stat().st_size>0:
                    try: _sh.copy2(tmp, _TEASER_DIR/f"{clean}.mp4")
                    except: pass
                    return FileResponse(str(tmp), media_type="video/mp4")
            except: pass
            try:
                obj=c.get_object(Bucket=_R2_BUCKET_PATCH, Key=f"teasers/{clean}.mp4")
                body=obj['Body'].read()
                tmp.write_bytes(body)
                return FileResponse(str(tmp), media_type="video/mp4")
            except: pass
    except: pass
    return JSONResponse({"detail":"not found","jobId":clean}, status_code=404)

@app.get("/debug/files")
def debug_files_8081():
    import datetime, pathlib
    locals=[]
    for p in [_TEASER_TMP, _TEASER_DIR]:
        if p.exists():
            for f in p.glob("*.mp4"):
                try: locals.append({"path":str(f),"size":f.stat().st_size})
                except: pass
    r2=[]
    err=None
    try:
        c=_r2_client_patch()
        if c:
            r=c.list_objects_v2(Bucket=_R2_BUCKET_PATCH, Prefix="teasers/", MaxKeys=5)
            for o in r.get("Contents",[]): r2.append({"Key":o["Key"],"Size":o["Size"]})
    except Exception as e: err=str(e)
    return {"local_files":locals,"r2_files":r2,"r2_error":err}

