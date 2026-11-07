"""Production video selection guards — no placeholders, off-topic assets or blurred strips."""
from backend.app.asset_relevance import candidate_is_relevant, relevant_candidates
from backend.app import render, stock


def test_cricket_video_does_not_pick_lava_lamp_or_office():
    scene = {"search_terms": ["cricket stadium crowd"], "visual_direction": "Spectators watching cricket"}
    found = [
        {"source": "pixabay", "external_id": "1", "download_url": "https://cdn.example/1.mp4",
         "title": "Lava lamp on an office desk", "source_url": "https://pixabay.com/videos/lava-lamp-1"},
        {"source": "pixabay", "external_id": "2", "download_url": "https://cdn.example/2.mp4",
         "title": "Cricket stadium spectators cheering", "source_url": "https://pixabay.com/videos/cricket-stadium-2"},
    ]
    allowed, dropped = relevant_candidates(found, scene, "cricket stadium crowd")
    assert [c["external_id"] for c in allowed] == ["2"]
    assert [c["external_id"] for c in dropped] == ["1"]


def test_provider_query_echo_is_not_relevance_evidence():
    scene = {"search_terms": ["cricket stadium"], "visual_direction": "Cricket players"}
    candidate = {
        "source": "pexels", "external_id": "123", "download_url": "https://cdn.example/123.mp4",
        "title": "Pexels video 123", "source_url": "https://pexels.com/video/123",
        "tags": ["cricket stadium"], "query": "cricket stadium",
    }
    assert not candidate_is_relevant(candidate, scene, "cricket stadium")


def test_mock_assets_are_never_auto_selected():
    scene = {"search_terms": ["cricket batsman"]}
    assert not candidate_is_relevant({
        "source": "mock", "external_id": "1", "download_url": "https://picsum.photos/1",
        "title": "Cricket batsman", "source_url": "https://pexels.com/video/cricket-batsman-1",
    }, scene, "cricket batsman")


def test_portrait_provider_video_is_accepted():
    clip = {"id": 123, "width": 1080, "height": 1920, "duration": 12,
            "video_files": [{"file_type": "video/mp4", "quality": "hd",
                             "link": "https://cdn.example/123.mp4"}]}
    result = stock._normalise_pexels_video(clip, "cricket stadium")
    assert result is not None
    assert result["height"] > result["width"]


def test_portrait_frame_uses_full_bleed_not_blurred_strip():
    vf = render._blur_fill_vf(1080, 1920)
    assert "crop=1080:1920" in vf
    assert "boxblur" not in vf
    assert "overlay" not in vf
