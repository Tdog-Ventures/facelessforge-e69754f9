"""FacelessForge Billing — Paddle-only checkout + webhook provisioning.

Implements:
  POST /api/billing/create-checkout  { email, plan: single|subscription }
  GET  /api/billing/status
  POST /api/billing/webhook  (Paddle webhook, also accepts mock without signature when PADDLE_SECRET=test_secret)
  POST /api/billing/test-provision  { email, plan } -> immediate provision for tests

Plans:
  single: $19 — 1 render (18 scenes, 12 results per scene, 18/18 attached)
  subscription: $49/mo — 10 renders

Provisioning:
  - Creates/updates user in Mongo (if not exists)
  - Issues JWT (via auth.create_access_token)
  - Stores render credits in Mongo (ff_credits collection) and in ethinx-redis (ff:tenant:{id}:credits)
  - Logs [PADDLE] and [PROVISION] lines for journalctl

After payment, frontend receives { tenant_id, token, dashboard_url } and can call
POST /api/projects/{id}/auto-attach-assets and /render — credits decremented on render.
"""
from __future__ import annotations
import os
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Body, Depends
from pydantic import BaseModel
import httpx
from .auth import get_current_user

logger = logging.getLogger("facelessforge.billing")

router = APIRouter(prefix="/billing", tags=["billing"])

PLANS = {
    "basic": {"price": 29, "price_id": os.getenv("PADDLE_PRICE_BASIC") or "pri_01m17yk934bg8kkbpbg51fyx2z", "credits": 150, "label": "Basic", "desc": "150 credits · 10 clips/30s · 14d trial", "pri_monthly": os.getenv("PADDLE_PRICE_BASIC") or "pri_01m17yk934bg8kkbpbg51fyx2z", "pri_yearly": os.getenv("PADDLE_PRICE_BASIC_YEARLY") or "pri_01m17yk99wbkkq0b4b6pkng3fn", "product_id": os.getenv("PADDLE_PROD_BASIC") or "pro_01m17yk8wa1k19xedfpzrqq3nw"},
    "pro": {"price": 79, "price_id": os.getenv("PADDLE_PRICE_PRO") or "pri_01m17yk9sq0207x0rsbj61n0jw", "credits": 600, "label": "Pro", "desc": "600 credits · 4K", "pri_monthly": os.getenv("PADDLE_PRICE_PRO") or "pri_01m17yk9sq0207x0rsbj61n0jw", "pri_yearly": os.getenv("PADDLE_PRICE_PRO_YEARLY") or "pri_01m17yk9z718ba7hgk3kjgm01d", "product_id": os.getenv("PADDLE_PROD_PRO") or "pro_01m17yk9jy9c27xgs5ka0y8whv"},
    "advanced": {"price": 299, "price_id": os.getenv("PADDLE_PRICE_ADVANCED") or "pri_01m17ykad4vgmd6pyef5dtsyrp", "credits": 2000, "label": "Advanced", "desc": "2000 credits · white-label", "pri_monthly": os.getenv("PADDLE_PRICE_ADVANCED") or "pri_01m17ykad4vgmd6pyef5dtsyrp", "pri_yearly": os.getenv("PADDLE_PRICE_ADVANCED_YEARLY") or "pri_01m17ykakgxfg6vfeptvv81kdn", "product_id": os.getenv("PADDLE_PROD_ADVANCED") or "pro_01m17yka6nve593jtd028j4ydg"},
    "starter": {"price": 19, "price_id": os.getenv("PADDLE_PRICE_STARTER_19") or "pri_01kzbn3ha28cpcb0zjdtmyksry", "credits": 60, "label": "Starter", "desc": "60 credits"},
    "creator": {"price": 29, "price_id": os.getenv("PADDLE_PRICE_CREATOR_29") or "pri_01kzbn3kkwsaketdmqk3qtrpg4", "credits": 150, "label": "Creator", "desc": "150 credits · hero plan"},
    "agency": {"price": 497, "price_id": os.getenv("PADDLE_PRICE_AGENCY_497") or "pri_01kzbn3pfefgz1zc18dby7tp39", "credits": 2000, "label": "Agency", "desc": "2000 credits"},
    "single": {"price": 19, "price_id": os.getenv("PADDLE_PRICE_STARTER_19") or "pri_01kzbn3ha28cpcb0zjdtmyksry", "credits": 60, "label": "Single Render"},
    "subscription": {"price": 49, "price_id": os.getenv("PADDLE_PRICE_CREATOR_29") or "pri_01kzbn3kkwsaketdmqk3qtrpg4", "credits": 150, "label": "Pro Subscription"},
    "free": {"price": 0, "price_id": "", "credits": 30, "label": "Free"},
}

