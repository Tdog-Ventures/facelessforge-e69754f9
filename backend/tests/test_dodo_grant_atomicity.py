"""Offline adversarial interleavings for Mongo Dodo grants (no provider calls)."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from pymongo.errors import DuplicateKeyError

from app.billing_dodo import _mirror_grant_to_mongo


class Collection:
    """Yield before each operation; individual Mongo document writes are atomic."""
    def __init__(self, key, initial=None):
        self.key = key
        self.docs = deepcopy(initial or [])
        self.unique = False

    async def create_index(self, *args, **kwargs):
        self.unique = kwargs.get('unique', False)

    def matches(self, doc, query):
        for key, value in query.items():
            if isinstance(value, dict) and '$ne' in value:
                if value['$ne'] in doc.get(key, []):
                    return False
            elif doc.get(key) != value:
                return False
        return True

    async def find_one(self, query, projection=None):
        result = next((deepcopy(d) for d in self.docs if self.matches(d, query)), None)
        await asyncio.sleep(0)
        return result

    async def insert_one(self, doc):
        await asyncio.sleep(0)
        if self.unique and any(d.get(self.key) == doc.get(self.key) for d in self.docs):
            raise DuplicateKeyError('duplicate account')
        self.docs.append(deepcopy(doc))

    async def update_one(self, query, update, upsert=False):
        await asyncio.sleep(0)
        doc = next((d for d in self.docs if self.matches(d, query)), None)
        inserted = doc is None
        if inserted:
            if not upsert:
                return SimpleNamespace(modified_count=0)
            doc = {k: v for k, v in query.items() if not isinstance(v, dict)}
            if self.unique and any(d.get(self.key) == doc.get(self.key) for d in self.docs):
                raise DuplicateKeyError('duplicate account')
            self.docs.append(doc)
            doc.update(deepcopy(update.get('$setOnInsert', {})))
        doc.update(deepcopy(update.get('$set', {})))
        for key, value in update.get('$inc', {}).items():
            doc[key] = doc.get(key, 0) + value
        for key, value in update.get('$addToSet', {}).items():
            values = doc.setdefault(key, [])
            if value not in values:
                values.append(value)
        return SimpleNamespace(modified_count=1)


class AtomicDodoGrants(unittest.IsolatedAsyncioTestCase):
    def database(self, existing=False):
        return SimpleNamespace(
            users=SimpleNamespace(find_one=AsyncMock(return_value={'id': 'user', 'email': 'test@example.invalid'})),
            ff_credits=Collection('tenant_id', [{'tenant_id': 'user', 'credits': 10}] if existing else []),
            subscriptions=Collection('user_id', [{'user_id': 'user', 'credits_remaining': 10, 'credits_monthly': 10}] if existing else []),
            ff_billing_events=Collection('_id'),
        )

    async def grant(self, transaction, credits=7):
        await _mirror_grant_to_mongo(tenant_id='user', product_id='pdt_starter', credits=credits,
                                    transaction_id=transaction, event_type='payment.succeeded', customer_email=None)

    def balances(self, db, expected):
        self.assertEqual(len(db.ff_credits.docs), 1)
        self.assertEqual(len(db.subscriptions.docs), 1)
        self.assertEqual(db.ff_credits.docs[0]['credits'], expected)
        self.assertEqual(db.subscriptions.docs[0]['credits_remaining'], expected)
        self.assertEqual(db.subscriptions.docs[0]['credits_monthly'], expected)

    async def test_distinct_concurrent_grants_sum_existing_balances(self):
        db = self.database(existing=True)
        with patch('app.db.get_db', return_value=db):
            await asyncio.gather(*(self.grant(str(i)) for i in range(12)))
        self.balances(db, 94)

    async def test_distinct_first_grants_create_single_account(self):
        db = self.database()
        with patch('app.db.get_db', return_value=db):
            await asyncio.gather(*(self.grant(str(i)) for i in range(12)))
        self.balances(db, 84)

    async def test_concurrent_and_later_replays_apply_once(self):
        db = self.database()
        with patch('app.db.get_db', return_value=db):
            await asyncio.gather(*(self.grant('same-event') for _ in range(12)))
            await self.grant('same-event')
        self.balances(db, 7)
        self.assertEqual(len(db.ff_billing_events.docs), 1)

    async def test_retry_after_second_account_failure_does_not_repeat_first(self):
        db = self.database()
        real_update = db.subscriptions.update_one
        failed = False
        async def fail_once(query, update, **kwargs):
            nonlocal failed
            if '$inc' in update and not failed:
                failed = True
                raise RuntimeError('synthetic interruption')
            return await real_update(query, update, **kwargs)
        db.subscriptions.update_one = fail_once
        with patch('app.db.get_db', return_value=db):
            with self.assertRaises(RuntimeError):
                await self.grant('retry-event')
            await self.grant('retry-event')
        self.balances(db, 7)


    async def test_index_failure_aborts_before_either_balance_changes(self):
        db = self.database(existing=True)
        db.subscriptions.create_index = AsyncMock(side_effect=DuplicateKeyError('legacy duplicate'))
        with patch('app.db.get_db', return_value=db):
            with self.assertRaises(DuplicateKeyError):
                await self.grant('blocked-event')
        self.balances(db, 10)
        self.assertEqual(db.ff_billing_events.docs, [])

    async def test_initial_upsert_duplicate_race_uses_existing_account(self):
        db = self.database(existing=True)
        real_update = db.ff_credits.update_one
        failed = False
        async def race_once(query, update, **kwargs):
            nonlocal failed
            if kwargs.get('upsert') and not failed:
                failed = True
                raise DuplicateKeyError('concurrent insert won')
            return await real_update(query, update, **kwargs)
        db.ff_credits.update_one = race_once
        with patch('app.db.get_db', return_value=db):
            await self.grant('race-event')
        self.balances(db, 17)


# Opt in only with a disposable Mongo server; never load the application's env.
import os
@unittest.skipUnless(os.environ.get('DODO_TEST_MONGO_URL'), 'requires disposable Mongo URL')
class RealMongoDodoGrants(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from motor.motor_asyncio import AsyncIOMotorClient
        import uuid
        self.client = AsyncIOMotorClient(os.environ['DODO_TEST_MONGO_URL'], serverSelectionTimeoutMS=3000)
        self.db = self.client['ff_security_grants_test_' + uuid.uuid4().hex]
        await self.db.users.insert_one({'id': 'user', 'email': 'test@example.invalid'})

    async def asyncTearDown(self):
        await self.client.drop_database(self.db.name)
        self.client.close()

    async def exercise(self, existing):
        if existing:
            await self.db.ff_credits.insert_one({'tenant_id': 'user', 'credits': 10})
            await self.db.subscriptions.insert_one({'user_id': 'user', 'credits_remaining': 10, 'credits_monthly': 10})
        async def grant(transaction):
            await _mirror_grant_to_mongo(tenant_id='user', product_id='pdt_starter', credits=7,
                transaction_id=transaction, event_type='payment.succeeded', customer_email=None)
        with patch('app.db.get_db', return_value=self.db):
            await asyncio.gather(*(grant(str(i)) for i in range(32)))
            await asyncio.gather(*(grant('0') for _ in range(16)))
        self.assertEqual(await self.db.ff_credits.count_documents({}), 1)
        self.assertEqual(await self.db.subscriptions.count_documents({}), 1)
        expected = (10 if existing else 0) + 32 * 7
        ff = await self.db.ff_credits.find_one({'tenant_id': 'user'})
        sub = await self.db.subscriptions.find_one({'user_id': 'user'})
        self.assertEqual(ff['credits'], expected)
        self.assertEqual(sub['credits_remaining'], expected)
        self.assertEqual(sub['credits_monthly'], expected)
        self.assertEqual(await self.db.ff_billing_events.count_documents({}), 32)

    async def test_real_mongo_existing_account_concurrency_and_replay(self):
        await self.exercise(True)

    async def test_real_mongo_first_account_concurrency_and_replay(self):
        await self.exercise(False)

    async def test_real_mongo_legacy_duplicates_fail_closed(self):
        await self.db.subscriptions.insert_many([{'user_id': 'user', 'credits_remaining': 10},
                                                 {'user_id': 'user', 'credits_remaining': 20}])
        with patch('app.db.get_db', return_value=self.db):
            with self.assertRaises(DuplicateKeyError):
                await _mirror_grant_to_mongo(tenant_id='user', product_id='pdt_starter', credits=7,
                    transaction_id='blocked', event_type='payment.succeeded', customer_email=None)
        self.assertEqual(await self.db.ff_credits.count_documents({}), 0)
        self.assertEqual(await self.db.ff_billing_events.count_documents({}), 0)
