# BO/VPN Worker: инструкция по использованию

Документ для оператора стенда и разработчика внешнего Telegram-бота. Worker уже можно использовать как HTTP API для MVP-диагностики через full-compose.

## Где находится worker

В репозитории:

```text
/Users/w6itec6apel/Github/bo-vpn-agent
```

Основные файлы worker-а:

```text
bo_vpn_agent/api.py        HTTP endpoints
bo_vpn_agent/service.py    task lifecycle, auth, inventory lookup
bo_vpn_agent/runner.py     worker -> runner client
bo_vpn_agent/__main__.py   entrypoint python -m bo_vpn_agent
```

На сервере стенда:

```text
/home/timur/bo-vpn-agent
```

В Docker:

```text
bo-vpn-worker   HTTP API, port 8000
bo-vpn-runner   runner-daemon, port 8091 inside compose network
univpn-service  UniVPN CLI/network namespace
```

Основной verified deployment:

```text
docker-compose.full.yml
```

Рабочая схема:

```text
external Telegram bot / curl
  -> http://127.0.0.1:8000
    -> bo-vpn-worker
      -> bo-vpn-runner
        -> univpn-service network namespace
          -> vehicle
```

## Что worker делает

Worker принимает задачи диагностики, хранит task status в памяти, делает inventory lookup при необходимости и передаёт работу runner-у.

Worker сам не получает Docker socket и не знает деталей UniVPN CLI. В full-compose режиме бот передаёт:

```json
{
  "vpn": {
    "mode": "container_secret"
  },
  "runner_mode": "container_namespace"
}
```

Runner сам делает managed UniVPN login, проверку `cnem_vnic`, route `172.26.0.0/15`, TCP/SSH checks и возвращает структурированный результат.

## Базовый URL и авторизация

На сервере:

```text
http://127.0.0.1:8000
```

Protected endpoints требуют:

```http
Authorization: Bearer dev-token
X-Request-Id: <stable-request-id>
```

`X-Request-Id` нужен для трассировки и идемпотентности. Для одной пользовательской команды бот должен генерировать один стабильный `request_id`.

## Проверка, что worker жив

```bash
curl -sS http://127.0.0.1:8000/health
```

Ожидаемо:

```json
{"ok": true, "worker": "bo-vpn-worker", "status": "idle"}
```

Если `status=busy`, worker уже выполняет задачу. В MVP разрешена одна активная задача.

## Capabilities

```bash
curl -sS http://127.0.0.1:8000/capabilities \
  -H 'Authorization: Bearer dev-token' \
  -H 'X-Request-Id: capabilities-check'
```

Для текущего MVP реально подключать к боту:

```text
vehicle_reachability
basic_status
```

Не подключать пока:

```text
validators_status
collect_bundle_light
ui_screenshot
run_command
select_route
```

## Resolve vehicle

Если нужно проверить запись в inventory без запуска диагностики:

```bash
curl -sS 'http://127.0.0.1:8000/vehicles/resolve?query=81006217' \
  -H 'Authorization: Bearer dev-token' \
  -H 'X-Request-Id: resolve-81006217'
```

Inventory на сервере:

```text
/home/timur/bo-vpn-agent/config/vehicles.csv
```

В контейнере:

```text
/app/config/vehicles.csv
```

Важно: в текущей выгрузке `garage_number` похож на индекс строки. Для первого MVP лучше поддерживать прямой IP и lookup по `vehicle_id`.

## Создать задачу `/check` по прямому IP

Это рекомендуемый smoke для бота:

```bash
REQ_ID="check-172-26-129-179-$(date +%s)"

curl -sS -X POST http://127.0.0.1:8000/tasks \
  -H 'Authorization: Bearer dev-token' \
  -H 'Content-Type: application/json' \
  -H "X-Request-Id: $REQ_ID" \
  -d "{
    \"request_id\": \"$REQ_ID\",
    \"telegram_user_id\": 123456,
    \"user_role\": \"engineer\",
    \"vehicle\": {
      \"number\": \"manual-172.26.129.179\",
      \"ip\": \"172.26.129.179\"
    },
    \"vpn\": {
      \"mode\": \"container_secret\"
    },
    \"operation\": \"vehicle_reachability\",
    \"params\": {},
    \"runner_mode\": \"container_namespace\",
    \"timeout_sec\": 120
  }"
```

Verified result on server `192.168.10.50`:

```text
state=finished
duration_sec=20
tcp_22=open
tcp_443=open
tcp_80=closed
```

## Создать задачу `/check` через inventory

