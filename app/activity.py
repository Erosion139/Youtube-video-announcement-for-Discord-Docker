"""Activity feed shown in the web interface (also mirrored to the container log)."""
import logging

log = logging.getLogger("activity")


class Activity:
    def __init__(self, db):
        self.db = db

    def _add(self, level: str, log_level: int, message: str):
        log.log(log_level, message)
        try:
            self.db.add_activity(level, message)
        except Exception:  # never let logging break the caller
            log.exception("Could not store activity entry")

    def info(self, message: str):
        self._add("info", logging.INFO, message)

    def success(self, message: str):
        self._add("success", logging.INFO, message)

    def warn(self, message: str):
        self._add("warn", logging.WARNING, message)

    def error(self, message: str):
        self._add("error", logging.ERROR, message)
