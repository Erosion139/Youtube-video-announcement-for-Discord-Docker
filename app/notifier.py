"""Detect new uploads (by polling feeds or from WebSub pushes) and post them to Discord."""
import asyncio
import logging
import random
import re
import time

from . import youtube
from .discord_bot import describe_error
from .youtube import TransientError, Video, YouTubeError

log = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{(channel|title|url)\}")
DISCORD_LIMIT = 2000

# YouTube's feed endpoint fails intermittently for channels that are perfectly
# fine, so a channel is only reported as broken after this many failures in a row.
FAIL_BEFORE_REPORT = 3


def jitter(seconds: float, spread: float = 0.25) -> float:
    """Vary a delay a little so requests don't fall into a fixed rhythm."""
    if seconds <= 0:
        return 0.0
    return seconds * random.uniform(1 - spread, 1 + spread)


def build_message(template: str, channel: str, title: str, url: str) -> str:
    """Message text + video link. The link goes on its own line so Discord shows the preview.

    Templates may use {channel}, {title} and {url}. Without {url}, the link is appended.
    """
    template = (template or "").strip()
    if not template:
        return url
    values = {"channel": channel or "", "title": title or "", "url": url}
    text = PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)
    if "{url}" in template:
        return text if len(text) <= DISCORD_LIMIT else text[: DISCORD_LIMIT - 1] + "…"
    room = DISCORD_LIMIT - len(url) - 1
    if len(text) > room:
        text = text[: room - 1] + "…"
    return f"{text}\n{url}"


