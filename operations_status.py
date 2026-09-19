#!/usr/bin/env python3
"""Read-only operational health summary for the ETHINX sovereign revenue loop."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"


def load_config() -> dict[str, Any]:
    config_path = Path(os.environ.get("ETHINX_CONFIG", DEFAULT_CONFIG))
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


DEFAULT_MAX_AGE_SECONDS = {
    "bank-reconcile": 7_200,
    "access-lifecycle": 7_200,
    "content-draft": 345_600,
    "backup": 93_600,
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def status_rank(value: str) -> int:
    return {"ok": 0, "attention": 1, "unknown": 2, "degraded": 3, "critical": 4}.get(value, 4)


def check_database(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "critical", "reason": "database_missing"}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            open_reviews = connection.execute("SELECT COUNT(*) FROM review_queue WHERE status = 'open'").fetchone()[0]
            pending_notifications = connection.execute("SELECT COUNT(*) FROM operator_notifications WHERE status = 'pending'").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return {"status": "critical", "reason": "database_read_failed", "error_type": type(exc).__name__}
    if integrity != "ok":
        return {"status": "critical", "reason": "sqlite_integrity_check_failed", "integrity": integrity}
    return {
        "status": "ok", "integrity": integrity, "open_review_items": int(open_reviews),
        "pending_operator_notifications": int(pending_notifications),
    }


def check_job(job_name: str, data_dir: Path, max_age_seconds: int, current: datetime) -> dict[str, Any]:
    path = data_dir / "monitoring" / "jobs" / f"{job_name}.json"
    if not path.is_file():
        return {"status": "unknown", "reason": "no_execution_record", "max_age_seconds": max_age_seconds}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        finished = parse_time(str(record["finished_at"]))
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return {"status": "critical", "reason": "invalid_execution_record", "max_age_seconds": max_age_seconds}
    age_seconds = max(0, int((current - finished).total_seconds()))
    result = {
        "status": "ok", "last_status": record.get("status"), "finished_at": record.get("finished_at"),
        "age_seconds": age_seconds, "max_age_seconds": max_age_seconds, "exit_code": record.get("exit_code"),
        "heartbeat": record.get("heartbeat", {}),
    }
    if record.get("status") != "succeeded" or record.get("exit_code") != 0:
        result["status"] = "critical"
        result["reason"] = "last_execution_failed"
    elif age_seconds > max_age_seconds:
        result["status"] = "critical"
        result["reason"] = "execution_stale"
    elif isinstance(record.get("heartbeat"), dict) and record["heartbeat"].get("configured") and not record["heartbeat"].get("delivered"):
        result["status"] = "degraded"
        result["reason"] = "monitor_heartbeat_not_delivered"
    return result


def collect_status(config: dict[str, Any], data_dir: Path, database_path: Path | None = None, current: datetime | None = None) -> dict[str, Any]:
    current = current or now_utc()
    database_path = database_path or Path(os.environ.get("ETHINX_DB", data_dir / "ethinx.sqlite3"))
    monitoring = config.get("monitoring", {})
    configured_max_age = monitoring.get("job_max_age_seconds", {})
    database = check_database(database_path)
    jobs = {
        name: check_job(name, data_dir, int(configured_max_age.get(name, maximum)), current)
        for name, maximum in DEFAULT_MAX_AGE_SECONDS.items()
    }
    job_worst = max([status_rank(item["status"]) for item in jobs.values()], default=0)
    worst = max(status_rank(database["status"]), job_worst)
    if worst >= status_rank("critical"):
        overall = "critical"
    elif worst >= status_rank("degraded"):
        overall = "degraded"
    elif database.get("open_review_items", 0) or database.get("pending_operator_notifications", 0):
        overall = "attention"
    elif job_worst >= status_rank("unknown"):
        overall = "unknown"
    else:
        overall = "ok"
    return {
        "schema_version": 1, "service": "ethinx-sovereign-revenue-loop", "generated_at": current.replace(microsecond=0).isoformat(),
        "overall_status": overall, "database": database, "jobs": jobs,
        "operational_boundary": "Status is read-only. It does not claim that an operator has completed queued external communications or bank actions.",
    }


def main() -> int:
    config = load_config()
    data_dir = Path(os.environ.get("ETHINX_DATA_DIR", ROOT / "data"))
    database_path = Path(os.environ.get("ETHINX_DB", data_dir / "ethinx.sqlite3"))
    print(json.dumps(collect_status(config, data_dir, database_path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
