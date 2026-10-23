#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# UniVPN Profile Initialization Helper
# Initializes UniVPN connection profile on a clean host without old profile backup.
# ==============================================================================

PROFILE_NAME="DefaultVPN"
GATEWAY=""
PORT="443"
PROFILE_DIR="${UNIVPN_PROFILE_DIR:-/opt/univpn/profile}"
IMAGE="${UNIVPN_IMAGE:-local/univpn:10781.16.0.0730}"
INTERACTIVE=false

usage() {
  cat << EOF
Usage: $0 [OPTIONS]

Initializes a persistent UniVPN connection profile on a clean host.

Options:
  -g, --gateway <host/ip>   UniVPN gateway address or hostname (required for automated mode)
  -n, --name <name>         Profile name (default: DefaultVPN)
  -p, --port <port>         Gateway port (default: 443)
  -d, --profile-dir <path>  Host directory for profile persistence (default: /opt/univpn/profile)
  -i, --image <docker_image> UniVPN Docker image (default: local/univpn:10781.16.0.0730)
      --interactive         Run interactive UniVPNCS CLI menu directly in container
  -h, --help                Show this help message

Examples:
  # Automated profile creation:
  $0 --gateway vpn.example.com --name CorporateVPN --port 443

  # Interactive configuration:
  $0 --interactive

EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -g|--gateway)
      GATEWAY="$2"
      shift 2
      ;;
    -n|--name)
      PROFILE_NAME="$2"
      shift 2
      ;;
    -p|--port)
      PORT="$2"
      shift 2
      ;;
    -d|--profile-dir)
      PROFILE_DIR="$2"
      shift 2
      ;;
    -i|--image)
      IMAGE="$2"
      shift 2
      ;;
    --interactive)
      INTERACTIVE=true
      shift 1
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed or not in PATH." >&2
  exit 1
fi

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "ERROR: Docker image '${IMAGE}' not found. Please build it first with ./scripts/build_univpn_image.sh" >&2
  exit 1
fi

# Ensure profile directory exists on host
if [ ! -d "${PROFILE_DIR}" ]; then
  echo "Creating profile directory: ${PROFILE_DIR}"
  mkdir -p "${PROFILE_DIR}" || sudo mkdir -p "${PROFILE_DIR}"
fi

if [ "${INTERACTIVE}" = true ]; then
  echo "=== Running UniVPNCS in interactive mode ==="
  echo "To create a connection: 1:New Connection -> 1:SSL VPN -> configure Name & Gateway -> 7:Save -> 2:Exit"
  echo "---------------------------------------------------------------------------------------------------"
  docker run --rm -it \
    --cap-add=NET_ADMIN \
    --device=/dev/net/tun \
    -v "${PROFILE_DIR}:/home/vpn/UniVPN" \
    "${IMAGE}" \
    bash -c '
      set -e
      useradd -m vpn >/dev/null 2>&1 || true
      chown -R vpn:vpn /home/vpn/UniVPN
      /usr/local/UniVPN/promote/UniVPNPromoteService >/dev/null 2>&1 &
      sleep 1
      su - vpn -c /usr/local/UniVPN/serviceclient/UniVPNCS
    '
else
  if [ -z "${GATEWAY}" ]; then
    if [ -t 0 ]; then
      read -rp "Enter UniVPN Gateway Address (IP or hostname): " GATEWAY
      if [ -z "${GATEWAY}" ]; then
        echo "ERROR: Gateway address cannot be empty." >&2
        exit 1
      fi
      read -rp "Enter Profile Name [${PROFILE_NAME}]: " INPUT_NAME
      if [ -n "${INPUT_NAME}" ]; then
        PROFILE_NAME="${INPUT_NAME}"
      fi
      read -rp "Enter Port [${PORT}]: " INPUT_PORT
      if [ -n "${INPUT_PORT}" ]; then
        PORT="${INPUT_PORT}"
      fi
    else
      echo "ERROR: --gateway is required when running non-interactively." >&2
      usage
    fi
  fi

  echo "=== Initializing UniVPN Profile ==="
  echo "Profile Name: ${PROFILE_NAME}"
  echo "Gateway:      ${GATEWAY}"
  echo "Port:         ${PORT}"
  echo "Profile Dir:  ${PROFILE_DIR}"
  echo "Docker Image: ${IMAGE}"

  docker run --rm \
    --cap-add=NET_ADMIN \
    --device=/dev/net/tun \
    -v "${PROFILE_DIR}:/home/vpn/UniVPN" \
    "${IMAGE}" \
    bash -c "
      set -e
      useradd -m vpn >/dev/null 2>&1 || true
      chown -R vpn:vpn /home/vpn/UniVPN
      /usr/local/UniVPN/promote/UniVPNPromoteService >/dev/null 2>&1 &
      sleep 1
      if [ \"${PORT}\" = \"443\" ]; then
        printf \"1\n1\n1\n${PROFILE_NAME}\n3\n${GATEWAY}\n7\n2\n\" | su - vpn -c /usr/local/UniVPN/serviceclient/UniVPNCS >/dev/null
      else
        printf \"1\n1\n1\n${PROFILE_NAME}\n3\n${GATEWAY}\n4\n${PORT}\n7\n2\n\" | su - vpn -c /usr/local/UniVPN/serviceclient/UniVPNCS >/dev/null
      fi
    "
fi

# Validate created files
if [ -f "${PROFILE_DIR}/sysconfig.ini" ]; then
  echo ""
  echo "SUCCESS: UniVPN connection profile initialized successfully!"
  echo "Created files in ${PROFILE_DIR}:"
  ls -la "${PROFILE_DIR}"
  if [ -d "${PROFILE_DIR}/config" ]; then
    ls -la "${PROFILE_DIR}/config"
  fi
  echo ""
  echo "Next steps:"
  echo "1. Create /opt/univpn/secret.env with mode 600:"
  echo "     VPN_USERNAME=your_username"
  echo "     VPN_PASSWORD=your_password"
  echo "2. (Optional) Place SSH private key in /opt/univpn/keys/rsa.key (mode 600)"
  echo "3. Start the VPN stack:"
  echo "     docker compose -f docker-compose.vpn.yml up -d"
else
  echo "ERROR: Profile initialization failed: ${PROFILE_DIR}/sysconfig.ini was not created." >&2
  exit 1
fi