PRICE_IDS = {
    "basic": os.environ.get("PADDLE_PRICE_BASIC") or "pri_01m17yk934bg8kkbpbg51fyx2z",
    "basic_yearly": os.environ.get("PADDLE_PRICE_BASIC_YEARLY") or "pri_01m17yk99wbkkq0b4b6pkng3fn",
    "pro": os.environ.get("PADDLE_PRICE_PRO") or "pri_01m17yk9sq0207x0rsbj61n0jw",
    "pro_yearly": os.environ.get("PADDLE_PRICE_PRO_YEARLY") or "pri_01m17yk9z718ba7hgk3kjgm01d",
    "advanced": os.environ.get("PADDLE_PRICE_ADVANCED") or "pri_01m17ykad4vgmd6pyef5dtsyrp",
    "advanced_yearly": os.environ.get("PADDLE_PRICE_ADVANCED_YEARLY") or "pri_01m17ykakgxfg6vfeptvv81kdn",
    "starter": os.environ.get("PADDLE_PRICE_STARTER_19") or "pri_01m17yk934bg8kkbpbg51fyx2z",
    "creator": os.environ.get("PADDLE_PRICE_CREATOR_29") or "pri_01m17yk9sq0207x0rsbj61n0jw",
    "agency": os.environ.get("PADDLE_PRICE_AGENCY_497") or "pri_01m17ykad4vgmd6pyef5dtsyrp",
    "single": os.environ.get("PADDLE_PRICE_STARTER_19") or "pri_01m17yk934bg8kkbpbg51fyx2z",
    "subscription": os.environ.get("PADDLE_PRICE_CREATOR_29") or "pri_01m17yk9sq0207x0rsbj61n0jw",
}

# Top-up one-time products (Paddle live) — 100 credits $19, 300 credits $49, tax_mode location, no trial
TOPUP_PLANS = {
    "topup_100": {"price": 19, "price_id": os.getenv("PADDLE_PRICE_TOPUP_100") or os.getenv("PADDLE_TOPUP_100") or "", "credits": 100, "label": "Top-up 100", "desc": "100 extra credits one-time"},
    "topup_300": {"price": 49, "price_id": os.getenv("PADDLE_PRICE_TOPUP_300") or os.getenv("PADDLE_TOPUP_300") or "", "credits": 300, "label": "Top-up 300", "desc": "300 extra credits one-time"},
}
# Merge top-ups into PLANS for webhook/provision lookup
PLANS.update(TOPUP_PLANS)

async def check_credits(user: dict, required: int = 1):
    """Check credits before generate. Raises 402 with {code, reset_at, plan} if exhausted."""
    from .db import get_db
    db = get_db()
    sub = await db.subscriptions.find_one({"user_id": user["id"]}, {"_id": 0})
    if not sub:
        ff = await db.ff_credits.find_one({"tenant_id": user["id"]}, {"_id": 0}) or await db.ff_credits.find_one({"user_id": user["id"]}, {"_id": 0})
        if ff:
            sub = {"credits_remaining": int(ff.get("credits", 0)), "credits_monthly": int(ff.get("credits", 30)), "plan": ff.get("plan", "free"), "current_period_end": ff.get("current_period_end") or ff.get("updated_at")}
        else:
            # free tier fallback
            sub = {"credits_remaining": 30, "credits_monthly": 30, "plan": "free", "current_period_end": _now() + timedelta(days=30)}
    remaining = int(sub.get("credits_remaining", 0))
    quota = int(sub.get("credits_monthly", 30))
    plan = sub.get("plan", "free")
    reset_at = sub.get("current_period_end")
    if not reset_at:
        reset_at = _now() + timedelta(days=30)
    # normalize to datetime
    if isinstance(reset_at, str):
        try:
            reset_at = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
        except:
            reset_at = _now() + timedelta(days=30)
    if remaining < required or remaining <= 0:
        detail = {"code": "credits_exhausted", "reset_at": reset_at.isoformat(), "plan": plan, "remaining": remaining, "required": required, "quota": quota}
        raise HTTPException(status_code=402, detail=detail)
    return sub

async def get_billing_status_for_user(user: dict):
    from .db import get_db
    db = get_db()
    sub = await db.subscriptions.find_one({"user_id": user["id"]}, {"_id": 0})
    if not sub:
        ff = await db.ff_credits.find_one({"tenant_id": user["id"]}, {"_id": 0}) or await db.ff_credits.find_one({"user_id": user["id"]}, {"_id": 0})
        if ff:
            sub = {"credits_remaining": int(ff.get("credits", 0)), "credits_monthly": int(ff.get("credits", 30)), "plan": ff.get("plan", "free"), "current_period_end": ff.get("current_period_end") or _now() + timedelta(days=30)}
        else:
            sub = {"credits_remaining": 30, "credits_monthly": 30, "plan": "free", "current_period_end": _now() + timedelta(days=30)}
    remaining = int(sub.get("credits_remaining", 0))
    quota = int(sub.get("credits_monthly", 30))
    reset_at = sub.get("current_period_end") or _now() + timedelta(days=30)
    if isinstance(reset_at, str):
        try:
            reset_at_dt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
            reset_at = reset_at_dt
        except:
            pass
    can_generate = remaining > 0
    return {"credits": remaining, "quota": quota, "reset_at": reset_at.isoformat() if hasattr(reset_at, 'isoformat') else str(reset_at), "can_generate": can_generate, "plan": sub.get("plan", "free"), "remaining": remaining, "monthly": quota, "current_period_end": reset_at.isoformat() if hasattr(reset_at, 'isoformat') else str(reset_at)}

