FROM python:3.11-slim@sha256:d1e9ca7c4e78d1e8ecadb5d44bfc8e956e7a65b659a9950f569f243d72b326d0

# Install ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Copy app (context = deploy/, so backend/ is deploy/backend/)
COPY backend ./backend
# Duplicate for compatibility with parent-context import path deploy.backend.server
COPY backend ./deploy/backend
COPY frontend/build ./frontend/build
# Duplicate for deploy.backend.server import path (/app/deploy/frontend/build)
COPY frontend/build ./deploy/frontend/build
COPY main.py .

# Ensure frontend build exists (both import paths)
RUN mkdir -p /app/frontend/build /app/deploy/frontend/build && test -f /app/frontend/build/index.html || echo '<html><body>FacelessForge</body></html>' > /app/frontend/build/index.html
RUN test -f /app/deploy/frontend/build/index.html || cp /app/frontend/build/index.html /app/deploy/frontend/build/index.html 2>/dev/null || true

# Set PYTHONPATH
ENV PYTHONPATH=/app/backend:/app/deploy/backend:/app

EXPOSE 8080
ENTRYPOINT ["uvicorn","main:app","--host","0.0.0.0","--port","8080","--workers","1"]
