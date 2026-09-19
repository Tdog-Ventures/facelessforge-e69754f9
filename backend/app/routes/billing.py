from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
import os
from ..auth import get_current_user
from ..revenue_loop import create_prospect, check_activation, admin_approve, list_review_queue, get_bank_instructions, get_mongo

billing_router = APIRouter()

class CreateTransferRequest(BaseModel):
    email: Optional[str] = None
    amount_cents: Optional[int] = None
    plan: Optional[str] = "pro"

class ApproveRequest(BaseModel):
    reference: str

@billing_router.post("/create-bank-transfer")
async def create_bank_transfer(body: CreateTransferRequest, user=Depends(get_current_user)):
    email = (body.email or user.get("email") or user.get("id")).lower()
    result = await create_prospect(email, body.amount_cents)
    return {"ok": True, "email": result["email"], "reference": result["reference"], "amount_cents": result["amount_cents"], "amount_display": result["instructions"]["amount_display"], "instructions": result["instructions"], "status": result["status"], "next_steps": "Transfer exact amount with exact reference. Then click 'I've Paid'."}

@billing_router.get("/instructions")
async def get_instructions(reference: str, user=Depends(get_current_user)):
    db = get_mongo()
    rec = db.billing.find_one({"reference": reference})
    if not rec: raise HTTPException(status_code=404, detail="Reference not found")
    if rec["email"] != user["email"] and user.get("role") != "admin": raise HTTPException(status_code=403, detail="Not your reference")
    return get_bank_instructions(reference, rec["amount_cents"])

@billing_router.get("/status")
async def billing_status(reference: Optional[str] = None, email: Optional[str] = None, user=Depends(get_current_user)):
    check_email = email or user["email"]
    if check_email != user["email"] and user.get("role") != "admin": raise HTTPException(status_code=403, detail="Forbidden")
    result = await check_activation(email=check_email, reference=reference)
    return result

@billing_router.post("/ive-paid")
async def ive_paid(reference: str, user=Depends(get_current_user)):
    result = await check_activation(reference=reference)
    if result["active"]:
        return {"ok": True, "active": True, "message": "Payment confirmed - Pro activated"}
    else:
        return {"ok": True, "active": False, "message": "Not yet reconciled. CSV import runs every hour.", "status": result.get("status")}

@billing_router.get("/me")
async def my_billing(user=Depends(get_current_user)):
    db = get_mongo()
    recs = list(db.billing.find({"email": user["email"]}).sort("created_at", -1).limit(5))
    for r in recs: r["_id"] = str(r["_id"])
    return {"email": user["email"], "billing": recs, "subscription_status": user.get("subscription_status", "free")}

@billing_router.post("/admin/approve")
async def approve_transfer(body: ApproveRequest, user=Depends(get_current_user)):
    if user.get("role") != "admin": raise HTTPException(status_code=403, detail="Admin only")
    result = await admin_approve(body.reference, user["email"])
    return result

@billing_router.get("/admin/review-queue")
async def review_queue(user=Depends(get_current_user)):
    if user.get("role") != "admin": raise HTTPException(status_code=403, detail="Admin only")
    items = await list_review_queue()
    for it in items: it["_id"] = str(it["_id"])
    return {"queue": items, "count": len(items)}

@billing_router.get("/admin/all")
async def all_billing(user=Depends(get_current_user)):
    if user.get("role") != "admin": raise HTTPException(status_code=403, detail="Admin only")
    db = get_mongo()
    all_rec = list(db.billing.find({}).sort("created_at", -1).limit(100))
    for r in all_rec: r["_id"] = str(r["_id"])
    return {"billing": all_rec}