```bash
REQ_ID="check-81006217-$(date +%s)"

curl -sS -X POST http://127.0.0.1:8000/tasks \
  -H 'Authorization: Bearer dev-token' \
  -H 'Content-Type: application/json' \
  -H "X-Request-Id: $REQ_ID" \
  -d "{
    \"request_id\": \"$REQ_ID\",
    \"telegram_user_id\": 123456,
    \"user_role\": \"engineer\",
    \"vehicle\": {
      \"number\": \"81006217\"
    },
    \"vpn\": {
      \"mode\": \"container_secret\"
    },
    \"operation\": \"vehicle_reachability\",
    \"params\": {},
    \"runner_mode\": \"container_namespace\",
    \"timeout_sec\": 120
  }"
```

## Создать задачу `/basic`

```bash
REQ_ID="basic-172-26-129-179-$(date +%s)"

curl -sS -X POST http://127.0.0.1:8000/tasks \
  -H 'Authorization: Bearer dev-token' \
  -H 'Content-Type: application/json' \
  -H "X-Request-Id: $REQ_ID" \
  -d "{
    \"request_id\": \"$REQ_ID\",
    \"telegram_user_id\": 123456,
    \"user_role\": \"engineer\",
    \"vehicle\": {
      \"number\": \"manual-172.26.129.179\",
      \"ip\": \"172.26.129.179\"
    },
    \"vpn\": {
      \"mode\": \"container_secret\"
    },
    \"operation\": \"basic_status\",
    \"params\": {},
    \"runner_mode\": \"container_namespace\",
    \"timeout_sec\": 120
  }"
```

`basic_status` возвращает raw-поля: hostname, uptime, system time, disk root, memory. Нормализация в проценты пока не делается.

## Получить статус задачи

```bash
curl -sS http://127.0.0.1:8000/tasks/<task_id> \
  -H 'Authorization: Bearer dev-token' \
  -H 'X-Request-Id: get-task-001'
```

Бот должен polling-ить каждые 2-3 секунды и останавливаться на:

```text
finished
failed
timeout
```

Пример polling:

```bash
TASK_ID=<task_id>

for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  curl -sS "http://127.0.0.1:8000/tasks/${TASK_ID}" \
    -H 'Authorization: Bearer dev-token' \
    -H "X-Request-Id: poll-${i}"
  echo
  sleep 3
done
```

## Smoke scripts

Из корня репозитория на сервере:

```bash
cd /home/timur/bo-vpn-agent
```

Проверка full-compose reachability:

```bash
WORKER_URL=http://127.0.0.1:8000 AUTH_TOKEN=dev-token VEHICLE_IP=172.26.129.179 \
  REQUEST_ID=compose-check-172-26-129-179-$(date +%s) \
  scripts/smoke/smoke_compose_reachability_by_ip.sh
```

Resolve inventory:

```bash
WORKER_URL=http://127.0.0.1:8000 AUTH_TOKEN=dev-token VEHICLE_QUERY=81006217 \
  scripts/smoke/resolve_vehicle.sh
```

Get task:

```bash
WORKER_URL=http://127.0.0.1:8000 AUTH_TOKEN=dev-token TASK_ID=<task_id> \
  scripts/smoke/get_task.sh
```

## Запуск full-compose

```bash
cd /home/timur/bo-vpn-agent
docker compose -f docker-compose.full.yml up -d --build
docker compose -f docker-compose.full.yml ps
```

Проверить worker:

```bash
curl -sS http://127.0.0.1:8000/health
```

Остановить:

```bash
docker compose -f docker-compose.full.yml down
```

## Где смотреть логи

Worker:

```bash
docker logs --tail=100 bo-vpn-worker
```

Runner:

```bash
docker logs --tail=200 bo-vpn-runner
```

Audit worker-а:

```text
/home/timur/bo-vpn-agent/logs/audit.jsonl
```

Не использовать для отладки:

```bash
docker logs univpn-service
```

Причина: старые unsafe контейнеры могли писать UniVPN console transcript с credential input. Текущий compose глушит UniVPN console session в `/dev/null`, но старые логи нужно считать потенциально чувствительными.

## Частые ошибки

`409 worker_busy`

```text
Worker занят другой диагностикой. Повторите запрос позже.
```

`vehicle_ip_not_found`

```text
ТС не найдена в inventory. Проверить vehicles.csv или использовать прямой IP.
```

`vehicle_unreachable`

```text
VPN поднят, но ТС не отвечает по 22/443. Проверить IP и доступность ТС.
```

`vpn_client_error`

```text
Проблема с UniVPN session, cnem_vnic или route 172.26.0.0/15.
```

`ssh_failed`

```text
TCP может быть доступен, но SSH basic_status не смог подключиться. Проверить ключ, пользователя root и порт 22.
```

## Что бот не должен делать

Бот не должен:

```text
вызывать Docker
вызывать nsenter
читать UniVPN secrets
писать в /run/univpn/univpn.in
делать SSH к ТС
вызывать произвольные shell-команды
```

Бот работает только через worker HTTP API.
