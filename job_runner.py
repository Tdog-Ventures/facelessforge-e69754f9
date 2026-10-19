#!/usr/bin/env python3
"""Run a named ETHINX job with durable local telemetry and optional monitor heartbeat."""
from __future__ import annotations

import argparse
import importlib
import io
import os
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ops_telemetry import JobTelemetry
from server import ROOT

JOBS = {
    "bank-reconcile": "reconcile_inbox",
    "access-lifecycle": "access_lifecycle_job",
    "content-draft": "content_job",
    "backup": "backup",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=sorted(JOBS), help="Named local job to execute")
    args = parser.parse_args()
    data_dir = Path(os.environ.get("ETHINX_DATA_DIR", ROOT / "data"))
    telemetry = JobTelemetry(args.job, data_dir)
    captured_stdout, captured_stderr = io.StringIO(), io.StringIO()
    exit_code = 1
    try:
        module = importlib.import_module(JOBS[args.job])
        original_argv = sys.argv[:]
        try:
            sys.argv = [str(getattr(module, "__file__", JOBS[args.job]))]
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                return_value = module.main()
        finally:
            sys.argv = original_argv
        exit_code = int(return_value or 0)
    except BaseException:
        traceback.print_exc(file=captured_stderr)
        exit_code = 1
    state = telemetry.write(
        success=exit_code == 0,
        exit_code=exit_code,
        stdout=captured_stdout.getvalue(),
        stderr=captured_stderr.getvalue(),
        metadata={"module": JOBS[args.job]},
    )
    print(captured_stdout.getvalue(), end="")
    if captured_stderr.getvalue():
        print(captured_stderr.getvalue(), file=sys.stderr, end="")
    print(f"monitoring_state={telemetry.state_path} status={state['status']}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
