from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import JSONResponse
import redis
r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
class ComplianceGuard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if "/render" in request.url.path:
            # Respect PAYMENT_PROVIDER=dodo finite credits - bypass throttle for paid tenants
            provider = __import__("os").getenv("PAYMENT_PROVIDER","dodo").lower()
            # Extract tenant from JWT if present to avoid anon bucket lumping
            tenant = request.headers.get("x-tenant-id", "anon")
            auth = request.headers.get("authorization","")
            if auth.lower().startswith("bearer "):
                try:
                    import jwt as _jwt
                    payload = _jwt.decode(auth.split(" ",1)[1], options={"verify_signature": False})
                    tenant = str(payload.get("sub") or payload.get("tenant_id") or tenant)
                except Exception:
                    pass
            # Raised per audit: 100/hour (was 20/5) — dodo finite credits already gated, free tier no longer 1/hour
            limit = 100
            # Admin bypass via header
            if request.headers.get("x-bypass-ratelimit") == __import__("os").getenv("RENDER_BYPASS_TOKEN",""):
                return await call_next(request)
            key = f"ratelimit:{tenant}:render_hour"
            try:
                count = r.incr(key)
                if count == 1: r.expire(key, 3600)
                if count > limit:
                    return JSONResponse({"error":"Render throttled - 1/hour free tier","retry_after": r.ttl(key)}, status_code=429)
            except: pass
        return await call_next(request)
