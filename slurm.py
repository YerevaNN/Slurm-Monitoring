"""Query Slurm (squeue/scontrol) and return job list for the dashboard."""
import re
import shlex
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
    """Expand Slurm nodelist to a flat list of node names using scontrol show hostnames."""
    if not nodelist or nodelist in ("(null)", "N/A"):
        return []
    # Fast path: plain single node (no brackets or commas)
    if "[" not in nodelist and "," not in nodelist:
        return [nodelist]
    # Delegate to scontrol which handles all Slurm nodelist formats
    # (ranges, non-contiguous indices, mixed formats like gpu[01,06-08])
    out = _run(["scontrol", "show", "hostnames", nodelist])
    if out:
        return [n.strip() for n in out.splitlines() if n.strip()]
    # Fallback: plain comma-separated (no brackets)
    if "," in nodelist and "[" not in nodelist:
        return [n.strip() for n in nodelist.split(",")]
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


def _parse_cpuonly(gres: str) -> int:
    """Extract cpuonly GRES count (e.g. gres/cpuonly:96)."""
    if not gres or gres in ("(null)", "N/A"):
        return 0
    m = re.search(r"cpuonly:(\d+)", gres, re.I)
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
    """For jobs with 0 GPUs (squeue %b N/A), set GPU count from scontrol show job ReqTRES/AllocTRES."""
    need = [j["job_id"] for j in jobs if j.get("req_gpus", 0) == 0]
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
                # Update req_gpus (multiply by array task count for display)
                array_tasks = j.get("array_task_count", 1)
                j["req_gpus"] = g * array_tasks
                # Update allocations if present
                if j.get("allocations"):
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
    """Parse task count from a compact Slurm array ID.

    Slurm appends ``%N`` inside the brackets for throttled arrays, for example
    ``210_[141-261%8]``.  The throttle limits concurrent tasks; it does not
    reduce the number of queued tasks represented by the row.
    """
    if not job_id or "_" not in job_id:
        return 1
    suffix = job_id.split("_", 1)[1]
    m = re.match(r"\[(\d+)-(\d+)(?:%\d+)?\]", suffix)
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
    # %V submit, %S start, %e end, %m min memory (MB), %l time limit, %P partition, %r pending reason.
    # %r is last because pending reasons are free text and get the split remainder.
    fmt = "%i|%u|%j|%T|%S|%e|%V|%N|%C|%b|%m|%Q|%l|%P|%r"
    out = _run(["squeue", "-h", "-a", "-o", fmt])
    jobs = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 14)
        if len(parts) < 12:
            continue
        job_id, user, job_name, state, start_s, end_s, submit_s, nodelist, cpus_s, gres, mem_s, priority_s = (
            parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip(),
            parts[4].strip(), parts[5].strip(), parts[6].strip(), parts[7].strip(),
            parts[8].strip(), parts[9].strip(), parts[10].strip(), parts[11].strip()
        )
        time_limit_s = parts[12].strip() if len(parts) > 12 else ""
        partition = parts[13].strip() if len(parts) > 13 else ""
        pending_reason = parts[14].strip() if len(parts) > 14 else ""
        try:
            job_id_int = int(job_id.split("_")[0])
        except ValueError:
            continue
        priority = int(priority_s) if priority_s.isdigit() else 0
        submit_time = _parse_ts(submit_s)
        # For PENDING jobs, squeue %S is Slurm's scheduling estimate, not an actual start.
        # Storing it as start_time_ms would corrupt queue calculations once the job
        # disappears (cancelled/failed) — the estimated future time stays in the DB while
        # end_time is set to the past, making the job appear perpetually in-queue.
        start_time = None if state == "PENDING" else _parse_ts(start_s)
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
        cpuonly = _parse_cpuonly(gres)
        nodes = _expand_nodelist(nodelist)
        array_tasks = _array_task_count(job_id)

        # Allocations: %b (gres) is per-node, %C (cpus) is total across all nodes.
        num_nodes = max(1, len(nodes))
        gpus_per_node = gpus          # %b is already per-node
        cpus_per_node = cpus // num_nodes
        if nodes:
            allocations = [{"node": n, "gpus": gpus_per_node, "cpus": cpus_per_node} for n in nodes]
        else:
            allocations = []

        time_limit_ms = _parse_time_limit(time_limit_s)

        # For display/sizing: total GPUs = per-node × num_nodes × array tasks
        req_gpus_display = gpus * num_nodes * array_tasks
        req_cpus_display = cpus * array_tasks
        req_cpuonly_display = cpuonly * num_nodes * array_tasks

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
            "req_cpuonly": req_cpuonly_display,
            "req_mem": req_mem,
            "array_task_count": array_tasks,
            "pending_reason": pending_reason,
            "partition": partition,
        }
        jobs.append(job)
    _fill_gpus_from_scontrol(jobs)
    return jobs


