# Compose VPN Runner Design

This document describes the next deployment shape for the confirmed MVP path. It does not add new diagnostic operations.

## Discovery Status

Server-side `docker inspect univpn-service` confirmed the current UniVPN container shape:

```text
Name=/univpn-service
Image=local/univpn:10781.16.0.0730
Entrypoint=null
NetworkMode=bridge
Privileged=false
CapAdd=["CAP_NET_ADMIN"]
Devices=[/dev/net/tun:/dev/net/tun:rwm]
```

Real mounts:

```text
/home/timur/univpn/secret.env -> /run/secrets/univpn.env:ro
/home/timur/univpn/logs -> /var/log/univpn:rw
/home/timur/univpn/profile -> /home/vpn/UniVPN:rw
```

Real command creates a FIFO at `/run/univpn.in` and runs `UniVPNCS` through `script`:

```bash
bash -lc '
    set -e
    useradd -m vpn >/dev/null 2>&1 || true
    chown -R vpn:vpn /home/vpn/UniVPN
    rm -f /run/univpn.in
    mkfifo /run/univpn.in
    /usr/local/UniVPN/promote/UniVPNPromoteService >/var/log/univpn/promote.log 2>&1 &
    cd /usr/local/UniVPN/serviceclient
    tail -f /run/univpn.in | script -qfec "su - vpn -c /usr/local/UniVPN/serviceclient/UniVPNCS" <console-transcript>
'
```

`docker-compose.full.yml` intentionally adapts that command without changing the image: the FIFO is moved to `/run/univpn/univpn.in` so runner can write to it through the shared `univpn-run` volume. The compose file does not mount the whole `/run`; only `/run/univpn` is shared.

Still to confirm on the stand:

- Worker can reach runner through `http://univpn-service:8091` while runner uses `network_mode: "service:univpn-service"`.
- UniVPN login through `/run/univpn/univpn.in` succeeds with the file-mounted secret.
- A safe UniVPN disconnect/exit sequence.

Until those smoke-tests pass, `docker-compose.full.yml` remains experimental and must not be treated as production-ready.

## Secret Logging Hardening

UniVPN CLI echoes interactive credential input. For this reason, full-compose intentionally discards the UniVPN console session:

```bash
tail -f /run/univpn/univpn.in | script -qfec "su - vpn -c /usr/local/UniVPN/serviceclient/UniVPNCS" /dev/null >/dev/null 2>&1
```

This keeps the pseudo-terminal behavior required by the CLI, but sends the `script` transcript, stdout and stderr to `/dev/null`.

Security rules:

- Do not write the UniVPN console session to `/var/log/univpn`.
- Do not expose UniVPN console stdout/stderr through `docker logs univpn-service`.
- Keep `/var/log/univpn/promote.log` only for the promote service.
- Do not echo login sequence lines in shell commands.
- Treat logs from old unsafe full-compose containers as potentially containing UniVPN credentials and rotate credentials if they were exposed.

Runner phase messages must stay high-level: login started, waiting for `cnem_vnic`, route found or missing, connected or failed, cleanup started or finished. Runner must not log `VPN_USERNAME` or `VPN_PASSWORD` values.

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
| Compose command differs from inspected command | Intentional only for FIFO path relocation from `/run/univpn.in` to `/run/univpn/univpn.in`; image is not changed. |

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
BO_VPN_UNIVPN_LOGIN_AFTER_PROFILE_DELAY_SEC=2
BO_VPN_UNIVPN_LOGIN_AFTER_CONNECT_DELAY_SEC=4
BO_VPN_UNIVPN_LOGIN_AFTER_USERNAME_DELAY_SEC=2
BO_VPN_UNIVPN_POST_LOGIN_WAIT_SEC=12
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

The managed login writes the sequence step by step because UniVPNCS behaves as an interactive menu:

```text
write 3, wait BO_VPN_UNIVPN_LOGIN_AFTER_PROFILE_DELAY_SEC
write 1, wait BO_VPN_UNIVPN_LOGIN_AFTER_CONNECT_DELAY_SEC
write VPN_USERNAME, wait BO_VPN_UNIVPN_LOGIN_AFTER_USERNAME_DELAY_SEC
write VPN_PASSWORD, wait BO_VPN_UNIVPN_POST_LOGIN_WAIT_SEC
poll cnem_vnic and route
```

Runner debug logs are sanitized. They may show control path existence, secret keys found as booleans, polling attempt number, interface found true/false and route found true/false. They must never include `VPN_USERNAME` or `VPN_PASSWORD` values.

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

The UniVPN image, profile mount and secret mount are aligned to the inspected stand paths, but the full stack still requires a successful smoke-test before promotion from experimental status.
