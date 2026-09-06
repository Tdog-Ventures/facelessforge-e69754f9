"""
ethinx-sovereign-revenue-loop adapter for FacelessForge
Location: /opt/facelessforge/deploy/backend/app/revenue_loop.py
"""
import logging
import os, secrets, string
from datetime import datetime, timezone
import pymongo
import httpx
from typing import Optional, Dict, Any

try:
    from rapidfuzz import fuzz
except ImportError:
    fuzz = None  # type: ignore

logger = logging.getLogger("facelessforge.revenue_loop")

def fuzzy_reference_score(expected: str, received: str) -> float:
    """Return fuzz.ratio score 0-100. Uses rapidfuzz if available else exact match fallback."""
    if not expected or not received:
        return 0.0
    exp = expected.strip().upper()
    rec = received.strip().upper()
    if exp == rec:
        return 100.0
    if fuzz is None:
        return 100.0 if exp == rec else 0.0
    # Use fuzz.ratio on the reference vs description substring scoring via partial
    # For bank descriptions longer than reference, use partial_ratio as well and take max
    try:
        r = fuzz.ratio(exp, rec)
        # also try token-level partial for embedded reference
        pr = fuzz.partial_ratio(exp, rec)
        return max(float(r), float(pr))
    except Exception:
        return float(fuzz.ratio(exp, rec))

def fuzzy_match_decision(score: float) -> str:
    if score >= 85:
        return "auto_approve"
    if score >= 75:
        return "review_required"
    return "reject"

def find_best_fuzzy_match(received: str, candidates: list[str]) -> tuple[Optional[str], float]:
    best = None
    best_score = 0.0
    for cand in candidates:
        s = fuzzy_reference_score(cand, received)
        if s > best_score:
            best_score = s
            best = cand
    return best, best_score

SOVEREIGN_URL = os.getenv("SOVEREIGN_LOOP_URL", "http://127.0.0.1:8099").rstrip("/")
SOVEREIGN_TOKEN = os.getenv("SOVEREIGN_LOOP_TOKEN", "")
BANK_NAME = os.getenv("BANK_NAME", "ANZ")
BANK_BSB = os.getenv("BANK_BSB", "012-XXX")
BANK_ACCOUNT = os.getenv("BANK_ACCOUNT", "XXXXXXX")
BANK_ACCOUNT_NAME = os.getenv("BANK_ACCOUNT_NAME", "Ethinx Solutions")
PLAN_AMOUNT_CENTS = int(os.getenv("PLAN_AMOUNT_CENTS", "4900"))
MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://127.0.0.1:27017/facelessforge"

def get_mongo():
    c = pymongo.MongoClient(MONGO_URI)
    try:
        return c.facelessforge
    except:
        return c.get_database("facelessforge")

def generate_reference(email: str) -> str:
    raw = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    checksum = str(sum(ord(ch) for ch in raw) % 10)
    return f"FF-{raw}-{checksum}"

def get_bank_instructions(reference: str, amount_cents: int) -> Dict[str, Any]:
    dollars = amount_cents / 100
    return {
        "bank_name": BANK_NAME,
        "bsb": BANK_BSB,
        "account_number": BANK_ACCOUNT,
        "account_name": BANK_ACCOUNT_NAME,
        "reference": reference,
        "amount_cents": amount_cents,
        "amount_display": f"${dollars:.2f} AUD",
        "instructions": f"Transfer {dollars:.2f} AUD to BSB {BANK_BSB} Account {BANK_ACCOUNT} ({BANK_ACCOUNT_NAME}) with reference {reference}.",
        "warning": "Only exact amount + exact reference activates."
    }

async def sovereign_call(method: str, path: str, json: Optional[dict] = None) -> Optional[dict]:
    if not SOVEREIGN_URL:
        return None
    url = f"{SOVEREIGN_URL}{path}"
    headers = {}
    if SOVEREIGN_TOKEN:
        headers["Authorization"] = f"Bearer {SOVEREIGN_TOKEN}"
        headers["X-Admin-Token"] = SOVEREIGN_TOKEN
    headers["Origin"] = os.getenv("SOVEREIGN_TRUSTED_ORIGIN", "https://facelessforge.ethinx.solutions")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.request(method, url, json=json, headers=headers)
            if r.status_code == 200:
                return r.json()
            return None
    except Exception:
        return None

async def create_prospect(email: str, amount_cents: Optional[int] = None) -> Dict[str, Any]:
    amount_cents = amount_cents or PLAN_AMOUNT_CENTS
    db = get_mongo()
    existing = db.billing.find_one({"email": email.lower(), "status": {"$in": ["active", "pending"]}})
    if existing:
        ref = existing["reference"]
        return {"email": email, "reference": ref, "amount_cents": existing["amount_cents"], "status": existing["status"], "instructions": get_bank_instructions(ref, existing["amount_cents"]), "source": "local-existing"}
    reference = generate_reference(email.lower())
    sovereign_result = await sovereign_call("POST", "/api/prospects", json={"email": email.lower(), "reference": reference, "amount_cents": amount_cents, "source": "facelessforge"})
    if not sovereign_result:
        sovereign_result = await sovereign_call("POST", "/api/apply", json={"email": email.lower(), "reference": reference, "amount_cents": amount_cents})
    now = datetime.now(timezone.utc)
    doc = {"email": email.lower(), "reference": reference, "amount_cents": amount_cents, "status": "pending", "sovereign_id": sovereign_result.get("id") if sovereign_result else None, "created_at": now, "updated_at": now, "activations": [], "review_notes": []}
    db.billing.insert_one(doc)
    return {"email": email.lower(), "reference": reference, "amount_cents": amount_cents, "status": "pending", "instructions": get_bank_instructions(reference, amount_cents), "source": "sovereign" if sovereign_result else "local"}

