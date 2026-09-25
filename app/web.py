"""Web interface (static page) and the JSON API it uses."""
import asyncio
import base64
import binascii
import hmac
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

from . import VERSION
from .config import ADMIN_PASSWORD, ADMIN_USER
from .discord_bot import describe_error
from .websub import CALLBACK_PATH
from .youtube import YouTubeError, resolve_channel

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
DISCORD_ID_RE = re.compile(r"^\d{15,25}$")
MAX_MESSAGE = 1800


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---- middleware ------------------------------------------------------------

@web.middleware
async def error_middleware(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except ApiError as exc:
        return web.json_response({"error": exc.message}, status=exc.status)
    except Exception as exc:
        log.exception("Unhandled error on %s %s", request.method, request.path)
        return web.json_response({"error": f"Something went wrong: {exc}"}, status=500)


def _open_path(path: str) -> bool:
    return path.startswith("/websub/") or path == "/health"


@web.middleware
async def auth_middleware(request, handler):
    if ADMIN_PASSWORD and not _open_path(request.path):
        ok = False
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                ok = hmac.compare_digest(user.encode(), ADMIN_USER.encode()) and hmac.compare_digest(
                    password.encode(), ADMIN_PASSWORD.encode()
                )
            except (binascii.Error, UnicodeDecodeError):
                ok = False
        if not ok:
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="Upload Notifier"'})
    return await handler(request)


@web.middleware
async def csrf_middleware(request, handler):
    # A custom header can't be sent cross-site without a CORS preflight, which we never allow.
    if (
        request.path.startswith("/api/")
        and request.method not in ("GET", "HEAD", "OPTIONS")
        and request.headers.get("X-Requested-With") != "fetch"
    ):
        raise web.HTTPForbidden(text="Missing X-Requested-With header")
    return await handler(request)


# ---- helpers ---------------------------------------------------------------

async def read_json(request) -> dict:
    try:
        data = await request.json()
    except Exception as exc:
        raise ApiError("The request body wasn't valid JSON.") from exc
    if not isinstance(data, dict):
        raise ApiError("Expected a JSON object.")
    return data


def parse_discord_channel(value) -> str:
    """Accept a channel ID, a <#mention>, or a discord.com/channels/... link."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"discord(?:app)?\.com/channels/\d+/(\d+)", text)
    if match:
        text = match.group(1)
    text = text.strip("<>#")
    if not DISCORD_ID_RE.match(text):
        raise ApiError(
            "That isn't a Discord channel ID. In Discord, turn on Developer Mode "
            "(Settings > Advanced), then right-click the channel and choose Copy Channel ID."
        )
    return text


def mask(secret: str) -> str:
    return f"ends in {secret[-4:]}" if len(secret) >= 8 else ("saved" if secret else "")


def channel_json(ch: dict, websub_on: bool, now: float) -> dict:
    push = "off"
    if websub_on and ch["enabled"]:
        push = "active" if ch["websub_expires"] > now else "pending"
    return {
        "id": ch["id"],
        "yt_channel_id": ch["yt_channel_id"],
        "name": ch["name"],
        "handle": ch["handle"],
        "thumbnail": ch["thumbnail"],
        "url": f"https://www.youtube.com/channel/{ch['yt_channel_id']}",
        "enabled": bool(ch["enabled"]),
        "message": ch["message"],
        "discord_channel_id": ch["discord_channel_id"],
        "include_shorts": bool(ch["include_shorts"]),
        "last_checked": ch["last_checked"],
        "last_error": ch["last_error"],
        "last_video_id": ch["last_video_id"],
        "last_video_title": ch["last_video_title"],
        "last_video_published": ch["last_video_published"],
        "last_posted_at": ch["last_posted_at"],
        "push": push,
    }


def settings_json(ctx) -> dict:
    s = ctx.db.get_settings()
    return {
        "discord_token_set": bool(s["discord_token"]),
        "discord_token_hint": mask(s["discord_token"]),
        "discord_channel_id": s["discord_channel_id"],
        "default_message": s["default_message"],
        "poll_interval": int(s["poll_interval"] or 300),
        "request_gap": ctx.notifier.request_gap(),
        "max_age_hours": int(s["max_age_hours"] or 0),
        "youtube_api_key_set": bool(s["youtube_api_key"]),
        "youtube_api_key_hint": mask(s["youtube_api_key"]),
        "public_url": s["public_url"],
        "callback_url": ctx.websub.callback_url(),
    }


def get_channel_or_404(ctx, request) -> dict:
    try:
        pk = int(request.match_info["id"])
    except ValueError as exc:
        raise ApiError("Unknown channel.", 404) from exc
    ch = ctx.db.get_channel(pk)
    if ch is None:
        raise ApiError("That channel is no longer in the list.", 404)
    return ch


# ---- handlers --------------------------------------------------------------

async def index(request):
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("__VERSION__", VERSION)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def health(request):
    return web.json_response({"ok": True, "version": VERSION})


async def get_status(request):
    ctx = request.app["ctx"]
    channels = ctx.db.list_channels()
    now = time.time()
    enabled = [c for c in channels if c["enabled"]]
    return web.json_response({
        "version": VERSION,
        "bot": ctx.bot.info(),
        "poll": {
            "last": ctx.notifier.last_poll,
            "next": ctx.notifier.next_poll,
            "running": ctx.notifier.polling,
            "interval": ctx.notifier.poll_interval(),
        },
        "websub": {
            "enabled": ctx.websub.enabled,
            "active": sum(1 for c in enabled if c["websub_expires"] > now),
        },
        "channels": {"total": len(channels), "enabled": len(enabled)},
        "default_channel_set": bool(ctx.db.get_setting("discord_channel_id")),
    })


async def get_settings(request):
    return web.json_response(settings_json(request.app["ctx"]))


async def put_settings(request):
    ctx = request.app["ctx"]
    data = await read_json(request)
    old = ctx.db.get_settings()
    updates: dict[str, str] = {}

    restart_bot = False
    if data.get("clear_discord_token"):
        updates["discord_token"] = ""
        restart_bot = True
    elif str(data.get("discord_token") or "").strip():
        token = str(data["discord_token"]).strip()
        if token.lower().startswith("bot "):
            token = token[4:].strip()
        if len(token) < 50 or " " in token:
            raise ApiError("That doesn't look like a bot token. Use Reset Token on the Bot page and copy the full value.")
        updates["discord_token"] = token
        restart_bot = True

    if "discord_channel_id" in data:
        updates["discord_channel_id"] = parse_discord_channel(data["discord_channel_id"])

    if "default_message" in data:
        message = str(data["default_message"] or "").strip()
        if len(message) > MAX_MESSAGE:
            raise ApiError(f"Keep the default message under {MAX_MESSAGE} characters.")
        updates["default_message"] = message

    if "poll_interval" in data:
        try:
            interval = int(data["poll_interval"])
        except (TypeError, ValueError) as exc:
            raise ApiError("The check interval must be a whole number of seconds.") from exc
        if not 60 <= interval <= 86400:
            raise ApiError("Check every 1 minute at the most, and at least once a day.")
        updates["poll_interval"] = str(interval)

    if "request_gap" in data:
        try:
            gap = float(data["request_gap"])
        except (TypeError, ValueError) as exc:
            raise ApiError("The wait between channels must be a number of seconds.") from exc
        if not 0 <= gap <= 300:
            raise ApiError("The wait between channels must be between 0 and 300 seconds.")
        updates["request_gap"] = str(int(gap))

    if "max_age_hours" in data:
        try:
            hours = int(data["max_age_hours"])
        except (TypeError, ValueError) as exc:
            raise ApiError("The age limit must be a whole number of hours.") from exc
        if not 0 <= hours <= 8760:
            raise ApiError("The age limit must be between 0 and 8760 hours.")
        updates["max_age_hours"] = str(hours)

    if data.get("clear_youtube_api_key"):
        updates["youtube_api_key"] = ""
    elif str(data.get("youtube_api_key") or "").strip():
        updates["youtube_api_key"] = str(data["youtube_api_key"]).strip()

    if "public_url" in data:
        url = str(data["public_url"] or "").strip().rstrip("/")
        if url:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ApiError("Enter the full public address, like https://yt.example.com")
            if parsed.path.endswith(CALLBACK_PATH):
                url = url[: -len(CALLBACK_PATH)]
        updates["public_url"] = url

    ctx.db.set_settings(updates)

    if restart_bot:
        await ctx.bot.start(updates["discord_token"])
        ctx.activity.info("Discord bot token removed" if not updates["discord_token"] else "Discord bot token updated")
    if "poll_interval" in updates and updates["poll_interval"] != old["poll_interval"]:
        ctx.notifier.wake()
    if "public_url" in updates and updates["public_url"] != old["public_url"]:
        ctx.db.reset_websub()
        ctx.websub.wake()
        if updates["public_url"]:
            ctx.activity.info("Instant notifications turned on; asking YouTube to subscribe")
        else:
            ctx.activity.info("Instant notifications turned off")
    return web.json_response(settings_json(ctx))


async def discord_channels(request):
    ctx = request.app["ctx"]
    return web.json_response({"ready": ctx.bot.ready, "guilds": ctx.bot.list_channels()})


async def discord_test(request):
    ctx = request.app["ctx"]
    data = await read_json(request)
    target = parse_discord_channel(data.get("channel_id")) or ctx.db.get_setting("discord_channel_id")
    if not target:
        raise ApiError("Choose a Discord channel first.")
    try:
        where = await ctx.bot.send(
            target, "✅ Upload notifications are set up. New videos will be posted in this channel."
        )
    except Exception as exc:
        raise ApiError(f"Test message failed: {describe_error(exc)}") from exc
    return web.json_response({"ok": True, "channel": where})


async def list_channels(request):
    ctx = request.app["ctx"]
    now = time.time()
    on = ctx.websub.enabled
    return web.json_response([channel_json(c, on, now) for c in ctx.db.list_channels()])


async def add_channel(request):
    ctx = request.app["ctx"]
    data = await read_json(request)
    try:
        info = await resolve_channel(ctx.session, str(data.get("query", "")), ctx.db.get_setting("youtube_api_key"))
    except YouTubeError as exc:
        raise ApiError(str(exc)) from exc
    existing = ctx.db.get_channel_by_yt(info.channel_id)
    if existing:
        raise ApiError(f"{existing['name']} is already in the list.", 409)
    pk = ctx.db.add_channel(info.channel_id, info.name, info.handle, info.thumbnail)
    ctx.activity.info(f"Now watching {info.name}")
    await ctx.notifier.check_channel(ctx.db.get_channel(pk))  # remembers existing videos
    ctx.websub.wake()
    return web.json_response(channel_json(ctx.db.get_channel(pk), ctx.websub.enabled, time.time()), status=201)


async def update_channel(request):
    ctx = request.app["ctx"]
    ch = get_channel_or_404(ctx, request)
    data = await read_json(request)
    updates = {}

    if "message" in data:
        message = str(data["message"] or "").strip()
        if len(message) > MAX_MESSAGE:
            raise ApiError(f"Keep the message under {MAX_MESSAGE} characters.")
        updates["message"] = message
    if "discord_channel_id" in data:
        updates["discord_channel_id"] = parse_discord_channel(data["discord_channel_id"])
    if "include_shorts" in data:
        updates["include_shorts"] = 1 if data["include_shorts"] else 0

    turned_on = turned_off = False
    if "enabled" in data:
        enabled = 1 if data["enabled"] else 0
        if enabled and not ch["enabled"]:
            turned_on = True
            # Start fresh: don't announce anything uploaded while it was switched off.
            updates.update(enabled=1, seeded=0, watch_since=time.time(),
                           websub_requested=0, websub_expires=0, last_error="")
        elif not enabled and ch["enabled"]:
            turned_off = True
            updates.update(enabled=0, websub_expires=0, websub_requested=0)

    ctx.db.update_channel(ch["id"], **updates)
    if turned_on:
        ctx.activity.info(f"Turned on {ch['name']}")
        await ctx.notifier.check_channel(ctx.db.get_channel(ch["id"]))
        ctx.websub.wake()
    if turned_off:
        ctx.activity.info(f"Turned off {ch['name']}")
        ctx.websub.unsubscribe_soon(ch["yt_channel_id"])
    return web.json_response(channel_json(ctx.db.get_channel(ch["id"]), ctx.websub.enabled, time.time()))


async def delete_channel(request):
    ctx = request.app["ctx"]
    ch = get_channel_or_404(ctx, request)
    ctx.db.delete_channel(ch["id"])
    ctx.websub.unsubscribe_soon(ch["yt_channel_id"])
    ctx.activity.info(f"Removed {ch['name']}")
    return web.json_response({"ok": True})


async def test_channel(request):
    ctx = request.app["ctx"]
    ch = get_channel_or_404(ctx, request)
    try:
        where = await ctx.notifier.test_post(ch)
    except Exception as exc:
        raise ApiError(f"Test post failed: {describe_error(exc)}") from exc
    return web.json_response({"ok": True, "channel": where})


async def check_channel(request):
    ctx = request.app["ctx"]
    ch = get_channel_or_404(ctx, request)
    if not ch["enabled"]:
        raise ApiError("Turn this channel on to check it.")
    result = await ctx.notifier.check_channel(ch)
    return web.json_response({
        **result,
        "channel": channel_json(ctx.db.get_channel(ch["id"]), ctx.websub.enabled, time.time()),
    })


async def check_all(request):
    ctx = request.app["ctx"]
    if ctx.notifier.polling:
        return web.json_response({"ok": True, "already_running": True})
    ctx.notifier.wake()
    await asyncio.sleep(0)  # let the poller start
    return web.json_response({"ok": True})


async def activity(request):
    return web.json_response(request.app["ctx"].db.recent_activity())


def create_app(ctx) -> web.Application:
    app = web.Application(middlewares=[error_middleware, auth_middleware, csrf_middleware])
    app["ctx"] = ctx
    r = app.router
    r.add_get("/", index)
    r.add_get("/health", health)
    r.add_static("/static/", STATIC_DIR, append_version=False)

    r.add_get("/api/status", get_status)
    r.add_get("/api/settings", get_settings)
    r.add_put("/api/settings", put_settings)
    r.add_get("/api/discord/channels", discord_channels)
    r.add_post("/api/discord/test", discord_test)
    r.add_get("/api/channels", list_channels)
    r.add_post("/api/channels", add_channel)
    r.add_patch("/api/channels/{id}", update_channel)
    r.add_delete("/api/channels/{id}", delete_channel)
    r.add_post("/api/channels/{id}/test", test_channel)
    r.add_post("/api/channels/{id}/check", check_channel)
    r.add_post("/api/check", check_all)
    r.add_get("/api/activity", activity)

    r.add_get(CALLBACK_PATH, ctx.websub.handle_verify)
    r.add_post(CALLBACK_PATH, ctx.websub.handle_notification)
    return app
