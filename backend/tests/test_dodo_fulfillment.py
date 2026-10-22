"""Durable Dodo fulfillment: crash recovery, retries, and exactly-once grants.

These tests verify the billing boundary that sits *after* webhook signature
verification and *before* acknowledgement. All payment/provider calls are
mocked; no production integration suite runs here.
"""
import asyncio
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import pytest
from starlette.requests import Request

from app import billing_dodo


class FakeRedis:
    """In-memory Redis sufficient for the Dodo fulfillment path."""

    def __init__(self):
        self.hash = {}
        self.strings = {}
        self.sets = {}
        self._eval_hook = None

    def eval(self, script, numkeys, *args):
        if self._eval_hook:
            self._eval_hook()
        fulfillment_key = args[0]
        account_key = args[1]
        payment_id = args[2]
        credits = int(args[3])
        now_iso = args[4]
        h = self.hash.setdefault(fulfillment_key, {})
        if payment_id in h:
            return 0
        h[payment_id] = str(credits)
        acc = self.hash.setdefault(account_key, {})
        acc["credits"] = str(int(acc.get("credits", 0)) + credits)
        acc["status"] = "ACTIVE"
        acc["last_payment"] = now_iso
        return 1

    def hgetall(self, key):
        return dict(self.hash.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        h = self.hash.setdefault(key, {})
        if mapping:
            h.update({k: str(v) for k, v in mapping.items()})
        return len(mapping or {})

    def hincrby(self, key, field, amount):
        h = self.hash.setdefault(key, {})
        h[field] = str(int(h.get(field, 0)) + amount)
        return int(h[field])

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)
        return 1

    def delete(self, *keys):
        for key in keys:
            self.hash.pop(key, None)
            self.strings.pop(key, None)
        return len(keys)

    def exists(self, *keys):
        return sum(1 for k in keys if k in self.hash or k in self.strings)

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def hincrby(self, key, field, amount):
        self.ops.append(("hincrby", key, field, amount))
        return self

    def hset(self, key, mapping=None, **kwargs):
        self.ops.append(("hset", key, mapping or kwargs))
        return self

    def sadd(self, key, member):
        self.ops.append(("sadd", key, member))
        return self

    def expire(self, key, seconds):
        self.ops.append(("expire", key, seconds))
        return self

    def lpush(self, key, value):
        self.ops.append(("lpush", key, value))
        return self

    def ltrim(self, key, start, end):
        self.ops.append(("ltrim", key, start, end))
        return self

    def execute(self):
        for op in self.ops:
            name = op[0]
            if name == "hincrby":
                self.redis.hincrby(op[1], op[2], op[3])
            elif name == "hset":
                self.redis.hset(op[1], op[2])
            elif name == "sadd":
                self.redis.sadd(op[1], op[2])
            elif name == "lpush":
                lst = self.redis.hash.setdefault(op[1], [])
                if not isinstance(lst, list):
                    self.redis.hash[op[1]] = [lst]
                self.redis.hash[op[1]].insert(0, op[2])
            elif name == "ltrim":
                lst = self.redis.hash.get(op[1], [])
                if isinstance(lst, list):
                    self.redis.hash[op[1]] = lst[op[2] : op[3] + 1]
        return [None] * len(self.ops)


