#!/usr/bin/env python3
"""Sample local allocated GPUs every 15s and send to the controller. No raw files."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gpu_metrics import PERIOD_MS, gres_minors, parse_allocations, parse_nvidia_xml


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=8).stdout


def sample(node, gres_path, allocation_fetch=None):
    minors = gres_minors(Path(gres_path).read_text(), node)
    def allocations():
        if allocation_fetch is None:
            return parse_allocations(run(["scontrol", "show", "job", "-d", "-o"]), node, minors)
        result = []
        for job in allocation_fetch():
            indices = job["gpu_indices"]
            if not indices or any(not isinstance(i, int) or i < 0 or i >= len(minors) for i in indices):
                raise ValueError("Controller GPU indices outside local GRES mapping")
            result.append({k: v for k, v in job.items() if k != "gpu_indices"} |
                          {"minors": sorted(minors[i] for i in indices)})
        return result
    before = allocations()
    devices = parse_nvidia_xml(run(["nvidia-smi", "-q", "-x"]))
    if not devices:
        raise ValueError("No supported physical GPU counters available")
    now = int(time.time() * 1000)
    after = allocations()
    jobs = []
    for job in before:
        # Reject completions/requeues/reallocations that raced the NVIDIA query.
        if job not in after or any(m not in devices for m in job["minors"]):
            continue
        jobs.append({**job, "gpus": [devices[m] for m in job["minors"]]})
    return {"node": node, "timestamp_ms": now, "jobs": jobs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--gres", default="/etc/slurm/gres.conf")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--controller-allocations", action="store_true",
                        help="Read authenticated allocations from the controller instead of local scontrol")
    args = parser.parse_args()
    token = "" if args.dry_run and not args.controller_allocations else (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "gpu-token").read_text().strip()
    def allocation_fetch():
        base = args.url.rsplit("/api/gpu-samples", 1)[0]
        req = urllib.request.Request(base + "/api/gpu-allocations/" + quote(args.node, safe=""),
                                     headers={"X-GPU-Monitor-Token": token})
        with urllib.request.urlopen(req, timeout=8) as response:
            return json.load(response)["jobs"]
    while True:
        started = time.monotonic()
        try:
            payload = sample(args.node, args.gres, allocation_fetch if args.controller_allocations else None)
            if args.dry_run:
                print(json.dumps(payload))
            else:
                req = urllib.request.Request(args.url, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", "X-GPU-Monitor-Token": token})
                with urllib.request.urlopen(req, timeout=8) as response:
                    response.read(1024)
        except Exception as exc:
            # No payload, token or process environment in service logs.
            print(f"GPU sample unavailable: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(0.1, PERIOD_MS / 1000 - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