async def check_activation(email: str = None, reference: str = None) -> Dict[str, Any]:
    db = get_mongo()
    query = {}
    if email: query["email"] = email.lower()
    if reference: query["reference"] = reference
    if not query: return {"active": False, "reason": "no email or reference"}
    record = db.billing.find_one(query, sort=[("created_at", -1)])
    # Fuzzy fallback if exact reference not found
    if not record and reference:
        all_refs = list(db.billing.find({}, {"reference": 1, "_id": 0}))
        candidates = [r["reference"] for r in all_refs if r.get("reference")]
        best, score = find_best_fuzzy_match(reference, candidates)
        logger.info(f"FUZZY MATCH: expected={best} got={reference} score={score:.1f}")
        if best and score >= 85:
            record = db.billing.find_one({"reference": best}, sort=[("created_at", -1)])
        elif best and 75 <= score < 85:
            rec = db.billing.find_one({"reference": best})
            if rec:
                db.billing.update_one({"_id": rec["_id"]}, {"$set": {"status": "review", "updated_at": datetime.now(timezone.utc)}, "$push": {"review_notes": f"FUZZY REVIEW: expected={best} got={reference} score={score:.1f}"}})
            return {"active": False, "reason": "review_required", "score": score, "expected": best, "received": reference}
        elif best:
            logger.info(f"FUZZY REJECT: expected={best} got={reference} score={score:.1f} (<75)")
    if not record: return {"active": False, "reason": "no billing record"}
    sovereign_status = None
    if record.get("reference"):
        sovereign_status = await sovereign_call("GET", f"/api/prospects/{record['reference']}/status")
        if not sovereign_status:
            sovereign_status = await sovereign_call("GET", f"/api/billing/status?reference={record['reference']}")
    if sovereign_status and sovereign_status.get("status") == "active":
        if record["status"] != "active":
            db.billing.update_one({"_id": record["_id"]}, {"$set": {"status": "active", "updated_at": datetime.now(timezone.utc)}})
            db.users.update_one({"email": record["email"]}, {"$set": {"subscription_status": "active", "plan": "pro"}})
        return {"active": True, "reference": record["reference"], "source": "sovereign", "record": record}
    is_active = record["status"] == "active"
    return {"active": is_active, "reference": record["reference"], "status": record["status"], "source": "local", "record": record}

async def admin_approve(reference: str, approver_email: str) -> Dict[str, Any]:
    db = get_mongo()
    now = datetime.now(timezone.utc)
    record = db.billing.find_one({"reference": reference})
    if not record:
        # fuzzy fallback for typo'd reference
        all_refs = list(db.billing.find({}, {"reference": 1}))
        candidates = [r["reference"] for r in all_refs if r.get("reference")]
        best, score = find_best_fuzzy_match(reference, candidates)
        logger.info(f"FUZZY MATCH: expected={best} got={reference} score={score:.1f}")
        if best and score >= 75 and score < 85:
            rec = db.billing.find_one({"reference": best})
            if rec:
                db.billing.update_one({"_id": rec["_id"]}, {"$set": {"status": "review", "updated_at": now}, "$push": {"review_notes": f"FUZZY REVIEW: expected={best} got={reference} score={score:.1f}"}})
                return {"ok": False, "reason": "review_required", "score": score, "expected": best}
        if best and score >= 85:
            record = db.billing.find_one({"reference": best})
            logger.info(f"FUZZY MATCH: expected={best} got={reference} score={score:.1f} -> auto-approving {best}")
        else:
            if best:
                logger.info(f"FUZZY REJECT: expected={best} got={reference} score={score:.1f}")
            return {"ok": False, "reason": "reference not found"}
    db.billing.update_one({"reference": reference}, {"$set": {"status": "active", "updated_at": now, "approved_by": approver_email, "approved_at": now}, "$push": {"activations": {"at": now, "by": approver_email, "method": "manual_exact_match"}}})
    db.users.update_one({"email": record["email"]}, {"$set": {"subscription_status": "active", "plan": "pro", "activated_at": now}})
    await sovereign_call("POST", f"/api/prospects/{reference}/activate", json={"by": approver_email, "method": "manual"})
    return {"ok": True, "email": record["email"], "reference": reference}

async def list_review_queue() -> list:
    db = get_mongo()
    return list(db.billing.find({"status": "review"}).sort("created_at", -1).limit(100))
