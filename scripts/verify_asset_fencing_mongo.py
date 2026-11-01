"""Bounded, local-only fencing probe in a disposable Mongo database.

Run from the repo root with PYTHONPATH=.:backend and a scratch STORAGE_DIR.
Uses no providers, HTTP endpoints, generation or rendering. Never touches an
application database: it creates and drops only its own random ff_fence_probe_* DB.
"""
import asyncio
import uuid
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from backend.app import asset_commit, project_lease
from backend.app.routes.api import _debit_committed_attachments


async def probe():
    client = AsyncIOMotorClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=3000)
    name = f"ff_fence_probe_{uuid.uuid4().hex}"
    db = client[name]
    try:
        hello = await client.admin.command("hello")
        print({k: hello.get(k) for k in ("setName", "msg", "maxWireVersion")})
        await db.projects.insert_one({"id": "project"})
        await db.assets.insert_one({"id": "original", "project_id": "project"})
        await db.subscriptions.insert_one({"user_id": "user", "credits_remaining": 10})

        async def acquire():
            return await project_lease.acquire(db, "project", project_lease.ASSET_LEASE, ttl_seconds=120)

        async def expire():
            await db.projects.update_one({"id": "project"}, {"$set": {"asset_lease_expires": datetime(2001, 1, 1, tzinfo=timezone.utc)}})

        first = await acquire()
        await expire()  # old stock lookup outlives its lease
        current = await acquire()
        assert current and current.fence == first.fence + 1

        async def current_commit(fenced_db):
            await asset_commit.set_intent(fenced_db, {"originals": [{"id": "original"}], "candidates": [{"id": "current"}]})
            await fenced_db.assets.insert_one({"id": "current", "project_id": "project", "status": "attached"})
            await fenced_db.assets.delete_one({"id": "original", "project_id": "project"})
            await _debit_committed_attachments(
                fenced_db, collection="subscriptions", balance_filter={"user_id": "user"},
                field="credits_remaining", user_id="user", asset_ids=["current"], reason="isolated probe",
            )

        await asset_commit.run(db, current, current_commit)
        snapshot = await db.assets.find({}).to_list(None)
        account = await db.subscriptions.find_one({"user_id": "user"})

        async def stale_commit(fenced_db):
            raise AssertionError("stale mutation callback ran")

        try:
            await asset_commit.run(db, first, stale_commit)
        except asset_commit.LeaseLost:
            pass
        else:
            raise AssertionError("stale commit accepted")
        await project_lease.release(db, first)
        assert await project_lease.still_held(db, current)
        assert await db.assets.find({}).to_list(None) == snapshot
        assert await db.subscriptions.find_one({"user_id": "user"}) == account

        async def retry_debit(fenced_db):
            return await _debit_committed_attachments(
                fenced_db, collection="subscriptions", balance_filter={"user_id": "user"},
                field="credits_remaining", user_id="user", asset_ids=["current"], reason="isolated retry",
            )
        assert await asset_commit.run(db, current, retry_debit) == 0
        assert (await db.subscriptions.find_one({"user_id": "user"}))["credits_remaining"] == 9

        entered, finish = asyncio.Event(), asyncio.Event()

        async def in_flight(fenced_db):
            entered.set()
            await finish.wait()
            await fenced_db.assets.update_one({"id": "current"}, {"$set": {"acknowledged": True}})

        task = asyncio.create_task(asset_commit.run(db, current, in_flight))
        await entered.wait()
        await expire()
        assert await acquire() is None  # actual Mongo CAS, not a fake predicate
        finish.set()
        await task
        next_holder = await acquire()
        assert next_holder and next_holder.fence == current.fence + 1
        assert (await db.assets.find_one({"id": "current"}))["acknowledged"] is True
        print("PASS: stale callback fenced; selection/balance preserved; current commit/retry complete; in-flight takeover denied")
    finally:
        await client.drop_database(name)
        client.close()
        print(f"Disposable database removed: {name}")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(probe(), timeout=15))
