# bo-vpn-agent

BO/VPN diagnostic worker MVP for remote read-only diagnostics through UniVPN.

The worker is intentionally separated from the Telegram bot. The bot owns UI, vehicle lookup and user interaction; this service owns task lifecycle, service authorization, idempotency, operation registry, audit metadata and the runner boundary.

Current detailed implementation status is tracked in [docs/technical_spec_status.md](docs/technical_spec_status.md).

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
- `container_namespace` - experimental full-compose mode where runner already shares the UniVPN container network namespace and does not use Docker or `nsenter`.
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
BO_VPN_VEHICLE_INVENTORY_PATH=/app/config/vehicles.csv
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
BO_VPN_MANAGE_VPN_SESSION=false
BO_VPN_STOP_VPN_AFTER_TASK=false
BO_VPN_UNIVPN_CONTROL_PATH=/run/univpn/univpn.in
BO_VPN_UNIVPN_LOGIN_TIMEOUT_SEC=45
BO_VPN_UNIVPN_CONNECT_POLL_INTERVAL_SEC=2
BO_VPN_UNIVPN_ROUTE_CIDR=172.26.0.0/15
BO_VPN_UNIVPN_INTERFACE=cnem_vnic
BO_VPN_UNIVPN_LOGIN_MODE=container_secret
BO_VPN_UNIVPN_SECRET_PATH=/run/secrets/univpn.env
```

## Existing Container Preflight

Before any `existing_container` operation, runner-daemon checks the active UniVPN session inside the existing container namespace:

- `docker inspect -f '{{.State.Pid}}' univpn-service`
- `nsenter -t <PID> -n ip addr show cnem_vnic`
- `nsenter -t <PID> -n ip route`

Preflight succeeds only when `cnem_vnic` exists and route `172.26.0.0/15` is present. Missing container, missing interface or missing VPN route returns `vpn_client_error`; after successful preflight, closed `22/443/80` on the vehicle returns `vehicle_unreachable`.

## Container Namespace Runner

`container_namespace` is the experimental compose-oriented runner mode. It assumes runner-daemon is already running inside the same network namespace as `univpn-service`:

```text
worker container -> runner container -> shared UniVPN namespace -> vehicle
```

In this mode runner-daemon:

- does not call Docker;
- does not call `nsenter`;
- checks `cnem_vnic` and route `172.26.0.0/15` with ordinary `ip` commands;
- runs TCP checks and SSH normally from inside the shared namespace;
- can write the UniVPN login sequence to `BO_VPN_UNIVPN_CONTROL_PATH` when `BO_VPN_MANAGE_VPN_SESSION=true`.

`BO_VPN_STOP_VPN_AFTER_TASK=false` is the default because the safe UniVPN disconnect sequence still has to be confirmed on the stand. See [docs/compose_vpn_runner_design.md](docs/compose_vpn_runner_design.md).

## Vehicle Inventory

For the stand MVP, worker can resolve a vehicle IP from a local read-only CSV inventory. This is a simple file-based resolver for about 150 vehicles; later it can be replaced by an external inventory service or bot-side resolver. Access policy for a user and a concrete vehicle is still outside the worker and belongs to the bot or external auth layer.

Set the file path:

```env
BO_VPN_VEHICLE_INVENTORY_PATH=/app/config/vehicles.csv
```

CSV format:

```csv
garage_number,vehicle_id,plate,ip,mac,model,branch,updated_at,comment
1001,81001001,A 001 AA 77,192.0.2.10,00:11:22:33:44:55,Example Bus,Example Branch,2026-06-30T07:00:00+03:00,Synthetic example
```

`ip` is required. At least one identifier must be present: `garage_number`, `vehicle_id` or `plate`. Other fields are optional. Plates are matched exactly after whitespace normalization; fuzzy matching is intentionally not implemented.

Resolve a vehicle without starting diagnostics:

```bash
curl -sS 'http://127.0.0.1:8000/vehicles/resolve?query=6217' \
  -H 'Authorization: Bearer dev-token' \
  -H 'X-Request-Id: resolve-6217-001'
```

Create a task without `vehicle.ip`:

```bash
curl -sS -X POST http://127.0.0.1:8000/tasks \
  -H 'Authorization: Bearer dev-token' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: smoke-inventory-reachability-6217-001' \
  -d '{
    "request_id": "smoke-inventory-reachability-6217-001",
    "telegram_user_id": 123456,
    "user_role": "engineer",
    "vehicle": {"number": "6217"},
    "vpn": {"mode": "inline_once", "username": "smoke-user", "password": "smoke-password"},
    "operation": "vehicle_reachability",
    "params": {},
    "runner_mode": "existing_container",
    "timeout_sec": 60
  }'
