"""The renderer must use saved attachments in order and recover from a bad download."""
import asyncio

from app.render import _resolve_scene_visual


def test_renderer_uses_next_saved_asset_after_a_failed_download(tmp_path, monkeypatch):
    searches = {"count": 0}

    async def fake_download(url, out_path, max_bytes, allow_audio=False):
        if "dead.example" in url:
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-video")
        return True

    async def fake_motion(path):
        return True

    async def fail_search(*args, **kwargs):
        searches["count"] += 1
        raise AssertionError("renderer searched for replacement footage")

    monkeypatch.setattr("app.render._download_to", fake_download)
    monkeypatch.setattr("app.render._video_has_motion", fake_motion)
    monkeypatch.setattr("app.render.stock_service.search_stock", fail_search)

    scene = {"id": "scene-a", "narration_text": "steam rises from the kettle"}
    assets = [
        {
            "scene_id": "scene-a",
            "asset_type": "stock_video",
            "external_id": "dead-1",
            "download_url": "http://dead.example/missing.mp4",
        },
        {
            "scene_id": "scene-a",
            "asset_type": "stock_video",
            "external_id": "good-1",
            "download_url": "http://cdn.example/good-1.mp4",
        },
        {
            "scene_id": "scene-a",
            "asset_type": "stock_video",
            "external_id": "good-2",
            "download_url": "http://cdn.example/good-2.mp4",
        },
    ]

    async def run():
        used = set()
        first = await _resolve_scene_visual(
            scene, assets, {}, tmp_path, 0, used_ext_ids=used, allow_fallback=True,
        )
        used.add(str(first[2]))
        second = await _resolve_scene_visual(
            scene, assets, {}, tmp_path, 1, used_ext_ids=used, allow_fallback=True,
        )
        return first, second

    first, second = asyncio.run(run())
    assert first[1] == "video"
    assert first[2] == "good-1"
    assert second[2] == "good-2"
    assert searches["count"] == 0


def test_unusable_saved_assets_fail_closed_when_fallback_is_off(tmp_path, monkeypatch):
    searches = {"count": 0}

    async def fake_download(url, out_path, max_bytes, allow_audio=False):
        return False

    async def fake_motion(path):
        return True

    async def fail_search(*args, **kwargs):
        searches["count"] += 1
        raise AssertionError("renderer searched for replacement footage")

    monkeypatch.setattr("app.render._download_to", fake_download)
    monkeypatch.setattr("app.render._video_has_motion", fake_motion)
    monkeypatch.setattr("app.render.stock_service.search_stock", fail_search)

    scene = {"id": "scene-a", "narration_text": "steam rises from the kettle"}
    assets = [
        {
            "scene_id": "scene-a",
            "asset_type": "stock_video",
            "external_id": "dead-1",
            "download_url": "http://dead.example/one.mp4",
        },
        {
            "scene_id": "scene-a",
            "asset_type": "stock_video",
            "external_id": "dead-2",
            "download_url": "http://dead.example/two.mp4",
        },
    ]

    async def run():
        await _resolve_scene_visual(
            scene, assets, {}, tmp_path, 0, used_ext_ids=set(), allow_fallback=False,
        )

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("resolver returned footage after every download failed")
    assert "footage: all candidates rejected" in message
    assert "no stock substitution is permitted" not in message
    assert searches["count"] == 0

"""Semantic acceptance: overlap is not footage, and a bad file is not a charge."""
import asyncio

from app.asset_relevance import candidate_is_relevant
from app.models import AutoAttachRequest
from app.routes import api as api_routes
from tests.fake_db import FakeCollection, FakeDatabase


def test_incidental_one_token_overlap_is_rejected():
    scene = {
        "search_terms": ["orange on the kitchen counter"],
        "narration_text": "An orange sits beside the empty cup.",
    }
    scoreboard = {
        "source": "pexels",
        "external_id": "board",
        "download_url": "https://cdn.example/board.mp4",
        "title": "Orange digital scoreboard",
        "source_url": "https://www.pexels.com/video/orange-scoreboard-board/",
    }
    assert not candidate_is_relevant(scoreboard, scene, "orange on the kitchen counter")
    kettle = {
        "source": "pexels",
        "external_id": "kettle",
        "download_url": "https://cdn.example/kettle.mp4",
        "title": "Kettle",
        "source_url": "https://www.pexels.com/video/kettle-1/",
        "visual_relevance": {"accepted": True, "score": 88, "reason": "boiling kettle"},
    }
    assert candidate_is_relevant(
        kettle,
        {"narration_text": "The kitchen kettle steams on the counter.", "search_terms": ["boiling kettle steam"]},
        "boiling kettle steam",
    )
    unsure = dict(kettle)
    unsure["visual_relevance"] = {"accepted": True, "score": 40, "reason": "maybe a kettle"}
    assert not candidate_is_relevant(unsure, scene, "boiling kettle steam")
    gull = dict(kettle)
    gull["visual_relevance"] = {"accepted": False, "score": 96, "reason": "gull over ocean"}
    assert not candidate_is_relevant(gull, scene, "boiling kettle steam")


