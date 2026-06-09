#!/bin/bash
# deploy.sh — pull latest from GitHub and restart dashboard if changed
set -e
cd /home/multi_mind/projects/trading-dashboard

export PATH="$HOME/.local/bin:$PATH"

# Record current HEAD
before=$(git rev-parse HEAD 2>/dev/null)

git pull origin main 2>/dev/null

after=$(git rev-parse HEAD 2>/dev/null)

if [ "$before" != "$after" ]; then
    echo "=== New commits pulled ==="
    git log --oneline "$before..$after"
    echo "=== Restarting dashboard ==="
    fuser -k 8090/tcp 2>/dev/null || true
    sleep 1
    ~/.hermes/hermes-agent/venv/bin/python3 -u app.py &
    echo "=== Dashboard deployed ==="
fi
# Silent exit when no changes — nothing delivered to user
