"""Auto-post base - platform abstraction with rate limit + compliance.
Vault only, no secrets in repo. All tokens from /opt/ethinx/deployment/secrets-vault/facelessforge-deploy.env via os.environ.
"""
from __future__ import annotations
import os
import re
import logging
import redis
from datetime import datetime, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger("autopost.base")

r = redis.Redis(host=os.getenv("REDIS_HOST","localhost"), port=int(os.getenv("REDIS_PORT","6379")), db=0, decode_responses=True)

# Compliance blocklist - shared across platforms
BANNED_PHRASES = [
    "guaranteed income", "get rich quick", "miracle cure",
    "hate speech placeholder", "csrf", "xss"
]
# Per-platform limits (seconds, bytes)
PLATFORM_LIMITS = {
    "youtube": {"max_duration": 43200, "max_size_mb": 2048, "daily_cap": 6},
    "tiktok": {"max_duration": 600, "max_size_mb": 500, "daily_cap": 10},
    "instagram": {"max_duration": 90, "max_size_mb": 650, "daily_cap": 6},
    "facebook": {"max_duration": 90, "max_size_mb": 650, "daily_cap": 6},
    "x": {"max_duration": 140, "max_size_mb": 512, "daily_cap": 10},
    "linkedin": {"max_duration": 600, "max_size_mb": 500, "daily_cap": 5},
    "reddit": {"max_duration": 600, "max_size_mb": 500, "daily_cap": 5},
    "product_hunt": {"max_duration": 600, "max_size_mb": 500, "daily_cap": 1},
}

GRAPH_VERSION = "v26.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

def compliance_check(platform: str, title: str, description: str, duration: Optional[float] = None) -> tuple[bool, str]:
    text = f"{title} {description}".lower()
    for phrase in BANNED_PHRASES:
        if phrase.lower() in text:
            return False, f"blocked phrase: {phrase}"
    limits = PLATFORM_LIMITS.get(platform, {})
    max_dur = limits.get("max_duration")
    if max_dur and duration and duration > max_dur:
        return False, f"duration {duration}s > {max_dur}s for {platform}"
    # Basic profanity / spam check placeholder
    if len(title) < 5:
        return False, "title too short"
    return True, "ok"

def rate_limit_check(platform: str, tenant_id: str, daily_cap: Optional[int] = None) -> tuple[bool, int]:
    cap = daily_cap or PLATFORM_LIMITS.get(platform, {}).get("daily_cap", 5)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"autopost:{platform}:{tenant_id}:{day}"
    try:
        count = r.incr(key)
        if count == 1:
            r.expire(key, 86400)
        if count > cap:
            ttl = r.ttl(key)
            return False, ttl
        return True, cap - count
    except Exception as e:
        logger.warning(f"rate_limit redis fail {e}")
        return True, cap

def log_analytics(platform: str, tenant_id: str, project_id: str, result: Dict[str, Any]):
    try:
        r.lpush(f"analytics:autopost:{platform}", __import__("json").dumps({"tenant": tenant_id, "project": project_id, "result": result, "ts": datetime.now(timezone.utc).isoformat()}))
        r.ltrim(f"analytics:autopost:{platform}", 0, 999)
        r.incr(f"analytics:autopost:{platform}:count")
    except Exception:
        pass

class PlatformPoster:
    platform: str = "base"
    def __init__(self):
        self.vault_tokens = self._load_tokens()
    def _load_tokens(self) -> Dict[str, str]:
        # All tokens via env (vault loaded via systemd EnvironmentFile)
        return {
            "youtube_refresh": os.getenv("YOUTUBE_REFRESH_TOKEN",""),
            "youtube_client_id": os.getenv("YOUTUBE_CLIENT_ID",""),
            "tiktok_access": os.getenv("TIKTOK_ACCESS_TOKEN",""),
            "instagram_token": os.getenv("INSTAGRAM_ACCESS_TOKEN",""),
            "instagram_account_id": os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID",""),
            "facebook_token": os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN",""),
            "facebook_page_id": os.getenv("FACEBOOK_PAGE_ID",""),
            "x_bearer": os.getenv("X_BEARER_TOKEN","") or os.getenv("TWITTER_BEARER_TOKEN",""),
            "reddit_client": os.getenv("REDDIT_CLIENT_ID",""),
        }
    async def post(self, project: dict, video_url: str, metadata: dict, tenant_id: str) -> Dict[str, Any]:
        raise NotImplementedError
    def is_configured(self) -> bool:
        return False
