"""Auto-attach must not fill a scene with near-identical stock visuals."""
from __future__ import annotations

from backend.app.asset_dedupe import filter_distinct_titles, titles_are_near_duplicates


EDITING_UI = [
    {"title": "Close-up of a video editing software interface showing timeline and controls."},
    {"title": "Detailed view of a video editing timeline displayed on a computer screen, highlighting the tools used in video production."},
    {"title": "Close-up of video editing software interface on a computer monitor showing timeline and preview."},
]


def test_three_editing_ui_results_collapse_to_one_visual():
    kept, rejected = filter_distinct_titles(EDITING_UI, [])
    assert len(kept) == 1
    assert len(rejected) == 2


def test_existing_project_asset_blocks_a_similar_new_candidate():
    kept, rejected = filter_distinct_titles(
        [EDITING_UI[2]],
        [EDITING_UI[0]["title"]],
    )
    assert kept == []
    assert len(rejected) == 1


def test_different_subjects_are_kept():
    candidates = [
        {"title": "Close-up of a video editing software interface showing timeline and controls."},
        {"title": "A professional camera on a tripod filming a sunset over the ocean."},
        {"title": "A person in silhouette wearing headphones editing video in a dark room."},
    ]
    kept, rejected = filter_distinct_titles(candidates, [])
    assert len(kept) == 3
    assert rejected == []


def test_short_or_generic_titles_are_not_treated_as_duplicates():
    assert titles_are_near_duplicates("Camera", "Camera") is False
    assert titles_are_near_duplicates("Video editing timeline", "Video editing timeline") is True
