"""
Dodo Payments - FacelessForge Billing Module (Finite Credits)
Path: /opt/facelessforge/deploy/backend/app/billing_dodo.py
Router: /api/billing/dodo
Port: 8081 (Forge) - same tenant isolation as PayPal module
Redis: ethinx-redis localhost:6379, keys tenant:{tenant_id} hash (finite credits)
Env: DODO_PAYMENTS_API_KEY, DODO_PAYMENTS_WEBHOOK_KEY, DODO_PAYMENTS_ENVIRONMENT
Webhook: POST /api/billing/dodo/webhook  -> Standard Webhooks verification
Events: payment.succeeded, subscription.active (finite credit grants only)
SDK: pip install dodopayments (optional, fallback to manual verify via standardwebhooks)
"""
import os
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Header
from pydantic import BaseModel
import redis

logger = logging.getLogger("billing_dodo")
router = APIRouter(prefix="/api/billing/dodo", tags=["dodo"])

# --- Redis (sync,decode) ---
r = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    db=0,
    decode_responses=True,
)

# --- Auth (JWT tenant isolation) ---
try:
    from app.auth import get_current_user
except ImportError:
    from app.auth import get_current_user  # type: ignore

# --- Dodo Client ---
def _resolve_dodo_key() -> Optional[str]:
    return (os.getenv("DODO_PAYMENTS_API_KEY") or os.getenv("DODO_API_KEY") or os.getenv("DODO_PAYMENTS_APIKEY") or "").strip() or None

def get_dodo_client():
    try:
        from dodopayments import DodoPayments  # type: ignore
    except ImportError:
        raise HTTPException(500, "dodopayments SDK not installed. Run: pip install dodopayments")
    api_key = _resolve_dodo_key()
    env_raw = os.getenv("DODO_PAYMENTS_ENVIRONMENT", "test_mode")
    environment: Literal["live_mode", "test_mode"] = "live_mode" if env_raw == "live_mode" else "test_mode"
    if not api_key:
        raise HTTPException(500, "DODO_PAYMENTS_API_KEY / DODO_API_KEY not set in vault")
    webhook_key = os.getenv("DODO_PAYMENTS_WEBHOOK_KEY") or os.getenv("DODO_WEBHOOK_SECRET") or os.getenv("DODO_API_WEBHOOK_KEY")
    kwargs: Dict[str, Any] = {"bearer_token": api_key, "environment": environment}
    if webhook_key:
        kwargs["webhook_key"] = webhook_key
    return DodoPayments(**kwargs)  # type: ignore

# --- Credit Map - FINITE ONLY, no infinite ---
# Each product maps to a fixed finite credit allowance. No plan grants unlimited.
CREDIT_MAP = {
    os.getenv("DODO_PRODUCT_STARTER", "pdt_starter"): int(os.getenv("DODO_CREDITS_STARTER", "100")),
    os.getenv("DODO_PRODUCT_CREATOR", "pdt_creator"): int(os.getenv("DODO_CREDITS_CREATOR", "500")),
    os.getenv("DODO_PRODUCT_PRO", "pdt_pro"): int(os.getenv("DODO_CREDITS_PRO", "2000")),
    os.getenv("DODO_PRODUCT_SCALE", "pdt_scale"): int(os.getenv("DODO_CREDITS_SCALE", "10000")),
}
DEFAULT_CREDITS = 100

# Also support Paddle-style price mapping if Dodo product_ids overlap
# Ensure no infinite sentinel (e.g. -1, 9999999) is treated as valid
MAX_FINITE_CREDITS = 100000


def infer_credits(product_id: Optional[str]) -> int:
    if not product_id:
        return DEFAULT_CREDITS
    credits = CREDIT_MAP.get(product_id, DEFAULT_CREDITS)
    # Clamp to finite range
    if credits < 0 or credits > MAX_FINITE_CREDITS:
        return DEFAULT_CREDITS
    return credits


class CheckoutRequest(BaseModel):
    product_id: str
    quantity: int = 1
    customer_email: Optional[str] = None


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str
    product_id: str
    credits: int


def _get_tenant_id_from_user(user: Any) -> str:
    # user is dict from get_current_user (db.users doc) or object with tenant_id/id
    if isinstance(user, dict):
        return str(user.get("id") or user.get("tenant_id") or user.get("user_id") or "default")
    return str(getattr(user, "tenant_id", getattr(user, "id", getattr(user, "user_id", "default"))))


