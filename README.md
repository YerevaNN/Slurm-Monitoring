# Slurm cluster dashboard

Minimal web dashboard for a Slurm cluster (8 nodes gpu01–gpu08, 8 GPUs and 224 CPUs per node). Shows running and pending jobs on GPU (left) and CPU (right) timelines with queue-wait overlay.

## Run

Requires Slurm `squeue` (and optionally `scontrol`) on the same machine. On the login node:

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:19526 (or set `SLURM_DASHBOARD_PORT`).

## Config

- **Port**: default `19526`; override with `SLURM_DASHBOARD_PORT`.
- **Refresh**: 5 / 10 / 30 / 60 s via the dropdown (default 10 s).
- **Time range**: default ±12 h; use − / + to zoom out/in.
