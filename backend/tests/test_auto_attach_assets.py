"""Auto-attach and manual scene attach: replacement, credits and concurrency.

Behavioural contract under test (see routes/api.py auto_attach_assets):
- A failed replacement preserves the previously usable selection and charges nothing.
- Replacement is insert-first: originals are removed only after the replacement is
  fully written, and only by their captured ids.
- Duplicate-key conflicts are actual duplicates and are skipped; every other write
  error is a storage/database failure that rolls the scene back.
- Credits are debited once per committed attachment and never for a rolled-back one.
- Concurrent writers of a project's stock selection are refused while a fenced
  asset lease is held; a repeated request with the same idempotency key replays.

Balances and selections are asserted on the stored documents, not on call logs.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from backend.app import project_lease
from backend.app.models import AutoAttachRequest, StockAttachRequest
from backend.app.routes import api as api_routes
from backend.app.routes.api import _debit_committed_attachments
from backend.tests.fake_db import FailingWrite, FakeCollection, FakeDatabase

PID = "p1"
UID = "user-1"


def _unique_stock_index():
    return [(("project_id", "scene_id", "external_id", "source"),
             lambda d: isinstance(d.get("external_id"), str))]


def _project(**over):
    doc = {"id": PID, "user_id": UID, "topic": "Test topic", "status": "SCENES_READY"}
    doc.update(over)
    return doc


def _scene(sid="scene-1", number=1, duration=10.0, terms=("video editing timeline",)):
    return {
        "id": sid, "project_id": PID, "scene_number": number,
        "duration": duration, "search_terms": list(terms),
        "visual_direction": "editing desk timeline",
    }


def _stock(sid, ext, name, scene_id="scene-1", source="pexels"):
    return {
        "id": sid, "project_id": PID, "scene_id": scene_id, "asset_type": "stock_video",
        "external_id": ext, "source": source, "preview_url": f"pv-{ext}", "name": name,
    }


def _db(*, scenes=None, assets=None, credits=10, ff=None, project=None):
    db = FakeDatabase(
        projects=FakeCollection("projects", [project or _project()]),
        scenes=FakeCollection("scenes", scenes if scenes is not None else [_scene()]),
        assets=FakeCollection("assets", assets or [], unique=_unique_stock_index()),
        subscriptions=FakeCollection("subscriptions", [{"user_id": UID, "credits_remaining": credits, "credits_monthly": 30}]),
        ff_credits=FakeCollection("ff_credits", ff or []),
        credit_transactions=FakeCollection("credit_transactions"),
        asset_operations=FakeCollection("asset_operations"),
    )
    return db


def _balance(db, field="credits_remaining"):
    return db.subscriptions.docs[0][field]


def _rows(coll):
    """Stored rows without the fake's internal _id, for content comparisons."""
    return [{k: v for k, v in d.items() if k != "_id"} for d in coll.docs]


def _patch_stock(monkeypatch, db, titles, *, by_query=None, ext_prefix="ext"):
    """Stock search returns ``titles`` unless ``by_query`` maps the query to other titles."""
    async def fake_search(*_args, **kwargs):
        query = str(kwargs.get("query") or (_args[0] if _args else "")).lower()
        chosen = titles
        for needle, alt in (by_query or {}).items():
            if needle in query:
                chosen = alt
                break
        return {"results": [
            {"external_id": f"{ext_prefix}{i}", "preview_url": f"pv-{ext_prefix}{i}", "title": title,
             "media_type": "stock_video", "source": "pexels"}
            for i, title in enumerate(chosen)
        ]}

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", fake_search, raising=False)
    monkeypatch.setattr(api_routes.stock_service, "is_mock_mode", lambda: True)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)


def _user(uid=UID):
    return {"id": uid, "role": "user", "email": f"{uid}@example.com"}


def _attach(replace=False, key=None):
    return api_routes.auto_attach_assets(PID, AutoAttachRequest(replace_existing=replace, idempotency_key=key), _user())


DISTINCT_TITLES = [
    "Video editing timeline interface",
    "Close up of hands typing keyboard desk",
    "Morning coffee ritual relaxing home",
    "Team collaboration whiteboard planning",
]
KITCHEN_TITLES = [
    "Chef chopping vegetables kitchen",
    "Pasta boiling water pot steam",
    "Bakery bread loaves shelf",
    "Spices arranged market stall",
]


# ---------------------------------------------------------------- existing contract

