"""Bounded takeover, commit-window, cancellation and fail-closed recovery probes."""
import asyncio
import copy

import pytest
from fastapi import HTTPException
from pymongo.errors import NetworkTimeout

from backend.app import asset_commit, project_lease
from backend.app.models import AutoAttachRequest, StockAttachRequest
from backend.app.routes import api as api_routes
from backend.tests.test_auto_attach_assets import (
    DISTINCT_TITLES, KITCHEN_TITLES, PID, _attach, _balance, _db, _patch_stock,
    _rows, _stock, _user,
)


def _expire(db):
    db.projects.docs[0]["asset_lease_expires"] = project_lease._now().replace(year=2001)


async def _acquire(db):
    return await project_lease.acquire(db, PID, project_lease.ASSET_LEASE, ttl_seconds=120)


def test_lookup_outlives_lease_without_changing_originals_or_balance(monkeypatch):
    old = _stock("original", "old", "Original selection")
    db = _db(assets=[old])
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    search = api_routes.stock_service.search_stock_aggregated
    holders = []

    async def takeover(*args, **kwargs):
        _expire(db)
        holders.append(await _acquire(db))
        assert holders[-1] is not None
        return await search(*args, **kwargs)

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", takeover)
    result = asyncio.run(asyncio.wait_for(_attach(replace=True, key="old-lookup-holder"), 2))

    assert result["aborted"] == "lease_lost"
    assert result["attached"] == result["credits_charged"] == 0
    assert _rows(db.assets) == [old]
    assert _balance(db) == 10
    assert db.credit_transactions.docs == []
    assert db.projects.docs[0]["asset_lease_holder"] == holders[0].token


@pytest.mark.parametrize("boundary", ["lookup", "commit_cas", "scene_record_cas", "final_record_cas"])
@pytest.mark.parametrize("same_key", [False, True])
def test_takeover_holder_completes_and_stale_holder_cannot_undo_or_recharge(monkeypatch, boundary, same_key):
    db = _db(assets=[_stock("original", "old", "Original selection")])
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    old_key = "takeover-old-key"
    new_key = old_key if same_key else "takeover-current-key"
    search = api_routes.stock_service.search_stock_aggregated
    took_over = False
    current = {}

    async def takeover():
        nonlocal took_over
        took_over = True  # before nested awaits: no recursive re-entry
        _expire(db)
        lease = await _acquire(db)
        assert lease is not None
        # Different external IDs distinguish a stale selection from the current one.
        _patch_stock(monkeypatch, db, KITCHEN_TITLES, ext_prefix="current")
        try:
            body = AutoAttachRequest(replace_existing=True, idempotency_key=new_key)
            response = await api_routes._auto_attach_under_lease(
                db, lease, PID, db.projects.docs[0], body, _user(), f"{PID}:{new_key}",
            )
            assert response["failed"] == 0 and response["attached"] == 4
        finally:
            await project_lease.release(db, lease)
        current.update(assets=copy.deepcopy(db.assets.docs), account=copy.deepcopy(db.subscriptions.docs),
                       ledger=copy.deepcopy(db.credit_transactions.docs),
                       operation=copy.deepcopy(next(o for o in db.asset_operations.docs if o["_id"] == f"{PID}:{new_key}")))

    async def lookup(*args, **kwargs):
        if not took_over and boundary in ("lookup", "commit_cas"):
            if boundary == "lookup":
                await takeover()
        return await search(*args, **kwargs)

    async def before_cas(query, update):
        # Transfer immediately before Mongo evaluates a boundary CAS, including
        # operation-record writes AFTER an acknowledged scene commit.
        if took_over or not update.get("$set", {}).get("asset_commit_barrier"):
            return
        operation = next((o for o in db.asset_operations.docs if o["_id"] == f"{PID}:{old_key}"), {})
        trigger = (
            (boundary == "commit_cas" and bool(operation) and not db.assets.insert_calls)
            or (boundary == "scene_record_cas" and _balance(db) == 6)
            or (boundary == "final_record_cas" and bool(operation.get("scene_results")))
        )
        if trigger:
            await takeover()

    db.projects.on_update = before_cas
    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", lookup)
    try:
        result = asyncio.run(asyncio.wait_for(_attach(replace=True, key=old_key), 2))
        assert result["aborted"] == "lease_lost"
    except HTTPException as exc:
        assert boundary in ("scene_record_cas", "final_record_cas") and exc.status_code == 409
    assert took_over
    assert db.assets.docs == current["assets"]
    assert db.subscriptions.docs == current["account"]
    assert db.credit_transactions.docs == current["ledger"]
    assert next(o for o in db.asset_operations.docs if o["_id"] == f"{PID}:{new_key}") == current["operation"]
    # Render loads this exact collection. No staged/stale IDs can be selected:
    # stale holder was rejected before its first insert, not hidden by a UI filter.
    if boundary in ("lookup", "commit_cas"):
        assert all(a["external_id"].startswith("current") for a in db.assets.docs)
    assert _balance(db) == (6 if same_key or boundary in ("lookup", "commit_cas") else 2)
    replay = asyncio.run(asyncio.wait_for(_attach(replace=True, key=new_key), 2))
    assert replay["replayed"] is True
    assert db.assets.docs == current["assets"] and db.subscriptions.docs == current["account"]


