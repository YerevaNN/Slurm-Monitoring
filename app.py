"""Flask app: serve dashboard and /api/jobs."""
import os
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

import config
import history
from slurm import fetch_jobs, fetch_node_capacities, fetch_node_states, fetch_nodes, get_job_final_info, update_job_settings

app = Flask(__name__, static_folder="static", static_url_path="")

# Recorder state
_recorder_interval_sec = config.DEFAULT_REFRESH
_recorder_interval_lock = threading.Lock()


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


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
    })


@app.route("/api/config")
def api_config():
    return jsonify({
        "refresh_intervals": config.REFRESH_INTERVALS,
        "default_refresh": config.DEFAULT_REFRESH,
    })


@app.route("/api/job/<job_id>/settings", methods=["POST"])
def api_job_settings(job_id):
    data = request.get_json(silent=True) or {}
    priority = data.get("priority")
    num_cpus = data.get("num_cpus")
    memory_mb = data.get("memory_mb")
    if priority is None and num_cpus is None and memory_mb is None:
        return jsonify({"ok": False, "error": "No settings to update"}), 400
    ok, msg = update_job_settings(job_id, priority=priority, num_cpus=num_cpus, memory_mb=memory_mb)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True})


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
        gpus_per_node, cpus_per_node = fetch_node_capacities()
        
        if gpus_per_node <= 0:
            gpus_per_node = config.GPUS_PER_NODE
        if cpus_per_node <= 0:
            cpus_per_node = config.CPUS_PER_NODE
        
        history.upsert_jobs(config.HISTORY_DB_PATH, jobs, now_ms)
        history.update_meta(config.HISTORY_DB_PATH, nodes, node_states, node_reasons,
                           gpus_per_node, cpus_per_node, now_ms)
        
        interval = get_recorder_interval()
        stale_threshold = now_ms - (2 * interval * 1000)
        history.close_stale_jobs(config.HISTORY_DB_PATH, stale_threshold, get_job_final_info)
        
        history.cleanup(config.HISTORY_DB_PATH, config.HISTORY_RETENTION_DAYS)
    except Exception as e:
        print(f"Recorder error: {e}")
        import traceback
        traceback.print_exc()


def start_recorder():
    """Start background recorder thread."""
    os.makedirs(os.path.dirname(config.HISTORY_DB_PATH), exist_ok=True)
    history.init_db(config.HISTORY_DB_PATH)
    thread = threading.Thread(target=recorder_loop, daemon=True)
    thread.start()


# Start recorder on module load
start_recorder()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
