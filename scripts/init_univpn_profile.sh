#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# UniVPN Profile Initialization Helper
# Safe interactive initialization of UniVPN profile in temporary container.
# ==============================================================================

PROFILE_DIR="${UNIVPN_PROFILE_DIR:-/opt/univpn/profile}"
IMAGE="${UNIVPN_IMAGE:-local/univpn:10781.16.0.0730}"
TIMEOUT_SEC="${INIT_TIMEOUT_SEC:-300}"
CONTAINER_NAME="univpn-init-$(date +%s)-$RANDOM-$$"

usage() {
  cat << EOF
Usage: $0 [OPTIONS]

Safely initializes a persistent UniVPN connection profile on a clean host.
UniVPNCS requires an interactive TTY console to configure SSL VPN settings.

Options:
  -d, --profile-dir <path>   Host directory for profile persistence (default: /opt/univpn/profile)
  -i, --image <docker_image> UniVPN Docker image (default: local/univpn:10781.16.0.0730)
  -t, --timeout <seconds>    Execution timeout in seconds (default: 300)
  -h, --help                 Show this help message

Interactive workflow inside UniVPNCS:
  1: New Connection -> 1: SSL VPN -> 1: Connection Name -> 3: Gateway Address -> 7: Save -> 2: Exit
EOF
  exit 0
}

# Parse command line options
while [[ $# -gt 0 ]]; do
  case "$1" in
    -d|--profile-dir)
      PROFILE_DIR="$2"
      shift 2
      ;;
    -i|--image)
      IMAGE="$2"
      shift 2
      ;;
    -t|--timeout)
      TIMEOUT_SEC="$2"
      shift 2
      ;;
    -g|--gateway|-n|--name|-p|--port)
      echo "WARNING: Blind non-interactive parameter '$1' is disabled to prevent terminal desynchronization and infinite EOF loops in UniVPNCS." >&2
      echo "Interactive setup is required. Please follow the on-screen prompts." >&2
      shift 2
      ;;
    --interactive)
      # Retained for backwards compatibility
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

# Ensure docker is installed
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is not installed or not in PATH." >&2
  exit 1
fi

# Ensure docker image exists
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "ERROR: Docker image '${IMAGE}' not found. Please build it first with ./scripts/build_univpn_image.sh" >&2
  exit 1
fi

# Require interactive TTY terminal (unless explicitly bypassed by test harness)
if [ "${INIT_UNIVPN_FORCE_NONINTERACTIVE:-0}" != "1" ]; then
  if [ ! -t 0 ] || [ ! -t 1 ]; then
    cat << 'EOF' >&2
ERROR: UniVPNCS requires an interactive TTY (terminal).
Automated piping or non-interactive background execution is disabled to prevent
terminal desynchronization, infinite EOF loops, and runaway Docker log generation.

Please run this script directly in an interactive terminal session:
  ./scripts/init_univpn_profile.sh
EOF
    exit 1
  fi
fi

# Trap to guarantee container removal on exit or interruption
cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM HUP
  if command -v docker >/dev/null 2>&1; then
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "${CONTAINER_NAME}"; then
      echo "" >&2
      echo "Cleaning up temporary container ${CONTAINER_NAME}..." >&2
      docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    fi
  fi
  exit "${exit_code}"
}
trap cleanup EXIT INT TERM HUP

# Ensure profile directory exists on host
if [ ! -d "${PROFILE_DIR}" ]; then
  echo "Creating profile directory: ${PROFILE_DIR}"
  mkdir -p "${PROFILE_DIR}" || sudo mkdir -p "${PROFILE_DIR}"
fi

cat << EOF
======================================================================
Интерактивная настройка профиля UniVPN
======================================================================
Временный контейнер : ${CONTAINER_NAME}
Ограничение логов   : 10m (максимум 3 файла ротации)
Таймаут выполнения  : ${TIMEOUT_SEC} сек
Каталог профиля     : ${PROFILE_DIR}

