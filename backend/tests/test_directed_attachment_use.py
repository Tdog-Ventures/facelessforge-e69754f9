"""The renderer must use saved attachments in order and recover from a bad download."""
import asyncio

from app.render import _resolve_scene_visual


def test_renderer_uses_next_saved_asset_after_a_failed_download(tmp_path, monkeypatch):
    searches = {"count": 0}

    async def fake_download(url, out_path, max_bytes, allow_audio=False):
        if "dead.example" in url:
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-video")
        return True

    async def fake_motion(path):
        return True

    async def fail_search(*args, **kwargs):
        searches["count"] += 1
        raise AssertionError("renderer searched for replacement footage")

    monkeypatch.setattr("app.render._download_to", fake_download)
    monkeypatch.setattr("app.render._video_has_motion", fake_motion)
    monkeypatch.setattr("app.render.stock_service.search_stock", fail_search)

    scene = {"id": "scene-a", "narration_text": "steam rises from the kettle"}
    assets = [
        {
            "scene_id": "scene-a",
            "asset_type": "stock_video",
            "external_id": "dead-1",
            "download_url": "http://dead.example/missing.mp4",
        },
        {
            "scene_id": "scene-a",
            "asset_type": "stock_video",
            "external_id": "good-1",
            "download_url": "http://cdn.example/good-1.mp4",
        },
        {
            "scene_id": "scene-a",
            "asset_type": "stock_video",
            "external_id": "good-2",
            "download_url": "http://cdn.example/good-2.mp4",
        },
    ]

    async def run():
        used = set()
        first = await _resolve_scene_visual(
            scene, assets, {}, tmp_path, 0, used_ext_ids=used, allow_fallback=True,
        )
        used.add(str(first[2]))
        second = await _resolve_scene_visual(
            scene, assets, {}, tmp_path, 1, used_ext_ids=used, allow_fallback=True,
        )
        return first, second

    first, second = asyncio.run(run())
    assert first[1] == "video"
    assert first[2] == "good-1"
    assert second[2] == "good-2"
    assert searches["count"] == 0
