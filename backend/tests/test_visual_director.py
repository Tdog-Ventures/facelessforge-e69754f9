import asyncio
import copy
from pathlib import Path
import json
import httpx
import pytest

from app.visual_director import (DirectorError, make_beats, validate_choices, direct,
                                 checked_shots, resolve_directed_scene, gemini_choices,
                                 stock_queries_by_scene)
from app.directed_graphics import graphic_frame


def test_deepseek_request_and_response_contract(monkeypatch):
    from app import visual_director
    beats = make_beats(scenes())
    captured = []
    def provider(request):
        payload = json.loads(request.content)
        captured.append((request.url.host, request.url.path, payload["model"]))
        assert json.loads(payload["messages"][1]["content"]) == beats
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(choices(beats))}}]})
    client = httpx.AsyncClient
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-only")
    monkeypatch.setattr(visual_director.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(provider), **kwargs))
    result = asyncio.run(visual_director.deepseek_choices(beats))
    assert len(validate_choices(beats, result)) == 3
    assert captured == [("api.deepseek.com", "/chat/completions", "deepseek-chat")]


def test_provider_error_does_not_leak_body_or_retry(monkeypatch):
    from app import visual_director
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(429, text="private-provider-body")
    client = httpx.AsyncClient
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-only")
    monkeypatch.setattr(visual_director.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(provider), **kwargs))
    with pytest.raises(DirectorError) as exc:
        asyncio.run(visual_director.deepseek_choices(make_beats(scenes())))
    assert "private-provider-body" not in str(exc.value)
    assert len(calls) == 1


def test_gemini_is_the_default_chooser_and_hides_provider_bodies(monkeypatch):
    from app import visual_director
    beats = make_beats(scenes())
    calls = []

    async def fake(model, system, payload, key):
        calls.append((model, key, payload))
        assert "stock_video" in system
        assert "visible subject" in system
        if key == "explode":
            raise RuntimeError("private-provider-body")
        return choices(beats)

    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "fixture-only")
    monkeypatch.setattr(visual_director, "_gemini_generate", fake)
    assert visual_director.direct.__defaults__[0] is gemini_choices
    result = asyncio.run(gemini_choices(beats))
    assert len(validate_choices(beats, result)) == 3
    assert calls[0][0] == "gemini-3.8-flash"
    assert calls[0][2] == beats
    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "explode")
    with pytest.raises(DirectorError) as exc:
        asyncio.run(gemini_choices(beats))
    assert "private-provider-body" not in str(exc.value)
    assert len(calls) == 2


def test_stock_queries_follow_saved_shot_order():
    plan = {"shots": [
        {"kind": "text", "scene_id": "scene-a", "labels": ["First"]},
        {"kind": "stock_video", "scene_id": "scene-a", "query": "hands editing timeline"},
        {"kind": "stock_video", "scene_id": "scene-a", "query": "hands editing timeline"},
        {"kind": "stock_video", "scene_id": "scene-b", "query": "city street night"},
    ]}
    assert stock_queries_by_scene(plan) == {
        "scene-a": ["hands editing timeline"],
        "scene-b": ["city street night"],
    }


def scenes():
    return [{"id": "scene-a", "scene_number": 1, "duration": 10,
             "narration_text": "First inspect the script. Then choose relevant footage. Finally review the result."}]


def choices(beats):
    return {"shots": [{"id": b["id"], "kind": "text", "labels": [b["narration"]]} for b in beats]}


def test_frame_budget_and_narration_are_preserved():
    source = scenes()
    original = copy.deepcopy(source)
    beats = make_beats(source)
    assert sum(b["frames"] for b in beats) == 300
    assert " ".join(b["narration"] for b in beats) == source[0]["narration_text"]
    assert source == original
    assert all(b["start_frame"] == sum(v["frames"] for v in beats[:i]) for i, b in enumerate(beats))


@pytest.mark.parametrize("kind", ["map", "code", "avatar", "<script>"])
def test_unsupported_types_fail(kind):
    beats = make_beats(scenes())
    data = choices(beats)
    data["shots"][0]["kind"] = kind
    with pytest.raises(DirectorError): validate_choices(beats, data)


def test_invented_facts_fail():
    beats = make_beats(scenes())
    data = choices(beats)
    data["shots"][0]["labels"] = ["Save 80% guaranteed"]
    with pytest.raises(DirectorError): validate_choices(beats, data)


def test_duplicate_and_missing_choices_fail():
    beats = make_beats(scenes())
    data = choices(beats)
    data["shots"][1] = data["shots"][0]
    with pytest.raises(DirectorError): validate_choices(beats, data)
    with pytest.raises(DirectorError): validate_choices(beats, {"shots": []})


def test_model_cannot_override_frames_or_narration():
    beats = make_beats(scenes())
    data = choices(beats)
    data["shots"][0].update(frames=999999, narration="rewritten")
    result = validate_choices(beats, data)
    assert result[0]["frames"] == beats[0]["frames"]
    assert result[0]["narration"] == beats[0]["narration"]


def test_stale_plan_is_rejected_and_input_unmodified():
    source = scenes()
    async def chooser(beats): return choices(beats)
    plan = asyncio.run(direct(source, "original script", chooser))
    assert len(checked_shots(plan, source, "original script")["scene-a"]) == 3
    with pytest.raises(DirectorError): checked_shots(plan, source, "edited script")
    changed = copy.deepcopy(source)
    changed[0]["narration_text"] = "Different narration"
    with pytest.raises(DirectorError): checked_shots(plan, changed, "original script")


def test_graphic_frames_change_and_match_dimensions():
    shot = {"kind": "steps", "labels": ["Inspect", "Review"]}
    first = graphic_frame(shot, 0, 640, 360)
    last = graphic_frame(shot, 1, 640, 360)
    assert first.size == (640, 360)
    assert first.tobytes() != last.tobytes()


