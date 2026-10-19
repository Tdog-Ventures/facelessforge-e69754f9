#!/usr/bin/env python3
"""Evaluate local access expiry and create operator-notification queue records."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from server import ROOT, load_config
from sovereign_db import SovereignStore


def main() -> int:
    config = load_config()
    data_dir = Path(os.environ.get("ETHINX_DATA_DIR", ROOT / "data"))
    database_path = Path(os.environ.get("ETHINX_DB", data_dir / "ethinx.sqlite3"))
    store = SovereignStore(database_path, config)
    result = {
        "processed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "changes": store.evaluate_access(),
        "delivery": "Operator-notification queue only; no email or external message was sent.",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
