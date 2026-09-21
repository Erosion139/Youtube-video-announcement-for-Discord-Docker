"""Entry point: start the web interface, the Discord bot, the feed poller and WebSub renewals."""
import asyncio
import logging
import os
import signal
import sqlite3
import sys
from types import SimpleNamespace

import aiohttp
from aiohttp import web

from . import VERSION
from .activity import Activity
from .config import ADMIN_PASSWORD, DATA_DIR, DB_PATH, LOG_LEVEL, PORT, USER_AGENT
from .db import Database
from .discord_bot import BotManager
from .notifier import Notifier
from .web import create_app
from .websub import WebSubManager

log = logging.getLogger("app")


async def main():
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    # This bot never joins voice channels, so the "voice will NOT be supported" notice is noise.
    logging.getLogger("discord.client").addFilter(
        lambda record: "voice will NOT be supported" not in record.getMessage()
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    try:
        db = Database(DB_PATH)
    except (sqlite3.Error, OSError) as exc:
        log.error(
            "Can't open the database at %s (%s). Make sure the folder mounted at %s "
            "is writable by user %s:%s.", DB_PATH, exc, DATA_DIR, os.getuid(), os.getgid(),
        )
        raise SystemExit(1) from exc

    activity = Activity(db)
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20),
        headers={"User-Agent": USER_AGENT},
    )
    bot = BotManager(activity)
    notifier = Notifier(db, bot, session, activity)
    websub = WebSubManager(db, session, notifier, activity)
    ctx = SimpleNamespace(db=db, activity=activity, session=session, bot=bot,
                          notifier=notifier, websub=websub)

    runner = web.AppRunner(create_app(ctx), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=PORT).start()
    log.info("Upload Notifier %s: web interface on port %s%s", VERSION, PORT,
             " (password protected)" if ADMIN_PASSWORD else "")
    activity.info(f"Started version {VERSION}")

    await bot.start(db.get_setting("discord_token"))
    tasks = [
        asyncio.create_task(notifier.run(), name="feed-poller"),
        asyncio.create_task(websub.run(), name="websub-renewal"),
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()

    log.info("Shutting down")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await bot.stop()
    await runner.cleanup()
    await session.close()
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
