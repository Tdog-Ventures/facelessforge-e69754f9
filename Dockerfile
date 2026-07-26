FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY backend ./backend
COPY main.py .

# Set PYTHONPATH so imports work
ENV PYTHONPATH=/app/backend:$PYTHONPATH

# Expose port
EXPOSE 8080

# Run uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
RUN apt-get update && apt-get install -y ffmpeg && apt-get clean
