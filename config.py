"""App config. Nodes discovered from sinfo; GPUS/CPUS_PER_NODE are fallbacks when sinfo has no capacity."""
import os

PORT = int(os.environ.get("SLURM_DASHBOARD_PORT", "19526"))
GPUS_PER_NODE = 8
CPUS_PER_NODE = 224
REFRESH_INTERVALS = [5, 10, 30, 60]  # seconds
DEFAULT_REFRESH = 10

# History settings
HISTORY_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "slurm_history.db")
HISTORY_RETENTION_DAYS = None  # None = keep forever (no cleanup)

# Disk usage tracking
DISK_MIN_SIZE_GB = 200
DISK_RETENTION_DAYS = 14