@pytest.mark.parametrize("phase", ["insert", "delete_originals", "debit", "reversal", "rollback", "scene_record", "final_record"])
def test_expired_lease_cannot_transfer_inside_mutation_or_compensation(monkeypatch, phase):
    old = _stock("original", "old", "Original selection")
    db = _db(assets=[old], credits=4)
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    checked = []

    async def cannot_transfer():
        _expire(db)
        assert db.projects.docs[0].get("asset_commit_barrier")
        assert await _acquire(db) is None
        checked.append(phase)

    if phase == "insert":
        db.assets.on_insert = lambda doc: cannot_transfer()
    elif phase in ("delete_originals", "rollback"):
        original_delete = db.assets.delete_many

        async def deleting(query):
            await cannot_transfer()
            return await original_delete(query)
        db.assets.delete_many = deleting
        if phase == "rollback":
            db.assets.fail_inserts_where = lambda doc: doc.get("external_id") == "ext1"
    elif phase in ("scene_record", "final_record"):
        async def recording(query, update):
            values = update.get("$set", {})
            if (phase == "scene_record" and "scene_results" in values) or (phase == "final_record" and values.get("status") == "completed"):
                await cannot_transfer()
        db.asset_operations.on_update = recording
    else:
        async def accounting(query, update):
            delta = update.get("$inc", {}).get("credits_remaining", 0)
            if (phase == "debit" and delta < 0) or (phase == "reversal" and delta > 0):
                await cannot_transfer()
        db.subscriptions.on_update = accounting
        if phase == "reversal":
            db.credit_transactions.on_insert = lambda doc: db.subscriptions.docs[0].update(credits_remaining=0)

    response = asyncio.run(asyncio.wait_for(_attach(replace=True, key=f"commit-phase-{phase}"), 2))
    assert checked
    assert db.projects.docs[0].get("asset_commit_barrier") is None
    if phase in ("rollback", "reversal"):
        assert response["failed"] == 1
        assert _rows(db.assets) == [old]
        assert _balance(db) == (1 if phase == "reversal" else 4)
    else:
        assert response["failed"] == 0 and response["attached"] == 4
        assert _balance(db) == 0


def test_manual_attach_is_fenced_at_insert_boundary(monkeypatch):
    db = _db(assets=[_stock("original", "old", "Original selection")])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    transferred = []

    async def transfer_before_barrier(query, update):
        if not transferred and update.get("$set", {}).get("asset_commit_barrier"):
            _expire(db)
            transferred.append(await _acquire(db))
            assert transferred[-1] is not None

    db.projects.on_update = transfer_before_barrier
    with pytest.raises(HTTPException) as exc:
        asyncio.run(asyncio.wait_for(api_routes.attach_scene_asset(
            PID, "scene-1", StockAttachRequest(title="Stale", media_type="stock_video", source="pexels",
                                              external_id="stale-manual", preview_url="pv-stale"), _user(),
        ), 2))
    assert exc.value.status_code == 409
    assert [a["id"] for a in db.assets.docs] == ["original"]
    assert db.rights_registry.docs == [] and _balance(db) == 10
    assert db.projects.docs[0]["asset_lease_holder"] == transferred[0].token


def test_cancellation_drains_acknowledged_mutations_before_releasing_barrier():
    db = _db()

    async def scenario():
        lease = await _acquire(db)
        entered, finish = asyncio.Event(), asyncio.Event()

        async def mutation(fenced_db):
            entered.set()
            await finish.wait()
            await fenced_db.assets.insert_one(_stock("committed", "one", "Committed"))

        task = asyncio.create_task(asset_commit.run(db, lease, mutation))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        _expire(db)
        assert await _acquire(db) is None
        await project_lease.release(db, lease)
        assert db.projects.docs[0]["asset_lease_holder"] == lease.token
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [a["id"] for a in db.assets.docs] == ["committed"]
        assert db.projects.docs[0].get("asset_commit_barrier") is None
        assert await _acquire(db) is not None

    asyncio.run(asyncio.wait_for(scenario(), 2))


