# Production Readiness For Trusted Local Synode

Synode is intended for a trusted local machine or trusted LAN. It does not
provide app-level authentication or RBAC in this phase, so do not expose the API
or UI directly to the public internet.

## Deployment

Use Docker Compose as the default deployment path:

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/runtime/status
```

The stack runs a one-shot `migrate` service before the API and worker start.
Ollama remains outside Docker and is reached through the host network at
`127.0.0.1:11434` by default.

For trusted LAN access, bind only the ports you intend to share and use a host
firewall to restrict clients. If a reverse proxy is used, terminate TLS there
and restrict access to trusted source networks.

## Worker Runtime

HTTP requests create runs and move them to `queued`. The worker claims queued
runs from Postgres, heartbeats while executing, and records state in the run
row plus `worker_heartbeats`.

Useful checks:

```bash
synode runtime status
synode worker once
synode worker run
```

If a worker crashes, stale `running` runs are requeued after
`SYNODE_WORKER_STALE_AFTER_SECONDS`. Runs in `cancelling` are finalized as
`cancelled` after the same stale window.

## Sandbox

Risky native tools require an explicit sandbox backend even after approval.
The default local backend is `process`; it enforces workspace allowlist, command
timeouts, output truncation, CPU, RAM, and file-size limits. If
`SYNODE_SANDBOX_BACKEND=none`, approved write tools fail closed with a sandbox
error.

Use `SYNODE_SANDBOX_BACKEND=docker` for container isolation of shell and Python
execution. The Docker backend talks to the Docker Engine through a unix socket,
creates one short-lived container per command, bind-mounts the requested
workspace at `/workspace`, disables networking by default, drops Linux
capabilities, sets `no-new-privileges`, uses a read-only container root
filesystem plus tmpfs `/tmp`, applies CPU/RAM/PID/file-size limits, captures
stdout/stderr, and removes the container after completion. It fails closed when
the Docker socket is missing or unreachable.

Relevant settings:

```bash
SYNODE_SANDBOX_BACKEND=process
SYNODE_SANDBOX_CPU_SECONDS=30
SYNODE_SANDBOX_MEMORY_MB=512
SYNODE_SANDBOX_DISK_MB=1024
SYNODE_SANDBOX_OUTPUT_MAX_BYTES=12000
SYNODE_SANDBOX_DOCKER_IMAGE=synode-sandbox:local
SYNODE_SANDBOX_DOCKER_SOCKET=/var/run/docker.sock
SYNODE_SANDBOX_DOCKER_NETWORK=none
SYNODE_SANDBOX_DOCKER_WORKDIR=/workspace
SYNODE_SANDBOX_DOCKER_USER=1000:1000
SYNODE_SANDBOX_DOCKER_CPUS=1
SYNODE_SANDBOX_DOCKER_PIDS_LIMIT=128
SYNODE_SANDBOX_DOCKER_TMPFS_MB=64
```

Build the default sandbox image:

```bash
make docker-sandbox-build
```

To run the Compose stack with the Docker sandbox, use the explicit overlay:

```bash
docker compose -f docker-compose.yaml -f docker-compose.sandbox.yaml up -d --build
```

The overlay mounts `/var/run/docker.sock` and runs API/worker as root so they
can talk to the host Docker daemon. That is an intentional local-operator
tradeoff, not a public SaaS boundary. The overlay maps container workspaces
under `/workspace` to the host path in `SYNODE_SANDBOX_DOCKER_HOST_WORKSPACE`;
set it to an absolute path if your shell does not export `PWD` or if you run
Compose from another directory.

## Data Lifecycle

Retention cleanup is explicit:

```bash
make cleanup
```

The cleanup command prunes old run events, model token deltas, tool audit
records, artifacts, and archived threads. Tune these settings for local disk
capacity:

```bash
SYNODE_RUN_EVENT_RETENTION_DAYS=30
SYNODE_MODEL_DELTA_RETENTION_DAYS=7
SYNODE_TOOL_AUDIT_RETENTION_DAYS=30
SYNODE_ARTIFACT_RETENTION_DAYS=30
SYNODE_ARCHIVED_THREAD_RETENTION_DAYS=90
```

Event, tool-audit, and artifact payloads are truncated before persistence when
they exceed configured byte limits. Truncated records include `_truncated`,
`original_size_bytes`, `max_size_bytes`, and `preview`.

## Backup And Restore

Create a Postgres backup:

```bash
make backup
```

Restore a backup into the Compose Postgres service:

```bash
make restore BACKUP=var/backups/synode-YYYYmmdd-HHMMSS.sql
```

For a restore smoke on disposable data, start a fresh stack, restore the dump,
then verify:

```bash
curl http://127.0.0.1:8787/runtime/status
curl http://127.0.0.1:8787/runs
```
