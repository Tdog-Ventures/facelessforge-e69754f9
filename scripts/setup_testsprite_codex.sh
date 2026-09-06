#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -z "${TESTSPRITE_API_KEY:-}" ]]; then
  echo "ERROR: TESTSPRITE_API_KEY is not set in this shell."
  echo "Set it securely before running this script; do not commit it to the repository."
  exit 3
fi

if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: Node.js is required. TestSprite requires Node 20.19+, 22.13+, or 24+."
  exit 1
fi

NODE_VERSION="$(node -p 'process.versions.node')"
IFS=. read -r NODE_MAJOR NODE_MINOR NODE_PATCH <<< "$NODE_VERSION"

SUPPORTED=0
if (( NODE_MAJOR == 20 && NODE_MINOR >= 19 )); then SUPPORTED=1; fi
if (( NODE_MAJOR == 22 && NODE_MINOR >= 13 )); then SUPPORTED=1; fi
if (( NODE_MAJOR >= 24 )); then SUPPORTED=1; fi

if (( SUPPORTED != 1 )); then
  echo "ERROR: Node $NODE_VERSION is not in TestSprite's supported range."
  echo "Required: Node 20.19+, 22.13+, or 24+."
  exit 1
fi

echo "Node $NODE_VERSION OK"
echo "Configuring TestSprite for Codex without printing credentials..."

npx --yes @testsprite/testsprite-cli setup --from-env --yes --agent codex
npx --yes @testsprite/testsprite-cli doctor
npx --yes @testsprite/testsprite-cli agent status

echo
echo "TestSprite/Codex setup complete."
echo "Next: give Codex the instructions in TESTSPRITE_FIRST_AUDIT.md"
