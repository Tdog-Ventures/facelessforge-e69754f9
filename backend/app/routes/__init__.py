"""FacelessForge routing package.

The canonical public API router lives in :mod:`app.routes.api`.  Keeping it
inside this package avoids a module/package name collision and leaves feature
subrouters (YouTube, long-form, and billing) available under explicit imports.
"""

from .api import router

__all__ = ["router"]