def test_directed_stock_receives_its_query_and_preserves_order(tmp_path):
    captured = []
    async def stock(scene, assets, project, work, idx, **kwargs):
        captured.append((scene["visual_direction"], assets))
        return Path("footage.mp4"), "video", "external-1"
    shots = [{"kind": "stock_video", "query": "hands editing timeline", "frames": 90}]
    used = set()
    result = asyncio.run(resolve_directed_scene(scenes()[0], shots, {}, tmp_path,
                                               0, 640, 360, used, stock))
    assert captured == [("hands editing timeline", [])]
    assert result == [(Path("footage.mp4"), "video")]
    assert used == {"external-1"}


def test_directed_stock_prefers_existing_attachments(tmp_path):
    captured = []
    async def stock(scene, assets, project, work, idx, **kwargs):
        captured.append((list(assets), kwargs.get("allow_fallback")))
        return Path("footage.mp4"), "video", "external-9"
    attached = [{"id": "asset-1", "scene_id": "scene-a", "asset_type": "stock_video"}]
    shots = [{"kind": "stock_video", "query": "hands editing timeline", "frames": 90}]
    asyncio.run(resolve_directed_scene(scenes()[0], shots, {}, tmp_path,
                                       0, 640, 360, set(), stock, attached_assets=attached))
    assert captured == [(attached, False)]


def test_images_cannot_silently_replace_directed_footage(tmp_path, monkeypatch):
    import subprocess
    from app.render import _resolve_scene_visual

    searches = {"n": 0}

    async def fail_search(*args, **kwargs):
        searches["n"] += 1
        raise AssertionError("search_stock")

    monkeypatch.setattr("app.render.stock_service.search_stock", fail_search)
    monkeypatch.setattr("app.render.stock_service.search_stock_videos", fail_search)

    async def resolve(scene, assets, project, work, idx, **kwargs):
        assert kwargs.get("allow_fallback") is False
        if idx == 0:
            return await _resolve_scene_visual(
                scene, [], project, work, idx, used_ext_ids=set(), allow_fallback=False,
            )
        return Path("still.jpg"), "image", "not-footage"

    shots = [
        {"kind": "stock_video", "query": "boiling kettle releasing steam", "frames": 45,
         "narration": "The kitchen kettle steams on the counter."},
        {"kind": "stock_video", "query": "rain on a window", "frames": 45,
         "narration": "Rain taps the kitchen window above cups."},
    ]
    result = asyncio.run(resolve_directed_scene(
        scenes()[0], shots, {}, tmp_path, 0, 640, 360, set(), resolve))
    assert searches["n"] == 0
    assert [kind for _path, kind in result] == ["video", "video"]
    assert all(path.name.startswith("directed_000_") for path, _kind in result)
    assert Path("still.jpg") not in [path for path, _kind in result]
    graphic = result[0][0]
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_frames,duration", "-of", "json", str(graphic)],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(probe.stdout)
    stream = payload["streams"][0]
    duration = float(stream["duration"])
    assert abs(duration - 1.5) < 0.15
    frames = []
    for stamp, name in ((0.0, "a.png"), (1.1, "b.png")):
        dest = tmp_path / name
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(stamp), "-i", str(graphic),
             "-frames:v", "1", str(dest)],
            check=True,
        )
        frames.append(dest.read_bytes())
    assert frames[0] != frames[1]


def test_client_asset_projects_are_protected(tmp_path):
    with pytest.raises(DirectorError):
        asyncio.run(resolve_directed_scene(scenes()[0], [], {"production_mode": "commercial_ad"},
                    tmp_path, 0, 640, 360, set(), None))


def test_aggregated_search_preserves_a_director_query(monkeypatch):
    from app.stock import search_stock_aggregated
    monkeypatch.setenv("USE_MOCK_PEXELS", "true")
    query = "editor reviews footage timeline"
    result = asyncio.run(search_stock_aggregated(query, media_type="videos", per_page=2))
    assert result["query"] == query
    assert result["mock"] is True

def test_directed_plan_covers_pacing_without_stock(monkeypatch):
    from app.render import validate_prerequisites
    from app.timing import TimingContract

    narration = TimingContract.from_target(5).narration_seconds
    project = {
        "id": "p1", "target_duration": 5, "music_enabled": False,
        "selected_voiceover_asset_id": "v1", "selected_thumbnail_asset_id": "t1",
    }
    script = {"full_script": "Rain taps the kitchen window above cups."}
    scenes = [{
        "id": "s1", "scene_number": 1, "duration": narration, "end_time": narration,
        "narration_text": "Rain taps the kitchen window above cups.",
    }]
    metadata = {"id": "m1"}
    assets = [
        {"id": "t1", "asset_type": "generated_thumbnail"},
        {"id": "v1", "asset_type": "voiceover_audio", "duration": narration, "status": "selected"},
    ]

    def pacing(check):
        return next(item["ok"] for item in check["checklist"] if item["key"] == "scene_assets")

    plain = validate_prerequisites(project, script, scenes, metadata, assets)
    assert pacing(plain) is False
    assert "Scene visuals meet 3-second pacing" in plain["issues"]

    enabled = {**project, "visual_director": {"enabled": True}}
    unplanned = validate_prerequisites(enabled, script, scenes, metadata, assets)
    assert pacing(unplanned) is False

    def shots(_plan, _scenes, _script):
        return {"s1": [{"kind": "stock_video", "frames": 75}]}

    monkeypatch.setattr("app.visual_director.checked_shots", shots)
    covered = validate_prerequisites(enabled, script, scenes, metadata, assets)
    assert pacing(covered) is True
    assert covered["ok"] is True