def test_auto_attach_filters_near_duplicates_and_attaches(monkeypatch):
    titles = [
        "Video editing timeline interface",
        "Close up of hands typing keyboard desk",
        "Video editing software timeline",
        "Morning coffee ritual relaxing home",
        "Close up of hands typing keyboard desk",
        "Team collaboration whiteboard planning",
    ]
    db = _db()
    _patch_stock(monkeypatch, db, titles)

    response = asyncio.run(_attach())

    assert response["attached"] == 4, response
    assert response["failed"] == 0
    assert response["near_duplicates_skipped"] == 2
    reasons = [d.get("reason") for d in response["details"] if d.get("reason")]
    assert reasons == ["near_duplicate", "near_duplicate"], reasons
    # A1 regression: filter_distinct_titles must actually run (no NameError).
    titles_seen = [d.get("title") for d in response["details"] if d.get("reason") == "near_duplicate"]
    assert "Video editing software timeline" in titles_seen
    assert len(db.assets.docs) == 4
    assert _balance(db) == 6



def test_auto_attach_retries_alternative_queries_when_primary_has_one_asset(monkeypatch):
    """A single stock result must not end the search for a 10s scene needing four."""
    db = _db()
    queries = []

    async def search(*_args, **kwargs):
        query = str(kwargs.get("query") or (_args[0] if _args else ""))
        queries.append(query)
        ix = len(queries)
        return {"results": [{
            "external_id": f"alternative-{ix}", "preview_url": f"preview-{ix}",
            "title": f"Distinct footage subject {ix} scene view",
            "media_type": "stock_video", "source": "pexels",
        }]}

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", search, raising=False)
    monkeypatch.setattr(api_routes.stock_service, "is_mock_mode", lambda: True)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    db.scenes.docs[0]["search_terms"] = [
        "video editing timeline", "editing studio workspace", "creative production desk",
    ]
    response = asyncio.run(_attach())

    assert len(queries) >= 3, queries
    assert response["attached"] >= 3, response
    assert len(db.assets.docs) == response["attached"]
    assert len({a["external_id"] for a in db.assets.docs}) == response["attached"]


def test_topup_does_not_re_attach_existing_assets(monkeypatch):
    existing = _stock("asset-e1", "ext0", "Video editing timeline interface")
    db = _db(assets=[existing])
    _patch_stock(monkeypatch, db, DISTINCT_TITLES + ["Stock charts finance data screen"])

    response = asyncio.run(_attach(replace=False))

    assert response["attached"] == 3, response
    assert response["failed"] == 0
    assert db.assets.delete_calls == []
    assert existing in _rows(db.assets)
    assert [a["external_id"] for a in db.assets.docs].count("ext0") == 1
    assert _balance(db) == 7


def test_replace_defers_deletion_until_replacement_confirmed(monkeypatch):
    old = _stock("asset-old", "old1", "Old stock clip")
    db = _db(assets=[old])
    _patch_stock(monkeypatch, db, [])

    response = asyncio.run(_attach(replace=True, key="replace-key-0001"))

    assert response["failed"] == 1
    assert db.assets.delete_calls == [], "originals must not be deleted on a futile search"
    assert _rows(db.assets) == [old]
    assert _balance(db) == 10


def test_replace_replaces_scene_assets_when_replacement_confirmed(monkeypatch):
    old = _stock("asset-old", "old1", "Old stock clip")
    other_scene = _stock("asset-other", "keep1", "Other scene clip", scene_id="scene-2")
    db = _db(assets=[old, other_scene])
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)

    response = asyncio.run(_attach(replace=True, key="replace-key-0002"))

    assert response["attached"] == 4, response
    assert response["failed"] == 0
    assert all(a["id"] != "asset-old" for a in db.assets.docs)
    assert "Old stock clip" not in [a["name"] for a in db.assets.docs]
    assert other_scene in _rows(db.assets), "replacement must never touch another scene"
    # Originals are removed by captured id, never by a scene-wide filter.
    assert len(db.assets.delete_calls) == 1
    assert db.assets.delete_calls[0] == {"project_id": PID, "id": {"$in": ["asset-old"]}}
    assert _balance(db) == 6


# ---------------------------------------------------------------- blocker 1: preservation

