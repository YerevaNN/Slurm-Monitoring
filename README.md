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

## Historical installed capacity


The usage API and charts integrate installed GPU/CPU capacity over the requested
time range. White step lines show installed capacity; amber drain and red down
lines are separate diagnostics and never reduce the installed-capacity denominator.
Summary and sidebar percentages use allocated resource-hours divided by installed
resource-hours. Bucket durations are clipped to the selected end and current time,
including a final partial bucket. Tooltips compare bucket-average allocation with
time-weighted capacity in the same bucket.

The recorder saves changes in `capacity_history` (per-node capacities, drain/down
states/reasons, timestamp and source). Identical polls and ordinary idle/mixed
changes do not add rows. Installed capacity changes on node addition/removal or
resource-count changes, not job starts or drain transitions. Failed/partial node
discovery is rejected rather than recorded as zero capacity. This history is not
subject to disk-sample or job retention.

Upgrading seeds history from the last known metadata timestamp only. Capacity
before the first evidenced snapshot remains unknown: the UI shows the gap and
suppresses whole-range percentages rather than applying today's capacity to it.
For a range fully covered by capacity history, percentages include all selected
time, even where allocation history itself may be incomplete.

To import reviewed historical snapshots, first back up the database, then run:

```bash
python scripts/import_capacity_history.py --db data/slurm_history.db \
  --snapshots capacity-snapshots.json
```

Each snapshot supplies `timestamp_ms`, per-node `node_capacities` with integer
`gpus`/`cpus`, optional `node_states`/`node_reasons`, and an evidence `source`.
Import only reviewed evidence for the database's own cluster. Identical imports
are idempotent; conflicts fail atomically. Do not infer historical capacity from
job occupancy. Node registration includes validation partitions; draining does
not remove installed capacity.

Run `node tests/test_usage_capacity.js` for frontend calculations, tooltips and
filtering regression checks.
