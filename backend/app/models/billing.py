"""Billing models - Paddle subscription & credit transactions."""
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field
import uuid


def _now():
    return datetime.now(timezone.utc)


class Subscription(BaseModel):
    user_id: str  # PK
    paddle_customer_id: Optional[str] = None
    paddle_subscription_id: Optional[str] = None
    paddle_price_id: Optional[str] = None
    plan: str = Field(default="free", description="free/basic/pro/advanced")
    status: str = Field(default="active", description="active/canceled/past_due/inactive")
    credits_monthly: int = 30
    credits_remaining: int = 30
    current_period_end: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    class Config:
        extra = "allow"


class CreditTransaction(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    amount: int  # positive credit, negative debit
    reason: str = Field(description="e.g. purchase basic, auto-attach scene 02, monthly reset")
    created_at: datetime = Field(default_factory=_now)

    class Config:
        extra = "allow"
