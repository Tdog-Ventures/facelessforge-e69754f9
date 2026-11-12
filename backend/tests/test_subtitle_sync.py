"""Caption clock: narration placement, not scene boundaries."""
from pathlib import Path

from app.render import _ffmpeg_intro_from_image, _ffmpeg_intro_from_video, intro_title_bounds
from app.subtitles import build_cues_from_words, build_srt_from_words, subtitle_region


def _overlaps(box, region) -> bool:
    ax0, ay0, ax1, ay1 = box
    bx0, by0, bx1, by1 = region
    return ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0


def test_word_clock_includes_intro_and_ignores_scene_boundaries():
    # Spoken 0.4s into the second scene. Scene holds are 2.9s then 2.866667s,
    # so the scene boundary on the video is 2.5+2.9, not the word.
    scene_durations = [2.9, 2.866667]
    spoken = scene_durations[0] + 0.4
    words = [{"word": "orange", "start": spoken, "end": spoken + 0.35}]
    cues = build_cues_from_words(words, narration_start=2.5, fit_scale=1.0)
    assert len(cues) == 1
    assert cues[0]["start"] == 2.5 + spoken
    scene_boundary = 2.5 + scene_durations[0]
    constant_clock = 2.5 + 1 * 2.9
    assert cues[0]["start"] != scene_boundary
    assert cues[0]["start"] != constant_clock
    assert abs(cues[0]["start"] - scene_boundary) > 0.2


def test_pre_fit_words_use_the_same_scale_as_narration_fit():
    measured = 57.756735
    budget = 57.5
    fit_scale = budget / measured
    words = [{"word": "kettle", "start": 10.0, "end": 10.4}]
    cues = build_cues_from_words(words, narration_start=2.5, fit_scale=fit_scale)
    expected = 2.5 + 10.0 * fit_scale
    assert cues[0]["start"] == expected
    assert cues[0]["start"] != 2.5 + 10.0
    assert cues[0]["start"] != 10.0


def test_variable_scene_durations_do_not_use_scene_index_times_constant():
    durations = [2.9, 2.866667, 2.866667]
    # Word sits inside scene 2, not on either edge.
    spoken = durations[0] + durations[1] + 0.55
    words = [{"word": "cup.", "start": spoken, "end": spoken + 0.3}]
    cues = build_cues_from_words(words, narration_start=2.5)
    assert cues[0]["start"] == 2.5 + spoken
    assert cues[0]["start"] != 2.5 + 2 * 2.9
    assert cues[0]["start"] != 2.5 + sum(durations)


def test_final_cue_keeps_the_last_words_inside_the_video():
    # Reconstructed from the 60s burn: six words already span >3s, then "dries."
    tokens = ["Rain", "ends", "as", "the", "kitchen", "counter", "dries."]
    holds = [1.22, 0.92, 0.42, 0.16, 0.24, 0.40, 0.42]
    cursor = 53.10
    words = []
    for token, hold in zip(tokens, holds):
        words.append({"word": token, "start": cursor, "end": cursor + hold})
        cursor += hold
    video_end = 60.0
    cues = build_cues_from_words(
        words,
        narration_start=2.5,
        video_end=video_end,
        words_per_cue=7,
        max_cue_seconds=3.0,
    )
    assert len(cues) == 1
    for token in tokens:
        assert token in cues[0]["text"]
    assert cues[0]["start"] == 2.5 + 53.10
    assert cues[0]["end"] <= video_end
    assert cues[0]["end"] >= 2.5 + 53.10 + sum(holds) - 0.01

    # A cue that would run past the video keeps every word and ends on the video.
    late = [{"word": token, "start": 58.0 + i * 0.4, "end": 58.2 + i * 0.4} for i, token in enumerate(tokens)]
    clamped = build_cues_from_words(late, narration_start=2.5, video_end=60.0, words_per_cue=7)
    assert clamped[-1]["end"] <= 60.0
    assert "dries." in clamped[-1]["text"]
    assert "counter" in clamped[-1]["text"]


def test_line_length_clamp_does_not_drop_words():
    tokens = ["alpha", "beta"] + ["supercalifragilisticexpialidocious"] * 4
    from app.subtitles import _clean_caption
    text = _clean_caption(" ".join(tokens), max_chars_per_line=12)
    for token in tokens:
        assert token in text
    assert "…" not in text
    srt = build_srt_from_words(
        [{"word": token, "start": i * 0.2, "end": i * 0.2 + 0.2} for i, token in enumerate(tokens)],
        words_per_cue=20,
    )
    for token in tokens:
        assert token in srt


def test_intro_title_box_does_not_cover_the_subtitle_region(tmp_path: Path):
    title = "RAIN ON THE KITCHEN COUNTER"
    width, height = 1080, 1920
    box = intro_title_bounds(title, width, height)
    region = subtitle_region(width, height)
    assert not _overlaps(box, region)
    assert box[1] <= 160
    assert region[1] >= 1400
    assert box[3] <= region[1]

    long_title = " ".join(["KITCHEN"] * 40)
    long_box = intro_title_bounds(long_title, width, height)
    assert not _overlaps(long_box, subtitle_region(width, height))
    assert long_box[3] <= region[1]

    wide = intro_title_bounds(title, 1920, 1080)
    assert not _overlaps(wide, subtitle_region(1920, 1080))

    for builder in (_ffmpeg_intro_from_video, _ffmpeg_intro_from_image):
        cmd = builder(
            Path("hook.mp4"), 2.5, tmp_path / "intro.mp4", title, tmp_path, w=width, h=height,
        )
        vf = " ".join(cmd)
        assert f"y={box[1]}" in vf
        assert "(h-text_h)/2" not in vf
