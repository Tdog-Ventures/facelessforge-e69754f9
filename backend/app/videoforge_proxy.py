"""OptiVid / VideoForge proxy — forwards to localhost:8000"""
import httpx
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Optional
import logging

logger = logging.getLogger("videoforge_proxy")
router = APIRouter(prefix="/videoforge", tags=["videoforge"])

OPTI_BASE = "http://127.0.0.1:8000/api/v1"
TIMEOUT = httpx.Timeout(900.0, connect=10.0)

@router.get("/health")
async def health_proxy():
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get("http://127.0.0.1:8000/health")
            return r.json()
        except Exception as e:
            return {"ok": False, "optivid": str(e)}

@router.post("/jobs")
async def create_job(
    file: UploadFile = File(...),
    preset: str = Form("viral"),
    platform: Optional[str] = Form(None),
    settings_override: str = Form("{}")
):
    try:
        content = await file.read()
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            files = {"file": (file.filename, content, file.content_type or "video/mp4")}
            data = {"preset": preset, "settings_override": settings_override}
            if platform:
                data["platform"] = platform
            r = await client.post(f"{OPTI_BASE}/jobs", files=files, data=data)
            return JSONResponse(status_code=r.status_code, content=r.json())
    except Exception as e:
        logger.exception("proxy create job failed")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/jobs")
async def list_jobs(status: Optional[str] = None, limit: int = 50, offset: int = 0):
    params = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{OPTI_BASE}/jobs", params=params)
        return JSONResponse(status_code=r.status_code, content=r.json())

@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{OPTI_BASE}/jobs/{job_id}")
        return JSONResponse(status_code=r.status_code, content=r.json())

@router.get("/jobs/{job_id}/logs")
async def get_logs(job_id: str):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{OPTI_BASE}/jobs/{job_id}/logs")
        return JSONResponse(status_code=r.status_code, content=r.json())

@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{OPTI_BASE}/jobs/{job_id}/cancel")
        return JSONResponse(status_code=r.status_code, content=r.json())

@router.get("/jobs/{job_id}/stream")
async def stream_proxy(job_id: str):
    # Proxy SSE stream
    async def gen():
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            async with client.stream("GET", f"{OPTI_BASE}/jobs/{job_id}/stream") as r:
                async for chunk in r.aiter_bytes():
                    yield chunk
    return StreamingResponse(gen(), media_type="text/event-stream")
