"""Facebook Page Reels via the Graph API. Unconfigured returns not_configured; provider failure raises."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .base import GRAPH_BASE, PLATFORM_LIMITS, PlatformPoster, compliance_check, rate_limit_check, log_analytics

logger = logging.getLogger("autopost.facebook")

_POLL_INTERVAL = 5
_POLL_TIMEOUT = 120


class FacebookPoster(PlatformPoster):
    platform = "facebook"

    def is_configured(self) -> bool:
        return bool(self.vault_tokens.get("facebook_token")) and bool(
            self.vault_tokens.get("facebook_page_id")
        )

    async def post(self, project: dict, video_url: str, metadata: dict, tenant_id: str) -> dict:
        title = (metadata.get("selected_title") or project.get("name") or "")[:90]
        description = metadata.get("description") or ""
        duration = metadata.get("duration") or project.get("target_duration") or 60
        ok, reason = compliance_check("facebook", title, description, duration)
        if not ok:
            return {"platform": "facebook", "status": "blocked", "reason": reason}
        allowed, remaining = rate_limit_check("facebook", tenant_id)
        if not allowed:
            return {"platform": "facebook", "status": "rate_limited", "retry_after": remaining}
        if not self.is_configured():
            res = {
                "platform": "facebook",
                "status": "not_configured",
                "reason": "FACEBOOK_PAGE_ACCESS_TOKEN / FACEBOOK_PAGE_ID not set",
            }
            log_analytics("facebook", tenant_id, project.get("id"), res)
            return res
        if not video_url or not str(video_url).startswith("http"):
            raise RuntimeError("Facebook upload failed - video_url is not a public URL")

        token = self.vault_tokens["facebook_token"]
        page_id = self.vault_tokens["facebook_page_id"]
        caption = description or title

        try:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                # Meta ingests the bytes we send, not the source URL.
                download = await client.get(video_url)
                download.raise_for_status()
                video_bytes = download.content
                file_size = len(video_bytes)
                max_mb = PLATFORM_LIMITS["facebook"]["max_size_mb"]
                if file_size > max_mb * 1024 * 1024:
                    raise RuntimeError(
                        f"Facebook upload failed - video {file_size} bytes > {max_mb} MB limit"
                    )

                start = await client.post(
                    f"{GRAPH_BASE}/{page_id}/video_reels",
                    data={"upload_phase": "start", "access_token": token},
                    timeout=30.0,
                )
                start.raise_for_status()
                start_data = start.json()
                video_id = start_data.get("video_id")
                upload_url = start_data.get("upload_url")
                if not video_id or not upload_url:
                    raise RuntimeError("Facebook start returned no video_id/upload_url")

                upload = await client.post(
                    upload_url,
                    headers={
                        "Authorization": f"OAuth {token}",
                        "offset": "0",
                        "file_size": str(file_size),
                    },
                    content=video_bytes,
                    timeout=300.0,
                )
                upload.raise_for_status()

                waited = 0
                while True:
                    status_resp = await client.get(
                        f"{GRAPH_BASE}/{video_id}",
                        params={"fields": "status", "access_token": token},
                        timeout=30.0,
                    )
                    status_resp.raise_for_status()
                    status_data = status_resp.json()
                    video_status = (
                        status_data.get("status", {}).get("video_status")
                        or status_data.get("video_status")
                    )
                    if video_status in ("upload_complete", "ready"):
                        break
                    if video_status in ("error", "upload_failed", "expired"):
                        raise RuntimeError(f"Facebook upload failed - status {video_status}")
                    if waited >= _POLL_TIMEOUT:
                        raise RuntimeError("Facebook upload status timed out (120s)")
                    await asyncio.sleep(_POLL_INTERVAL)
                    waited += _POLL_INTERVAL

                finish = await client.post(
                    f"{GRAPH_BASE}/{page_id}/video_reels",
                    data={
                        "upload_phase": "finish",
                        "video_id": video_id,
                        "video_state": "PUBLISHED",
                        "description": caption,
                        "access_token": token,
                    },
                    timeout=30.0,
                )
                finish.raise_for_status()
                if not finish.json().get("success", True):
                    raise RuntimeError("Facebook finish returned success=false")

            url = f"https://www.facebook.com/reel/{video_id}"
            res = {
                "platform": "facebook",
                "status": "uploaded",
                "video_id": video_id,
                "url": url,
                "title": title,
            }
            log_analytics("facebook", tenant_id, project.get("id"), res)
            logger.info(f"FB reel uploaded tenant={tenant_id} video_id={video_id}")
            return res
        except httpx.HTTPError as e:
            logger.warning("FB real upload failed (%s)", type(e).__name__)
            raise RuntimeError("Facebook upload failed - provider rejected the upload") from None
