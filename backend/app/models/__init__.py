"""Pydantic models for request/response schemas."""
from datetime import datetime
from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, EmailStr, Field, ConfigDict
# ---------- Auth ----------
class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=6, max_length=200)
    role: Literal["creator", "editor", "viewer"] = "creator"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=16, max_length=200)
    new_password: str = Field(min_length=6, max_length=200)


class UserPublic(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    email: EmailStr
    role: str
    created_at: datetime


# ---------- Project ----------
class StructuredBrief(BaseModel):
    service: str = Field(min_length=2, max_length=300)
    offer: str = Field(min_length=2, max_length=500)
    audience: str = Field(min_length=2, max_length=300)
    location: str = Field(min_length=2, max_length=300)
    problem: Optional[str] = Field(default=None, max_length=1000)
    proof_points: List[str] = Field(default_factory=list, max_length=10)


class VerifiedCTA(BaseModel):
    text: str = Field(min_length=2, max_length=200)
    destination: str = Field(min_length=3, max_length=500)
    verified: bool = False


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    niche: str = Field(min_length=3, max_length=120)
    topic: str = Field(min_length=10, max_length=1000)
    audience: str = Field(min_length=1, max_length=200)
    tone: str = Field(min_length=1, max_length=80)
    target_duration: int = Field(ge=5, le=3600)
    voice_style: Optional[str] = "neutral male narrator"
    voice_id: Optional[str] = None
    visual_style: Optional[str] = "cinematic b-roll"
    monetisation_intent: Optional[str] = "ads + affiliate"
    cta_goal: Optional[str] = "subscribe"
    auto_post: Optional[bool] = False
    platforms: Optional[List[str]] = []
    # Commercial client ads must opt in; ordinary YouTube projects are
    # editorial. Existing projects without this field already render as
    # editorial, so the default preserves their timeline contract.
    production_mode: Literal["commercial_ad", "editorial"] = "editorial"
    client_name: Optional[str] = Field(default=None, max_length=160)
    structured_brief: Optional[StructuredBrief] = None
    verified_cta: Optional[VerifiedCTA] = None
    brand_assets_supplied: bool = False
    aspect_ratio: Optional[Literal["9:16", "1:1", "16:9"]] = "9:16"
    resolution: Optional[str] = Field(default="1080x1920", max_length=30)


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    niche: Optional[str] = None
    topic: Optional[str] = None
    audience: Optional[str] = None
    tone: Optional[str] = None
    target_duration: Optional[int] = Field(default=None, ge=5, le=3600)
    voice_style: Optional[str] = None
    voice_id: Optional[str] = None
    visual_style: Optional[str] = None
    monetisation_intent: Optional[str] = None
    cta_goal: Optional[str] = None
    status: Optional[str] = None
    auto_post: Optional[bool] = None
    platforms: Optional[List[str]] = None
    production_mode: Optional[Literal["commercial_ad", "editorial"]] = None
    client_name: Optional[str] = Field(default=None, max_length=160)
    structured_brief: Optional[StructuredBrief] = None
    verified_cta: Optional[VerifiedCTA] = None
    brand_assets_supplied: Optional[bool] = None
    aspect_ratio: Optional[Literal["9:16", "1:1", "16:9"]] = None
    resolution: Optional[str] = Field(default=None, max_length=30)


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    user_id: str
    name: str
    niche: str
    topic: str
    audience: str
    tone: str
    target_duration: int
    voice_style: str
    visual_style: str
    monetisation_intent: str
    cta_goal: str
    status: str
    quality_score: int
    estimated_cost: float
    auto_post: bool = False
    platforms: List[str] = []
    created_at: datetime
    updated_at: datetime


# ---------- Script / Scene / Metadata (output payloads) ----------
class ScriptOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    project_id: str
    hook_option_one: str
    hook_option_two: str
    hook_option_three: str
    selected_hook: str
    full_script: str
    retention_beats: List[str]
    cta_block: str
    word_count: int
    estimated_duration: int
    created_at: datetime
    updated_at: datetime


class SceneOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    project_id: str
    scene_number: int
    start_time: int
    end_time: int
    narration_text: str
    visual_direction: str
    asset_type: str
    search_terms: List[str]
    image_prompt: str
    caption_text: str
    status: str


class MetadataOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    project_id: str
    title_options: List[str]
    selected_title: str
    description: str
    tags: List[str]
    hashtags: List[str]
    chapters: List[dict]
    pinned_comment: str


class ThumbnailConcept(BaseModel):
    thumbnail_title_text: str
    visual_composition: str
    emotion_angle: str
    background_idea: str
    subject_focal_point: str
    colour_direction: str
    click_trigger: str
    image_prompt: str


# ---------- Script editing ----------
class ScriptUpdate(BaseModel):
    selected_hook: Optional[str] = None
    full_script: Optional[str] = None
    retention_beats: Optional[List[str]] = None
    cta_block: Optional[str] = None
    source: Optional[str] = None  # "manual" | "ai_generated"; explicit wins, else preserved


class MusicSelectRequest(BaseModel):
    music_gain_db: Optional[float] = None
    music_ducking_enabled: Optional[bool] = None
    rights_confirmed: Optional[bool] = None
    rights_note: Optional[str] = None


class MetadataUpdate(BaseModel):
    selected_title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    pinned_comment: Optional[str] = None
    chapters: Optional[List[dict]] = None


# ---------- Provider settings ----------
class ProviderSettingsUpdate(BaseModel):
    default_tone: Optional[str] = None
    default_visual_style: Optional[str] = None
    cost_limit_monthly: Optional[float] = None
    preferred_provider: Optional[str] = None
    # White-label branding (Advanced plan)
    brand_name: Optional[str] = Field(default=None, max_length=120)
    brand_logo_url: Optional[str] = Field(default=None, max_length=500)
    brand_color: Optional[str] = Field(default=None, max_length=20)
    white_label_enabled: Optional[bool] = None
    custom_domain: Optional[str] = Field(default=None, max_length=200)


# ---------- Asset ----------
class AssetCreate(BaseModel):
    name: str
    asset_type: str
    source: str = "generated_brief"
    tags: List[str] = []
    file_path: Optional[str] = None
    scene_id: Optional[str] = None
    provenance: Optional[str] = Field(default=None, max_length=100)
    source_url: Optional[str] = Field(default=None, max_length=1000)


# ---------- Stock / Pexels ----------
class StockAttachRequest(BaseModel):
    """Caller sends one normalised stock result as returned by /find-assets."""
    source: str = "pexels"
    external_id: str
    media_type: Literal["stock_video", "stock_image"]
    title: str
    preview_url: Optional[str] = None
    source_url: Optional[str] = None
    download_url: Optional[str] = None
    attribution_name: Optional[str] = None
    attribution_url: Optional[str] = None
    width: Optional[int] = 0
    height: Optional[int] = 0
    duration: Optional[int] = None
    tags: List[str] = []
    query: Optional[str] = None


class FindAssetsRequest(BaseModel):
    query: Optional[str] = None
    media_type: Literal["both", "videos", "photos"] = "both"
    per_page: int = Field(default=12, ge=1, le=40)


class AssetStatusUpdate(BaseModel):
    status: Literal["suggested", "attached", "rejected", "selected", "ready"]


# ---------- Auto-attach / Thumbnail images ----------
class AutoAttachRequest(BaseModel):
    replace_existing: bool = False
    media_type: Literal["both", "videos", "photos"] = "both"
    # Client-generated key for this operation. Replacement requires one; a repeated
    # request with the same key replays the stored outcome instead of attaching or
    # charging again, and a failed one resumes without re-charging finished scenes.
    idempotency_key: Optional[str] = Field(
        default=None, min_length=8, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$",
    )


class GenerateThumbnailImagesRequest(BaseModel):
    variants: int = Field(default=1, ge=1, le=3)


# ---------- Voiceover (TTS) ----------
class GenerateVoiceoverRequest(BaseModel):
    voice_style: Optional[str] = None  # narrator preset ("NEUTRAL_MALE_NARRATOR") or style word (calm, energetic, ...)
    tone: Optional[str] = None  # style/tone word tuning ElevenLabs settings: calm, energetic, dramatic, documentary, corporate, mysterious, narrator
    text_override: Optional[str] = Field(default=None, max_length=5000)


# ---------- Render ----------
class RenderStartRequest(BaseModel):
    force: Optional[bool] = False  # allow concurrent renders if explicitly forced (admin/owner)
    voice_style: Optional[str] = None  # narrator preset or style (moved from generate-script to render payload)
    voice_id: Optional[str] = None  # explicit ElevenLabs voice_id (also on projects table, not just scripts)
    burn_subtitles: Optional[bool] = False  # optional at render time, default off (auto-captions on platforms)
    aspect_ratio: Optional[str] = None  # 9:16|1:1|16:9 override at render time


class ReleaseReviewRequest(BaseModel):
    """Human visual sign-off after watching the finished client ad."""
    scene_reviews: List[Dict[str, Any]] = Field(min_length=1)
    approved: bool
    notes: Optional[str] = Field(default=None, max_length=2000)


# ---------- Share ----------
class ShareUpdate(BaseModel):
    title_override: Optional[str] = Field(default=None, max_length=200)

# --- Billing models (Paddle) ---
"""Billing models - Paddle subscription & credit transactions."""
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


def _now():
    return datetime.now(timezone.utc)


class Subscription(BaseModel):
    user_id: str
    paddle_customer_id: Optional[str] = None
    paddle_subscription_id: Optional[str] = None
    paddle_price_id: Optional[str] = None
    plan: str = Field(default="free", description="free/starter/creator/pro/agency")
    status: str = Field(default="active", description="active/canceled/past_due/inactive")
    credits_monthly: int = 30
    credits_remaining: int = 30
    current_period_end: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    class Config:
        extra = "allow"


class CreditTransaction(BaseModel):
    user_id: str
    amount: int  # positive credit, negative debit
    reason: str = Field(description="e.g. purchase starter, auto-attach scene 02, monthly reset")
    created_at: datetime = Field(default_factory=_now)

    class Config:
        extra = "allow"


# --- Release Contract (v6.2) ---
class DisclosureDecision(BaseModel):
    contains_synthetic_media: bool = False
    is_realistic_alteration: bool = False
    is_altered: bool = False
    rationale: str = Field(default="", max_length=1000)


class ReleaseManifest(BaseModel):
    tenant_id: str
    channel_id: Optional[str] = None
    project_id: str
    job_id: str
    video_hash: str  # sha256:ffprobe file
    thumbnail_hash: Optional[str] = None
    metadata_hash: Optional[str] = None
    duration: float
    disclosure_decision: DisclosureDecision = Field(default_factory=DisclosureDecision)
    qa_version: str = "v1"
    policy_version: str = "2026-09-10"
    approver: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)


