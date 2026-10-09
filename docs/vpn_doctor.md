# VPN doctor

`scripts/vpn_doctor.py` checks the Docker services, worker and runner health,
shared network namespaces, `cnem_vnic`, the `172.26.0.0/15` route and host
isolation. Repair mode starts missing services, recreates stale runner/proxy
sidecars and triggers the existing managed UniVPN login through the worker.

The normal SSH helper runs a read-only preflight. It never starts, restarts or
recreates containers and never initiates a VPN login:

```bash
cd /home/timur/bo-vpn-agent
scripts/vpn_ssh.sh 172.27.148.167 'hostname; date -Is; uptime'
```

Manual commands:

```bash
scripts/vpn_doctor.py status
scripts/vpn_doctor.py status --json
scripts/vpn_doctor.py preflight
scripts/vpn_doctor.py repair --canary-ip 172.27.148.167
```

Set `BO_VPN_PREFLIGHT=false` to bypass the preflight for a controlled call.
Preflight results are cached for five seconds. Concurrent fleet workers share a
small cache lock, so one process reads Docker state and the rest reuse its
result. The preflight checks only UniVPN, runner, their namespace, SSH key,
route and host isolation; it does not call the worker API.

Repair is always explicit and uses the separate `/tmp/bo-vpn-repair.lock`.
Only a supervisor or an operator should run it. If a fleet worker sees exit
code 69 from `vpn_ssh.sh`, it should report VPN unavailable and stop; it must
not invoke repair itself. A central supervisor can then run one repair and
resume the batch.

For unattended recovery, `scripts/vpn_supervisor.sh` is the central supervisor.
It takes `/tmp/bo-vpn-supervisor.lock`, runs the read-only status check and calls
repair only when the stack is degraded. Configure a canary through
`BO_VPN_DOCTOR_CANARY_IP`; do not call this script from fleet workers. A quiet
once-per-minute cron entry is suitable for the current host deployment:

```cron
* * * * * BO_VPN_DOCTOR_CANARY_IP=<canary-ip> /home/timur/bo-vpn-agent/scripts/vpn_supervisor.sh >/dev/null 2>&1
```

Doctor state changes and repair actions remain available in the bounded doctor
log, so cron itself does not need a separate growing output file.

The default log is `/home/timur/univpn/logs/vpn-doctor.log`. It is capped at
1 MiB with two backups. Repeated healthy checks do not append log entries;
state changes, repair actions and failures do. On failure, the doctor captures
at most 60 recent lines from runner, worker and TCP proxy. Lines mentioning
credentials, authorization, tokens or secrets are replaced completely. It
never reads Docker logs from `univpn-service` or the discarded UniVPN console
session.

Configuration can be supplied through the environment:

```text
BO_VPN_DOCTOR_CANARY_IP
BO_VPN_DOCTOR_WORKER_URL
BO_VPN_DOCTOR_WORKER_TOKEN
BO_VPN_DOCTOR_LOG
BO_VPN_DOCTOR_LOG_MAX_BYTES
BO_VPN_DOCTOR_LOG_BACKUPS
BO_VPN_REPAIR_LOCK
BO_VPN_PREFLIGHT
BO_VPN_PREFLIGHT_CACHE
BO_VPN_PREFLIGHT_CACHE_TTL_SEC
```

Exit status is `0` when the complete path is healthy, `1` when it remains
degraded and `75` when another repair holds the lock for more than two minutes.
