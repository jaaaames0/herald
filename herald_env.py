"""Optional loader for an explicitly selected private environment file.

Production gets its environment from systemd EnvironmentFile=. For manual runs,
set HERALD_ENV_PATH (or keep a gitignored `.env` next to the code). Only the
keys below are loaded, and values already in the environment win.
"""
import os
from pathlib import Path

LOAD_KEYS = (
    "AUTH_TOKEN", "CT0", "NANOGPT_API_KEY",
    "HERALD_BIRD_BIN", "HERALD_DB_PATH", "HERALD_TZ", "HERALD_PAGE_SIZE", "HERALD_CHAIN_READS",
    "HERALD_PLAN_MODEL", "HERALD_CHECK_MODEL", "HERALD_WRITE_MODEL",
    "HERALD_VISION_MODEL", "HERALD_VISION_MAX_PER_RUN", "HERALD_LLM_URL",
)


def load(path=None):
    path = Path(path or os.environ.get("HERALD_ENV_PATH") or Path(__file__).resolve().parent / ".env")
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        if key in LOAD_KEYS:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))
    return True
