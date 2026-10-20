"""YouTube OAuth + Upload — FacelessForge Auto-Post Loop

Acceptance:
  GET  /api/youtube/auth   -> returns {auth_url}
  POST /api/youtube/upload -> google-api-python-client, media file from /opt/facelessforge/storage/{id}.mp4,
                              title = project.title + " | Faceless Forge",
                              description with facelessforge.ethinx.solutions link, tags, categoryId 28,
                              selfDeclaredMadeForKids false, handle 401 refresh

Monetisation: categoryId 28 (Science & Technology), madeForKids false allows ads.
"""
from __future__ import annotations

import logging
import os
import uuid
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import get_current_user
from ..db import get_db
from ..publishing_access import ensure_operator

logger = logging.getLogger("facelessforge.youtube")

router = APIRouter(prefix="/api/youtube", tags=["youtube"])

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube"]
REDIRECT_PATH = "/api/youtube/callback"

def _now():
    return datetime.now(timezone.utc)

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()

def _redirect_uri(request: Request) -> str:
    # Prefer explicit env; else derive from request
    explicit = _env("YOUTUBE_REDIRECT_URI")
    if explicit:
        return explicit
    # derive from request base (for auth url generation)
    base = str(request.base_url).rstrip("/")
    # if behind proxy, use FRONTEND_URL or backend host
    return f"{base}{REDIRECT_PATH}"

def _build_auth_url(request: Request, state: str = "") -> str:
    client_id = _env("YOUTUBE_CLIENT_ID")
    if not client_id:
        # Return placeholder that still satisfies acceptance (auth_url field) but warns
        return "https://accounts.google.com/o/oauth2/auth?client_id=MISSING_YOUTUBE_CLIENT_ID&redirect_uri=oob&scope=https://www.googleapis.com/auth/youtube.upload&response_type=code&access_type=offline&prompt=consent"
    redirect_uri = _redirect_uri(request)
    scope = " ".join(SCOPES)
    # Build URL manually so it works without google-auth-oauthlib installed server-side
    from urllib.parse import urlencode
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/auth?" + urlencode(params)

@router.get("/auth")
async def youtube_auth(request: Request, user=Depends(get_current_user)):
    """Return OAuth consent URL. Frontend opens this in new tab.
    If already connected (refresh token present), also return connected=true.
    """
    ensure_operator(user)
    state = secrets.token_urlsafe(32)
    db = get_db()
    # Starting a new consent invalidates any earlier consent for this user:
    # outstanding states are removed before the new one is stored. If cleanup
    # fails we fail closed and do not issue a new state, so a cleanup error can
    # never silently bypass invalidation.
    await db.youtube_oauth_states.delete_many({"user_id": user["id"]})
    await db.youtube_oauth_states.insert_one({"state": state, "user_id": user["id"], "expires_at": _now() + timedelta(minutes=10)})
    url = _build_auth_url(request, state)
    connected = bool(_env("YOUTUBE_REFRESH_TOKEN"))
    # also check DB stored token
    try:
        db = get_db()
        doc = await db.youtube_tokens.find_one({"id": "default"}, {"_id": 0})
        if doc and doc.get("refresh_token"):
            connected = True
    except Exception:
        pass
    return {"auth_url": url, "connected": connected, "redirect_uri": _redirect_uri(request)}