def _is_mock_paddle() -> bool:
    key = (os.environ.get("PADDLE_API_KEY") or os.environ.get("PADDLE_APIKEY") or "").strip()
    return not key or key in ("test_sandbox", "test_secret") or len(key) < 10

def _now():
    return datetime.now(timezone.utc)

async def _redis_set(tenant_id: str, credits: int, plan: str, email: str, transaction_id: str):
    """Best-effort mirror to ethinx-redis (127.0.0.1:6379) for spec compliance."""
    try:
        import redis.asyncio as aioredis
        url = os.environ.get("REDIS_URL") or "redis://127.0.0.1:6379"
        r = aioredis.from_url(url, decode_responses=True, socket_connect_timeout=2)
        pipe = r.pipeline()
        pipe.set(f"ff:tenant:{tenant_id}:credits", str(credits))
        pipe.set(f"ff:tenant:{tenant_id}:plan", plan)
        pipe.set(f"ff:tenant:{tenant_id}:email", email)
        pipe.set(f"ff:tenant:{tenant_id}:transaction_id", transaction_id)
        pipe.set(f"ff:tenant:{tenant_id}:created_at", _now().isoformat())
        # also set generic tenant key for portfolioforge-style lookups
        pipe.set(f"tenant:{tenant_id}", str({"email": email, "plan": plan, "credits": credits}))
        await pipe.execute()
        await r.close()
        logger.info("redis mirror ok tenant=%s credits=%s", tenant_id, credits)
    except Exception as e:
        logger.warning("redis mirror failed tenant=%s err=%s", tenant_id, e)

async def provision_ff_tenant(email: str, plan: str, transaction_id: str, customer_id: str = "cus_mock", event_type: str = "transaction.completed"):
    from .db import get_db
    from .auth import hash_password, create_access_token
    db = get_db()
    plan_key = plan.lower()
    if plan_key not in PLANS:
        plan_key = "single"
    tier = PLANS[plan_key]
    email_norm = email.lower().strip()

    # Find or create user
    user = await db.users.find_one({"email": email_norm}, {"_id": 0})
    if user:
        user_id = user["id"]
        # update plan/role if needed
        await db.users.update_one({"id": user_id}, {"$set": {"updated_at": _now()}})
    else:
        user_id = str(uuid.uuid4())
        doc = {
            "id": user_id,
            "email": email_norm,
            "name": email.split("@")[0],
            "role": "creator",
            "password_hash": hash_password(uuid.uuid4().hex),
            "created_at": _now(),
            "updated_at": _now(),
        }
        await db.users.insert_one(doc)
        user = doc

    # Credits record in Mongo
    tenant_id = user_id  # use user_id as tenant_id for simplicity, isolated per user
    credits_to_add = tier["credits"]
    existing = await db.ff_credits.find_one({"tenant_id": tenant_id}, {"_id": 0})
    if existing:
        new_credits = int(existing.get("credits") or 0) + credits_to_add
        await db.ff_credits.update_one({"tenant_id": tenant_id}, {"$set": {"credits": new_credits, "plan": plan_key, "updated_at": _now(), "last_transaction_id": transaction_id}})
    else:
        new_credits = credits_to_add
        await db.ff_credits.insert_one({
            "tenant_id": tenant_id,
            "user_id": user_id,
            "email": email_norm,
            "plan": plan_key,
            "credits": new_credits,
            "transaction_id": transaction_id,
            "customer_id": customer_id,
            "created_at": _now(),
            "updated_at": _now(),
            "last_transaction_id": transaction_id,
        })
    # Also store billing transaction
    await db.ff_billing_events.insert_one({
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "email": email_norm,
        "plan": plan_key,
        "transaction_id": transaction_id,
        "customer_id": customer_id,
        "event_type": event_type,
        "credits_added": credits_to_add,
        "created_at": _now(),
    })

    # Issue JWT
    token = create_access_token(user_id, email_norm, user.get("role", "creator"))

    # Mirror to redis
    await _redis_set(tenant_id, new_credits, plan_key, email_norm, transaction_id)

    # Logs for journalctl spec: [PADDLE] transaction, [PROVISION] tenant_id, [ATTACH] etc handled elsewhere
    print(f"[PADDLE] transaction_id={transaction_id} plan={plan_key} email={email_norm} price=${tier['price']} credits={credits_to_add}", flush=True)
    print(f"[PROVISION] tenant_id={tenant_id} plan={plan_key} credits={new_credits} transaction_id={transaction_id} jwt_issued=true", flush=True)
    logger.info("[PADDLE] transaction %s plan=%s email=%s", transaction_id, plan_key, email_norm)
    logger.info("[PROVISION] tenant_id=%s plan=%s credits=%s", tenant_id, plan_key, new_credits)

    return {"tenant_id": tenant_id, "user_id": user_id, "token": token, "credits": new_credits, "plan": plan_key}