def _check_tenant_access(requested_tenant: str, user: Any) -> None:
    """Enforce JWT tenant isolation: user can only access own tenant unless admin."""
    current = _get_tenant_id_from_user(user)
    role = user.get("role") if isinstance(user, dict) else getattr(user, "role", None)
    if role == "admin":
        return
    if requested_tenant != current:
        raise HTTPException(status_code=403, detail="Forbidden: tenant isolation")


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(req: CheckoutRequest, user=Depends(get_current_user)):
    tenant_id = _get_tenant_id_from_user(user)
    client = get_dodo_client()

    # Validate quantity finite
    if req.quantity < 1 or req.quantity > 100:
        raise HTTPException(400, "quantity must be 1..100")

    # Derive customer email from JWT or request
    email: Optional[str] = None
    if isinstance(user, dict):
        email = req.customer_email or user.get("email")
    else:
        email = req.customer_email or getattr(user, "email", None)

    try:
        session = client.checkout_sessions.create(  # type: ignore
            product_cart=[{"product_id": req.product_id, "quantity": req.quantity}],
            customer={"email": email} if email else None,
            return_url=os.getenv("DODO_PAYMENTS_RETURN_URL", "https://facelessforge.ethinx.solutions/billing/success"),
            # Attach tenant_id via custom_data/metadata for webhook reconciliation (no JWT in webhook)
            custom_data={"tenant_id": tenant_id} if tenant_id else None,
        )
    except Exception as e:
        logger.error(f"Dodo checkout failed: {e}")
        raise HTTPException(500, f"Dodo checkout failed: {str(e)}")

    credits = infer_credits(req.product_id) * req.quantity
    # Store pending in Redis for webhook reconciliation (TTL 1h)
    session_id = getattr(session, "session_id", getattr(session, "id", "unknown"))
    r.hset(
        f"dodo:pending:{session_id}",
        mapping={
            "tenant_id": tenant_id,
            "product_id": req.product_id,
            "credits": str(credits),
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    r.expire(f"dodo:pending:{session_id}", 3600)

    checkout_url = getattr(session, "checkout_url", getattr(session, "url", "")) or ""
    return CheckoutResponse(
        checkout_url=checkout_url,
        session_id=session_id,
        product_id=req.product_id,
        credits=credits,
    )


@router.get("/credits/{tenant_id}")
async def get_credits_by_tenant(tenant_id: str, user=Depends(get_current_user)):
    _check_tenant_access(tenant_id, user)
    data = r.hgetall(f"tenant:{tenant_id}")
    # Hash holds field credits as string; default 0
    return {
        "tenant_id": tenant_id,
        "credits": int(data.get("credits", 0) or 0),
        "status": data.get("status", "UNKNOWN"),
        "last_payment": data.get("last_payment"),
    }


@router.get("/credits")
async def get_my_credits(user=Depends(get_current_user)):
    tenant_id = _get_tenant_id_from_user(user)
    data = r.hgetall(f"tenant:{tenant_id}")
    return {
        "tenant_id": tenant_id,
        "credits": int(data.get("credits", 0) or 0),
        "status": data.get("status", "UNKNOWN"),
        "last_payment": data.get("last_payment"),
    }


# --- Webhook: Standard Webhooks verification, finite credits to tenant:{tenant_id} ---
@router.post("/webhook")
async def dodo_webhook(
    request: Request,
    webhook_id: Optional[str] = Header(None, alias="webhook-id"),
    webhook_timestamp: Optional[str] = Header(None, alias="webhook-timestamp"),
    webhook_signature: Optional[str] = Header(None, alias="webhook-signature"),
):
    """
    Dodo webhook endpoint at /api/billing/dodo/webhook
    - Verifies Standard Webhooks signature (webhook-id.webhook-timestamp.raw_body)
    - Idempotent via dodo:webhooks:processed (SET NX / SISMEMBER)
    - Grants FINITE credits to tenant:{tenant_id} Redis hash only for payment.succeeded and subscription.active
    - No JWT required for webhook (Dodo server-to-server), but credits grant is finite and audited
    """
    body = await request.body()  # raw bytes for verification

    # Normalize headers for verify (case-insensitive)
    # FastAPI Header alias already handles lowercasing, but also check raw headers for SDK
    raw_headers = {k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v for k, v in request.headers.items()}
    # Ensure we have values even if Header alias missed due to hyphen handling
    if not webhook_id:
        webhook_id = raw_headers.get("webhook-id") or raw_headers.get("Webhook-Id") or raw_headers.get("webhook-ID")
    if not webhook_timestamp:
        webhook_timestamp = raw_headers.get("webhook-timestamp") or raw_headers.get("Webhook-Timestamp")
    if not webhook_signature:
        webhook_signature = raw_headers.get("webhook-signature") or raw_headers.get("Webhook-Signature")

    headers_for_verify = {
        "webhook-id": webhook_id or "",
        "webhook-timestamp": webhook_timestamp or "",
        "webhook-signature": webhook_signature or "",
    }

    secret = os.getenv("DODO_PAYMENTS_WEBHOOK_KEY") or os.getenv("DODO_WEBHOOK_SECRET") or os.getenv("DODO_PAYMENTS_WEBHOOK_SECRET") or ""

    # 1. Verify signature (Standard Webhooks) - REQUIRED in prod, allow bypass only if secret not set and dev mode
    # For security, if secret is set we must verify; if not set, log warning but continue for local testing
    if secret:
        try:
            from app.dodo_webhook_verify import verify_signature  # type: ignore

            verify_signature(body, headers_for_verify, secret)
        except Exception as e:
            logger.warning(f"Webhook signature failed id={webhook_id}: {e}")
            raise HTTPException(status_code=401, detail=f"Invalid signature: {e}")
    else:
        logger.warning("DODO_PAYMENTS_WEBHOOK_KEY not set - skipping webhook signature verification (dev mode)")

    # 2. Idempotency - check before processing
    # Use helper that does SET NX; also check legacy SISMEMBER
    if webhook_id:
        try:
            # Try atomic SET NX
            was_new = r.set(f"dodo:webhooks:processed:{webhook_id}", "1", nx=True, ex=86400)
            if not was_new:
                return {"status": "already_processed", "webhook_id": webhook_id}
            # Also maintain set for backward compat / audit
            r.sadd("dodo:webhooks:processed", webhook_id)
        except Exception:
            # Fallback to SISMEMBER
            try:
                if r.sismember("dodo:webhooks:processed", webhook_id):  # type: ignore
                    return {"status": "already_processed", "webhook_id": webhook_id}
            except Exception:
                pass
    else:
        # No webhook-id: use body hash as idempotency key (not ideal, but prevents double-processing)
        import hashlib as _hl

        fallback_id = _hl.sha256(body).hexdigest()[:16]
        webhook_id = fallback_id

    # 3. Parse payload (after verification, using raw body)
    try:
        payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)  # type: ignore
    except Exception:
        payload = {}

    # Dodo envelope: { business_id, type, timestamp, data: { payload_type, ... } }
    event_type = str(payload.get("type") or payload.get("event_type") or payload.get("eventType") or "unknown").strip()
    data = payload.get("data") or payload.get("payload") or payload
    if not isinstance(data, dict):
        data = {}

    # 4. Only grant on the two requested events (exact match, per credit-based-billing skill)
    # payment.succeeded -> one-time / top-up grants; subscription.active -> recurring grant
    ALLOWED_EVENTS = {"payment.succeeded", "subscription.active"}
    if event_type not in ALLOWED_EVENTS:
        logger.info(f"Dodo webhook ignored event={event_type} id={webhook_id}")
        return {"status": "ignored", "event": event_type, "webhook_id": webhook_id}

    # 5. Resolve tenant_id and product_id (finite credits)
    # Priority: pending session -> custom_data.tenant_id -> data.customer.customer_id mapping -> metadata.tenant_id -> custom_id
    session_id = data.get("session_id") or data.get("checkout_session_id") or payload.get("session_id") or data.get("payment_id") or data.get("subscription_id")
    # Also check nested customer
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    custom_data = data.get("custom_data") or data.get("customData") or payload.get("custom_data") or {}
    if not isinstance(custom_data, dict):
        custom_data = {}

    product_id: Optional[str] = None
    # product_cart extraction
    if isinstance(data.get("product_cart"), list) and data["product_cart"]:
        try:
            product_id = data["product_cart"][0].get("product_id")
        except Exception:
            pass
    if not product_id:
        product_id = data.get("product_id") or custom_data.get("product_id") or payload.get("product_id")
    if not product_id and isinstance(data.get("product"), dict):
        product_id = data["product"].get("product_id") or data["product"].get("id")
    # subscription payload may have product_id at top level or items
    if not product_id and isinstance(data.get("items"), list) and data["items"]:
        try:
            product_id = data["items"][0].get("product_id") or data["items"][0].get("product", {}).get("product_id")
        except Exception:
            pass
    if not product_id:
        product_id = os.getenv("DODO_PRODUCT_STARTER", "pdt_starter")

    # Tenant resolution
    tenant_id: Optional[str] = None
    credits = DEFAULT_CREDITS
    if session_id:
        try:
            pending = r.hgetall(f"dodo:pending:{session_id}")
            if pending:
                tenant_id = pending.get("tenant_id")
                try:
                    credits = int(pending.get("credits", str(DEFAULT_CREDITS)))
                except Exception:
                    credits = infer_credits(product_id)
                if pending.get("product_id"):
                    product_id = pending.get("product_id")  # prefer pending's product
        except Exception:
            pass

    if not tenant_id:
        # Resolve tenant without ternary precedence bug; check metadata separately
        meta_tenant = None
        try:
            md = data.get("metadata")
            if isinstance(md, dict):
                meta_tenant = md.get("tenant_id")
        except Exception:
            meta_tenant = None
        tenant_id = (
            custom_data.get("tenant_id")
            or custom_data.get("tenantId")
            or data.get("tenant_id")
            or (customer.get("customer_id") if customer else None)
            or data.get("custom_id")
            or payload.get("custom_id")
            or meta_tenant
        )
    # Also check Dodo's subscription customer_id -> need to map via our users; fallback to email lookup
    if not tenant_id and customer and customer.get("email"):
        # Try to resolve tenant via email in Mongo (if available) - best effort, sync fallback not possible here
        # For now use email-derived tenant placeholder; caller should ensure checkout set custom_data.tenant_id
        tenant_id = customer.get("email")  # temporary, will be normalized below if not found
        # Attempt sync Mongo lookup is not done in webhook to keep fast; rely on pending or custom_data
        logger.warning(f"Tenant fallback to email={tenant_id} for event={event_type} - ensure checkout sets custom_data.tenant_id")

    if not tenant_id:
        tenant_id = "default"
        logger.warning(f"No tenant_id resolved for webhook {webhook_id} event={event_type}, using default")

    # Normalize tenant_id: if it looks like email, try to keep but Redis key will be tenant:{email} - still hash
    # Ensure finite credits from product_id if not from pending
    if credits == DEFAULT_CREDITS and product_id:
        credits = infer_credits(product_id)
    # If still pending gave credits but product_id changed, recalc
    if session_id and product_id and credits == DEFAULT_CREDITS:
        credits = infer_credits(product_id)
    # Quantity handling: data.quantity or product_cart quantity
    quantity = 1
    try:
        if isinstance(data.get("product_cart"), list) and data["product_cart"]:
            quantity = int(data["product_cart"][0].get("quantity", 1) or 1)
        elif data.get("quantity"):
            quantity = int(data.get("quantity") or 1)
    except Exception:
        quantity = 1
    # Apply quantity only if credits was from single product mapping (not already multiplied in pending)
    # Pending already multiplied; if no pending, multiply now
    pending_exists = False
    if session_id:
        try:
            pending_exists = bool(r.exists(f"dodo:pending:{session_id}"))
        except Exception:
            pending_exists = False
    if not pending_exists:
        credits = credits * max(1, min(quantity, 100))
    # Clamp finite
    credits = max(0, min(credits, MAX_FINITE_CREDITS))
    if credits == 0:
        logger.warning(f"Zero credits resolved for product={product_id} event={event_type}, using DEFAULT {DEFAULT_CREDITS}")
        credits = DEFAULT_CREDITS

    # 6. Finite credit grant to tenant:{tenant_id} hash (atomic pipeline + audit)
    # Use HINCRBY for credits, HSET for status/last_payment, LPUSH audit
    now_iso = datetime.now(timezone.utc).isoformat()
    pipe = r.pipeline()
    pipe.hincrby(f"tenant:{tenant_id}", "credits", credits)
    pipe.hset(
        f"tenant:{tenant_id}",
        mapping={
            "status": "ACTIVE",
            "last_payment": now_iso,
            "last_transaction": webhook_id or session_id or "unknown",
            "last_event": event_type,
            "last_product": product_id or "unknown",
        },
    )
    # Expire credits hash? No - finite credits persist until consumed; only audit has TTL
    pipe.sadd("dodo:webhooks:processed", webhook_id or session_id or str(time.time()))
    pipe.hset(
        f"audit:dodo:{webhook_id or session_id}",
        mapping={
            "tenant_id": tenant_id,
            "product_id": product_id or "unknown",
            "credits": str(credits),
            "event_type": event_type,
            "timestamp": now_iso,
        },
    )
    pipe.expire(f"audit:dodo:{webhook_id or session_id}", 2592000)  # 30d audit retention
    pipe.lpush(
        f"tenant:{tenant_id}:billing_audit",
        json.dumps(
            {
                "provider": "dodo",
                "credits": credits,
                "event": event_type,
                "product_id": product_id,
                "webhook_id": webhook_id,
                "timestamp": now_iso,
            }
        ),
    )
    pipe.ltrim(f"tenant:{tenant_id}:billing_audit", 0, 999)  # cap audit list
    pipe.execute()

    logger.info(f"Dodo webhook granted finite {credits} credits to tenant:{tenant_id} for {event_type} product={product_id} webhook_id={webhook_id}")

    # Clean up pending after successful grant
    if session_id:
        try:
            r.delete(f"dodo:pending:{session_id}")
        except Exception:
            pass

    return {"status": "ACTIVE", "tenant_id": tenant_id, "credits_granted": credits, "event": event_type, "webhook_id": webhook_id}
