#!/usr/bin/env python3
"""Create a consistent local SQLite backup for the ETHINX sovereign revenue loop."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from server import ROOT


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup(database_path: Path, destination: Path, keep_days: int) -> dict[str, str | int]:
    if not database_path.is_file():
        raise FileNotFoundError(f"Database not found: {database_path}")
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw_backup = destination / f"ethinx-{stamp}.sqlite3"
    compressed = destination / f"{raw_backup.name}.gz"
    source = sqlite3.connect(database_path)
    target = sqlite3.connect(raw_backup)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    with raw_backup.open("rb") as source_handle, gzip.open(compressed, "wb", compresslevel=9) as target_handle:
        while chunk := source_handle.read(1_048_576):
            target_handle.write(chunk)
    raw_backup.unlink()
    manifest = destination / f"{compressed.name}.sha256.json"
    details = {
        "database": str(database_path),
        "backup": str(compressed),
        "sha256": checksum(compressed),
        "bytes": compressed.stat().st_size,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    manifest.write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    deleted = 0
    for candidate in destination.glob("ethinx-*.sqlite3.gz*"):
        if candidate.stat().st_mtime < cutoff.timestamp():
            candidate.unlink()
            deleted += 1
    details["expired_files_deleted"] = deleted
    return details


def main() -> int:
    data_dir = Path(os.environ.get("ETHINX_DATA_DIR", ROOT / "data"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(os.environ.get("ETHINX_DB", data_dir / "ethinx.sqlite3")))
    parser.add_argument("--output", type=Path, default=data_dir / "backups")
    parser.add_argument("--keep-days", type=int, default=int(os.environ.get("ETHINX_BACKUP_KEEP_DAYS", "30")))
    args = parser.parse_args()
    if args.keep_days < 1:
        parser.error("--keep-days must be at least 1")
    print(json.dumps(backup(args.database, args.output, args.keep_days), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
