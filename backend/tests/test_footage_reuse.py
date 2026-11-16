"""Option 1: fill missing segments by reusing a scene's own on-topic clips.

Regression for egg render 7704a4c6: scenes had 1-3 accepted clips but the
sub-clip plan needed 6 distinct sources each, so the render searched stock
(unjudged) for the rest. Reuse must fill the plan first, never back to back,
with a new trim and zoom/pan per repeat. No network, no paid calls.
"""
from __future__ import annotations

import shutil
import subprocess
from itertools import groupby
from pathlib import Path

import pytest

from backend.app import footage_reuse as fr
from backend.app import render
from backend.app.render import (
    KEN_BURNS_DIRECTIONS,
    AttachedFootageUnavailable,
    _ffmpeg_normalise_video,
    _pick_borrowed_sources,
    validate_prerequisites,
)


def _no_adjacent_repeats(order):
    idx = [s for s, _p in order]
    return all(len(list(g)) == 1 for _k, g in groupby(idx))


def test_two_own_clips_fill_six_slots_alternating_with_new_passes():
    order = fr.schedule_reuse(2, 0, 6)
    assert [s for s, _ in order] == [0, 1, 0, 1, 0, 1]
    assert [p for _, p in order] == [0, 0, 1, 1, 2, 2]
    assert _no_adjacent_repeats(order)


def test_one_own_clip_borrows_neighbours_once_each_and_never_back_to_back():
    order = fr.schedule_reuse(1, 3, 6)
    sources = [s for s, _ in order]
    assert sources.count(0) == 3            # own clip at the cap
    assert sorted(sources.count(b) for b in (1, 2, 3)) == [1, 1, 1]
    assert _no_adjacent_repeats(order)


def test_enough_own_clips_keep_the_original_one_source_per_cut():
    assert fr.schedule_reuse(6, 0, 6) == [(i, 0) for i in range(6)]
    assert fr.schedule_reuse(8, 0, 6) == [(i, 0) for i in range(6)]


def test_borrowed_clips_are_only_used_for_the_shortfall():
    order = fr.schedule_reuse(2, 4, 6)       # own capacity 6 already fills it
    assert {s for s, _ in order} == {0, 1}


def test_no_own_clip_means_no_borrowing():
    assert fr.schedule_reuse(0, 5, 6) == []
    assert fr.reuse_capacity(0, 5) == 0
    assert fr.scene_can_fill_by_reuse(0, [9, 9], 6) is False


def test_single_source_still_fills_the_plan_without_inventing_footage():
    order = fr.schedule_reuse(1, 0, 4)
    assert [s for s, _ in order] == [0, 0, 0, 0]
    assert [p for _, p in order] == [0, 1, 2, 3]


def test_capacity_and_sources_needed():
    assert fr.reuse_capacity(2) == 6
    assert fr.reuse_capacity(1, 3) == 6
    assert fr.own_sources_needed(6) == 2
    assert fr.own_sources_needed(7) == 3
    assert fr.scene_can_fill_by_reuse(1, [3, 8], 10) is True
    assert fr.scene_can_fill_by_reuse(1, [], 10) is False


def test_each_use_of_a_still_moves_differently():
    dirs = {fr.ken_burns_direction(KEN_BURNS_DIRECTIONS, 1, 0, p) for p in range(3)}
    assert len(dirs) == 3


def test_repeated_video_gets_new_trim_and_motion():
    assert fr.video_reuse_motion(0) is None
    motions = [fr.video_reuse_motion(p) for p in (1, 2, 3)]
    assert len(set(motions)) == 3
    offsets = [fr.reuse_offset(p, 4.4, 20.0) for p in (0, 1, 2)]
    assert offsets[0] == 0.0
    assert len(set(offsets)) == 3
    assert all(0 <= o <= 20.0 - 4.4 for o in offsets)
    assert fr.reuse_offset(2, 5.0, 4.0) == 0.0   # short source: motion carries the change


def test_borrow_order_alternates_nearest_neighbours():
    order = fr.borrow_order(1, [["a1", "a2"], ["b1"], ["c1", "c2"]])
    assert order == [(0, 1), (2, 0), (0, 0), (2, 1)]
    assert fr.borrow_order(0, [["a1"], ["b1"]]) == [(1, 0)]


def test_pick_borrowed_sources_stops_at_capacity():
    own_sources = [[("p1", "video"), ("p2", "image")], [("own", "image")], [("n1", "image"), ("n2", "video")]]
    picked = _pick_borrowed_sources(1, own_sources, 1, 5, own_sources[1])
    assert len(picked) == 2 and ("own", "image") not in picked
    assert _pick_borrowed_sources(1, own_sources, 0, 5, []) == []


