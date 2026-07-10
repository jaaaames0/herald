"""Herald env loader. Sources ~/.config/herald/.env into the current process.

Used by collect.py (Twitter auth) and herald_digest.py (OpenRouter key + model
choices). Whitelist-based so we never accidentally pull in unrelated env vars
from the .env file.

Each var is set via os.environ.setdefault — if it's already set in the parent
environment (e.g. systemd EnvironmentFile=, an interactive `export`, or a
caller that loaded it itself), we leave it alone.
"""
import os
from pathlib import Path

HERALD_ENV_PATH = Path(os.environ.get("HERALD_ENV_PATH", "~/.config/herald/.env")).expanduser()

# Keys we will load from the .env file. Keep this list narrow.
LOAD_KEYS = (
    # Twitter auth (used by collect.py → bird)
    "AUTH_TOKEN",
    "CT0",
    # OpenRouter (used by herald_digest.py)
    "OPENROUTER_API_KEY",
    "HERALD_MODEL",
    "HERALD_EMBEDDING_MODEL",
    "HERALD_VISION_MODEL",
    # Operational knobs
    "HERALD_BIRD_BIN",
    "HERALD_DB_PATH",
    "HERALD_ENV_PATH",
    "HERALD_MAX_CANDIDATES",
    "HERALD_MAX_ITEMS_PER_DIGEST",
    "HERALD_STAGE1_MAX_TOKENS",
    "HERALD_STAGE2_MAX_TOKENS",
    "HERALD_STAGE1_MAX_REASONING_TOKENS",
    "HERALD_STAGE2_MAX_REASONING_TOKENS",
    # Phase 3.6 context features
    "HERALD_IMAGES_PER_STORY",
    "HERALD_MAX_MEDIA_FETCHES_PER_CYCLE",
    "HERALD_VISION_TIMEOUT",
    # Phase 3.6.2 link context
    "HERALD_LINK_DROP_QUERY_KEYS",
    "HERALD_LINK_MAX_BYTES_FETCH",
    "HERALD_LINK_MAX_CHARS_RENDER",
    "HERALD_LINK_MAX_CHARS_STORE",
    "HERALD_LINK_MIN_TEXT_CHARS",
    "HERALD_LINK_TIMEOUT",
    "HERALD_LINK_USER_AGENT",
    "HERALD_LINKS_PER_STORY",
    "HERALD_MAX_LINK_FETCHES_PER_CYCLE",
)


def load_herald_env(path=None):
    """Source Herald's .env file into os.environ (setdefault for whitelist keys).

    Returns the path that was loaded, or None if the file was missing.
    Never raises — missing or unreadable .env is non-fatal; callers decide
    whether the missing var is fatal (e.g. bird surfaces missing-creds error,
    herald_digest raises on missing OPENROUTER_API_KEY).
    """
    env_path = Path(path) if path else HERALD_ENV_PATH
    if not env_path.exists():
        return None
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k in LOAD_KEYS:
                    os.environ.setdefault(k, v)
    except OSError:
        return None
    return env_path