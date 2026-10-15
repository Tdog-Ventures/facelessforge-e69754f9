from pathlib import Path

import pytest

from app import render, subtitles, visual_query


def test_vertical_ass_uses_vertical_canvas_and_smart_wrapping():
    words = [
        {"word": word, "start": index * 0.3, "end": (index + 1) * 0.3}
        for index, word in enumerate(
            "change the visual whenever the idea changes today".split()
        )
    ]

    ass = subtitles.build_ass_karaoke(
        words,
        play_res_x=1080,
        play_res_y=1920,
    )

    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    assert "WrapStyle: 0" in ass
    assert "Style: Default,Arial,64" in ass
    assert ",2,96,96,300,1" in ass


def test_release_eligibility_is_blocked_by_deterministic_qa_failure():
    decision = render.release_eligible_after_qa(
        commercial=False,
        deterministic_blocked=["freezedetect"],
        video_qa={"release_eligible": True},
        verification_passed=True,
    )

    assert decision is False


def test_release_eligibility_requires_every_gate_to_pass():
    assert render.release_eligible_after_qa(
        commercial=False,
        deterministic_blocked=[],
        video_qa={"release_eligible": True},
        verification_passed=True,
    ) is True
    assert render.release_eligible_after_qa(
        commercial=False,
        deterministic_blocked=[],
        video_qa={"release_eligible": False},
        verification_passed=True,
    ) is False
    assert render.release_eligible_after_qa(
        commercial=False,
        deterministic_blocked=[],
        video_qa={"release_eligible": True},
        verification_passed=False,
    ) is False


@pytest.mark.asyncio
async def test_deterministic_stock_query_runs_before_optional_llm(monkeypatch, tmp_path):
    async def fail_if_llm_is_called(*_args, **_kwargs):
        raise AssertionError("optional LLM must not delay a usable deterministic stock query")

    async def fake_search_stock_videos(_query, per_page):
        assert per_page == 30
        return [{
            "external_id": "clip-1",
            "asset_type": "stock_video",
            "media_type": "stock_video",
            "download_url": "https://example.test/clip.mp4",
        }]

    async def fake_resolve_scene_visual(
        _scene,
        candidates,
        _project,
        _work_dir,
        _idx,
        **_kwargs,
    ):
        assert candidates[0]["external_id"] == "clip-1"
        return tmp_path / "clip.mp4", "video", "clip-1"

    monkeypatch.setattr(visual_query, "generate_broll_keywords", fail_if_llm_is_called)
    monkeypatch.setattr(render.stock_service, "search_stock_videos", fake_search_stock_videos)
    monkeypatch.setattr(render, "_resolve_scene_visual", fake_resolve_scene_visual)

    visuals = await render._resolve_scene_visuals(
        {
            "id": "scene-1",
            "visual_direction": "A professional video editor working at a timeline",
            "narration_text": "Change the visual whenever the idea changes.",
            "search_terms": ["video editing timeline"],
        },
        [],
        {"topic": "Professional AI video"},
        Path(tmp_path),
        0,
        max_visuals=1,
        used_ext_ids=set(),
    )

    assert visuals == [(tmp_path / "clip.mp4", "video")]