@pytest.mark.parametrize("motion", fr.VIDEO_REUSE_MOTIONS)
@pytest.mark.parametrize("w,h", [(1080, 1920), (1920, 1080)])
def test_motion_filter_is_built_for_every_aspect(motion, w, h):
    cmd = _ffmpeg_normalise_video(Path("src.mp4"), 4.4, Path("out.mp4"),
                                  start_offset=3.0, w=w, h=h, motion=motion)
    vf = cmd[cmd.index("-vf") + 1]
    assert f"crop={w}:{h}" in vf
    assert ("zoompan" in vf) if motion == "push_in" else ("min(t/4.400,1)" in vf)
    assert cmd[cmd.index("-ss") + 1] == "3.00"


def test_no_motion_keeps_the_existing_video_filter():
    plain = _ffmpeg_normalise_video(Path("s.mp4"), 4.0, Path("o.mp4"), w=1080, h=1920)
    again = _ffmpeg_normalise_video(Path("s.mp4"), 4.0, Path("o.mp4"), w=1080, h=1920, motion=None)
    assert plain == again


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg not installed")
@pytest.mark.parametrize("motion", fr.VIDEO_REUSE_MOTIONS)
def test_motion_filter_encodes_exact_size_and_duration(tmp_path, motion):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=25:duration=3", "-pix_fmt", "yuv420p", str(src)],
                   check=True)
    out = tmp_path / "out.mp4"
    cmd = _ffmpeg_normalise_video(src, 2.0, out, start_offset=0.5, w=360, h=640, motion=motion)
    cmd[0] = "ffmpeg"
    subprocess.run(cmd, check=True, capture_output=True)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                            "-show_entries", "stream=width,height,nb_read_frames",
                            "-of", "csv=p=0", str(out)], check=True, capture_output=True, text=True)
    width, height, frames = probe.stdout.strip().split(",")
    assert (int(width), int(height)) == (360, 640)
    assert abs(int(frames) - 60) <= 1


@pytest.mark.asyncio
async def test_failed_attached_clip_is_skipped_without_a_stock_search(monkeypatch, tmp_path):
    async def no_stock(*_a, **_k):
        raise AssertionError("render must not search stock when reuse can fill the scene")

    monkeypatch.setattr(render.stock_service, "search_stock", no_stock)
    monkeypatch.setattr(render.stock_service, "search_stock_videos", no_stock)

    async def fake_download(url, target, **_k):
        if "broken" in url:
            return False
        Path(target).write_bytes(b"jpg")
        return True

    monkeypatch.setattr(render, "_download_to", fake_download)
    scene = {"id": "s1", "narration_text": "Soft boiled eggs in a pot of water."}
    assets = [
        {"id": "a", "scene_id": "s1", "asset_type": "stock_image", "source": "pexels",
         "external_id": "broken1", "download_url": "https://x.test/broken1.jpg"},
        {"id": "b", "scene_id": "s1", "asset_type": "stock_image", "source": "pexels",
         "external_id": "ok1", "download_url": "https://x.test/ok1.jpg"},
        {"id": "c", "scene_id": "s1", "asset_type": "stock_image", "source": "pexels",
         "external_id": "ok2", "download_url": "https://x.test/ok2.jpg"},
    ]
    visuals = await render._resolve_scene_visuals(scene, assets, {"topic": "eggs"}, tmp_path, 0,
                                                  max_visuals=6, used_ext_ids=set(), topup=False)
    assert len(visuals) == 2 and all(kind == "image" for _p, kind in visuals)


@pytest.mark.asyncio
async def test_stock_retry_false_raises_instead_of_searching(monkeypatch, tmp_path):
    async def no_stock(*_a, **_k):
        raise AssertionError("no stock search")

    async def fail_download(*_a, **_k):
        return False

    monkeypatch.setattr(render.stock_service, "search_stock", no_stock)
    monkeypatch.setattr(render, "_download_to", fail_download)
    scene = {"id": "s1", "narration_text": "Poached eggs."}
    asset = {"id": "a", "scene_id": "s1", "asset_type": "stock_image", "source": "pexels",
             "external_id": "x", "download_url": "https://x.test/x.jpg"}
    with pytest.raises(AttachedFootageUnavailable):
        await render._resolve_scene_visual(scene, [asset], {}, tmp_path, 0, stock_retry=False)


def _egg_preflight(scene_assets):
    pid = "p-egg"
    project = {"id": pid, "target_duration": 136, "selected_voiceover_asset_id": "v1",
               "selected_thumbnail_asset_id": "t1"}
    durs = [26.066667, 28.266667, 26.1, 26.5, 26.566667]   # word-aligned, after the render
    scenes, t = [], 0.0
    for n, d in enumerate(durs, start=1):
        scenes.append({"id": f"s{n}", "scene_number": n, "duration": d,
                       "start_time": t, "end_time": t + d})
        t += d
    assets = [
        {"id": "t1", "asset_type": "generated_thumbnail"},
        {"id": "v1", "asset_type": "voiceover_audio", "duration": 132.68, "status": "selected"},
    ]
    k = 0
    for sid, count, hold in scene_assets:
        for _ in range(count):
            k += 1
            assets.append({"id": f"a{k}", "asset_type": "stock_image", "scene_id": sid,
                           "source": "pexels", "external_id": f"ext-{k}",
                           "download_url": f"https://stock.example/{k}.jpg",
                           "duration": hold, "status": "attached"})
    check = validate_prerequisites(project, {"full_script": "eggs " * 330}, scenes, {"id": "m"}, assets)
    return next(item for item in check["checklist"] if item["key"] == "scene_assets")