```

Do not commit real vehicle inventory files with internal IP addresses. The repository includes only [examples/vehicles.csv](examples/vehicles.csv) with synthetic data; local files under `config/` are ignored except `config/.gitkeep`.

## Verified Failure Scenarios

The current `existing_container` MVP has mock-based test coverage for the main failure paths. Real stand verification is still required for failures that depend on actual vehicle/network state.

| Scenario | Expected error | Coverage |
| --- | --- | --- |
| Vehicle is absent from inventory | `vehicle_ip_not_found` | Mock-based unit test |
| Inventory has multiple matching records | `vehicle_inventory_ambiguous` | Mock-based unit test |
| VPN container is missing/stopped or PID is unavailable | `vpn_client_error` | Mock-based unit test |
| `cnem_vnic` is missing | `vpn_client_error` | Mock-based unit test |
| VPN route `172.26.0.0/15` is missing | `vpn_client_error` | Mock-based unit test |
| `nsenter` lacks permissions | `vpn_client_error` with `nsenter permission denied` | Mock-based unit test |
| TCP ports `22/443/80` are unavailable after successful preflight | `vehicle_unreachable` | Mock-based unit test, real stand recommended |
| SSH fails during `basic_status` | `ssh_failed` | Mock-based unit test, real stand recommended |
| External command exceeds timeout | `operation_timeout` | Mock-based unit test |
| Second task is created while one is active | `worker_busy` | Mock-based unit test, real stand recommended |

Operational notes:

- If a vehicle is not found in inventory, first check that `/app/config/vehicles.csv` is mounted, current and contains the lookup identifier.
- If `vehicle_unreachable` is returned, check VPN preflight, the resolved IP and TCP ports `22/443/80` from inside the UniVPN namespace.
- If `ssh_failed` is returned, check the SSH key, SSH user, port `22` and SSH availability from inside the UniVPN namespace.

Smoke helper scripts are available under `scripts/smoke/`. They take parameters through environment variables and do not contain real secrets:

```bash
WORKER_URL=http://127.0.0.1:8000 AUTH_TOKEN=dev-token VEHICLE_QUERY=81006217 \
  scripts/smoke/resolve_vehicle.sh

WORKER_URL=http://127.0.0.1:8000 AUTH_TOKEN=dev-token VEHICLE_QUERY=81006217 \
  REQUEST_ID=smoke-inventory-reachability-81006217-001 \
  scripts/smoke/smoke_reachability_by_vehicle_id.sh

WORKER_URL=http://127.0.0.1:8000 AUTH_TOKEN=dev-token TASK_ID=<task-id> \
  scripts/smoke/get_task.sh
```

Manual failure smoke-tests should use controlled inputs, for example a missing inventory identifier for `vehicle_ip_not_found` or a known unreachable test IP for `vehicle_unreachable`. Do not use state-changing operations or arbitrary shell commands.

## Run locally

```bash
python -m bo_vpn_agent --host 127.0.0.1 --port 8080
```

Run the runner-daemon on the host:

```bash
bo-vpn-runner-daemon --host 127.0.0.1 --port 8091
```

## Run Worker In Docker

For the current stand, run only the worker in Docker. Keep runner-daemon on the host so it can use `docker inspect`, `nsenter`, the SSH key and the UniVPN container PID without giving Docker socket access to the worker.

```text
bo-vpn-worker container
    |
    | BO_VPN_RUNNER_URL=http://host.docker.internal:8091
    v
bo-vpn-runner-daemon on host
    |
    | docker inspect + nsenter
    v
univpn-service
```

Start the host-side runner-daemon:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .

export BO_VPN_EXISTING_CONTAINER_NAME=univpn-service
export BO_VPN_DOCKER_BIN=/usr/bin/docker
export BO_VPN_NSENTER_BIN=/usr/bin/nsenter
export BO_VPN_SSH_BIN=/usr/bin/ssh
export BO_VPN_SSH_KEY_PATH=/home/timur/univpn/rsa.key
export BO_VPN_DEFAULT_SSH_USER=root
export BO_VPN_NSENTER_TIMEOUT_SEC=8
export BO_VPN_COMMAND_OUTPUT_MAX_BYTES=65536

bo-vpn-runner-daemon --host 127.0.0.1 --port 8091
```

Start the Dockerized worker:

```bash
docker compose -f docker-compose.worker.yml up --build
```

The worker container does not mount `/var/run/docker.sock`.

## Full Docker Compose Deployment

