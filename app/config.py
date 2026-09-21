"""Start-up configuration read from environment variables.

Everything else (bot token, channels, intervals...) is configured in the web
interface and stored in the SQLite database inside DATA_DIR.
"""
import os
from pathlib import Path

PORT = int(os.environ.get("PORT", "25599"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "notifier.db"

# Optional password protection for the web interface (HTTP basic auth).
# The /websub/ callback and /health endpoints are always left open.
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
