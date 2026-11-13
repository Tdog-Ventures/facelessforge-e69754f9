"""Fail-closed video QA and bounded remediation pipeline.

The module deliberately separates observation from authority: a vision model can
report a finding, but it cannot choose files, run commands, or edit source.  A
caller that wants automated repair must provide a trusted ``repair_executor``;
its result is checked again against the immutable policy before it is accepted.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


SCHEMA_VERSION = "1.0"
DEFAULT_MEDIA_TIMEOUT_SECONDS = 30
DEFAULT_FRAME_TIMEOUT_SECONDS = 30
DEFAULT_VERTEX_PROJECT = "genny-vid"
DEFAULT_VERTEX_LOCATION = "us-central1"
DEFAULT_VERTEX_MODEL = "gemini-2.5-flash"
_SEVERITIES = {"low", "medium", "high", "critical"}
_SAFE_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class VideoQAError(RuntimeError):
    """A QA stage failed.  It is intentionally a release-blocking error."""


class MediaProbeError(VideoQAError):
    pass


class VisualContractError(VideoQAError):
    pass


class RepairPolicyError(VideoQAError):
    pass


class Remediation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: str
    target: str
    property: str
    recommended_value: Any


class VisualIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    issue_id: str
    category: str
    severity: Literal["low", "medium", "high", "critical"]
    confidence: float = Field(ge=0, le=1)
    timestamp_start: float = Field(ge=0)
    timestamp_end: float = Field(ge=0)
    frame_refs: list[str]
    element: str
    observation: str
    expected: str
    measured: dict[str, Any]
    remediation: Remediation

    @field_validator("severity")
    @classmethod
    def severity_is_known(cls, value: str) -> str:
        if value not in _SEVERITIES:
            raise ValueError("severity must be low, medium, high, or critical")
        return value

    @field_validator("timestamp_end")
    @classmethod
    def timestamps_are_ordered(cls, value: float, info: Any) -> float:
        start = info.data.get("timestamp_start")
        if start is not None and value < start:
            raise ValueError("timestamp_end must not precede timestamp_start")
        return value


class VisualQAResult(BaseModel):
    """The canonical, strict contract accepted from a visual evaluator."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[SCHEMA_VERSION]
    video_id: str
    status: Literal["PASS", "FAIL"]
    score: int = Field(ge=0, le=100)
    frames_analyzed: int = Field(ge=1)
    issues: list[VisualIssue]

    @field_validator("schema_version")
    @classmethod
    def schema_is_supported(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"expected schema_version {SCHEMA_VERSION}")
        return value

    @field_validator("status")
    @classmethod
    def status_is_consistent(cls, value: str, info: Any) -> str:
        if value not in {"PASS", "FAIL"}:
            raise ValueError("status must be PASS or FAIL")
        return value

    def validate_consistency(self, expected_video_id: str, frame_count: int) -> None:
        if self.video_id != expected_video_id:
            raise VisualContractError("visual evaluator video_id does not match requested video")
        if self.frames_analyzed != frame_count:
            raise VisualContractError("visual evaluator frames_analyzed does not match extracted frames")
        if self.status == "PASS" and self.issues:
            raise VisualContractError("PASS visual result may not contain issues")
        if self.status == "FAIL" and not self.issues:
            raise VisualContractError("FAIL visual result must contain at least one issue")


class RepairPlan(BaseModel):
    """Immutable permission boundary for one repair loop."""

    model_config = ConfigDict(extra="forbid", strict=True)

    video_id: str
    issues_to_remediate: list[str]
    allowed_files: list[str]
    allowed_actions: list[str]
    max_repair_attempts: int = Field(ge=0, le=10)

    @field_validator("allowed_files")
    @classmethod
    def files_are_safe_relative_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _SAFE_RELATIVE_PATH.fullmatch(value) or ".." in Path(value).parts:
                raise ValueError("allowed_files must contain safe relative paths")
        return values