Порядок действий в консоли UniVPNCS:
  1. Выберите '1' (New Connection) -> Нажмите Enter
  2. Выберите '1' (SSL VPN) -> Нажмите Enter
  3. Нажмите '1' (Connection Name) -> Введите имя (напр. CorporateVPN) -> Enter
  4. Нажмите '3' (Gateway Address) -> Введите IP или FQDN шлюза -> Enter
  5. (Опционально) '4' (Port) -> Введите порт (по умолчанию 443) -> Enter
  6. Нажмите '7' (Save) -> Дождитесь подтверждения 'Save successfully!'
  7. Нажмите '2' (Exit) -> Для выхода из меню
======================================================================
Запуск UniVPNCS...
EOF

# Run docker container with strict timeout, log cap, and cleanup
run_with_timeout() {
  local timeout_sec="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --foreground "${timeout_sec}s" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout --foreground "${timeout_sec}s" "$@"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "
import subprocess, sys
try:
    sys.exit(subprocess.run(sys.argv[2:], timeout=int(sys.argv[1])).returncode)
except subprocess.TimeoutExpired:
    sys.stderr.write(f'ERROR: Execution timed out after {sys.argv[1]}s.\n')
    sys.exit(124)
" "${timeout_sec}" "$@"
  else
    "$@"
  fi
}

DOCKER_TTY_FLAG="-t"
if [ -t 0 ]; then
  DOCKER_TTY_FLAG="-it"
fi

set +e
run_with_timeout "${TIMEOUT_SEC}" docker run ${DOCKER_TTY_FLAG} \
  --name "${CONTAINER_NAME}" \
  --rm \
  --stop-timeout 5 \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
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
DOCKER_EXIT_CODE=$?
set -e

if [ "${DOCKER_EXIT_CODE}" -ne 0 ]; then
  echo "" >&2
  if [ "${DOCKER_EXIT_CODE}" -eq 124 ] || [ "${DOCKER_EXIT_CODE}" -eq 137 ] || [ "${DOCKER_EXIT_CODE}" -eq 143 ]; then
    echo "ERROR: Время настройки превысило таймаут (${TIMEOUT_SEC} сек) или процесс был прерван." >&2
  else
    echo "ERROR: Контейнер завершился с кодом ошибки: ${DOCKER_EXIT_CODE}" >&2
  fi
fi

# Validate created profile
if [ -f "${PROFILE_DIR}/sysconfig.ini" ] && [ -s "${PROFILE_DIR}/sysconfig.ini" ]; then
  echo ""
  echo "SUCCESS: Профиль подключения UniVPN успешно сохранён!"
  echo "Файлы конфигурации в ${PROFILE_DIR}:"
  ls -la "${PROFILE_DIR}"
  if [ -d "${PROFILE_DIR}/config" ]; then
    ls -la "${PROFILE_DIR}/config"
  fi
  chmod -R u=rwX,go= "${PROFILE_DIR}" 2>/dev/null || true

  echo ""
  echo "Следующие шаги:"
  echo "1. Создайте файл учётных данных /opt/univpn/secret.env с правами 600:"
  echo "     tee /opt/univpn/secret.env << 'EOF'"
  echo "     VPN_USERNAME=your_username"
  echo "     VPN_PASSWORD=your_password"
  echo "     EOF"
  echo "     chmod 600 /opt/univpn/secret.env"
  echo "2. (Опционально) Поместите SSH-ключ в /opt/univpn/keys/rsa.key (chmod 600)"
  echo "3. Запустите стек VPN Gateway:"
  echo "     docker compose -f docker-compose.vpn.yml up -d"
else
  echo "" >&2
  echo "ERROR: Профиль не был создан: файл '${PROFILE_DIR}/sysconfig.ini' отсутствует или пуст." >&2
  echo "Сессия UniVPNCS была прервана или завершена без сохранения (пункт 7: Save)." >&2
  echo "Пожалуйста, запустите скрипт снова и обязательно выберите пункт 7 (Save) перед выходом." >&2
  exit 1
fi
