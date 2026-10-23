"""App config. Nodes discovered from sinfo; GPUS/CPUS_PER_NODE are fallbacks when sinfo has no capacity."""
import os

PORT = int(os.environ.get("SLURM_DASHBOARD_PORT", "19526"))
HOST = os.environ.get("SLURM_DASHBOARD_HOST", "0.0.0.0")
ADMIN_TOKEN = os.environ.get("SLURM_DASHBOARD_ADMIN_TOKEN", "")
DISABLE_RECORDER = os.environ.get("SLURM_DASHBOARD_DISABLE_RECORDER", "").lower() in (
    "1", "true", "yes",
)
GPUS_PER_NODE = 8
CPUS_PER_NODE = 224
REFRESH_INTERVALS = [5, 10, 30, 60]  # seconds
DEFAULT_REFRESH = 10

# History settings
HISTORY_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "slurm_history.db")
HISTORY_RETENTION_DAYS = None  # None = keep forever (no cleanup)
JOB_NAME_PREFIX_LEN = 35
DURATION_STATS_TTL_SEC = 3600  # 1 hour
PARTITIONS_TTL_SEC = 300  # partition tiers are static cluster config

# Color palette for research units (12 distinct colors)
PROJECT_PALETTE = [
    '#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6',
    '#ec4899', '#06b6d4', '#84cc16', '#f97316', '#6366f1',
    '#14b8a6', '#a855f7',
]

# Disk usage tracking
DISK_MIN_SIZE_GB = 200
DISK_RETENTION_DAYS = 14