def test_all_inserts_failing_preserves_previous_selection_and_charges_nothing(monkeypatch):
    originals = [_stock("asset-a", "old1", "Old clip A"), _stock("asset-b", "old2", "Old clip B")]
    db = _db(assets=originals)
    db.assets.fail_inserts_where = lambda d: True  # every insert hits a storage failure
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)

    response = asyncio.run(_attach(replace=True, key="replace-key-0003"))

    assert response["attached"] == 0
    assert response["failed"] == 1
    reasons = [d.get("reason") for d in response["details"]]
    assert reasons == ["storage_error"], reasons
    assert "duplicate" not in reasons
    # The previously usable selection is intact and no originals were removed.
    assert _rows(db.assets) == originals
    assert db.assets.delete_calls == []
    # Nothing committed, so nothing was debited and no ledger row exists.
    assert _balance(db) == 10
    assert db.credit_transactions.docs == []
    assert db.subscriptions.docs[0].get("updated_at") is None


def test_partial_insert_failure_rolls_back_and_keeps_previous_selection(monkeypatch):
    originals = [_stock("asset-a", "old1", "Old clip A")]
    db = _db(assets=originals)
    # The first candidate lands; the second hits a storage failure.
    db.assets.fail_inserts_where = lambda d: d.get("external_id") == "ext1"
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)

    response = asyncio.run(_attach(replace=True, key="replace-key-0004"))

    assert response["attached"] == 0
    assert [d.get("reason") for d in response["details"]] == ["storage_error"]
    # The row that landed before the failure was rolled back.
    assert [a["id"] for a in db.assets.docs] == ["asset-a"]
    assert _rows(db.assets) == originals
    assert _balance(db) == 10
    assert db.credit_transactions.docs == []


def test_retry_after_storage_recovery_applies_replacement_and_charges_once(monkeypatch):
    originals = [_stock("asset-a", "old1", "Old clip A")]
    db = _db(assets=originals)
    db.assets.fail_inserts_where = lambda d: True
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)

    failed = asyncio.run(_attach(replace=True, key="replace-key-0005"))
    assert failed["attached"] == 0 and _rows(db.assets) == originals and _balance(db) == 10

    db.assets.fail_inserts_where = None  # storage recovers
    retried = asyncio.run(_attach(replace=True, key="replace-key-0005"))

    assert retried["attached"] == 4, retried
    assert retried["replayed"] is False
    assert "asset-a" not in [a["id"] for a in db.assets.docs]
    assert len(db.assets.docs) == 4
    assert _balance(db) == 6, "exactly one debit per committed attachment"
    assert len(db.credit_transactions.docs) == 4


# ---------------------------------------------------------------- duplicates vs failures

def test_actual_duplicate_is_skipped_not_reported_as_failure(monkeypatch):
    db = _db()

    def concurrent_writer_lands_first(doc):
        # Another writer inserted the same stock source between candidate selection and this write.
        if doc.get("external_id") == "ext1" and not any(a.get("external_id") == "ext1" for a in db.assets.docs):
            db.assets.docs.append({
                "id": "phantom", "project_id": PID, "scene_id": "scene-1",
                "asset_type": "stock_video", "external_id": "ext1", "source": "pexels", "name": "Phantom",
            })

    db.assets.on_insert = concurrent_writer_lands_first
    _patch_stock(monkeypatch, db, DISTINCT_TITLES[:3])

    response = asyncio.run(_attach())

    reasons = [d.get("reason") for d in response["details"] if d.get("status") == "skipped"]
    assert reasons == ["duplicate"], response["details"]
    assert response["failed"] == 0
    assert response["attached"] == 2
    assert _balance(db) == 8, "only the two committed attachments are debited"


# ---------------------------------------------------------------- credits at debit time

def test_credit_exhaustion_at_debit_rolls_back_the_scene(monkeypatch):
    originals = [_stock("asset-a", "old1", "Old clip A")]
    db = _db(assets=originals, credits=4)

    def concurrent_debit_drains_balance(doc):
        # After this operation's first debit lands, another project debits the rest
        # of the same account (a decrement, as the database would apply it).
        account = db.subscriptions.docs[0]
        account["credits_remaining"] -= account["credits_remaining"]

    db.credit_transactions.on_insert = concurrent_debit_drains_balance
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)

    response = asyncio.run(_attach(replace=True, key="replace-key-0006"))

    assert response["attached"] == 0
    assert [d.get("reason") for d in response["details"]] == ["credits_exhausted"]
    assert _rows(db.assets) == originals, "uncharged attachments must not survive"
    assert db.credit_transactions.docs == [], "no partial ledger rows remain"
    # 4 credits: this operation's first debit (1) was reversed; the other debit took 3.
    assert _balance(db) == 1
    assert db.subscriptions.docs[0]["charged_attach_ids"] == []