def fetch_nodes() -> list[str]:
    """Return unique node names, with numbered GPU nodes first in natural order."""
    out = _run(["sinfo", "-h", "-N", "-o", "%N"])
    if not out.strip():
        return []
    seen: set[str] = set()
    for line in out.splitlines():
        node = line.strip().split(".")[0].strip()
        if node:
            seen.add(node)
    return sorted(seen, key=_node_sort_key)


def _node_sort_key(node: str) -> tuple:
    """Sort gpu01..gpuNN numerically, followed by other accelerator nodes."""
    match = re.fullmatch(r"gpu(\d+)", node.lower())
    if match:
        return (0, int(match.group(1)), node.lower())
    return (1, 0, node.lower())


def fetch_node_states() -> tuple[dict[str, str], dict[str, str]]:
    """Return (node_name -> state, node_name -> reason). State can be compound e.g. DOWN+NOT_RESPONDING."""
    out = _run(["sinfo", "-h", "-N", "-o", "%N|%t|%E"])
    if not out.strip():
        out = _run(["sinfo", "-h", "-N", "-o", "%N|%t"])
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


def fetch_node_capacities() -> dict[str, dict[str, int]]:
    """Return per-node GPU and CPU capacities discovered from sinfo.

    ``sinfo -N`` can emit the same node once per partition, so retain the
    largest capacity seen for each normalized short node name.
    """
    out = _run(["sinfo", "-h", "-N", "-o", "%N|%c|%G"])
    if not out.strip():
        return {}
    capacities: dict[str, dict[str, int]] = {}
    for line in out.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) < 3:
            continue
        node = parts[0].strip().split(".")[0]
        if not node:
            continue
        try:
            cpus = int(parts[1].strip()) if parts[1].strip() else 0
        except ValueError:
            cpus = 0
        gpus = _parse_gres(parts[2].strip())
        current = capacities.setdefault(node, {"gpus": 0, "cpus": 0})
        current["gpus"] = max(current["gpus"], gpus)
        current["cpus"] = max(current["cpus"], cpus)
    return capacities


def fetch_partitions() -> dict[str, dict[str, int]]:
    """
    Return partition name -> {priority_tier, job_factor} from scontrol show partition.

    PriorityTier is what actually orders the scheduler's queue: Slurm sorts pending
    jobs by tier first and only then by job priority, so a low-priority job in a
    high-tier partition is still considered before a high-priority job in a low tier.
    """
    out = _run(["scontrol", "show", "partition"])
    partitions: dict[str, dict[str, int]] = {}
    current: str | None = None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"PartitionName=(\S+)", line)
        if m:
            current = m.group(1)
            partitions[current] = {"priority_tier": 1, "job_factor": 1}
            continue
        if not current:
            continue
        m = re.search(r"PriorityTier=(\d+)", line)
        if m:
            partitions[current]["priority_tier"] = int(m.group(1))
        m = re.search(r"PriorityJobFactor=(\d+)", line)
        if m:
            partitions[current]["job_factor"] = int(m.group(1))
    return partitions


def _parse_scontrol_time_field(val: str) -> int | None:
    """Parse StartTime/EndTime value; Unknown/N/A means unset."""
    v = val.strip()
    if not v or v in ("Unknown", "N/A", "None"):
        return None
    return _parse_ts(v)


def get_job_final_info(job_id: str) -> tuple[str | None, int | None, int | None]:
    """
    Get state and times from scontrol show job (works while job is still active).
    Returns (state, end_time_ms, start_time_ms) or (None, None, None) if not found/purged.
    """
    out = _run(["scontrol", "show", "job", str(job_id)])
    if not out:
        return None, None, None

    state = None
    end_time_ms = None
    start_time_ms = None

    m = re.search(r"JobState=(\S+)", out)
    if m:
        state = m.group(1).strip().upper().rstrip(",")

    m = re.search(r"EndTime=(\S+)", out)
    if m:
        end_time_ms = _parse_scontrol_time_field(m.group(1))

    m = re.search(r"StartTime=(\S+)", out)
    if m:
        start_time_ms = _parse_scontrol_time_field(m.group(1))

    return state, end_time_ms, start_time_ms


def build_job_settings_command(job_id: str, priority: int | None = None, num_cpus: int | None = None, memory_mb: int | None = None) -> tuple[bool, str]:
    """Build, but never execute, a copyable scontrol command."""
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
    return True, shlex.join(args)
