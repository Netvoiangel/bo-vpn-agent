#!/usr/bin/env sh
set -eu

usage() {
  cat <<'USAGE'
Usage:
  scripts/vpn_ssh.sh <vehicle-ip> [command...]

Examples:
  scripts/vpn_ssh.sh 172.26.146.46
  scripts/vpn_ssh.sh 172.26.146.46 'hostname; date; uptime'

Environment:
  BO_VPN_RUNNER_CONTAINER  Docker container that shares UniVPN namespace. Default: bo-vpn-runner
  BO_SSH_USER              SSH user on vehicle. Default: root
  BO_SSH_KEY               SSH key path inside runner container. Default: /run/keys/rsa.key
  BO_SSH_CONNECT_TIMEOUT   SSH connect timeout seconds. Default: 8
  BO_VPN_PREFLIGHT         Run cached read-only VPN preflight. Default: true
USAGE
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

if [ "$#" -lt 1 ]; then
  usage >&2
  exit 64
fi

target_ip="$1"
shift

container="${BO_VPN_RUNNER_CONTAINER:-bo-vpn-runner}"
ssh_user="${BO_SSH_USER:-root}"
ssh_key="${BO_SSH_KEY:-/run/keys/rsa.key}"
connect_timeout="${BO_SSH_CONNECT_TIMEOUT:-8}"

if [ "${BO_VPN_PREFLIGHT:-true}" = "true" ]; then
  script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  python3 "$script_dir/vpn_doctor.py" preflight --quiet || {
    echo "VPN is unavailable; run: $script_dir/vpn_doctor.py repair --canary-ip $target_ip" >&2
    exit 69
  }
fi

if [ "$#" -eq 0 ]; then
  exec docker exec -it "$container" ssh \
    -i "$ssh_key" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=no \
    -o ConnectTimeout="$connect_timeout" \
    "$ssh_user@$target_ip"
fi

exec docker exec "$container" ssh \
  -i "$ssh_key" \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=no \
  -o ConnectTimeout="$connect_timeout" \
  "$ssh_user@$target_ip" \
  "$@"