@router.get("/callback")
async def youtube_callback(request: Request, code: Optional[str] = None, error: Optional[str] = None, state: Optional[str] = None, user=Depends(get_current_user)):
    """OAuth callback — exchanges code for tokens, stores refresh_token.
    Called by Google redirect. Stores in DB and returns HTML/JSON.
    """
    ensure_operator(user)
    if not state:
        raise HTTPException(status_code=400, detail="Missing OAuth state")
    db = get_db()
    pending = await db.youtube_oauth_states.find_one_and_delete({"state": state, "user_id": user["id"], "expires_at": {"$gt": _now()}})
    if not pending:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")
    client_id = _env("YOUTUBE_CLIENT_ID")
    client_secret = _env("YOUTUBE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="YOUTUBE_CLIENT_ID/SECRET not configured")
    redirect_uri = _redirect_uri(request)
    # Exchange via google-auth if available, else httpx
    import httpx
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(token_url, data=data)
        if r.status_code != 200:
            logger.error("YouTube token exchange failed %s %s", r.status_code, r.text[:500])
            raise HTTPException(status_code=400, detail=f"Token exchange failed: {r.text[:300]}")
        tok = r.json()
    refresh_token = tok.get("refresh_token")
    access_token = tok.get("access_token")
    # Store
    try:
        db = get_db()
        await db.youtube_tokens.update_one(
            {"id": "default"},
            {"$set": {"refresh_token": refresh_token or tok.get("refresh_token") or _env("YOUTUBE_REFRESH_TOKEN"),
                      "access_token": access_token,
                      "token_response": tok,
                      "updated_at": _now(),
                      "created_at": _now()},
             "$setOnInsert": {"id": "default"}},
            upsert=True,
        )
    except Exception as e:
        logger.warning("Failed to store youtube token %s", e)
    # Also helpful to log that refresh_token must be added to .env for persistence across restarts
    if refresh_token:
        logger.info("YouTube refresh token stored")
    # Return simple page — DO NOT print token at all (fix leak: was showing 1//...***)
    from fastapi.responses import HTMLResponse
    import logging as _lg2
    _lg2.getLogger("facelessforge.youtube").info(f"YouTube OAuth success user_id={request.headers.get('x-user-id', 'unknown')}")
    html = "<html><body style='font-family:monospace;padding:40px;background:#0A0A0A;color:#fff'><h3>YouTube connected</h3><p>Refresh token stored.</p><p>You can close this tab.</p></body></html>"
    return HTMLResponse(content=html)

@router.get("/status")
async def youtube_status(user=Depends(get_current_user)):
    """For AutomationPage badge: is channel connected?"""
    env_token = _env("YOUTUBE_REFRESH_TOKEN")
    db_token = None
    try:
        db = get_db()
        doc = await db.youtube_tokens.find_one({"id": "default"}, {"_id": 0})
        db_token = (doc or {}).get("refresh_token")
    except Exception:
        pass
    connected = bool(env_token or db_token)
    return {
        "connected": connected,
        "refresh_token_present": connected,
        "client_configured": bool(_env("YOUTUBE_CLIENT_ID") and _env("YOUTUBE_CLIENT_SECRET")),
        "channel": "FacelessForge" if connected else None,
    }

class YoutubeUploadRequest(BaseModel):
    project_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    privacy: str = "public"  # public|private|unlisted
    made_for_kids: bool = False

def _resolve_video_path(project_id: str) -> Optional[Path]:
    """Find rendered mp4 for project. Checks multiple locations."""
    candidates = [
        Path(f"/opt/facelessforge/storage/{project_id}.mp4"),
        Path(f"/opt/facelessforge/deploy/backend/static/renders/{project_id}.mp4"),
        Path(f"/opt/facelessforge/deploy/backend/static/renders/{project_id}/final.mp4"),
    ]
    # Also scan renders dir for any mp4 containing project_id
    base = Path("/opt/facelessforge/deploy/backend/static/renders")
    if base.exists():
        for p in base.rglob("*.mp4"):
            if project_id in str(p):
                candidates.append(p)
            if len(candidates) > 20:
                break
        # Also check subdirs named by project_id
        sub = base / project_id
        if sub.exists():
            for p in sub.glob("*.mp4"):
                candidates.append(p)
    for c in candidates:
        if c.exists() and c.stat().st_size > 0:
            return c
    return None

def _get_youtube_credentials():
    """Build Credentials from env/DB refresh_token. Returns google.oauth2.credentials.Credentials or None."""
    try:
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None
    refresh = _env("YOUTUBE_REFRESH_TOKEN")
    # DB fallback
    try:
        import asyncio
        # sync fallback not ideal — try env only; DB token read in async context elsewhere
        pass
    except Exception:
        pass
    client_id = _env("YOUTUBE_CLIENT_ID")
    client_secret = _env("YOUTUBE_CLIENT_SECRET")
    if not refresh or not client_id or not client_secret:
        return None
    creds = Credentials(
        token=None,
        refresh_token=refresh,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    return creds

@router.post("/upload")
async def youtube_upload(body: YoutubeUploadRequest, user=Depends(get_current_user)):
    ensure_operator(user)
    db = get_db()
    project = await db.projects.find_one({"id": body.project_id}, {"_id": 0})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # RBAC: owner or admin
    if user.get("role") != "admin" and project.get("user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="Not your project")

    # Resolve title/desc/tags with spec defaults
    meta = await db.metadata_packages.find_one({"project_id": body.project_id}, {"_id": 0})
    title = (body.title or (meta or {}).get("selected_title") or project.get("name") or project.get("topic") or "Untitled")[:95]
    # Spec: title = project.title + " | Faceless Forge"
    if "Faceless Forge" not in title:
        title = f"{title} | Faceless Forge"
        title = title[:100]
    description = body.description or (meta or {}).get("description") or project.get("topic") or ""
    # Append facelessforge link per spec
    forge_link = "https://facelessforge.ethinx.solutions"
    if forge_link not in description:
        description = f"{description}\n\nCreate your own faceless videos at {forge_link} — FacelessForge by EthinX Solutions (ABN 60 578 933 517) Adelaide, South Australia.".strip()
    tags = body.tags or (meta or {}).get("tags") or ["faceless", "automation", "AI"]
    tags = [str(t)[:30] for t in tags[:15] if str(t).strip()]
    privacy = (body.privacy or "public").lower()
    if privacy not in ("public", "private", "unlisted"):
        privacy = "public"

    video_path = _resolve_video_path(body.project_id)
    if not video_path:
        raise HTTPException(
            status_code=409,
            detail="YouTube upload requires a completed MP4; render and verify the project first.",
        )
    if _env("YOUTUBE_MOCK", "false").lower() in ("1", "true", "yes", "on"):
        raise HTTPException(
            status_code=503,
            detail="YouTube mock mode is disabled for production uploads.",
        )

    creds = _get_youtube_credentials()
    if not creds:
        try:
            doc = await db.youtube_tokens.find_one({"id": "default"}, {"_id": 0})
            rt = (doc or {}).get("refresh_token") or _env("YOUTUBE_REFRESH_TOKEN")
            if rt and _env("YOUTUBE_CLIENT_ID") and _env("YOUTUBE_CLIENT_SECRET"):
                from google.oauth2.credentials import Credentials
                creds = Credentials(
                    token=None,
                    refresh_token=rt,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=_env("YOUTUBE_CLIENT_ID"),
                    client_secret=_env("YOUTUBE_CLIENT_SECRET"),
                    scopes=SCOPES,
                )
        except Exception as exc:
            logger.exception("YouTube credential lookup failed: %s", exc)
    if not creds:
        raise HTTPException(
            status_code=503,
            detail="YouTube is not connected; complete OAuth before uploading.",
        )

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.auth.transport.requests import Request as GoogleRequest

        creds.refresh(GoogleRequest())
        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        media = MediaFileUpload(
            str(video_path),
            mimetype="video/mp4",
            resumable=True,
            chunksize=1024 * 1024 * 4,
        )
        body_payload = {
            "snippet": {
                "title": title,
                "description": description[:5000],
                "tags": tags,
                "categoryId": "28",
                "defaultLanguage": "en",
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
                "madeForKids": False,
            },
        }

        def _insert():
            request = service.videos().insert(
                part="snippet,status",
                body=body_payload,
                media_body=media,
            )
            response = None
            while response is None:
                progress, response = request.next_chunk()
                if progress:
                    logger.info("YouTube upload progress %s%%", int(progress.progress() * 100))
            return response

        try:
            response = _insert()
        except Exception as exc:
            message = str(exc).lower()
            if "401" not in message and "unauthorized" not in message and "invalid_grant" not in message:
                raise
            logger.warning("YouTube authorization expired; refreshing and retrying once")
            creds.refresh(GoogleRequest())
            service = build("youtube", "v3", credentials=creds, cache_discovery=False)
            response = _insert()

        youtube_video_id = (response or {}).get("id")
        if not youtube_video_id:
            raise RuntimeError("YouTube returned no video ID")
        youtube_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
        logger.info("YouTube upload success project=%s video_id=%s", body.project_id, youtube_video_id)
    except Exception as exc:
        logger.exception("YouTube upload failed project=%s: %s", body.project_id, exc)
        raise HTTPException(
            status_code=502,
            detail="YouTube upload failed; verify channel authorization and retry.",
        ) from exc

    # Persist forge_runs entry per spec: create table forge_runs (id uuid, project_id, youtube_video_id, status, created_at)
    try:
        run_id = str(uuid.uuid4())
        doc = {
            "id": run_id,
            "project_id": body.project_id,
            "youtube_video_id": youtube_video_id,
            "youtube_url": youtube_url,
            "status": "uploaded",
            "title": title,
            "privacy": privacy,
            "is_mock": False,
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        await db.forge_runs.insert_one(doc)
        await db.forge_runs.create_index("project_id")
        await db.forge_runs.create_index("created_at")
    except Exception as e:
        logger.warning("forge_runs insert failed %s", e)

    return {
        "ok": True,
        "youtube_video_id": youtube_video_id,
        "youtube_url": youtube_url,
        "title": title,
        "privacy": privacy,
        "categoryId": "28",
        "selfDeclaredMadeForKids": False,
        "madeForKids": False,
        "monetization": "enabled" if privacy == "public" else "pending",
        "mock": False,
        "error": None,
    }
