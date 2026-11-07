"""Stock searches must use short footage subjects, not descriptive prompts."""
from backend.app.scene_terms import compact_stock_query
from backend.app.routes.api import _build_scene_stock_query, _auto_stock_query


def test_compact_query_removes_camera_direction_without_losing_subject():
    assert compact_stock_query(
        "Cinematic slow motion wide shot of a crowded cricket stadium, dramatic lighting 4k b-roll"
    ) == "crowded cricket stadium"


def test_stock_query_preserves_cricket_search_intent():
    scene = {
        "search_terms": [
            "Cinematic wide shot of One Day International cricket players batting at stadium, 4k slow motion b-roll cinematic",
            "cricket stadium crowd",
        ],
        "visual_direction": "Wide angle tracking the cricket batsman hitting a six under floodlights",
    }
    query = _build_scene_stock_query(scene, {"topic": "History of One Day International cricket"})
    assert query == "One Day International cricket players"
    assert _auto_stock_query({"topic": "History of One Day International cricket"}, scene) == query


def test_generated_terms_are_alternatives_not_joined():
    scene = {"search_terms": ["cricket crowd", "cricket batsman", "stadium floodlights"]}
    assert _build_scene_stock_query(scene, {}) == "cricket crowd"


def test_manual_override_is_respected_without_forcing_auto_keywords():
    scene = {"search_terms": ["bowler delivering cricket ball"]}
    assert _build_scene_stock_query(scene, {}, "stadium crowd cheering") == "stadium crowd cheering"


def test_visual_fallback_is_short_and_concrete():
    scene = {
        "visual_direction": "A professional aerial tracking shot of a cricket stadium with players walking onto the pitch, cinematic 4k",
    }
    query = _build_scene_stock_query(scene, {})
    assert len(query.split()) <= 5
    assert "cricket" in query and "cinematic" not in query
