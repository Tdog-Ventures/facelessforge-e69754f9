"""Standalone-Mongo commit fencing for project asset writers.

The lease and the non-expiring commit barrier share the SAME project document.
Entering a mutation compares holder/fence and installs the barrier atomically;
lease acquisition refuses a barrier even after expiry. Thus takeover either wins
before entry (the stale callback never runs), or waits until all asset, debit,
reversal and rollback writes have finished. A pre-write ownership read cannot
provide this property across collections.

Cancellation drains the protected task before unpinning. A process death or an
ambiguous Mongo write leaves the barrier pinned, intentionally failing closed.
There is no TTL unlock: recovery requires quiescing the writer AND outstanding DB
commands, reconciling assets/account IDs, then clearing the exact barrier by CAS.
See docs/asset-commit-fencing.md. This trades crash availability for correctness
on the inspected standalone topology; it does not pretend to be a transaction.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from pymongo.errors import DuplicateKeyError, PyMongoError, WriteError

from . import project_lease

logger = logging.getLogger("facelessforge.asset_commit")


class LeaseLost(Exception):
    """Ownership changed before the mutation boundary; no callback was executed."""


class CommitUncertain(Exception):
    """A durable barrier is retained until an ambiguous write is reconciled."""


class _Collection:
    def __init__(self, collection, state):
        self._collection = collection
        self._state = state

    def __getattr__(self, name):
        method = getattr(self._collection, name)
        if name not in {"insert_one", "insert_many", "update_one", "update_many",
                        "delete_one", "delete_many", "find_one_and_update", "replace_one"}:
            return method

        async def write(*args, **kwargs):
            # Once a command's result is unknown, no compensation or later write
            # can safely assume its outcome. Retain the barrier, even if a caller
            # catches the original storage error.
            if self._state["uncertain"]:
                raise CommitUncertain("Asset commit requires reconciliation")
            try:
                result = await method(*args, **kwargs)
                if getattr(result, "acknowledged", True) is False:
                    self._state["uncertain"] = True
                    raise CommitUncertain("Unacknowledged asset commit write")
                return result
            except (DuplicateKeyError, WriteError):
                raise  # acknowledged rejection, not an ambiguous write
            except PyMongoError:
                self._state["uncertain"] = True
                raise
            except asyncio.CancelledError:
                self._state["uncertain"] = True
                raise

        return write


class _Database:
    def __init__(self, db, state, guard):
        self._db = db
        self._state = state
        self._guard = guard

    def __getattr__(self, name):
        return _Collection(getattr(self._db, name), self._state)


async def set_intent(fenced_db, intent: dict):
    """Persist recovery evidence before changing assets or accounts."""
    result = await fenced_db.projects.update_one(
        fenced_db._guard, {"$set": {"asset_commit_intent": intent}},
    )
    if getattr(result, "matched_count", 0) != 1:
        fenced_db._state["uncertain"] = True
        raise CommitUncertain("Asset recovery intent could not be recorded")


def require_recovery(db):
    """A failed compensation must not discard its durable reconciliation data."""
    if isinstance(db, _Database):
        db._state["uncertain"] = True


async def run(db, lease: project_lease.Lease, callback):
    """Run an async mutation under a datastore-enforced, non-expiring barrier.

    The callback must use the supplied database for ALL protected writes, must
    await them (no detached work), and must not call lease acquire/release itself.
    Stock/network lookups belong outside this short mutation boundary.
    """
    if lease.prefix != project_lease.ASSET_LEASE:
        raise ValueError("Asset commit requires an asset lease")

    async def protected():
        barrier = uuid.uuid4().hex
        guard = {"id": lease.project_id, **lease.guard(), "asset_commit_barrier": barrier}
        result = await db.projects.update_one(
            {"id": lease.project_id, **lease.guard(), "asset_commit_barrier": None},
            {"$set": {"asset_commit_barrier": barrier, "asset_commit_started": project_lease._now()}},
        )
        if getattr(result, "matched_count", 0) != 1:
            raise LeaseLost("Asset ownership changed or a commit needs recovery")
        state = {"uncertain": False}
        try:
            return await callback(_Database(db, state, guard))
        except asyncio.CancelledError:
            state["uncertain"] = True
            raise
        finally:
            if state["uncertain"]:
                logger.critical("asset commit pinned for reconciliation project=%s barrier=%s", lease.project_id, barrier)
                raise CommitUncertain("Asset commit outcome is uncertain; reconciliation required")
            # Failure/timeout here also fails closed: no timer may clear a barrier.
            cleared = await db.projects.update_one(
                guard, {"$unset": {"asset_commit_barrier": "", "asset_commit_started": "", "asset_commit_intent": ""}},
            )
            if getattr(cleared, "matched_count", 0) != 1:
                raise CommitUncertain("Asset commit barrier could not be released")

    task = asyncio.create_task(protected())
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                # An inner cancellation is not an acknowledged commit outcome.
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result
