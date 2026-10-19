# FacelessForge root entry - fixes PID1 SIGTERM + durable jobs
# Works both locally (context=deploy) and in container (/app/backend + /app/deploy/backend)
import os
import sys
sys.path.insert(0, "/app/backend")
sys.path.insert(0, "/app/deploy/backend")
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

# Import real app from deploy/backend/server.py (container) or backend/server.py (local)
try:
    from deploy.backend.server import app as real_app
    app = real_app
except Exception:
    try:
        from backend.server import app as real_app
        app = real_app
    except Exception:
        from server import app as real_app  # noqa: E402 (local deploy/ root layout)
        app = real_app

# Durable jobs fix - immediate response, no 120s timeout, no double charge
try:
    from app.api.jobs import router as jobs_router
    app.include_router(jobs_router, prefix="/api", tags=["jobs"])
    print("✅ Jobs router wired from app.api.jobs")
except Exception as e:
    print(f"Jobs router from app.api.jobs failed: {e}")
    try:
        from backend.app.api.jobs import router as jobs_router
        app.include_router(jobs_router, prefix="/api", tags=["jobs"])
        print("✅ Jobs router wired from backend.app.api.jobs")
    except Exception as e2:
        print(f"Jobs router failed: {e2}")

# Health check already in server.py
print("🚀 FacelessForge main.py loaded - durable jobs enabled")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
