#!/usr/bin/env sh
set -eu

WORKER_URL="${WORKER_URL:-http://127.0.0.1:8000}"
AUTH_TOKEN="${AUTH_TOKEN:-dev-token}"
TASK_ID="${TASK_ID:?set TASK_ID}"
REQUEST_ID="${REQUEST_ID:-get-task-${TASK_ID}}"

curl -sS "${WORKER_URL}/tasks/${TASK_ID}" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "X-Request-Id: ${REQUEST_ID}"
