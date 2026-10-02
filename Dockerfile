FROM python:3.11-slim@sha256:d1e9ca7c4e78d1e8ecadb5d44bfc8e956e7a65b659a9950f569f243d72b326d0

# Install ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY backend ./backend
COPY frontend/build ./frontend/build
COPY main.py .

# Verify frontend build exists before proceeding
RUN test -f /app/frontend/build/index.html || { echo "BUILD MISSING - aborting deploy"; exit 1; }

# Set PYTHONPATH so imports work
ENV PYTHONPATH=/app/backend:$PYTHONPATH

# Expose port
EXPOSE 8080

# Run uvicorn — exec form ENTRYPOINT fixes PID1 SIGTERM (user p12-13 audit)
ENTRYPOINT ["uvicorn","main:app","--host","0.0.0.0","--port","8080","--workers","1"]
