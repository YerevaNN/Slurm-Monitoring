"""Flask app: serve dashboard and /api/jobs."""
from flask import Flask, jsonify, request, send_from_directory

import config
from slurm import fetch_jobs, fetch_node_capacities, fetch_node_states, fetch_nodes, update_job_settings

app = Flask(__name__, static_folder="static", static_url_path="")


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/jobs")
def api_jobs():
    jobs = fetch_jobs()
    nodes = fetch_nodes()
    node_states, node_reasons = fetch_node_states()
    gpus_per_node, cpus_per_node = fetch_node_capacities()
    if gpus_per_node <= 0:
        gpus_per_node = config.GPUS_PER_NODE
    if cpus_per_node <= 0:
        cpus_per_node = config.CPUS_PER_NODE
    return jsonify({
        "jobs": jobs,
        "nodes": nodes,
        "node_states": node_states,
        "node_reasons": node_reasons,
        "gpus_per_node": gpus_per_node,
        "cpus_per_node": cpus_per_node,
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