def test_preflight_counts_reuse_after_word_alignment_moved_scene_durations():
    # Scene 2's single photo, shown three times, is still one distinct clip.
    pacing = _egg_preflight([("s1", 3, 8.933), ("s2", 1, 26.767), ("s3", 8, 3.346),
                             ("s4", 7, 3.771), ("s5", 9, 3)])
    assert pacing["ok"] is False, pacing
    assert "Scene 2 has 1 of 8 required distinct clips - run Find footage for scene 2." in pacing["hint"]


def test_preflight_still_fails_a_scene_with_no_clip_of_its_own():
    pacing = _egg_preflight([("s1", 3, 8.933), ("s3", 8, 3.346), ("s4", 7, 3.771), ("s5", 9, 3)])
    assert pacing["ok"] is False
    assert "Scene 2 has 0 of 8 required distinct clips" in pacing["hint"]


def _plan(*counts):
    return {i: {"subclips": [4.4] * n} for i, n in enumerate(counts)}


@pytest.mark.asyncio
async def test_egg_shape_fills_by_reuse_with_no_stock_search(monkeypatch, tmp_path):
    """Scenes with 2, 1 and 6 clips and 6 slots each: no top-up at all."""
    async def no_topup(*_a, **_k):
        raise AssertionError("reuse could fill these scenes; stock must not be searched")

    monkeypatch.setattr(render, "_topup_scene_visuals", no_topup)
    s1 = [(Path("s1a.mp4"), "video"), (Path("s1b.jpg"), "image")]
    s2 = [(Path("s2a.jpg"), "image")]
    s3 = [(Path(f"s3{k}.jpg"), "image") for k in range(6)]
    scene_visuals = [(list(s1), {"id": "1"}), (list(s2), {"id": "2"}), (list(s3), {"id": "3"})]
    own_counts = [2, 1, 6]
    await render._fill_scenes_by_reuse(scene_visuals, own_counts, _plan(6, 6, 6), [], {},
                                       tmp_path, set(), commercial=False)
    assert own_counts == [2, 1, 6]
    assert scene_visuals[0][0] == s1                       # 2 own x 3 uses = 6, no borrowing
    v2 = scene_visuals[1][0]
    assert v2[0] == s2[0] and len(v2) == 4                # 1 own (3 uses) + 3 borrowed once
    assert set(v2[1:]) <= set(s1) | set(s3)
    assert scene_visuals[2][0] == s3


@pytest.mark.asyncio
async def test_stock_is_searched_only_when_reuse_cannot_fill(monkeypatch, tmp_path):
    calls = []

    async def fake_topup(scene, project, work_dir, idx, visuals, seen, used, target):
        calls.append((idx, target))
        visuals.append((Path("stock.mp4"), "video"))

    monkeypatch.setattr(render, "_topup_scene_visuals", fake_topup)
    scene_visuals = [([(Path("only.jpg"), "image")], {"id": "1"})]   # single scene, no neighbours
    own_counts = [1]
    await render._fill_scenes_by_reuse(scene_visuals, own_counts, _plan(6), [], {},
                                       tmp_path, set(), commercial=False)
    assert calls == [(0, 2)]          # just enough own sources for 3 uses each
    assert own_counts == [2]
    order = fr.schedule_reuse(own_counts[0], len(scene_visuals[0][0]) - own_counts[0], 6)
    assert _no_adjacent_repeats(order)


@pytest.mark.asyncio
async def test_commercial_never_borrows_or_searches(monkeypatch, tmp_path):
    async def no_topup(*_a, **_k):
        raise AssertionError("commercial projects never substitute stock")

    monkeypatch.setattr(render, "_topup_scene_visuals", no_topup)
    scene_visuals = [([(Path("c1.jpg"), "image")], {"id": "1"}), ([(Path("c2.jpg"), "image")], {"id": "2"})]
    own_counts = [1, 1]
    await render._fill_scenes_by_reuse(scene_visuals, own_counts, _plan(6, 6), [], {},
                                       tmp_path, set(), commercial=True)
    assert [len(v) for v, _s in scene_visuals] == [1, 1]


@pytest.mark.asyncio
async def test_director_scenes_are_left_alone(monkeypatch, tmp_path):
    async def no_topup(*_a, **_k):
        raise AssertionError("director scenes keep their own mapping")

    monkeypatch.setattr(render, "_topup_scene_visuals", no_topup)
    scene_visuals = [([(Path("d.mp4"), "video")], {"id": "1"})]
    own_counts = [None]
    await render._fill_scenes_by_reuse(scene_visuals, own_counts, _plan(6), [], {},
                                       tmp_path, set(), commercial=False)
    assert scene_visuals[0][0] == [(Path("d.mp4"), "video")] and own_counts == [None]
