import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

# Load env from .env.facelessforge if present (local)
try:
    from pathlib import Path
    _env_file = Path(__file__).parent / ".env.facelessforge"
    if _env_file.exists():
        for line in _env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()
except Exception as e:
    print(f"env load failed: {e}")

# Lazy import uvicorn to avoid early import side effects
import uvicorn

# Import FastAPI app from backend.server
from server import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
