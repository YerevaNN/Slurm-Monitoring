# Per-job GPU measurements

The main dashboard's existing job bars are colored by observed average GPU
activity, with the average also in each label. Hover shows current GPU activity,
VRAM occupancy, and memory read/write activity alongside observed mean/std.
Click a job (or focus it and press Enter/Space) for a detail dialog with per-GPU
statistics, used/total GiB, peak VRAM, sample coverage, and its chart. Escape,
Close, or a backdrop click closes the dialog. Pending-job clicks retain their
priority command controls. There is no separate measurements panel. Gray job
bars indicate missing measurements; the legend explains activity thresholds.
All metric badges and timeline activity colors share one scale:
0–19% red, 20–39% orange, 40–59% yellow, 60–79% yellow-green, 80–100% green.
Chart lines keep distinct colors: sky blue GPU activity, violet VRAM occupancy,
and amber memory activity. All three series use solid lines.
Faint red-to-green horizontal background bands mark the percentage ranges.
Gray badges/bars mean unavailable.
CPU-only jobs use a nearby muted gray-blue (`#566477`) on both timelines,
separate from GPU activity. Pending jobs still use their pending-reason palette.
Automatic detail refresh updates existing text, badges and SVG paths in place;
the dialog, focused controls, scroll containers and existing chart stay mounted.
The background timeline redraw pauses while details are open and catches up
immediately on close. A transient chart fetch error retains the last chart.
Hovering the chart snaps a crosshair/markers to the nearest recorded point and
shows date/time (seconds and browser timezone) and all three percentage values.
Downsampled points are labeled as chart-bin averages, unknown counters stay
unknown, and extended collection gaps do not fabricate a hovered reading.
Pending jobs use a separate cool palette: blue/indigo for ordinary waiting,
violet for dependencies, magenta for impossible dependencies, cyan for QOS, and
slate for holds. Pending-job colors never represent measured GPU activity.
The existing Usage page continues
to report allocation-hours, separately from these measured samples.

## Collection and attribution

`scripts/gpu_node_sampler.py` runs unprivileged on each GPU node every 15 seconds.
It uses `nvidia-smi -q -x` and either local `scontrol show job -d -o` or the
token-protected controller allocation API (`--controller-allocations`). The
controller-assisted mode requires no Slurm client on compute nodes. Slurm GRES IDX is resolved
through that node's explicit `/dev/nvidia...` File order in `gres.conf`, then
matched to the NVIDIA minor number and UUID. CUDA-local ordinals, process names,
and Unix usernames are not attribution evidence. Array tasks use their canonical
`array_job_id_task_id`; submit time is also part of the stored identity.

The allocation is checked both before and after the GPU query. Changed,
ambiguous/shared, missing-device and MIG allocations are skipped. The initial
implementation supports explicit per-node whole-GPU File entries; unsupported
GRES layouts fail visibly in the sampler journal instead of guessing. These are
measurements of the job's exclusively allocated devices, including any activity
outside Slurm on those devices; they are not per-process accounting.

NVIDIA GPU activity and memory activity counters describe activity during the
driver's short sampling window. Polling every 15 seconds estimates activity and
can miss short bursts. Memory activity is time spent reading/writing device
memory, **not percentage of theoretical memory bandwidth**. VRAM occupancy is
used framebuffer memory / total framebuffer memory. Unsupported counters stay
null; collection failures never become zero utilization.

Node snapshots POST to `/api/gpu-samples` using a dedicated collector token in
`X-GPU-Monitor-Token`. The controller validates timestamps, identities, running
state, start time, node GPU counts and counters before writing. Old or duplicate
snapshots do not inflate summaries. The Firebird URL uses its private network;
for deployment across an untrusted network, use HTTPS or a protected tunnel.

## Storage and statistics

* `gpu_samples`: compact numeric 15-second readings per job identity/node/GPU UUID.
  Keep every reading while running, and for 24 hours after a confirmed end.
  Cleanup runs every five minutes in the existing recorder. Samples for suspended
  or still-running jobs are preserved. FINISHED_UNKNOWN with a recorded end also
  expires. Interrupted collection/restarts create gaps, never invented history.
* `gpu_summaries`: permanent Welford count/mean/M2/peak per metric and per GPU,
  first/last observation, latest reading, and sample counts. Job mean/std merges
  these moments across GPUs and time. Standard deviation expresses dispersion
  across observed GPU samples, not a confidence interval or std of job-average
  snapshots. Missing metrics have their own independent observation count.
* Summaries include earlier attempts when the same submitted job is requeued;
  current readings and approximate coverage use the current start time. Coverage
  is a nominal 15 seconds per observed GPU snapshot divided by current-attempt
  allocated GPU runtime, capped at 100%. Collection begins at installation;
  pre-installation job activity cannot be reconstructed.
* `/api/jobs` attaches `gpu_telemetry`. `/api/job/<id>/gpu?submit=<epoch-ms>` returns
  at most approximately 720 chart buckets, averaged on demand from raw samples.
  Display grouping leaves stored 15-second readings unchanged. After expiry the
  chart reports its absence and the summaries remain available.

A SQLite benchmark with the actual schema and 184,320 samples (32 GPUs for 24h)
used about **30.5 MiB**, excluding small summaries and transient WAL space. Raw
space grows with running-job duration, then freed pages are reused; SQLite does
not necessarily shrink the database file on deletion. No periodic full VACUUM is
needed. Permanent summaries are roughly 1–2 KiB per GPU/job, depending on IDs and
available counters. No process lists, command lines or raw NVIDIA XML are saved.

## Service setup

See [the complete setup/recovery guide](deploy/GPU_TELEMETRY_SETUP.md).
`deploy/slurm-gpu-monitor.service` is a generic template. Replace MONITOR_USER,
MONITOR_GROUP, NODE_NAME and the private controller URL for each deployment.
Append `--controller-allocations` if node-side Slurm lookup is unavailable.
Install the sampler and
`gpu_metrics.py` under `/opt/slurm-gpu-monitor`. Run as the existing account with
read-only Slurm/GPU access. This requires no changes to jobs or Slurm daemons.

Generate a separate random collector token, retained root-only at
`/etc/slurm-gpu-monitor/token` on controller and nodes. Do not put its contents in
source, command arguments, URLs or logs. Add to the dashboard's systemd drop-in:

```ini
[Service]
LoadCredential=gpu-token:/etc/slurm-gpu-monitor/token
```

The node unit uses the same LoadCredential entry. Systemd exposes the token to
each service through its private credentials directory. Without that credential,
ingestion returns 503; it never falls back to anonymous writes or the admin token.

Restart only the dashboard and enable `slurm-gpu-monitor` on compute nodes. The
sampler unit limits memory to 128 MiB and CPU to half of one core, allowing the
brief NVIDIA query to finish without affecting the job allocation. Typical CPU
use is much lower. It never launches GPU workloads or changes clock settings.

Checks:

```bash
systemctl status slurm-gpu-monitor
journalctl -u slurm-gpu-monitor --since '5 minutes ago'
```

For a read-only mapping probe, use `--dry-run --once` with the sampler's normal
`--node` and `--url` arguments. It prints only metric/identity data, without a
token, and does not write or send samples. In controller-assisted mode the probe
still needs the private collector credential for authenticated allocation GETs.

Rollback: stop/disable only `slurm-gpu-monitor` on the nodes, restore backed-up
dashboard source/static files, remove its dedicated telemetry drop-in, and
restart the dashboard. The additive GPU tables can remain without affecting
existing job/capacity/disk history. Do not restart Slurm or reset a dirty checkout.
