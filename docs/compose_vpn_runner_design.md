# Compose VPN Runner Design

This document describes the next deployment shape for the confirmed MVP path. It does not add new diagnostic operations.

## Discovery Status

Fact discovery for the live `univpn-service` still must be completed on the stand because this local workspace cannot inspect the remote Docker daemon.

Run on the server:

```bash
docker inspect univpn-service > /tmp/univpn-service.inspect.json
docker inspect univpn-service --format '{{.Config.Image}}'
docker inspect univpn-service --format '{{json .Config.Entrypoint}} {{json .Config.Cmd}}'
docker inspect univpn-service --format '{{json .Mounts}}'
docker inspect univpn-service --format '{{json .Config.Env}}'
docker inspect univpn-service --format '{{json .HostConfig.CapAdd}} {{json .HostConfig.Devices}} {{json .HostConfig.Privileged}}'
docker exec univpn-service sh -lc 'ls -l /run /run/univpn /run/univpn.in /run/secrets 2>/dev/null || true'
```

Items to confirm:

- Current image name and tag.
- Entrypoint and command.
- Mounts and required profile paths.
- Whether `/run/univpn.in` is a regular file, FIFO or another control endpoint.
- How `/run/secrets/univpn.env` is created and mounted.
- Whether `/run/univpn.in` can be replaced by a shared volume path such as `/run/univpn/univpn.in`.
- Whether UniVPN CLI supports a safe disconnect/exit sequence.
- Whether `NET_ADMIN` plus `/dev/net/tun` is enough, or `privileged` is currently required.

Until that discovery is completed, `docker-compose.full.yml` is experimental.

## Current Host-Side Scheme

```text
worker container
  -> host-side runner-daemon
    -> docker inspect
      -> nsenter
        -> univpn-service network namespace
          -> vehicle
```

This is the currently confirmed stand mode. The worker has no Docker socket. The trusted host-side runner has Docker and `nsenter` access.

## Target Compose Scheme

```text
worker container
  -> runner container
    -> shared network namespace with univpn-service
      -> vehicle
```

In this mode:

- Worker still has no Docker socket.
- Runner has no Docker socket and does not call `nsenter`.
- Runner uses `network_mode: "service:univpn-service"`.
- TCP checks and SSH run normally from inside the shared UniVPN network namespace.
- Runner preflight checks `cnem_vnic` and route `172.26.0.0/15` with ordinary `ip` commands.

## Why Host VPN Should Not Be Affected

The compose scheme is designed to avoid host routing changes:

- `univpn-service` must not use `network_mode: host`.
- `NET_ADMIN` applies inside the container network namespace.
- `/dev/net/tun` is passed to the UniVPN container.
- Routes through `cnem_vnic` are created inside the container namespace.
- Host routing table should not change.
- Host services such as Hysteria 2 remain in the host namespace.

After starting full compose, compare host `ip route` and `ip addr` before/after and check the Hysteria service if it exists.

## Risks And Guardrails

| Risk | Status / guardrail |
| --- | --- |
| UniVPN container uses `network_mode: host` | Not allowed for this design. |
| UniVPN modifies host iptables/routes | Not allowed; stop and inspect image/entrypoint. |
| `privileged` is required | Must be justified; prefer `cap_add: [NET_ADMIN]` and `/dev/net/tun`. |
| Control pipe cannot be safely shared | Compose automation remains incomplete until a wrapper is added. |
| Disconnect sequence is unknown | `BO_VPN_STOP_VPN_AFTER_TASK=false` by default; cleanup is documented as not fully implemented. |

## `container_namespace` Runner Mode

`container_namespace` assumes the runner process already lives inside the VPN network namespace.

Behavior:

- No Docker calls.
- No `nsenter`.
- Preflight:
  - `ip -br addr show cnem_vnic`
  - `ip route`
- `vehicle_reachability`:
  - normal Python socket checks for `22/tcp`, `443/tcp`, `80/tcp`.
- `basic_status`:
  - normal `ssh` call from the runner container.

Compatible error codes:

- `vpn_client_error`
- `vehicle_unreachable`
- `ssh_failed`
- `operation_timeout`

## Managed VPN Session

Settings:

```env
BO_VPN_MANAGE_VPN_SESSION=true
BO_VPN_STOP_VPN_AFTER_TASK=false
BO_VPN_UNIVPN_CONTROL_PATH=/run/univpn/univpn.in
BO_VPN_UNIVPN_LOGIN_TIMEOUT_SEC=45
BO_VPN_UNIVPN_CONNECT_POLL_INTERVAL_SEC=2
BO_VPN_UNIVPN_ROUTE_CIDR=172.26.0.0/15
BO_VPN_UNIVPN_INTERFACE=cnem_vnic
BO_VPN_UNIVPN_LOGIN_MODE=container_secret
BO_VPN_UNIVPN_SECRET_PATH=/run/secrets/univpn.env
```

Login sequence for the current stand:

```text
3
1
VPN_USERNAME from secret
VPN_PASSWORD from secret
```

Secrets are read only inside the runner/UniVPN environment. They are not returned by the worker API and are not written to audit logs.

Cleanup status:

- Cleanup hook is implemented.
- Safe disconnect is not fully implemented until the correct UniVPN exit sequence is confirmed.
- If `BO_VPN_STOP_VPN_AFTER_TASK=true` but `BO_VPN_UNIVPN_DISCONNECT_SEQUENCE` is not set, runner adds a warning and leaves the session running.
- If a disconnect sequence is configured, runner writes it to the control path.

## Full Compose File

`docker-compose.full.yml` provides an experimental full stack:

- `univpn-service`
- `bo-vpn-runner`
- `bo-vpn-worker`
- shared `univpn-run` volume for the UniVPN control path
- internal `bo-vpn-internal` network for worker to reach runner via `http://univpn-service:8091`

The real UniVPN image, profile mount and secret mount must be validated against live `docker inspect univpn-service` before using this as production deployment.
