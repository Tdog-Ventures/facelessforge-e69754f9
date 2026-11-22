#!/bin/bash
set -euo pipefail
cd /opt/ethinx/facelessforge-ops
exec 9>state/mutation.lock
flock -n 9 || { echo 'Another host mutation is running'; exit 1; }
exec python3 deploy-prepared.py "$@"
