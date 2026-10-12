#!/bin/bash
# ================================================== WRAPPER_INIT
LOGDIR="$HOME/winegard/minicom_logs/"
mkdir -p "$LOGDIR"
TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
PORT_TAG=""
for arg in "$@"; do
    case "$arg" in
        /dev/*) PORT_TAG=$(basename "$arg") ;;
    esac
done
if [ -n "$PORT_TAG" ]; then
    LOGFILE="$LOGDIR/${PORT_TAG}_${TIMESTAMP}.log"
else
    LOGFILE="$LOGDIR/minicom_${TIMESTAMP}.log"
fi
exec minicom "$@" -C "$LOGFILE"