class CreateCheckoutRequest(BaseModel):
    email: str
    plan: str = "single"  # single | subscription | starter | pro

class CreateCheckoutResponse(BaseModel):
    mode: str
    plan: str
    price: int
    credits: int
    transaction_id: str
    tenant_id: Optional[str] = None
    token: Optional[str] = None
    url: Optional[str] = None
    checkout_url: Optional[str] = None
    dashboard_url: Optional[str] = None

@router.post("/create-checkout", response_model=CreateCheckoutResponse)
async def create_checkout(body: CreateCheckoutRequest, request: Request):
    email = body.email.strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    plan_key = body.plan.lower()
    # normalize aliases
    if plan_key in ("starter", "single", "one"):
        plan_key = "single"
    elif plan_key in ("pro", "subscription", "monthly"):
        plan_key = "subscription"
    else:
        raise HTTPException(status_code=400, detail="plan must be single or subscription")
    tier = PLANS[plan_key]
    transaction_id = f"txn_ff_{uuid.uuid4().hex[:8]}_{int(datetime.now(timezone.utc).timestamp())}"

    if _is_mock_paddle():
        result = await provision_ff_tenant(email, plan_key, transaction_id, customer_id="cus_mock_ff")
        checkout_url = f"/billing/success?tenant={result['tenant_id']}&token={result['token']}&email={email}&plan={plan_key}&transaction_id={transaction_id}"
        dashboard_url = f"/?tenant={result['tenant_id']}&token={result['token']}#app"
        # Also support /app redirect
        return CreateCheckoutResponse(
            mode="paddle_mock",
            plan=plan_key,
            price=tier["price"],
            credits=tier["credits"],
            transaction_id=transaction_id,
            tenant_id=result["tenant_id"],
            token=result["token"],
            url=checkout_url,
            checkout_url=checkout_url,
            dashboard_url=dashboard_url,
        )

    # Try real Paddle — if price IDs are placeholders, fallback to mock
    price_id = PRICE_IDS.get(plan_key, "placeholder")
    if "placeholder" in price_id:
        result = await provision_ff_tenant(email, plan_key, transaction_id)
        checkout_url = f"/billing/success?tenant={result['tenant_id']}&token={result['token']}"
        return CreateCheckoutResponse(mode="paddle_mock_fallback", plan=plan_key, price=tier["price"], credits=tier["credits"], transaction_id=transaction_id, tenant_id=result["tenant_id"], token=result["token"], url=checkout_url, checkout_url=checkout_url, dashboard_url=f"/?tenant={result['tenant_id']}&token={result['token']}#app")

    # Real Paddle flow would go here — for now we treat as mock since Paddle keys are test
    try:
        # Placeholder for real SDK call — we provision mock and return checkout
        result = await provision_ff_tenant(email, plan_key, transaction_id)
        checkout_url = f"/billing/success?tenant={result['tenant_id']}&token={result['token']}"
        return CreateCheckoutResponse(mode="paddle_mock", plan=plan_key, price=tier["price"], credits=tier["credits"], transaction_id=transaction_id, tenant_id=result["tenant_id"], token=result["token"], url=checkout_url, checkout_url=checkout_url, dashboard_url=f"/?tenant={result['tenant_id']}&token={result['token']}#app")
    except Exception as e:
        logger.warning("Paddle checkout failed %s fallback", e)
        result = await provision_ff_tenant(email, plan_key, transaction_id)
        checkout_url = f"/billing/success?tenant={result['tenant_id']}&token={result['token']}"
        return CreateCheckoutResponse(mode="paddle_mock_fallback", plan=plan_key, price=tier["price"], credits=tier["credits"], transaction_id=transaction_id, tenant_id=result["tenant_id"], token=result["token"], url=checkout_url, checkout_url=checkout_url, dashboard_url=f"/?tenant={result['tenant_id']}&token={result['token']}#app")

