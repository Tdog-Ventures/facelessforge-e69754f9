#!/usr/bin/env python3
"""Local task telemetry and optional monitor heartbeat delivery for ETHINX jobs."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

# Deploy root: /opt/facelessforge/deploy (Adelaide prod)
DEPLOY_ROOT = Path(os.environ.get("DEPLOY_ROOT", Path(__file__).resolve().parent))
# Revenue loop module root: /opt/ethinx-sovereign-revenue-loop
REVENUE_LOOP_ROOT = Path(os.environ.get("REVENUE_LOOP_ROOT", "/opt/ethinx-sovereign-revenue-loop"))
ROOT = DEPLOY_ROOT


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def safe_name(value: str) -> str:
    if not value or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in value):
        raise ValueError("Job name must use lowercase letters, digits, and hyphens only.")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def compact(text: str, maximum: int = 4_000) -> str:
    stripped = text.strip()
    return stripped[-maximum:] if len(stripped) > maximum else stripped


class JobTelemetry:
    """Records execution state locally and optionally pings a configured monitor URL."""

    def __init__(self, job_name: str, data_dir: Path):
        self.job_name = safe_name(job_name)
        self.data_dir = data_dir
        self.started_at = utc_now()
        self.started_monotonic = monotonic()
        self.state_path = data_dir / "monitoring" / "jobs" / f"{job_name}.json"

    def push_url(self) -> str:
        env_name = f"ETHINX_PUSH_URL_{self.job_name.upper().replace('-', '_')}"
        return os.environ.get(env_name, "").strip()

    def push_success(self) -> dict[str, Any]:
        url = self.push_url()
        if not url:
            return {"configured": False, "delivered": False, "reason": "not_configured"}
        try:
            request = Request(url, method="GET", headers={"User-Agent": "ethinx-sovereign-monitor/1.0"})
            with urlopen(request, timeout=10) as response:
                status = response.status
                response.read(1024)
            return {"configured": True, "delivered": 200 <= status < 300, "http_status": status}
        except (OSError, URLError, ValueError) as exc:
            return {"configured": True, "delivered": False, "reason": type(exc).__name__}

    def write(self, success: bool, exit_code: int, stdout: str = "", stderr: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        state: dict[str, Any] = {
            "schema_version": 1,
            "job_name": self.job_name,
            "status": "succeeded" if success else "failed",
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "duration_seconds": round(monotonic() - self.started_monotonic, 3),
            "exit_code": exit_code,
            "stdout_tail": compact(stdout),
            "stderr_tail": compact(stderr),
            "metadata": metadata or {},
        }
        # Heartbeats are a secondary monitoring concern: their failure must not make the job fail.
        state["heartbeat"] = self.push_success() if success else {"configured": bool(self.push_url()), "delivered": False, "reason": "job_failed"}
        atomic_json(self.state_path, state)
        return state


def record_ad_hoc_success(job_name: str, data_dir: Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    telemetry = JobTelemetry(job_name, data_dir)
    return telemetry.write(True, 0, metadata=metadata)
