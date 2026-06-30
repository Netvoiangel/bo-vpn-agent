# External Telegram Bot -> Worker API

This contract is for the external Telegram bot repository. Do not put bot code, Docker access, `nsenter`, SSH or UniVPN credentials handling into this repository.

Base URL:

```text
http://<worker-host>:8000
```

Protected endpoints require:

```http
Authorization: Bearer <BO_VPN_WORKER_AUTH_TOKEN>
X-Request-Id: <uuid-or-stable-request-id>
```

The bot should use a stable `request_id` per user action. Repeating the same request body with the same `request_id` is idempotent.

Endpoints used by the bot:

- `GET /health`
- `GET /capabilities`
- `GET /vehicles/resolve?query=<vehicle-query>`
- `POST /tasks`
- `GET /tasks/{task_id}`

## Health

```bash
curl -sS http://127.0.0.1:8000/health
```

## Capabilities

```bash
curl -sS http://127.0.0.1:8000/capabilities \
  -H 'Authorization: Bearer dev-token' \
  -H 'X-Request-Id: capabilities-check'
```

## Resolve Vehicle

```bash
curl -sS 'http://127.0.0.1:8000/vehicles/resolve?query=81006217' \
  -H 'Authorization: Bearer dev-token' \
  -H 'X-Request-Id: resolve-81006217'
```

For the current stand export, use `vehicle_id=81006217` for smoke-tests. The current CSV `garage_number` column contains row/index values, not real garage numbers.

## Create Vehicle Reachability By Direct IP

```bash
curl -sS -X POST http://127.0.0.1:8000/tasks \
  -H 'Authorization: Bearer dev-token' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: check-172-26-129-179-001' \
  -d '{
    "request_id": "check-172-26-129-179-001",
    "telegram_user_id": 123456,
    "user_role": "engineer",
    "vehicle": {
      "number": "manual-172.26.129.179",
      "ip": "172.26.129.179"
    },
    "vpn": {
      "mode": "container_secret"
    },
    "operation": "vehicle_reachability",
    "params": {},
    "runner_mode": "container_namespace",
    "timeout_sec": 90
  }'
```

Use `vpn.mode=inline_once` only for the host-side `existing_container` stand flow where credentials are intentionally supplied per task. In the full-compose `container_namespace` flow, use `vpn.mode=container_secret`; credentials stay inside the runner/UniVPN environment.

## Create Vehicle Reachability By Inventory

```bash
curl -sS -X POST http://127.0.0.1:8000/tasks \
  -H 'Authorization: Bearer dev-token' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: check-81006217-001' \
  -d '{
    "request_id": "check-81006217-001",
    "telegram_user_id": 123456,
    "user_role": "engineer",
    "vehicle": {
      "number": "81006217"
    },
    "vpn": {
      "mode": "container_secret"
    },
    "operation": "vehicle_reachability",
    "params": {},
    "runner_mode": "container_namespace",
    "timeout_sec": 90
  }'
```

## Create Basic Status Task

```bash
curl -sS -X POST http://127.0.0.1:8000/tasks \
  -H 'Authorization: Bearer dev-token' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: basic-status-81006217-001' \
  -d '{
    "request_id": "basic-status-81006217-001",
    "telegram_user_id": 123456,
    "user_role": "engineer",
    "vehicle": {
      "number": "81006217"
    },
    "vpn": {
      "mode": "container_secret"
    },
    "operation": "basic_status",
    "params": {},
    "runner_mode": "container_namespace",
    "timeout_sec": 90
  }'
```

## Poll Task

```bash
curl -sS http://127.0.0.1:8000/tasks/<task_id> \
  -H 'Authorization: Bearer dev-token' \
  -H 'X-Request-Id: get-task-001'
```

Recommended bot polling:

- Poll every 2 seconds.
- Stop on `finished`, `failed` or `timeout`.
- Use a total wait limit of 90 seconds for current MVP operations.
- Treat `409 worker_busy` as retry-later.
- Do not show raw tracebacks or internal command output to users.

## Error Mapping

| Worker error | Bot user-facing meaning |
| --- | --- |
| `vehicle_ip_not_found` | ТС не найдена в inventory |
| `vehicle_inventory_ambiguous` | Несколько записей inventory подходят под запрос |
| `vpn_client_error` | Не удалось поднять VPN-сессию |
| `vehicle_unreachable` | ТС недоступна через VPN |
| `ssh_failed` | SSH к ТС недоступен |
| `operation_timeout` | Операция превысила timeout |
| `worker_busy` | Worker занят другой задачей |

## Bot Must Not Do

- Do not call Docker.
- Do not call `nsenter`.
- Do not SSH to vehicles.
- Do not handle UniVPN credentials in the full-compose `container_secret` flow.
- Do not expose arbitrary shell commands to users.
- Do not implement `validators_status`, `collect_bundle_light`, `ui_screenshot`, `run_command` or `select_route` in this increment.
