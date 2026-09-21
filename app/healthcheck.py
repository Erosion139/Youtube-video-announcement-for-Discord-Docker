"""Used by the Docker HEALTHCHECK: exits 0 when the web interface answers."""
import os
import sys
import urllib.request

port = os.environ.get("PORT", "25599")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
