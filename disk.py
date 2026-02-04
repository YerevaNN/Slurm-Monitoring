"""Collect disk usage via df -h and return samples for storage."""
import re
import subprocess
from typing import Any


def _run(cmd: list[str], timeout: int = 10) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _size_to_gb(s: str) -> float:
    """Parse human size (e.g. 2.0T, 200G, 50M) to GB."""
    if not s or not s.strip():
        return 0.0
    s = s.strip().upper()
    m = re.match(r"^([\d.]+)\s*([KMGT])?$", s)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    scale = {"B": 1 / (1024**3), "K": 1 / (1024**2), "M": 1 / 1024, "G": 1, "T": 1024}
    return val * scale.get(unit, 1)


def fetch_disk_usage(min_size_gb: float) -> list[dict[str, Any]]:
    """
    Run df -h, parse output, filter to disks with total size >= min_size_gb.
    Returns list of { mount, total_gb, used_gb, avail_gb, use_pct }.
    Uses df --output for predictable columns when available.
    """
    # Prefer fixed columns (GNU df)
    out = _run(["df", "-h", "--output=source,size,used,avail,pcent,target"])
    if out:
        return _parse_df_output(out, min_size_gb, use_output=True)
    out = _run(["df", "-h"])
    if not out:
        return []
    return _parse_df_output(out, min_size_gb, use_output=False)


def _parse_df_output(out: str, min_size_gb: float, use_output: bool) -> list[dict[str, Any]]:
    lines = out.splitlines()
    if len(lines) < 2:
        return []
    result = []
    if use_output:
        # source, size, used, avail, pcent, target (skip header)
        for line in lines[1:]:
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            mount = parts[5].strip()
            if mount.startswith("/dev/loop") or mount == "tmpfs" or mount == "udev":
                continue
            total_gb = _size_to_gb(parts[1])
            if total_gb < min_size_gb:
                continue
            used_gb = _size_to_gb(parts[2])
            avail_gb = _size_to_gb(parts[3])
            use_pct_s = parts[4].rstrip("%")
            try:
                use_pct = float(use_pct_s)
            except ValueError:
                use_pct = (used_gb / total_gb * 100) if total_gb > 0 else 0
            result.append({
                "mount": mount,
                "total_gb": round(total_gb, 2),
                "used_gb": round(used_gb, 2),
                "avail_gb": round(avail_gb, 2),
                "use_pct": round(use_pct, 1),
            })
        return result
    # Fallback: parse default df -h (Filesystem Size Used Avail Use% Mounted on)
    header = lines[0].split()
    idx_mount = len(header) - 1
    for line in lines[1:]:
        parts = line.split(None, idx_mount)
        if len(parts) <= idx_mount:
            continue
        mount = parts[idx_mount].strip()
        if mount.startswith("/dev/loop") or mount == "tmpfs" or mount == "udev":
            continue
        if len(parts) < 5:
            continue
        total_gb = _size_to_gb(parts[1])
        if total_gb < min_size_gb:
            continue
        used_gb = _size_to_gb(parts[2])
        avail_gb = _size_to_gb(parts[3])
        use_pct_s = parts[4].rstrip("%")
        try:
            use_pct = float(use_pct_s)
        except ValueError:
            use_pct = (used_gb / total_gb * 100) if total_gb > 0 else 0
        result.append({
            "mount": mount,
            "total_gb": round(total_gb, 2),
            "used_gb": round(used_gb, 2),
            "avail_gb": round(avail_gb, 2),
            "use_pct": round(use_pct, 1),
        })
    return result