`docker-compose.full.yml` is an experimental full-stack deployment for:

- `univpn-service`;
- `bo-vpn-runner`;
- `bo-vpn-worker`.

It keeps the worker and bot away from Docker socket access. The runner container uses `network_mode: "service:univpn-service"` and calls the new `container_namespace` executor.

Before using it as the main stand flow, run the live discovery commands in [docs/compose_vpn_runner_design.md](docs/compose_vpn_runner_design.md) and align image, entrypoint, mounts, secrets and UniVPN control path with the current `univpn-service`.

Start:

```bash
docker compose -f docker-compose.full.yml up -d --build
docker compose -f docker-compose.full.yml ps
```

Check that host routing did not change:

```bash
ip route
ip addr
```

The compose design intentionally avoids `network_mode: host` for UniVPN so routes through `cnem_vnic` stay inside the container namespace.

## External Telegram Bot Integration

The Telegram bot stays in a separate repository. It should call only the worker HTTP API:

- `GET /health`
- `GET /capabilities`
- `GET /vehicles/resolve?query=...`
- `POST /tasks`
- `GET /tasks/{task_id}`

The bot must not call Docker, `nsenter`, SSH, UniVPN CLI or arbitrary shell commands. In the full-compose flow, it should send `vpn.mode=container_secret`; credentials are mounted only into the runner/UniVPN environment.

Detailed request examples, polling guidance and error mapping are documented in [docs/bot_worker_api.md](docs/bot_worker_api.md).

## Host-side Runner-Daemon As Systemd Service

For the current MVP stand, runner-daemon stays on the host as an infrastructure component. This keeps the worker container simple and avoids mounting Docker socket into it. In `existing_container` mode, runner-daemon must be able to run `docker inspect` and `nsenter` into the `univpn-service` network namespace; on the stand the recommended service user is `root`.

Install the environment file:

```bash
sudo cp deployment/systemd/bo-vpn-runner.env.example /etc/bo-vpn-runner.env
sudo chown root:root /etc/bo-vpn-runner.env
sudo chmod 600 /etc/bo-vpn-runner.env
```

Install and start the unit:

```bash
sudo cp deployment/systemd/bo-vpn-runner.service.example /etc/systemd/system/bo-vpn-runner.service
sudo systemctl daemon-reload
sudo systemctl enable bo-vpn-runner
sudo systemctl start bo-vpn-runner
sudo systemctl status bo-vpn-runner
```

Check health and logs:

```bash
curl -sS http://127.0.0.1:8091/health
journalctl -u bo-vpn-runner -f
```

If the service fails with `status=203/EXEC` and `Permission denied`, check the executable path and SELinux state:

```bash
ls -l /home/timur/bo-vpn-agent/.venv/bin/bo-vpn-runner-daemon
namei -l /home/timur/bo-vpn-agent/.venv/bin/bo-vpn-runner-daemon
getenforce
ls -Z /home/timur/bo-vpn-agent/.venv/bin/bo-vpn-runner-daemon
journalctl -xeu bo-vpn-runner --no-pager | tail -80
```

On AlmaLinux/RHEL with SELinux in `Enforcing` mode, systemd may refuse to execute an entrypoint script directly from `/home/.../.venv/bin`. The example unit runs the daemon through `python -m bo_vpn_agent.runner_daemon_main` to avoid the fragile console-script path. A later production layout should move the runner to `/opt/bo-vpn-agent` with `/etc/bo-vpn-runner.env`.

Confirmed smoke-test:

```text
Date: 2026-06-30
Mode: dockerized worker + host-side runner-daemon + existing_container
Vehicle: 6217 / 172.26.129.119
Operation: vehicle_reachability
Result:
- tcp_22: open
- tcp_443: open
- tcp_80: closed
- duration: 1 sec
```

Healthcheck:

```bash
curl http://127.0.0.1:8000/health
```

Capabilities:

```bash
curl \
  -H 'Authorization: Bearer dev-token' \
  -H 'X-Request-Id: local-check' \
  http://127.0.0.1:8000/capabilities
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
  http://127.0.0.1:8000/tasks
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
  http://127.0.0.1:8000/tasks
```

## Security notes

- VPN credentials are accepted as `vpn.mode=inline_once` for per-task stand checks or `vpn.mode=container_secret` for compose-managed runner secrets.
- VPN credentials are kept in in-memory task state only while the task runs.
- VPN password and full username are not returned by API responses or audit log.
- Request bodies are not written to access logs.
- Audit log records task metadata, operation, result state, error code, duration, runner mode, risk and cleanup flag.

## Test

```bash
python3 -m unittest discover -s tests
```
