#!/usr/bin/env bash
# Build + start the full stack, then print a clickable link once it's healthy.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose up --build -d --wait

ui_port="$(docker compose port ui 80 2>/dev/null | cut -d: -f2)"
ui_port="${ui_port:-5173}"

echo ""
echo "Aetheril is up:"
echo "  UI:      http://localhost:${ui_port}"
echo "  Service: http://localhost:8000/docs"
