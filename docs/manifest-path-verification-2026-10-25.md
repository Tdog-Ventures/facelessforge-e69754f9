# Manifest path verification — 25 October 2026

PR #21 starting head: ca81319d8f33b9861833cb6ddcb939c849a75a3d.

The existing 24 input-security and manifest checks plus six HTTP-boundary
checks passed: **30 passed, zero failed, zero skipped**. HTTP checks use the
actual FastAPI router and request validation, with authentication and database
storage replaced by isolated fixtures. Absolute, parent-relative, another
project's static path and empty file_path values all return 422 without an
insert. Omitted and null paths still allow metadata-only creation.

A compatibility snapshot with the corrected PR #22 code passed 65 focused
checks in total. This is not a full repository suite or an end-to-end renderer,
provider, deployment or production verification.

The patch continues to provide shared-root containment, not per-project file
ownership or a universal read gate. Stable symlink escapes and directories are
rejected; symlinks resolving inside the root are allowed. Earlier legacy render
reads through _local_path_for_asset remain outside this patch. Investigate old
stored paths before deployment and contain those reads in a separate scoped
change. No production reads, data changes, provider calls or publication ran.

Reproduce the focused path suite in an isolated environment:

```bash
PYTHONPATH=backend PYTHON_DOTENV_DISABLED=1 python -m pytest \
  backend/tests/test_manifest_paths.py \
  backend/tests/test_security_inputs.py \
  backend/tests/test_manifest_http_boundary.py -q
```
