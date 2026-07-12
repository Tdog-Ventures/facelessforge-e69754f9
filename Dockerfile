# Stage 1: Build React frontend
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --legacy-peer-deps --quiet && npm install ajv@8 ajv-keywords@5 --legacy-peer-deps --quiet
COPY frontend/ ./
RUN npm run build

# Stage 2: Python backend + built frontend
FROM python:3.11-slim

# System deps: ffmpeg for video assembly
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (excluding emergentintegrations — not needed)
COPY backend/requirements.txt ./requirements.txt
RUN grep -v "emergentintegrations" requirements.txt > requirements_clean.txt && \
    pip install --no-cache-dir -r requirements_clean.txt

# Copy backend source
COPY backend/ ./backend/

# Patch tts.py and generation.py with Hetzner-compatible versions
COPY patches/tts.py ./backend/app/tts.py
COPY patches/generation.py ./backend/app/generation.py

# Copy built frontend into backend static dir
RUN mkdir -p ./backend/static/frontend
COPY --from=frontend-builder /app/frontend/build/ ./backend/static/frontend/

# Create required dirs
RUN mkdir -p ./backend/static/renders ./backend/static/audio ./backend/static/thumbnails

WORKDIR /app/backend

EXPOSE 7001

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:7001/api/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7001", "--workers", "2"]
