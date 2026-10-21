"""Auto-attach endpoint: distinct-title filtering wired in, replacement
deletion deferred until a non-empty replacement set is confirmed.

Regression coverage for CodeRabbit findings on PR #17/#18:
- A1: filter_distinct_titles was never imported into api.py, so every scene
  died at collection time with a NameError (surfacing as scene-level
  "failed" details).
- A2: replacement mode deleted a scene's existing stock assets before the
  stock search was known to succeed; a futile search left the scene empty.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.app.models import AutoAttachRequest
from backend.app.routes import api as api_routes


def _match(doc, query):
    for key, cond in query.items():
        if isinstance(cond, dict) and "$in" in cond:
            if doc.get(key) not in cond["$in"]:
                return False
        elif isinstance(cond, dict):
            if not _match(doc.get(key) or {}, cond):
                return False
        elif doc.get(key) != cond:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = [dict(d) for d in docs]

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, _n):
        return [dict(d) for d in self._docs]


class FakeAssets:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]
        self.delete_calls = []

    def find(self, query=None, projection=None):
        return FakeCursor([d for d in self.docs if _match(d, query or {})])

    async def count_documents(self, query):
        query = query or {}
        return sum(1 for d in self.docs if _match(d, query))

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def delete_many(self, query):
        self.delete_calls.append(dict(query))
        before = len(self.docs)
        self.docs[:] = [d for d in self.docs if not _match(d, query)]
        return SimpleNamespace(deleted_count=before - len(self.docs))


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    def find(self, query=None, projection=None):
        return FakeCursor([d for d in self.docs if _match(d, query or {})])

    async def find_one(self, query, _projection=None):
        for doc in self.docs:
            if _match(doc, query):
                return dict(doc)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, _query, update, upsert=False):
        for doc in self.docs:
            if not update.get("$set"):
                break
            doc.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=len(self.docs))

    async def count_documents(self, query):
        return sum(1 for d in self.docs if _match(d, query))


class FakeDB:
    def __init__(self, project, scenes, assets=None):
        self.projects = FakeCollection([project])
        self.scenes = FakeCollection(scenes)
        self.assets = FakeAssets(assets)
        self.subscriptions = FakeCollection()
        self.ff_credits = FakeCollection()
        self.credit_transactions = FakeCollection()
        self.render_jobs = FakeCollection()

    def __getattr__(self, name):
        if name in ("projects", "scenes", "assets", "subscriptions", "ff_credits", "credit_transactions", "render_jobs"):
            raise AttributeError(name)
        return self.__dict__.setdefault("_" + name, FakeCollection())


def _project(pid="p1", uid="user-1"):
    return {"id": pid, "user_id": uid, "topic": "Test topic", "status": "SCENES_READY"}


def _scene(sid="scene-1", duration=10.0, terms=("video editing timeline",)):
    return {
        "id": sid, "project_id": "p1", "scene_number": 1,
        "duration": duration, "search_terms": list(terms),
        "visual_direction": "editing desk timeline",
    }


def _stock_results(titles):
    return {"results": [
        {
            "external_id": f"ext{i}", "preview_url": f"pv{i}",
            "title": title, "media_type": "stock_video", "source": "pexels",
        }
        for i, title in enumerate(titles)
    ]}


def _patch_stock(monkeypatch, titles):
    async def fake_search(*_args, **_kwargs):
        return _stock_results(titles)

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", fake_search, raising=False)
    monkeypatch.setattr(api_routes.stock_service, "is_mock_mode", lambda: True)


def _user(uid="user-1"):
    return {"id": uid, "role": "user", "email": f"{uid}@example.com"}


DISTINCT_TITLES = [
    "Video editing timeline interface",
    "Close up of hands typing keyboard desk",
    "Morning coffee ritual relaxing home",
    "Team collaboration whiteboard planning",
]


def test_auto_attach_filters_near_duplicates_and_attaches(monkeypatch):
    # Two near-duplicate pairs: the second "timeline" title (same visual idea
    # as the first) and the repeated "close up" title.
    titles = [
        "Video editing timeline interface",
        "Close up of hands typing keyboard desk",
        "Video editing software timeline",
        "Morning coffee ritual relaxing home",
        "Close up of hands typing keyboard desk",
        "Team collaboration whiteboard planning",
    ]
    _patch_stock(monkeypatch, titles)
    db = FakeDB(_project(), [_scene()])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    response = asyncio.run(api_routes.auto_attach_assets(
        "p1", AutoAttachRequest(replace_existing=False), _user(),
    ))

    assert response["attached"] == 4, response
    assert response["failed"] == 0
    assert response["near_duplicates_skipped"] == 2
    reasons = [d.get("reason") for d in response["details"] if d.get("reason")]
    assert reasons == ["near_duplicate", "near_duplicate"], reasons
    assert set(reason for d in response["details"] if d.get("status") == "failed") == set()
    # A1 regression: filter_distinct_titles must actually run (no NameError).
    titles_seen = [d.get("title") for d in response["details"] if d.get("reason") == "near_duplicate"]
    assert "Video editing software timeline" in titles_seen
    attached_names = [d["asset_id"] for d in response["details"] if d.get("asset_id")]
    assert len(attached_names) == 4


def test_replace_defers_deletion_until_replacement_confirmed(monkeypatch):
    # Regression for A2: no usable results -> the scene keeps its visuals.
    _patch_stock(monkeypatch, [])
    old_asset = {
        "id": "asset-old", "project_id": "p1", "scene_id": "scene-1",
        "asset_type": "stock_video", "external_id": "old1",
        "preview_url": "old-pv", "name": "Old stock clip",
    }
    db = FakeDB(_project(), [_scene()], assets=[old_asset])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    response = asyncio.run(api_routes.auto_attach_assets(
        "p1", AutoAttachRequest(replace_existing=True), _user(),
    ))

    assert response["failed"] == 1
    assert db.assets.delete_calls == [], "originals must not be deleted on a futile search"
    assert db.assets.docs == [old_asset]


def test_replace_replaces_scene_assets_when_replacement_confirmed(monkeypatch):
    _patch_stock(monkeypatch, DISTINCT_TITLES)
    old_asset = {
        "id": "asset-old", "project_id": "p1", "scene_id": "scene-1",
        "asset_type": "stock_video", "external_id": "old1",
        "preview_url": "old-pv", "name": "Old stock clip",
    }
    db = FakeDB(_project(), [_scene()], assets=[old_asset])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    response = asyncio.run(api_routes.auto_attach_assets(
        "p1", AutoAttachRequest(replace_existing=True), _user(),
    ))

    assert response["attached"] == 4
    assert response["failed"] == 0
    assert len(db.assets.delete_calls) == 1
    assert db.assets.delete_calls[0]["scene_id"] == "scene-1"
    assert all(a["id"] != "asset-old" for a in db.assets.docs)
    # Replacement is not blocked by the old asset's identity.
    names = [a["name"] for a in db.assets.docs]
    assert "Old stock clip" not in names


def test_topup_does_not_re_attach_existing_assets(monkeypatch):
    titles = DISTINCT_TITLES + ["Stock charts finance data screen"]
    _patch_stock(monkeypatch, titles)
    existing = {
        "id": "asset-e1", "project_id": "p1", "scene_id": "scene-1",
        "asset_type": "stock_video", "external_id": "ext0",
        "preview_url": "pv0", "name": "Video editing timeline interface",
    }
    db = FakeDB(_project(), [_scene()], assets=[existing])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    response = asyncio.run(api_routes.auto_attach_assets(
        "p1", AutoAttachRequest(replace_existing=False), _user(),
    ))

    assert response["attached"] == 3, response
    assert response["failed"] == 0
    assert len(db.assets.delete_calls) == 0
    assert existing in db.assets.docs
    attached_ext = [a["external_id"] for a in db.assets.docs]
    assert attached_ext.count("ext0") == 1, "top-up must not duplicate the same source asset"
