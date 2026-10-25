"""Recovery must use the durable payment contract, not retry-time resolution."""
from unittest.mock import patch

import pytest

from app import billing_dodo
from test_dodo_fulfillment import FakeRedis, _database


def pending_database(status="pending"):
    db = _database()
    db.ff_dodo_fulfillments.docs.append({
        "_id": "payment", "payment_id": "payment", "tenant_id": "user",
        "product_id": "pdt_starter", "credits": 100,
        "event_type": "payment.succeeded", "status": status,
        "customer_email": "test@example.invalid", "attempts": 0,
    })
    return db


async def retry(db, redis, *, tenant="user", product="pdt_creator", credits=500):
    with patch("app.db.get_db", return_value=db), patch.object(billing_dodo, "r", redis):
        return await billing_dodo._fulfill_dodo_grant(
            tenant_id=tenant, product_id=product, credits=credits,
            payment_id="payment", event_type="payment.succeeded",
            customer_email="changed@example.invalid", webhook_id="retry",
        )


@pytest.mark.asyncio
async def test_pending_retry_uses_original_product_and_credits():
    db, redis = pending_database(), FakeRedis()
    result = await retry(db, redis)
    assert redis.hash["tenant:user"]["credits"] == "100"
    assert db.ff_credits.docs[0]["credits"] == 100
    assert db.subscriptions.docs[0]["credits_remaining"] == 100
    assert db.ff_credits.docs[0]["plan"] == "starter"
    assert result["credits_granted"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "completed"])
async def test_payment_cannot_be_reassigned_to_another_tenant(status):
    db, redis = pending_database(status), FakeRedis()
    with pytest.raises(ValueError, match="tenant"):
        await retry(db, redis, tenant="another-user")
    assert redis.hash == {}
    assert db.ff_credits.docs == []
