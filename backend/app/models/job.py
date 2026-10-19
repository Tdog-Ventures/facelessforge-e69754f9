from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field

class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SCRIPTING = "SCRIPTING"
    SCENES = "SCENES"
    THUMBNAIL = "THUMBNAIL"
    VOICE = "VOICE"
    CLIPS = "CLIPS"
    CAPTIONS = "CAPTIONS"
    RENDERING = "RENDERING"
    READY_TO_RENDER = "READY_TO_RENDER"
    RENDER_QUEUED = "RENDER_QUEUED"
    RENDERING_EXTERNAL = "RENDERING_EXTERNAL"
    MUXING = "MUXING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class JobLedger(BaseModel):
    user_id: str
    project_id: str
    params_hash: str
    idempotency_key: Optional[str] = None
    requested_duration_seconds: int = 300
    actual_voice_duration_seconds: Optional[int] = None
    commercial_enabled: bool = False
    commercial_details: Optional[Dict[str, Any]] = None
    status: JobStatus = JobStatus.QUEUED
    progress: int = 0
    steps: Dict[str, str] = Field(default_factory=lambda: {
        "script": "pending", "scenes": "pending", "thumbnail": "pending",
        "voice": "pending", "clips": "pending", "captions": "pending", "render": "pending"
    })
    provider_used: Optional[str] = None
    cost_breakdown: Dict[str, Any] = Field(default_factory=dict)
    lease_expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    billed: bool = False
    transaction_id: Optional[str] = None

    class Config:
        extra = "allow"
