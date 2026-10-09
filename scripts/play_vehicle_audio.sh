#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 4 ]; then
  echo "Usage: $0 <vehicle-ip> <audio-file> [volume] [salon|street|tangenta]" >&2
  exit 2
fi

vehicle_ip="$1"
audio_file="$2"
volume="${3:-25}"
output="${4:-salon}"
ssh_user="${BO_SSH_USER:-root}"
ssh_key="${BO_SSH_KEY:-/run/keys/rsa.key}"
known_hosts_file="${BO_KNOWN_HOSTS_FILE:-/data/known_hosts}"

[ -r "$audio_file" ] || { echo "Audio file is not readable: $audio_file" >&2; exit 2; }
case "$volume" in ''|*[!0-9]*) echo "Volume must be 1..100" >&2; exit 2;; esac
[ "$volume" -ge 1 ] && [ "$volume" -le 100 ] || { echo "Volume must be 1..100" >&2; exit 2; }
case "$output" in salon|street|tangenta) ;; *) echo "Unknown output: $output" >&2; exit 2;; esac

ssh_options=(
  -F /dev/null -i "$ssh_key" -o BatchMode=yes -o ConnectTimeout=30
  -o ServerAliveInterval=10 -o ServerAliveCountMax=3
  -o KexAlgorithms=diffie-hellman-group14-sha256
  -o HostKeyAlgorithms=ssh-ed25519 -c aes128-ctr -o IPQoS=none
  -o "UserKnownHostsFile=$known_hosts_file" -o StrictHostKeyChecking=accept-new
)
if [ -n "${BO_SSH_PROXY_COMMAND:-}" ]; then
  ssh_options+=(-o "ProxyCommand=${BO_SSH_PROXY_COMMAND}")
fi

remote_host="${ssh_user}@${vehicle_ip}"
remote_file="/tmp/mnt-audio-test-$(date +%s)-$$.media"
remote_pid_file="${remote_file}.pid"
uploaded=0
cleaned=0

cleanup_remote() {
  [ "$uploaded" -eq 1 ] && [ "$cleaned" -eq 0 ] || return 0
  if ssh "${ssh_options[@]}" "$remote_host" bash -s -- "$remote_file" "$remote_pid_file" <<'REMOTE_CLEANUP'
set -u
audio_file="$1"
pid_file="$2"
if [ -r "$pid_file" ]; then
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  case "$pid" in ''|*[!0-9]*) pid='';; esac
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [ "$pgid" = "$pid" ]; then
      case "$args" in *"$audio_file"*) kill -TERM -- -"$pid";; *) exit 1;; esac
      n=0
      while kill -0 "$pid" 2>/dev/null && [ "$n" -lt 30 ]; do sleep 0.1; n=$((n + 1)); done
      kill -0 "$pid" 2>/dev/null && exit 1
    else
      exit 1
    fi
  fi
fi
rm -f -- "$audio_file" "$pid_file"
REMOTE_CLEANUP
  then
    cleaned=1
    echo "Remote playback stopped and temporary files removed"
  else
    echo "Could not stop remote playback cleanly" >&2
    return 1
  fi
}

on_exit() {
  rc=$?
  trap - EXIT HUP INT TERM
  cleanup_remote || { [ "$rc" -ne 0 ] || rc=1; }
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

printf -v volume_factor '%d.%02d' "$((volume / 100))" "$((volume % 100))"
audio_bytes="$(stat -c %s "$audio_file")"
echo "TRANSFER_START bytes=$audio_bytes"
uploaded=1
upload_ok=0
for attempt in 1 2; do
  echo "TRANSFER_ATTEMPT attempt=$attempt"
  ssh "${ssh_options[@]}" "$remote_host" "cat > '$remote_file'" <"$audio_file" &
  transfer_pid=$!
  elapsed=0
  while kill -0 "$transfer_pid" 2>/dev/null; do
    sleep 10
    kill -0 "$transfer_pid" 2>/dev/null || break
    elapsed=$((elapsed + 10))
    if [ $((elapsed % 60)) -eq 0 ]; then
      echo "TRANSFER_WAIT seconds=$elapsed attempt=$attempt"
    fi
  done
  if wait "$transfer_pid"; then
    upload_ok=1
    break
  fi
  echo "TRANSFER_RETRY attempt=$attempt"
  sleep 3
done
if [ "$upload_ok" -ne 1 ]; then
  echo "TRANSFER_FAILED"
  exit 20
fi
echo "TRANSFER_COMPLETE bytes=$audio_bytes"

if ! ssh "${ssh_options[@]}" "$remote_host" bash -s -- \
  "$remote_file" "$remote_pid_file" "$volume_factor" "$output" <<'REMOTE_PLAY'
set -euo pipefail
audio_file="$1"
pid_file="$2"
volume_factor="$3"
output="$4"
cleanup() { rm -f -- "$audio_file" "$pid_file"; }
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
printf '%s\n' "$$" >"$pid_file"
for cmd in ffmpeg ffprobe aplay; do command -v "$cmd" >/dev/null; done
aplay -L 2>/dev/null | grep -Fx "$output" >/dev/null
duration="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$audio_file")"
echo "Playback started: ${duration:-unknown}s, output=$output"
ffmpeg -nostdin -v error -i "$audio_file" -af "volume=$volume_factor" -ac 2 -ar 48000 -f wav - | aplay -q -D "$output"
echo "Playback completed"
cleanup
trap - EXIT HUP INT TERM
systemctl is-active --quiet mnt-sound.service
REMOTE_PLAY
then
  echo "PLAYBACK_FAILED"
  exit 21
fi

cleaned=1
echo "Done"
