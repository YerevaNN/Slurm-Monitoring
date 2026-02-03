"""Query Slurm (squeue/scontrol) and return job list for the dashboard."""
import re
import subprocess
from datetime import datetime
from typing import Any


def _run(cmd: list[str], timeout: int = 30) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _parse_ts(s: str) -> int | None:
    """Parse Slurm timestamp to Unix ms. Handles N/A, Unknown, ISO, and raw Unix seconds."""
    if not s or s in ("N/A", "Unknown", "None"):
        return None
    s = s.strip()
    if s.isdigit():
        sec = int(s)
        return sec * 1000 if sec < 1e12 else sec
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00")[:19])
        else:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _expand_nodelist(nodelist: str) -> list[str]:
    """Expand Slurm nodelist (e.g. gpu[01-08], gpu01,gpu02) to list of node names."""
    if not nodelist or nodelist in ("(null)", "N/A"):
        return []
    # Already comma-separated
    if "," in nodelist and "[" not in nodelist:
        return [n.strip() for n in nodelist.split(",")]
    # Bracketed range: gpu[01-08] -> gpu01, gpu02, ...
    m = re.match(r"^(.+)\[(\d+)-(\d+)\]$", nodelist)
    if m:
        prefix, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
        w = max(len(m.group(2)), len(m.group(3)))
        return [f"{prefix}{i:0{w}d}" for i in range(lo, hi + 1)]
    return [nodelist]


def _parse_gres(gres: str) -> int:
    """Extract GPU count from GRES string (e.g. gpu:8, gpu:h100:8). Use count after last colon for typed GRES."""
    if not gres or gres in ("(null)", "N/A"):
        return 0
    # gpu:h100:8 or gpu:8 -> take the last :N (the count)
    m = re.search(r"gpu(?::\w+)*:(\d+)", gres, re.I)
    if m:
        return int(m.group(1))
    # fallback: plain gpu:N
    m = re.search(r"gpu[:\s]*(\d+)", gres, re.I)
    return int(m.group(1)) if m else 0


def _parse_alloc_tres(alloc_tres: str) -> int:
    """Extract GPU count from AllocTRES (e.g. gres/gpu:8, gres/gpu:h100=8)."""
    if not alloc_tres or alloc_tres in ("(null)", "N/A"):
        return 0
    m = re.search(r"gres/gpu(?::\w+)?=(\d+)", alloc_tres, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"gres/gpu:(\d+)(?:,|$)", alloc_tres, re.I)
    return int(m.group(1)) if m else 0


def _fill_gpus_from_scontrol(jobs: list[dict[str, Any]]) -> None:
    """For jobs with allocations but 0 GPUs (squeue %b N/A), set GPU count from scontrol show job AllocTRES/ReqTRES."""
    need = [j["job_id"] for j in jobs if j.get("allocations") and j.get("req_gpus", 0) == 0]
    if not need:
        return
    # scontrol show job accepts only one job ID per call on many Slurm versions
    out_parts = []
    for jid in need:
        o = _run(["scontrol", "show", "job", jid])
        if o:
            out_parts.append(o)
    out = "\n\n".join(out_parts)
    if not out:
        return
    current_job_id = None
    req_tres_gpus: dict[str, int] = {}
    applied: set[str] = set()

    def apply_gpus(jid: str, g: int) -> None:
        if g == 0:
            return
        for j in jobs:
            if j["job_id"] == jid:
                j["req_gpus"] = g
                num_nodes = max(1, len(j["allocations"]))
                gpus_per_node = g // num_nodes
                for a in j["allocations"]:
                    a["gpus"] = gpus_per_node
                applied.add(jid)
                break

    for line in out.splitlines():
        line = line.strip()
        if line.startswith("JobId="):
            if current_job_id and current_job_id not in applied and req_tres_gpus.get(current_job_id):
                apply_gpus(current_job_id, req_tres_gpus[current_job_id])
            current_job_id = line.split("=", 1)[1].split()[0].rstrip(",")
        elif current_job_id and line.startswith("ReqTRES="):
            req_tres_gpus[current_job_id] = _parse_alloc_tres(line.split("=", 1)[1].strip())
        elif current_job_id and line.startswith("AllocTRES="):
            parsed = _parse_alloc_tres(line.split("=", 1)[1].strip())
            gpus = parsed if parsed else req_tres_gpus.get(current_job_id, 0)
            apply_gpus(current_job_id, gpus)
            current_job_id = None
    if current_job_id and current_job_id not in applied and req_tres_gpus.get(current_job_id):
        apply_gpus(current_job_id, req_tres_gpus[current_job_id])