# ---------------------------------------------------------------- concurrency

def test_conflicting_replacement_is_refused_while_first_operation_holds_the_lease(monkeypatch):
    db = _db(assets=[_stock("asset-a", "old1", "Old clip A")])
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    refused = []

    async def second_request_arrives_mid_write(_doc):
        if refused or not _doc.get("external_id"):
            return
        if len(db.assets.insert_calls) != 1:
            return  # fire once, on the first candidate write of the first operation
        try:
            await api_routes.auto_attach_assets(
                PID, AutoAttachRequest(replace_existing=True, idempotency_key="replace-key-second"), _user(),
            )
        except HTTPException as exc:
            refused.append(exc.status_code)

    db.assets.on_insert = second_request_arrives_mid_write
    response = asyncio.run(_attach(replace=True, key="replace-key-first"))

    assert refused == [409], refused
    assert response["attached"] == 4, response
    names = sorted(a["name"] for a in db.assets.docs)
    assert len(names) == 4 and "Old clip A" not in names
    assert _balance(db) == 6, "the refused request must not be debited"
    assert db.asset_operations.docs[0]["status"] == "completed"


def test_manual_scene_attach_is_refused_while_replacement_runs(monkeypatch):
    db = _db()
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    refused = []

    async def manual_attach_arrives(doc):
        if refused or len(db.assets.insert_calls) != 1:
            return
        try:
            await api_routes.attach_scene_asset(
                PID, "scene-1",
                StockAttachRequest(
                    title="Manual clip", media_type="stock_video", source="pexels",
                    external_id="manual-1", preview_url="pv-manual-1",
                ),
                _user(),
            )
        except HTTPException as exc:
            refused.append(exc.status_code)

    db.assets.on_insert = manual_attach_arrives
    asyncio.run(_attach())

    assert refused == [409], refused
    assert all(a.get("external_id") != "manual-1" for a in db.assets.docs)


def test_manual_attach_insert_failure_is_not_reported_as_duplicate(monkeypatch):
    db = _db()
    db.assets.fail_inserts_where = lambda d: True
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_routes.attach_scene_asset(
            PID, "scene-1",
            StockAttachRequest(
                title="Manual clip", media_type="stock_video", source="pexels",
                external_id="manual-2", preview_url="pv-manual-2",
            ),
            _user(),
        ))
    assert exc.value.status_code == 503
    assert db.assets.docs == []


def test_manual_attach_real_duplicate_is_conflict(monkeypatch):
    db = _db(assets=[_stock("asset-x", "dup-1", "Existing")])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    # The pre-check finds nothing (concurrent writer), but the unique index rejects it.
    real_find_one = db.assets.find_one

    async def blind_find_one(query, projection=None, **kw):
        if query.get("external_id") == "dup-1":
            return None
        return await real_find_one(query, projection, **kw)

    db.assets.find_one = blind_find_one
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_routes.attach_scene_asset(
            PID, "scene-1",
            StockAttachRequest(
                title="Dup", media_type="stock_video", source="pexels", external_id="dup-1", preview_url="pv",
            ),
            _user(),
        ))
    assert exc.value.status_code == 409


def test_lease_is_fenced_against_a_stale_holder():
    db = _db()

    async def scenario():
        first = await project_lease.acquire(db, PID, project_lease.ASSET_LEASE, ttl_seconds=60)
        assert first is not None
        # The first holder stalls; its lease expires and a second writer takes over.
        db.projects.docs[0]["asset_lease_expires"] = project_lease._now().replace(year=2001)
        second = await project_lease.acquire(db, PID, project_lease.ASSET_LEASE, ttl_seconds=60)
        assert second is not None and second.fence == first.fence + 1
        # The stale holder can neither renew, confirm, nor release the new holder's lease.
        assert await project_lease.renew(db, first, ttl_seconds=60) is False
        assert await project_lease.still_held(db, first) is False
        await project_lease.release(db, first)
        assert await project_lease.still_held(db, second) is True

    asyncio.run(scenario())


# ---------------------------------------------------------------- repeated requests

