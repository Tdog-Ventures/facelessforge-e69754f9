#!/usr/bin/env python3
"""Reconcile a configured bank CSV into ETHINX's local ledger without a payment SaaS.
Adelaide migration: CSV timestamps from Sydney banks are AEDT, convert to Adelaide before storing.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sys
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

# Deploy root: /opt/facelessforge/deploy
DEPLOY_ROOT = Path(os.environ.get("DEPLOY_ROOT", Path(__file__).resolve().parent))
# Revenue loop module root: /opt/ethinx-sovereign-revenue-loop
REVENUE_LOOP_ROOT = Path(os.environ.get("REVENUE_LOOP_ROOT", "/opt/ethinx-sovereign-revenue-loop"))
sys.path.insert(0, str(REVENUE_LOOP_ROOT))

from bank_adapter import adapter_from_config  # noqa: E402
from server import ROOT, load_config  # noqa: E402
from sovereign_db import DomainError, SovereignStore  # noqa: E402
from ops_telemetry import JobTelemetry  # noqa: E402

# Fuzzy matching is handled inside SovereignStore.candidate_applications/import_payment
# using rapidfuzz fuzz.ratio threshold 85 (75-84 REVIEW_REQUIRED, <75 reject).
# See sovereign_db.py for FUZZY MATCH logging.

from datetime import timedelta

def to_adelaide(dt: datetime) -> datetime:
    """Convert AEDT (Sydney) timestamps to Adelaide (ACST, UTC+9:30).
    Assumes dt is timezone-naive and represents AEDT (UTC+11).
    Returns dt adjusted to Adelaide time (UTC+9:30).
    """
    if dt.tzinfo is None:
        # Treat naive datetime as AEDT (Sydney time, UTC+11)
        dt = dt.replace(tzinfo=timezone(timedelta(hours=11)))
    # Convert to Adelaide time (UTC+9:30)
    return dt.astimezone(timezone(timedelta(hours=9, minutes=30)))


def write_receipt(destination: Path, config: dict[str, Any], application: dict[str, Any], bank_date: str) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    receipt_id = f"R-{application['id']}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    receipt = destination / f"{receipt_id}.html"
    business = config["business"]
    amount = application["amount_cents"] / 100
    receipt.write_text(
        f"""<!DOCTYPE html><html lang=\"en\"><meta charset=\"utf-8\"><title>{html.escape(receipt_id)}</title>
<body style=\"font-family:system-ui;max-width:680px;margin:48px auto;line-height:1.5\">
<h1>Payment receipt</h1><p><strong>{html.escape(business['legal_name'])}</strong></p>
<p>Receipt: <strong>{html.escape(receipt_id)}</strong><br>Bank statement date: {html.escape(bank_date)}</p>
<table><tr><td>Customer</td><td>{html.escape(application['full_name'])}</td></tr>
<tr><td>Email</td><td>{html.escape(application['email'])}</td></tr>
<tr><td>Plan</td><td>{html.escape(application['plan_code'])}</td></tr>
<tr><td>Amount received</td><td>{html.escape(business['currency'])} {amount:,.2f}</td></tr>
<tr><td>Payment reference</td><td>{html.escape(application['payment_reference'])}</td></tr>
<tr><td>Paid through</td><td>{html.escape(application['paid_through'] or '')}</td></tr></table>
<p>This receipt records a bank-transfer payment matched by ETHINX's local reconciliation process.</p>
</body></html>""",
        encoding="utf-8",
    )
    return receipt


def reconcile_file(store: SovereignStore, config: dict[str, Any], csv_path: Path, receipt_directory: Path) -> dict[str, Any]:
    adapter = adapter_from_config(config)
    telemetry = JobTelemetry("reconcile-bank-csv", RECEIPT_DIR := DEPLOY_ROOT / "data" / "monitoring")
    results: list[dict[str, Any]] = []
    for row_number, row in adapter.rows(csv_path):
        # Adelaide migration: convert CSV timestamps from AEDT (Sydney) to Adelaide
        if isinstance(row.get("date"), str):
            try:
                parsed = datetime.fromisoformat(row["date"])
                row["date"] = to_adelaide(parsed).strftime("%Y-%m-%dT%H:%M:%S%z")
            except (ValueError, TypeError):
                pass  # leave as-is if unparseable
        result = store.import_payment(row, source_file=str(csv_path), row_number=row_number)
        result["row_number"] = row_number
        if result["outcome"] == "matched":
            receipt = write_receipt(receipt_directory, config, result["application"], row["date"])
            result["receipt_file"] = str(receipt)
        results.append(result)
    review_required = sum(item["outcome"] in {"unmatched", "ambiguous", "underpaid", "overpaid"} for item in results)
    return {
        "file": str(csv_path), "processed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "matched": sum(item["outcome"] == "matched" for item in results),
        "review_required": review_required,
        "duplicate": sum(item["outcome"] == "duplicate" for item in results),
        "ignored": sum(item["outcome"] == "ignored" for item in results),
        "rows": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", type=Path, help="Bank CSV accepted by the configured bank_import adapter")
    parser.add_argument("--commit", action="store_true", help="Required acknowledgement to modify the local database")
    parser.add_argument("--archive-dir", type=Path, default=ROOT / "data" / "bank-imports", help="Immutable import archive")
    args = parser.parse_args()
    if not args.commit:
        print(json.dumps({"status": "blocked", "reason": "Pass --commit to reconcile payment records."}))
        return 2
    if not args.csv_file.is_file():
        print(json.dumps({"status": "blocked", "reason": "CSV file not found."}))
        return 2
    config = load_config()
    database_path = Path(os.environ.get("ETHINX_DB", ROOT / "data" / "ethinx.sqlite3"))
    store = SovereignStore(database_path, config)
    args.archive_dir.mkdir(parents=True, exist_ok=True)
    archived = args.archive_dir / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{args.csv_file.name}"
    shutil.copy2(args.csv_file, archived)
    result = reconcile_file(store, config, archived, ROOT / "data" / "receipts")
    result["archive_file"] = str(archived)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DomainError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        raise SystemExit(2)