class Collection:
    def __init__(self, key, initial=None):
        self.key = key
        self.docs = deepcopy(initial or [])
        self.unique = False
        self._next_raise = None

    async def create_index(self, *args, **kwargs):
        self.unique = kwargs.get("unique", False)

    def matches(self, doc, query):
        for key, value in query.items():
            if isinstance(value, dict) and "$ne" in value:
                if value["$ne"] in doc.get(key, []):
                    return False
            elif isinstance(value, dict) and "$exists" in value:
                if value["$exists"] != (key in doc):
                    return False
            elif doc.get(key) != value:
                return False
        return True

    async def find_one(self, query, projection=None):
        if self._next_raise:
            raise self._next_raise
        return next((deepcopy(d) for d in self.docs if self.matches(d, query)), None)

    async def insert_one(self, doc):
        if self._next_raise:
            raise self._next_raise
        if self.unique and any(d.get(self.key) == doc.get(self.key) for d in self.docs):
            from pymongo.errors import DuplicateKeyError
            raise DuplicateKeyError("duplicate")
        self.docs.append(deepcopy(doc))

    async def update_one(self, query, update, upsert=False):
        if self._next_raise:
            raise self._next_raise
        doc = next((d for d in self.docs if self.matches(d, query)), None)
        inserted = doc is None
        if inserted:
            if not upsert:
                return SimpleNamespace(modified_count=0)
            doc = {k: v for k, v in query.items() if not isinstance(v, dict)}
            if self.unique and any(d.get(self.key) == doc.get(self.key) for d in self.docs):
                from pymongo.errors import DuplicateKeyError
                raise DuplicateKeyError("duplicate")
            self.docs.append(doc)
            doc.update(deepcopy(update.get("$setOnInsert", {})))
        doc.update(deepcopy(update.get("$set", {})))
        for key, value in update.get("$inc", {}).items():
            doc[key] = doc.get(key, 0) + value
        for key, value in update.get("$addToSet", {}).items():
            values = doc.setdefault(key, [])
            if value not in values:
                values.append(value)
        return SimpleNamespace(modified_count=1)


def _database():
    return SimpleNamespace(
        users=SimpleNamespace(find_one=AsyncMock(return_value={"id": "user", "email": "test@example.invalid"})),
        ff_credits=Collection("tenant_id"),
        subscriptions=Collection("user_id"),
        ff_billing_events=Collection("_id"),
        ff_dodo_fulfillments=Collection("_id"),
    )


def _payment_payload(payment_id="pay_1", tenant_id="user", product_id="pdt_starter", session_id=None):
    data = {
        "payment_id": payment_id,
        "product_id": product_id,
        "custom_data": {"tenant_id": tenant_id},
        "customer": {"email": "test@example.invalid"},
    }
    if session_id:
        data["session_id"] = session_id
    return {"type": "payment.succeeded", "data": data}


async def _post_webhook(payload, fake_redis, db, webhook_id="wh_1"):
    body = json.dumps(payload).encode()
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    request = Request({"type": "http", "headers": []}, receive)
    with patch.dict("os.environ", {"DODO_PAYMENTS_WEBHOOK_KEY": "test"}), \
         patch("app.dodo_webhook_verify.verify_signature"), \
         patch.object(billing_dodo, "r", fake_redis), \
         patch("app.db.get_db", return_value=db):
        return await billing_dodo.dodo_webhook(
            request,
            webhook_id=webhook_id,
            webhook_timestamp="1",
            webhook_signature="sig",
        )


@pytest.mark.asyncio
async def test_payment_succeeded_grants_credits_once():
    fake_redis = FakeRedis()
    db = _database()
    payload = _payment_payload()
    result = await _post_webhook(payload, fake_redis, db)
    assert result["status"] == "ACTIVE"
    assert result["credits_granted"] == 100  # pdt_starter default
    assert fake_redis.hash["tenant:user"]["credits"] == "100"
    assert db.ff_dodo_fulfillments.docs[0]["status"] == "completed"
    assert db.subscriptions.docs[0]["credits_remaining"] == 100


@pytest.mark.asyncio
async def test_subscription_active_without_payment_is_ignored():
    fake_redis = FakeRedis()
    db = _database()
    payload = {"type": "subscription.active", "data": {"subscription_id": "sub_1", "custom_data": {"tenant_id": "user"}}}
    result = await _post_webhook(payload, fake_redis, db)
    assert result["status"] == "ignored"
    assert "tenant:user" not in fake_redis.hash
    assert db.ff_dodo_fulfillments.docs == []


