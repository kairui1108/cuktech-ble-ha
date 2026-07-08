#!/bin/bash
set -euo pipefail
ACTION="${1:-status}"
API="http://localhost:8199/api"
TIMEOUT=5

case "$ACTION" in
    start)
        curl -s --max-time $TIMEOUT -X POST "$API/enable" -H "Content-Type: application/json" -d '{"enabled": true}' > /dev/null 2>&1 &
        echo "started"
        ;;
    stop)
        curl -s --max-time $TIMEOUT -X POST "$API/enable" -H "Content-Type: application/json" -d '{"enabled": false}' > /dev/null 2>&1 &
        echo "stopped"
        ;;
    toggle)
        STATUS=$(curl -s --max-time $TIMEOUT "$API/status" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('connected',False))" 2>/dev/null || echo "False")
        if [ "$STATUS" = "True" ]; then
            curl -s --max-time $TIMEOUT -X POST "$API/enable" -H "Content-Type: application/json" -d '{"enabled": false}' > /dev/null 2>&1 &
            echo "stopped"
        else
            curl -s --max-time $TIMEOUT -X POST "$API/enable" -H "Content-Type: application/json" -d '{"enabled": true}' > /dev/null 2>&1 &
            echo "started"
        fi
        ;;
    restart)
        curl -s --max-time $TIMEOUT -X POST "$API/enable" -H "Content-Type: application/json" -d '{"enabled": false}' > /dev/null 2>&1
        sleep 1
        curl -s --max-time $TIMEOUT -X POST "$API/enable" -H "Content-Type: application/json" -d '{"enabled": true}' > /dev/null 2>&1 &
        echo "restarted"
        ;;
    status)
        result=$(curl -s --max-time $TIMEOUT "$API/status" 2>/dev/null)
        if echo "$result" | python3 -c "import sys,json; exit(0 if json.load(sys.stdin).get('connected') else 1)" 2>/dev/null; then
            echo "running"
        else
            echo "stopped"
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|toggle|restart|status}"
        exit 1
        ;;
esac
