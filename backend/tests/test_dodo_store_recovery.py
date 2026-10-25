"""Exercise partial fulfillment with disposable real Mongo and Redis stores."""
import os
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from motor.motor_asyncio import AsyncIOMotorClient
import pytest

from app import billing_dodo
from .test_dodo_redis_integration import isolated_redis


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("DODO_TEST_MONGO_URL"), reason="requires disposable Mongo URL")
async def test_real_stores_recover_partial_mirror_without_regranting(isolated_redis):
    client = AsyncIOMotorClient(os.environ["DODO_TEST_MONGO_URL"], serverSelectionTimeoutMS=1000)
    db = client["ff_dodo_recovery_test_" + uuid.uuid4().hex]
    try:
        await db.users.insert_one({"id": "user", "email": "test@example.invalid"})
        real_subscriptions = db.subscriptions
        fail_once = True

        async def interrupted_subscription_update(*args, **kwargs):
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise RuntimeError("interrupted before second account update")
            return await real_subscriptions.update_one(*args, **kwargs)

        database = SimpleNamespace(
            users=db.users, ff_credits=db.ff_credits,
            subscriptions=SimpleNamespace(create_index=real_subscriptions.create_index,
                                          update_one=interrupted_subscription_update),
            ff_dodo_fulfillments=db.ff_dodo_fulfillments,
            ff_billing_events=db.ff_billing_events,
        )

        async def fulfill(credits):
            return await billing_dodo._fulfill_dodo_grant(
                tenant_id="user", product_id="pdt_starter", credits=credits,
                payment_id="partial-payment", event_type="payment.succeeded",
                customer_email="test@example.invalid", webhook_id="delivery",
            )

        with patch("app.db.get_db", return_value=database), patch.object(billing_dodo, "r", isolated_redis):
            with pytest.raises(RuntimeError, match="interrupted"):
                await fulfill(100)
            assert isolated_redis.hget("tenant:user", "credits") == "100"
            assert (await db.ff_credits.find_one({"tenant_id": "user"}))["credits"] == 100
            assert await real_subscriptions.find_one({"user_id": "user"}) is None
            assert (await db.ff_dodo_fulfillments.find_one({"_id": "partial-payment"}))["status"] == "pending"
            assert (await fulfill(500))["credits_granted"] == 100
            assert (await fulfill(500))["status"] == "already_fulfilled"

        assert isolated_redis.hget("tenant:user", "credits") == "100"
        assert (await db.ff_credits.find_one({"tenant_id": "user"}))["credits"] == 100
        assert (await real_subscriptions.find_one({"user_id": "user"}))["credits_remaining"] == 100
        assert await db.ff_billing_events.count_documents({}) == 1
        assert (await db.ff_dodo_fulfillments.find_one({"_id": "partial-payment"}))["status"] == "completed"
    finally:
        await client.drop_database(db.name)
        client.close()
