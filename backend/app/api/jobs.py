from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta
import hashlib, json

try:
    from app.auth import get_current_user
except Exception:
    try:
        from backend.app.auth import get_current_user
    except Exception:
        def get_current_user(): return None

try:
    from app.models.job import JobLedger, JobStatus
except Exception:
    try:
        from backend.app.models.job import JobLedger, JobStatus
    except Exception:
        from deploy.backend.app.models.job import JobLedger, JobStatus

try:
    from app.db import get_db
except Exception:
    try:
        from backend.app.db import get_db
    except Exception:
        from deploy.backend.app.db import get_db

router = APIRouter()

class GenerateRequest(BaseModel):
    project_id: str
    requested_duration_seconds: int = 300
    commercial_enabled: bool = False
    commercial_details: dict | None = None
    idempotency_key: str | None = None

def make_idempotency_hash(user_id, project_id, params):
    raw = f"{user_id}:{project_id}:{json.dumps(params, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]

@router.get("/jobs/test")
async def jobs_test():
    return {"status": "ok", "jobs": "wired", "ts": datetime.utcnow().isoformat()}
