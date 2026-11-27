"""Instagram Reels adapter - vault INSTAGRAM_ACCESS_TOKEN + INSTAGRAM_BUSINESS_ACCOUNT_ID, 90s max.

Real upload via the Instagram Graph API content-publishing flow
(Meta Graph API v26.0):
  1. create container - POST /{ig-user-id}/media
     media_type=REELS, video_url, caption, share_to_feed=true
     -> {id: <container-id>}
  2. poll - GET /{container-id}?fields=status_code until FINISHED
  3. publish - POST /{ig-user-id}/media_publish creation_id=<container-id>
     -> {id: <media-id>}, then a best-effort permalink lookup.

No mock IDs: unconfigured returns not_configured, provider failure raises
(BOOT.md HARD RULE 3 - same fail-loud posture as the YouTube adapter).
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .base import PlatformPoster, compliance_check, rate_limit_check, log_analytics

logger = logging.getLogger("autopost.instagram")

GRAPH_VERSION = "v26.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
_POLL_INTERVAL = 10     # seconds between container status checks
_POLL_TIMEOUT = 300     # seconds max waiting for the container (Meta guidance)


class InstagramPoster(PlatformPoster):
    platform = "instagram"

    def is_configured(self):
        return bool(self.vault_tokens.get("instagram_token")) and bool(
            self.vault_tokens.get("instagram_account_id")
        )

    async def post(self, project, video_url, metadata, tenant_id):
        title = (metadata.get("selected_title") or project.get("name") or "")[:90]
        caption = (metadata.get("description") or title)[:2200]
        duration = metadata.get("duration") or project.get("target_duration") or 60
        ok, reason = compliance_check("instagram", title, metadata.get("description", ""), duration)
        if not ok:
            # Auto-trim to 90s would be needed - for now block if >90
            return {"platform": "instagram", "status": "blocked", "reason": reason}
        allowed, remaining = rate_limit_check("instagram", tenant_id)
        if not allowed:
            return {"platform": "instagram", "status": "rate_limited", "retry_after": remaining}
        if not self.is_configured():
            res = {
                "platform": "instagram",
                "status": "not_configured",
                "reason": "INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_BUSINESS_ACCOUNT_ID not set",
            }
            log_analytics("instagram", tenant_id, project.get("id"), res)
            return res
        if not video_url or not str(video_url).startswith("http"):
            raise RuntimeError("Instagram upload failed - video_url is not a public URL")

        token = self.vault_tokens["instagram_token"]
        ig_id = self.vault_tokens["instagram_account_id"]
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                # 1. Create the media container
                create = await client.post(
                    f"{GRAPH_BASE}/{ig_id}/media",
                    data={
                        "media_type": "REELS",
                        "video_url": video_url,
                        "caption": caption,
                        "share_to_feed": "true",
                        "access_token": token,
                    },
                )
                create.raise_for_status()
                container_id = create.json().get("id")
                if not container_id:
                    raise RuntimeError("Instagram returned no container id")

                # 2. Poll until Meta finishes processing the container
                waited = 0
                while True:
                    status_resp = await client.get(
                        f"{GRAPH_BASE}/{container_id}",
                        params={"fields": "status_code", "access_token": token},
                    )
                    status_resp.raise_for_status()
                    status = status_resp.json().get("status_code")
                    if status == "FINISHED":
                        break
                    if status in ("ERROR", "EXPIRED"):
                        raise RuntimeError(f"Instagram container {status}")
                    if waited >= _POLL_TIMEOUT:
                        raise RuntimeError("Instagram container timed out (300s)")
                    await asyncio.sleep(_POLL_INTERVAL)
                    waited += _POLL_INTERVAL

                # 3. Publish the container
                publish = await client.post(
                    f"{GRAPH_BASE}/{ig_id}/media_publish",
                    data={"creation_id": container_id, "access_token": token},
                )
                publish.raise_for_status()
                media_id = publish.json().get("id")
                if not media_id:
                    raise RuntimeError("Instagram publish returned no media id")

                # Best-effort permalink lookup (numeric id is not a URL on its own)
                url = None
                try:
                    perm = await client.get(
                        f"{GRAPH_BASE}/{media_id}",
                        params={"fields": "permalink", "access_token": token},
                    )
                    perm.raise_for_status()
                    url = perm.json().get("permalink")
                except httpx.HTTPError:
                    logger.warning("IG permalink lookup failed, returning media id only")

            res = {
                "platform": "instagram",
                "status": "uploaded",
                "video_id": media_id,
                "url": url,
                "title": title,
            }
            log_analytics("instagram", tenant_id, project.get("id"), res)
            logger.info(f"IG reel uploaded tenant={tenant_id} media_id={media_id}")
            return res
        except httpx.HTTPError as e:
            logger.warning("IG real upload failed (%s)", type(e).__name__)
            raise RuntimeError("Instagram upload failed - provider rejected the upload") from None
