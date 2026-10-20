"""Resolve public SPA assets without allowing traversal or symlink escapes."""
from pathlib import Path
from fastapi import HTTPException


def public_file(root: Path, name: str) -> Path | None:
    root = root.resolve()
    # Reject NUL and backslashes before resolving: Path.resolve() raises
    # ValueError on an embedded NUL, which would surface as a 500 rather than
    # the intended 404.
    if '\\' in name or '\x00' in name:
        raise HTTPException(status_code=404, detail='Not found')
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise HTTPException(status_code=404, detail='Not found')
    return path if path.is_file() else None