def _attach_db():
    project = {"id": "p1", "user_id": "user-1", "topic": "kitchen kettle", "status": "SCENES_READY"}
    scene = {
        "id": "scene-1", "project_id": "p1", "scene_number": 1, "duration": 3.0,
        "search_terms": ["boiling kettle steam"],
        "visual_direction": "boiling kettle steam",
        "narration_text": "The kitchen kettle steams on the counter.",
    }
    return FakeDatabase(
        projects=FakeCollection("projects", [project]),
        scenes=FakeCollection("scenes", [scene]),
        assets=FakeCollection("assets", [], unique=[
            (("project_id", "scene_id", "external_id", "source"), lambda d: isinstance(d.get("external_id"), str)),
        ]),
        subscriptions=FakeCollection("subscriptions", [{
            "user_id": "user-1", "credits_remaining": 5, "credits_monthly": 30,
        }]),
        ff_credits=FakeCollection("ff_credits", []),
        credit_transactions=FakeCollection("credit_transactions"),
        asset_operations=FakeCollection("asset_operations"),
    )


def _candidate(url="https://cdn.example/kettle.mp4"):
    return {
        "external_id": "k1",
        "preview_url": "https://cdn.example/kettle.jpg",
        "download_url": url,
        "title": "Boiling kettle steam",
        "source": "pexels",
        "media_type": "stock_video",
        "source_url": "https://www.pexels.com/video/boiling-kettle-steam-k1/",
    }


def _bind(monkeypatch, db, *, download, probe=None, judge=None):
    async def search(*_args, **_kwargs):
        return {"results": [_candidate()]}

    monkeypatch.setattr(api_routes.stock_service, "search_stock_aggregated", search, raising=False)
    monkeypatch.setattr(api_routes.stock_service, "is_mock_mode", lambda: False)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr("app.asset_relevance.download_candidate", download)
    if probe is not None:
        monkeypatch.setattr("app.asset_relevance.probe_stock_video", probe)
    if judge is not None:
        monkeypatch.setattr("app.asset_relevance.judge_frame", judge)


def _run(db):
    user = {"id": "user-1", "role": "user"}
    body = AutoAttachRequest(replace_existing=False, media_type="videos")
    return asyncio.run(api_routes.auto_attach_assets("p1", body, user))


def test_failed_download_inserts_nothing_and_debits_nothing(monkeypatch):
    db = _attach_db()
    probes = {"n": 0}

    async def download(_url, _dest):
        return False

    async def probe(_path):
        probes["n"] += 1
        return {"width": 640, "height": 360, "duration": 2.0, "file_size": 9000}

    _bind(monkeypatch, db, download=download, probe=probe)
    response = _run(db)
    assert response["attached"] == 0
    assert response["credits_charged"] == 0
    assert db.assets.docs == []
    assert db.subscriptions.docs[0]["credits_remaining"] == 5
    assert db.credit_transactions.docs == []
    assert probes["n"] == 0
    assert any(item.get("reason") == "download_failed" for item in response["details"])


def test_failed_probe_inserts_nothing_and_debits_nothing(monkeypatch, tmp_path):
    db = _attach_db()
    judged = {"n": 0}

    async def download(_url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 9000)
        return True

    async def judge(*_args, **_kwargs):
        judged["n"] += 1
        return {"accepted": True, "score": 99, "reason": "should not run"}

    _bind(monkeypatch, db, download=download, judge=judge)
    response = _run(db)
    assert response["attached"] == 0
    assert db.assets.docs == []
    assert db.subscriptions.docs[0]["credits_remaining"] == 5
    assert db.credit_transactions.docs == []
    assert judged["n"] == 0
    assert any(item.get("reason") == "probe_failed" for item in response["details"])


def test_vision_rejection_inserts_nothing_and_debits_nothing(monkeypatch):
    db = _attach_db()

    async def download(_url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 9000)
        return True

    async def probe(_path):
        return {"width": 640, "height": 360, "duration": 4.5, "file_size": 9000}

    async def judge(*_args, **_kwargs):
        raise RuntimeError("vision backend down")

    _bind(monkeypatch, db, download=download, probe=probe, judge=judge)
    response = _run(db)
    assert response["attached"] == 0
    assert response["credits_charged"] == 0
    assert db.assets.docs == []
    assert db.subscriptions.docs[0]["credits_remaining"] == 5
    assert db.credit_transactions.docs == []
    assert any(item.get("reason") == "vision_failed" for item in response["details"])


def test_same_subject_frame_is_stored_with_probe_and_one_debit(monkeypatch):
    db = _attach_db()

    async def download(_url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 9000)
        return True

    async def probe(_path):
        return {"width": 720, "height": 1280, "duration": 4.5, "file_size": 12000}

    async def judge(*_args, **_kwargs):
        return {"accepted": True, "score": 91, "reason": "kettle steam"}

    _bind(monkeypatch, db, download=download, probe=probe, judge=judge)
    response = _run(db)
    assert response["attached"] == 1
    assert response["credits_charged"] == 1
    assert len(db.assets.docs) == 1
    doc = db.assets.docs[0]
    assert doc["external_id"] == "k1"
    assert doc["relevance_score"] == 91
    assert doc["relevance_reason"] == "kettle steam"
    assert doc["duration"] == 4.5
    assert doc["file_size"] == 12000
    assert doc["width"] == 720
    assert doc["height"] == 1280
    assert db.subscriptions.docs[0]["credits_remaining"] == 4
    assert len(db.credit_transactions.docs) == 1
    assert db.credit_transactions.docs[0]["amount"] == -1
