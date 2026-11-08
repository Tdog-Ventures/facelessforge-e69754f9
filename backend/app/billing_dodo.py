"""
Dodo Payments - FacelessForge Billing Module (Finite Credits)
Path: /opt/facelessforge/deploy/backend/app/billing_dodo.py
Router: /api/billing/dodo
Port: 8081 (Forge) - same tenant isolation as PayPal module
Redis: ethinx-redis localhost:6379, keys tenant:{tenant_id} hash (finite credits)
Env: DODO_PAYMENTS_API_KEY, DODO_PAYMENTS_WEBHOOK_KEY, DODO_PAYMENTS_ENVIRONMENT
Webhook: POST /api/billing/dodo/webhook  -> Standard Webhooks verification
Events: payment.succeeded (finite credit grants only); subscription.active is not a charge event
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


class DodoGrantRetryable(Exception):
    """A transient store failure during Dodo fulfillment that must be retried.

    Raised after Redis/Mongo write or connection errors so the webhook
    acknowledges with 503 and Dodo redelivers; no credits are lost because
    both the Redis grant and the Mongo mirror are idempotent per payment_id.
    """


# Atomic Redis grant: record the payment_id in a per-tenant fulfillment hash,
# then increment the tenant credit balance. HSETNX guarantees exactly-once
# Redis crediting even for concurrent deliveries or different webhook IDs.
_REDIS_GRANT_LUA = """
local fulfillment_key = KEYS[1]
local payment_id = ARGV[1]
local credits = ARGV[2]
local account_key = KEYS[2]
local now_iso = ARGV[3]

if redis.call('HSETNX', fulfillment_key, payment_id, credits) == 0 then
    return 0
end
-- Lua execution is isolated, but Redis does not roll back earlier commands
-- on a runtime error. A failed increment must not consume the payment marker.
local increment = redis.pcall('HINCRBY', account_key, 'credits', credits)
if type(increment) == 'table' and increment.err then
    redis.call('HDEL', fulfillment_key, payment_id)
    return redis.error_reply(increment.err)
