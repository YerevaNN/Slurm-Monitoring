# Agent Notes

## SQLite History Database

The app stores runtime history in a local SQLite database at `data/slurm_history.db`, configured by `config.HISTORY_DB_PATH`. The `data/` directory is gitignored, so do not expect the database file to exist in a clean checkout and do not commit generated database files.

`app.start_recorder()` creates the directory, calls `history.init_db()`, then runs `history.migrate_db()` before starting the background recorder. Keep database access centralized in `history.py` where possible; it owns the module-level `_db_lock`, sets `PRAGMA journal_mode=WAL`, and wraps each operation in short-lived SQLite connections.

All timestamps in the database use Unix epoch milliseconds. JSON-shaped values are stored as text and decoded by `history.py` before being returned through Flask APIs.

### Tables

- `jobs`: one row per Slurm job identity, keyed by unique index `(job_id, submit_time_ms)`. Stores user, job name, state, submit/start/end times, requested GPU/CPU/memory, time limit, priority, array task count, `allocations_json`, and `last_seen_ms`. Running jobs usually have `end_time_ms = NULL`; pending jobs should not use Slurm estimated start times as real `start_time_ms`.
- `meta`: singleton row with `id = 1`. Stores node list, node states, node drain reasons, discovered GPUs/CPUs per node, and update timestamp.
- `disk_samples`: one row per recorder poll with `timestamp_ms` and `samples_json`, a JSON list of filesystem usage samples.
- `research_units`: editable grouping records with unique `title` and display `color`.
- `projects`: project records with optional `unit_id` and `sort_order`.
- `project_assignments`: time-bounded user-to-project assignments with `start_time_ms` and optional `end_time_ms`.

### Common Access Patterns

- Recorder writes use `history.upsert_jobs()`, `history.update_meta()`, `history.close_stale_jobs()`, `history.cleanup()`, `history.insert_disk_sample()`, and `history.cleanup_disk_samples()`.
- Timeline reads use `history.get_jobs_in_window(db_path, from_ms, to_ms)`, which returns jobs overlapping `[from_ms, to_ms]` and maps `_ms` column names back to API field names.
- Usage and wait charts use `history.get_usage_buckets()` and `history.get_wait_buckets()`. Both accept `group_by` as `user`, `project`, or `unit`, and use project assignment intervals when grouping beyond users.
- Disk history uses `history.get_disk_samples_in_window()`.
- Research unit, project, and assignment CRUD is exposed through the corresponding `history.create_*`, `history.update_*`, and `history.delete_*` helpers.

### Safety Notes

- Prefer adding schema changes inside `history.init_db()` plus an idempotent migration helper. Existing deployments may already have a populated `data/slurm_history.db`.
- Do not bypass `history.py` for writes unless there is a strong reason; direct writes can skip locking, JSON normalization, and cleanup invariants.
- Preserve the `(job_id, submit_time_ms)` uniqueness rule. Slurm job IDs can reappear after requeue or reuse, so `submit_time_ms` is part of the identity.
- Be careful with `start_time_ms`: for completed/running jobs it means real start time, but Slurm can expose future estimates for pending jobs. The existing migration clears impossible `start_time_ms > end_time_ms` records and future pending estimates.
