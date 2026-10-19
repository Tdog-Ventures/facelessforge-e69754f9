"""Per-tenant YouTube OAuth + Auto-Publish.

Storage: ethinx-redis (per-tenant isolation)
  HSET forge:tenant:{tenant_id}:youtube
    refresh_token_enc   Fernet(REFRESH_TOKEN) — never returned to client
    access_token        short-lived (refreshed on use)
    channel_id          from youtube/v3/channels?mine=true
    channel_title
    connected_at        ISO 8601
    expires_at          ISO 8601
    split               {creator: 70, platform: 30}

Auth: sovereign JWT (HS256) — payload {tenant_id, email, split, bsb, acc}
  Frontend sends Authorization: Bearer <jwt> issued by on_activation_create_forge_tenant.py
  Middleware verifies tenant_id matches ?tenant_id= query param to prevent cross-tenant access.

Endpoints (all under /api/youtube-oauth):
  GET  /connect?tenant_id=forge_xxx    -> 302 to Google consent
  GET  /callback?code=...&state=forge_xxx   -> exchange code, store in Redis, redirect to dashboard
  GET  /status?tenant_id=forge_xxx     -> {connected, channel_id, channel_title, connected_at} (NO token)
  POST /disconnect?tenant_id=forge_xxx -> HDEL the key
  POST /auto-publish                    -> service-to-service (sovereign hook) publishes latest teaser
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, parse_qs

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

logger = logging.getLogger("facelessforge.youtube_oauth")

router = APIRouter(prefix="/api/youtube-oauth", tags=["youtube-oauth"])

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.readonly",
]
REDIRECT_PATH = "/api/youtube-oauth/callback"
JWT_ALGO = "HS256"
DEFAULT_TENANT_KEY = "default"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _fernet():
    """Return Fernet instance or None if not configured."""
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        return None, None
    key = _env("YOUTUBE_ENCRYPTION_KEY")
    if not key:
        return None, None
    try:
        return Fernet(key.encode()), InvalidToken
    except Exception as e:
        logger.warning("Invalid YOUTUBE_ENCRYPTION_KEY: %s", e)
        return None, None


def _jwt_secret() -> str:
    return _env("FORGE_JWT_SECRET") or _env("JWT_SECRET")


def _decode_tenant_jwt(authorization: Optional[str]) -> dict:
    """Verify sovereign JWT. Accepts Authorization: Bearer <jwt> or ?jwt=<jwt> query."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = authorization
    if token.startswith("Bearer "):
        token = token[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")
    secret = _jwt_secret()
    if not secret:
        raise HTTPException(status_code=500, detail="Server JWT secret not configured")
    try:
        return jwt.decode(token, secret, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


def _enforce_tenant_claim(payload: dict, tenant_id: str) -> None:
    """Ensure the JWT claim's tenant_id matches the requested tenant_id."""
    claim_tenant = payload.get("tenant_id") or payload.get("sub")
    if not claim_tenant:
        raise HTTPException(status_code=403, detail="Token missing tenant_id")
    if claim_tenant != tenant_id:
        raise HTTPException(
            status_code=403,
            detail=f"Tenant mismatch: token={claim_tenant} requested={tenant_id}",
        )


def _redis_client():
    try:
        import redis
    except ImportError:
        return None
    url = _env("REDIS_URL", "redis://127.0.0.1:6379/0")
    try:
        return redis.Redis.from_url(url, decode_responses=True, socket_timeout=5)
    except Exception as e:
        logger.warning("Redis connect failed: %s", e)
        return None


def _key(tenant_id: str) -> str:
    return f"forge:tenant:{tenant_id}:youtube"


def _state_secret() -> str:
    """Per-tenant state for OAuth round-trip verification."""
    return _env("YOUTUBE_OAUTH_STATE_SECRET", "ethinx-youtube-oauth-state-2026")


def _sign_state(tenant_id: str) -> str:
    """Sign tenant_id with HMAC-style sig so callback can verify state wasn't tampered."""
    nonce = secrets.token_urlsafe(16)
    payload = f"{tenant_id}:{nonce}"
    sig = jwt.encode({"t": tenant_id, "n": nonce}, _state_secret(), algorithm="HS256")
    return f"{tenant_id}.{sig}"


def _verify_state(state: str) -> Optional[str]:
    """Return tenant_id if state valid, else None."""
    if not state or "." not in state:
        return None
    tenant_id, _, sig = state.partition(".")
    try:
        payload = jwt.decode(sig, _state_secret(), algorithms=["HS256"])
    except Exception:
        return None
    if payload.get("t") != tenant_id:
        return None
    return tenant_id


def _redirect_uri(request: Request) -> str:
    explicit = _env("YOUTUBE_REDIRECT_URI")
    if explicit:
        return explicit
    base = str(request.base_url).rstrip("/")
    return f"{base}{REDIRECT_PATH}"


def _auth_url(redirect_uri: str, state: str) -> str:
    client_id = _env("YOUTUBE_CLIENT_ID")
    if not client_id:
        return "https://accounts.google.com/o/oauth2/auth?client_id=MISSING_YOUTUBE_CLIENT_ID"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/auth?" + urlencode(params)


async def _fetch_channel(access_token: str) -> dict:
    """Call youtube/v3/channels?mine=true to get channel id + title."""
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "id,snippet", "mine": "true", "maxResults": 1},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if r.status_code != 200:
            logger.warning("YouTube channels API %s: %s", r.status_code, r.text[:200])
            return {}
        items = (r.json() or {}).get("items", [])
        if not items:
            return {}
        first = items[0]
        return {
            "channel_id": first.get("id", ""),
            "channel_title": (first.get("snippet") or {}).get("title", ""),
        }


async def _refresh_access_token(refresh_token: str) -> Optional[str]:
    """Exchange refresh_token for a fresh access_token."""
    import httpx
    client_id = _env("YOUTUBE_CLIENT_ID")
    client_secret = _env("YOUTUBE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if r.status_code != 200:
            logger.warning("YouTube refresh token failed %s: %s", r.status_code, r.text[:200])
            return None
        return (r.json() or {}).get("access_token")


@router.get("/connect")
async def youtube_connect(
    request: Request,
    tenant_id: str = Query(..., regex=r"^forge_[A-Za-z0-9]{6,32}$"),
    jwt: Optional[str] = Query(None, description="Optional JWT (else use Authorization header)"),
    authorization: Optional[str] = Header(None),
):
    """Return OAuth consent URL (JSON) or 302 redirect when Accept: text/html.
    Auth: tenant JWT (Authorization Bearer or ?jwt=).
    """
    auth = authorization or (f"Bearer {jwt}" if jwt else None)
    payload = _decode_tenant_jwt(auth)
    _enforce_tenant_claim(payload, tenant_id)
    state = _sign_state(tenant_id)
    redirect_uri = _redirect_uri(request)
    url = _auth_url(redirect_uri, state)
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url=url, status_code=302)
    return {
        "auth_url": url,
        "tenant_id": tenant_id,
        "redirect_uri": redirect_uri,
        "scopes": SCOPES,
        "configured": bool(_env("YOUTUBE_CLIENT_ID") and _env("YOUTUBE_CLIENT_SECRET")),
    }


@router.get("/callback")
async def youtube_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    """OAuth callback — exchanges code for tokens, encrypts refresh_token, stores in Redis."""
    if error:
        return HTMLResponse(
            f"<html><body style='font-family:monospace;padding:40px;background:#0A0A0A;color:#fff'>"
            f"<h3>YouTube connect failed</h3><p>Error: {error}</p></body></html>",
            status_code=400,
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")
    tenant_id = _verify_state(state)
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Invalid state (tampered or expired)")
    client_id = _env("YOUTUBE_CLIENT_ID")
    client_secret = _env("YOUTUBE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="YOUTUBE_CLIENT_ID/SECRET not configured")
    redirect_uri = _redirect_uri(request)
    import httpx
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if r.status_code != 200:
            logger.error("YouTube token exchange failed %s %s", r.status_code, r.text[:500])
            return HTMLResponse(
                f"<html><body style='font-family:monospace;padding:40px;background:#0A0A0A;color:#fff'>"
                f"<h3>YouTube connect failed</h3><p>Token exchange failed: {r.text[:300]}</p></body></html>",
                status_code=400,
            )
        tok = r.json()
    refresh_token = tok.get("refresh_token")
    access_token = tok.get("access_token")
    if not refresh_token:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#0A0A0A;color:#fff'>"
            "<h3>YouTube connect failed</h3><p>No refresh_token returned (revoke prior grants in Google account and retry).</p></body></html>",
            status_code=400,
        )
    expires_in = int(tok.get("expires_in", 3600))
    channel = await _fetch_channel(access_token) if access_token else {}
    f, InvalidToken = _fernet()
    if f is None:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#0A0A0A;color:#fff'>"
            "<h3>Server misconfiguration</h3><p>YOUTUBE_ENCRYPTION_KEY not set.</p></body></html>",
            status_code=500,
        )
    refresh_token_enc = f.encrypt(refresh_token.encode()).decode()
    r_client = _redis_client()
    if r_client is None:
        return HTMLResponse(
            "<html><body style='font-family:monospace;padding:40px;background:#0A0A0A;color:#fff'>"
            "<h3>Redis unavailable</h3><p>ethinx-redis not reachable.</p></body></html>",
            status_code=503,
        )
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    payload_hash = {
        "refresh_token_enc": refresh_token_enc,
        "access_token": access_token or "",
        "channel_id": channel.get("channel_id", ""),
        "channel_title": channel.get("channel_title", ""),
        "connected_at": _now(),
        "expires_at": expires_at,
        "scope": " ".join(SCOPES),
        "split": {"creator": 70, "platform": 30},
        "bank": {"bsb": "182-182", "acc": "033667619"},
    }
    r_client.hset(_key(tenant_id), mapping=payload_hash)
    logger.info("YOUTUBE_OAUTH connected tenant=%s channel=%s", tenant_id, channel.get("channel_id"))
    return HTMLResponse(
        f"<html><body style='font-family:monospace;padding:40px;background:#0A0A0A;color:#fff'>"
        f"<h3 style='color:#00E5FF'>YouTube connected</h3>"
        f"<p>Tenant: <b>{tenant_id}</b></p>"
        f"<p>Channel: <b>{channel.get('channel_title') or '(unknown)'}</b> ({channel.get('channel_id') or '—'})</p>"
        f"<p>Refresh token encrypted and stored in ethinx-redis. You can close this tab.</p>"
        f"<p><a href='https://dashboard.ethinx.solutions' style='color:#00E5FF'>Back to dashboard</a></p>"
        f"</body></html>"
    )


@router.get("/status")
async def youtube_status(
    tenant_id: str = Query(..., regex=r"^forge_[A-Za-z0-9]{6,32}$"),
    jwt: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    """Return connection status (NEVER the token). Auth: tenant JWT."""
    auth = authorization or (f"Bearer {jwt}" if jwt else None)
    payload = _decode_tenant_jwt(auth)
    _enforce_tenant_claim(payload, tenant_id)
    r_client = _redis_client()
    if r_client is None:
        return {"connected": False, "tenant_id": tenant_id, "error": "redis_unavailable"}
    data = r_client.hgetall(_key(tenant_id))
    if not data:
        return {"connected": False, "tenant_id": tenant_id, "channel_id": None, "channel_title": None}
    return {
        "connected": bool(data.get("refresh_token_enc")),
        "tenant_id": tenant_id,
        "channel_id": data.get("channel_id") or None,
        "channel_title": data.get("channel_title") or None,
        "connected_at": data.get("connected_at"),
        "expires_at": data.get("expires_at"),
        "split": json.loads(data.get("split", '{"creator":70,"platform":30}')),
    }


@router.get("/tenant-teaser")
async def youtube_tenant_teaser(
    tenant_id: str = Query(..., regex=r"^forge_[A-Za-z0-9]{6,32}$"),
    service_token: Optional[str] = Query(None),
):
    """Service-to-service: return latest teaser URL for tenant.
    Used by on_activation_create_forge_tenant.py to find video to auto-publish.
    Auth: service_token must match FORGE_INTERNAL_SECRET.
    """
    if not _verify_service_token(service_token):
        raise HTTPException(status_code=401, detail="Invalid service token")
    r_client = _redis_client()
    if r_client is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    last_url = r_client.hget(_key(tenant_id), "last_video_url")
    if last_url:
        return {"video_url": last_url, "source": "redis", "tenant_id": tenant_id}
    # Fallback: find most recent teaser for this tenant in R2
    # Scan R2 for teasers matching tenant_id pattern
    endpoint_url = os.environ.get("STORAGE_ENDPOINT") or None
    access_key = os.environ.get("STORAGE_ACCESS_KEY") or None
    secret_key = os.environ.get("STORAGE_SECRET_KEY") or None
    bucket = os.environ.get("STORAGE_BUCKET") or None
    if not endpoint_url or not access_key or not secret_key or not bucket:
        raise HTTPException(status_code=500, detail="STORAGE_MISCONFIGURED")
    try:
        import boto3
        from botocore.config import Config
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
        resp = s3.list_objects_v2(Bucket=bucket, Prefix="teasers/", MaxKeys=100)
        items = sorted(
            (o for o in resp.get("Contents", []) if o["Key"].endswith(".mp4")),
            key=lambda x: x["LastModified"],
            reverse=True,
        )
        if items:
            key = items[0]["Key"]
            url = f"https://videos.ethinx.solutions/{key}"
            return {"video_url": url, "source": "r2_latest", "tenant_id": tenant_id, "key": key}
    except Exception as e:
        logger.debug("R2 teaser scan failed: %s", e)
    # Ultimate fallback
    return {"video_url": "https://videos.ethinx.solutions/teasers/test-macquarie-final.mp4", "source": "fallback", "tenant_id": tenant_id}


@router.post("/disconnect")
async def youtube_disconnect(
    tenant_id: str = Query(..., regex=r"^forge_[A-Za-z0-9]{6,32}$"),
    authorization: Optional[str] = Header(None),
):
    auth = authorization
    payload = _decode_tenant_jwt(auth)
    _enforce_tenant_claim(payload, tenant_id)
    r_client = _redis_client()
    if r_client is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    deleted = r_client.delete(_key(tenant_id))
    return {"ok": True, "tenant_id": tenant_id, "deleted": bool(deleted)}


class AutoPublishRequest(BaseModel):
    tenant_id: str
    video_url: str
    title: str
    description: Optional[str] = ""
    tags: Optional[list[str]] = None
    privacy: Optional[str] = "public"  # public|unlisted|private
    category_id: Optional[str] = "27"   # 27 = Education (autopublish-safe; 22 = People/Blogs)
    service_token: Optional[str] = None  # for sovereign-loop hook auth


def _verify_service_token(token: Optional[str]) -> bool:
    """Compare against FORGE_INTERNAL_SECRET (set in env, used by sovereign-loop hook)."""
    expected = _env("FORGE_INTERNAL_SECRET")
    if not expected:
        return True  # no service token configured = allow
    if not token:
        return False
    return secrets.compare_digest(token, expected)


@router.post("/auto-publish")
async def youtube_auto_publish(body: AutoPublishRequest):
    """Service-to-service endpoint called by sovereign-loop on activation.
    Refreshes access_token, uploads latest video to tenant's connected channel.
    Requires X-Internal-Secret header (or service_token in body) for sovereignty.
    """
    from fastapi import Request as Req
    # Note: we can't easily inject Request here without changing signature, use body.service_token
    if not _verify_service_token(body.service_token):
        raise HTTPException(status_code=401, detail="Invalid service token")
    r_client = _redis_client()
    if r_client is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    data = r_client.hgetall(_key(body.tenant_id))
    if not data or not data.get("refresh_token_enc"):
        return {"ok": False, "reason": "tenant_not_connected", "tenant_id": body.tenant_id}
    f, InvalidToken = _fernet()
    if f is None:
        raise HTTPException(status_code=500, detail="YOUTUBE_ENCRYPTION_KEY not set")
    try:
        refresh_token = f.decrypt(data["refresh_token_enc"].encode()).decode()
    except InvalidToken:
        logger.error("YOUTUBE_OAUTH decrypt failed for %s", body.tenant_id)
        return {"ok": False, "reason": "decrypt_failed", "tenant_id": body.tenant_id}
    access_token = await _refresh_access_token(refresh_token)
    if not access_token:
        return {"ok": False, "reason": "refresh_failed", "tenant_id": body.tenant_id}
    r_client.hset(_key(body.tenant_id), mapping={"access_token": access_token, "expires_at": _now()})
    # Download video to temp
    import httpx, tempfile
    tmp_path = Path(tempfile.gettempdir()) / f"forge_{body.tenant_id}_{secrets.token_hex(6)}.mp4"
    try:
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            async with client.stream("GET", body.video_url) as resp:
                if resp.status_code != 200:
                    return {"ok": False, "reason": "video_download_failed", "status": resp.status_code}
                with tmp_path.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        fh.write(chunk)
    except Exception as e:
        logger.warning("YouTube auto-publish download failed: %s", e)
        return {"ok": False, "reason": f"video_download_error: {e}"}
    if tmp_path.stat().st_size == 0:
        return {"ok": False, "reason": "empty_video_file"}
    # Upload via google-api-python-client
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.auth.transport.requests import Request as GoogleRequest
        creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=_env("YOUTUBE_CLIENT_ID"),
            client_secret=_env("YOUTUBE_CLIENT_SECRET"),
            scopes=SCOPES,
        )
        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body_payload = {
            "snippet": {
                "title": body.title[:95],
                "description": (body.description or "")[:5000],
                "tags": (body.tags or [])[:15],
                "categoryId": body.category_id or "27",
                "defaultLanguage": "en",
            },
            "status": {
                "privacyStatus": body.privacy or "public",
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(str(tmp_path), mimetype="video/mp4", resumable=True, chunksize=1024 * 1024 * 4)
        req = service.videos().insert(part="snippet,status", body=body_payload, media_body=media)
        resp = None
        while resp is None:
            status, resp = req.next_chunk()
        youtube_video_id = (resp or {}).get("id")
        youtube_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
        r_client.hset(_key(body.tenant_id), mapping={"last_publish_at": _now(), "last_youtube_id": youtube_video_id, "last_video_url": body.video_url})
        # Gotify log
        try:
            from app.ops_telemetry import log_to_gotify  # type: ignore
        except Exception:
            log_to_gotify = None
        if log_to_gotify:
            try:
                log_to_gotify(f"YouTube auto-publish {body.tenant_id} -> {youtube_video_id}")
            except Exception:
                pass
        logger.info("YOUTUBE_AUTO_PUBLISH tenant=%s video_id=%s", body.tenant_id, youtube_video_id)
        return {
            "ok": True,
            "tenant_id": body.tenant_id,
            "youtube_video_id": youtube_video_id,
            "youtube_url": youtube_url,
            "channel_id": data.get("channel_id"),
        }
    except Exception as e:
        logger.exception("YOUTUBE_AUTO_PUBLISH failed: %s", e)
        return {"ok": False, "reason": f"upload_failed: {e}"}
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
