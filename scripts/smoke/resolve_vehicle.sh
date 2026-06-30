#!/usr/bin/env sh
set -eu

WORKER_URL="${WORKER_URL:-http://127.0.0.1:8000}"
AUTH_TOKEN="${AUTH_TOKEN:-dev-token}"
VEHICLE_QUERY="${VEHICLE_QUERY:?set VEHICLE_QUERY}"
REQUEST_ID="${REQUEST_ID:-resolve-${VEHICLE_QUERY}}"

curl -sS "${WORKER_URL}/vehicles/resolve?query=${VEHICLE_QUERY}" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "X-Request-Id: ${REQUEST_ID}"
