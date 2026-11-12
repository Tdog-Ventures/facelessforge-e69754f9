"""Caption clock: narration placement, not scene boundaries."""
from pathlib import Path

from app.render import (
    _ffmpeg_intro_from_image,
    _ffmpeg_intro_from_video,
    _intro_title_placement,
    _try_load_font,
)
from app.subtitles import (
    build_ass_karaoke,
    build_cues_from_words,
    build_srt_from_words,
    subtitle_region,
)


def _overlaps(box, region) -> bool:
    ax0, ay0, ax1, ay1 = box
    bx0, by0, bx1, by1 = region
    return ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0


def _title_box(title: str, width: int, height: int) -> tuple[int, int, int, int]:
    """Pixel box for the y `_intro_title_placement` already pins on the filter."""
    wrapped, font_size, y = _intro_title_placement(title, width=width, height=height)
    lines = [line for line in (wrapped or "").split("\n") if line]
    font = _try_load_font(font_size)
    text_w = max((int(font.getlength(line)) for line in lines), default=0)
    block_h = len(lines) * (font_size + 8) if lines else 0
    x = max(0, (width - text_w) // 2)
    return x, y, x + text_w, y + block_h


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
    )
    assert len(cues) == 1
    for token in tokens:
        assert token in cues[0]["text"]
    assert cues[0]["start"] == 2.5 + 53.10
    assert cues[0]["end"] <= video_end
    assert cues[0]["end"] >= 2.5 + 53.10 + sum(holds) - 0.01

    # The word cap would split "dries." off. The gap is 0, so it stays on the sentence.
    merged = build_cues_from_words(
        words, narration_start=2.5, video_end=video_end, words_per_cue=6,
    )
    assert len(merged) == 1
    assert merged[0]["start"] == 2.5 + 53.10
    assert "dries." in merged[0]["text"]


def _srt_spans(srt: str) -> list[tuple[str, str]]:
    return [tuple(line.split(" --> ")) for line in srt.splitlines() if " --> " in line]


def _ass_spans(ass: str) -> list[tuple[str, str]]:
    spans = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",")
            spans.append((parts[1], parts[2]))
    return spans


def test_cue_past_video_end_is_omitted_and_inside_cue_is_clamped():
    # Narration 58.2 with a 2.5s intro is 60.70. Clamping only the end wrote
    # 60.70 --> 60.00 into SRT and ASS.
    past = [{"word": "later.", "start": 58.2, "end": 58.5}]
    assert build_cues_from_words(past, narration_start=2.5, video_end=60.0) == []
    srt = build_srt_from_words(past, intro_offset_seconds=2.5, video_end=60.0)
    ass = build_ass_karaoke(
        past, intro_offset_seconds=2.5, video_end=60.0, play_res_x=1080, play_res_y=1920,
    )
    assert _srt_spans(srt) == []
    assert _ass_spans(ass) == []
    assert "00:01:00,700 --> 00:01:00,000" not in srt
    assert "0:01:00.70,0:01:00.00" not in ass

    # A word placed exactly on the video end has no samples left either.
    edge = [{"word": "edge.", "start": 57.5, "end": 57.7}]
    assert build_cues_from_words(edge, narration_start=2.5, video_end=60.0) == []

    # "Now." is spoken at 59.90, inside the video. Emit it, clamp the end, leave the start.
    spoken = 57.4
    inside = [{"word": "Now.", "start": spoken, "end": spoken + 0.1}]
    cues = build_cues_from_words(inside, narration_start=2.5, video_end=60.0)
    assert len(cues) == 1
    assert cues[0]["text"] == "Now."
    assert cues[0]["start"] == 2.5 + spoken
    assert cues[0]["start"] < 60.0
    assert cues[0]["end"] == 60.0
    assert cues[0]["start"] != 59.5
    srt = build_srt_from_words(inside, intro_offset_seconds=2.5, video_end=60.0)
    ass = build_ass_karaoke(
        inside, intro_offset_seconds=2.5, video_end=60.0, play_res_x=1080, play_res_y=1920,
    )
    srt_spans = _srt_spans(srt)
    ass_spans = _ass_spans(ass)
    assert len(srt_spans) == 1
    assert len(ass_spans) == 1
    assert srt_spans[0][0] <= srt_spans[0][1]
    assert ass_spans[0][0] <= ass_spans[0][1]
    assert "Now." in srt
    assert "Now." in ass


