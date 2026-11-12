"""Spoken-word scene bounds. No render, no network."""
import asyncio
import inspect
import json

import pytest

from app.subtitles import build_cues_from_words
from app.timing import (
    SceneAlignmentError,
    align_scene_timeline,
    alignment_within_frame,
    normalize_scene_timeline,
    split_segment_holds,
    word_alignment_report,
)
from app.render import MAX_SUBCLIP_SECONDS, _build_subclip_plan, _run_render


def _words(text: str, start: float, step: float = 0.3) -> list[dict]:
    words = []
    cursor = start
    for token in text.split():
        words.append({"word": token, "start": cursor, "end": cursor + min(0.15, step * 0.5)})
        cursor += step
    return words


def _align(scenes, words, *, frames=900, fps=30, intro=2.5):
    return align_scene_timeline(
        scenes, words, narration_frames=frames, fps=fps, intro_seconds=intro,
    )


def _assert_budget(scenes, frames):
    assert scenes[0]["start_frame"] == 0
    assert scenes[-1]["end_frame"] == frames
    total = 0
    for index, scene in enumerate(scenes):
        assert scene["duration_frames"] >= 1
        assert scene["end_frame"] == scene["start_frame"] + scene["duration_frames"]
        total += scene["duration_frames"]
        if index:
            assert scene["start_frame"] == scenes[index - 1]["end_frame"]
    assert total == frames


def test_scene_bounds_follow_speaking_speed_not_equal_slots():
    scenes = [
        {"id": "a", "scene_number": 1, "narration_text": "One two."},
        {"id": "b", "scene_number": 2, "narration_text": "Three four five six."},
        {"id": "c", "scene_number": 3, "narration_text": "Seven."},
    ]
    fast_words = (
        _words(scenes[0]["narration_text"], 0.0, 0.2)
        + _words(scenes[1]["narration_text"], 0.8, 0.2)
        + _words(scenes[2]["narration_text"], 2.0, 0.2)
    )
    slow_words = (
        _words(scenes[0]["narration_text"], 0.0, 0.5)
        + _words(scenes[1]["narration_text"], 4.0, 0.5)
        + _words(scenes[2]["narration_text"], 12.0, 0.5)
    )
    fast = _align(scenes, fast_words)
    slow = _align(scenes, slow_words)
    slots = normalize_scene_timeline(scenes, narration_frames=900, fps=30)

    _assert_budget(fast, 900)
    _assert_budget(slow, 900)
    assert fast[1]["start_frame"] == 24
    assert slow[1]["start_frame"] == 120
    assert fast[1]["start_frame"] != slow[1]["start_frame"]
    assert slots[1]["start_frame"] not in {fast[1]["start_frame"], slow[1]["start_frame"]}
    assert fast[1]["start_time"] == pytest.approx(0.8)
    assert slow[1]["start_time"] == pytest.approx(4.0)


def test_unequal_scene_durations_follow_the_words():
    scenes = [
        {"id": "a", "scene_number": 1, "narration_text": "Hi."},
        {"id": "b", "scene_number": 2, "narration_text": "The kettle boils hard."},
        {"id": "c", "scene_number": 3, "narration_text": "Done."},
    ]
    words = (
        _words(scenes[0]["narration_text"], 0.0)
        + _words(scenes[1]["narration_text"], 0.5, 0.3)
        + _words(scenes[2]["narration_text"], 3.5)
    )
    aligned = _align(scenes, words, frames=300)
    _assert_budget(aligned, 300)
    assert [scene["duration_frames"] for scene in aligned] == [15, 90, 195]
    assert len(set(scene["duration_frames"] for scene in aligned)) == 3


def test_pause_keeps_the_picture_until_the_next_word_and_cues_stay_split():
    scenes = [
        {"id": "rain", "scene_number": 1, "narration_text": "Rain hits"},
        {"id": "orange", "scene_number": 2, "narration_text": "Orange sits"},
    ]
    words = [
        {"word": "Rain", "start": 0.2, "end": 0.6},
        {"word": "hits", "start": 0.6, "end": 1.0},
        {"word": "Orange", "start": 1.5, "end": 1.9},
        {"word": "sits", "start": 1.9, "end": 2.3},
    ]
    aligned = _align(scenes, words, frames=300)
    _assert_budget(aligned, 300)
    # 0.5s gap. The midpoint of the pause is 1.25s; the cut is the next word.
    assert aligned[0]["end_time"] == pytest.approx(1.5)
    assert aligned[1]["start_frame"] == 45
    assert abs(aligned[0]["end_time"] - 1.5) < abs(aligned[0]["end_time"] - 1.25)

    cues = build_cues_from_words(words, narration_start=2.5, fit_scale=1.0, video_end=12.5)
    assert len(cues) == 2
    assert cues[0]["start"] == pytest.approx(2.7)
    assert cues[1]["start"] == pytest.approx(4.0)
    assert cues[0]["end"] <= cues[1]["start"]
    assert cues[1]["words"][0]["start"] - cues[0]["words"][-1]["end"] > 0.45
    # The cue stays on its first word. It is not pulled back to the scene edge.
    assert cues[0]["start"] > 2.5