def test_repeated_request_with_same_key_replays_without_charging_again(monkeypatch):
    db = _db(assets=[_stock("asset-a", "old1", "Old clip A")])
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)

    first = asyncio.run(_attach(replace=True, key="replace-key-0007"))
    inserts_after_first = len(db.assets.insert_calls)
    balance_after_first = _balance(db)
    second = asyncio.run(_attach(replace=True, key="replace-key-0007"))

    assert first["attached"] == 4 and balance_after_first == 6
    assert second["replayed"] is True
    assert second["attached"] == first["attached"]
    assert len(db.assets.insert_calls) == inserts_after_first, "a replay must not write again"
    assert _balance(db) == 6
    assert len(db.credit_transactions.docs) == 4


def test_failed_request_resumes_without_reprocessing_finished_scenes(monkeypatch):
    scenes = [_scene("scene-1", 1), _scene("scene-2", 2, terms=("kitchen cooking pan",))]
    db = _db(scenes=scenes, assets=[_stock("asset-s1", "o1", "Old one", scene_id="scene-1"),
                                    _stock("asset-s2", "o2", "Old two", scene_id="scene-2")])
    _patch_stock(monkeypatch, db, DISTINCT_TITLES, by_query={"kitchen": KITCHEN_TITLES})
    # Scene 2 fails to write on the first attempt only.
    db.assets.fail_inserts_where = lambda d: d.get("scene_id") == "scene-2"

    first = asyncio.run(_attach(replace=True, key="replace-key-0008"))
    assert first["failed"] == 1 and first["attached"] == 4
    scene1_ids = sorted(a["id"] for a in db.assets.docs if a["scene_id"] == "scene-1")
    assert _balance(db) == 6, "scene 1 committed and charged once"

    db.assets.fail_inserts_where = None
    second = asyncio.run(_attach(replace=True, key="replace-key-0008"))

    assert second["failed"] == 0
    assert sorted(a["id"] for a in db.assets.docs if a["scene_id"] == "scene-1") == scene1_ids, \
        "a resumed run must not re-replace a scene it already finished"
    assert _balance(db) == 2, "scene 2 charged once on the retry; scene 1 not charged again"


def test_replacement_requires_an_idempotency_key(monkeypatch):
    db = _db()
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_routes.auto_attach_assets(PID, AutoAttachRequest(replace_existing=True), _user()))
    assert exc.value.status_code == 422
    assert db.assets.insert_calls == []
    assert _balance(db) == 10


def test_idempotency_key_format_is_validated():
    with pytest.raises(Exception):
        AutoAttachRequest(replace_existing=True, idempotency_key="short")
    with pytest.raises(Exception):
        AutoAttachRequest(replace_existing=True, idempotency_key="has spaces in it")
    AutoAttachRequest(replace_existing=True, idempotency_key="aa-0c4f9e1b-22d4-4c3a-9d1e-7a2b3c4d5e6f")


def test_legacy_ff_credit_store_is_debited_when_no_subscription_exists(monkeypatch):
    db = _db(ff=[{"tenant_id": UID, "credits": 9}])
    db.subscriptions.docs.clear()
    _patch_stock(monkeypatch, db, DISTINCT_TITLES[:2])

    response = asyncio.run(_attach())

    assert response["attached"] == 2
    assert db.ff_credits.docs[0]["credits"] == 7
    assert db.subscriptions.docs == []


# ---------------------------------------------------------------- crash windows

def _stop_before_recording(db):
    """Simulate the process stopping after commit and debit but before recording the scene."""
    db.asset_operations.fail_updates_where = lambda q, u: "scene_results" in (u.get("$set") or {})


def test_resume_after_commit_does_not_replace_or_charge_again(monkeypatch):
    db = _db(assets=[_stock("asset-a", "old1", "Old clip A")])
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    _stop_before_recording(db)

    with pytest.raises(FailingWrite):
        asyncio.run(_attach(replace=True, key="replace-key-crash1"))

    committed_ids = sorted(a["id"] for a in db.assets.docs)
    assert len(committed_ids) == 4 and "asset-a" not in committed_ids
    assert _balance(db) == 6
    inserts_before_resume = len(db.assets.insert_calls)

    db.asset_operations.fail_updates_where = None
    resumed = asyncio.run(_attach(replace=True, key="replace-key-crash1"))

    assert resumed["attached"] == 4, resumed
    assert resumed["failed"] == 0
    assert len(db.assets.insert_calls) == inserts_before_resume, "the committed scene must not be written again"
    assert sorted(a["id"] for a in db.assets.docs) == committed_ids
    assert _balance(db) == 6, "the committed attachments were already paid for exactly once"
    assert len(db.credit_transactions.docs) == 4


