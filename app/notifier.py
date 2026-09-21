"""Detect new uploads (by polling feeds or from WebSub pushes) and post them to Discord."""
import asyncio
import logging
import re
import time

from . import youtube
from .discord_bot import describe_error
from .youtube import Video, YouTubeError

log = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{(channel|title|url)\}")
DISCORD_LIMIT = 2000


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
                self.db.update_channel(channel["id"], last_error=f"Couldn't post: {reason}")
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

    async def check_channel(self, channel: dict) -> dict:
        """Read one channel's feed and post anything new."""
        try:
            feed_title, videos = await youtube.fetch_feed(self.session, channel["yt_channel_id"])
        except YouTubeError as exc:
            self.db.update_channel(channel["id"], last_checked=time.time(), last_error=str(exc))
            return {"posted": 0, "error": str(exc)}

        updates = {"last_checked": time.time()}
        if feed_title and feed_title != channel["name"]:
            updates["name"] = feed_title
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
        if self.polling:
            return
        self.polling = True
        try:
            channels = [c for c in self.db.list_channels() if c["enabled"]]
            semaphore = asyncio.Semaphore(4)

            async def one(channel):
                async with semaphore:
                    try:
                        await self.check_channel(channel)
                    except Exception:
                        log.exception("Checking %s failed", channel.get("name"))

            await asyncio.gather(*(one(c) for c in channels))
            self.last_poll = time.time()
        finally:
            self.polling = False

    async def run(self):
        await asyncio.sleep(5)  # let the Discord bot connect first
        while True:
            try:
                await self.poll_all()
            except Exception:
                log.exception("Feed check failed")
            interval = self.poll_interval()
            self.next_poll = time.time() + interval
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    # ---- manual actions -------------------------------------------------
    async def test_post(self, channel: dict) -> str:
        """Post the channel's latest video with its message, without touching history."""
        _, videos = await youtube.fetch_feed(self.session, channel["yt_channel_id"])
        if not videos:
            raise ValueError("this channel has no public videos to test with.")
        video = max(videos, key=lambda v: v.published)
        settings = self.db.get_settings()
        target = self.target_for(channel, settings)
        if not target:
            raise ValueError("no Discord channel is set. Pick one in Discord bot settings.")
        return await self.bot.send(target, self.message_for(channel, settings, video))
