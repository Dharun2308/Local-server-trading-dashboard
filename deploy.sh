#!/bin/bash
# deploy.sh — pull latest from GitHub and restart the dashboard
set -e

cd /home/multi_mind/projects/trading-dashboard

echo "=== Pulling latest from GitHub ==="
export PATH="$HOME/.local/bin:$PATH"
git pull origin main

echo "=== Restarting dashboard ==="
fuser -k 8090/tcp 2>/dev/null || true
sleep 1
~/.hermes/hermes-agent/venv/bin/python3 -u app.py &

echo "=== Dashboard deployed on https://0.0.0.0:8090 ==="
