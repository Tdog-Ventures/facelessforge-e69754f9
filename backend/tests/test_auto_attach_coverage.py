"""Auto-attach covers a scene by duration, not by a fixed clip count."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from backend.app.models import AutoAttachRequest
from backend.app.render import _ffmpeg_normalise_image, _ffmpeg_normalise_video, validate_prerequisites
from backend.app.routes import api as api_routes
from backend.app.scene_terms import broaden_stock_queries, plan_hold_durations
from backend.tests.fake_db import FakeCollection, FakeDatabase
from backend.tests.test_auto_attach_assets import (
    PID, UID, _balance, _project, _scene, _unique_stock_index, _user,
)


def test_hold_durations_sum_to_the_scene_and_one_clip_holds_the_whole_scene():
    holds = plan_hold_durations(28.833333, 5)
    assert len(holds) == 5
    assert sum(holds) == pytest.approx(28.833333, abs=0.01)
    assert min(holds) > 3
    assert plan_hold_durations(22.5, 1) == [22.5]
    assert plan_hold_durations(10, 0) == []


def test_egg_scene_broadens_from_the_action_to_the_kitchen():
    scene = {
        "narration_text": "Crack the eggs into the hot pan and scramble them until just set.",
        "visual_direction": "close-up eggs scrambling in a buttered frying pan",
        "search_terms": ["eggs scrambling frying pan"],
    }
    project = {"topic": "Every way to cook eggs", "niche": "cooking"}
    queries = broaden_stock_queries(scene, project, "eggs scrambling frying pan")
    assert queries[0].startswith("eggs scrambling")
    assert "eggs cooking" in queries
    assert queries[-1] == "kitchen cooking"
    assert len(queries) == len({q.casefold() for q in queries})


def test_unrelated_topic_does_not_fall_back_to_a_kitchen():
    queries = broaden_stock_queries(
        {"narration_text": "A quiet library desk at night", "search_terms": ["library desk lamp"]},
        {"topic": "Study habits", "niche": "education"},
        "library desk lamp",
    )
    assert "kitchen cooking" not in queries
    assert any("education" in q or "study" in q or q == "everyday life" for q in queries)


def _db(scene, credits=10):
    return FakeDatabase(
        projects=FakeCollection("projects", [_project(topic="Every way to cook eggs", niche="cooking")]),
        scenes=FakeCollection("scenes", [scene]),
        assets=FakeCollection("assets", [], unique=_unique_stock_index()),
        subscriptions=FakeCollection("subscriptions", [{"user_id": UID, "credits_remaining": credits, "credits_monthly": 30}]),
        ff_credits=FakeCollection("ff_credits", []),
        credit_transactions=FakeCollection("credit_transactions"),
        asset_operations=FakeCollection("asset_operations"),
    )


def _clip(ext, title, query_match):
    return {
        "external_id": ext, "preview_url": f"pv-{ext}", "title": title,
        "download_url": f"https://stock.example/{ext}.mp4",
        "source_url": f"https://stock.example/{ext}",
        "media_type": "stock_video", "source": "pexels", "query_match": query_match,
    }


def test_few_results_cover_the_scene_and_broader_queries_fill_gaps(monkeypatch):
    scene = _scene(
        duration=28.833333,
        terms=("eggs scrambling frying pan",),
    )
    scene["narration_text"] = "Crack the eggs into the hot pan and scramble them."
    scene["visual_direction"] = "eggs scrambling in a frying pan"
    db = _db(scene)
    seen = []

    async def search(*_args, **kwargs):
        query = str(kwargs.get("query") or "")
        seen.append(query)
        if "kitchen" in query:
            return {"results": [_clip("kitchen-1", "chef in a bright kitchen cooking", "kitchen")]}
        if "scrambling" in query or "frying" in query:
            return {"results": [_clip("egg-1", "eggs scrambling in a frying pan", "specific")]}
        return {"results": []}

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", search, raising=False)
    monkeypatch.setattr(api_routes.stock_service, "is_mock_mode", lambda: True)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    response = asyncio.run(api_routes.auto_attach_assets(
        PID, AutoAttachRequest(replace_existing=False), _user(),
    ))

    assert response["failed"] == 0, response
    assert response["partial"] == 1
    assert response["attached"] == 2
    assert "scrambling" in seen[0]
    assert "kitchen cooking" in seen
    assert seen.index(seen[0]) < seen.index("kitchen cooking")
    rows = sorted(db.assets.docs, key=lambda row: row["start_offset"])
    assert [row["external_id"] for row in rows] == ["egg-1", "kitchen-1"]
    assert [row["start_offset"] for row in rows] == [0.0, round(28.833333 / 2, 3)]
    assert sum(row["duration"] for row in rows) == pytest.approx(28.833333, abs=0.01)
    assert _balance(db) == 8


def test_duplicate_clip_across_scenes_is_skipped_and_does_not_500(monkeypatch):
    scenes = [
        _scene("scene-1", 1, duration=12, terms=("eggs scrambling frying pan",)),
        _scene("scene-2", 2, duration=12, terms=("boiling eggs in a pot",)),
    ]
    scenes[0]["narration_text"] = "Scramble the eggs in the pan."
    scenes[1]["narration_text"] = "Boil the eggs in a pot."
    db = FakeDatabase(
        projects=FakeCollection("projects", [_project(topic="eggs cooking", niche="cooking")]),
        scenes=FakeCollection("scenes", scenes),
        assets=FakeCollection("assets", [], unique=_unique_stock_index()),
        subscriptions=FakeCollection("subscriptions", [{"user_id": UID, "credits_remaining": 10, "credits_monthly": 30}]),
        ff_credits=FakeCollection("ff_credits", []),
        credit_transactions=FakeCollection("credit_transactions"),
        asset_operations=FakeCollection("asset_operations"),
    )

    async def search(*_args, **kwargs):
        query = str(kwargs.get("query") or "")
        if "boiling" in query or "pot" in query:
            return {"results": [
                _clip("shared-egg", "eggs scrambling in a frying pan", "dup"),
                _clip("pot-1", "eggs boiling in a metal pot", "scene2"),
            ]}
        return {"results": [_clip("shared-egg", "eggs scrambling in a frying pan", "scene1")]}

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", search, raising=False)
    monkeypatch.setattr(api_routes.stock_service, "is_mock_mode", lambda: True)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    response = asyncio.run(api_routes.auto_attach_assets(
        PID, AutoAttachRequest(replace_existing=False), _user(),
    ))

    assert response["failed"] == 0, response
    ids_by_scene = {}
    for row in db.assets.docs:
        ids_by_scene.setdefault(row["scene_id"], []).append(row["external_id"])
    assert ids_by_scene["scene-1"] == ["shared-egg"]
    assert "shared-egg" not in ids_by_scene["scene-2"]
    assert "pot-1" in ids_by_scene["scene-2"]
    assert sum(row["duration"] for row in db.assets.docs if row["scene_id"] == "scene-1") == pytest.approx(12)


def test_no_footage_is_the_only_failure(monkeypatch):
    db = _db(_scene(duration=20, terms=("eggs scrambling frying pan",)))

    async def search(*_args, **kwargs):
        return {"results": []}

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", search, raising=False)
    monkeypatch.setattr(api_routes.stock_service, "is_mock_mode", lambda: True)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    response = asyncio.run(api_routes.auto_attach_assets(
        PID, AutoAttachRequest(replace_existing=False), _user(),
    ))
    assert response["failed"] == 1
    assert response["attached"] == 0
    assert db.assets.docs == []


def test_preflight_accepts_holds_that_cover_the_scene():
    project = {"id": PID, "target_duration": 29, "selected_voiceover_asset_id": "v1", "selected_thumbnail_asset_id": "t1"}
    scenes = [{"id": "scene-1", "scene_number": 1, "duration": 28.8, "start_time": 0, "end_time": 28.8}]
    assets = [
        {"id": "t1", "asset_type": "generated_thumbnail"},
        {"id": "v1", "asset_type": "voiceover_audio", "duration": 28.8, "status": "selected"},
        {"id": "a", "asset_type": "stock_video", "scene_id": "scene-1", "source": "pexels", "duration": 14.4, "status": "attached"},
        {"id": "b", "asset_type": "stock_video", "scene_id": "scene-1", "source": "pexels", "duration": 14.4, "status": "attached"},
    ]
    check = validate_prerequisites(project, {"full_script": "eggs " * 40}, scenes, {"id": "m"}, assets)
    pacing = next(item for item in check["checklist"] if item["key"] == "scene_assets")
    assert pacing["ok"] is True


def test_short_video_and_still_are_looped_for_a_longer_hold():
    video = _ffmpeg_normalise_video(Path("short.mp4"), 14.4, Path("out.mp4"))
    assert video[video.index("-stream_loop") + 1] == "-1"
    assert "14.40" in video
    image = _ffmpeg_normalise_image(Path("still.jpg"), 14.4, Path("out.mp4"))
    assert image[image.index("-loop") + 1] == "1"
    assert "14.40" in image


def test_ollama_logs_the_url_and_falls_through_when_localhost_is_unreachable(monkeypatch, caplog):
    from app import generation as gen

    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class Refuse:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, json=None):
            raise ConnectionError("All connection attempts failed")

    monkeypatch.setattr(gen.httpx, "AsyncClient", Refuse)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(gen._llm_json("system", "user", "scenes-egg")) is None
    text = caplog.text
    assert "http://127.0.0.1:11434/api/generate" in text
    assert "host.docker.internal" in text
    assert "scenes-egg" in text
