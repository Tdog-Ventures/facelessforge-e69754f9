import pytest
import asyncio

from app.timing import (
    TimingContract,
    build_timing_status,
    normalize_scene_timeline,
    target_word_count,
)


def test_editorial_214_second_project_has_one_exact_timeline():
    timing = TimingContract.from_project({"target_duration": 214})

    assert timing.final_frames == 6420
    assert timing.intro_frames == 75
    assert timing.narration_frames == 6345
    assert timing.final_samples == 10_272_000
    assert timing.narration_seconds == 211.5
    assert target_word_count(timing.narration_seconds) == 529


def test_commercial_project_keeps_its_zero_intro_contract():
    timing = TimingContract.from_project({
        "target_duration": 15,
        "production_mode": "commercial_ad",
    })

    assert timing.intro_seconds == 0
    assert timing.narration_seconds == 15
    assert timing.final_frames == timing.narration_frames == 450


def test_scene_timestamps_from_ai_are_discarded_and_end_on_budget():
    timing = TimingContract.from_project({"target_duration": 214})
    scenes = [
        {"start_time": 10, "end_time": 200, "narration_text": "one two"},
        {"start_time": 250, "end_time": 579, "narration_text": "three four five six"},
    ]

    normalized = normalize_scene_timeline(
        scenes,
        narration_frames=timing.narration_frames,
        fps=timing.fps,
    )

    assert [(scene["start_frame"], scene["end_frame"]) for scene in normalized] == [
        (0, 2115),
        (2115, 6345),
    ]
    assert normalized[-1]["end_time"] == timing.narration_seconds


def test_voiceover_over_five_percent_requires_rebuild():
    status = build_timing_status(
        project={"target_duration": 214},
        scenes=[{"end_time": 211.5}],
        voiceover={"duration": 230},
    )

    assert status["authority"] == "selected_duration"
    assert status["voiceover_action"] == "rebuild"
    assert status["ready"] is False


def test_target_must_exceed_editorial_intro():
    with pytest.raises(ValueError, match="longer than the intro"):
        TimingContract.from_project({"target_duration": 2.5})


def test_render_subclips_sum_to_the_selected_narration_budget():
    from app.render import _build_subclip_plan

    plan = _build_subclip_plan([
        {"start_time": 0, "end_time": 70.5},
        {"start_time": 70.5, "end_time": 211.5},
    ], timeline_duration=211.5)

    assert round(sum(sum(item["subclips"]) for item in plan), 6) == 211.5


def test_verifier_uses_selected_duration_instead_of_raw_voiceover(monkeypatch):
    from app import verify

    async def video_duration(_path):
        return 214.0

    async def narration_duration(_path):
        return 211.5

    monkeypatch.setattr(verify, "ffprobe_video_duration", video_duration)
    monkeypatch.setattr(verify, "ffprobe_duration", narration_duration)

    result = asyncio.run(verify.check_a_timing("video.mp4", "voice.wav", 214.0))

    assert result["passed"] is True
    assert result["authority"] == "selected_duration"
    assert result["diff_s"] == 0
