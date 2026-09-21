"""Instant upload notifications via YouTube's WebSub (PubSubHubbub) hub.

When a public URL is configured, each enabled channel is subscribed at Google's
hub. YouTube then POSTs an Atom entry to /websub/callback within seconds of an
upload. Subscriptions expire, so they are renewed automatically. Feed polling
keeps running as a safety net.
"""
import asyncio
import hashlib
import hmac
import logging
import time
from urllib.parse import parse_qs, urlparse

import aiohttp
from aiohttp import web

from . import youtube
from .youtube import TOPIC_URL, YouTubeError

log = logging.getLogger(__name__)

HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"
LEASE_SECONDS = 432000        # 5 days, YouTube's usual lease
RENEW_BEFORE = 86400          # renew when less than a day is left
RETRY_UNVERIFIED = 6 * 3600   # re-request an unconfirmed subscription after 6 hours
CALLBACK_PATH = "/websub/callback"


def topic_channel_id(topic: str) -> str:
    try:
        return parse_qs(urlparse(topic).query).get("channel_id", [""])[0]
    except ValueError:
        return ""


class WebSubManager:
    def __init__(self, db, session, notifier, activity):
        self.db = db
        self.session = session
        self.notifier = notifier
        self.activity = activity
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    # ---- configuration --------------------------------------------------
    def callback_url(self) -> str:
        base = self.db.get_setting("public_url").strip().rstrip("/")
        return f"{base}{CALLBACK_PATH}" if base else ""

    @property
    def enabled(self) -> bool:
        return bool(self.callback_url())

    @property
    def secret(self) -> bytes:
        return self.db.get_setting("websub_secret").encode()

    def wake(self):
        self._wake.set()

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- hub requests ---------------------------------------------------
    async def _request(self, yt_channel_id: str, mode: str) -> bool:
        callback = self.callback_url()
        if not callback:
            return False
        data = {
            "hub.callback": callback,
            "hub.topic": TOPIC_URL.format(yt_channel_id),
            "hub.verify": "async",
            "hub.mode": mode,
            "hub.lease_seconds": str(LEASE_SECONDS),
        }
        if mode == "subscribe":
            data["hub.secret"] = self.secret.decode()
        try:
            async with self.session.post(HUB_URL, data=data) as resp:
                if resp.status in (202, 204):
                    return True
                body = (await resp.text())[:200]
                log.warning("Hub %s for %s failed: HTTP %s %s", mode, yt_channel_id, resp.status, body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Hub %s for %s failed: %s", mode, yt_channel_id, exc)
        return False

    def unsubscribe_soon(self, yt_channel_id: str):
        if self.enabled:
            self._spawn(self._request(yt_channel_id, "unsubscribe"))

    async def renew_due(self):
        if not self.enabled:
            return
        now = time.time()
        for channel in self.db.list_channels():
            if not channel["enabled"]:
                continue
            if channel["websub_expires"] > now + RENEW_BEFORE:
                continue
            if now - channel["websub_requested"] < RETRY_UNVERIFIED:
                continue
            self.db.update_channel(channel["id"], websub_requested=now)
            await self._request(channel["yt_channel_id"], "subscribe")
            await asyncio.sleep(0.3)

    async def run(self):
        await asyncio.sleep(10)
        while True:
            try:
                await self.renew_due()
            except Exception:
                log.exception("WebSub renewal failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1800)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    # ---- callback endpoint ----------------------------------------------
    async def handle_verify(self, request: web.Request) -> web.Response:
        """The hub confirms a (un)subscription by asking us to echo hub.challenge."""
        query = request.query
        mode = query.get("hub.mode", "")
        challenge = query.get("hub.challenge", "")
        yt_channel_id = topic_channel_id(query.get("hub.topic", ""))
        channel = self.db.get_channel_by_yt(yt_channel_id) if yt_channel_id else None

        if mode == "denied":
            log.warning("Hub denied subscription for %s: %s", yt_channel_id, query.get("hub.reason", ""))
            return web.Response(text="ok")
        if not challenge or not yt_channel_id:
            return web.Response(status=400, text="missing parameters")

        wanted = channel is not None and bool(channel["enabled"]) and self.enabled
        if mode == "subscribe":
            if not wanted:
                return web.Response(status=404, text="not subscribed")
            try:
                lease = int(query.get("hub.lease_seconds", LEASE_SECONDS))
            except ValueError:
                lease = LEASE_SECONDS
            was_active = channel["websub_expires"] > time.time()
            self.db.update_channel(channel["id"], websub_expires=time.time() + lease)
            if not was_active:
                self.activity.info(f"Instant notifications active for {channel['name']}")
            return web.Response(text=challenge)
        if mode == "unsubscribe":
            if wanted:
                return web.Response(status=404, text="still subscribed")
            if channel is not None:
                self.db.update_channel(channel["id"], websub_expires=0)
            return web.Response(text=challenge)
        return web.Response(status=400, text="unknown mode")

    async def handle_notification(self, request: web.Request) -> web.Response:
        """YouTube pushes an Atom entry here when a channel uploads or edits a video."""
        body = await request.read()
        signature = request.headers.get("X-Hub-Signature", "")
        expected = "sha1=" + hmac.new(self.secret, body, hashlib.sha1).hexdigest()
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            log.warning("Ignored WebSub notification with a bad signature")
            return web.Response(text="ok")  # per spec: acknowledge, but ignore
        try:
            _, videos = youtube.parse_feed(body)
        except YouTubeError:
            return web.Response(text="ok")
        for video in videos:
            channel = self.db.get_channel_by_yt(video.channel_id)
            if channel and channel["enabled"]:
                self._spawn(self.notifier.process_video(channel, video, "websub"))
        return web.Response(text="ok")