def test_resume_after_top_up_commit_debits_each_attachment_once(monkeypatch):
    db = _db()
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    _stop_before_recording(db)

    with pytest.raises(FailingWrite):
        asyncio.run(_attach(replace=False, key="topup-key-crash1"))
    assert _balance(db) == 6

    db.asset_operations.fail_updates_where = None
    resumed = asyncio.run(_attach(replace=False, key="topup-key-crash1"))

    assert resumed["attached"] == 4, resumed
    assert len(db.assets.docs) == 4
    assert _balance(db) == 6


def test_debit_is_idempotent_per_attachment():
    db = _db()

    async def scenario():
        first = await _debit_committed_attachments(
            db, collection="subscriptions", balance_filter={"user_id": UID}, field="credits_remaining",
            user_id=UID, asset_ids=["a1", "a2"], reason="test",
        )
        again = await _debit_committed_attachments(
            db, collection="subscriptions", balance_filter={"user_id": UID}, field="credits_remaining",
            user_id=UID, asset_ids=["a1", "a2"], reason="test",
        )
        return first, again

    assert asyncio.run(scenario()) == (2, 0)
    assert _balance(db) == 8
    assert sorted(r["asset_id"] for r in db.credit_transactions.docs) == ["a1", "a2"]


def test_partial_debit_is_reversed_when_balance_runs_out():
    db = _db(credits=1)

    async def scenario():
        await _debit_committed_attachments(
            db, collection="subscriptions", balance_filter={"user_id": UID}, field="credits_remaining",
            user_id=UID, asset_ids=["a1", "a2"], reason="test",
        )

    with pytest.raises(api_routes._CreditsExhausted):
        asyncio.run(scenario())
    assert _balance(db) == 1, "the first debit must be reversed"
    assert db.credit_transactions.docs == []
    assert db.subscriptions.docs[0].get("charged_attach_ids", []) == []


def test_paid_replacement_resumes_without_requiring_a_second_balance(monkeypatch):
    db = _db(credits=4, assets=[_stock("asset-old", "old", "Old selection")])
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)
    _stop_before_recording(db)

    with pytest.raises(FailingWrite):
        asyncio.run(asyncio.wait_for(_attach(replace=True, key="paid-crash-0001"), timeout=2))
    assert _balance(db) == 0
    committed_ids = sorted(row["id"] for row in db.assets.docs)

    db.asset_operations.fail_updates_where = None
    result = asyncio.run(asyncio.wait_for(_attach(replace=True, key="paid-crash-0001"), timeout=2))

    assert result["failed"] == 0
    assert result["attached"] == 4
    assert sorted(row["id"] for row in db.assets.docs) == committed_ids
    assert _balance(db) == 0
    assert len(db.credit_transactions.docs) == 4


def test_debit_remembers_attachments_after_more_than_500_new_charges():
    db = _db(credits=600)

    async def scenario():
        kwargs = dict(collection="subscriptions", balance_filter={"user_id": UID},
                      field="credits_remaining", user_id=UID, reason="retention regression")
        assert await _debit_committed_attachments(
            db, asset_ids=[f"asset-{i}" for i in range(501)], **kwargs,
        ) == 501
        return await _debit_committed_attachments(db, asset_ids=["asset-0"], **kwargs)

    assert asyncio.run(asyncio.wait_for(scenario(), timeout=5)) == 0
    assert _balance(db) == 99
    assert len(db.credit_transactions.docs) == 501


def test_resumed_topup_counts_carried_and_new_debits(monkeypatch):
    key = "topup-partial-0001"
    carried = [_stock(f"carried-{i}", f"ext{i}", title) for i, title in enumerate(DISTINCT_TITLES[:2])]
    for row in carried:
        row["attach_op"] = f"{PID}:{key}"
    db = _db(credits=4, assets=carried)
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)

    result = asyncio.run(asyncio.wait_for(_attach(key=key), timeout=2))

    assert result["failed"] == 0
    assert result["attached"] == 4
    assert result["credits_charged"] == 4
    assert _balance(db) == 0
    assert len(db.credit_transactions.docs) == 4
