"""Speech-to-text variants in scene alignment. No render, no network."""
import pytest

from app.timing import SceneAlignmentError, align_scene_timeline


def _words(tokens, start=0.0, step=0.3):
    return [{"word": t, "start": start + i * step, "end": start + i * step + 0.15}
            for i, t in enumerate(tokens)]


def _scenes(texts):
    return [{"id": f"s{i}", "scene_number": i + 1, "narration_text": t} for i, t in enumerate(texts)]


LONG = [
    "Start with eggs in the shell soft boiled six minutes in boiling water the white is set "
    "the yolk is runny hard boiled ten minutes ice bath right after stops the cooking",
    "Frittata beat eggs with veggies bake at 350 for 20 minutes baked eggs crack into a "
    "ramekin bake at 375 for 12 minutes sous vide 145 degrees for 45 minutes",
    "Omelette for fancy brunch let it set 60 seconds fold slide out baked for a crowd "
    "375 for 12 minutes check at 10 and serve them warm with toast",
]


def test_digits_and_number_words_match_both_ways():
    scenes = _scenes(["Hard boiled ten minutes at 145 degrees."])
    words = _words(["Hard", "-boiled", "10", "minutes", "at", "one", "forty", "-five", "degrees."])
    aligned = align_scene_timeline(scenes, words, narration_frames=120, fps=30)
    assert aligned[0]["end_frame"] == 120
    assert len(aligned[0]["aligned_words"]) == len(words)


def test_long_narration_tolerates_a_few_misheard_words():
    spoken = []
    starts = []
    cursor = 0.0
    for text in LONG:
        tokens = text.split()
        tokens = ["three" if t == "350" else t for t in tokens]  # "three famous"
        tokens = [t if t != "Omelette" else "Omelet" for t in tokens]
        tokens = [t for t in tokens if t != "for"] if text is LONG[1] else tokens
        starts.append(cursor)
        spoken += _words(tokens, cursor)
        cursor += len(tokens) * 0.3 + 0.5
    frames = int(cursor * 30) + 30
    aligned = align_scene_timeline(_scenes(LONG), spoken, narration_frames=frames, fps=30)
    assert [s["start_frame"] for s in aligned] == [0, round(starts[1] * 30), round(starts[2] * 30)]
    assert aligned[-1]["end_frame"] == frames


def test_tolerance_does_not_rescue_wrong_or_missing_narration():
    # Short scripts stay strict.
    with pytest.raises(SceneAlignmentError, match="does not match"):
        align_scene_timeline(_scenes(["The kettle boils."]),
                             _words(["The", "orange", "sits."]), narration_frames=90, fps=30)
    # A long narration that is mostly different words still fails.
    other = _words(("the cat sat on a mat " * 20).split())
    with pytest.raises(SceneAlignmentError):
        align_scene_timeline(_scenes(LONG), other, narration_frames=3000, fps=30)
    # A scene whose words were never spoken still fails.
    spoken = _words(LONG[0].split()) + _words(LONG[2].split(), 20.0)
    with pytest.raises(SceneAlignmentError):
        align_scene_timeline(_scenes(LONG), spoken, narration_frames=3000, fps=30)
