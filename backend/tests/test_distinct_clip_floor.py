"""Distinct-clip floor: no scene may render on one repeated still.

No provider is contacted and the frame judge is a fake Gemini client.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from backend.app import asset_relevance, render
from backend.app.models import AutoAttachRequest
from backend.app.render import validate_prerequisites
from backend.app.routes import api as api_routes
from backend.app.scene_terms import (
    PER_TERM_JUDGE_BUDGET, clips_to_find, footage_shortfall_message, min_distinct_clips,
)
from backend.app.stock import _normalise_pixabay_video, search_stock_aggregated
from backend.tests.test_auto_attach_assets import PID, _balance, _db, _patch_stock, _scene, _stock, _user


def test_min_distinct_clips_is_one_per_four_seconds_and_at_least_three():
    assert min_distinct_clips(0) == 3
    assert min_distinct_clips(None) == 3
    assert min_distinct_clips(-4) == 3
    assert min_distinct_clips(8) == 3
    assert min_distinct_clips(12) == 3
    assert min_distinct_clips(12.1) == 4
    assert min_distinct_clips(28) == 7
    assert min_distinct_clips(28.27) == 8
    # The search still aims at about one shot every 3 seconds.
    assert clips_to_find(10) == 4
    assert clips_to_find(28.27) == 10
    assert footage_shortfall_message(2, 1, 8) == (
        "Scene 2 has 1 of 8 required distinct clips - run Find footage for scene 2."
    )


def _clip(external_id: str, title: str) -> dict:
    return {
        "external_id": external_id,
        "preview_url": f"https://stock.example/{external_id}.jpg",
        "download_url": f"https://stock.example/{external_id}.mp4",
        "source_url": f"https://stock.example/watch/{external_id}",
        "title": title,
        "media_type": "stock_video",
        "source": "pexels",
        "visual_relevance": {"accepted": True, "score": 90, "reason": "subject visible"},
    }


def _egg_scene():
    scene = _scene(
        duration=28.27,
        terms=("poached egg water", "fried egg pan", "scrambled eggs pan", "omelette pan"),
    )
    scene["narration_text"] = (
        "Poach the egg, fry it with the lid on, scramble it, then fold an omelette."
    )
    scene["visual_direction"] = "eggs cooking in a pan"
    return scene


def test_judge_budget_is_spread_across_every_scene_term_before_escalating(monkeypatch):
    scene = _egg_scene()
    db = _db(scenes=[scene], credits=40, project={
        "id": PID, "user_id": "user-1", "topic": "Every way to cook eggs",
        "niche": "cooking", "status": "SCENES_READY",
    })
    searches: list[str] = []
    judged: list[str] = []

    async def search(*_args, **kwargs):
        query = str(kwargs.get("query") or "")
        searches.append(query)
        page = int(kwargs.get("page") or 1)
        if page > 1:
            return {"mock": False, "results": []}
        return {"mock": False, "results": [
            _clip(f"{query}-{i}", f"{query} plate {i}") for i in range(12)
        ]}

    async def select(candidates, _scene, query, *, needed, renew=None):
        judged.extend([query] * len(candidates))
        rejected = [{**item, "reject_reason": "vision_rejected"} for item in candidates]
        return [], rejected

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", search, raising=False)
    monkeypatch.setattr(api_routes.stock_service, "is_mock_mode", lambda: True)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(api_routes, "select_footage_candidates", select)
    monkeypatch.setattr(api_routes, "relevant_candidates", lambda candidates, _scene, _query: (candidates, []))
    monkeypatch.setenv("FF_MAX_API_REQUESTS_PER_OPERATION", "80")

    asyncio.run(api_routes.auto_attach_assets(
        PID, AutoAttachRequest(replace_existing=False, idempotency_key="spread-terms"), _user(),
    ))

    # Compaction drops camera-noise words such as "pan", so the provider sees
    # the scene subjects, not the raw search-term strings.
    subjects = ("poached", "fried", "scrambled", "omelette")
    assert all(any(subject in query for query in searches) for subject in subjects)
    scene_queries = [query for query in searches if any(subject in query for subject in subjects)]
    counts: dict[str, int] = {}
    seen_before_ninth: set[str] = set()
    for query in judged:
        if query not in scene_queries:
            continue
        counts[query] = counts.get(query, 0) + 1
        if counts[query] == PER_TERM_JUDGE_BUDGET + 1:
            break
        seen_before_ninth.add(query)
    assert len(seen_before_ninth) >= 4
    # No scene term is judged a ninth time before the others have had a turn.
    assert all(count >= 1 for count in counts.values())


def test_search_escalates_until_the_minimum_is_met(monkeypatch):
    scene = _egg_scene()
    db = _db(scenes=[scene], credits=40, project={
        "id": PID, "user_id": "user-1", "topic": "Every way to cook eggs",
        "niche": "cooking", "status": "SCENES_READY",
    })
    searches: list[str] = []

    async def search(*_args, **kwargs):
        query = str(kwargs.get("query") or "")
        searches.append(query)
        if "kitchen" not in query:
            return {"mock": False, "results": []}
        titles = [
            "Whisking yolks in a glass bowl",
            "Butter foaming beside chopped chives",
            "Steam rising over a copper saucepan",
            "Folding a yellow omelette with a spatula",
            "Cracking a shell on a wooden board",
            "Pouring cream into beaten yolks",
            "Flipping a fried egg with a fish slice",
            "Grating cheese over a hot skillet",
            "Slicing mushrooms on a marble counter",
            "Lifting a poached egg from simmering water",
        ]
        return {"mock": False, "results": [
            _clip(f"kitchen-{i}", title) for i, title in enumerate(titles)
        ]}

    async def select(candidates, _scene, query, *, needed, renew=None):
        return list(candidates)[:needed], []

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", search, raising=False)
    monkeypatch.setattr(api_routes.stock_service, "is_mock_mode", lambda: True)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(api_routes, "select_footage_candidates", select)
    monkeypatch.setattr(api_routes, "relevant_candidates", lambda candidates, _scene, _query: (candidates, []))
    monkeypatch.setenv("FF_MAX_API_REQUESTS_PER_OPERATION", "80")

    response = asyncio.run(api_routes.auto_attach_assets(
        PID, AutoAttachRequest(replace_existing=False, idempotency_key="escalate-floor"), _user(),
    ))

    assert response["attached"] >= min_distinct_clips(28.27), response
    assert "kitchen cooking" in searches
    assert searches.index("poached egg water") < searches.index("kitchen cooking")
    assert len({row["external_id"] for row in db.assets.docs}) >= 8


def test_replace_does_not_shrink_below_the_old_set_or_the_floor(monkeypatch):
    originals = [_stock(f"asset-{i}", f"old-{i}", f"Old breakfast clip {i}") for i in range(9)]
    scene = _egg_scene()
    db = _db(assets=originals, credits=40, scenes=[scene], project={
        "id": PID, "user_id": "user-1", "topic": "Every way to cook eggs",
        "niche": "cooking", "status": "SCENES_READY",
    })
    _patch_stock(monkeypatch, db, ["One poached egg photo"])

    response = asyncio.run(api_routes.auto_attach_assets(
        PID, AutoAttachRequest(replace_existing=True, idempotency_key="replace-keep-nine"), _user(),
    ))

    assert [row["id"] for row in db.assets.docs] == [row["id"] for row in originals]
    original_ids = {row["id"] for row in originals}
    for call in db.assets.delete_calls:
        removed = set((call.get("id") or {}).get("$in") or [])
        assert not removed & original_ids
    assert response["attached"] == 0
    assert _balance(db) == 40
    reasons = [detail.get("reason") for detail in response["details"]]
    assert "replacement_below_minimum" in reasons


def test_pexels_videos_survive_a_full_photo_page(monkeypatch):
    photos = [
        {"source": "pexels", "external_id": f"photo-{i}", "media_type": "stock_image",
         "preview_url": f"https://images.example/p{i}.jpg", "download_url": f"https://images.example/p{i}.jpg"}
        for i in range(40)
    ]
    videos = [
        {"source": "pexels", "external_id": f"video-{i}", "media_type": "stock_video",
         "preview_url": f"https://images.example/v{i}.jpg", "download_url": f"https://images.example/v{i}.mp4"}
        for i in range(40)
    ]

    async def pexels(*_args, **_kwargs):
        return photos + videos

    monkeypatch.setattr("backend.app.stock._search_pexels", pexels)
    monkeypatch.setattr("backend.app.stock._LAST_ASSET_IDS", set())
    monkeypatch.setattr("backend.app.stock._LAST_ASSET_IDS_ORDER", [])
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
    monkeypatch.setenv("USE_MOCK_PEXELS", "false")

    result = asyncio.run(search_stock_aggregated("fried egg pan", media_type="both", per_page=40))
    ids = [item["external_id"] for item in result["results"]]
    assert "video-0" in ids
    assert ids.index("video-0") < ids.index("photo-0")
    assert sum(item["media_type"] == "stock_video" for item in result["results"]) == 40


def test_pixabay_portrait_keeps_portrait_and_square_videos():
    def hit(width, height, ident):
        return {
            "id": ident,
            "videos": {"large": {"url": f"https://cdn.example/{ident}.mp4", "width": width, "height": height}},
            "duration": 8,
            "tags": "eggs frying",
            "user": "chef",
        }

    assert _normalise_pixabay_video(hit(1080, 1920, "portrait"), "frying", orientation="portrait")
    assert _normalise_pixabay_video(hit(1080, 1080, "square"), "frying", orientation="portrait")
    assert _normalise_pixabay_video(hit(1920, 1080, "wide"), "frying", orientation="portrait") is None
    assert "9:16" in inspect.getsource(render._topup_scene_visuals)


def test_judge_prompt_has_no_leftover_examples_and_accepts_a_frying_narration(monkeypatch):
    prompt = asset_relevance.frame_judge_prompt(
        "Fry the eggs in the pan and keep the lid on until the whites set.",
        "poached egg swirling water vinegar",
    )
    for leftover in ("outdoor kettle", "ink pour", "oranges", "scoreboard", "does not match a kettle"):
        assert leftover not in prompt
    assert "Fry the eggs" in prompt
    assert "poached egg swirling water vinegar" in prompt
    assert asset_relevance.MIN_RELEVANCE_SCORE == 70

    captured = {}

    class Models:
        async def generate_content(self, *, model, contents, config):
            captured["contents"] = contents

            class Response:
                text = json.dumps({
                    "same_subject": True,
                    "score": 88,
                    "reason": "eggs frying in a pan",
                })

            return Response()

    class Client:
        def __init__(self, api_key=None):
            self.aio = type("Aio", (), {"models": Models()})()

    monkeypatch.setenv("FF_ALLOW_PAID_API_CALLS", "1")
    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "test-key")
    monkeypatch.setattr("google.genai.Client", Client)
    verdict = asyncio.run(asset_relevance.judge_frame(
        b"\xff\xd8fake-frying-frame",
        "image/jpeg",
        "Fry the eggs in the pan and keep the lid on until the whites set.",
        "poached egg swirling water vinegar",
    ))
    sent = captured["contents"][1]
    assert "Fry the eggs" in sent
    assert "outdoor kettle" not in sent
    assert verdict["accepted"] is True
    assert verdict["score"] == 88

    class RejectModels:
        async def generate_content(self, *, model, contents, config):
            class Response:
                text = json.dumps({"same_subject": True, "score": 69, "reason": "close"})
            return Response()

    class RejectClient:
        def __init__(self, api_key=None):
            self.aio = type("Aio", (), {"models": RejectModels()})()

    monkeypatch.setattr("google.genai.Client", RejectClient)
    low = asyncio.run(asset_relevance.judge_frame(
        b"\xff\xd8fake-frying-frame", "image/jpeg", "Fry the eggs.", "fried egg pan",
    ))
    assert low["accepted"] is False
    assert low["score"] == 69


def _ready_project():
    return {
        "id": "egg", "target_duration": 136, "selected_voiceover_asset_id": "v1",
        "selected_thumbnail_asset_id": "t1",
    }


def test_gate_names_a_one_clip_scene_and_ignores_borrowed_clips():
    duration = 28.27
    scenes = [
        {"id": "s1", "scene_number": 1, "duration": duration},
        {"id": "s2", "scene_number": 2, "duration": duration},
    ]
    assets = [
        {"id": "t1", "asset_type": "generated_thumbnail"},
        {"id": "v1", "asset_type": "voiceover_audio", "duration": 132.0, "status": "selected"},
    ]
    for index in range(8):
        assets.append({
            "id": f"own-{index}", "asset_type": "stock_image", "scene_id": "s1",
            "source": "pexels", "external_id": f"scene1-{index}",
            "download_url": f"https://stock.example/s1-{index}.jpg", "status": "attached",
        })
    assets.append({
        "id": "only", "asset_type": "stock_image", "scene_id": "s2", "source": "pexels",
        "external_id": "poached-only", "download_url": "https://stock.example/poached.jpg",
        "status": "attached",
    })
    # The same provider id attached twice is still one clip.
    assets.append({
        "id": "repeat", "asset_type": "stock_image", "scene_id": "s2", "source": "pexels",
        "external_id": "poached-only", "download_url": "https://stock.example/poached.jpg",
        "status": "attached",
    })
    check = validate_prerequisites(_ready_project(), {"full_script": "eggs " * 40}, scenes, {"id": "m"}, assets)
    pacing = next(item for item in check["checklist"] if item["key"] == "scene_assets")
    sentence = "Scene 2 has 1 of 8 required distinct clips - run Find footage for scene 2."
    assert pacing["ok"] is False
    assert sentence in pacing["hint"]
    assert sentence in check["issues"]
    assert check["ok"] is False
    assert "Scene 1 has" not in " ".join(check["issues"])


def test_render_fails_after_download_instead_of_padding_with_reuse():
    scene = {"id": "s2", "scene_number": 2, "duration": 28.27}
    with pytest.raises(RuntimeError, match="Scene 2 has 1 of 8 required distinct clips - run Find footage for scene 2."):
        render._require_downloaded_clip_floor(
            [([(Path("only.jpg"), "image")], scene)],
            [1],
        )
    render._require_downloaded_clip_floor(
        [([(Path(f"{i}.jpg"), "image") for i in range(8)], scene)],
        [8],
    )


@pytest.mark.asyncio
async def test_slow_download_stops_at_the_deadline_and_retries_once(monkeypatch, tmp_path):
    assert render.DOWNLOAD_DEADLINE_SECONDS == 30.0
    monkeypatch.setattr(render, "DOWNLOAD_DEADLINE_SECONDS", 0.15)
    calls: list[str] = []

    class Slow:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        async def aiter_bytes(self, chunk_size=65536):
            yield b"\xff\xd8\xff"
            await asyncio.sleep(5)

    @asynccontextmanager
    async def slow_stream(_client, url):
        calls.append(str(url))
        yield Slow()

    monkeypatch.setattr("backend.app.public_http.public_stream", slow_stream)
    started = time.monotonic()
    ok = await render._download_to(
        "https://images.pexels.com/photos/1/photo.jpg?w=1200",
        tmp_path / "hung.jpg",
        max_bytes=5_000_000,
    )
    elapsed = time.monotonic() - started
    assert ok is False
    assert len(calls) == 2
    assert "w=640" in calls[1] or "w=600" in calls[1]
    assert elapsed < 2
    assert not (tmp_path / "hung.jpg").exists()
