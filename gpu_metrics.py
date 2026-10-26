"""GPU sampling helpers. No Python packages or privileged GPU access required."""
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime

METRICS = ("gpu", "vram", "memory", "used_mib")
PERIOD_MS = 15_000
STALE_MS = 45_000


def number(value):
    """Unsupported NVIDIA counters remain unknown, never zero."""
    try:
        result = float(str(value).split()[0])
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, IndexError):
        return None


def parse_nvidia_xml(text):
    result = {}
    for device in ET.fromstring(text).findall("gpu"):
        # Whole-device utilization cannot safely represent MIG instances.
        if device.findtext("mig_mode/current_mig") == "Enabled":
            continue
        minor = number(device.findtext("minor_number"))
        uuid = device.findtext("uuid")
        if minor is None or not uuid:
            continue
        used = number(device.findtext("fb_memory_usage/used"))
        total = number(device.findtext("fb_memory_usage/total"))
        result[int(minor)] = {
            "uuid": uuid, "minor": int(minor),
            "gpu": number(device.findtext("utilization/gpu_util")),
            "memory": number(device.findtext("utilization/memory_util")),
            "vram": 100 * used / total if used is not None and total else None,
            "used_mib": used, "total_mib": total,
        }
    return result


def expand_indices(text):
    result = []
    for part in text.split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ValueError("Unsupported GPU index list")
        low, high = int(match[1]), int(match[2] or match[1])
        if high < low or high > 4096:
            raise ValueError("Invalid GPU index range")
        result.extend(range(low, high + 1))
    return result


def node_matches(expression, node):
    if expression == node:
        return True
    # Standard Slurm numeric host ranges; never approximate using shell globs.
    match = re.fullmatch(r"([^\[\],]*)\[([\d,-]+)\]([^\[\],]*)", expression)
    if not match:
        return False
    for item in match[2].split(","):
        pieces = item.split("-")
        low, high = int(pieces[0]), int(pieces[-1])
        width = len(pieces[0]) if pieces[0].startswith("0") else 0
        if high - low > 4096:
            raise ValueError("Node range too large")
        if any(node == match[1] + str(i).zfill(width) + match[3] for i in range(low, high + 1)):
            return True
    return False


def gres_minors(text, node):
    """GRES IDX uses configured File ordering, not CUDA-visible device numbers."""
    result = []
    for line in text.splitlines():
        fields = dict(re.findall(r"(\w+)=([^\s#]+)", line.split("#", 1)[0]))
        if fields.get("Name") != "gpu" or not node_matches(fields.get("NodeName", node), node):
            continue
        path = fields.get("File", "")
        match = re.fullmatch(r"/dev/nvidia(?:\[([\d,-]+)\]|(\d+))", path)
        if not match:
            raise ValueError("Unsupported GRES File mapping; refusing GPU attribution")
        result.extend(expand_indices(match[1] or match[2]))
    if not result or len(set(result)) != len(result):
        raise ValueError("Missing or ambiguous GRES File mapping")
    return result


def timestamp(text):
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def parse_allocations(text, node, minors):
    """Return exclusive whole-GPU allocations, including canonical array task IDs."""
    jobs = []
    for line in text.splitlines():
        fields = dict(re.findall(r"\b(JobId|ArrayJobId|ArrayTaskId|JobState|SubmitTime|StartTime|Restarts)=([^\s]+)", line))
        if fields.get("JobState") != "RUNNING":
            continue
        indices = []
        for segment in re.finditer(r"\bNodes=(\S+)\s+CPU_IDs=\S+\s+Mem=\S+\s+GRES=(\S+)", line):
            if not node_matches(segment[1], node):
                continue
            for gpu in re.finditer(r"(?:^|,)gpu(?::[^,()]+)?\(IDX:([\d,-]+)\)", segment[2]):
                indices.extend(expand_indices(gpu[1]))
        if not indices or any(i >= len(minors) for i in indices):
            continue
        job_id = fields.get("JobId", "")
        if fields.get("ArrayJobId", "").isdigit() and fields.get("ArrayTaskId", "").isdigit():
            job_id = fields["ArrayJobId"] + "_" + fields["ArrayTaskId"]
        submit, start = timestamp(fields.get("SubmitTime")), timestamp(fields.get("StartTime"))
        if not submit or not start or not re.fullmatch(r"\d+(?:_\d+)?", job_id):
            continue
        jobs.append({"job_id": job_id, "submit_time_ms": submit, "start_time_ms": start,
                     "restarts": int(fields.get("Restarts", 0)),
                     "minors": sorted({minors[i] for i in indices})})
    owners = {}
    for job in jobs:
        for minor in job["minors"]:
            owners[minor] = owners.get(minor, 0) + 1
    # Shared/MPS or ambiguous ownership must never double-credit utilization.
    return [job for job in jobs if all(owners[m] == 1 for m in job["minors"])]


def update_moments(stats, value):
    if value is None:
        return
    n = stats.get("n", 0) + 1
    old = stats.get("mean", 0)
    mean = old + (value - old) / n
    stats.update(n=n, mean=mean, m2=stats.get("m2", 0) + (value - old) * (value - mean),
                 peak=max(stats.get("peak", value), value))


def combine_moments(items):
    n, mean, m2, peak = 0, 0, 0, None
    for item in items:
        count = item.get("n", 0)
        if not count:
            continue
        total = n + count
        delta = item["mean"] - mean
        m2 += item["m2"] + delta * delta * n * count / total
        mean += delta * count / total
        n = total
        peak = max(peak if peak is not None else item["peak"], item["peak"])
    return {"n": n, "mean": mean if n else None,
            "std": math.sqrt(max(0, m2 / n)) if n else None, "peak": peak}