def test_ambiguous_write_stays_pinned_and_does_not_attempt_reversal(monkeypatch):
    db = _db(assets=[_stock("original", "old", "Original selection")])
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    real_update = db.subscriptions.update_one

    async def lost_ack(query, update, **kwargs):
        result = await real_update(query, update, **kwargs)
        if update.get("$inc", {}).get("credits_remaining", 0) < 0:
            raise NetworkTimeout("acknowledgement lost after server applied debit")
        return result

    db.subscriptions.update_one = lost_ack
    with pytest.raises(HTTPException) as exc:
        asyncio.run(asyncio.wait_for(_attach(replace=True, key="ambiguous-debit-key"), 2))
    assert exc.value.status_code == 503
    assert _balance(db) == 9
    assert len(db.subscriptions.docs[0]["charged_attach_ids"]) == 1
    barrier = db.projects.docs[0]["asset_commit_barrier"]
    intent = db.projects.docs[0]["asset_commit_intent"]
    assert intent["originals"][0]["id"] == "original"
    assert {a["id"] for a in intent["candidates"]} == {a["id"] for a in db.assets.docs}
    _expire(db)
    assert asyncio.run(_acquire(db)) is None
    with pytest.raises(HTTPException) as retry:
        asyncio.run(asyncio.wait_for(_attach(replace=True, key="ambiguous-debit-key"), 2))
    assert retry.value.status_code == 409
    assert _balance(db) == 9 and db.projects.docs[0]["asset_commit_barrier"] == barrier

    # Simulate operator recovery ONLY after the original request/commands are
    # drained. Clear the exact pin, then resume using durable attachment IDs.
    db.subscriptions.update_one = real_update
    asyncio.run(db.projects.update_one(
        {"id": PID, "asset_commit_barrier": barrier},
        {"$unset": {"asset_commit_barrier": "", "asset_commit_started": ""}},
    ))
    response = asyncio.run(asyncio.wait_for(_attach(replace=True, key="ambiguous-debit-key"), 2))
    assert response["failed"] == 0 and response["attached"] == 4
    assert _balance(db) == 6
    again = asyncio.run(asyncio.wait_for(_attach(replace=True, key="ambiguous-debit-key"), 2))
    assert again["replayed"] and _balance(db) == 6


def test_resumed_paid_topup_survives_transient_paid_id_lookup_failure(monkeypatch):
    key = "paid-topup-read-failure"
    carried = [_stock(f"paid-{i}", f"ext{i}", title) for i, title in enumerate(DISTINCT_TITLES)]
    for row in carried:
        row["attach_op"] = f"{PID}:{key}"
    db = _db(assets=carried, credits=0)
    paid_ids = [row["id"] for row in carried]
    db.subscriptions.docs[0]["charged_attach_ids"] = paid_ids[:]
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    real_find = db.subscriptions.find_one

    async def failed_paid_lookup(query, *args, **kwargs):
        if "charged_attach_ids" in query:
            raise NetworkTimeout("transient read failure for a previously paid ID")
        return await real_find(query, *args, **kwargs)

    db.subscriptions.find_one = failed_paid_lookup
    failed = asyncio.run(asyncio.wait_for(_attach(key=key), 2))
    assert failed["failed"] == 1 and failed["details"][-1]["reason"] == "charge_failed"
    assert _rows(db.assets) == carried and _balance(db) == 0
    assert db.subscriptions.docs[0]["charged_attach_ids"] == paid_ids
    assert db.credit_transactions.docs == []
    assert db.projects.docs[0].get("asset_commit_barrier") is None

    db.subscriptions.find_one = real_find
    resumed = asyncio.run(asyncio.wait_for(_attach(key=key), 2))
    assert resumed["failed"] == 0 and resumed["attached"] == 4
    assert resumed["credits_charged"] == 0
    assert _rows(db.assets) == carried and _balance(db) == 0
    assert asyncio.run(asyncio.wait_for(_attach(key=key), 2))["replayed"] is True


def test_mixed_paid_unpaid_topup_survives_concurrent_account_spend_and_resumes(monkeypatch):
    key = "mixed-paid-topup"
    carried = [_stock("paid", "ext0", DISTINCT_TITLES[0]), _stock("unpaid", "ext1", DISTINCT_TITLES[1])]
    for row in carried:
        row["attach_op"] = f"{PID}:{key}"
    db = _db(assets=carried, credits=3)
    db.subscriptions.docs[0]["charged_attach_ids"] = ["paid"]
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    search = api_routes.stock_service.search_stock_aggregated

    async def other_project_spends_balance(*args, **kwargs):
        db.subscriptions.docs[0]["credits_remaining"] = 0
        return await search(*args, **kwargs)

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", other_project_spends_balance)
    failed = asyncio.run(asyncio.wait_for(_attach(key=key), 2))
    assert failed["failed"] == 1 and failed["details"][-1]["reason"] == "credits_exhausted"
    assert _rows(db.assets) == carried and _balance(db) == 0
    assert db.subscriptions.docs[0]["charged_attach_ids"] == ["paid"]
    assert db.credit_transactions.docs == []

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", search)
    db.subscriptions.docs[0]["credits_remaining"] = 3  # external credit grant
    resumed = asyncio.run(asyncio.wait_for(_attach(key=key), 2))
    assert resumed["failed"] == 0 and resumed["attached"] == 4
    assert resumed["credits_charged"] == 3 and _balance(db) == 0
    assert all(row in _rows(db.assets) for row in carried)
    assert len(db.subscriptions.docs[0]["charged_attach_ids"]) == 4
    assert len(db.credit_transactions.docs) == 3
    account = copy.deepcopy(db.subscriptions.docs)
    assert asyncio.run(asyncio.wait_for(_attach(key=key), 2))["replayed"] is True
    assert db.subscriptions.docs == account