def _array_task_count(job_id: str) -> int:
    """Parse job array task count from job_id (e.g. 210_1-3 -> 3, 210_1 -> 1)."""
    if not job_id or "_" not in job_id:
        return 1
    suffix = job_id.split("_", 1)[1]
    m = re.match(r"\[(\d+)-(\d+)\]", suffix)
    if m:
        return max(1, int(m.group(2)) - int(m.group(1)) + 1)
    m = re.match(r"(\d+)-(\d+)$", suffix)
    if m:
        return max(1, int(m.group(2)) - int(m.group(1)) + 1)
    return 1


def _parse_time_limit(s: str) -> int | None:
    """Parse Slurm time limit (e.g. 1-12:30:00, 12:30:00, 30:00) to duration in ms. Returns None if UNLIMITED or unparseable."""
    if not s or not s.strip() or s.strip().upper() in ("UNLIMITED", "N/A", "(null)"):
        return None
    s = s.strip()
    days, hours, mins, secs = 0, 0, 0, 0
    if "-" in s:
        day_part, time_part = s.split("-", 1)
        days = int(day_part.strip()) if day_part.strip().isdigit() else 0
        s = time_part.strip()
    parts = s.split(":")
    if len(parts) == 3:
        hours, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 2:
        mins, secs = int(parts[0]), int(parts[1])
    elif len(parts) == 1 and parts[0].isdigit():
        mins = int(parts[0])
    else:
        return None
    total_sec = days * 86400 + hours * 3600 + mins * 60 + secs
    return total_sec * 1000 if total_sec >= 0 else None


def fetch_jobs() -> list[dict[str, Any]]:
    """
    Run squeue and return list of job dicts with:
    job_id, user, job_name, state, priority, submit_time, start_time, end_time (ms),
    allocations [{node, gpus, cpus}], req_gpus, req_cpus (for pending).
    """
    # Delimiter | to avoid splitting on spaces in job names.
    # %V submit, %S start, %e end, %m min memory (MB), %l time limit.
    fmt = "%i|%u|%j|%T|%S|%e|%V|%N|%C|%b|%m|%Q|%l"
    out = _run(["squeue", "-h", "-a", "-o", fmt])
    jobs = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 13)
        if len(parts) < 12:
            continue
        job_id, user, job_name, state, start_s, end_s, submit_s, nodelist, cpus_s, gres, mem_s, priority_s = (
            parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip(),
            parts[4].strip(), parts[5].strip(), parts[6].strip(), parts[7].strip(),
            parts[8].strip(), parts[9].strip(), parts[10].strip(), parts[11].strip()
        )
        time_limit_s = parts[12].strip() if len(parts) > 12 else ""
        try:
            job_id_int = int(job_id.split("_")[0])
        except ValueError:
            continue
        priority = int(priority_s) if priority_s.isdigit() else 0
        submit_time = _parse_ts(submit_s)
        start_time = _parse_ts(start_s)
        end_time = _parse_ts(end_s)
        try:
            cpus = int(cpus_s) if cpus_s and cpus_s != "N/A" else 0
        except ValueError:
            cpus = 0
        try:
            req_mem = int(mem_s) if mem_s and mem_s != "N/A" else 0
        except ValueError:
            req_mem = 0
        gpus = _parse_gres(gres)
        nodes = _expand_nodelist(nodelist)
        array_tasks = _array_task_count(job_id)

        # Allocations: per-node GPU/CPU (even split if multi-node)
        num_nodes = max(1, len(nodes))
        gpus_per_node = gpus // num_nodes
        cpus_per_node = cpus // num_nodes
        if nodes:
            allocations = [{"node": n, "gpus": gpus_per_node, "cpus": cpus_per_node} for n in nodes]
        else:
            allocations = []

        time_limit_ms = _parse_time_limit(time_limit_s)

        # For display/sizing: pending arrays contribute (per-task × task count) GPUs/CPUs
        req_gpus_display = gpus * array_tasks
        req_cpus_display = cpus * array_tasks

        job = {
            "job_id": job_id,
            "job_id_int": job_id_int,
            "user": user,
            "job_name": job_name or job_id,
            "state": state,
            "priority": priority,
            "submit_time": submit_time,
            "start_time": start_time,
            "end_time": end_time,
            "time_limit_ms": time_limit_ms,
            "allocations": allocations,
            "req_gpus": req_gpus_display,
            "req_cpus": req_cpus_display,
            "req_mem": req_mem,
            "array_task_count": array_tasks,
        }
        jobs.append(job)
    _fill_gpus_from_scontrol(jobs)
    return jobs


