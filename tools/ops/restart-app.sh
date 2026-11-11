#!/bin/bash
set -euo pipefail
cd /opt/ethinx/facelessforge-ops
exec 9>state/mutation.lock
flock -n 9 || { echo 'Another host mutation is running'; exit 1; }
python3 guard-idle.py
if [[ "${1:-}" == '--check' ]]; then
  echo 'Idle guard passed; check-only, no restart performed'
  exit 0
fi
[[ $# -eq 0 ]] || { echo 'Unexpected arguments'; exit 2; }
docker restart --time 30 facelessforge-app >/dev/null
for attempt in {1..45}; do
  if curl --fail --silent --max-time 3 http://127.0.0.1:7001/api/health >/dev/null; then
    echo 'FacelessForge restarted; local health HTTP 200'
    exit 0
  fi
  sleep 2
done
echo 'Restart health verification failed; inspect the host'
exit 1
