from fastapi import APIRouter, Request, HTTPException, Header
import json, redis, os, hmac, hashlib, base64, logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("billing.paypal")
router = APIRouter(prefix="/api/billing")
r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

# PAYMENT_PROVIDER single env flip: paypal|dodo (default dodo per vault)
PAYMENT_PROVIDER = (os.getenv("PAYMENT_PROVIDER") or "dodo").strip().lower()

# Finite credit map - same as dodo, no infinite
PAYPAL_CREDIT_MAP = {"19": 100, "29": 500, "49": 1000, "79": 2000}
DEFAULT_PAYPAL_CREDITS = 100

def _paypal_credits_for_amount(amount_str: Optional[str]) -> int:
    try:
        amt = str(amount_str or "").strip()
        # map exact amounts to credits; fallback default
        if amt in PAYPAL_CREDIT_MAP:
            return PAYPAL_CREDIT_MAP[amt]
        # legacy 5000 handling
        if amt == "5000.00":
            return 5000
        val = float(amt) if amt else 0
        if val >= 79:
            return 2000
        if val >= 49:
            return 1000
        if val >= 29:
            return 500
        if val >= 19:
            return 100
    except Exception:
        pass
    return DEFAULT_PAYPAL_CREDITS

def _verify_paypal_signature(body: bytes, headers: dict) -> bool:
    # PayPal webhook verify: if PAYPAL_WEBHOOK_ID set, verify via PayPal API; else HMAC fallback via secret
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()
    secret = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
    # If no webhook id/secret, allow but warn (dev mode) - DODO already enforces sig when secret set
    if not webhook_id and not secret:
        logger.warning("PAYPAL_WEBHOOK_ID/SECRET not set - skipping PayPal sig verify (dev)")
        return True
    # Standard Webhooks style? PayPal sends transmission headers
    # For now verify HMAC if header present, else accept and audit
    sig = headers.get("paypal-transmission-sig") or headers.get("PAYPAL-TRANSMISSION-SIG") or ""
    if sig and secret:
        try:
            expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
            return hmac.compare_digest(sig, expected)
        except Exception:
            return False
    return True

@router.get("/provider")
async def get_provider():
    provider = (os.getenv("PAYMENT_PROVIDER") or PAYMENT_PROVIDER).strip().lower()
    return {"provider": provider, "paypal_env": os.getenv("PAYPAL_ENVIRONMENT","sandbox"), "dodo_env": os.getenv("DODO_PAYMENTS_ENVIRONMENT","test_mode")}

@router.post("/webhook/paypal")
async def paypal_webhook(request: Request, paypal_transmission_sig: Optional[str] = Header(None)):
    raise HTTPException(status_code=410, detail="disabled")
    provider = (os.getenv("PAYMENT_PROVIDER") or PAYMENT_PROVIDER).strip().lower()
    if provider not in ("paypal","both"):
        # When PAYMENT_PROVIDER=dodo, PayPal webhook is dormant but still ack for probes
        logger.info(f"PayPal webhook dormant provider={provider} - ack without grant")
        return {"status":"dormant","provider":provider}
    body = await request.body()
    # verify
    if not _verify_paypal_signature(body, dict(request.headers)):
        raise HTTPException(status_code=401, detail="Invalid PayPal signature")
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    transaction_id = payload.get("id") or payload.get("resource",{}).get("id") or payload.get("event_id")
    if not transaction_id:
        raise HTTPException(400, "No transaction_id")
    # idempotency - atomic SET NX
    was_new = r.set(f"paypal:webhooks:processed:{transaction_id}", "1", nx=True, ex=86400)
    if not was_new:
        return {"status":"already_processed","transaction_id":transaction_id}
    # also legacy set
    r.sadd("processed_transactions", transaction_id)
    r.sadd("paypal:webhooks:processed", transaction_id)
    amount = None
    try:
        amount = payload.get("resource",{}).get("amount",{}).get("value") or payload.get("purchase_units",[{}])[0].get("amount",{}).get("value") or payload.get("amount",{}).get("value")
    except Exception:
        amount = None
    tenant_id = payload.get("custom_id") or payload.get("resource",{}).get("custom_id") or payload.get("tenant_id") or (payload.get("resource",{}).get("custom_data",{}).get("tenant_id") if isinstance(payload.get("resource",{}).get("custom_data"), dict) else None)
    # fallback metadata
    if not tenant_id:
        md = payload.get("resource",{}).get("metadata") or payload.get("metadata") or {}
        if isinstance(md, dict):
            tenant_id = md.get("tenant_id")
    if not tenant_id:
        raise HTTPException(400, "No tenant_id")
    credits_to_add = _paypal_credits_for_amount(str(amount) if amount else None)
    # clamp finite
    credits_to_add = max(0, min(credits_to_add, 100000))
    now_iso = datetime.now(timezone.utc).isoformat()
    pipe = r.pipeline()
    pipe.hincrby(f"tenant:{tenant_id}", "credits", credits_to_add)
    pipe.hset(f"tenant:{tenant_id}", mapping={"status":"ACTIVE","last_payment":now_iso,"last_transaction":transaction_id,"last_provider":"paypal"})
    # do NOT expire tenant hash - finite credits persist; only audit expires
    pipe.hset(f"audit:transaction:{transaction_id}", mapping={"tenant_id":tenant_id,"amount":str(amount or ""),"credits":str(credits_to_add),"timestamp":now_iso,"provider":"paypal"})
    pipe.expire(f"audit:transaction:{transaction_id}", 2592000)
    pipe.lpush(f"tenant:{tenant_id}:billing_audit", json.dumps({"provider":"paypal","credits":credits_to_add,"amount":str(amount or ""),"transaction_id":transaction_id,"timestamp":now_iso}))
    pipe.ltrim(f"tenant:{tenant_id}:billing_audit", 0, 999)
    pipe.execute()
    logger.info(f"PayPal webhook granted {credits_to_add} to tenant:{tenant_id} tx={transaction_id}")
    return {"status":"ACTIVE","tenant_id":tenant_id,"credits_granted":credits_to_add,"credits":credits_to_add,"transaction_id":transaction_id,"provider":"paypal"}
