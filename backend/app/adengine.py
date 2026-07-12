"""
FacelessForge → AdEngine connector
Posts completed videos to social media via AdEngine
"""
import logging
from datetime import datetime, timezone
import requests

logger = logging.getLogger(__name__)

ADENGINE_WEBHOOK = "http://localhost:5678/webhook/video-ready"
ADENGINE_SHEET = "ethinx-adengine-videos"  # placeholder


async def send_to_adengine(video_url: str, caption: str, platforms: list, project_id: str):
    """
    Send completed video to AdEngine for distribution.
    """
    payload = {
        "video_url": video_url,
        "caption": caption,
        "platforms": platforms,
        "project_id": project_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "facelessforge",
    }

    logger.info("[ADENGINE] Sending video to social: %s", video_url)
    logger.info("[ADENGINE] Platforms: %s", platforms)
    logger.info("[ADENGINE] Caption: %s...", caption[:100])

    try:
        resp = requests.post(ADENGINE_WEBHOOK, json=payload, timeout=30)
        resp.raise_for_status()
        logger.info("[ADENGINE] Webhook response: %s", resp.status_code)
        return {"status": "queued", "payload": payload, "webhook_status": resp.status_code}
    except Exception as e:
        logger.error("[ADENGINE] Webhook failed: %s", e)
        return {"status": "failed", "payload": payload, "error": str(e)}