@pytest.mark.asyncio
async def test_duplicate_webhook_id_grants_once():
    fake_redis = FakeRedis()
    db = _database()
    payload = _payment_payload()
    r1 = await _post_webhook(payload, fake_redis, db, webhook_id="wh_1")
    r2 = await _post_webhook(payload, fake_redis, db, webhook_id="wh_1")
    assert r1["status"] == "ACTIVE"
    assert r2["status"] == "already_fulfilled"
    assert fake_redis.hash["tenant:user"]["credits"] == "100"
    assert db.ff_billing_events.docs[0]["credits_added"] == 100


@pytest.mark.asyncio
async def test_different_webhook_ids_same_payment_grants_once():
    fake_redis = FakeRedis()
    db = _database()
    payload = _payment_payload(payment_id="pay_shared")
    r1 = await _post_webhook(payload, fake_redis, db, webhook_id="wh_A")
    r2 = await _post_webhook(payload, fake_redis, db, webhook_id="wh_B")
    assert r1["status"] == "ACTIVE"
    assert r2["status"] == "already_fulfilled"
    assert fake_redis.hash["tenant:user"]["credits"] == "100"
    assert len(db.ff_billing_events.docs) == 1


@pytest.mark.asyncio
async def test_separate_payments_both_grant():
    fake_redis = FakeRedis()
    db = _database()
    r1 = await _post_webhook(_payment_payload(payment_id="pay_1"), fake_redis, db)
    r2 = await _post_webhook(_payment_payload(payment_id="pay_2"), fake_redis, db)
    assert r1["status"] == "ACTIVE"
    assert r2["status"] == "ACTIVE"
    assert fake_redis.hash["tenant:user"]["credits"] == "200"
    assert len(db.ff_billing_events.docs) == 2


@pytest.mark.asyncio
async def test_crash_after_pending_record_is_repaired_on_retry():
    fake_redis = FakeRedis()
    db = _database()
    real_update = db.ff_dodo_fulfillments.update_one
    failed = False

    async def fail_once(query, update, upsert=False):
        nonlocal failed
        if "$setOnInsert" in update and not failed:
            failed = True
            raise RuntimeError("crash after pending insert")
        return await real_update(query, update, upsert=upsert)

    db.ff_dodo_fulfillments.update_one = fail_once
    payload = _payment_payload()
    with pytest.raises(RuntimeError):
        await _post_webhook(payload, fake_redis, db)
    # Fulfillment record is pending; repair on retry.
    db.ff_dodo_fulfillments.update_one = real_update
    result = await _post_webhook(payload, fake_redis, db)
    assert result["status"] == "ACTIVE"
    assert fake_redis.hash["tenant:user"]["credits"] == "100"
    assert db.ff_dodo_fulfillments.docs[0]["status"] == "completed"
    assert db.ff_dodo_fulfillments.docs[0]["attempts"] == 1


@pytest.mark.asyncio
async def test_crash_after_redis_grant_recovers_without_double_redis_credit():
    fake_redis = FakeRedis()
    db = _database()

    original_eval = fake_redis.eval
    calls = []
    def crashing_eval(script, numkeys, *args):
        calls.append(args)
        if len(calls) == 1:
            # First call succeeds in Redis but then we crash before Mongo.
            result = original_eval(script, numkeys, *args)
            raise RuntimeError("crash after redis grant")
        return original_eval(script, numkeys, *args)

    fake_redis.eval = crashing_eval
    payload = _payment_payload()
    with pytest.raises(RuntimeError):
        await _post_webhook(payload, fake_redis, db)

    fake_redis.eval = original_eval
    result = await _post_webhook(payload, fake_redis, db)
    assert result["status"] == "ACTIVE"
    assert fake_redis.hash["tenant:user"]["credits"] == "100"
    assert db.subscriptions.docs[0]["credits_remaining"] == 100