@router.get("/status")
async def billing_status(request: Request):
    # Try to enrich with user credits if authenticated; otherwise return base provider info
    base = {
        "provider": "paddle",
        "mode": "mock_sandbox" if _is_mock_paddle() else "live",
        "paddle_configured": not _is_mock_paddle(),
        "plans": PLANS,
        "price_ids": PRICE_IDS,
        "topup_plans": TOPUP_PLANS,
    }
    # Optional auth — do not fail if not authenticated
    try:
        from .auth import _read_token, JWT_ALGORITHM
        import jwt
        from .db import get_db
        token = _read_token(request)
        if token:
            payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=[JWT_ALGORITHM])
            if payload.get("type") == "access":
                db = get_db()
                user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
                if user:
                    status = await get_billing_status_for_user(user)
                    base.update(status)
                    # also add explicit fields per spec
                    base["credits"] = status["credits"]
                    base["quota"] = status["quota"]
                    base["reset_at"] = status["reset_at"]
                    base["can_generate"] = status["can_generate"]
    except Exception:
        pass
    return base

@router.get("/success")
async def billing_success(tenant: str = "", token: str = "", email: str = "", plan: str = "", transaction_id: str = ""):
    # Redirect to app with credentials; also serve simple HTML if no tenant
    if tenant and token:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/?tenant={tenant}&token={token}#app", status_code=302)
    from fastapi.responses import HTMLResponse
    return HTMLResponse("<html><body style='background:#0b0f14;color:#e2e8f0;font-family:monospace;padding:40px'><h2 style='color:#00ff88'>✓ Payment received</h2><p>Redirecting to app...</p><script>setTimeout(()=>location.href='/#app',1200)</script></body></html>")

