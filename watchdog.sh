#!/bin/bash
# Veles watchdog — temporary cron-based measure
# Checks supervisor heartbeat, restarts if stale > 180s

HEARTBEAT_FILE="/opt/veles-data/logs/supervisor.jsonl"
LOG_FILE="/opt/veles-data/logs/watchdog.log"
MAX_STALE=180
SCREEN_NAME="veles"
START_DIR="/opt/veles"

# Get last heartbeat timestamp
LAST_TS=$(grep '"main_loop_heartbeat"' "$HEARTBEAT_FILE" 2>/dev/null | tail -1 | python3 -c "
import sys, json
from datetime import datetime, timezone
try:
    line = sys.stdin.readline().strip()
    if not line:
        print(0)
        sys.exit()
    ts = json.loads(line)['ts']
    dt = datetime.fromisoformat(ts)
    print(int(dt.timestamp()))
except:
    print(0)
")

NOW=$(date +%s)
DIFF=$((NOW - LAST_TS))

if [ "$LAST_TS" -eq 0 ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARN: no heartbeat found in log" >> "$LOG_FILE"
    exit 0
fi

if [ "$DIFF" -gt "$MAX_STALE" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WATCHDOG: heartbeat stale ${DIFF}s (limit ${MAX_STALE}s). Killing and restarting." >> "$LOG_FILE"
    
    # Kill existing screen session
    screen -S "$SCREEN_NAME" -X quit 2>/dev/null
    sleep 2
    
    # Kill any remaining colab_launcher processes
    pkill -f 'python3.*colab_launcher.py' 2>/dev/null
    sleep 1
    
    # Restart in screen
    screen -S "$SCREEN_NAME" -dm bash -c "cd $START_DIR && source venv/bin/activate && set -a && source .env && set +a && python3 colab_launcher.py"
    
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WATCHDOG: restart issued. New screen session started." >> "$LOG_FILE"
fi
