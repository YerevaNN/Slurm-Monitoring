"""SQLite-based history storage for Slurm jobs and cluster metadata."""
import colorsys as _colorsys
import json
import sqlite3
import threading
from typing import Any


_db_lock = threading.Lock()


def init_db(path: str):
    """Create database and tables if they don't exist."""
    with _db_lock:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                user TEXT,
                job_name TEXT,
                state TEXT,
                start_time_ms INTEGER,
                end_time_ms INTEGER,
                submit_time_ms INTEGER,
                req_gpus INTEGER,
                req_cpus INTEGER,
                req_cpuonly INTEGER,
                req_mem INTEGER,
                time_limit_ms INTEGER,
                priority INTEGER,
                array_task_count INTEGER,
                allocations_json TEXT,
                pending_reason TEXT,
                partition TEXT,
                last_seen_ms INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_job_submit 
            ON jobs(job_id, submit_time_ms)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_last_seen 
            ON jobs(last_seen_ms)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_submit_end 
            ON jobs(submit_time_ms, end_time_ms)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                nodes_json TEXT,
                node_states_json TEXT,
                node_reasons_json TEXT,
                gpus_per_node INTEGER,
                cpus_per_node INTEGER,
                node_capacities_json TEXT,
                partitions_json TEXT,
                updated_at_ms INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS disk_samples (
                timestamp_ms INTEGER NOT NULL,
                samples_json TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_disk_timestamp ON disk_samples(timestamp_ms)")
        # Migrations for existing databases
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "pending_reason" not in existing:
            conn.execute("ALTER TABLE jobs ADD COLUMN pending_reason TEXT")
        if "req_cpuonly" not in existing:
            conn.execute("ALTER TABLE jobs ADD COLUMN req_cpuonly INTEGER DEFAULT 0")
        if "partition" not in existing:
            conn.execute("ALTER TABLE jobs ADD COLUMN partition TEXT")
        existing_meta = {row[1] for row in conn.execute("PRAGMA table_info(meta)")}
        if existing_meta and "partitions_json" not in existing_meta:
            conn.execute("ALTER TABLE meta ADD COLUMN partitions_json TEXT")
        if existing_meta and "node_capacities_json" not in existing_meta:
            conn.execute("ALTER TABLE meta ADD COLUMN node_capacities_json TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS research_units (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL UNIQUE,
                color TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                unit_id INTEGER REFERENCES research_units(id),
                sort_order INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                user TEXT NOT NULL,
                start_time_ms INTEGER NOT NULL,
                end_time_ms INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_project_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                user TEXT NOT NULL,
                match_type TEXT NOT NULL CHECK (match_type IN ('exact', 'prefix')),
                pattern TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user, match_type, pattern)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_project_rules_user ON job_project_rules(user)")
        conn.commit()
        conn.close()


def upsert_jobs(db_path: str, jobs: list[dict[str, Any]], fetch_ts_ms: int):
    """Upsert jobs: update if (job_id, submit_time_ms) exists, else insert."""
    if not jobs:
        return
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            for job in jobs:
                allocations_json = json.dumps(job.get("allocations", []))
                submit_time_ms = job.get("submit_time")
                job_id = job.get("job_id")
                
                # Check if exists
                cursor = conn.execute(
                    "SELECT id FROM jobs WHERE job_id = ? AND submit_time_ms = ?",
                    (job_id, submit_time_ms)
                )
                row = cursor.fetchone()
                
                if row:
                    # Update
                    conn.execute("""
                        UPDATE jobs SET
                            user = ?, job_name = ?, state = ?,
                            start_time_ms = ?, end_time_ms = ?,
                            req_gpus = ?, req_cpus = ?, req_mem = ?,
                            req_cpuonly = ?,
                            time_limit_ms = ?, priority = ?,
                            array_task_count = ?, allocations_json = ?,
                            pending_reason = ?, partition = ?, last_seen_ms = ?
                        WHERE id = ?
                    """, (
                        job.get("user"), job.get("job_name"), job.get("state"),
                        job.get("start_time"), job.get("end_time"),
                        job.get("req_gpus", 0), job.get("req_cpus", 0), job.get("req_mem", 0),
                        job.get("req_cpuonly", 0),
                        job.get("time_limit_ms"), job.get("priority", 0),
                        job.get("array_task_count", 1), allocations_json,
                        job.get("pending_reason", ""), job.get("partition", ""),
                        fetch_ts_ms, row[0]
                    ))
                else:
                    # Insert
                    conn.execute("""
                        INSERT INTO jobs (
                            job_id, user, job_name, state,
                            start_time_ms, end_time_ms, submit_time_ms,
                            req_gpus, req_cpus, req_mem, req_cpuonly,
                            time_limit_ms, priority, array_task_count,
                            allocations_json, pending_reason, partition, last_seen_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        job_id, job.get("user"), job.get("job_name"), job.get("state"),
                        job.get("start_time"), job.get("end_time"), submit_time_ms,
                        job.get("req_gpus", 0), job.get("req_cpus", 0), job.get("req_mem", 0),
                        job.get("req_cpuonly", 0),
                        job.get("time_limit_ms"), job.get("priority", 0),
                        job.get("array_task_count", 1), allocations_json,
                        job.get("pending_reason", ""), job.get("partition", ""), fetch_ts_ms
                    ))
            conn.commit()
        finally:
            conn.close()


def update_meta(db_path: str, nodes: list[str], node_states: dict[str, str],
                node_reasons: dict[str, str], gpus_per_node: int, cpus_per_node: int, ts_ms: int,
                partitions: dict[str, dict[str, int]] | None = None,
                node_capacities: dict[str, dict[str, int]] | None = None):
    """Update cluster metadata (single row, id=1)."""
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO meta (
                    id, nodes_json, node_states_json, node_reasons_json,
                    gpus_per_node, cpus_per_node, node_capacities_json,
                    partitions_json, updated_at_ms
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                json.dumps(nodes), json.dumps(node_states), json.dumps(node_reasons),
                gpus_per_node, cpus_per_node, json.dumps(node_capacities or {}),
                json.dumps(partitions or {}), ts_ms
            ))
            conn.commit()
        finally:
            conn.close()


def get_latest_meta(db_path: str) -> dict[str, Any]:
    """Read latest meta row; return dict or empty dict if not present."""
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute("""
                SELECT nodes_json, node_states_json, node_reasons_json,
                       gpus_per_node, cpus_per_node, partitions_json,
                       node_capacities_json
                FROM meta WHERE id = 1
            """)
            row = cursor.fetchone()
            if not row:
                return {
                    "nodes": [],
                    "node_states": {},
                    "node_reasons": {},
                    "gpus_per_node": 8,
                    "cpus_per_node": 224,
                    "node_capacities": {},
                    "total_gpus": 0,
                    "total_cpus": 0,
                    "partitions": {}
                }
            nodes = json.loads(row[0]) if row[0] else []
            gpus_per_node = row[3] or 8
            cpus_per_node = row[4] or 224
            node_capacities = json.loads(row[6]) if row[6] else {}
            for node in nodes:
                node_capacities.setdefault(node, {
                    "gpus": gpus_per_node,
                    "cpus": cpus_per_node,
                })
            return {
                "nodes": nodes,
                "node_states": json.loads(row[1]) if row[1] else {},
                "node_reasons": json.loads(row[2]) if row[2] else {},
                "gpus_per_node": gpus_per_node,
                "cpus_per_node": cpus_per_node,
                "node_capacities": node_capacities,
                "total_gpus": sum(int(c.get("gpus", 0)) for c in node_capacities.values()),
                "total_cpus": sum(int(c.get("cpus", 0)) for c in node_capacities.values()),
                "partitions": json.loads(row[5]) if row[5] else {}
            }
        finally:
            conn.close()


def get_jobs_in_window(db_path: str, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
    """Return jobs that overlap the time window [from_ms, to_ms]. Returns only the latest version of each job."""
    with _db_lock:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Get only the latest version of each job (highest submit_time for same job_id)
            cursor = conn.execute("""
                WITH latest_jobs AS (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY submit_time_ms DESC) as rn
                    FROM jobs
                    WHERE submit_time_ms < ?
                      AND (end_time_ms IS NULL OR end_time_ms > ?)
                )
                SELECT * FROM latest_jobs WHERE rn = 1
            """, (to_ms, from_ms))
            rows = cursor.fetchall()
            jobs = []
            for row in rows:
                job = dict(row)
                job["allocations"] = json.loads(job.pop("allocations_json", "[]"))
                job.pop("id", None)
                job.pop("last_seen_ms", None)
                job.pop("rn", None)  # Remove ROW_NUMBER column
                # Rename fields to match frontend expectations (remove _ms suffix)
                job["start_time"] = job.pop("start_time_ms", None)
                job["end_time"] = job.pop("end_time_ms", None)
                job["submit_time"] = job.pop("submit_time_ms", None)
                job["time_limit_ms"] = job.get("time_limit_ms")  # Keep _ms suffix for this one
                jobs.append(job)
            return jobs
        finally:
            conn.close()


def get_job_duration_stats_by_prefix(
    db_path: str, prefix_len: int = 35,
) -> dict[str, dict[str, float | int]]:
    """Completed job runtime stats (hours) grouped by job_name prefix."""
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute(
                """
                SELECT prefix,
                       AVG(dur_h) AS mean_h,
                       SQRT(MAX(0, AVG(dur_h * dur_h) - AVG(dur_h) * AVG(dur_h))) AS std_h,
                       COUNT(*) AS n
                FROM (
                    SELECT substr(job_name, 1, ?) AS prefix,
                           (end_time_ms - start_time_ms) / 3600000.0 AS dur_h
                    FROM jobs
                    WHERE state = 'COMPLETED'
                      AND job_name IS NOT NULL AND job_name != ''
                      AND start_time_ms IS NOT NULL
                      AND end_time_ms IS NOT NULL
                      AND end_time_ms > start_time_ms
                )
                WHERE prefix IS NOT NULL AND prefix != ''
                GROUP BY prefix
                """,
                (prefix_len,),
            )
            stats: dict[str, dict[str, float | int]] = {}
            for row in cursor.fetchall():
                prefix, mean_h, std_h, n = row
                stats[prefix] = {
                    "mean_h": float(mean_h or 0),
                    "std_h": float(std_h or 0),
                    "n": int(n or 0),
                }
            return stats
        finally:
            conn.close()


# Slurm terminal states (job no longer active in the queue)
_SLURM_TERMINAL = frozenset({
    "COMPLETED", "CANCELLED", "FAILED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY",
    "BOOT_FAIL", "DEADLINE", "SPECIAL_EXIT",
})


def close_stale_jobs(db_path: str, stale_threshold_ms: int, get_final_info_fn, now_ms: int):
    """
    Close jobs where last_seen_ms < stale_threshold_ms and state is RUNNING, PENDING, or COMPLETING.

    COMPLETING is included: array lines can disappear from squeue while scontrol still works;
    requeued jobs show PENDING again and must clear end_time.

    get_final_info_fn(job_id) -> (state, end_time_ms, start_time_ms) from scontrol.
    If a newer row with the same job_id exists (newer submit_time), mark REQUEUED.
    """
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute("""
                SELECT id, job_id, last_seen_ms, submit_time_ms FROM jobs
                WHERE last_seen_ms < ? AND state IN ('RUNNING', 'PENDING', 'COMPLETING')
            """, (stale_threshold_ms,))
            stale_rows = cursor.fetchall()

            for row_id, job_id, last_seen_ms, submit_time_ms in stale_rows:
                newer = conn.execute("""
                    SELECT COUNT(*) FROM jobs
                    WHERE job_id = ? AND submit_time_ms > ?
                """, (job_id, submit_time_ms)).fetchone()[0]

                if newer > 0:
                    conn.execute("""
                        UPDATE jobs
                        SET state = 'REQUEUED', end_time_ms = ?, last_seen_ms = ?
                        WHERE id = ?
                    """, (last_seen_ms, now_ms, row_id))
                    continue

                final_state, end_ms, start_ms = get_final_info_fn(job_id)
                if final_state is None:
                    conn.execute("""
                        UPDATE jobs
                        SET state = 'FINISHED_UNKNOWN', end_time_ms = ?,
                            start_time_ms = CASE
                                WHEN start_time_ms IS NOT NULL AND start_time_ms > ? + 60000
                                THEN NULL
                                ELSE start_time_ms
                            END,
                            last_seen_ms = ?
                        WHERE id = ?
                    """, (last_seen_ms, last_seen_ms, now_ms, row_id))
                    continue

                fs = final_state.upper()
                if fs in _SLURM_TERMINAL:
                    fin_end = end_ms if end_ms is not None else last_seen_ms
                    conn.execute("""
                        UPDATE jobs
                        SET state = ?, end_time_ms = ?,
                            start_time_ms = CASE
                                WHEN start_time_ms IS NOT NULL AND start_time_ms > ? + 60000
                                THEN NULL
                                ELSE start_time_ms
                            END,
                            last_seen_ms = ?
                        WHERE id = ?
                    """, (fs, fin_end, fin_end, now_ms, row_id))
                else:
                    # Still active (e.g. PENDING after requeue)
                    conn.execute("""
                        UPDATE jobs SET
                            state = ?,
                            end_time_ms = ?,
                            start_time_ms = CASE
                                WHEN ? = 'PENDING' THEN NULL
                                WHEN ? IS NOT NULL THEN ?
                                ELSE start_time_ms
                            END,
                            last_seen_ms = ?
                        WHERE id = ?
                    """, (fs, end_ms, fs, start_ms, start_ms, now_ms, row_id))

            conn.commit()
        finally:
            conn.close()


def migrate_db(db_path: str):
    """Clear corrupted start_time_ms values caused by Slurm's estimated start times.

    When a PENDING job disappears from squeue, close_stale_jobs sets end_time_ms to
    last_seen_ms but historically left the Slurm-estimated (future) start_time_ms intact.
    This produces records where start_time > end_time — impossible in reality, and it
    causes queue calculations to treat cancelled jobs as perpetually in-queue.

    Safe to run repeatedly: only touches records where start_time_ms > end_time_ms.
    """
    import time as _time
    now_ms = int(_time.time() * 1000)
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            # Any job where start > end never actually started before it ended.
            # The start_time was a Slurm scheduling estimate, not a real start.
            cur = conn.execute("""
                UPDATE jobs SET start_time_ms = NULL
                WHERE start_time_ms IS NOT NULL
                  AND end_time_ms IS NOT NULL
                  AND start_time_ms > end_time_ms
            """)
            cleaned_terminal = cur.rowcount
            # Also clear future estimates on currently-PENDING jobs; the next poll
            # will leave start_time NULL (Layer 1 fix in slurm.py), but clean up now
            # in case the server restarts before the next poll.
            cur2 = conn.execute("""
                UPDATE jobs SET start_time_ms = NULL
                WHERE state = 'PENDING'
                  AND start_time_ms IS NOT NULL
                  AND start_time_ms > ?
            """, (now_ms,))
            cleaned_pending = cur2.rowcount
            conn.commit()
        finally:
            conn.close()
    return cleaned_terminal + cleaned_pending


def cleanup(db_path: str, retention_days: int | None):
    """Delete jobs where end_time_ms < (now - retention_days). If retention_days is None or <= 0, skip (keep forever)."""
    import time
    if retention_days is None or retention_days <= 0:
        return
    retention_ms = retention_days * 24 * 60 * 60 * 1000
    cutoff = int(time.time() * 1000) - retention_ms
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM jobs WHERE end_time_ms < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()


def insert_disk_sample(db_path: str, samples: list[dict[str, Any]], timestamp_ms: int):
    """Insert one disk sample (all disks at one timestamp)."""
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO disk_samples (timestamp_ms, samples_json) VALUES (?, ?)",
                (timestamp_ms, json.dumps(samples))
            )
            conn.commit()
        finally:
            conn.close()


def get_disk_samples_in_window(db_path: str, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
    """Return disk samples in [from_ms, to_ms]. Each item: { ts_ms, disks: { mount: { use_pct, used_gb, total_gb, avail_gb } } }."""
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute(
                "SELECT timestamp_ms, samples_json FROM disk_samples WHERE timestamp_ms >= ? AND timestamp_ms <= ? ORDER BY timestamp_ms",
                (from_ms, to_ms)
            )
            rows = cursor.fetchall()
            result = []
            for ts_ms, samples_json in rows:
                samples = json.loads(samples_json)
                disks = {}
                for s in samples:
                    mount = s.get("mount", "")
                    disks[mount] = {
                        "use_pct": s.get("use_pct", 0),
                        "used_gb": s.get("used_gb", 0),
                        "total_gb": s.get("total_gb", 0),
                        "avail_gb": s.get("avail_gb", 0),
                    }
                result.append({"ts_ms": ts_ms, "disks": disks})
            return result
        finally:
            conn.close()


def get_wait_buckets(db_path: str, from_ms: int, to_ms: int, group_by: str = 'user') -> dict:
    """Compute per-entity queued GPU counts per bucket and wait time stats.

    group_by: 'user' (default) | 'project' | 'unit'
    """
    import re as _re
    import statistics as _stats
    import time as _time

    _ARRAY_RANGE_RE = _re.compile(r'^(\d+)_\[')

    range_ms = to_ms - from_ms
    H = 3_600_000
    D = 86_400_000
    if range_ms <= 24 * H:
        bucket_size_ms = 10 * 60_000
    elif range_ms <= 10 * D:
        bucket_size_ms = H
    elif range_ms <= 60 * D:
        bucket_size_ms = D
    else:
        bucket_size_ms = 7 * D

    buckets = list(range(from_ms, to_ms, bucket_size_ms))
    n = len(buckets)
    now_ms = int(_time.time() * 1000)

    with _db_lock:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute("""
                WITH latest AS (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY submit_time_ms DESC) AS rn
                    FROM jobs
                    WHERE submit_time_ms < ?
                      AND (
                        (submit_time_ms >= ? AND start_time_ms IS NOT NULL AND start_time_ms <= ?)
                        OR
                        (start_time_ms IS NOT NULL AND start_time_ms <= ? AND start_time_ms > ?)
                        OR ((start_time_ms IS NULL OR start_time_ms > ?) AND (end_time_ms IS NULL OR end_time_ms > ?))
                      )
                )
                SELECT job_id, user, job_name, submit_time_ms, start_time_ms, end_time_ms, req_gpus, last_seen_ms
                FROM latest WHERE rn = 1
            """, (to_ms, from_ms, now_ms, now_ms, from_ms, now_ms, from_ms))
            rows = cursor.fetchall()
            meta_row = conn.execute(
                "SELECT nodes_json, gpus_per_node, cpus_per_node, node_capacities_json "
                "FROM meta WHERE id = 1"
            ).fetchone()
            proj_map, unit_counts = ({}, {}) if group_by == 'user' else _fetch_proj_map(conn, from_ms, to_ms)
            rule_map = {} if group_by == 'user' else _fetch_job_rule_map(conn)
        finally:
            conn.close()

    gpus_per_node = (meta_row["gpus_per_node"] or 8) if meta_row else 8
    node_count = max(1, len(json.loads(meta_row["nodes_json"] or "[]"))) if meta_row else 1

    # Deduplicate array-range snapshots (keep most-recently-seen per base_id+submit)
    deduped: list = []
    array_best: dict[tuple, object] = {}
    for row in rows:
        m = _ARRAY_RANGE_RE.match(row["job_id"])
        if m:
            key = (m.group(1), row["submit_time_ms"])
            existing = array_best.get(key)
            if existing is None or existing["last_seen_ms"] < row["last_seen_ms"]:
                array_best[key] = row
        else:
            deduped.append(row)
    deduped.extend(array_best.values())

    users_gpu: dict[str, list[float]] = {}
    # raw_waits: {user: [(start_ms, wait_h, req_gpus), ...]} — used for stats/charts
    raw_waits: dict[str, list] = {}
    colors: dict[str, str] = {}

    for row in deduped:
        user = row["user"] or "unknown"
        job_name = row["job_name"] or ""
        submit = row["submit_time_ms"]
        start = row["start_time_ms"]
        end = row["end_time_ms"]
        req_gpus = row["req_gpus"] or 0

        if start is not None and start <= now_ms:
            q_end = start
        elif end is not None:
            q_end = end
        else:
            q_end = now_ms

        first = max(0, (submit - from_ms) // bucket_size_ms)
        last = min(n - 1, (q_end - from_ms - 1) // bucket_size_ms)
        for i in range(int(first), int(last) + 1):
            b_start = from_ms + i * bucket_size_ms
            b_end = b_start + bucket_size_ms
            seg_start = max(submit, b_start)
            seg_end = min(q_end, b_end)
            if seg_end <= seg_start:
                continue
            if group_by == 'user':
                if user not in users_gpu:
                    users_gpu[user] = [0.0] * n
                users_gpu[user][i] += ((seg_end - seg_start) / bucket_size_ms) * req_gpus
            else:
                _accumulate_grouped_usage(
                    users_gpu, None, colors, n, i,
                    seg_start, seg_end, user, req_gpus, 0, bucket_size_ms,
                    proj_map, group_by, unit_counts, job_name, rule_map,
                )

        if start is not None and start <= now_ms and from_ms <= start < to_ms:
            wait_h = max(0, (start - submit) / H)
            raw_waits.setdefault(user, []).append((start, wait_h, req_gpus) if group_by == "user" else (start, wait_h, req_gpus, job_name))

    # Re-key wait stats by start-time group for project/unit views
    if group_by != 'user':
        grouped_raw: dict[str, list] = {}
        for user, entries in raw_waits.items():
            for (start_ms, wait_h, gp, job_name) in entries:
                gk, gc = _resolve_group_key(user, start_ms, proj_map, group_by, unit_counts, job_name, rule_map)
                grouped_raw.setdefault(gk, []).append((start_ms, wait_h, gp))
                colors[gk] = gc
        raw_waits = grouped_raw

    # Build stats and per-start-bucket sums/counts per entity.  The client combines
    # the selected entities, calculates each bucket's average, and carries the
    # previous value through buckets with no starts.
    stats: dict[str, dict] = {}
    stats_8gpu: dict[str, dict] = {}
    wait_time_buckets: dict[str, dict[str, list]] = {}
    for key, entries in raw_waits.items():
        series = {
            "cpu_only_sum": [0.0] * n,
            "cpu_only_count": [0] * n,
            "gpu_1_7_sum": [0.0] * n,
            "gpu_1_7_count": [0] * n,
            "gte8_sum": [0.0] * n,
            "gte8_count": [0] * n,
        }
        for start_ms, wait_h, gp in entries:
            i = int((start_ms - from_ms) // bucket_size_ms)
            if not 0 <= i < n:
                continue
            if gp >= 8:
                prefix = "gte8"
            elif gp >= 1:
                prefix = "gpu_1_7"
            else:
                prefix = "cpu_only"
            series[f"{prefix}_sum"][i] += wait_h
            series[f"{prefix}_count"][i] += 1
        wait_time_buckets[key] = series

        waits_sorted = sorted(e[1] for e in entries)
        gpu_num = sum(e[1] * e[2] for e in entries)
        gpu_den = sum(e[2] for e in entries)
        jc = len(waits_sorted)
        p90_idx = min(int(jc * 0.9), jc - 1)
        stats[key] = {
            "avg": sum(waits_sorted) / jc,
            "median": _stats.median(waits_sorted),
            "p90": waits_sorted[p90_idx],
            "gpu_weighted_avg": (gpu_num / gpu_den) if gpu_den > 0 else None,
            "job_count": jc,
        }
        large_entries = [e for e in entries if e[2] >= 8]
        if large_entries:
            large_waits = sorted(e[1] for e in large_entries)
            stats_8gpu[key] = {
                "avg": sum(large_waits) / len(large_waits),
                "job_count": len(large_waits),
            }

    return {
        "buckets": buckets,
        "bucket_size_ms": bucket_size_ms,
        "users": users_gpu,
        "wait_time_buckets": wait_time_buckets,
        "stats": stats,
        "stats_8gpu": stats_8gpu,
        "colors": colors,
        "group_by": group_by,
        "gpus_per_node": gpus_per_node,
        "node_count": node_count,
    }


def _shade_color(hex_color: str, sort_order: int, total: int) -> str:
    """Return a lightness-shaded variant of hex_color for the sort_order-th item among total."""
    if not hex_color or len(hex_color) != 7 or not hex_color.startswith('#'):
        return '#888888'
    try:
        r, g, b = int(hex_color[1:3], 16)/255, int(hex_color[3:5], 16)/255, int(hex_color[5:7], 16)/255
    except ValueError:
        return '#888888'
    h, l, s = _colorsys.rgb_to_hls(r, g, b)
    # Spread lightness 0.35 (darkest/first) → 0.62 (lightest/last)
    new_l = 0.35 + (sort_order % max(total, 1)) / max(total, 1) * 0.27
    # Keep original saturation — do NOT boost it for achromatic colours (grey, s≈0)
    # because grey has hue=0 (red) and boosting saturation would produce red/pink.
    r2, g2, b2 = _colorsys.hls_to_rgb(h, new_l, s)
    return '#{:02x}{:02x}{:02x}'.format(round(r2*255), round(g2*255), round(b2*255))


def _job_rule_matches(job_name: str, match_type: str, pattern: str) -> bool:
    if match_type == 'exact':
        return job_name == pattern
    return match_type == 'prefix' and job_name.startswith(pattern)


def _matching_job_rule(user: str, job_name: str, rule_map: dict) -> tuple | None:
    for row in rule_map.get(user, []):
        match_type, pattern = row[0], row[1]
        if _job_rule_matches(job_name, match_type, pattern):
            return row
    return None


def _group_key_for_rule(row: tuple, group_by: str, unit_counts: dict) -> tuple[str, str]:
    _match_type, _pattern, proj_id, proj_title, sort_order, unit_id, unit_title, unit_color = row
    assignment_row = (0, None, proj_id, proj_title, sort_order, unit_id, unit_title, unit_color)
    return _group_key_for_assignment(assignment_row, group_by, unit_counts)


def _resolve_group_key(
    user: str, ts: int, proj_map: dict, group_by: str, unit_counts: dict,
    job_name: str = '', rule_map: dict | None = None,
) -> tuple[str, str]:
    """Return (group_key, color) for user at timestamp ts given a project map."""
    rule = _matching_job_rule(user, job_name, rule_map or {})
    if rule is not None:
        return _group_key_for_rule(rule, group_by, unit_counts)
    for row in proj_map.get(user, []):
        start_ms, end_ms = row[0], row[1]
        if start_ms <= ts and (end_ms is None or end_ms > ts):
            return _group_key_for_assignment(row, group_by, unit_counts)
    return 'Unassigned', '#555555'


def _group_key_for_assignment(row: tuple, group_by: str, unit_counts: dict) -> tuple[str, str]:
    """Map one assignment row to (group_key, color)."""
    _start, _end, _proj_id, proj_title, sort_order, unit_id, unit_title, unit_color = row
    if group_by == 'project':
        base = unit_color or '#888888'
        return proj_title, _shade_color(base, sort_order, unit_counts.get(unit_id, 1) if unit_id else 1)
    return (unit_title or 'No Unit'), (unit_color or '#888888')


def _iter_assignment_segments(
    user: str, seg_start: int, seg_end: int, proj_map: dict, group_by: str, unit_counts: dict,
    job_name: str = '', rule_map: dict | None = None,
):
    """Partition [seg_start, seg_end) by project-assignment intervals (gaps → Unassigned)."""
    if seg_end <= seg_start:
        return
    rule = _matching_job_rule(user, job_name, rule_map or {})
    if rule is not None:
        key, color = _group_key_for_rule(rule, group_by, unit_counts)
        yield seg_start, seg_end, key, color
        return
    assignments = sorted(proj_map.get(user, []), key=lambda x: x[0])
    cursor = seg_start
    for row in assignments:
        a_start, a_end = row[0], row[1]
        if a_end is not None and a_end <= cursor:
            continue
        if a_start >= seg_end:
            break
        if cursor < a_start:
            yield cursor, min(a_start, seg_end), 'Unassigned', '#555555'
            cursor = a_start
        if cursor >= seg_end:
            return
        sub_end = seg_end if a_end is None else min(a_end, seg_end)
        if sub_end > cursor:
            key, color = _group_key_for_assignment(row, group_by, unit_counts)
            yield cursor, sub_end, key, color
            cursor = sub_end
    if cursor < seg_end:
        yield cursor, seg_end, 'Unassigned', '#555555'


def _accumulate_grouped_usage(
    grouped_gpu: dict[str, list[float]],
    grouped_cpu: dict[str, list[float]] | None,
    colors: dict[str, str],
    n: int,
    bucket_idx: int,
    seg_start: int,
    seg_end: int,
    user: str,
    req_gpus: int,
    req_cpus: int,
    bucket_size_ms: int,
    proj_map: dict,
    group_by: str,
    unit_counts: dict,
    job_name: str = '',
    rule_map: dict | None = None,
):
    """Add GPU/CPU bucket fractions for seg_start..seg_end split by assignment overlap."""
    for sub_start, sub_end, key, color in _iter_assignment_segments(
        user, seg_start, seg_end, proj_map, group_by, unit_counts, job_name, rule_map
    ):
        frac = (sub_end - sub_start) / bucket_size_ms
        if frac <= 0:
            continue
        if key not in grouped_gpu:
            grouped_gpu[key] = [0.0] * n
        grouped_gpu[key][bucket_idx] += frac * req_gpus
        if grouped_cpu is not None:
            if key not in grouped_cpu:
                grouped_cpu[key] = [0.0] * n
            grouped_cpu[key][bucket_idx] += frac * req_cpus
        colors[key] = color


def _fetch_proj_map(conn, from_ms: int, to_ms: int) -> tuple[dict, dict]:
    """Fetch project assignment intervals and unit project counts from an open connection."""
    proj_rows = conn.execute("""
        SELECT pa.user, pa.start_time_ms, pa.end_time_ms,
               p.id, p.title, p.sort_order,
               u.id, u.title, u.color
        FROM project_assignments pa
        JOIN projects p ON p.id = pa.project_id
        LEFT JOIN research_units u ON u.id = p.unit_id
        WHERE pa.start_time_ms < ?
          AND (pa.end_time_ms IS NULL OR pa.end_time_ms > ?)
        ORDER BY pa.user, pa.start_time_ms DESC
    """, (to_ms, from_ms)).fetchall()
    uc_rows = conn.execute(
        "SELECT unit_id, COUNT(*) FROM projects WHERE unit_id IS NOT NULL GROUP BY unit_id"
    ).fetchall()
    unit_counts = {r[0]: r[1] for r in uc_rows}
    proj_map: dict[str, list] = {}
    for r in proj_rows:
        user, start_ms, end_ms, proj_id, proj_title, sort_order, unit_id, unit_title, unit_color = r
        proj_map.setdefault(user, []).append(
            (start_ms, end_ms, proj_id, proj_title, sort_order or 0, unit_id, unit_title, unit_color)
        )
    return proj_map, unit_counts


def _fetch_job_rule_map(conn) -> dict:
    """Fetch job-name project overrides, most specific and highest priority first."""
    rows = conn.execute("""
        SELECT r.user, r.match_type, r.pattern,
               p.id, p.title, p.sort_order,
               u.id, u.title, u.color
        FROM job_project_rules r
        JOIN projects p ON p.id = r.project_id
        LEFT JOIN research_units u ON u.id = p.unit_id
        ORDER BY r.user, r.priority DESC, LENGTH(r.pattern) DESC, r.id
    """).fetchall()
    rule_map: dict[str, list] = {}
    for row in rows:
        user, match_type, pattern, proj_id, proj_title, sort_order, unit_id, unit_title, unit_color = row
        rule_map.setdefault(user, []).append(
            (match_type, pattern, proj_id, proj_title, sort_order or 0,
             unit_id, unit_title, unit_color)
        )
    return rule_map


def get_usage_buckets(db_path: str, from_ms: int, to_ms: int, group_by: str = 'user') -> dict:
    """Compute per-entity GPU/CPU average allocation per time bucket.

    group_by: 'user' (default) | 'project' | 'unit'
    Bucket sizes: ≤24h→10min, ≤10d→1h, ≤60d→1d, else 1w.
    """
    import time as _time

    range_ms = to_ms - from_ms
    H = 3_600_000
    D = 86_400_000
    if range_ms <= 24 * H:
        bucket_size_ms = 10 * 60_000
    elif range_ms <= 10 * D:
        bucket_size_ms = H
    elif range_ms <= 60 * D:
        bucket_size_ms = D
    else:
        bucket_size_ms = 7 * D

    buckets = list(range(from_ms, to_ms, bucket_size_ms))
    n = len(buckets)

    with _db_lock:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute("""
                WITH latest AS (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY submit_time_ms DESC) AS rn
                    FROM jobs
                    WHERE start_time_ms IS NOT NULL
                      AND start_time_ms < ?
                      AND (end_time_ms IS NULL OR end_time_ms > ?)
                )
                SELECT user, job_name, start_time_ms, end_time_ms, req_gpus, req_cpus
                FROM latest WHERE rn = 1
            """, (to_ms, from_ms))
            rows = cursor.fetchall()
            meta_row = conn.execute(
                "SELECT nodes_json, gpus_per_node, cpus_per_node, node_capacities_json "
                "FROM meta WHERE id = 1"
            ).fetchone()
            proj_map, unit_counts = ({}, {}) if group_by == 'user' else _fetch_proj_map(conn, from_ms, to_ms)
            rule_map = {} if group_by == 'user' else _fetch_job_rule_map(conn)
        finally:
            conn.close()

    gpus_per_node = (meta_row["gpus_per_node"] or 8) if meta_row else 8
    cpus_per_node = (meta_row["cpus_per_node"] or 224) if meta_row else 224
    node_count = max(1, len(json.loads(meta_row["nodes_json"] or "[]"))) if meta_row else 1
    node_capacities = json.loads(meta_row["node_capacities_json"] or "{}") if meta_row else {}
    total_gpus = sum(int(c.get("gpus", 0)) for c in node_capacities.values())
    total_cpus = sum(int(c.get("cpus", 0)) for c in node_capacities.values())
    if total_gpus <= 0:
        total_gpus = node_count * gpus_per_node
    if total_cpus <= 0:
        total_cpus = node_count * cpus_per_node

    now_ms = int(_time.time() * 1000)
    users_gpu: dict[str, list[float]] = {}
    users_cpu: dict[str, list[float]] = {}
    colors: dict[str, str] = {}

    for row in rows:
        user = row["user"] or "unknown"
        job_name = row["job_name"] or ""
        start = row["start_time_ms"]
        end = row["end_time_ms"] if row["end_time_ms"] else now_ms
        req_gpus = row["req_gpus"] or 0
        req_cpus = row["req_cpus"] or 0

        first = max(0, (start - from_ms) // bucket_size_ms)
        last = min(n - 1, (end - from_ms - 1) // bucket_size_ms)
        for i in range(int(first), int(last) + 1):
            b_start = from_ms + i * bucket_size_ms
            b_end = b_start + bucket_size_ms
            seg_start = max(start, b_start)
            seg_end = min(end, b_end)
            if seg_end <= seg_start:
                continue
            if group_by == 'user':
                if user not in users_gpu:
                    users_gpu[user] = [0.0] * n
                    users_cpu[user] = [0.0] * n
                frac = (seg_end - seg_start) / bucket_size_ms
                users_gpu[user][i] += frac * req_gpus
                users_cpu[user][i] += frac * req_cpus
            else:
                _accumulate_grouped_usage(
                    users_gpu, users_cpu, colors, n, i,
                    seg_start, seg_end, user, req_gpus, req_cpus, bucket_size_ms,
                    proj_map, group_by, unit_counts, job_name, rule_map,
                )

    return {
        "buckets": buckets,
        "bucket_size_ms": bucket_size_ms,
        "users": users_gpu,
        "cpu_users": users_cpu,
        "colors": colors,
        "group_by": group_by,
        "gpus_per_node": gpus_per_node,
        "cpus_per_node": cpus_per_node,
        "node_count": node_count,
        "node_capacities": node_capacities,
        "total_gpus": total_gpus,
        "total_cpus": total_cpus,
    }


def cleanup_disk_samples(db_path: str, retention_days: int):
    """Delete disk_samples older than retention_days."""
    import time
    if retention_days <= 0:
        return
    retention_ms = retention_days * 24 * 60 * 60 * 1000
    cutoff = int(time.time() * 1000) - retention_ms
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM disk_samples WHERE timestamp_ms < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()


# ── Research units & projects CRUD ───────────────────────────

def get_all_units(db_path: str) -> dict:
    """Return all units with their projects and assignments."""
    with _db_lock:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            units = conn.execute(
                "SELECT id, title, color FROM research_units ORDER BY title"
            ).fetchall()
            projects = conn.execute(
                "SELECT id, title, unit_id, sort_order FROM projects ORDER BY unit_id, sort_order, title"
            ).fetchall()
            assignments = conn.execute(
                "SELECT id, project_id, user, start_time_ms, end_time_ms "
                "FROM project_assignments ORDER BY start_time_ms"
            ).fetchall()
        finally:
            conn.close()

    assigns_by_proj: dict[int, list] = {}
    for a in assignments:
        assigns_by_proj.setdefault(a['project_id'], []).append({
            'id': a['id'], 'user': a['user'],
            'start_time_ms': a['start_time_ms'], 'end_time_ms': a['end_time_ms'],
        })

    projs_by_unit: dict[int, list] = {}
    unaffiliated: list = []
    for p in projects:
        d = {
            'id': p['id'], 'title': p['title'],
            'unit_id': p['unit_id'], 'sort_order': p['sort_order'],
            'assignments': assigns_by_proj.get(p['id'], []),
        }
        if p['unit_id'] is not None:
            projs_by_unit.setdefault(p['unit_id'], []).append(d)
        else:
            unaffiliated.append(d)

    return {
        'units': [
            {'id': u['id'], 'title': u['title'], 'color': u['color'],
             'projects': projs_by_unit.get(u['id'], [])}
            for u in units
        ],
        'unaffiliated': unaffiliated,
    }


def create_unit(db_path: str, title: str, color: str | None = None) -> int:
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "INSERT INTO research_units (title, color) VALUES (?, ?)", (title, color)
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def update_unit(db_path: str, unit_id: int, data: dict):
    """data keys: title, color"""
    fields, vals = [], []
    for key in ('title', 'color'):
        if key in data:
            fields.append(f'{key} = ?')
            vals.append(data[key])
    if not fields:
        return
    vals.append(unit_id)
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(f"UPDATE research_units SET {', '.join(fields)} WHERE id = ?", vals)
            conn.commit()
        finally:
            conn.close()


def delete_unit(db_path: str, unit_id: int):
    """Unaffiliate projects instead of cascading delete."""
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE projects SET unit_id = NULL WHERE unit_id = ?", (unit_id,))
            conn.execute("DELETE FROM research_units WHERE id = ?", (unit_id,))
            conn.commit()
        finally:
            conn.close()


def create_project(db_path: str, title: str, unit_id: int | None = None) -> int:
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            if unit_id is not None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(sort_order) + 1, 0) FROM projects WHERE unit_id = ?",
                    (unit_id,)
                ).fetchone()
                sort_order = row[0] if row else 0
            else:
                sort_order = 0
            cur = conn.execute(
                "INSERT INTO projects (title, unit_id, sort_order) VALUES (?, ?, ?)",
                (title, unit_id, sort_order)
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def update_project(db_path: str, project_id: int, data: dict):
    """data keys: title, unit_id (None to unaffiliate)"""
    fields, vals = [], []
    for key in ('title', 'unit_id'):
        if key in data:
            fields.append(f'{key} = ?')
            vals.append(data[key])
    if not fields:
        return
    vals.append(project_id)
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = ?", vals)
            conn.commit()
        finally:
            conn.close()


def delete_project(db_path: str, project_id: int):
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM project_assignments WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            conn.commit()
        finally:
            conn.close()


def create_assignment(db_path: str, project_id: int, user: str,
                      start_time_ms: int, end_time_ms: int | None = None) -> int:
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "INSERT INTO project_assignments (project_id, user, start_time_ms, end_time_ms) VALUES (?, ?, ?, ?)",
                (project_id, user, start_time_ms, end_time_ms)
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def update_assignment(db_path: str, assignment_id: int, data: dict):
    """data keys: user, start_time_ms, end_time_ms"""
    fields, vals = [], []
    for key in ('user', 'start_time_ms', 'end_time_ms'):
        if key in data:
            fields.append(f'{key} = ?')
            vals.append(data[key])
    if not fields:
        return
    vals.append(assignment_id)
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(f"UPDATE project_assignments SET {', '.join(fields)} WHERE id = ?", vals)
            conn.commit()
        finally:
            conn.close()


def delete_assignment(db_path: str, assignment_id: int):
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM project_assignments WHERE id = ?", (assignment_id,))
            conn.commit()
        finally:
            conn.close()


def create_job_project_rule(
    db_path: str, project_id: int, user: str, match_type: str,
    pattern: str, priority: int = 0,
) -> int:
    if match_type not in ('exact', 'prefix'):
        raise ValueError("match_type must be 'exact' or 'prefix'")
    if not user or not pattern:
        raise ValueError('user and pattern are required')
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "INSERT INTO job_project_rules "
                "(project_id, user, match_type, pattern, priority) VALUES (?, ?, ?, ?, ?)",
                (project_id, user, match_type, pattern, priority),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def delete_job_project_rule(db_path: str, rule_id: int):
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM job_project_rules WHERE id = ?", (rule_id,))
            conn.commit()
        finally:
            conn.close()


def get_unassigned_jobs(db_path: str) -> list[dict]:
    """Return users who have jobs not covered by any project assignment.

    For each such user, returns job count, total GPU-hours, and the time range
    of unassigned jobs so the editor can suggest a sensible assignment period.
    """
    import time as _time
    now_ms = int(_time.time() * 1000)
    with _db_lock:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT j.user,
                       COUNT(*) AS job_count,
                       SUM(j.req_gpus *
                           (COALESCE(j.end_time_ms, ?) - j.start_time_ms) / 3600000.0
                       ) AS gpu_hours,
                       MIN(j.start_time_ms) AS earliest_ms,
                       MAX(j.start_time_ms) AS latest_ms
                FROM jobs j
                WHERE j.start_time_ms IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM project_assignments pa
                    WHERE pa.user = j.user
                      AND pa.start_time_ms <= j.start_time_ms
                      AND (pa.end_time_ms IS NULL OR pa.end_time_ms > j.start_time_ms)
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM job_project_rules r
                    WHERE r.user = j.user
                      AND (
                        (r.match_type = 'exact' AND COALESCE(j.job_name, '') = r.pattern)
                        OR (r.match_type = 'prefix' AND
                            SUBSTR(COALESCE(j.job_name, ''), 1, LENGTH(r.pattern)) = r.pattern)
                      )
                  )
                GROUP BY j.user
                ORDER BY gpu_hours DESC
            """, (now_ms,)).fetchall()
        finally:
            conn.close()
    return [
        {
            'user': r['user'],
            'job_count': r['job_count'],
            'gpu_hours': round(r['gpu_hours'] or 0, 1),
            'earliest_ms': r['earliest_ms'],
            'latest_ms': r['latest_ms'],
        }
        for r in rows
    ]


def get_known_users(db_path: str) -> list[str]:
    """Return distinct users from the jobs table."""
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT user FROM jobs WHERE user IS NOT NULL ORDER BY user"
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