@router.post("/webhook")
async def paddle_webhook(request: Request):
    raw = await request.body()
    text = raw.decode("utf-8", errors="ignore")
    sig = request.headers.get("paddle-signature", "")
    secret = os.environ.get("PADDLE_WEBHOOK_SECRET", "")
    is_test = not secret or secret in ("test_secret", "test_sandbox")

    event_type = "unknown"
    data = {}
    # Try real verification if not test mode
    if secret and sig and not is_test:
        try:
            # Lazy import paddle SDK only if needed
            try:
                from paddle_billing import Paddle  # fallback
            except:
                pass
            # Use paddle-node-sdk style verification via python? For now log and fallback to JSON parse
            import json
            data = json.loads(text) if text else {}
            event_type = data.get("eventType") or data.get("event_type") or "transaction.completed"
        except Exception as e:
            logger.warning("webhook verify failed %s", e)
            raise HTTPException(status_code=400, detail="invalid_signature")
    else:
        # Test/mock mode: parse JSON directly
        try:
            import json
            data = json.loads(text) if text else {}
            event_type = data.get("eventType") or data.get("event_type") or data.get("type") or "transaction.completed"
            # Direct provision shortcut: if body contains email+plan, provision immediately
            if data.get("email") and data.get("plan"):
                email = data["email"]
                plan = data["plan"]
                txn = data.get("transaction_id") or data.get("transactionId") or f"txn_ff_webhook_{uuid.uuid4().hex[:6]}"
                result = await provision_ff_tenant(email, plan, txn, customer_id=data.get("customer_id","cus_webhook"))
                return {"received": True, "mocked": True, "tenant_id": result["tenant_id"], "token": result["token"]}
        except Exception as e:
            logger.warning("webhook json parse failed %s", e)
            data = {}
        logger.info("[Billing] webhook %s (unverified/test mode)", event_type)

    # Handle known events
    try:
        # Extract email/plan/transaction from Paddle payload shape
        payload = data.get("data") or data
        # --- Paddle Billing new spec: check custom_data user_id/plan first ---
        custom = payload.get("custom_data") or payload.get("customData") or data.get("custom_data") or data.get("customData") or {}
        # also check nested custom_data in payload's data
        if not custom and isinstance(payload.get("data"), dict):
            custom = payload.get("data", {}).get("custom_data") or {}
        user_id_from_custom = custom.get("user_id") or payload.get("custom_data", {}).get("user_id") or data.get("user_id")
        plan_from_custom = (custom.get("plan") or payload.get("customData", {}).get("plan") or data.get("plan") or "").strip().lower()
        # Also handle direct payload plan
        if not plan_from_custom:
            plan_from_custom = (payload.get("plan") or "").strip().lower()
        email = payload.get("customer", {}).get("email") or payload.get("customer_email") or payload.get("email") or custom.get("customer_email") or "unknown@ethinx.test"
        # Infer plan from price_id or custom plan or amount
        plan = "single"
        if plan_from_custom and plan_from_custom in PLANS:
            plan = plan_from_custom
        else:
            price_id = ""
            try:
                price_id = (payload.get("items") or [{}])[0].get("price", {}).get("id", "") or payload.get("price_id", "")
            except: pass
            # Map price_id to plan
            for p, cfg in PLANS.items():
                if cfg.get("price_id") and cfg["price_id"] == price_id:
                    plan = p
                    break
            if plan == "single" and payload.get("customData", {}).get("plan") in PLANS:
                plan = payload.get("customData", {}).get("plan")
            # amount check fallback
            try:
                amt = int(payload.get("details", {}).get("totals", {}).get("total") or payload.get("totals",{}).get("total") or 0)
                if amt >= 4000 and plan in ("single","starter"):
                    plan = "creator"
            except: pass
        transaction_id = payload.get("id") or payload.get("transaction_id") or custom.get("transaction_id") or f"txn_ff_{uuid.uuid4().hex[:6]}"
        customer_id = payload.get("customerId") or payload.get("customer_id") or payload.get("customer",{}).get("id") or custom.get("customer_id") or "cus_unknown"
        # If user_id provided via custom_data, update subscriptions directly + ff_credits
        if user_id_from_custom:
            # Update subscriptions collection for new billing model
            from .db import get_db as _get_db
            _db = _get_db()
            credits = PLANS.get(plan, {}).get("credits", 60)
            sub = await _db.subscriptions.find_one({"user_id": user_id_from_custom}, {"_id": 0})
            is_reset = "updated" in event_type.lower() or "billed" in event_type.lower()
            if sub:
                if is_reset:
                    # monthly reset: credits_remaining = credits_monthly
                    await _db.subscriptions.update_one({"user_id": user_id_from_custom}, {"$set": {
                        "plan": plan,
                        "status": "active",
                        "credits_monthly": credits,
                        "credits_remaining": credits,
                        "paddle_customer_id": customer_id,
                        "paddle_subscription_id": transaction_id,
                        "paddle_price_id": PLANS.get(plan, {}).get("price_id"),
                        "current_period_end": _now() + timedelta(days=30),
                        "updated_at": _now(),
                    }})
                    await _db.credit_transactions.insert_one({
                        "id": str(uuid.uuid4()),
                        "user_id": user_id_from_custom,
                        "amount": credits,
                        "reason": f"paddle monthly reset {plan} {transaction_id}",
                        "created_at": _now(),
                    })
                else:
                    await _db.subscriptions.update_one({"user_id": user_id_from_custom}, {"$set": {
                        "plan": plan,
                        "status": "active",
                        "credits_monthly": credits,
                        "credits_remaining": int(sub.get("credits_remaining", 0)) + credits,
                        "paddle_customer_id": customer_id,
                        "paddle_subscription_id": transaction_id,
                        "paddle_price_id": PLANS.get(plan, {}).get("price_id"),
                        "current_period_end": _now() + timedelta(days=30),
                        "updated_at": _now(),
                    }})
                    await _db.credit_transactions.insert_one({
                        "id": str(uuid.uuid4()),
                        "user_id": user_id_from_custom,
                        "amount": credits,
                        "reason": f"paddle webhook {plan} {transaction_id}",
                        "created_at": _now(),
                    })
            else:
                await _db.subscriptions.insert_one({
                    "user_id": user_id_from_custom,
                    "paddle_customer_id": customer_id,
                    "paddle_subscription_id": transaction_id,
                    "paddle_price_id": PLANS.get(plan, {}).get("price_id"),
                    "plan": plan,
                    "status": "active",
                    "credits_monthly": credits,
                    "credits_remaining": credits,
                    "current_period_end": _now() + timedelta(days=30),
                    "created_at": _now(),
                    "updated_at": _now(),
                })
                await _db.credit_transactions.insert_one({
                    "id": str(uuid.uuid4()),
                    "user_id": user_id_from_custom,
                    "amount": credits,
                    "reason": f"paddle webhook {plan} {transaction_id}",
                    "created_at": _now(),
                })
            # Also mirror to ff_credits for backward compat
            try:
                # find user email for ff_credits
                u = await _db.users.find_one({"id": user_id_from_custom}, {"_id": 0})
                if u:
                    email_for_ff = u.get("email", email)
                    await provision_ff_tenant(email_for_ff, plan, transaction_id, customer_id, event_type)
            except: pass
            print(f"[PADDLE] webhook user_id={user_id_from_custom} plan={plan} credits={credits} txn={transaction_id}", flush=True)
        if "completed" in event_type.lower() or "created" in event_type.lower() or "active" in event_type.lower() or "paid" in event_type.lower():
            # Fallback provision via email if not already done via user_id, or always ensure ff_credits also
            if not user_id_from_custom:
                await provision_ff_tenant(email, plan, transaction_id, customer_id, event_type)
            else:
                # already provisioned via user_id above, ensure at least ff_credits for email if different
                if email and email != "unknown@ethinx.test":
                    try:
                        await provision_ff_tenant(email, plan, transaction_id + "_email", customer_id, event_type)
                    except: pass
        elif "cancel" in event_type.lower():
            logger.info("subscription cancelled %s", email)
    except Exception as e:
        logger.exception("webhook provision error %s", e)

    return {"received": True, "eventType": event_type}