def test_paused_short_tail_stays_on_its_word_time():
    narration_start = 2.5
    words = []
    t = 10.0
    for token in ("one", "two", "three", "four", "five", "six", "seven"):
        words.append({"word": token, "start": t, "end": t + 0.30})
        t += 0.35
    tail_start = words[-1]["end"] + 1.2
    words.append({"word": "late", "start": tail_start, "end": tail_start + 0.25})
    words.append({"word": "tail", "start": tail_start + 0.30, "end": tail_start + 0.55})
    cues = build_cues_from_words(words, narration_start=narration_start, words_per_cue=7)
    assert len(cues) == 2
    assert "tail" not in cues[0]["text"]
    assert cues[1]["text"] == "late tail"
    assert cues[1]["start"] == narration_start + tail_start
    assert cues[1]["start"] != cues[0]["start"]


def test_short_final_cue_is_not_pulled_over_the_previous_cue():
    narration_start = 2.5
    video_end = 60.0
    # "The counter." is on screen 59.00–59.55. "Done." is spoken at 59.70.
    words = [
        {"word": "The", "start": 56.50, "end": 56.80},
        {"word": "counter.", "start": 56.80, "end": 57.05},
        {"word": "Done.", "start": 57.20, "end": 57.35},
    ]
    cues = build_cues_from_words(words, narration_start=narration_start, video_end=video_end)
    done_start = narration_start + 57.20
    assert cues[0]["text"] == "The counter."
    assert cues[0]["end"] == narration_start + 57.05
    assert cues[1]["text"] == "Done."
    assert cues[1]["start"] == done_start
    assert cues[1]["start"] >= cues[0]["end"]
    assert cues[1]["end"] <= video_end
    assert cues[1]["end"] < done_start + 0.5
    assert cues[1]["start"] != video_end - 0.5

    # "Yes." is spoken at 59.85. The last 0.5s must not pull it back to 59.50.
    yes = words[:2] + [{"word": "Yes.", "start": 57.35, "end": 57.50}]
    yes_cues = build_cues_from_words(yes, narration_start=narration_start, video_end=video_end)
    yes_start = narration_start + 57.35
    assert yes_cues[-1]["text"] == "Yes."
    assert yes_cues[-1]["start"] == yes_start
    assert yes_cues[-1]["start"] >= yes_cues[0]["end"]
    assert yes_cues[-1]["end"] <= video_end
    assert yes_cues[-1]["start"] != 59.50


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
    box = _title_box(title, width, height)
    region = subtitle_region(width, height)
    assert not _overlaps(box, region)
    assert box[1] <= 160
    assert region[1] >= 1400
    assert box[3] <= region[1]

    long_title = " ".join(["KITCHEN"] * 40)
    long_box = _title_box(long_title, width, height)
    assert not _overlaps(long_box, subtitle_region(width, height))
    assert long_box[3] <= region[1]

    wide = _title_box(title, 1920, 1080)
    assert not _overlaps(wide, subtitle_region(1920, 1080))

    for builder in (_ffmpeg_intro_from_video, _ffmpeg_intro_from_image):
        cmd = builder(
            Path("hook.mp4"), 2.5, tmp_path / "intro.mp4", title, tmp_path, w=width, h=height,
        )
        vf = " ".join(cmd)
        assert f"y={box[1]}" in vf
        assert "(h-text_h)/2" not in vf
