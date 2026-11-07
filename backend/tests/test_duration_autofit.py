"""Auto-fit loop: VO-is-master duration reconciliation (no manual patching).

Incident: two E2E renders each needed manual target_duration patches
(27->30, 30->35) because preflight hard-failed "Voiceover fits selected
duration" whenever measured narration drifted >5% from the selection.
The loop below moves the selection to fit measured narration within the
5..3600s product bounds; anything else still fails closed.
"""
import pytest

from app import render
from app.generation import _finalize_script
from app.timing import suggest_fitted_duration
from backend.tests.fake_db import FakeCollection, FakeDatabase


def test_suggest_fits_measured_narration():
    assert suggest_fitted_duration(voiceover_seconds=27.2745625, intro_seconds=2.5) == 30
    assert suggest_fitted_duration(voiceover_seconds=32.6558125, intro_seconds=2.5) == 35
    assert suggest_fitted_duration(voiceover_seconds=30.0, intro_seconds=0.0) == 30


def test_suggest_rejects_out_of_bounds_and_empty():
    assert suggest_fitted_duration(voiceover_seconds=2.0, intro_seconds=2.5) is None
    assert suggest_fitted_duration(voiceover_seconds=5000.0, intro_seconds=2.5) is None
    assert suggest_fitted_duration(voiceover_seconds=0, intro_seconds=2.5) is None
    assert suggest_fitted_duration(voiceover_seconds=-3, intro_seconds=2.5) is None
    assert suggest_fitted_duration(voiceover_seconds=None, intro_seconds=2.5) is None


def test_finalize_script_reports_word_budget():
    script = "word " * 48
    out = _finalize_script({"full_script": script.strip()}, 27.5)
    assert out["word_budget"] == 69  # 27.5s * 150wpm / 60
    assert out["budget_coverage"] == pytest.approx(48 / 69, abs=0.01)
    assert out["word_count"] == 48


def test_suggest_duration_autofit_proposes_move():
    project = {"target_duration": 27}
    voiceover = {"duration": 27.2745625}
    suggestion = render.suggest_duration_autofit(project, voiceover)
    assert suggestion == {"from": 27, "to": 30, "voiceover_seconds": 27.2745625}


def test_suggest_duration_autofit_quiet_when_fitting_or_missing():
    fitting = {"target_duration": 30}
    assert render.suggest_duration_autofit(fitting, {"duration": 27.9}) is None
    assert render.suggest_duration_autofit({"target_duration": 27}, None) is None
    assert render.suggest_duration_autofit({"target_duration": 27}, {"duration": 0}) is None


def _fake_db(project, voice):
    updates = []

    db = FakeDatabase(
        projects=FakeCollection("projects", [project] if project else []),
        assets=FakeCollection("assets", [{**voice, "project_id": project["id"]}] if voice else []),
    )
    update_project = db.projects.update_one

    async def tracked_update(query, update, **kwargs):
        if "target_duration" in update.get("$set", {}):
            updates.append(update)
        return await update_project(query, update, **kwargs)

    db.projects.update_one = tracked_update
    return db, updates


@pytest.mark.asyncio
async def test_autofit_persists_fitted_duration(monkeypatch):
    db, updates = _fake_db(
        {"id": "p1", "target_duration": 27, "selected_voiceover_asset_id": "v1"},
        {"id": "v1", "asset_type": "voiceover_audio", "duration": 27.2745625},
    )
    monkeypatch.setattr(render, "get_db", lambda: db)

    suggestion = await render.autofit_project_duration("p1")

    assert suggestion == {"from": 27, "to": 30, "voiceover_seconds": 27.2745625}
    assert updates == [{"$set": {"target_duration": 30, "updated_at": updates[0]["$set"]["updated_at"]}}]
    assert updates[0]["$set"]["target_duration"] == 30


@pytest.mark.asyncio
async def test_autofit_fails_closed_without_fit(monkeypatch):
    # No project
    db, _ = _fake_db(None, None)
    monkeypatch.setattr(render, "get_db", lambda: db)
    with pytest.raises(RuntimeError, match="not found"):
        await render.autofit_project_duration("missing")

    # No full voiceover
    db, _ = _fake_db({"id": "p1", "target_duration": 27}, None)
    monkeypatch.setattr(render, "get_db", lambda: db)
    with pytest.raises(RuntimeError, match="full-script voiceover"):
        await render.autofit_project_duration("p1")

    # Already fitting — nothing to move
    db, updates = _fake_db(
        {"id": "p1", "target_duration": 30, "selected_voiceover_asset_id": "v1"},
        {"id": "v1", "asset_type": "voiceover_audio", "duration": 27.9},
    )
    monkeypatch.setattr(render, "get_db", lambda: db)
    with pytest.raises(RuntimeError, match="regenerate"):
        await render.autofit_project_duration("p1")
    assert updates == []


def test_preflight_exposes_autofit_suggestion():
    project = {
        "id": "p1", "target_duration": 27,
        "selected_voiceover_asset_id": "v1",
        "selected_thumbnail_asset_id": "t1",
    }
    script = {"full_script": "word " * 60}
    scenes = [{"scene_number": 1, "duration": 27.5, "end_time": 27.5}]
    metadata = {"id": "m1"}
    assets = [
        {"id": "t1", "asset_type": "generated_thumbnail"},
        {"id": "v1", "asset_type": "voiceover_audio", "duration": 27.2745625,
         "status": "selected"},
        {"id": "s1", "asset_type": "stock_video", "scene_id": "scene-1"},
    ]
    check = render.validate_prerequisites(project, script, scenes, metadata, assets)
    assert check["ok"] is False
    assert check["issues"] == ["Voiceover fits selected duration", "Scene visuals meet 3-second pacing"]
    assert check["autofit_suggestion"] == {"from": 27, "to": 30, "voiceover_seconds": 27.2745625}
