#!/usr/bin/env sh
set -eu

WORKER_URL="${WORKER_URL:-http://127.0.0.1:8000}"
AUTH_TOKEN="${AUTH_TOKEN:-dev-token}"
VEHICLE_QUERY="${VEHICLE_QUERY:?set VEHICLE_QUERY}"
REQUEST_ID="${REQUEST_ID:-smoke-reachability-${VEHICLE_QUERY}}"
TELEGRAM_USER_ID="${TELEGRAM_USER_ID:-123456}"
USER_ROLE="${USER_ROLE:-engineer}"
VPN_USERNAME="${VPN_USERNAME:-smoke-user}"
VPN_PASSWORD="${VPN_PASSWORD:-smoke-password}"
RUNNER_MODE="${RUNNER_MODE:-existing_container}"
TIMEOUT_SEC="${TIMEOUT_SEC:-60}"

curl -sS -X POST "${WORKER_URL}/tasks" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: ${REQUEST_ID}" \
  -d "{
    \"request_id\": \"${REQUEST_ID}\",
    \"telegram_user_id\": ${TELEGRAM_USER_ID},
    \"user_role\": \"${USER_ROLE}\",
    \"vehicle\": {\"number\": \"${VEHICLE_QUERY}\"},
    \"vpn\": {\"mode\": \"inline_once\", \"username\": \"${VPN_USERNAME}\", \"password\": \"${VPN_PASSWORD}\"},
    \"operation\": \"vehicle_reachability\",
    \"params\": {},
    \"runner_mode\": \"${RUNNER_MODE}\",
    \"timeout_sec\": ${TIMEOUT_SEC}
  }"
