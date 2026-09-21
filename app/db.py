"""SQLite storage for settings, watched channels, seen videos and the activity log."""
import secrets
import sqlite3
import time
from pathlib import Path

DEFAULT_SETTINGS = {
    "discord_token": "",
    "discord_channel_id": "",
    "default_message": "",
    "poll_interval": "300",
    "max_age_hours": "48",
    "youtube_api_key": "",
    "public_url": "",
    "websub_secret": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    yt_channel_id TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_videos (
    video_id      TEXT PRIMARY KEY,
    yt_channel_id TEXT NOT NULL,
    seen_at       REAL NOT NULL,
    posted        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_seen_channel ON seen_videos (yt_channel_id);
CREATE TABLE IF NOT EXISTS activity (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);
"""

# Extra columns on `channels`. Missing ones are added automatically on start-up,
# so new options can be introduced without breaking existing databases.
CHANNEL_COLUMNS = {
    "handle": "TEXT NOT NULL DEFAULT ''",
    "thumbnail": "TEXT NOT NULL DEFAULT ''",
    "enabled": "INTEGER NOT NULL DEFAULT 1",
    "message": "TEXT NOT NULL DEFAULT ''",
    "discord_channel_id": "TEXT NOT NULL DEFAULT ''",
    "include_shorts": "INTEGER NOT NULL DEFAULT 1",
    "watch_since": "REAL NOT NULL DEFAULT 0",
    "seeded": "INTEGER NOT NULL DEFAULT 0",
    "last_checked": "REAL NOT NULL DEFAULT 0",
    "last_error": "TEXT NOT NULL DEFAULT ''",
    "last_video_id": "TEXT NOT NULL DEFAULT ''",
    "last_video_title": "TEXT NOT NULL DEFAULT ''",
    "last_video_published": "REAL NOT NULL DEFAULT 0",
    "last_posted_at": "REAL NOT NULL DEFAULT 0",
    "websub_expires": "REAL NOT NULL DEFAULT 0",
    "websub_requested": "REAL NOT NULL DEFAULT 0",
}

UPDATABLE = {"name"} | set(CHANNEL_COLUMNS)


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._init_settings()
        self._activity_writes = 0

    def close(self):
        self.conn.close()

    def _migrate(self):
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(channels)")}
        for column, ddl in CHANNEL_COLUMNS.items():
            if column not in existing:
                self.conn.execute(f"ALTER TABLE channels ADD COLUMN {column} {ddl}")

    def _init_settings(self):
        for key, value in DEFAULT_SETTINGS.items():
            self.conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        if not self.get_setting("websub_secret"):
            self.set_settings({"websub_secret": secrets.token_hex(24)})

    # ---- settings -------------------------------------------------------
    def get_setting(self, key: str) -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

    def get_settings(self) -> dict:
        values = dict(DEFAULT_SETTINGS)
        for row in self.conn.execute("SELECT key, value FROM settings"):
            values[row["key"]] = row["value"]
        return values

    def set_settings(self, values: dict):
        for key, value in values.items():
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    # ---- channels -------------------------------------------------------
    def list_channels(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM channels ORDER BY name COLLATE NOCASE, id")
        return [dict(r) for r in rows]

    def get_channel(self, channel_pk: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM channels WHERE id = ?", (channel_pk,)).fetchone()
        return dict(row) if row else None

    def get_channel_by_yt(self, yt_channel_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM channels WHERE yt_channel_id = ?", (yt_channel_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_channel(self, yt_channel_id: str, name: str, handle: str = "", thumbnail: str = "") -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO channels (yt_channel_id, name, handle, thumbnail, created_at, watch_since) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (yt_channel_id, name, handle, thumbnail, now, now),
        )
        return cur.lastrowid

    def update_channel(self, channel_pk: int, **fields):
        fields = {k: v for k, v in fields.items() if k in UPDATABLE}
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE channels SET {assignments} WHERE id = ?", (*fields.values(), channel_pk)
        )

    def reset_websub(self):
        self.conn.execute("UPDATE channels SET websub_expires = 0, websub_requested = 0")

    def delete_channel(self, channel_pk: int):
        ch = self.get_channel(channel_pk)
        if not ch:
            return
        self.conn.execute("DELETE FROM seen_videos WHERE yt_channel_id = ?", (ch["yt_channel_id"],))
        self.conn.execute("DELETE FROM channels WHERE id = ?", (channel_pk,))

    # ---- seen videos ----------------------------------------------------
    def is_seen(self, video_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM seen_videos WHERE video_id = ?", (video_id,)
        ).fetchone() is not None

    def mark_seen(self, video_id: str, yt_channel_id: str, posted: bool):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_videos (video_id, yt_channel_id, seen_at, posted) "
            "VALUES (?, ?, ?, ?)",
            (video_id, yt_channel_id, time.time(), int(posted)),
        )

    # ---- activity log ---------------------------------------------------
    def add_activity(self, level: str, message: str):
        self.conn.execute(
            "INSERT INTO activity (ts, level, message) VALUES (?, ?, ?)",
            (time.time(), level, message[:500]),
        )
        self._activity_writes += 1
        if self._activity_writes % 50 == 0:
            self.conn.execute(
                "DELETE FROM activity WHERE id <= (SELECT MAX(id) FROM activity) - 500"
            )

    def recent_activity(self, limit: int = 60) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, level, message FROM activity ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]
