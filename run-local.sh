#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

ENVIRONMENT=${1:-dev}
LOG_LEVEL=${2:-info}
WRITE_LOGS_INPUT=${3:-}
LOG_DIR_INPUT=${4:-}

LOG_DIR="$LOG_DIR_INPUT"
if [ -z "$LOG_DIR" ]; then
  LOG_DIR="$SCRIPT_DIR/logs"
fi

if [ -n "$WRITE_LOGS_INPUT" ]; then
  WRITE_LOGS="$WRITE_LOGS_INPUT"
else
  WRITE_LOGS="true"
fi

if [ -n "$LOG_DIR_INPUT" ] && [ -z "$WRITE_LOGS_INPUT" ]; then
  WRITE_LOGS="true"
fi

if [ "$WRITE_LOGS" = "true" ]; then
  mkdir -p "$LOG_DIR"
fi

cd "$SCRIPT_DIR"
exec python3 -m daft_monitor \
  --environment "$ENVIRONMENT" \
  --log-level "$LOG_LEVEL" \
  --write-logs "$WRITE_LOGS" \
  --log-dir "$LOG_DIR"
