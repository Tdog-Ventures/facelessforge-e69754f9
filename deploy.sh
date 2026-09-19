#!/bin/bash
set -e
apt update && apt install -y redis-server ffmpeg libavcodec-dev python3-venv curl
systemctl enable --now redis-server
redis-cli ping

# Verify frontend build exists before proceeding
[ -f /opt/facelessforge/deploy/frontend/build/index.html ] || { echo "BUILD MISSING - aborting deploy"; exit 1; }

cd /opt/facelessforge/deploy/backend
source venv/bin/activate
pip install -r requirements.txt
systemctl daemon-reload
systemctl enable facelessforge
systemctl restart facelessforge
sleep 4
curl -f http://127.0.0.1:8081/api/health && echo "DEPLOY OK" || journalctl -u facelessforge -n 100 --no-pager