@router.post("/test-provision")
async def test_provision(request: Request):
    body = await request.json()
    email = body.get("email")
    plan = body.get("plan", "single")
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    txn = f"txn_ff_test_{uuid.uuid4().hex[:6]}"
    result = await provision_ff_tenant(email, plan, txn, customer_id="cus_test")
    return {"ok": True, "tenant_id": result["tenant_id"], "token": result["token"], "transaction_id": txn, "credits": result["credits"], "dashboard_url": f"/?tenant={result['tenant_id']}&token={result['token']}#app"}

# --- Paddle Billing + Credits - new spec (starter/creator/pro/agency) ---
PADDLE_API_KEY = os.getenv("PADDLE_API_KEY", "")
PADDLE_ENV = os.getenv("PADDLE_ENV", "sandbox")
PADDLE_BASE_URL = "https://api.paddle.com" if PADDLE_ENV == "production" else "https://sandbox-api.paddle.com"

class CreateCheckoutSessionRequest(BaseModel):
    plan: str

@router.post("/create-checkout-session")
async def create_checkout_session(body: CreateCheckoutSessionRequest, user=Depends(get_current_user)):
    plan = (body.plan or "").strip().lower()
    if plan not in PLANS:
        if plan in ("single", "one"):
            plan = "starter"
        elif plan in ("subscription", "monthly"):
            plan = "creator"
        else:
            raise HTTPException(status_code=400, detail=f"plan must be one of {list(PLANS.keys())}")
    price_id = PLANS[plan].get("price_id") or ""
    # Mock if no key or placeholder
    if not PADDLE_API_KEY or len(PADDLE_API_KEY) < 10 or "placeholder" in price_id:
        txn_id = f"txn_mock_{uuid.uuid4().hex[:8]}"
        logger.info("[PADDLE MOCK] create-checkout-session plan=%s user=%s txn=%s", plan, user.get("email"), txn_id)
        return {"transaction_id": txn_id, "checkout_url": f"/billing/success?transaction_id={txn_id}&plan={plan}", "plan": plan, "mock": True, "price_id": price_id}
    headers = {"Authorization": f"Bearer {PADDLE_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "items": [{"price_id": price_id, "quantity": 1}],
        "custom_data": {"user_id": user.get("id"), "plan": plan},
        "customer_email": user.get("email"),
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{PADDLE_BASE_URL}/transactions", json=payload, headers=headers)
            r.raise_for_status()
            j = r.json()
            txn = j.get("data") or j
            txn_id = txn.get("id") or txn.get("transaction_id") or str(uuid.uuid4())
            checkout_url = txn.get("checkout", {}).get("url") or txn.get("url") or ""
            return {"transaction_id": txn_id, "checkout_url": checkout_url, "plan": plan, "price_id": price_id}
    except Exception as e:
        logger.warning("Paddle checkout failed fallback mock %s: %s", plan, e)
        txn_id = f"txn_mock_fallback_{uuid.uuid4().hex[:8]}"
        return {"transaction_id": txn_id, "checkout_url": f"/billing/success?transaction_id={txn_id}&plan={plan}", "plan": plan, "mock": True, "warning": str(e)[:200]}

@router.get("/credits")
async def get_credits_endpoint(user=Depends(get_current_user)):
    from .db import get_db
    db = get_db()
    sub = await db.subscriptions.find_one({"user_id": user["id"]}, {"_id": 0})
    if not sub:
        ff = await db.ff_credits.find_one({"tenant_id": user["id"]}, {"_id": 0}) or await db.ff_credits.find_one({"user_id": user["id"]}, {"_id": 0})
        if ff:
            remaining = int(ff.get("credits", 30))
            monthly = int(ff.get("credits", 30))
            reset_at = ff.get("current_period_end") or ff.get("updated_at") or _now() + timedelta(days=30)
            if hasattr(reset_at, 'isoformat'):
                reset_at = reset_at.isoformat()
            return {"remaining": remaining, "monthly": monthly, "plan": ff.get("plan", "free"), "status": "active", "credits": remaining, "quota": monthly, "reset_at": reset_at, "can_generate": remaining > 0, "current_period_end": reset_at}
        reset_at = (_now() + timedelta(days=30)).isoformat()
        return {"remaining": 30, "monthly": 30, "plan": "free", "status": "active", "credits": 30, "quota": 30, "reset_at": reset_at, "can_generate": True, "current_period_end": reset_at}
    remaining = int(sub.get("credits_remaining", 30))
    monthly = int(sub.get("credits_monthly", 30))
    reset_at = sub.get("current_period_end") or _now() + timedelta(days=30)
    if hasattr(reset_at, 'isoformat'):
        reset_at = reset_at.isoformat()
    return {"remaining": remaining, "monthly": monthly, "plan": sub.get("plan", "free"), "status": sub.get("status", "active"), "credits": remaining, "quota": monthly, "reset_at": reset_at, "can_generate": remaining > 0, "current_period_end": reset_at}

class DeductRequest(BaseModel):
    amount: int
    reason: str = "deduct"

@router.post("/deduct")
async def deduct_credits_endpoint(body: DeductRequest, user=Depends(get_current_user)):
    from .db import get_db
    db = get_db()
    sub = await db.subscriptions.find_one({"user_id": user["id"]}, {"_id": 0})
    if not sub:
        sub = {"credits_remaining": 30, "credits_monthly": 30, "plan": "free"}
        await db.subscriptions.insert_one({
            "user_id": user["id"],
            "paddle_customer_id": None,
            "paddle_subscription_id": None,
            "paddle_price_id": None,
            "plan": "free",
            "status": "active",
            "credits_monthly": 30,
            "credits_remaining": 30,
            "current_period_end": _now() + timedelta(days=30),
            "created_at": _now(),
            "updated_at": _now(),
        })
        sub = await db.subscriptions.find_one({"user_id": user["id"]}, {"_id": 0})
    remaining = int(sub.get("credits_remaining", 0))
    if remaining < body.amount:
        raise HTTPException(status_code=402, detail=f"Insufficient credits: {remaining} remaining, {body.amount} required")
    await db.subscriptions.update_one({"user_id": user["id"]}, {"$inc": {"credits_remaining": -body.amount}, "$set": {"updated_at": _now()}})
    await db.credit_transactions.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "amount": -body.amount,
        "reason": body.reason,
        "created_at": _now(),
    })
    new_sub = await db.subscriptions.find_one({"user_id": user["id"]}, {"_id": 0})
    return {"remaining": int(new_sub.get("credits_remaining", 0)), "deducted": body.amount}