def test_raindrops_matches_only_when_the_pieces_spell_the_word():
    scenes = [{"id": "s", "scene_number": 1, "narration_text": "Raindrops hit glass."}]
    words = [
        {"word": "Rain", "start": 0.2, "end": 0.4},
        {"word": "drops", "start": 0.4, "end": 0.7},
        {"word": "hit", "start": 0.7, "end": 0.9},
        {"word": "GLASS.", "start": 0.9, "end": 1.2},
    ]
    aligned = _align(scenes, words, frames=90)
    assert [word["word"] for word in aligned[0]["aligned_words"]] == [
        "Rain", "drops", "hit", "GLASS.",
    ]

    broken = [
        {"word": "Rain", "start": 0.0, "end": 0.3},
        {"word": "falls.", "start": 0.3, "end": 0.6},
    ]
    joined = [{"id": "s", "scene_number": 1, "narration_text": "Rain drops hit glass."}]
    whisper = [
        {"word": "Raindrops", "start": 0.2, "end": 0.7},
        {"word": "hit", "start": 0.7, "end": 0.9},
        {"word": "glass.", "start": 0.9, "end": 1.2},
    ]
    merged = _align(joined, whisper, frames=90)
    assert [word["word"] for word in merged[0]["aligned_words"]] == [
        "Raindrops", "hit", "glass.",
    ]

    with pytest.raises(SceneAlignmentError, match="does not match"):
        _align(scenes, broken, frames=90)


def test_a_shared_generic_word_does_not_align_the_scene():
    scenes = [{"id": "s", "scene_number": 1, "narration_text": "The kettle boils."}]
    words = [
        {"word": "The", "start": 0.0, "end": 0.2},
        {"word": "orange", "start": 0.2, "end": 0.5},
        {"word": "sits.", "start": 0.5, "end": 0.8},
    ]
    with pytest.raises(SceneAlignmentError, match="does not match"):
        _align(scenes, words, frames=90)


def test_alignment_failure_raises_instead_of_equal_slots():
    scenes = [
        {"id": "a", "scene_number": 1, "narration_text": "Alpha bravo."},
        {"id": "b", "scene_number": 2, "narration_text": "Charlie delta."},
    ]
    with pytest.raises(SceneAlignmentError, match="No spoken words"):
        _align(scenes, [], frames=300)
    with pytest.raises(SceneAlignmentError, match="missing spoken words"):
        _align(scenes, _words(scenes[0]["narration_text"], 0.0), frames=300)
    words = (
        _words(scenes[0]["narration_text"], 0.0)
        + _words(scenes[1]["narration_text"], 2.0)
        + [{"word": "extra", "start": 4.0, "end": 4.2}]
    )
    with pytest.raises(SceneAlignmentError, match="Leftover spoken words"):
        _align(scenes, words, frames=300)


def test_long_segment_splits_without_moving_neighbors():
    assert MAX_SUBCLIP_SECONDS == 5.0
    scenes = [
        {"id": "a", "scene_number": 1, "narration_text": "Hi."},
        {"id": "b", "scene_number": 2, "narration_text": "Rain falls on the long window."},
        {"id": "c", "scene_number": 3, "narration_text": "Bye."},
    ]
    words = (
        _words(scenes[0]["narration_text"], 0.0)
        + _words(scenes[1]["narration_text"], 1.0, 0.4)
        + _words(scenes[2]["narration_text"], 9.0)
    )
    aligned = _align(scenes, words, frames=360)
    _assert_budget(aligned, 360)
    before = [(scene["start_frame"], scene["end_frame"]) for scene in aligned]
    plan = _build_subclip_plan(aligned, 12.0, word_aligned=True, fps=30)
    assert [(scene["start_frame"], scene["end_frame"]) for scene in aligned] == before
    assert plan[0]["subclips"] == pytest.approx([1.0])
    assert sum(plan[1]["subclips"]) == pytest.approx(8.0)
    assert len(plan[1]["subclips"]) >= 2
    assert all(piece <= MAX_SUBCLIP_SECONDS + 1e-9 for piece in plan[1]["subclips"])
    assert plan[2]["subclips"] == pytest.approx([3.0])
    assert sum(sum(item["subclips"]) for item in plan) == pytest.approx(12.0)
    # 5.0s stays one hold. One frame past the live max splits. The comment's 6s does not.
    assert split_segment_holds(150, max_seconds=MAX_SUBCLIP_SECONDS, fps=30) == [150]
    assert split_segment_holds(151, max_seconds=MAX_SUBCLIP_SECONDS, fps=30) == [76, 75]


