# GPU telemetry deployment

Install the receiver on the existing dashboard host and one optional sampler per
GPU node. This does not configure Slurm, submit jobs or alter GPU drivers. Keep
one dashboard recorder per database. Without samplers, measurements stay unknown.

## Controller credential and networking

Generate a separate collector token on the controller, without printing it:

```bash
sudo install -d -m 0700 /etc/slurm-gpu-monitor
sudo sh -c 'umask 077; test ! -e /etc/slurm-gpu-monitor/token && python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /etc/slurm-gpu-monitor/token'
```

Retain an existing token on upgrade. Securely provision the same token to each
node at `/etc/slurm-gpu-monitor/token`, root-owned with mode 0600. Use the existing
encrypted administration route; never paste the credential into commands or logs.
For a systemd-managed dashboard, add to its existing service drop-in:

```ini
[Service]
LoadCredential=gpu-token:/etc/slurm-gpu-monitor/token
```

Run `sudo systemctl daemon-reload`, then restart **the dashboard service only**,
using its actual name. Alternatively, a non-systemd launcher can export
`SLURM_GPU_INGEST_TOKEN_FILE`, a private token-file path readable by the dashboard
account. It contains a path, not the secret. This explicit path takes precedence
over systemd credentials. Admin editing uses a separate token.

The controller account needs Slurm visibility and binaries in its service PATH.
The HTTP service must be reachable from nodes: a loopback-only Gunicorn bind
cannot accept direct node connections. Test from each network segment:

```bash
curl --connect-timeout 5 --max-time 8 http://CONTROLLER:19526/api/config
```

Expect JSON refresh settings, not portal fallback HTML. Use HTTPS or a protected
tunnel across untrusted networks. An administrator must manage any firewall rule
for the required node sources, destination and port. HTTP 401 means the endpoint
was reached; connection refusal is a TCP/network issue, not a token failure.

## Collection modes and prerequisites

| Mode | Prerequisites | Sampler option |
|---|---|---|
| Local Slurm lookup | `scontrol show job -d -o` works as the service account | Default |
| Controller-assisted | Token-protected controller allocation API is reachable; no node Slurm client needed | `--controller-allocations` |

Both require Python 3.10+, `nvidia-smi -q -x`, and readable explicit GRES
`File=/dev/nvidia...` mappings. Numeric `NodeName=gpu[01-08]` ranges are supported.
MIG, ambiguous/shared allocations and unsupported/AutoDetect-only mappings are
not guessed. Ask an administrator to inspect the actual GRES/device order and
provide a monitor-only mapping if required; do not alter generated Slurm configs.

Use Slurm's canonical NodeName, which may differ from `hostname -s`. Supply the
full sample URL, including any proxy prefix: `http://CONTROLLER:19526/api/gpu-samples`.
Synchronize clocks. In local mode, the sampler timezone must match the timezone
of Slurm's textual timestamps; assisted mode gets controller-parsed epoch values.

## Generic node installation

From the repository checkout, on each systemd GPU node:

```bash
sudo install -d -m 0755 /opt/slurm-gpu-monitor/scripts
sudo install -m 0644 gpu_metrics.py /opt/slurm-gpu-monitor/gpu_metrics.py
sudo install -m 0644 scripts/gpu_node_sampler.py /opt/slurm-gpu-monitor/scripts/
sudo install -m 0644 deploy/slurm-gpu-monitor.service /etc/systemd/system/slurm-gpu-monitor.service
```

Edit the installed unit before starting it:

* Replace `MONITOR_USER`/`MONITOR_GROUP` with an existing unprivileged account/group
  able to read the GPU counters and GRES file.
* Replace `NODE_NAME` and `CONTROLLER:19526` with the actual node and private route.
* Correct `--gres` if the local path differs.
* Append `--controller-allocations` for assisted mode. For local mode, ensure
  `scontrol` is in the service PATH and Slurm access works as that account.

Systemd must support `LoadCredential`. It exposes the root-only token privately
to the sampler; CPU/memory limits apply to the sampler, not users' jobs.

```bash
sudo systemd-analyze verify /etc/systemd/system/slurm-gpu-monitor.service
sudo systemctl daemon-reload
```

Before enabling, run a read-only probe. Substitute the same account/node/URL/GRES
as the unit, retaining `--controller-allocations` only for assisted mode:

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  -p User=MONITOR_USER -p Group=MONITOR_GROUP \
  -p LoadCredential=gpu-token:/etc/slurm-gpu-monitor/token \
  /usr/bin/python3 /opt/slurm-gpu-monitor/scripts/gpu_node_sampler.py \
  --node NODE_NAME --url http://CONTROLLER:19526/api/gpu-samples \
  --gres /etc/slurm/gres.conf --controller-allocations --dry-run --once
```

This prints identity/measurement JSON without the token and never POSTs samples.
Assisted mode still performs authenticated read requests. An idle node can return
an empty jobs list. Once the probe succeeds:

```bash
sudo systemctl enable --now slurm-gpu-monitor
systemctl is-active slurm-gpu-monitor
sudo journalctl -u slurm-gpu-monitor --since '5 minutes ago' --no-pager
```

Expect running GPU-job measurements after one or two 15-second cycles.

## Troubleshooting and partial-install recovery

| Symptom | Action |
|---|---|
| Connection refused/timeout | Check DNS, route, listener and firewall from the failing node; controller-local curl does not prove remote access |
| 401 | Collector credential rejected; check secure provisioning without displaying values |
| 503 | Controller credential unavailable or allocation lookup failed; inspect dashboard logs |
| Mapping error | Inspect explicit GRES File order and NVIDIA minor numbers; never substitute CUDA-local ordinals |
| Idle node has no samples | Expected: only stable running GPU allocations are exported |
| Gray/stale jobs | Check sampler journal, node identity, clocks, credentials and current allocation; readings expire after 45 seconds |
| Fabric Manager legacy PIDFile warning | This concerns another unit; address the actual sampler preflight error instead of restarting GPU services |

An installer can leave unit and source files in place before its preflight succeeds.
After fixing connectivity or credentials, verify the configured service identity,
node name, GRES mapping and endpoint. Rerun the read-only probe before enabling
the service. Preserve credentials and existing files during recovery.

## Upgrades, retention and stopping

Back up controller source/settings and SQLite with its backup API. Preserve jobs,
capacity and attribution history, credentials, launchers and dirty changes. GPU
tables are additive; never replace live databases with repository fixtures.

For a node upgrade, stop only `slurm-gpu-monitor`, back up its two Python files and
unit, install reviewed replacements, rerun the probe and restart the sampler.
Run `daemon-reload` if its unit changed. Coordinate token rotation across the
controller and all nodes.
No Slurm or NVIDIA-service restart is needed.

```bash
sudo systemctl disable --now slurm-gpu-monitor
```

Stopping leaves running jobs and the dashboard intact. Readings go stale while
history and lifetime summaries remain under [retention rules](../GPU_TELEMETRY.md#storage-and-statistics).
