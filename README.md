# bo-vpn-agent

BO/VPN diagnostic worker MVP for remote read-only diagnostics through UniVPN.

The worker is intentionally separated from the Telegram bot. The bot owns UI, vehicle lookup and user interaction; this service owns task lifecycle, service authorization, idempotency, operation registry, audit metadata and the runner boundary.

## Current implementation status

Implemented:

- Worker API
- In-memory task lifecycle
- Idempotency by `request_id`
- MVP capabilities
- service-token auth
- `dry_run` runner
- `existing_container` runner boundary
- `existing_container` runner-daemon executor for `vehicle_reachability` and `basic_status`
- `job_container` boundary placeholder
- audit redaction tests
- runner-daemon API skeleton

Not implemented yet:

- production-ready host-side runner-daemon deployment and cleanup
- real `job_container` Docker execution
- persistent task storage
- production artifact file storage
- real UniVPN connection lifecycle

Current summary: implemented MVP worker control plane and runner boundary; implemented `existing_container` execution in code; pending real smoke-test on the UniVPN stand and `job_container` execution.

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

Default local token is `dev-token`. Set `BO_VPN_WORKER_AUTH_TOKEN` in real deployments.

## Operations

MVP capabilities include only read-only operations:

- `vehicle_reachability`
- `basic_status`
- `validators_status`
- `collect_bundle_light`

`ui_screenshot`, `run_command` and `select_route` are intentionally absent from MVP capabilities.

## Runner modes

Set `BO_VPN_DEFAULT_RUNNER_MODE`:

- `dry_run` - default, no UniVPN connection, useful for bot integration and API checks.
- `existing_container` - staging/debug mode for execution from an existing VPN network namespace.
- `job_container` - target MVP boundary; currently fails with a normalized `vpn_client_error` until a host runner-daemon is wired in.

The worker does not mount or use the Docker socket directly.

When `BO_VPN_RUNNER_URL` is set, the worker sends jobs to the runner-daemon API instead of executing the local runner placeholder directly.

Runner-daemon MVP API:

- `GET /health`
- `POST /jobs`
- `GET /jobs/{job_id}`
- `POST /jobs/{job_id}/cancel`

Example internal runner request:

```json
{
  "request_id": "uuid",
  "runner_mode": "existing_container",
  "vehicle": {"number": "6968", "ip": "172.26.130.165"},
  "vpn": {"mode": "inline_once", "username": "user", "password": "password"},
  "operation": "vehicle_reachability",
  "params": {},
  "timeout_sec": 120
}
```

Useful env variables:

```env
BO_VPN_WORKER_AUTH_TOKEN=dev-token
BO_VPN_DEFAULT_RUNNER_MODE=dry_run
BO_VPN_RUNNER_URL=http://127.0.0.1:8091
BO_VPN_TASK_TIMEOUT_SEC=120
BO_VPN_ARTIFACT_TTL_HOURS=24
BO_VPN_AUDIT_LOG_PATH=./logs/audit.jsonl
BO_VPN_EXISTING_CONTAINER_NAME=univpn-service
BO_VPN_NSENTER_BIN=/usr/bin/nsenter
BO_VPN_DOCKER_BIN=/usr/bin/docker
BO_VPN_SSH_BIN=/usr/bin/ssh
BO_VPN_SSH_KEY_PATH=/home/timur/univpn/rsa.key
BO_VPN_DEFAULT_SSH_USER=root
BO_VPN_NSENTER_TIMEOUT_SEC=8
BO_VPN_COMMAND_OUTPUT_MAX_BYTES=65536
```

## Existing Container Preflight

Before any `existing_container` operation, runner-daemon checks the active UniVPN session inside the existing container namespace:

- `docker inspect -f '{{.State.Pid}}' univpn-service`
- `nsenter -t <PID> -n ip addr show cnem_vnic`
- `nsenter -t <PID> -n ip route`

Preflight succeeds only when `cnem_vnic` exists and route `172.26.0.0/15` is present. Missing container, missing interface or missing VPN route returns `vpn_client_error`; after successful preflight, closed `22/443/80` on the vehicle returns `vehicle_unreachable`.

## Run locally

```bash
python -m bo_vpn_agent --host 127.0.0.1 --port 8080
```

Run the runner-daemon skeleton:

```bash
bo-vpn-runner-daemon --host 127.0.0.1 --port 8091
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

Create an `existing_container` smoke task for the verified stand vehicle:

```bash
curl -X POST \
  -H 'Authorization: Bearer dev-token' \
  -H 'X-Request-Id: smoke-existing-reachability-001' \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "smoke-existing-reachability-001",
    "telegram_user_id": 123456,
    "user_role": "engineer",
    "vehicle": {"number": "6217", "ip": "172.26.129.119"},
    "vpn": {"mode": "inline_once", "username": "smoke-user", "password": "smoke-password"},
    "operation": "vehicle_reachability",
    "params": {},
    "runner_mode": "existing_container",
    "timeout_sec": 60
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