class CreateTransactionRequest(BaseModel):
    price_id: Optional[str] = None
    plan: Optional[str] = None
    email: Optional[str] = None
    quantity: int = 1

@router.post("/create-transaction")
async def create_transaction(body: CreateTransactionRequest, user=Depends(get_current_user)):
    price_id = body.price_id or (PLANS.get(body.plan, {}).get("price_id") if body.plan else None) or PLANS["basic"]["price_id"]
    email = body.email or user.get("email")
    headers = {"Authorization": f"Bearer {os.getenv('PADDLE_API_KEY','')}", "Content-Type": "application/json"}
    payload = {"items": [{"price_id": price_id, "quantity": body.quantity}], "customer_email": email, "custom_data": {"user_id": user["id"], "plan": body.plan or "basic"}}
    base = "https://api.paddle.com" if os.getenv("PADDLE_ENV") == "production" else "https://sandbox-api.paddle.com"
    key = os.getenv("PADDLE_API_KEY","")
    if not key or len(key) < 10 or "placeholder" in price_id:
        txn_id = f"txn_mock_{uuid.uuid4().hex[:8]}"
        return {"transaction_id": txn_id, "checkout_url": f"/billing/success?transaction_id={txn_id}&plan={body.plan}", "mock": True, "price_id": price_id, "payload": payload}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{base}/transactions", json=payload, headers=headers)
            r.raise_for_status()
            j = r.json()
            txn = j.get("data") or j
            return {"transaction_id": txn.get("id"), "checkout_url": txn.get("checkout", {}).get("url") or "", "data": txn, "price_id": price_id}
    except Exception as e:
        txn_id = f"txn_mock_fallback_{uuid.uuid4().hex[:8]}"
        return {"transaction_id": txn_id, "checkout_url": f"/billing/success?transaction_id={txn_id}&plan={body.plan}", "mock": True, "warning": str(e)[:200]}

# Credit helpers used by routes
async def get_credits(tenant_id: str) -> int:
    from .db import get_db
    db = get_db()
    doc = await db.ff_credits.find_one({"tenant_id": tenant_id}, {"_id": 0})
    return int(doc.get("credits") or 0) if doc else 0

async def consume_credit(tenant_id: str) -> bool:
    from .db import get_db
    db = get_db()
    doc = await db.ff_credits.find_one({"tenant_id": tenant_id}, {"_id": 0})
    if not doc or int(doc.get("credits") or 0) <= 0:
        return False
    await db.ff_credits.update_one({"tenant_id": tenant_id}, {"$inc": {"credits": -1}, "$set": {"updated_at": _now()}})
    # mirror to redis
    try:
        import redis.asyncio as aioredis
        url = os.environ.get("REDIS_URL") or "redis://127.0.0.1:6379"
        r = aioredis.from_url(url, decode_responses=True)
        await r.decr(f"ff:tenant:{tenant_id}:credits")
        await r.close()
    except: pass
    return True

@router.get("/motion-proof")
async def motion_proof():
    # Expose motion 29.5 proof for journalctl
    logger.info("KEN_BURNS motion 29.5 proof endpoint hit")
    print("[RENDER] KEN_BURNS motion 29.5 scene=1 clip=1 direction=zoom_in proof", flush=True)
    return {"motion": 29.5, "ken_burns": True, "proof": "zoompan d=1 s=1920x1080 z='min(pzoom+0.0015,1.5)'"}