def test_short_scenes_are_not_split_just_to_make_more_shots():
    lengths = [1.0, 2.5] * 9 + [1.5, 2.0]
    assert len(lengths) == 20
    assert sum(lengths) == pytest.approx(35.0)
    scenes = []
    words = []
    cursor = 0.0
    for number, length in enumerate(lengths, start=1):
        text = f"Shot{number}."
        scenes.append({"id": f"s{number}", "scene_number": number, "narration_text": text})
        words.extend(_words(text, cursor))
        cursor += length
    frames = 35 * 30
    aligned = _align(scenes, words, frames=frames)
    _assert_budget(aligned, frames)
    assert [scene["duration_frames"] for scene in aligned] == [int(length * 30) for length in lengths]
    plan = _build_subclip_plan(aligned, 35.0, word_aligned=True, fps=30)
    assert [len(item["subclips"]) for item in plan] == [1] * 20
    assert all(item["subclips"][0] <= MAX_SUBCLIP_SECONDS for item in plan)


def test_word_aligned_plan_does_not_floor_or_rescale_short_scenes():
    scenes = [
        {"id": "a", "scene_number": 1, "duration_frames": 36, "start_time": 0.0, "end_time": 1.2},
        {"id": "b", "scene_number": 2, "duration_frames": 264, "start_time": 1.2, "end_time": 10.0},
    ]
    plan = _build_subclip_plan(scenes, timeline_duration=100, word_aligned=True, fps=30)
    assert plan[0]["subclips"] == pytest.approx([1.2])
    assert plan[0]["subclips"][0] < 2.0
    assert sum(plan[1]["subclips"]) == pytest.approx(8.8)
    assert all(piece <= MAX_SUBCLIP_SECONDS + 1e-9 for piece in plan[1]["subclips"])

    legacy = _build_subclip_plan(
        [{"start_time": 0.0, "end_time": 1.2}, {"start_time": 1.2, "end_time": 10.0}],
        timeline_duration=100,
    )
    assert legacy[0]["subclips"][0] > 2.0


def test_final_scene_ends_on_the_budget_and_a_late_word_is_omitted():
    scenes = [
        {"id": "a", "scene_number": 1, "narration_text": "Ready now."},
        {"id": "b", "scene_number": 2, "narration_text": "Done later."},
    ]
    words = [
        {"word": "Ready", "start": 0.2, "end": 0.5},
        {"word": "now.", "start": 0.5, "end": 0.8},
        {"word": "Done", "start": 8.0, "end": 8.4},
        {"word": "later.", "start": 10.0, "end": 10.2},
    ]
    aligned = _align(scenes, words, frames=300)
    _assert_budget(aligned, 300)
    assert aligned[-1]["end_frame"] == 300
    assert aligned[-1]["end_time"] == pytest.approx(10.0)
    assert aligned[-1]["end_time"] < 10.2

    video_end = 12.5
    cues = build_cues_from_words(words, narration_start=2.5, fit_scale=1.0, video_end=video_end)
    assert all("later" not in cue["text"].lower() for cue in cues)
    assert any(cue["text"].startswith("Done") for cue in cues)
    report = word_alignment_report(
        aligned, cues, fps=30, intro_seconds=2.5, asset_ids=[None, None],
    )
    assert report[-1]["visual_end"] == pytest.approx(video_end)
    assert report[-1]["visual_end"] <= video_end
    assert report[-1]["narration_end"] == pytest.approx(video_end)
    assert alignment_within_frame(report, fps=30)


