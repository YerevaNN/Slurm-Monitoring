# Slurm Cluster Dashboard

Flask dashboard with live GPU/CPU allocation timelines, historical allocation and
queue/wait charts, aggregate filesystem samples, and user/project/unit grouping.

## Run

Requires Python 3.10+, Slurm CLI access (`squeue`, `sinfo`, `scontrol`), Flask and
Gunicorn. Install in a persistent host with access to the controller:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/gunicorn --workers 1 --threads 4 --timeout 120 --bind 127.0.0.1:19526 app:app
```

Use one worker: the serving process starts the background recorder, which polls
at 10-second intervals by default. For maintenance imports and tests, set
`SLURM_DASHBOARD_DISABLE_RECORDER=1`. Keep the SQLite database on local persistent
storage at `data/slurm_history.db`; back it up using SQLite's backup API.

Metadata editing requires `SLURM_DASHBOARD_ADMIN_TOKEN`, supplied through a
protected environment file. Read-only monitoring and generated Slurm commands
are available without that token; the app never executes generated job commands.
Deployment, HTTP exposure and reverse-proxy settings are operator-managed.

## Allocation semantics

GPU-hours and CPU-hours measure allocated resources multiplied by elapsed time,
not measured GPU/CPU utilization. Project and unit attribution follows explicit,
time-bounded assignments and optional job-name rules. Historical views use epoch
milliseconds; disk samples are aggregate filesystem readings, not per-user usage.
Per-node capacities are discovered independently, including heterogeneous nodes.

## Tests

```bash
SLURM_DASHBOARD_DISABLE_RECORDER=1 python -m unittest discover -s tests
```

An optional historical Parameter Golf production regression requires an explicit
`SLURM_PARAMETER_GOLF_TEST_DB` fixture path and otherwise skips.
