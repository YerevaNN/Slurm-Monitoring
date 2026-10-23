"""Flask app: serve dashboard and /api/jobs."""
import hmac
import os
import threading
import time
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory

import config
import history
from disk import fetch_disk_usage
from slurm import (
    build_job_settings_command,
    fetch_jobs, fetch_node_capacities, fetch_node_states, fetch_nodes, fetch_partitions,
    get_job_final_info,
)

app = Flask(__name__, static_folder="static", static_url_path="")


def admin_required(fn):
    """Require the dashboard's private token for metadata-changing requests."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not config.ADMIN_TOKEN:
            return jsonify({"ok": False, "error": "Admin editing is disabled"}), 503
        supplied = request.headers.get("X-Slurm-Monitor-Admin-Token", "")
        if not supplied or not hmac.compare_digest(supplied, config.ADMIN_TOKEN):
            return jsonify({"ok": False, "error": "Admin token required"}), 401
        return fn(*args, **kwargs)

    return wrapped

# Recorder state
_recorder_interval_sec = config.DEFAULT_REFRESH
_recorder_interval_lock = threading.Lock()

_duration_stats_cache: dict[str, dict] | None = None
_duration_stats_cache_ts: float = 0.0
_duration_stats_lock = threading.Lock()

_partitions_cache: dict[str, dict[str, int]] | None = None
_partitions_cache_ts: float = 0.0
_partitions_lock = threading.Lock()


def get_partitions(meta: dict | None = None) -> dict[str, dict[str, int]]:
    """
    Cached partition definitions (PriorityTier / PriorityJobFactor).

    Read live from scontrol rather than from the meta row: partition config is
    static cluster state, and the meta row can be overwritten by another recorder
    instance sharing the same database. Falls back to meta when scontrol fails.
    """
    global _partitions_cache, _partitions_cache_ts
    now = time.time()
    with _partitions_lock:
        if _partitions_cache and (now - _partitions_cache_ts) < config.PARTITIONS_TTL_SEC:
            return _partitions_cache
    partitions = fetch_partitions()
    if not partitions:
        return (meta or {}).get("partitions", {}) or {}
    with _partitions_lock:
        _partitions_cache = partitions
        _partitions_cache_ts = now
    return partitions


def get_job_duration_stats() -> dict[str, dict]:
    """Cached completed-job duration stats keyed by job_name prefix."""
    global _duration_stats_cache, _duration_stats_cache_ts
    now = time.time()
    with _duration_stats_lock:
        if (
            _duration_stats_cache is not None
            and (now - _duration_stats_cache_ts) < config.DURATION_STATS_TTL_SEC
        ):
            return _duration_stats_cache
    stats = history.get_job_duration_stats_by_prefix(
        config.HISTORY_DB_PATH, config.JOB_NAME_PREFIX_LEN,
    )
    with _duration_stats_lock:
        _duration_stats_cache = stats
        _duration_stats_cache_ts = now
    return stats


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/usage")
def usage_page():
    return send_from_directory(app.static_folder, "usage.html")


@app.route("/api/jobs")
def api_jobs():
    """
    Get jobs in a time window from DB.
    Query params: from (ms), to (ms), interval (seconds, optional).
    """
    # Optional: update recorder interval
    interval_param = request.args.get("interval")
    if interval_param:
        try:
            set_recorder_interval(int(interval_param))
        except (ValueError, TypeError):
            pass
    
    # Get time window
    from_ms = request.args.get("from", type=int)
    to_ms = request.args.get("to", type=int)
    
    if from_ms is None or to_ms is None:
        return jsonify({"error": "Missing from/to parameters"}), 400
    
    # Read from DB
    jobs = history.get_jobs_in_window(config.HISTORY_DB_PATH, from_ms, to_ms)
    meta = history.get_latest_meta(config.HISTORY_DB_PATH)
    
    return jsonify({
        "jobs": jobs,
        "nodes": meta["nodes"],
        "node_states": meta["node_states"],
        "node_reasons": meta["node_reasons"],
        "gpus_per_node": meta["gpus_per_node"],
        "cpus_per_node": meta["cpus_per_node"],
        "node_capacities": meta["node_capacities"],
        "total_gpus": meta["total_gpus"],
        "total_cpus": meta["total_cpus"],
        "partitions": get_partitions(meta),
    })


@app.route("/api/job-duration-stats")
def api_job_duration_stats():
    """Cached completed-job runtime stats keyed by job_name prefix."""
    return jsonify({
        "job_duration_stats": get_job_duration_stats(),
        "job_name_prefix_len": config.JOB_NAME_PREFIX_LEN,
    })


@app.route("/api/usage")
def api_usage():
    """Get GPU/CPU usage buckets. Params: from (ms), to (ms), group_by (user|project|unit)."""
    from_ms = request.args.get("from", type=int)
    to_ms = request.args.get("to", type=int)
    if from_ms is None or to_ms is None:
        return jsonify({"error": "Missing from/to parameters"}), 400
    group_by = request.args.get("group_by", "user")
    if group_by not in ("user", "project", "unit"):
        group_by = "user"
    data = history.get_usage_buckets(config.HISTORY_DB_PATH, from_ms, to_ms, group_by)
    return jsonify(data)


@app.route("/api/wait")
def api_wait():
    """Get job wait time buckets and stats. Params: from (ms), to (ms), group_by (user|project|unit)."""
    from_ms = request.args.get("from", type=int)
    to_ms = request.args.get("to", type=int)
    if from_ms is None or to_ms is None:
        return jsonify({"error": "Missing from/to parameters"}), 400
    group_by = request.args.get("group_by", "user")
    if group_by not in ("user", "project", "unit"):
        group_by = "user"
    data = history.get_wait_buckets(config.HISTORY_DB_PATH, from_ms, to_ms, group_by)
    return jsonify(data)


@app.route("/api/config")
def api_config():
    return jsonify({
        "refresh_intervals": config.REFRESH_INTERVALS,
        "default_refresh": config.DEFAULT_REFRESH,
    })


@app.route("/api/job/<job_id>/settings-command", methods=["POST"])
def api_job_settings_command(job_id):
    data = request.get_json(silent=True) or {}
    priority = data.get("priority")
    num_cpus = data.get("num_cpus")
    memory_mb = data.get("memory_mb")
    if priority is None and num_cpus is None and memory_mb is None:
        return jsonify({"ok": False, "error": "No settings to update"}), 400
    ok, result = build_job_settings_command(
        job_id, priority=priority, num_cpus=num_cpus, memory_mb=memory_mb,
    )
    if not ok:
        return jsonify({"ok": False, "error": result}), 400
    return jsonify({"ok": True, "command": result})


@app.route("/api/users")
def api_users():
    """Return distinct users from the jobs table."""
    return jsonify(history.get_known_users(config.HISTORY_DB_PATH))


@app.route("/api/unassigned-jobs")
def api_unassigned_jobs():
    """Return users with jobs not covered by any project assignment."""
    return jsonify(history.get_unassigned_jobs(config.HISTORY_DB_PATH))


# ── Research units ────────────────────────────────────────────

@app.route("/api/units", methods=["GET"])
def api_units_list():
    return jsonify(history.get_all_units(config.HISTORY_DB_PATH))


@app.route("/api/units", methods=["POST"])
@admin_required
def api_units_create():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    color = data.get("color") or _next_unit_color()
    unit_id = history.create_unit(config.HISTORY_DB_PATH, title, color)
    return jsonify({"ok": True, "id": unit_id})


@app.route("/api/units/<int:unit_id>", methods=["PUT"])
@admin_required
def api_units_update(unit_id):
    data = request.get_json(silent=True) or {}
    history.update_unit(config.HISTORY_DB_PATH, unit_id, data)
    return jsonify({"ok": True})


@app.route("/api/units/<int:unit_id>", methods=["DELETE"])
@admin_required
def api_units_delete(unit_id):
    history.delete_unit(config.HISTORY_DB_PATH, unit_id)
    return jsonify({"ok": True})


# ── Projects ──────────────────────────────────────────────────

@app.route("/api/projects", methods=["POST"])
@admin_required
def api_projects_create():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    unit_id = data.get("unit_id")
    project_id = history.create_project(config.HISTORY_DB_PATH, title, unit_id)
    return jsonify({"ok": True, "id": project_id})


@app.route("/api/projects/<int:project_id>", methods=["PUT"])
@admin_required
def api_projects_update(project_id):
    data = request.get_json(silent=True) or {}
    history.update_project(config.HISTORY_DB_PATH, project_id, data)
    return jsonify({"ok": True})


@app.route("/api/projects/<int:project_id>", methods=["DELETE"])
@admin_required
def api_projects_delete(project_id):
    history.delete_project(config.HISTORY_DB_PATH, project_id)
    return jsonify({"ok": True})


# ── Assignments ───────────────────────────────────────────────

@app.route("/api/projects/<int:project_id>/assignments", methods=["POST"])
@admin_required
def api_assignments_create(project_id):
    data = request.get_json(silent=True) or {}
    user = (data.get("user") or "").strip()
    start_ms = data.get("start_time_ms")
    if not user or start_ms is None:
        return jsonify({"error": "user and start_time_ms required"}), 400
    end_ms = data.get("end_time_ms")
    aid = history.create_assignment(config.HISTORY_DB_PATH, project_id, user, int(start_ms), end_ms)
    return jsonify({"ok": True, "id": aid})


@app.route("/api/projects/<int:project_id>/assignments/<int:assignment_id>", methods=["PUT"])
@admin_required
def api_assignments_update(project_id, assignment_id):
    data = request.get_json(silent=True) or {}
    history.update_assignment(config.HISTORY_DB_PATH, assignment_id, data)
    return jsonify({"ok": True})


@app.route("/api/projects/<int:project_id>/assignments/<int:assignment_id>", methods=["DELETE"])
@admin_required
def api_assignments_delete(project_id, assignment_id):
    history.delete_assignment(config.HISTORY_DB_PATH, assignment_id)
    return jsonify({"ok": True})


def _next_unit_color() -> str:
    """Pick the next unused color from the palette for a new unit."""
    import config as _cfg
    units_data = history.get_all_units(config.HISTORY_DB_PATH)
    used = {u["color"] for u in units_data["units"] if u["color"]}
    for c in _cfg.PROJECT_PALETTE:
        if c not in used:
            return c
    return _cfg.PROJECT_PALETTE[len(units_data["units"]) % len(_cfg.PROJECT_PALETTE)]


@app.route("/api/disk")
def api_disk():
    """Get disk samples in time window. Query params: from (ms), to (ms)."""
    from_ms = request.args.get("from", type=int)
    to_ms = request.args.get("to", type=int)
    if from_ms is None or to_ms is None:
        return jsonify({"error": "Missing from/to parameters"}), 400
    samples = history.get_disk_samples_in_window(config.HISTORY_DB_PATH, from_ms, to_ms)
    disks_set = set()
    for s in samples:
        disks_set.update(s.get("disks", {}).keys())
    disks = sorted(disks_set)
    return jsonify({"disks": disks, "samples": samples})


def get_recorder_interval() -> int:
    """Get current recorder interval in seconds."""
    with _recorder_interval_lock:
        return _recorder_interval_sec


def set_recorder_interval(seconds: int):
    """Set recorder interval (seconds)."""
    global _recorder_interval_sec
    with _recorder_interval_lock:
        _recorder_interval_sec = max(1, int(seconds))


def recorder_loop():
    """Background loop: fetch from Slurm, upsert to DB, close stale jobs, cleanup."""
    # Immediate first run before sleeping
    run_recorder_cycle()
    
    while True:
        interval = get_recorder_interval()
        time.sleep(interval)
        run_recorder_cycle()


def run_recorder_cycle():
    """One recorder cycle: fetch, upsert, close stale, cleanup."""
    try:
        now_ms = int(time.time() * 1000)
        jobs = fetch_jobs()
        nodes = fetch_nodes()
        node_states, node_reasons = fetch_node_states()
        node_capacities = fetch_node_capacities()
        partitions = fetch_partitions()

        gpus_per_node = max(
            (capacity.get("gpus", 0) for capacity in node_capacities.values()),
            default=0,
        ) or config.GPUS_PER_NODE
        cpus_per_node = max(
            (capacity.get("cpus", 0) for capacity in node_capacities.values()),
            default=0,
        ) or config.CPUS_PER_NODE
        
        history.upsert_jobs(config.HISTORY_DB_PATH, jobs, now_ms)
        history.update_meta(config.HISTORY_DB_PATH, nodes, node_states, node_reasons,
                           gpus_per_node, cpus_per_node, now_ms, partitions,
                           node_capacities)
        
        interval = get_recorder_interval()
        stale_threshold = now_ms - (2 * interval * 1000)
        history.close_stale_jobs(config.HISTORY_DB_PATH, stale_threshold, get_job_final_info, now_ms)
        
        history.cleanup(config.HISTORY_DB_PATH, config.HISTORY_RETENTION_DAYS)
        disk_samples = fetch_disk_usage(config.DISK_MIN_SIZE_GB)
        if disk_samples:
            history.insert_disk_sample(config.HISTORY_DB_PATH, disk_samples, now_ms)
        history.cleanup_disk_samples(config.HISTORY_DB_PATH, config.DISK_RETENTION_DAYS)
    except Exception as e:
        print(f"Recorder error: {e}")
        import traceback
        traceback.print_exc()


def start_recorder():
    """Start background recorder thread."""
    os.makedirs(os.path.dirname(config.HISTORY_DB_PATH), exist_ok=True)
    history.init_db(config.HISTORY_DB_PATH)
    cleaned = history.migrate_db(config.HISTORY_DB_PATH)
    if cleaned:
        print(f"[migrate_db] Cleared bogus start_time_ms on {cleaned} records.")
    thread = threading.Thread(target=recorder_loop, daemon=True)
    thread.start()


# Start one recorder in the serving process. Tests and maintenance imports can
# explicitly suppress it to avoid creating a second collector.
if not config.DISABLE_RECORDER:
    start_recorder()


if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, debug=False)
