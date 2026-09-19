from backend.app.grounding import script_duration_check, semantic_rejections, validate_commercial_grounding


def _project():
    return {
        "production_mode": "commercial_ad", "client_name": "Effective Wash",
        "target_duration": 15, "aspect_ratio": "9:16", "resolution": "1080x1920",
        "structured_brief": {"service": "pressure washing", "offer": "free quote", "audience": "homeowners", "location": "Adelaide"},
        "verified_cta": {"text": "Call for a free quote", "destination": "tel:+61812345678", "verified": True},
        "brand_assets_supplied": True,
    }


def test_15_second_ad_rejects_147_words():
    check = script_duration_check({"full_script": "word " * 147}, 15)
    assert check["ok"] is False
    assert check["maximum_words"] == 42


def test_known_wrong_stock_categories_are_rejected():
    assert "hacking" in semantic_rejections({"asset_type": "stock_video", "title": "Hacker coding at office computer"})
    assert "medical" in semantic_rejections({"asset_type": "stock_video", "tags": ["blood pressure", "doctor"]})
    assert "finance" in semantic_rejections({"asset_type": "stock_video", "query": "finance charts"})


def test_commercial_grounding_requires_client_assets_for_every_scene():
    scenes = [
        {"id": "s1", "scene_number": 1}, {"id": "s2", "scene_number": 2}, {"id": "s3", "scene_number": 3},
    ]
    assets = [
        {"id": "a1", "scene_id": "s1", "name": "before", "asset_type": "client_video", "source": "client_upload", "status": "attached"},
        {"id": "a2", "scene_id": "s2", "name": "cleaning", "asset_type": "client_video", "source": "client_upload", "status": "attached"},
        {"id": "a3", "scene_id": "s3", "name": "after", "asset_type": "client_video", "source": "client_upload", "status": "attached"},
        {"id": "logo", "name": "logo", "asset_type": "brand_logo", "source": "client_upload", "status": "attached"},
    ]
    script = {"full_script": "Stubborn grime? Effective Wash restores Adelaide driveways with careful pressure washing. See the real result, then call today for your free quote."}
    assert validate_commercial_grounding(_project(), script, scenes, assets)["ok"] is True
    assets[2]["asset_type"] = "stock_video"
    result = validate_commercial_grounding(_project(), script, scenes, assets)
    assert result["ok"] is False
    assert any("client visual" in issue.lower() for issue in result["issues"])