@pytest.mark.asyncio
async def test_crash_after_mongo_mirror_is_idempotent_on_retry():
    fake_redis = FakeRedis()
    db = _database()
    real_mirror = billing_dodo._mirror_grant_to_mongo
    failed = False

    async def fail_once(*, tenant_id, product_id, credits, transaction_id, event_type, customer_email):
        nonlocal failed
        await real_mirror(tenant_id=tenant_id, product_id=product_id, credits=credits,
                          transaction_id=transaction_id, event_type=event_type,
                          customer_email=customer_email)
        if not failed:
            failed = True
            raise RuntimeError("crash after mongo mirror")

    payload = _payment_payload()
    with patch.object(billing_dodo, "_mirror_grant_to_mongo", fail_once):
        with pytest.raises(RuntimeError):
            await _post_webhook(payload, fake_redis, db)

    result = await _post_webhook(payload, fake_redis, db)
    assert result["status"] == "ACTIVE"
    assert fake_redis.hash["tenant:user"]["credits"] == "100"
    assert db.subscriptions.docs[0]["credits_remaining"] == 100
    assert db.ff_dodo_fulfillments.docs[0]["status"] == "completed"
    assert len(db.ff_billing_events.docs) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_delivery_grants_once():
    fake_redis = FakeRedis()
    db = _database()
    payload = _payment_payload(payment_id="pay_race")

    # Simulate slow Mongo fulfillment record creation so both coroutines see
    # no completed record and proceed to Redis.
    real_update = db.ff_dodo_fulfillments.update_one
    async def slow_update(query, update, upsert=False):
        await AsyncMock()()  # yield
        return await real_update(query, update, upsert=upsert)
    db.ff_dodo_fulfillments.update_one = slow_update

    results = await asyncio.gather(
        _post_webhook(payload, fake_redis, db, webhook_id="wh_1"),
        _post_webhook(payload, fake_redis, db, webhook_id="wh_2"),
    )
    statuses = {r["status"] for r in results}
    assert "ACTIVE" in statuses
    assert statuses <= {"ACTIVE", "already_fulfilled"}
    assert fake_redis.hash["tenant:user"]["credits"] == "100"
    assert db.subscriptions.docs[0]["credits_remaining"] == 100
    assert len(db.ff_billing_events.docs) == 1


@pytest.mark.asyncio
async def test_redis_failure_is_not_acknowledged():
    fake_redis = FakeRedis()
    db = _database()
    fake_redis.eval = Mock(side_effect=RuntimeError("redis down"))
    payload = _payment_payload()
    with pytest.raises(RuntimeError):
        await _post_webhook(payload, fake_redis, db)
    # Webhook not acknowledged; processed marker not set.
    assert f"dodo:webhooks:processed:wh_1" not in fake_redis.strings
    assert db.ff_dodo_fulfillments.docs[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_mongo_mirror_failure_is_not_acknowledged():
    fake_redis = FakeRedis()
    db = _database()
    with patch.object(billing_dodo, "_mirror_grant_to_mongo", side_effect=RuntimeError("mongo down")):
        payload = _payment_payload()
        with pytest.raises(RuntimeError):
            await _post_webhook(payload, fake_redis, db)
    # Redis already has credits; webhook is not acknowledged so Dodo retries.
    assert fake_redis.hash["tenant:user"]["credits"] == "100"
    assert f"dodo:webhooks:processed:wh_1" not in fake_redis.strings
    assert db.ff_dodo_fulfillments.docs[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_no_zero_balance_after_initial_grant():
    """The single atomic upsert+increment must never leave a 0-credit subscription."""
    fake_redis = FakeRedis()
    db = _database()
    result = await _post_webhook(_payment_payload(), fake_redis, db)
    assert result["status"] == "ACTIVE"
    sub = db.subscriptions.docs[0]
    assert sub["credits_remaining"] == 100
    assert sub["credits_monthly"] == 100
    assert "pay_1" in sub["processed_dodo_transactions"]
