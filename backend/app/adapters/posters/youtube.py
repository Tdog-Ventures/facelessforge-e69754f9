"""YouTube adapter - uses existing youtube route logic, vault YOUTUBE_REFRESH_TOKEN"""
from .base import PlatformPoster, compliance_check, rate_limit_check, log_analytics
import os, uuid, logging
from datetime import datetime, timezone
logger = logging.getLogger("autopost.youtube")
class YoutubePoster(PlatformPoster):
    platform = "youtube"
    def is_configured(self) -> bool:
        return bool(self.vault_tokens.get("youtube_refresh"))
    async def post(self, project: dict, video_url: str, metadata: dict, tenant_id: str) -> dict:
        from ...publishing_access import ensure_operator_project
        await ensure_operator_project(project)
        title = (metadata.get("selected_title") or project.get("name") or project.get("topic") or "")[:95]
        if "Faceless Forge" not in title:
            title = f"{title} | Faceless Forge"
        description = metadata.get("description") or project.get("topic") or ""
        duration = metadata.get("duration") or project.get("target_duration") or 76
        ok, reason = compliance_check("youtube", title, description, duration)
        if not ok:
            return {"platform":"youtube","status":"blocked","reason":reason}
        allowed, remaining = rate_limit_check("youtube", tenant_id)
        if not allowed:
            return {"platform":"youtube","status":"rate_limited","retry_after": remaining}
        # Not configured: fail loud, no mock ID (BOOT.md HARD RULE 3)
        if not self.is_configured():
            raise RuntimeError("YouTube upload failed — not configured, mock IDs forbidden per BOOT.md HARD RULE 3")
        # Real upload - reuse existing logic from routes/youtube.py
        try:
            from ...routes.youtube import _get_youtube_credentials, _resolve_video_path
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
            from google.auth.transport.requests import Request as GoogleRequest
            creds = _get_youtube_credentials()
            creds.refresh(GoogleRequest())
            service = build("youtube","v3", credentials=creds, cache_discovery=False)
            video_path = _resolve_video_path(project["id"])
            if not video_path:
                raise RuntimeError("video_path not found")
            media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
            body = {"snippet":{"title":title,"description":description[:5000],"tags": metadata.get("tags",[])[:15],"categoryId":"28"},"status":{"privacyStatus":"public","selfDeclaredMadeForKids":False}}
            req = service.videos().insert(part="snippet,status", body=body, media_body=media)
            resp=None
            while resp is None:
                _, resp = req.next_chunk()
            vid = (resp or {}).get("id")
            if not vid:
                raise RuntimeError("YouTube returned no valid video id")
            url = f"https://www.youtube.com/watch?v={vid}"
            res={"platform":"youtube","status":"uploaded","video_id":vid,"url":url,"title":title}
            log_analytics("youtube", tenant_id, project.get("id"), res)
            return res
        except Exception as e:
            logger.warning("YT real upload failed (%s)", type(e).__name__)
            raise RuntimeError("YouTube upload failed — provider rejected the upload") from None