class QAReport(BaseModel):
    job_id: str
    project_id: str
    version: str = "v1"
    checks: List[dict] = Field(default_factory=list)
    blocked: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


class Approval(BaseModel):
    project_id: str
    job_id: str
    video_hash: str
    manifest_hash: str
    approver_id: str
    approval_time: datetime = Field(default_factory=_now)
    expiry: Optional[datetime] = None


class RightsRecord(BaseModel):
    project_id: str
    asset_id: str
    source: Optional[str] = None
    licence: Optional[str] = None
    licence_url: Optional[str] = None
    usage: Optional[str] = None
    sha256: Optional[str] = None
    original_page_url: Optional[str] = None
    ownership_declaration: Optional[str] = None
    retrieval_time: Optional[datetime] = None
    claim: Optional[str] = None


class ClaimRecord(BaseModel):
    project_id: str
    claim_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:8])
    exact_wording: str
    source_url: Optional[str] = None
    publisher: Optional[str] = None
    source_date: Optional[datetime] = None
    retrieval_time: Optional[datetime] = None
    excerpt: Optional[str] = None
    region: Optional[str] = None
    factual_status: Optional[str] = None  # firsthand, attributed, measured, inference
    contradiction_notes: Optional[str] = None
    reviewer: Optional[str] = None
    script_location: Optional[str] = None


class IntakePacket(BaseModel):
    project_id: str
    tenant_id: Optional[str] = None
    logo: Optional[str] = None  # base64 or URL
    colours: Optional[List[str]] = None
    service_descriptions: Optional[str] = None
    candidate_images: Optional[List[dict]] = None  # [{url, original_url, ownership_declaration, intended_use, consent}]
    version: int = 1
    created_at: datetime = Field(default_factory=_now)


class PublishIntent(BaseModel):
    release_id: str
    channel_id: str
    project_id: str
    job_id: str
    status: str = "pending"  # pending, uploading, processing, published, failed
    session_ref: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