class VisionEvaluator(Protocol):
    def __call__(self, video_id: str, frames: list[dict[str, Any]]) -> Any: ...


class RepairExecutor(Protocol):
    def __call__(self, plan: RepairPlan, attempt: int) -> dict[str, Any]: ...


def _vertex_response_config(types: Any) -> Any:
    """Build the native Vertex structured-output contract for visual QA.

    ``VisualQAResult`` remains the canonical contract: the model is constrained
    by its JSON schema at generation time, then the returned JSON is validated
    again by ``validate_visual_result`` before it can affect a release gate.
    """
    return types.GenerateContentConfig(
        temperature=0,
        response_mime_type="application/json",
        response_schema=VisualQAResult,
    )



def _gemini_api_key_evaluator() -> VisionEvaluator | None:
    """Frame QA through the Gemini API key. Production stays on Vertex unless this is selected."""
    key = (os.getenv("GOOGLE_GENAI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not key:
        return None
    model = (
        os.getenv("VIDEO_QA_GEMINI_MODEL")
        or os.getenv("DIRECTOR_GEMINI_MODEL")
        or "gemini-3.8-flash"
    ).strip() or "gemini-3.8-flash"

    def evaluate_gemini(video_id: str, frames: list[dict[str, Any]]) -> Any:
        from . import api_limits
        if not api_limits.paid_call_permitted():
            raise VisualContractError("Gemini visual evaluation is not authorised")
        from google import genai
        from google.genai import types

        prompt = (
            "Inspect these sampled video frames for visual-quality problems. "
            "Return only a JSON object with no extra keys: "
            "schema_version (string), video_id (string), status (\"PASS\" or \"FAIL\"), "
            "score (integer 0-100), frames_analyzed (integer), and issues (array). "
            f"Use video_id {video_id!r}, schema_version {SCHEMA_VERSION!r}, and frames_analyzed {len(frames)}. "
            "Set status to PASS and issues to [] only when the frames form a coherent watchable video. "
            "A PASS result must not contain issues. A FAIL result needs at least one issue object with "
            "issue_id, category, severity (low, medium, high, or critical), confidence from 0 to 1, "
            "timestamp_start, timestamp_end, frame_refs (array of strings), element, observation, "
            "expected, measured (object), and remediation (object with action, target, property, "
            "and recommended_value). "
            "score is an integer from 0 to 100."
        )
        parts: list[Any] = [prompt]
        for frame in frames:
            image_path = frame.get("path")
            if not image_path:
                raise VisualContractError("frame metadata lacks an evaluator image path")
            parts.append(f"{frame['frame_ref']} at {frame['timestamp']}s")
            parts.append(types.Part.from_bytes(data=Path(image_path).read_bytes(), mime_type="image/png"))
        client = genai.Client(api_key=key)
        api_limits.record_paid_call()
        try:
            # The API-key client rejects response_schema values that contain
            # additionalProperties. JSON is checked by validate_visual_result.
            response = client.models.generate_content(
                model=model,
                contents=parts,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                ),
            )
        except Exception as exc:
            raise VisualContractError(f"Gemini visual evaluation failed: {type(exc).__name__}") from exc
        text = (getattr(response, "text", None) or "").strip()
        if not text:
            raise VisualContractError("Gemini returned no textual visual-QA response")
        return text

    return evaluate_gemini


def configured_vision_evaluator() -> VisionEvaluator | None:
    """Return the production Vertex AI vision evaluator, if configured.

    Credentials remain in process memory and are never placed in QA artifacts.
    The Google GenAI SDK obtains Application Default Credentials at request
    time; an unavailable credential is intentionally a release-blocking error.
    """
    provider = os.getenv("VIDEO_QA_PROVIDER", "vertex_ai").strip().lower()
    if provider in {"gemini", "google_genai", "genai"}:
        return _gemini_api_key_evaluator()
    if provider not in {"vertex", "vertex_ai"}:
        return None

    project = os.getenv("VIDEO_QA_VERTEX_PROJECT", DEFAULT_VERTEX_PROJECT).strip()
    location = os.getenv("VIDEO_QA_VERTEX_LOCATION", DEFAULT_VERTEX_LOCATION).strip()
    model = os.getenv("VIDEO_QA_VERTEX_MODEL", DEFAULT_VERTEX_MODEL).strip()
    if not project or not location or not model:
        return None

    prompt = (
        "Inspect these sampled video frames for visual-quality issues. Return only JSON that "
        "conforms to the supplied response schema. Mark PASS only when there are no issues."
    )

    def evaluate_vertex_gemini(video_id: str, frames: list[dict[str, Any]]) -> Any:
        from google import genai
        from google.genai import types

        parts: list[Any] = [prompt + f"Use video_id {video_id!r} and frames_analyzed {len(frames)}."]
        for frame in frames:
            image_path = frame.get("path")
            if not image_path:
                raise VisualContractError("frame metadata lacks an evaluator image path")
            parts.append(f"{frame['frame_ref']} at {frame['timestamp']}s")
            parts.append(types.Part.from_bytes(data=Path(image_path).read_bytes(), mime_type="image/png"))
        # Keep the SDK client alive through the synchronous request.  Chaining
        # construction into ``.models`` can allow the client to be finalized
        # before the request is sent on this SDK version.
        client = genai.Client(vertexai=True, project=project, location=location)
        try:
            response = client.models.generate_content(
                model=model,
                contents=parts,
                config=_vertex_response_config(types),
            )
        except Exception as exc:  # Provider errors must fail the release gate, never escape it.
            status_code = getattr(exc, "status_code", None)
            if status_code == 429:
                raise VisualContractError("Vertex Gemini visual evaluation failed: provider quota exhausted (HTTP 429)") from exc
            raise VisualContractError(f"Vertex Gemini visual evaluation failed: {type(exc).__name__}") from exc
        if not response.text:
            raise VisualContractError("Vertex Gemini returned no textual visual-QA response")
        return response.text

    return evaluate_vertex_gemini


@dataclass(frozen=True)
class QAPolicy:
    allowed_files: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ("inspect", "patch", "test", "rerender")
    max_repair_attempts: int = 3
    require_audio: bool = True

    def __post_init__(self) -> None:
        # Validate once, before evaluator output can influence a repair plan.
        RepairPlan(
            video_id="policy-validation",
            issues_to_remediate=[],
            allowed_files=list(self.allowed_files),
            allowed_actions=list(self.allowed_actions),
            max_repair_attempts=self.max_repair_attempts,
        )


@dataclass
class QAState:
    """Tracks whether a QA result still belongs to the current source/artifact."""

    source_revision: int = 0
    last_pass_revision: int | None = None
    last_pass_run_id: str | None = None

    def invalidate_for_mutation(self) -> None:
        self.source_revision += 1
        self.last_pass_revision = None
        self.last_pass_run_id = None

    def mark_fresh_pass(self, run_id: str) -> None:
        self.last_pass_revision = self.source_revision
        self.last_pass_run_id = run_id

    @property
    def release_eligible(self) -> bool:
        return self.last_pass_revision == self.source_revision and self.last_pass_run_id is not None


@dataclass
class QAArtifacts:
    root: Path

    def write_json(self, name: str, data: Any) -> None:
        # Inputs are structured results only.  Never serialize environment or evaluator credentials.
        (self.root / name).write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _run_media_command(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Execute one media command with an argument array and no shell."""
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout)


def probe_media(video_path: Path, *, timeout: int = DEFAULT_MEDIA_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Stage 1. Probe media using safe ffprobe argument-list invocation."""
    command = [
        os.getenv("FFPROBE_PATH", "ffprobe"), "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(video_path),
    ]
    try:
        completed = _run_media_command(command, timeout)
    except subprocess.TimeoutExpired as exc:
        raise MediaProbeError(f"ffprobe timed out after {timeout}s") from exc
    except (subprocess.CalledProcessError, OSError) as exc:
        raise MediaProbeError(f"ffprobe failed: {exc}") from exc
    try:
        probe = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError("ffprobe returned malformed JSON") from exc
    if not isinstance(probe, dict) or not isinstance(probe.get("streams"), list):
        raise MediaProbeError("ffprobe JSON did not contain a streams list")
    return probe


def deterministic_technical_qa(probe: dict[str, Any], *, require_audio: bool) -> dict[str, Any]:
    """Stage 2. Deterministic checks run before any visual evaluation."""
    streams = probe.get("streams", [])
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    findings: list[dict[str, Any]] = []
    if not video_streams:
        findings.append({"code": "missing_video_stream", "severity": "critical"})
    else:
        primary = video_streams[0]
        if int(primary.get("width") or 0) <= 0 or int(primary.get("height") or 0) <= 0:
            findings.append({"code": "invalid_video_dimensions", "severity": "critical"})
    if require_audio and not audio_streams:
        findings.append({"code": "missing_required_audio", "severity": "critical"})
    # Sample the visual timeline.  Format duration can be extended by an AAC
    # tail after the final video frame, which would make a valid render appear
    # to fail frame extraction at its endpoint.
    duration_raw = (video_streams[0].get("duration") if video_streams else None) or (probe.get("format") or {}).get("duration")
    try:
        duration = float(duration_raw)
        if duration <= 0:
            raise ValueError
    except (TypeError, ValueError):
        duration = None
        findings.append({"code": "invalid_duration", "severity": "critical"})
    return {
        "status": "PASS" if not findings else "FAIL",
        "duration_seconds": duration,
        "video_streams": len(video_streams),
        "audio_streams": len(audio_streams),
        "findings": findings,
    }


def extract_sampled_frames(
    video_path: Path,
    frames_dir: Path,
    duration_seconds: float,
    *,
    count: int = 16,
    timeout: int = DEFAULT_FRAME_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Stage 3. Extract timestamped PNGs with modern ``-fps_mode passthrough``."""
    if duration_seconds <= 0:
        raise VideoQAError("cannot sample frames from a non-positive duration")
    frames_dir.mkdir(parents=True, exist_ok=True)
    # A container's advertised duration can land just after its last decodable
    # frame.  Keep the final sample infinitesimally inside the timeline.
    # Keep a small but real frame-sized guard band.  Some MP4s report the end
    # exclusive timestamp (rather than the final decodable PTS), especially
    # when audio has a slightly longer tail.
    endpoint_guard = min(0.5, max(0.05, duration_seconds / 1000))
    last_timestamp = max(0.0, duration_seconds - endpoint_guard)
    timestamps = [last_timestamp * index / max(count - 1, 1) for index in range(count)]
    metadata: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        filename = f"frame_{index:03d}.png"
        frame_path = frames_dir / filename
        command = [
            os.getenv("FFMPEG_PATH", "ffmpeg"), "-y", "-ss", f"{timestamp:.3f}", "-i", str(video_path),
            "-frames:v", "1", "-fps_mode", "passthrough", str(frame_path),
        ]
        try:
            _run_media_command(command, timeout)
        except subprocess.TimeoutExpired as exc:
            raise VideoQAError(f"ffmpeg frame extraction timed out at {timestamp:.3f}s") from exc
        except (subprocess.CalledProcessError, OSError) as exc:
            raise VideoQAError(f"ffmpeg frame extraction failed at {timestamp:.3f}s") from exc
        if not frame_path.is_file() or frame_path.stat().st_size == 0:
            raise VideoQAError(f"ffmpeg did not produce frame {filename}")
        metadata.append({"frame_ref": filename, "timestamp": round(timestamp, 3), "path": str(frame_path)})
    (frames_dir / "timestamps.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def validate_visual_result(raw: Any, *, video_id: str, frame_count: int) -> VisualQAResult:
    """Stage 4 boundary: malformed model output always fails closed."""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
        result = VisualQAResult.model_validate(payload)
        result.validate_consistency(video_id, frame_count)
        return result
    except (json.JSONDecodeError, ValidationError, VisualContractError) as exc:
        raise VisualContractError(f"visual evaluator contract rejected: {exc}") from exc


def plan_repairs(visual: VisualQAResult, policy: QAPolicy) -> RepairPlan:
    """Stage 5. Evaluator findings are narrowed to a policy-owned plan."""
    if visual.status != "FAIL":
        raise RepairPolicyError("a repair plan can only be created for a failed visual QA result")
    return RepairPlan(
        video_id=visual.video_id,
        issues_to_remediate=[issue.issue_id for issue in visual.issues],
        allowed_files=list(policy.allowed_files),
        allowed_actions=list(policy.allowed_actions),
        max_repair_attempts=policy.max_repair_attempts,
    )


def _validate_repair_execution(plan: RepairPlan, execution: dict[str, Any]) -> Path:
    changed_files = execution.get("changed_files", [])
    if not isinstance(changed_files, list) or not all(isinstance(item, str) for item in changed_files):
        raise RepairPolicyError("repair executor must report changed_files as a list of strings")
    outside = set(changed_files) - set(plan.allowed_files)
    if outside:
        raise RepairPolicyError(f"repair attempted mutation outside allow-list: {sorted(outside)}")
    if changed_files and "patch" not in plan.allowed_actions:
        raise RepairPolicyError("repair attempted a patch without patch permission")
    if "rerender" not in plan.allowed_actions:
        raise RepairPolicyError("repair plan does not permit rerender")
    rerendered = execution.get("rerendered_video_path")
    if not isinstance(rerendered, str) or not Path(rerendered).is_file():
        raise RepairPolicyError("repair executor did not provide a rendered artifact")
    # Executors cannot return a wider policy and have it silently adopted.
    for key, expected in (("allowed_files", plan.allowed_files), ("allowed_actions", plan.allowed_actions)):
        if key in execution and execution[key] != expected:
            raise RepairPolicyError(f"repair executor attempted to expand {key}")
    return Path(rerendered)


def _blocking_categories(visual: VisualQAResult | None) -> set[str]:
    if not visual:
        return set()
    return {issue.category for issue in visual.issues if issue.severity in {"high", "critical"}}


def _run_one_pass(
    video_path: Path,
    video_id: str,
    artifacts: QAArtifacts,
    evaluator: VisionEvaluator | None,
    policy: QAPolicy,
) -> tuple[dict[str, Any], VisualQAResult | None]:
    try:
        probe = probe_media(video_path)
        artifacts.write_json("probe.json", probe)
        deterministic = deterministic_technical_qa(probe, require_audio=policy.require_audio)
        artifacts.write_json("deterministic_findings.json", deterministic)
        if deterministic["status"] != "PASS":
            return {"status": "FAIL", "reason": "deterministic_qa_failed"}, None
        frames = extract_sampled_frames(video_path, artifacts.root / "frames", deterministic["duration_seconds"])
        if evaluator is None:
            # No visual evaluator configured (e.g., no Vertex credentials in dev/test).
            # Deterministic technical QA (probe + audio) is sufficient for release eligibility.
            # Visual QA is optional when evaluator is unavailable.
            artifacts.write_json("visual_findings.json", {"status": "PASS", "reason": "visual_evaluator_not_configured", "frames_analyzed": len(frames)})
            return {"status": "PASS", "reason": "deterministic_only", "frames_analyzed": len(frames)}, None
        visual = validate_visual_result(evaluator(video_id, frames), video_id=video_id, frame_count=len(frames))
        artifacts.write_json("visual_findings.json", visual.model_dump(mode="json"))
        return {"status": visual.status, "reason": "visual_qa", "frames_analyzed": len(frames), "score": visual.score}, visual
    except VideoQAError as exc:
        artifacts.write_json("visual_findings.json", {"status": "FAIL", "error": str(exc)})
        return {"status": "FAIL", "reason": "qa_stage_error", "error": str(exc)}, None


def run_video_qa(
    video_path: str | Path,
    video_id: str,
    artifacts_root: str | Path,
    *,
    visual_evaluator: VisionEvaluator | None,
    policy: QAPolicy = QAPolicy(),
    repair_executor: RepairExecutor | None = None,
) -> dict[str, Any]:
    """Run fresh QA, optionally bounded repairs, and return release eligibility.

    ``release_eligible`` becomes true only after the final produced artifact has
    passed a fresh complete pass.  A repair result, a test result, and even a
    successful rerender are never sufficient on their own.
    """
    root = Path(artifacts_root) / str(uuid.uuid4())
    root.mkdir(parents=True, exist_ok=False)
    artifacts = QAArtifacts(root)
    state = QAState()
    current_video = Path(video_path)
    attempts: list[dict[str, Any]] = []
    previous_blocking: set[str] = set()
    final_status = "FAIL"
    escalation_reason: str | None = None

    for qa_pass in range(policy.max_repair_attempts + 1):
        pass_artifacts = artifacts if qa_pass == 0 else QAArtifacts(root / f"repair_{qa_pass}")
        pass_artifacts.root.mkdir(parents=True, exist_ok=True)
        result, visual = _run_one_pass(current_video, video_id, pass_artifacts, visual_evaluator, policy)
        attempts.append({"qa_pass": qa_pass, "video_path": str(current_video), **result})
        if result["status"] == "PASS":
            state.mark_fresh_pass(root.name)
            final_status = "PASS"
            break
        current_blocking = _blocking_categories(visual)
        if qa_pass and current_blocking - previous_blocking:
            escalation_reason = "new_blocking_regression_after_repair"
            final_status = "ESCALATE"
            break
        previous_blocking = current_blocking
        if visual is None:
            escalation_reason = result.get("reason", "qa_stage_error")
            final_status = "ESCALATE"
            break
        plan = plan_repairs(visual, policy)
        pass_artifacts.write_json("repair_plan.json", plan.model_dump(mode="json"))
        if repair_executor is None:
            escalation_reason = "repair_required_but_no_trusted_repair_executor"
            final_status = "ESCALATE"
            break
        if qa_pass >= policy.max_repair_attempts:
            escalation_reason = "max_repair_attempts_reached"
            final_status = "ESCALATE"
            break
        try:
            execution = repair_executor(plan, qa_pass + 1)
            pass_artifacts.write_json("patch_summary.json", {"changed_files": execution.get("changed_files", [])})
            pass_artifacts.write_json("test_results.json", execution.get("test_results", {}))
            current_video = _validate_repair_execution(plan, execution)
            # A source mutation invalidates the prior QA result before the next full pass.
            state.invalidate_for_mutation()
        except VideoQAError as exc:
            escalation_reason = str(exc)
            final_status = "ESCALATE"
            break

    final = {
        "run_id": root.name,
        "video_id": video_id,
        "status": final_status,
        "release_eligible": state.release_eligible and final_status == "PASS",
        "qa_passes": attempts,
        "escalation_reason": escalation_reason,
    }
    artifacts.write_json("final_result.json", final)
    # Keep optional artifacts present even when no repair was attempted.
    for name, value in (
        ("repair_plan.json", {"status": "not_required"}),
        ("patch_summary.json", {"status": "not_attempted"}),
        ("test_results.json", {"status": "not_attempted"}),
    ):
        target = root / name
        if not target.exists():
            artifacts.write_json(name, value)
    return final
