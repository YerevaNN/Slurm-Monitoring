"""SQLite-based history storage for Slurm jobs and cluster metadata."""
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
                req_mem INTEGER,
                time_limit_ms INTEGER,
                priority INTEGER,
                array_task_count INTEGER,
                allocations_json TEXT,
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
                updated_at_ms INTEGER
            )
        """)
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
                            time_limit_ms = ?, priority = ?,
                            array_task_count = ?, allocations_json = ?,
                            last_seen_ms = ?
                        WHERE id = ?
                    """, (
                        job.get("user"), job.get("job_name"), job.get("state"),
                        job.get("start_time"), job.get("end_time"),
                        job.get("req_gpus", 0), job.get("req_cpus", 0), job.get("req_mem", 0),
                        job.get("time_limit_ms"), job.get("priority", 0),
                        job.get("array_task_count", 1), allocations_json,
                        fetch_ts_ms, row[0]
                    ))
                else:
                    # Insert
                    conn.execute("""
                        INSERT INTO jobs (
                            job_id, user, job_name, state,
                            start_time_ms, end_time_ms, submit_time_ms,
                            req_gpus, req_cpus, req_mem,
                            time_limit_ms, priority, array_task_count,
                            allocations_json, last_seen_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        job_id, job.get("user"), job.get("job_name"), job.get("state"),
                        job.get("start_time"), job.get("end_time"), submit_time_ms,
                        job.get("req_gpus", 0), job.get("req_cpus", 0), job.get("req_mem", 0),
                        job.get("time_limit_ms"), job.get("priority", 0),
                        job.get("array_task_count", 1), allocations_json, fetch_ts_ms
                    ))
            conn.commit()
        finally:
            conn.close()


def update_meta(db_path: str, nodes: list[str], node_states: dict[str, str],
                node_reasons: dict[str, str], gpus_per_node: int, cpus_per_node: int, ts_ms: int):
    """Update cluster metadata (single row, id=1)."""
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO meta (
                    id, nodes_json, node_states_json, node_reasons_json,
                    gpus_per_node, cpus_per_node, updated_at_ms
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
            """, (
                json.dumps(nodes), json.dumps(node_states), json.dumps(node_reasons),
                gpus_per_node, cpus_per_node, ts_ms
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
                       gpus_per_node, cpus_per_node
                FROM meta WHERE id = 1
            """)
            row = cursor.fetchone()
            if not row:
                return {
                    "nodes": [],
                    "node_states": {},
                    "node_reasons": {},
                    "gpus_per_node": 8,
                    "cpus_per_node": 224
                }
            return {
                "nodes": json.loads(row[0]) if row[0] else [],
                "node_states": json.loads(row[1]) if row[1] else {},
                "node_reasons": json.loads(row[2]) if row[2] else {},
                "gpus_per_node": row[3] or 8,
                "cpus_per_node": row[4] or 224
            }
        finally:
            conn.close()


def get_jobs_in_window(db_path: str, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
    """Return jobs that overlap the time window [from_ms, to_ms]."""
    with _db_lock:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute("""
                SELECT * FROM jobs
                WHERE submit_time_ms < ?
                  AND (end_time_ms IS NULL OR end_time_ms > ?)
            """, (to_ms, from_ms))
            rows = cursor.fetchall()
            jobs = []
            for row in rows:
                job = dict(row)
                job["allocations"] = json.loads(job.pop("allocations_json", "[]"))
                job.pop("id", None)
                job.pop("last_seen_ms", None)
                # Rename fields to match frontend expectations (remove _ms suffix)
                job["start_time"] = job.pop("start_time_ms", None)
                job["end_time"] = job.pop("end_time_ms", None)
                job["submit_time"] = job.pop("submit_time_ms", None)
                job["time_limit_ms"] = job.get("time_limit_ms")  # Keep _ms suffix for this one
                jobs.append(job)
            return jobs
        finally:
            conn.close()


def close_stale_jobs(db_path: str, stale_threshold_ms: int, get_final_info_fn):
    """
    Close jobs where last_seen_ms < stale_threshold_ms and state is RUNNING or PENDING.
    For each, call get_final_info_fn(job_id) -> (state, end_time_ms).
    """
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute("""
                SELECT id, job_id, last_seen_ms FROM jobs
                WHERE last_seen_ms < ? AND state IN ('RUNNING', 'PENDING')
            """, (stale_threshold_ms,))
            stale_rows = cursor.fetchall()
            
            for row_id, job_id, last_seen_ms in stale_rows:
                final_state, final_end_time_ms = get_final_info_fn(job_id)
                if final_state is None:
                    final_state = "FINISHED_UNKNOWN"
                if final_end_time_ms is None:
                    final_end_time_ms = last_seen_ms
                
                conn.execute("""
                    UPDATE jobs SET state = ?, end_time_ms = ?
                    WHERE id = ?
                """, (final_state, final_end_time_ms, row_id))
            
            conn.commit()
        finally:
            conn.close()


def cleanup(db_path: str, retention_days: int):
    """Delete jobs where end_time_ms < (now - retention_days)."""
    import time
    retention_ms = retention_days * 24 * 60 * 60 * 1000
    cutoff = int(time.time() * 1000) - retention_ms
    with _db_lock:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM jobs WHERE end_time_ms < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()
