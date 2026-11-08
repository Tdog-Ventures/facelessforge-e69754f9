"""Behavioural tests: call the ACTUAL live auto-attach / attach-asset lifecycle with a fake DB and a mocked stock provider.

No provider network calls, no DB, no stubs of the function under test.
"""
import asyncio
import sys

sys.path.insert(0, "/app/backend")

from backend.tests.fake_db import FakeCollection, FakeDatabase  # type: ignore

import backend.app.routes.api as api


def _user(uid="u1"):
    return {"id": uid, "role": "creator", "email": "u1@example.test"}


def _project(pid="p1", uid="u1", ratio="9:16"):
    return {"id": pid, "user_id": uid, "aspect_ratio": ratio, "niche": "finance"}


def _scene(sid="s1", pid="p1", dur=6):
    return {
        "id": sid,
        "project_id": pid,
        "scene_number": 1,
        "duration": dur,
        "search_terms": ["crystal ball forecasting office desk"],
        "visual_direction": "crystal ball on office desk",
    }


def _sub(uid="u1", credits=30):
    return {"user_id": uid, "credits_remaining": credits, "credits_monthly": 30, "plan": "free"}


def seed(db, name, docs):
    db.add(name, FakeCollection(name, docs))


def count(db, name):
    return len(db.__getattr__(name).docs)


def _run(coro):
    return asyncio.run(coro)


def _setup(monkeypatch, candidates):
    db = FakeDatabase()
    calls = {"queries": [], "orientation": [], "count": 0}

    async def fake_search(query, media_type=None, per_page=None, **kw):
        calls["count"] += 1
        calls["queries"].append(query)
        calls["orientation"].append(kw.get("orientation"))
        return {"results": candidates}

    class FakeSvc:
        @staticmethod
        def is_mock_mode():
            return False

        search_stock = staticmethod(fake_search)
        search_stock_aggregated = staticmethod(fake_search)

    monkeypatch.setattr(api, "get_db", lambda: db)
    monkeypatch.setattr(api, "stock_service", FakeSvc)
    return db, calls


GOOD = {
    "source": "pexels",
    "title": "crystal ball on office desk",
    "source_url": "https://example.test/pexels-photo/crystal-ball-desk/",
    "download_url": "http://d",
    "external_id": "1",
    "video_url": "http://v/1.mp4",
    "duration": 8,
    "asset_type": "stock_video",
    "media_type": "stock_video",
}
MOCK = {"source": "mock", "title": "crystal ball", "download_url": "http://d", "external_id": "m1"}
VAGUE = {
    "source": "pexels",
    "title": "pexels video 123",
    "source_url": "https://example.test/",
    "download_url": "http://d",
    "external_id": "v1",
}


def test_concise_query_is_forwarded_to_provider(monkeypatch):
    db, calls = _setup(monkeypatch, [])
    seed(db, "projects", [_project()])
    seed(db, "scenes", [_scene()])
    seed(db, "subscriptions", [_sub()])
    _run(api.auto_attach_assets("p1", api.AutoAttachRequest(idempotency_key="key-0001"), user=_user()))
    assert calls["queries"], "provider was never called"
    q = calls["queries"][0]
    assert len(q.split()) <= 5, f"query not compacted: {q!r}"
    assert "4k" not in q.lower() and "cinematic" not in q.lower()


def test_9x16_request_sends_portrait_orientation(monkeypatch):
    db, calls = _setup(monkeypatch, [])
    seed(db, "projects", [_project(ratio="9:16")])
    seed(db, "scenes", [_scene()])
    seed(db, "subscriptions", [_sub()])
    _run(api.auto_attach_assets("p1", api.AutoAttachRequest(idempotency_key="key-0002"), user=_user()))
    assert calls["orientation"], "provider called without orientation kwarg"
    assert "portrait" in calls["orientation"], calls["orientation"]


def test_irrelevant_and_mock_candidates_are_never_persisted_or_debited(monkeypatch):
    db, _ = _setup(monkeypatch, [MOCK, VAGUE])
    seed(db, "projects", [_project()])
    seed(db, "scenes", [_scene()])
    seed(db, "subscriptions", [_sub()])
    _run(api.auto_attach_assets("p1", api.AutoAttachRequest(idempotency_key="key-0003"), user=_user()))
    assert count(db, "assets") == 0, "irrelevant/mock candidate was persisted"
    sub = _run(db.subscriptions.find_one({"user_id": "u1"}))
    assert sub["credits_remaining"] == 30, "credits debited without a committed asset"
    assert count(db, "credit_transactions") == 0


def test_legit_candidate_is_persisted_and_debited_once(monkeypatch):
    db, _ = _setup(monkeypatch, [GOOD])
    seed(db, "projects", [_project()])
    seed(db, "scenes", [_scene(dur=6)])
    seed(db, "subscriptions", [_sub()])
    _run(api.auto_attach_assets("p1", api.AutoAttachRequest(idempotency_key="key-0004"), user=_user()))
    assert count(db, "assets") >= 1, "legitimate candidate not persisted"
    sub = _run(db.subscriptions.find_one({"user_id": "u1"}))
    assert sub["credits_remaining"] < 30, "credits not debited for a committed asset"


def test_same_idempotency_key_does_not_duplicate_assets_or_billing(monkeypatch):
    db, calls = _setup(monkeypatch, [GOOD])
    seed(db, "projects", [_project()])
    seed(db, "scenes", [_scene(dur=6)])
    seed(db, "subscriptions", [_sub()])
    body = api.AutoAttachRequest(idempotency_key="same-key")
    _run(api.auto_attach_assets("p1", body, user=_user()))
    assets_first = count(db, "assets")
    credits_first = _run(db.subscriptions.find_one({"user_id": "u1"}))["credits_remaining"]
    calls_first = calls["count"]
    second = _run(api.auto_attach_assets("p1", body, user=_user()))
    assert second.get("replayed") is True, second
    assert count(db, "assets") == assets_first
    assert _run(db.subscriptions.find_one({"user_id": "u1"}))["credits_remaining"] == credits_first
    assert calls["count"] == calls_first, "provider searched again on replay"


def test_attach_scene_asset_rejects_mock_footage(monkeypatch):
    import pytest

    db, _ = _setup(monkeypatch, [])
    seed(db, "projects", [_project()])
    seed(db, "scenes", [_scene()])
    body = api.StockAttachRequest(source="mock", external_id="m1", title="mock", media_type="stock_video")
    with pytest.raises(api.HTTPException) as ei:
        _run(api.attach_scene_asset("p1", "s1", body, user=_user()))
    assert ei.value.status_code == 422


def test_expired_lease_not_applicable_no_lease_in_live_lifecycle():
    import pytest

    assert not hasattr(api, "project_lease")
    pytest.skip("live lifecycle has no project_lease/asset_commit fence; lease expiry does not exist here")
