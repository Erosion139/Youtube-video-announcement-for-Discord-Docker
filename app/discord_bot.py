"""Discord bot connection: log in with a bot token, stay connected, send messages."""
import asyncio
import logging

import discord

log = logging.getLogger(__name__)

# View Channels + Send Messages + Embed Links + Mention Everyone (for @everyone / role pings)
INVITE_PERMISSIONS = 1024 | 2048 | 16384 | 131072


class BotNotReady(Exception):
    pass


def describe_error(exc: Exception) -> str:
    """A human-readable explanation of a failed Discord call."""
    if isinstance(exc, discord.Forbidden):
        return ("the bot isn't allowed to post in that channel. Give it View Channel "
                "and Send Messages there.")
    if isinstance(exc, discord.NotFound):
        return ("Discord channel not found. Check the channel ID, and that the bot "
                "has been added to that server.")
    if isinstance(exc, discord.HTTPException):
        return f"Discord returned an error ({exc.status}): {exc.text or exc}"
    return str(exc) or type(exc).__name__


class BotManager:
    def __init__(self, activity):
        self.activity = activity
        self.client: discord.Client | None = None
        self._task: asyncio.Task | None = None
        self.status = "not_configured"  # not_configured | connecting | connected | reconnecting | error
        self.error = ""

    # ---- lifecycle ------------------------------------------------------
    def _make_client(self) -> discord.Client:
        intents = discord.Intents.none()
        intents.guilds = True  # needed to list servers and channels in the web interface
        client = discord.Client(
            intents=intents,
            activity=discord.Activity(type=discord.ActivityType.watching, name="for new uploads"),
        )

        @client.event
        async def on_ready():
            if self.client is not client:
                return
            first = self.status != "connected"
            self.status, self.error = "connected", ""
            if first:
                self.activity.success(
                    f"Discord bot online as {client.user} in {len(client.guilds)} server(s)"
                )

        @client.event
        async def on_resumed():
            if self.client is client:
                self.status = "connected"

        @client.event
        async def on_disconnect():
            if self.client is client and self.status == "connected":
                self.status = "reconnecting"

        return client

    async def start(self, token: str):
        await self.stop()
        token = (token or "").strip()
        if not token:
            self.status, self.error = "not_configured", ""
            return
        self.status, self.error = "connecting", ""
        self._task = asyncio.create_task(self._run(token), name="discord-bot")

    async def _run(self, token: str):
        delay = 15
        while True:
            client = self._make_client()
            self.client = client
            try:
                await client.start(token)
                return  # closed on purpose
            except discord.LoginFailure:
                self.status = "error"
                self.error = "Discord rejected the bot token. Copy a fresh one from the Developer Portal."
                self.activity.error(self.error)
                return
            except discord.PrivilegedIntentsRequired:
                self.status = "error"
                self.error = "Discord refused the connection because of intents settings."
                self.activity.error(self.error)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network problems, Discord outages...
                self.status = "error"
                self.error = f"Couldn't connect to Discord ({type(exc).__name__}: {exc}). Retrying."
                log.warning(self.error)
            finally:
                if not client.is_closed():
                    try:
                        await client.close()
                    except Exception:
                        pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
            self.status = "connecting"

    async def stop(self):
        task, self._task = self._task, None
        client, self.client = self.client, None
        if client is not None and not client.is_closed():
            try:
                await client.close()
            except Exception:
                pass
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        self.status, self.error = "not_configured", ""

    # ---- queries --------------------------------------------------------
    @property
    def ready(self) -> bool:
        return self.client is not None and self.client.is_ready()

    def info(self) -> dict:
        client = self.client
        user = client.user if client is not None and self.ready else None
        app_id = (client.application_id if client else None) or (user.id if user else None)
        return {
            "status": self.status,
            "error": self.error,
            "user": str(user) if user else "",
            "user_id": str(user.id) if user else "",
            "avatar": str(user.display_avatar.url) if user else "",
            "guild_count": len(client.guilds) if user else 0,
            "invite_url": (
                f"https://discord.com/oauth2/authorize?client_id={app_id}"
                f"&scope=bot&permissions={INVITE_PERMISSIONS}"
            ) if app_id else "",
        }

    def list_channels(self) -> list[dict]:
        if not self.ready:
            return []
        guilds = []
        for guild in sorted(self.client.guilds, key=lambda g: g.name.lower()):
            me = guild.me
            channels = []
            for channel in guild.text_channels:
                perms = channel.permissions_for(me) if me else None
                channels.append({
                    "id": str(channel.id),
                    "name": channel.name,
                    "category": channel.category.name if channel.category else "",
                    "can_send": bool(perms.view_channel and perms.send_messages) if perms else True,
                })
            guilds.append({"id": str(guild.id), "name": guild.name, "channels": channels})
        return guilds

    # ---- sending --------------------------------------------------------
    async def send(self, channel_id: str, content: str) -> str:
        """Post a message; returns the channel's display name."""
        if not self.ready:
            raise BotNotReady("the Discord bot isn't connected. Check the bot token in settings.")
        try:
            cid = int(str(channel_id).strip())
        except ValueError as exc:
            raise BotNotReady(f"'{channel_id}' isn't a valid Discord channel ID.") from exc
        channel = self.client.get_channel(cid)
        if channel is None:
            channel = await self.client.fetch_channel(cid)
        if not hasattr(channel, "send"):
            raise BotNotReady("that Discord channel can't receive messages (pick a text channel).")
        await channel.send(
            content,
            allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=True),
        )
        name = getattr(channel, "name", "")
        return f"#{name}" if name else str(cid)
