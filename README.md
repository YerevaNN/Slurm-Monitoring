# Slurm Cluster Dashboard

Real-time and historical monitoring dashboard for Slurm clusters showing active jobs on GPU and CPU timelines.

## Features

- **Dual timeline view**: GPU and CPU resource allocation per node
- **Job details**: Hover for job info (ID, user, task, priority, resources, runtime, wait time)
- **Real-time updates**: Auto-refresh at configurable intervals (5/10/30/60s)
- **Historical view**: View any past time window (e.g. full 24h from 3 days ago)
- **Interactive timeline**: Zoom in/out, view relative time (hours/days from now)
- **Visual highlights**: Pending jobs shown with transparency; unusual states (failed, cancelled) highlighted; down/drained nodes shown with overlays
- **Job editing**: Click pending jobs to adjust priority, memory, and CPU allocation
- **Auto-discovery**: Node list and capacities discovered from Slurm (no hardcoded node names)
- **Job arrays**: Supports Slurm job arrays (pending grouped tasks, running individual tasks)
- **Persistent storage**: SQLite-based history with one row per job (no snapshot redundancy)

## Requirements

- Python 3.10+
- Slurm cluster with `squeue`, `scontrol`, and `sinfo` access

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run the dashboard:
```bash
python app.py
```

The dashboard will:
- Create a `data/` directory and initialize `slurm_history.db`
- Start a background recorder that polls Slurm at the selected refresh interval
- Serve the web UI at `http://localhost:19526`

3. Open browser at `http://localhost:19526` (or configured port)

## Configuration

Edit `config.py` to customize:
- `PORT`: Web server port (default: 19526)
- `GPUS_PER_NODE`, `CPUS_PER_NODE`: Fallback resource counts (auto-discovered from sinfo)
- `REFRESH_INTERVALS`, `DEFAULT_REFRESH`: Available refresh rates
- `HISTORY_DB_PATH`: SQLite database path (default: `data/slurm_history.db`)
- `HISTORY_RETENTION_DAYS`: How long to keep job history (default: 7 days)

Set port via environment:
```bash
export SLURM_DASHBOARD_PORT=8080
python app.py
```

## Using History Mode

- **Live mode** (default): Shows current jobs and auto-refreshes
- **History mode**: Pick a date/time in the datetime picker and click "View" to see jobs from that time window (±12h)
- Click **"Live"** to return to real-time view

## How It Works

- **Background recorder**: Polls Slurm every N seconds (controlled by the refresh dropdown), stores one row per job in SQLite
- **Job completion**: When a job disappears from `squeue`, the recorder calls `scontrol show job` to get the final state (COMPLETED, FAILED, etc.) before storing
- **Time windows**: All views (live and history) query jobs that overlap the time window `[from, to]`
- **No redundancy**: A 24-hour job is stored once; queries return all jobs in any time window