end
redis.call('HSET', account_key, 'status', 'ACTIVE')
redis.call('HSET', account_key, 'last_payment', now_iso)
return 1
"""

# --- Redis (sync,decode) ---
r = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    db=0,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
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


def scale_credits_for_checkout(*, pending: Optional[dict], quantity: int, unit_credits: int) -> int:
    """Apply checkout quantity once.

    A pending hash already stores unit credits times quantity. An empty hash
    means the key was read and was absent. Callers must not pass an empty hash
    when the Redis read failed; that failure has to be retried instead.
    """
    qty = max(1, min(int(quantity or 1), 100))
    if pending:
        try:
            stored = int(pending.get("credits"))
        except (TypeError, ValueError):
            stored = int(unit_credits) * qty
        return max(0, min(stored, MAX_FINITE_CREDITS))
    return max(0, min(int(unit_credits) * qty, MAX_FINITE_CREDITS))


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
    # Store pending in Redis for webhook reconciliation (TTL 1h).
    # The Dodo session already exists. A Redis timeout must not hide that URL:
    # the webhook grants the stored total when the write landed, and
    # unit credits times quantity when the key is absent.
    session_id = getattr(session, "session_id", getattr(session, "id", "unknown"))
    checkout_url = getattr(session, "checkout_url", getattr(session, "url", "")) or ""
    try:
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
    except Exception:
        logger.exception("Dodo pending record failed session=%s; returning the created checkout", session_id)
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
    - Idempotent by payment_id via durable Mongo fulfillment record + atomic Redis HSETNX
    - Grants FINITE credits to tenant:{tenant_id} Redis hash only for payment.succeeded
    - Acknowledges the webhook only after fulfillment succeeds so Dodo retries on failure
    - No JWT required for webhook (Dodo server-to-server), but credits grant is finite and audited
    """
    secret = os.getenv("DODO_PAYMENTS_WEBHOOK_KEY") or os.getenv("DODO_WEBHOOK_SECRET") or os.getenv("DODO_PAYMENTS_WEBHOOK_SECRET") or ""
    if not secret:
        raise HTTPException(status_code=503, detail="DODO webhook secret not configured")
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

    # 1. Verify signature (Standard Webhooks) - REQUIRED
    try:
        from app.dodo_webhook_verify import verify_signature  # type: ignore

        verify_signature(body, headers_for_verify, secret)
    except Exception as e:
        logger.warning(f"Webhook signature failed id={webhook_id}: {e}")
        raise HTTPException(status_code=401, detail=f"Invalid signature: {e}")

    # Persist reviews before the Redis grant guard: a crash must not acknowledge
    # a refund for which no durable review exists. Mongo _id provides dedupe.
    try:
        review_payload = json.loads(body)
    except (ValueError, TypeError):
        review_payload = {}
    review_type = str(review_payload.get("type") or "") if isinstance(review_payload, dict) else ""
    from .dodo_reversals import REVERSAL_EVENTS, REVIEW_EVENTS, handle_reversal
    if review_type in REVERSAL_EVENTS | REVIEW_EVENTS:
        if not webhook_id:
            raise HTTPException(400, "Webhook identity required")
        try:
            from .db import get_db
            review_data = review_payload.get("data") or {}
            if not isinstance(review_data, dict):
                review_data = {}
            return await handle_reversal(get_db(), r, review_type, review_data, webhook_id)
        except Exception:
            logger.exception("Dodo review persistence failed id=%s", webhook_id)
            raise HTTPException(503, "Billing review pending; retry required")

    # 2. Parse payload (after verification, using raw body)
    try:
        payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)  # type: ignore
    except Exception:
        payload = {}

    # Dodo envelope: { business_id, type, timestamp, data: { payload_type, ... } }
    event_type = str(payload.get("type") or payload.get("event_type") or payload.get("eventType") or "unknown").strip()
    data = payload.get("data") or payload.get("payload") or payload
    if not isinstance(data, dict):
        data = {}

    # 3. Only grant on payment.succeeded. subscription.active fires for trial
    # activations and, for paid subscriptions, is followed by payment.succeeded.
    # Using payment_id as the durable grant identity prevents double crediting.
    ALLOWED_EVENTS = {"payment.succeeded"}
    if event_type not in ALLOWED_EVENTS:
        logger.info(f"Dodo webhook ignored event={event_type} id={webhook_id}")
        return {"status": "ignored", "event": event_type, "webhook_id": webhook_id}

    payment_id = data.get("payment_id") if isinstance(data.get("payment_id"), str) else None
    if not payment_id:
        logger.info(f"Dodo {event_type} without payment_id ignored id={webhook_id}")
        return {"status": "ignored_no_payment_id", "event": event_type, "webhook_id": webhook_id}

    if not webhook_id:
        # No webhook-id: derive a stable placeholder from the payment identity.
        import hashlib as _hl
        webhook_id = _hl.sha256(payment_id.encode()).hexdigest()[:16]

    # 4. Resolve tenant_id and product_id (finite credits)
    # Priority: pending session -> custom_data.tenant_id -> data.customer.customer_id mapping -> metadata.tenant_id -> custom_id
    session_id = data.get("session_id") or data.get("checkout_session_id") or payload.get("session_id") or data.get("subscription_id")
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

    # Tenant resolution. One Redis read decides the pending record.
    # A timeout is not an absent key: treating it as absent used to multiply
    # quantity onto credits that checkout had already scaled.
    tenant_id: Optional[str] = None
    credits = DEFAULT_CREDITS
    pending: dict = {}
    if session_id:
        try:
            pending = r.hgetall(f"dodo:pending:{session_id}") or {}
        except Exception as exc:
            logger.exception("Dodo pending session unreadable session=%s", session_id)
            raise HTTPException(status_code=503, detail="Pending credit session unreadable; retry required") from exc
        if pending:
            tenant_id = pending.get("tenant_id")
            try:
                credits = int(pending.get("credits", str(DEFAULT_CREDITS)))
            except Exception:
                credits = infer_credits(product_id)
            if pending.get("product_id"):
                product_id = pending.get("product_id")  # prefer pending's product

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
        raise HTTPException(status_code=400, detail="tenant_id not resolved, grant nothing")

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
    # Pending credits were already multiplied by quantity at checkout.
    credits = scale_credits_for_checkout(
        pending=pending,
        quantity=quantity,
        unit_credits=credits,
    )
    # Clamp finite
    credits = max(0, min(credits, MAX_FINITE_CREDITS))
    if credits == 0:
        logger.warning(f"Zero credits resolved for product={product_id} event={event_type}, using DEFAULT {DEFAULT_CREDITS}")
        credits = DEFAULT_CREDITS

    # 5b. Durable, retry-safe fulfillment. Do not acknowledge the webhook
    # until the grant is recorded; Dodo retries on non-2xx/timeout.
    try:
        result = await _fulfill_dodo_grant(
            tenant_id=tenant_id,
            product_id=product_id,
            credits=credits,
            payment_id=payment_id,
            event_type=event_type,
            customer_email=(customer or {}).get("email"),
            webhook_id=webhook_id,
        )
    except DodoGrantRetryable as exc:
        # A store leg failed before acknowledgement. The fulfillment record
        # carries the recoverable state; Dodo redelivers on 503.
        logger.exception(
            "Dodo fulfillment retryable failure id=%s payment_id=%s tenant_id=%s; not acknowledged",
            webhook_id, payment_id, tenant_id,
        )
        raise HTTPException(status_code=503, detail="Dodo fulfillment pending; retry required") from exc
    except Exception:
        logger.exception("Dodo fulfillment failed id=%s payment_id=%s; not acknowledged", webhook_id, payment_id)
        raise

    # Clean up pending after successful grant
    if session_id:
        try:
            r.delete(f"dodo:pending:{session_id}")
        except Exception:
            pass

    # Record the webhook ID only after successful fulfillment. Dodo retries
    # are bounded to ~22.5 hours; keep the marker for 7 days as a guardrail.
    try:
        r.set(f"dodo:webhooks:processed:{webhook_id}", "1", ex=604800)
        r.sadd("dodo:webhooks:processed", webhook_id)
    except Exception:
        pass

    logger.info(f"Dodo webhook fulfilled {credits} credits to tenant:{tenant_id} for payment_id={payment_id} webhook_id={webhook_id}")
    return result


