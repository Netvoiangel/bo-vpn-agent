# bo-vpn-agent

BO/VPN diagnostic worker MVP for remote read-only diagnostics through UniVPN.

The worker is intentionally separated from the Telegram bot. The bot owns UI, vehicle lookup and user interaction; this service owns task lifecycle, service authorization, idempotency, operation registry, audit metadata and the runner boundary.

## MVP API

Implemented endpoints:

- `GET /health` without service auth.
- `GET /capabilities` with service auth.
- `POST /tasks` with service auth.
- `GET /tasks/{task_id}` with service auth.

Protected endpoints require:

```http
Authorization: Bearer <BOT_TO_WORKER_SERVICE_TOKEN>
X-Request-Id: <uuid>
```

Default local token is `dev-token`. Set `BOT_TO_WORKER_SERVICE_TOKEN` in real deployments.

## Operations

MVP capabilities include only read-only operations:

- `vehicle_reachability`
- `basic_status`
- `validators_status`
- `collect_bundle_light`

`ui_screenshot`, `run_command` and `select_route` are intentionally absent from MVP capabilities.

## Runner modes

Set `BO_VPN_RUNNER_MODE`:

- `dry_run` - default, no UniVPN connection, useful for bot integration and API checks.
- `existing_container` - staging/debug mode for execution from an existing VPN network namespace.
- `job_container` - target MVP boundary; currently fails with a normalized `vpn_client_error` until a host runner-daemon is wired in.

The worker does not mount or use the Docker socket directly.

## Run locally

```bash
python -m bo_vpn_agent --host 127.0.0.1 --port 8080
```

Healthcheck:

```bash
curl http://127.0.0.1:8080/health
```

Capabilities:

```bash
curl \
  -H 'Authorization: Bearer dev-token' \
  -H 'X-Request-Id: local-check' \
  http://127.0.0.1:8080/capabilities
```

Create a dry-run task:

```bash
curl -X POST \
  -H 'Authorization: Bearer dev-token' \
  -H 'X-Request-Id: local-task' \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "local-task",
    "telegram_user_id": 123456,
    "user_role": "engineer",
    "vehicle": {"number": "6968", "ip": "172.26.128.11"},
    "vpn": {"mode": "inline_once", "username": "user", "password": "password"},
    "operation": "basic_status",
    "params": {},
    "timeout_sec": 120
  }' \
  http://127.0.0.1:8080/tasks
```

## Security notes

- VPN credentials are accepted only as `vpn.mode=inline_once`.
- VPN credentials are kept in in-memory task state only while the task runs.
- VPN password and full username are not returned by API responses or audit log.
- Request bodies are not written to access logs.
- Audit log records task metadata, operation, result state, error code, duration, runner mode, risk and cleanup flag.

## Test

```bash
python3 -m unittest discover -s tests
```
