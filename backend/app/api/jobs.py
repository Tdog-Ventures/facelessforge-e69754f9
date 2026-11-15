from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime, timedelta

try:
    from app.auth import get_current_user
except Exception:
    try:
        from backend.app.auth import get_current_user
    except Exception:
        def get_current_user(): return None

try:
    from app.db import get_db
except Exception:
    from backend.app.db import get_db

router = APIRouter()

@router.get("/jobs/test")
async def jobs_test():
    return {"status": "ok", "jobs": "wired", "ts": datetime.utcnow().isoformat()}