def _dodo_plan_label(product_id: Optional[str]) -> str:
    """Map a Dodo product id to an FF plan label (env-driven, test-safe)."""
    pid = (product_id or "").strip()
    table = [
        ("scale", os.getenv("DODO_PRODUCT_SCALE", "pdt_scale")),
        ("pro", os.getenv("DODO_PRODUCT_PRO", "pdt_pro")),
        ("creator", os.getenv("DODO_PRODUCT_CREATOR", "pdt_creator")),
        ("starter", os.getenv("DODO_PRODUCT_STARTER", "pdt_starter")),
    ]
    for label, configured in table:
        if pid and pid == (configured or "").strip():
            return label
    return "creator"


def _apply_redis_grant(redis_client, tenant_id: str, payment_id: str, credits: int) -> bool:
    """Atomically credit a tenant once per payment_id. Returns True on first grant."""
    now_iso = datetime.now(timezone.utc).isoformat()
    result = redis_client.eval(
        _REDIS_GRANT_LUA,
        2,
        f"tenant:{tenant_id}:dodo_fulfillments",
        f"tenant:{tenant_id}",
        payment_id,
        str(credits),
        now_iso,
    )
    return bool(result)


async def _fulfill_dodo_grant(*, tenant_id: str, product_id: Optional[str],
                              credits: int, payment_id: str, event_type: str,
                              customer_email: Optional[str],
                              webhook_id: Optional[str]) -> dict:
    """Durable fulfillment: Redis + Mongo mirror, recoverable after crashes.

    A Mongo record keyed by payment_id drives recovery. It is created in the
    pending state, then Redis and Mongo are updated idempotently, then the
    record is marked completed. After the Redis grant succeeds the record is
    flipped to "redis_granted" before the Mongo mirror is attempted, so a
    retry can skip the Redis step entirely (never re-granting Redis credits).
    Transient Redis/Mongo failures are surfaced as DodoGrantRetryable so the
    webhook acknowledges with 503 and Dodo redelivers (bounded retry).
    Retries with the same payment_id (whether from Dodo replays or different
    webhook IDs) find the record and do not re-grant.
    """
    from pymongo.errors import PyMongoError

    from .db import get_db
    db = get_db()
    now = datetime.now(timezone.utc)

    def _retryable(action: str, exc: BaseException) -> DodoGrantRetryable:
        logger.error(
            "Dodo grant %s failed payment_id=%s tenant_id=%s: %s - webhook must be retried",
            action, payment_id, tenant_id, exc,
        )
        return DodoGrantRetryable(f"dodo grant {action} failed for payment_id={payment_id}: {exc}")

    # 1. Ensure a durable fulfillment record exists. Upsert-on-insert lets
    # concurrent deliveries race safely; only one creates the pending record.
    try:
        await db.ff_dodo_fulfillments.update_one(
            {"_id": payment_id},
            {"$setOnInsert": {
                "payment_id": payment_id,
                "tenant_id": tenant_id,
                "product_id": product_id,
                "credits": credits,
                "event_type": event_type,
                "status": "pending",
                "customer_email": customer_email,
                "attempts": 0,
                "webhook_id": webhook_id,
                "created_at": now,
                "updated_at": now,
            }},
            upsert=True,
        )
    except PyMongoError as exc:
        raise _retryable("fulfillment-record-create", exc) from exc
    try:
        fulfillment = await db.ff_dodo_fulfillments.find_one({"_id": payment_id}, {"_id": 0})
    except PyMongoError as exc:
        raise _retryable("fulfillment-record-read", exc) from exc
    if not fulfillment:
        raise DodoGrantRetryable(
            f"dodo grant fulfillment-record-read failed for payment_id={payment_id}: record was not persisted"
        )
    if fulfillment.get("tenant_id") != tenant_id:
        # The first durable record fixes the tenant contract. A redelivery that
        # claims another tenant must not be able to re-route the grant.
        raise ValueError("Dodo payment tenant does not match its fulfillment record")

    # The first durable record fixes the payment contract. Product mappings,
    # quantities and customer resolution may change before a later retry.
    tenant_id = fulfillment["tenant_id"]
    product_id = fulfillment.get("product_id")
    credits = int(fulfillment["credits"])
    event_type = fulfillment["event_type"]
    customer_email = fulfillment.get("customer_email", customer_email)
    if fulfillment and fulfillment.get("status") == "completed":
        return {
            "status": "already_fulfilled",
            "tenant_id": tenant_id,
            "credits_granted": fulfillment["credits"],
            "payment_id": payment_id,
        }

    # 2. Track this attempt.
    try:
        await db.ff_dodo_fulfillments.update_one(
            {"_id": payment_id},
            {"$inc": {"attempts": 1}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )
    except PyMongoError as exc:
        raise _retryable("attempts-counter", exc) from exc

    # 3. Redis grant (atomic once-per-payment via Lua HSETNX + HINCRBY).
    #    Skipped when a previous attempt already granted it and recorded the
    #    intermediate "redis_granted" state.
    redis_granted = bool(fulfillment and fulfillment.get("status") == "redis_granted")
    if redis_granted:
        logger.info("Dodo grant retry: Redis already granted earlier payment_id=%s tenant_id=%s", payment_id, tenant_id)
    else:
        try:
            _apply_redis_grant(r, tenant_id, payment_id, credits)
        except redis.RedisError as exc:
            # The Lua script released the payment marker when the increment did
            # not apply, so a bounded redelivery is safe and loses no credits.
            raise _retryable("redis-grant", exc) from exc
        # 3b. Durable intermediate state BEFORE the Mongo mirror: if the
        # mirror (or this marker write) fails, a retry will see redis_granted
        # and skip the Redis step instead of re-granting.
        try:
            await db.ff_dodo_fulfillments.update_one(
                {"_id": payment_id},
                {"$set": {"status": "redis_granted", "updated_at": datetime.now(timezone.utc)}},
            )
        except PyMongoError as exc:
            raise _retryable("redis-granted-marker", exc) from exc

    # 4. Mongo mirror (idempotent per payment_id).
    try:
        await _mirror_grant_to_mongo(
            tenant_id=tenant_id,
            product_id=product_id,
            credits=credits,
            transaction_id=payment_id,
            event_type=event_type,
            customer_email=customer_email,
        )
    except PyMongoError as exc:
        raise _retryable("mongo-mirror", exc) from exc

    # 5. Mark completed.
    try:
        await db.ff_dodo_fulfillments.update_one(
            {"_id": payment_id},
            {"$set": {"status": "completed", "updated_at": datetime.now(timezone.utc)}},
        )
    except PyMongoError as exc:
        raise _retryable("completion-marker", exc) from exc

    return {
        "status": "ACTIVE",
        "tenant_id": tenant_id,
        "credits_granted": credits,
        "payment_id": payment_id,
        "event": event_type,
        "webhook_id": webhook_id,
    }


async def _mirror_grant_to_mongo(*, tenant_id: str, product_id: Optional[str],
                                 credits: int, transaction_id: str,
                                 event_type: str, customer_email: Optional[str]) -> None:
    """Upsert the Dodo grant into the Mongo credit ledger.

    check_credits prefers `subscriptions` by user_id, then `ff_credits` by
    tenant_id/user_id. Without this mirror, Dodo-paid credits would sit in
    Redis only and renders would still 402.
    """
    from datetime import timedelta as _td

    from .db import get_db

    db = get_db()
    now = datetime.now(timezone.utc)
    plan = _dodo_plan_label(product_id)

    user = await db.users.find_one({"id": tenant_id}, {"_id": 0})
    if not user and customer_email:
        user = await db.users.find_one(
            {"email": str(customer_email).lower().strip()}, {"_id": 0}
        )
    user_id = (user or {}).get("id") or tenant_id
    email = ((user or {}).get("email") or customer_email or "").lower().strip()

    # Enforce one account before any balance mutation. Index errors (including
    # legacy duplicate accounts) must abort rather than silently weaken safety.
    await db.ff_credits.create_index(
        "tenant_id", unique=True, partialFilterExpression={"tenant_id": {"$type": "string"}},
        name="ff_credits_tenant_unique",
    )
    await db.subscriptions.create_index(
        "user_id", unique=True, partialFilterExpression={"user_id": {"$type": "string"}},
        name="subscriptions_user_unique",
    )

    period_end = now + _td(days=30)
    await _apply_mongo_grant(
        db.ff_credits, {"tenant_id": user_id}, transaction_id,
        defaults={"user_id": user_id, "email": email,
                  "transaction_id": transaction_id, "customer_id": "dodo", "created_at": now},
        increments={"credits": int(credits)},
        updates={"plan": plan, "updated_at": now, "last_transaction_id": transaction_id},
    )
    await _apply_mongo_grant(
        db.subscriptions, {"user_id": user_id}, transaction_id,
        defaults={"created_at": now,
                  "paddle_customer_id": None, "paddle_subscription_id": None, "paddle_price_id": None},
        increments={"credits_remaining": int(credits), "credits_monthly": int(credits)},
        updates={"plan": plan, "status": "active", "current_period_end": period_end,
                 "updated_at": now, "dodo_transaction_id": transaction_id},
    )

    # Each account records the transaction in the same atomic write as its
    # increment. A retry can therefore finish a partial Mongo mirror safely.
    # This does not make the earlier Redis grant/acknowledgement transactional.
    import hashlib
    audit_id = "dodo:" + hashlib.sha256(
        json.dumps([user_id, transaction_id], separators=(",", ":")).encode()
    ).hexdigest()
    await db.ff_billing_events.update_one({"_id": audit_id}, {"$setOnInsert": {
        "id": audit_id, "tenant_id": user_id, "email": email,
        "plan": plan, "transaction_id": transaction_id, "customer_id": "dodo",
        "event_type": f"dodo:{event_type}", "credits_added": int(credits),
        "created_at": now,
    }}, upsert=True)
    logger.info(f"Dodo mongo mirror ok tenant={user_id} plan={plan} credits={credits}")


async def _apply_mongo_grant(collection, identity: dict, transaction_id: str, *,
                             defaults: dict, increments: dict, updates: dict) -> None:
    """Create-or-update and increment in one atomic, idempotent write.

    Using a single upsert with $setOnInsert plus $inc/$addToSet removes the
    zero-balance window where a crash between account creation and increment
    could leave an authoritative subscription doc with no credits.
    """
    from pymongo.errors import DuplicateKeyError

    try:
        await collection.update_one(
            {**identity, "processed_dodo_transactions": {"$ne": transaction_id}},
            {
                "$setOnInsert": defaults,
                "$inc": increments,
                "$set": updates,
                "$addToSet": {"processed_dodo_transactions": transaction_id},
            },
            upsert=True,
        )
    except DuplicateKeyError:
        # A concurrent initial grant won the upsert race. Retry the update
        # side only; the transaction marker prevents double incrementing.
        await collection.update_one(
            {**identity, "processed_dodo_transactions": {"$ne": transaction_id}},
            {
                "$inc": increments,
                "$set": updates,
                "$addToSet": {"processed_dodo_transactions": transaction_id},
            },
        )

