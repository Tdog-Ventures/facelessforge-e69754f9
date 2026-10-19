#!/usr/bin/env python3
"""Process each locally dropped bank CSV once, archive it, and retain a reconciliation result.
Adelaide migration: deploy root = /opt/facelessforge/deploy, revenue loop at /opt/ethinx-sovereign-revenue-loop.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

# Deploy root: /opt/facelessforge/deploy
DEPLOY_ROOT = Path(os.environ.get("DEPLOY_ROOT", Path(__file__).resolve().parent))
# Revenue loop module root: /opt/ethinx-sovereign-revenue-loop
REVENUE_LOOP_ROOT = Path(os.environ.get("REVENUE_LOOP_ROOT", "/opt/ethinx-sovereign-revenue-loop"))
sys.path.insert(0, str(REVENUE_LOOP_ROOT))

from reconcile_bank_csv import reconcile_file  # noqa: E402
from server import load_config  # noqa: E402
from sovereign_db import DomainError, SovereignStore  # noqa: E402
from ops_telemetry import JobTelemetry  # noqa: E402

# Use DEPLOY_ROOT data dir, not /opt/ethinx-sovereign-revenue-loop
DATA_DIR = Path(os.environ.get("ETHINX_DATA_DIR", DEPLOY_ROOT / "data"))


def main() -> int:
    inbox = Path(os.environ.get("ETHINX_BANK_INBOX", DATA_DIR / "bank-inbox"))
    processed = Path(os.environ.get("ETHINX_BANK_PROCESSED", DATA_DIR / "bank-processed"))
    failed = Path(os.environ.get("ETHINX_BANK_FAILED", DATA_DIR / "bank-failed"))
    archive = DATA_DIR / "bank-imports"
    results_dir = DATA_DIR / "bank-results"
    for directory in (inbox, processed, failed, archive, results_dir):
        directory.mkdir(parents=True, exist_ok=True)
    config = load_config()
    database_path = Path(os.environ.get("ETHINX_DB", DATA_DIR / "ethinx.sqlite3"))
    store = SovereignStore(database_path, config)
    report = {"processed_at": datetime.now(UTC).replace(microsecond=0).isoformat(), "files": []}
    for source in sorted(inbox.glob("*.csv")):
        archive_name = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{source.name}"
        archive_file = archive / archive_name
        try:
            shutil.copy2(source, archive_file)
            outcome = reconcile_file(store, config, archive_file, DATA_DIR / "receipts")
            outcome["source_file"] = str(source)
            outcome["archive_file"] = str(archive_file)
            outcome["status"] = "processed"
            shutil.move(str(source), processed / source.name)
        except (DomainError, OSError, UnicodeError) as exc:
            outcome = {"source_file": str(source), "status": "failed", "reason": str(exc)}
            shutil.move(str(source), failed / source.name)
        report["files"].append(outcome)
    report["file_count"] = len(report["files"])
    report_file = results_dir / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["report_file"] = str(report_file)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
