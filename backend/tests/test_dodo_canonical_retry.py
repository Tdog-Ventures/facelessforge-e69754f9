"""Canonical grant parameters and tenant binding on pending retries.

The durable fulfillment record is created with $setOnInsert by the first
delivery. A redelivery must resume with those stored values, not with whatever
the new payload claims, or mutated metadata could re-route credits to another
tenant/product or change the amount. A different tenant is rejected outright:
the first durable record fixes the tenant contract.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import billing_dodo


class FakeCursor:
    """Minimal $setOnInsert / $inc / $set semantics for the fulfillment doc."""

    def __init__(self, stored):
        self.doc = dict(stored) if stored else None
        self.updates = []

    async def update_one(self, query, update, upsert=False):
        self.updates.append((query, update, upsert))
        if self.doc is None:
            if upsert:
                self.doc = dict(update.get("$setOnInsert") or {})
                self.doc.update(update.get("$set") or {})
        else:
            self.doc.update(update.get("$set") or {})
            for key, value in (update.get("$inc") or {}).items():
                self.doc[key] = self.doc.get(key, 0) + value
        return SimpleNamespace(upserted_id="upserted")

    async def find_one(self, query, projection=None):
        return dict(self.doc) if self.doc else None


class FakeDb:
    def __init__(self, stored):
        self.ff_dodo_fulfillments = FakeCursor(stored)


def _run(stored, **payload):
    db = FakeDb(stored)
    kwargs = {
        "tenant_id": "tenant-payload",
        "product_id": "prod-payload",
        "credits": 999,
        "payment_id": "pay-1",
        "event_type": "payment.succeeded",
        "customer_email": "someone@example.com",
        "webhook_id": "wh-1",
    }
    kwargs.update(payload)
    with patch("app.db.get_db", return_value=db), \
            patch.object(billing_dodo, "_apply_redis_grant") as redis_grant, \
            patch.object(billing_dodo, "_mirror_grant_to_mongo", new=AsyncMock()) as mirror:
        result = asyncio.run(billing_dodo._fulfill_dodo_grant(**kwargs))
    return result, redis_grant, mirror, db


def test_pending_retry_uses_stored_canonical_parameters():
    stored = {
        "_id": "pay-1",
        "tenant_id": "tenant-stored",
        "product_id": "prod-stored",
        "credits": 150,
        "event_type": "payment.succeeded",
        "customer_email": "stored@example.com",
        "status": "pending",
    }
    result, redis_grant, mirror, _ = _run(
        stored,
        tenant_id="tenant-stored",
        product_id="prod-payload",
        credits=999,
        customer_email="someone@example.com",
    )

    # Redis grant went to the stored tenant with the stored amount.
    assert redis_grant.call_args.args[1] == "tenant-stored"
    assert redis_grant.call_args.args[3] == 150
    # Mongo mirror used the stored canonical values too.
    assert mirror.call_args.kwargs["tenant_id"] == "tenant-stored"
    assert mirror.call_args.kwargs["product_id"] == "prod-stored"
    assert mirror.call_args.kwargs["credits"] == 150
    assert mirror.call_args.kwargs["customer_email"] == "stored@example.com"
    assert result["tenant_id"] == "tenant-stored"
    assert result["credits_granted"] == 150


def test_pending_retry_rejects_a_different_tenant():
    stored = {
        "_id": "pay-1",
        "tenant_id": "tenant-stored",
        "product_id": "prod-stored",
        "credits": 150,
        "event_type": "payment.succeeded",
        "status": "pending",
    }
    with pytest.raises(ValueError, match="tenant does not match"):
        _run(stored, tenant_id="tenant-payload")


def test_redis_granted_retry_still_skips_redis_and_uses_stored_values():
    stored = {
        "_id": "pay-1",
        "tenant_id": "tenant-stored",
        "product_id": "prod-stored",
        "credits": 150,
        "event_type": "payment.succeeded",
        "status": "redis_granted",
    }
    _result, redis_grant, mirror, _ = _run(stored, tenant_id="tenant-stored")
    redis_grant.assert_not_called()
    assert mirror.call_args.kwargs["tenant_id"] == "tenant-stored"
    assert mirror.call_args.kwargs["credits"] == 150


def test_completed_retry_is_a_no_op():
    stored = {
        "_id": "pay-1",
        "tenant_id": "tenant-stored",
        "product_id": "prod-stored",
        "credits": 150,
        "event_type": "payment.succeeded",
        "status": "completed",
    }
    result, redis_grant, mirror, _ = _run(stored, tenant_id="tenant-stored")
    redis_grant.assert_not_called()
    mirror.assert_not_called()
    # The candidate returns a distinct marker for an already-acknowledged payment.
    assert result["status"] == "already_fulfilled"


def test_first_delivery_keeps_payload_values():
    result, redis_grant, mirror, db = _run(None)
    assert redis_grant.call_args.args[1] == "tenant-payload"
    assert redis_grant.call_args.args[3] == 999
    assert mirror.call_args.kwargs["tenant_id"] == "tenant-payload"
    assert result["credits_granted"] == 999
    # The first durable record pins the full contract, including the email.
    assert db.ff_dodo_fulfillments.doc["customer_email"] == "someone@example.com"
    assert db.ff_dodo_fulfillments.doc["product_id"] == "prod-payload"