def test_report_flags_a_picture_more_than_one_frame_off_the_words():
    scenes = [
        {"id": "a", "scene_number": 1, "narration_text": "Hi there."},
        {"id": "b", "scene_number": 2, "narration_text": "Go."},
    ]
    words = _words(scenes[0]["narration_text"], 0.0, 0.2) + [
        {"word": "Go.", "start": 1.016, "end": 1.2},
    ]
    aligned = _align(scenes, words, frames=300)
    cues = build_cues_from_words(words, narration_start=2.5, fit_scale=1.0)
    report = word_alignment_report(
        aligned, cues, fps=30, intro_seconds=2.5, asset_ids=[None, "kept-asset"],
    )
    json.dumps(report)
    assert alignment_within_frame(report, fps=30)
    assert all(record["within_frame"] for record in report)
    assert report[1]["asset_id"] == "kept-asset"
    assert report[1]["scene_id"] == "b"
    # Quantized boundary is within one frame of the spoken 1.016s.
    # Scene documents stay on the narration clock; the report adds intro once.
    assert abs(report[1]["visual_start"] - report[1]["narration_start"]) * 1000 <= 1000 / 30
    assert abs((report[1]["narration_start"] - 2.5) - aligned[1]["start_time"]) <= (1 / 30) + 1e-6
    commercial = word_alignment_report(
        aligned, cues, fps=30, intro_seconds=0.0, asset_ids=[None, None],
    )
    assert commercial[1]["narration_start"] == pytest.approx(1.016)
    assert aligned[1]["start_time"] == pytest.approx(1.0)
    assert abs(commercial[1]["narration_start"] - aligned[1]["start_time"]) <= (1 / 30) + 1e-6

    slots = normalize_scene_timeline(scenes, narration_frames=300, fps=30)
    bad = word_alignment_report(
        aligned,
        cues,
        fps=30,
        intro_seconds=2.5,
        asset_ids=[None, None],
        visual_spans=[(slot["start_time"], slot["end_time"]) for slot in slots],
    )
    assert alignment_within_frame(bad, fps=30) is False
    assert any(record["within_frame"] is False for record in bad)
    assert bad[1]["subtitle_start"] == pytest.approx(cues[-1]["start"])


def test_missing_stock_picture_uses_the_spoken_segment(tmp_path, monkeypatch):
    scenes = [
        {"id": "rain", "scene_number": 1, "narration_text": "Rain falls on the window."},
        {"id": "sun", "scene_number": 2, "narration_text": "Sun returns."},
    ]
    words = _words(scenes[0]["narration_text"], 0.0, 0.4) + _words(scenes[1]["narration_text"], 8.0)
    aligned = _align(scenes, words, frames=300)
    assert aligned[0]["duration_frames"] == 240

    calls = {"still": 0, "stock": 0}
    captured = {}

    async def no_stock(*_args, **_kwargs):
        calls["stock"] += 1
        return None, None, None

    async def fake_still(_shot):
        calls["still"] += 1
        return b"png"

    def fake_picture(_image, output, _width, _height, frames, fps=30):
        captured["frames"] = frames
        output.write_bytes(b"picture")
        return output

    async def explode(*_args, **_kwargs):
        raise AssertionError("stock search")

    monkeypatch.setattr("app.directed_graphics.generate_shot_still", fake_still)
    monkeypatch.setattr("app.directed_graphics.render_picture", fake_picture)
    monkeypatch.setattr("app.stock.search_stock", explode)

    from app.visual_director import resolve_directed_scene

    shot = {
        "kind": "stock_video",
        "query": "rain on a window",
        "frames": 87,
        "narration": scenes[0]["narration_text"],
    }
    result = asyncio.run(resolve_directed_scene(
        aligned[0], [shot], {}, tmp_path, 0, 640, 360, set(), no_stock,
        aligned_frames=aligned[0]["duration_frames"],
    ))
    assert calls["stock"] == 1
    assert calls["still"] == 1
    assert captured["frames"] == 240
    assert captured["frames"] != 87
    assert [kind for _path, kind in result] == ["video"]
    plan = _build_subclip_plan(aligned, 10.0, word_aligned=True, fps=30)
    assert sum(plan[0]["subclips"]) == pytest.approx(8.0)
    assert all(piece <= MAX_SUBCLIP_SECONDS + 1e-9 for piece in plan[0]["subclips"])
    cues = build_cues_from_words(words, narration_start=2.5, fit_scale=1.0)
    spans = []
    for scene, item in zip(aligned, plan):
        start = scene["start_frame"] / 30
        spans.append((start, start + sum(item["subclips"])))
    report = word_alignment_report(
        aligned, cues, fps=30, intro_seconds=2.5, asset_ids=[None, None], visual_spans=spans,
    )
    assert report[0]["asset_id"] is None
    assert alignment_within_frame(report, fps=30)


def test_render_plans_pictures_from_the_spoken_words():
    source = inspect.getsource(_run_render)
    assert "align_scene_timeline" in source
    assert "word_aligned=word_aligned" in source
    assert "aligned_frames=" in source
    assert "placeholder_scenes" in source
    assert "if not word_aligned" in source
    assert "Refusing equal-duration scene slots" in source
    assert "required_visuals = 1 if word_aligned else max(n_clips, 1)" in source
