#!/usr/bin/env sh
set -eu

WORKER_URL="${WORKER_URL:-http://127.0.0.1:8000}"
AUTH_TOKEN="${AUTH_TOKEN:-dev-token}"
VEHICLE_IP="${VEHICLE_IP:?set VEHICLE_IP}"
VEHICLE_NUMBER="${VEHICLE_NUMBER:-manual-${VEHICLE_IP}}"
REQUEST_ID="${REQUEST_ID:-compose-check-${VEHICLE_IP}}"
TELEGRAM_USER_ID="${TELEGRAM_USER_ID:-123456}"
USER_ROLE="${USER_ROLE:-engineer}"
TIMEOUT_SEC="${TIMEOUT_SEC:-120}"

curl -sS -X POST "${WORKER_URL}/tasks" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: ${REQUEST_ID}" \
  -d "{
    \"request_id\": \"${REQUEST_ID}\",
    \"telegram_user_id\": ${TELEGRAM_USER_ID},
    \"user_role\": \"${USER_ROLE}\",
    \"vehicle\": {\"number\": \"${VEHICLE_NUMBER}\", \"ip\": \"${VEHICLE_IP}\"},
    \"vpn\": {\"mode\": \"container_secret\"},
    \"operation\": \"vehicle_reachability\",
    \"params\": {},
    \"runner_mode\": \"container_namespace\",
    \"timeout_sec\": ${TIMEOUT_SEC}
  }"
