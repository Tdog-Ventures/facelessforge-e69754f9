"""Auto-attach must keep searching after the first page of known or rejected clips."""
from __future__ import annotations

import asyncio

from backend.app.asset_relevance import select_footage_candidates
from backend.app.models import AutoAttachRequest
from backend.app.routes import api as api_routes
from backend.tests.test_auto_attach_assets import PID, _db, _scene, _stock, _user


def _clip(external_id: str, title: str) -> dict:
    return {
        "external_id": external_id,
        "preview_url": f"https://stock.example/{external_id}.jpg",
        "title": title,
        "download_url": f"https://stock.example/{external_id}.mp4",
        "source_url": f"https://stock.example/watch/{external_id}",
        "media_type": "stock_video",
        "source": "pexels",
    }


def _bind(monkeypatch, db, search, select):
    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", search, raising=False)
    monkeypatch.setattr(api_routes.stock_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(api_routes, "relevant_candidates", lambda candidates, _scene, _query: (list(candidates), []))
    monkeypatch.setattr(api_routes, "select_footage_candidates", select)


def test_rejected_first_page_does_not_hide_the_next_page(monkeypatch):
    """A scene that still needs pictures must judge the next provider page."""
    db = _db(credits=20, scenes=[_scene(duration=12.0)])
    pages: list[int] = []

    async def search(*_args, **kwargs):
        page = int(kwargs.get("page") or 1)
        pages.append(page)
        if page == 1:
            return {"mock": False, "results": [
                _clip(f"stale-{i}", f"Unrelated skyline aerial {i}") for i in range(8)
            ]}
        titles = [
            "Harbor crane loading cargo",
            "Mountain river kayak paddle",
            "Library reading lamp desk",
            "Violin bow across strings",
        ]
        return {"mock": False, "results": [
            _clip(f"fresh-{i}", titles[i]) for i in range(4)
        ]}

    async def select(candidates, _scene, _query, *, needed, renew=None):
        fresh = [item for item in candidates if str(item.get("external_id") or "").startswith("fresh")]
        rejected = [
            {**item, "reject_reason": "vision_rejected"}
            for item in candidates
            if not str(item.get("external_id") or "").startswith("fresh")
        ]
        return fresh[:needed], rejected

    _bind(monkeypatch, db, search, select)
    response = asyncio.run(api_routes.auto_attach_assets(
        PID, AutoAttachRequest(replace_existing=False, idempotency_key="page-depth"), _user(),
    ))

    assert 2 in pages, pages
    assert response["attached"] == 4, response
    assert response["failed"] == 0, response
    assert response["credits_charged"] == 4
    assert {row["external_id"] for row in db.assets.docs} == {f"fresh-{i}" for i in range(4)}


def test_clips_already_on_the_scene_do_not_consume_the_first_page(monkeypatch):
    existing = [_stock(f"asset-{i}", f"known-{i}", f"Known clip {i}") for i in range(6)]
    db = _db(assets=existing, credits=20, scenes=[_scene(duration=30.0)])
    pages: list[int] = []

    async def search(*_args, **kwargs):
        page = int(kwargs.get("page") or 1)
        pages.append(page)
        if page == 1:
            return {"mock": False, "results": [
                _clip(f"known-{i}", f"Known clip {i}") for i in range(6)
            ]}
        titles = [
            "Harbor crane loading cargo",
            "Mountain river kayak paddle",
            "Library reading lamp desk",
            "Violin bow across strings",
        ]
        return {"mock": False, "results": [
            _clip(f"fresh-{i}", titles[i]) for i in range(4)
        ]}

    async def select(candidates, _scene, _query, *, needed, renew=None):
        return list(candidates)[:needed], []

    _bind(monkeypatch, db, search, select)
    response = asyncio.run(api_routes.auto_attach_assets(
        PID, AutoAttachRequest(replace_existing=False, idempotency_key="known-page"), _user(),
    ))

    assert pages[0] == 1 and 2 in pages, pages
    assert response["attached"] == 4, response
    assert response["failed"] == 0, response
    assert response["credits_charged"] == 4
    stored = [row["external_id"] for row in db.assets.docs]
    assert stored.count("known-0") == 1
    assert {f"fresh-{i}" for i in range(4)} <= set(stored)


def test_judge_continues_until_the_scene_gap_is_filled(monkeypatch):
    calls = {"n": 0}

    async def inspect(candidate, _scene, _query):
        calls["n"] += 1
        if calls["n"] <= 20:
            return {**candidate, "reject_reason": "vision_rejected"}
        return {
            **candidate,
            "visual_relevance": {"accepted": True, "score": 90, "reason": "coffee cherries on the branch"},
        }

    monkeypatch.setattr("backend.app.asset_relevance._inspect_one", inspect)
    candidates = [
        _clip(f"c{i}", f"coffee cherries branch {i}") | {"media_type": "stock_image"}
        for i in range(30)
    ]
    scene = {
        "search_terms": ["coffee cherries"],
        "visual_direction": "coffee cherries on a branch",
        "narration_text": "Pick the red coffee cherries.",
    }
    accepted, _rejected = asyncio.run(select_footage_candidates(
        candidates, scene, "coffee cherries", needed=10,
    ))
    assert len(accepted) == 10
    assert calls["n"] == 30
