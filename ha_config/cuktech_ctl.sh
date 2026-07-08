#!/bin/bash
set -euo pipefail
ACTION="${1:-status}"

case "$ACTION" in
    start)
        /config/cuktech_api.sh start
        ;;
    stop)
        /config/cuktech_api.sh stop
        ;;
    restart)
        /config/cuktech_api.sh restart
        ;;
    status)
        /config/cuktech_api.sh status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
