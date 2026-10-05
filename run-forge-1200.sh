#!/bin/bash
set -e
cd /opt/facelessforge/deploy
# load from .env.facelessforge if exists
set -a; [ -f .env.facelessforge ] && . ./.env.facelessforge; set +a
EMAIL=${ADMIN_EMAIL:-${EMAIL:-}}
PASSWORD=${ADMIN_PASSWORD:-${PASSWORD:-}}
# fallback to first user in DB if still empty
if [ -z "$EMAIL" ]; then
  EMAIL=$(grep -m1 "ADMIN_EMAIL" .env.facelessforge 2>/dev/null | cut -d'=' -f2 | tr -d '"')
fi
TOKEN=$(curl -s http://127.0.0.1:8081/api/auth/login -H "Content-Type: application/json" -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token') or d.get('token') or '')")
echo "TOKEN len ${#TOKEN}"
curl -s -X POST http://127.0.0.1:8081/api/forge/run-now -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" | tee /tmp/forge-1200.log
