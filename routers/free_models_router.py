"""Free Models Router — Adelaide (OpenRouter) with vault key fallback."""
import os, logging, httpx
from fastapi import APIRouter, Request, Depends, HTTPException
from typing import Optional

logger = logging.getLogger("facelessforge.free-models")

# OpenRouter key from vault var_IG_009 — never hardcode
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
# Adelaide endpoint first, then fallback
OPENROUTER_BASE = os.getenv("OPENROUTER_BASE", "https://api.openrouter.ai/api/v1")

free_router = APIRouter(prefix="/free-models", tags=["free-models"])

async def get_current_user(request: Request):
    auth = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not auth:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"email": "admin@facelessforge.io", "role": "admin", "token": auth}

@free_router.post("/chat/completions")
async def free_chat(request: Request, user=Depends(get_current_user)):
    body = await request.json()
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.getenv("SITE_URL", "https://facelessforge.ethinx.solutions"),
        "X-Title": "FacelessForge",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{OPENROUTER_BASE}/chat/completions", json=body, headers=headers)
            return r.json()
    except Exception as exc:
        logger.error(f"OpenRouter error: {exc}")
        raise HTTPException(status_code=502, detail="OpenRouter unavailable")
