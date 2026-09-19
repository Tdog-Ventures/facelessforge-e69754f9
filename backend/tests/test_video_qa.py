"""Unit coverage for the fail-closed Agentic Video QA release gate."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app import video_qa


def _probe(*, audio: bool = True) -> dict:
    streams = [{"codec_type": "video", "width": 1920, "height": 1080}]
    if audio:
        streams.append({"codec_type": "audio"})
    return {"format": {"duration": "2.0"}, "streams": streams}


def _pass_visual(video_id: str, frames: list[dict]) -> dict:
    return {
        "schema_version": "1.0", "video_id": video_id, "status": "PASS",
        "score": 100, "frames_analyzed": len(frames), "issues": [],
    }


def _fail_visual(video_id: str, frames: list[dict], *, category: str = "safe_zone_violation") -> dict:
    return {
        "schema_version": "1.0", "video_id": video_id, "status": "FAIL", "score": 50,
        "frames_analyzed": len(frames),
        "issues": [{
            "issue_id": "QA-001", "category": category, "severity": "high", "confidence": 0.99,
            "timestamp_start": 0.0, "timestamp_end": 1.0, "frame_refs": ["frame_000.png"],
            "element": "caption", "observation": "Bad placement", "expected": "Safe placement",
            "measured": {},
            "remediation": {"action": "move_element", "target": "caption", "property": "y", "recommended_value": 10},
        }],
    }


def test_valid_exact_visual_schema_is_accepted():
    result = video_qa.validate_visual_result(_fail_visual("render-1", [{}]), video_id="render-1", frame_count=1)
    assert result.status == "FAIL"
    assert result.issues[0].measured == {}


def test_measured_object_alias_is_rejected():
    visual = _fail_visual("render-1", [{}])
    issue = visual["issues"][0]
    issue["measured_object"] = issue.pop("measured")
    with pytest.raises(video_qa.VisualContractError, match="measured"):
        video_qa.validate_visual_result(visual, video_id="render-1", frame_count=1)


def test_missing_measured_is_rejected():
    visual = _fail_visual("render-1", [{}])
    del visual["issues"][0]["measured"]
    with pytest.raises(video_qa.VisualContractError, match="measured"):
        video_qa.validate_visual_result(visual, video_id="render-1", frame_count=1)


def test_malformed_issue_is_rejected():
    visual = _fail_visual("render-1", [{}])
    visual["issues"][0]["severity"] = "urgent"
    with pytest.raises(video_qa.VisualContractError, match="severity"):
        video_qa.validate_visual_result(visual, video_id="render-1", frame_count=1)


def test_valid_no_issue_pass_response_is_accepted():
    result = video_qa.validate_visual_result(_pass_visual("render-1", [{}]), video_id="render-1", frame_count=1)
    assert result.status == "PASS"
    assert result.issues == []


def test_vertex_uses_native_pydantic_response_schema():
    from google.genai import types

    config = video_qa._vertex_response_config(types)
    assert config.response_mime_type == "application/json"
    assert config.response_schema is video_qa.VisualQAResult


def test_unprobeable_render_video_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from app import render

    async def fail_probe(*_args, **_kwargs):
        raise OSError("ffprobe unavailable")

    monkeypatch.setattr(render, "_resolve_ffprobe_bin", lambda: "ffprobe")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_probe)
    assert asyncio.run(render._video_has_motion(tmp_path / "invalid.mp4")) is False


def test_native_schema_requires_measured_and_lowercase_severity_enum():
    schema = video_qa.VisualQAResult.model_json_schema()
    issue = schema["$defs"]["VisualIssue"]
    assert "measured" in issue["required"]
    assert issue["properties"]["severity"]["enum"] == ["low", "medium", "high", "critical"]


def test_ffprobe_uses_safe_argument_list_check_and_timeout(tmp_path: Path):
    path = tmp_path / "video with spaces;$(nope).mp4"
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(_probe()), stderr="")
    with patch("app.video_qa.subprocess.run", return_value=completed) as run:
        assert video_qa.probe_media(path, timeout=17) == _probe()
    args = run.call_args.args[0]
    kwargs = run.call_args.kwargs
    assert isinstance(args, list)
    assert args[-1] == str(path)
    assert kwargs["check"] is True
    assert kwargs["timeout"] == 17
    assert kwargs.get("shell", False) is False


def test_probe_timeout_is_release_blocking(tmp_path: Path):
    with patch("app.video_qa.subprocess.run", side_effect=subprocess.TimeoutExpired(["ffprobe"], 1)):
        with pytest.raises(video_qa.MediaProbeError, match="timed out"):
            video_qa.probe_media(tmp_path / "video.mp4", timeout=1)


def test_malformed_ffprobe_json_is_rejected(tmp_path: Path):
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="not-json", stderr="")
    with patch("app.video_qa.subprocess.run", return_value=completed):
        with pytest.raises(video_qa.MediaProbeError, match="malformed JSON"):
            video_qa.probe_media(tmp_path / "video.mp4")


def test_missing_video_stream_and_required_audio_fail_deterministic_qa():
    no_video = {"format": {"duration": "2"}, "streams": [{"codec_type": "audio"}]}
    no_audio = _probe(audio=False)
    assert "missing_video_stream" in {f["code"] for f in video_qa.deterministic_technical_qa(no_video, require_audio=True)["findings"]}
    assert "missing_required_audio" in {f["code"] for f in video_qa.deterministic_technical_qa(no_audio, require_audio=True)["findings"]}


def test_malformed_visual_schema_fails_closed():
    with pytest.raises(video_qa.VisualContractError):
        video_qa.validate_visual_result({"status": "PASS"}, video_id="render-1", frame_count=16)


def test_vertex_defaults_configure_vision_evaluator(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VIDEO_QA_PROVIDER", raising=False)
    monkeypatch.delenv("VIDEO_QA_VERTEX_PROJECT", raising=False)
    monkeypatch.delenv("VIDEO_QA_VERTEX_LOCATION", raising=False)
    monkeypatch.delenv("VIDEO_QA_VERTEX_MODEL", raising=False)
    assert video_qa.configured_vision_evaluator() is not None


def test_non_vertex_provider_disables_vision_evaluator(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VIDEO_QA_PROVIDER", "openai")
    assert video_qa.configured_vision_evaluator() is None


def _fake_media_commands(tmp_path: Path):
    def fake(args: list[str], timeout: int):
        if "ffprobe" in Path(args[0]).name:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(_probe()), stderr="")
        Path(args[-1]).write_bytes(b"png")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    return fake


def test_frame_extraction_failure_blocks_vision_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    calls = Mock()

    def fail_frame(args: list[str], timeout: int):
        if "ffprobe" in Path(args[0]).name:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(_probe()), stderr="")
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(video_qa, "_run_media_command", fail_frame)
    result = video_qa.run_video_qa(video, "render-1", tmp_path / "qa", visual_evaluator=lambda *args: calls())
    assert result["release_eligible"] is False
    assert result["escalation_reason"] == "qa_stage_error"
    calls.assert_not_called()


def test_provider_failure_is_recorded_as_a_fail_closed_qa_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(video_qa, "_run_media_command", _fake_media_commands(tmp_path))

    def unavailable(*_args):
        raise video_qa.VisualContractError("Gemini visual evaluation failed: provider quota exhausted (HTTP 429)")

    result = video_qa.run_video_qa(video, "render-1", tmp_path / "qa", visual_evaluator=unavailable)
    assert result["status"] == "ESCALATE"
    assert result["release_eligible"] is False
    assert result["escalation_reason"] == "qa_stage_error"


def test_mutation_outside_allow_list_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(video_qa, "_run_media_command", _fake_media_commands(tmp_path))
    policy = video_qa.QAPolicy(allowed_files=("src/render/captions.py",), max_repair_attempts=1)
    result = video_qa.run_video_qa(
        video, "render-1", tmp_path / "qa", visual_evaluator=_fail_visual, policy=policy,
        repair_executor=lambda plan, attempt: {"changed_files": ["outside.py"], "rerendered_video_path": str(video)},
    )
    assert result["status"] == "ESCALATE"
    assert "outside allow-list" in result["escalation_reason"]


def test_repair_limit_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(video_qa, "_run_media_command", _fake_media_commands(tmp_path))
    executor = Mock(return_value={"changed_files": [], "rerendered_video_path": str(video)})
    result = video_qa.run_video_qa(
        video, "render-1", tmp_path / "qa", visual_evaluator=_fail_visual,
        policy=video_qa.QAPolicy(max_repair_attempts=2), repair_executor=executor,
    )
    assert result["status"] == "ESCALATE"
    assert result["escalation_reason"] == "max_repair_attempts_reached"
    assert executor.call_count == 2


def test_source_mutation_invalidates_prior_qa_result():
    state = video_qa.QAState()
    state.mark_fresh_pass("first-run")
    assert state.release_eligible is True
    state.invalidate_for_mutation()
    assert state.release_eligible is False


def test_release_eligibility_requires_fresh_full_qa_after_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(video_qa, "_run_media_command", _fake_media_commands(tmp_path))
    evaluator_calls = 0

    def evaluator(video_id: str, frames: list[dict]) -> dict:
        nonlocal evaluator_calls
        evaluator_calls += 1
        return _fail_visual(video_id, frames) if evaluator_calls == 1 else _pass_visual(video_id, frames)

    result = video_qa.run_video_qa(
        video, "render-1", tmp_path / "qa", visual_evaluator=evaluator,
        policy=video_qa.QAPolicy(allowed_files=("src/render/captions.py",), max_repair_attempts=1),
        repair_executor=lambda plan, attempt: {"changed_files": ["src/render/captions.py"], "rerendered_video_path": str(video), "test_results": {"passed": True}},
    )
    assert result["release_eligible"] is True
    assert evaluator_calls == 2
    assert len(result["qa_passes"]) == 2


def test_new_blocking_regression_after_repair_escalates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(video_qa, "_run_media_command", _fake_media_commands(tmp_path))
    calls = 0

    def evaluator(video_id: str, frames: list[dict]) -> dict:
        nonlocal calls
        calls += 1
        return _fail_visual(video_id, frames, category="safe_zone_violation" if calls == 1 else "caption_overlap")

    result = video_qa.run_video_qa(
        video, "render-1", tmp_path / "qa", visual_evaluator=evaluator,
        policy=video_qa.QAPolicy(allowed_files=("src/render/captions.py",), max_repair_attempts=2),
        repair_executor=lambda plan, attempt: {"changed_files": ["src/render/captions.py"], "rerendered_video_path": str(video)},
    )
    assert result["status"] == "ESCALATE"
    assert result["escalation_reason"] == "new_blocking_regression_after_repair"


def test_frame_extraction_uses_modern_fps_mode_and_never_shells(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured: list[list[str]] = []

    def fake(args: list[str], timeout: int):
        captured.append(args)
        Path(args[-1]).write_bytes(b"png")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video_qa, "_run_media_command", fake)
    frames = video_qa.extract_sampled_frames(tmp_path / "video name.mp4", tmp_path / "frames", 1, count=2)
    assert len(frames) == 2
    assert all("-fps_mode" in command and command[command.index("-fps_mode") + 1] == "passthrough" for command in captured)