class Notifier:
    def __init__(self, db, bot, session, activity):
        self.db = db
        self.bot = bot
        self.session = session
        self.activity = activity
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._failure_logged: set[str] = set()
        self.polling = False
        self.last_poll = 0.0
        self.next_poll = 0.0

    # ---- helpers --------------------------------------------------------
    def poll_interval(self) -> int:
        try:
            return max(60, int(self.db.get_setting("poll_interval")))
        except ValueError:
            return 300

    def request_gap(self) -> float:
        """Seconds to wait between one channel's feed request and the next."""
        try:
            return max(0.0, min(300.0, float(self.db.get_setting("request_gap"))))
        except (TypeError, ValueError):
            return 5.0

    def target_for(self, channel: dict, settings: dict) -> str:
        return (channel.get("discord_channel_id") or settings.get("discord_channel_id") or "").strip()

    def message_for(self, channel: dict, settings: dict, video: Video) -> str:
        template = channel.get("message") or settings.get("default_message") or ""
        return build_message(template, channel.get("name", ""), video.title, video.url)

    def _remember_latest(self, channel: dict, video: Video):
        if video.published >= (channel.get("last_video_published") or 0):
            self.db.update_channel(
                channel["id"],
                last_video_id=video.video_id,
                last_video_title=video.title,
                last_video_published=video.published,
            )
            channel.update(
                last_video_id=video.video_id,
                last_video_title=video.title,
                last_video_published=video.published,
            )

    def wake(self):
        self._wake.set()

    # ---- processing -----------------------------------------------------
    async def process_video(self, channel: dict, video: Video, source: str) -> str:
        """Decide what to do with one video. Returns posted / skipped / seen / failed."""
        async with self._lock:
            if self.db.is_seen(video.video_id):
                return "seen"
            channel = self.db.get_channel(channel["id"])
            if channel is None or not channel["enabled"]:
                return "skipped"
            settings = self.db.get_settings()
            now = time.time()
            self._remember_latest(channel, video)

            try:
                max_age = float(settings.get("max_age_hours") or 0) * 3600
            except ValueError:
                max_age = 48 * 3600
            too_old = max_age > 0 and now - video.published > max_age
            before_watch = video.published < channel["watch_since"] - 600
            if too_old or before_watch:
                # Old video re-announced by YouTube (edit, un-hidden, etc.) -> ignore quietly.
                self.db.mark_seen(video.video_id, channel["yt_channel_id"], posted=False)
                return "skipped"

            if not channel["include_shorts"]:
                short = video.short_hint or (await youtube.is_short(self.session, video.video_id)) is True
                if short:
                    self.db.mark_seen(video.video_id, channel["yt_channel_id"], posted=False)
                    self.activity.info(f"Skipped Short “{video.title}” from {channel['name']}")
                    return "skipped"

            target = self.target_for(channel, settings)
            try:
                if not target:
                    raise ValueError("no Discord channel is set. Pick one in Discord bot settings.")
                where = await self.bot.send(target, self.message_for(channel, settings, video))
            except Exception as exc:
                reason = describe_error(exc).rstrip(".") + "."
                # Forget the cached feed validators, so the retry on the next check
                # re-reads the feed in full instead of being told "nothing changed".
                self.db.update_channel(
                    channel["id"],
                    last_error=f"Couldn't post: {reason}",
                    feed_etag="",
                    feed_modified="",
                )
                if video.video_id not in self._failure_logged:
                    self._failure_logged.add(video.video_id)
                    self.activity.error(
                        f"Couldn't post “{video.title}” from {channel['name']}: {reason} "
                        "It will be retried on the next check."
                    )
                return "failed"

            self._failure_logged.discard(video.video_id)
            self.db.mark_seen(video.video_id, channel["yt_channel_id"], posted=True)
            self.db.update_channel(channel["id"], last_posted_at=now, last_error="")
            via = "instant" if source == "websub" else "feed check"
            self.activity.success(f"Posted “{video.title}” from {channel['name']} to {where} ({via})")
            return "posted"

    def _record_failure(self, channel: dict, message: str, retry_after: float = 0.0) -> dict:
        """Count a failed check. Only complain once it has happened repeatedly."""
        fails = (channel.get("fail_count") or 0) + 1
        updates = {"last_checked": time.time(), "fail_count": fails}
        if fails >= FAIL_BEFORE_REPORT:
            detail = message
            if "(404)" in message:
                detail += " It may have been deleted, renamed or made private."
            updates["last_error"] = f"{detail} Failed {fails} checks in a row."
            if fails == FAIL_BEFORE_REPORT:
                self.activity.warn(f"{channel['name']}: {detail}")
        self.db.update_channel(channel["id"], **updates)
        log.info("Feed check for %s failed (%d in a row): %s", channel.get("name"), fails, message)
        return {"posted": 0, "error": message, "failures": fails, "retry_after": retry_after}

    async def check_channel(self, channel: dict, *, retry: bool = True) -> dict:
        """Read one channel's feed and post anything new."""
        try:
            feed = await youtube.fetch_feed(
                self.session,
                channel["yt_channel_id"],
                etag=channel.get("feed_etag") or "",
                modified=channel.get("feed_modified") or "",
            )
        except TransientError as exc:
            if retry:
                # These usually clear on their own, so pause and try once more
                # before holding it against the channel.
                await asyncio.sleep(jitter(max(4.0, exc.retry_after)))
                fresh = self.db.get_channel(channel["id"]) or channel
                return await self.check_channel(fresh, retry=False)
            return self._record_failure(channel, str(exc), exc.retry_after)
        except YouTubeError as exc:
            return self._record_failure(channel, str(exc))

        updates = {"last_checked": time.time(), "fail_count": 0, "last_error": ""}
        if feed.etag or feed.modified:
            updates["feed_etag"] = feed.etag
            updates["feed_modified"] = feed.modified
        if feed.unchanged:
            # YouTube answered "nothing new since last time" without sending a body.
            self.db.update_channel(channel["id"], **updates)
            return {"posted": 0, "unchanged": True}

        videos = feed.videos
        if feed.title and feed.title != channel["name"]:
            updates["name"] = feed.title
        self.db.update_channel(channel["id"], **updates)
        channel = {**channel, **updates}

        videos.sort(key=lambda v: v.published)
        if videos:
            self._remember_latest(channel, videos[-1])

        if not channel["seeded"]:
            # First look at this channel: remember what's already there, post nothing.
            for video in videos:
                self.db.mark_seen(video.video_id, channel["yt_channel_id"], posted=False)
            self.db.update_channel(channel["id"], seeded=1, last_error="")
            return {"posted": 0, "seeded": True}

        posted = failed = 0
        for video in videos:
            if self.db.is_seen(video.video_id):
                continue
            result = await self.process_video(channel, video, "poll")
            posted += result == "posted"
            failed += result == "failed"
        if not failed:
            self.db.update_channel(channel["id"], last_error="")
        return {"posted": posted, "failed": failed}

    async def poll_all(self):
        """Check every enabled channel, one at a time, spaced out.

        Checking them all at once is what gets a home IP throttled: YouTube then
        answers 404 or 500 for channels that are perfectly healthy. Going through
        them in single file with a gap in between keeps the request rate low
        enough that this doesn't happen.
        """
        if self.polling:
            return
        self.polling = True
        try:
            channels = [c for c in self.db.list_channels() if c["enabled"]]
            gap = self.request_gap()
            for index, channel in enumerate(channels):
                if index:
                    await asyncio.sleep(jitter(gap))
                try:
                    result = await self.check_channel(channel)
                except Exception:
                    log.exception("Checking %s failed", channel.get("name"))
                    continue
                # If YouTube explicitly asked us to slow down, do as we're told.
                pause = result.get("retry_after") or 0
                if pause:
                    await asyncio.sleep(min(pause, 120))
            self.last_poll = time.time()
        finally:
            self.polling = False

    async def run(self):
        await asyncio.sleep(5)  # let the Discord bot connect first
        while True:
            started = time.time()
            try:
                await self.poll_all()
            except Exception:
                log.exception("Feed check failed")
            # A pass now takes real time, so count it towards the interval rather
            # than adding to it. Never less than 30s, however long the pass took.
            delay = max(30.0, self.poll_interval() - (time.time() - started))
            self.next_poll = time.time() + delay
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    # ---- manual actions -------------------------------------------------
    async def test_post(self, channel: dict) -> str:
        """Post the channel's latest video with its message, without touching history."""
        feed = await youtube.fetch_feed(self.session, channel["yt_channel_id"])
        if not feed.videos:
            raise ValueError("this channel has no public videos to test with.")
        video = max(feed.videos, key=lambda v: v.published)
        settings = self.db.get_settings()
        target = self.target_for(channel, settings)
        if not target:
            raise ValueError("no Discord channel is set. Pick one in Discord bot settings.")
        return await self.bot.send(target, self.message_for(channel, settings, video))
