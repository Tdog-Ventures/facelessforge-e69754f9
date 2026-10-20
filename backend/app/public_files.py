"""Resolve public SPA assets without allowing traversal or symlink escapes."""
from pathlib import Path
from fastapi import HTTPException


def public_file(root: Path, name: str) -> Path | None:
    root = root.resolve()
    path = (root / name).resolve()
    if '\\' in name or not path.is_relative_to(root):
        raise HTTPException(status_code=404, detail='Not found')
    return path if path.is_file() else None
