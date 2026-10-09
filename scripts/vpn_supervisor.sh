#!/usr/bin/env bash
set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
doctor="$script_dir/vpn_doctor.py"
lock_path="${BO_VPN_SUPERVISOR_LOCK:-/tmp/bo-vpn-supervisor.lock}"
canary_ip="${BO_VPN_DOCTOR_CANARY_IP:-}"

exec 9>"$lock_path"
flock -n 9 || exit 0

if "$doctor" status --quiet; then
  exit 0
fi

if [ -z "$canary_ip" ]; then
  echo "BO_VPN_DOCTOR_CANARY_IP is required for automatic repair" >&2
  exit 2
fi

exec "$doctor" repair --quiet --canary-ip "$canary_ip"