def fetch_nodes() -> list[str]:
    """Return sorted list of unique node names (short form, from sinfo)."""
    out = _run(["sinfo", "-h", "-N", "-o", "%n"])
    if not out.strip():
        return []
    seen: set[str] = set()
    for line in out.splitlines():
        node = line.strip().split(".")[0].strip()
        if node:
            seen.add(node)
    return sorted(seen)


def fetch_node_states() -> tuple[dict[str, str], dict[str, str]]:
    """Return (node_name -> state, node_name -> reason). State can be compound e.g. DOWN+NOT_RESPONDING."""
    out = _run(["sinfo", "-h", "-N", "-o", "%n|%t|%E"])
    if not out.strip():
        out = _run(["sinfo", "-h", "-N", "-o", "%n|%t"])
    states: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) < 2:
            continue
        node = parts[0].strip()
        state = parts[1].strip().upper()
        reason = parts[2].strip() if len(parts) > 2 else ""
        if node:
            states[node] = state
            if reason:
                reasons[node] = reason
            short = node.split(".")[0]
            if short != node:
                states[short] = state
                if reason:
                    reasons[short] = reason
    return states, reasons


def fetch_node_capacities() -> tuple[int, int]:
    """Return (gpus_per_node, cpus_per_node) from first node in sinfo. (0, 0) if unavailable."""
    out = _run(["sinfo", "-h", "-N", "-o", "%n|%c|%G"])
    if not out.strip():
        return 0, 0
    line = out.splitlines()[0]
    parts = line.strip().split("|", 2)
    if len(parts) < 3:
        return 0, 0
    try:
        cpus = int(parts[1].strip()) if parts[1].strip() else 0
    except ValueError:
        cpus = 0
    gpus = _parse_gres(parts[2].strip()) if len(parts) > 2 else 0
    return gpus, cpus


def update_job_settings(job_id: str, priority: int | None = None, num_cpus: int | None = None, memory_mb: int | None = None) -> tuple[bool, str]:
    """Update pending job settings via scontrol. Returns (success, message)."""
    args = ["scontrol", "update", "jobid=" + str(job_id)]
    if priority is not None:
        try:
            p = int(priority)
            if p < 0:
                return False, "Priority must be >= 0"
            args.append("Priority=" + str(p))
        except (TypeError, ValueError):
            return False, "Invalid priority"
    if num_cpus is not None:
        try:
            n = int(num_cpus)
            if n < 0:
                return False, "NumCPUs must be >= 0"
            args.extend(["CpusPerTask=" + str(n), "MinCPUsNode=" + str(n), "NumCPUs=" + str(n)])
        except (TypeError, ValueError):
            return False, "Invalid NumCPUs"
    if memory_mb is not None:
        try:
            m = int(memory_mb)
            if m < 0:
                return False, "Memory must be >= 0 MB"
            args.append("MinMemoryNode=" + str(m))
        except (TypeError, ValueError):
            return False, "Invalid memory (MB)"
    if len(args) == 3:
        return False, "No settings to update"
    r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "Failed").strip()
    return True, ""
