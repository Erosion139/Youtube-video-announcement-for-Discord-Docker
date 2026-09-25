"""YouTube helpers: resolve channels, read upload feeds, detect Shorts.

Uploads are read from YouTube's public Atom feed
(https://www.youtube.com/feeds/videos.xml?channel_id=...), which needs no API key
and has no quota. The same Atom format is used by YouTube's WebSub push
notifications, so `parse_feed` handles both.
"""
import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from xml.etree.ElementTree import ParseError

import aiohttp
from defusedxml import ElementTree as ET

log = logging.getLogger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
TOPIC_URL = "https://www.youtube.com/xml/feeds/videos.xml?channel_id={}"
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HANDLE_RE = re.compile(r"^@?[\w.\-·]{3,100}$", re.UNICODE)

# Skips the EU cookie-consent interstitial so channel pages can be read.
PAGE_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "SOCS=CAI; CONSENT=YES+cb",
}

FEED_HEADERS = {
    "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class YouTubeError(Exception):
    """A problem talking to YouTube, with a message fit to show the user."""


class TransientError(YouTubeError):
    """A failure that is usually temporary: rate limiting, a hiccup, a timeout.

    YouTube's feed endpoint is not consistent about how it refuses a request. Under
    load or rate limiting it answers 404, 429 or 5xx more or less interchangeably,
    and the same channel succeeds moments later. Callers should retry these rather
    than show them, and only report a channel as broken after several failures.
    """

    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class Video:
    video_id: str
    channel_id: str
    title: str
    url: str
    published: float
    author: str = ""
    short_hint: bool = False


@dataclass
class ChannelInfo:
    channel_id: str
    name: str = ""
    handle: str = ""
    thumbnail: str = ""


@dataclass
class FeedResult:
    """One feed read. `unchanged` means YouTube answered 304 and sent no body."""
    title: str = ""
    videos: list[Video] = field(default_factory=list)
    etag: str = ""
    modified: str = ""
    unchanged: bool = False


def parse_timestamp(value: str | None) -> float:
    """Parse an Atom timestamp (fractional seconds of any length) to epoch seconds."""
    if not value:
        return time.time()
    text = value.strip().replace("Z", "+00:00")
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", text)
    if match:
        text = match.group(1) + (match.group(2) or "")[:7] + match.group(3)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return time.time()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_feed(xml_text: str | bytes) -> tuple[str, list[Video]]:
    """Return (channel title, videos) from a YouTube Atom feed or WebSub payload."""
    try:
        root = ET.fromstring(xml_text)
    except (ParseError, ValueError) as exc:
        raise YouTubeError(f"YouTube sent a feed that couldn't be read ({exc})") from exc

    feed_title = (root.findtext(f"{ATOM}title") or "").strip()
    videos = []
    for entry in root.findall(f"{ATOM}entry"):
        video_id = (entry.findtext(f"{YT}videoId") or "").strip()
        if not video_id:
            continue
        link = ""
        for link_el in entry.findall(f"{ATOM}link"):
            if link_el.get("rel", "alternate") == "alternate":
                link = link_el.get("href", "")
                break
        if not link.startswith("https://www.youtube.com/"):
            link = f"https://www.youtube.com/watch?v={video_id}"
        videos.append(
            Video(
                video_id=video_id,
                channel_id=(entry.findtext(f"{YT}channelId") or "").strip(),
                title=(entry.findtext(f"{ATOM}title") or "").strip(),
                url=link,
                published=parse_timestamp(entry.findtext(f"{ATOM}published")),
                author=(entry.findtext(f"{ATOM}author/{ATOM}name") or "").strip(),
                short_hint="/shorts/" in link,
            )
        )
    return feed_title, videos


def _retry_after(resp) -> float:
    try:
        return max(0.0, min(300.0, float(resp.headers.get("Retry-After", "0"))))
    except ValueError:
        return 0.0


async def fetch_feed(
    session: aiohttp.ClientSession,
    channel_id: str,
    *,
    etag: str = "",
    modified: str = "",
) -> FeedResult:
    """Read a channel's upload feed.

    Passing the `etag`/`modified` from the previous read turns this into a
    conditional request: if nothing has been uploaded since, YouTube answers 304
    with no body, which is far cheaper and much less likely to trip rate limiting.
    """
    url = FEED_URL.format(channel_id)
    headers = dict(FEED_HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified

    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 304:
                return FeedResult(etag=etag, modified=modified, unchanged=True)
            if resp.status == 404:
                # Not necessarily gone: YouTube also answers 404 when it is
                # throttling, and for channels that have never uploaded.
                raise TransientError("YouTube didn't return a feed for this channel (404).")
            if resp.status == 429:
                raise TransientError(
                    "YouTube is rate limiting these requests (429).", _retry_after(resp)
                )
            if resp.status != 200:
                raise TransientError(
                    f"YouTube feed returned HTTP {resp.status}.", _retry_after(resp)
                )
            text = await resp.text()
            new_etag = resp.headers.get("ETag", "")
            new_modified = resp.headers.get("Last-Modified", "")
    except aiohttp.ClientError as exc:
        raise TransientError(f"Couldn't reach YouTube: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise TransientError("YouTube took too long to respond.") from exc

    title, videos = parse_feed(text)
    for video in videos:
        if not video.channel_id:
            video.channel_id = channel_id
    return FeedResult(title=title, videos=videos, etag=new_etag, modified=new_modified)


async def is_short(session: aiohttp.ClientSession, video_id: str) -> bool | None:
    """True if the video is a Short, False if not, None if YouTube didn't say.

    youtube.com/shorts/<id> answers 200 for Shorts and redirects to /watch otherwise.
    """
    url = f"https://www.youtube.com/shorts/{video_id}"
    try:
        async with session.head(url, allow_redirects=False, headers=PAGE_HEADERS) as resp:
            if resp.status == 200:
                return True
            if resp.status in (301, 302, 303, 307, 308):
                return False
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    return None


# ---- channel lookup -------------------------------------------------------

def _meta(text: str, prop: str) -> str:
    match = re.search(rf'<meta (?:property|name)="{re.escape(prop)}" content="([^"]*)"', text)
    return html.unescape(match.group(1)) if match else ""


async def _scrape_channel_page(session: aiohttp.ClientSession, url: str) -> ChannelInfo:
    try:
        async with session.get(url, headers=PAGE_HEADERS) as resp:
            if resp.status == 404:
                raise YouTubeError("YouTube says that channel doesn't exist. Check the link or handle.")
            if resp.status != 200:
                raise YouTubeError(f"YouTube returned HTTP {resp.status} for that channel page.")
            text = await resp.text()
    except aiohttp.ClientError as exc:
        raise YouTubeError(f"Couldn't reach YouTube: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise YouTubeError("YouTube took too long to respond. Try again.") from exc

    channel_id = None
    for pattern in (
        r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
        r'"externalId":"(UC[\w-]{22})"',
        r'<meta itemprop="identifier" content="(UC[\w-]{22})"',
        r'"channelId":"(UC[\w-]{22})"',
    ):
        match = re.search(pattern, text)
        if match:
            channel_id = match.group(1)
            break
    if not channel_id:
        raise YouTubeError(
            "Couldn't find a channel on that page. Try the channel ID instead "
            "(on the channel page: About > Share channel > Copy channel ID)."
        )
    handle_match = re.search(r'"vanityChannelUrl":"https?://www\.youtube\.com/(@[^"]+)"', text)
    return ChannelInfo(
        channel_id=channel_id,
        name=_meta(text, "og:title"),
        handle=html.unescape(handle_match.group(1)) if handle_match else "",
        thumbnail=_meta(text, "og:image"),
    )


async def _api_lookup(session, api_key: str, *, handle: str = "", channel_id: str = "") -> ChannelInfo | None:
    params = {"part": "snippet", "key": api_key}
    if handle:
        params["forHandle"] = handle
    else:
        params["id"] = channel_id
    try:
        async with session.get("https://www.googleapis.com/youtube/v3/channels", params=params) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                message = (data.get("error") or {}).get("message", f"HTTP {resp.status}")
                log.warning("YouTube Data API lookup failed: %s", message)
                return None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        log.warning("YouTube Data API lookup failed: %s", exc)
        return None
    items = data.get("items") or []
    if not items:
        return None
    snippet = items[0].get("snippet", {})
    return ChannelInfo(
        channel_id=items[0]["id"],
        name=snippet.get("title", ""),
        handle=snippet.get("customUrl", ""),
        thumbnail=(snippet.get("thumbnails", {}).get("default") or {}).get("url", ""),
    )


async def _channel_from_video(session, video_url: str) -> str:
    """Use oEmbed to find which channel uploaded a video; returns the channel page URL."""
    try:
        async with session.get(
            "https://www.youtube.com/oembed", params={"url": video_url, "format": "json"}
        ) as resp:
            if resp.status != 200:
                raise YouTubeError("Couldn't look up that video. Paste the channel link instead.")
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise YouTubeError(f"Couldn't reach YouTube: {exc}") from exc
    author_url = data.get("author_url", "")
    if not author_url:
        raise YouTubeError("Couldn't find who uploaded that video. Paste the channel link instead.")
    return author_url


async def resolve_channel(session: aiohttp.ClientSession, query: str, api_key: str = "") -> ChannelInfo:
    """Turn a channel URL, @handle, channel ID or video URL into ChannelInfo."""
    text = (query or "").strip()
    if not text:
        raise YouTubeError("Paste a YouTube channel link, @handle or channel ID.")

    channel_id = ""
    handle = ""
    page_url = ""

    if CHANNEL_ID_RE.match(text):
        channel_id = text
    elif text.startswith("@"):
        handle = text
    elif "youtube.com" in text or "youtu.be" in text:
        url = text if "://" in text else "https://" + text
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]
        if not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")):
            raise YouTubeError("That doesn't look like a YouTube link.")
        parts = [p for p in parsed.path.split("/") if p]
        if host == "youtu.be" or (parts and parts[0] in ("watch", "shorts", "live", "embed")):
            page_url = await _channel_from_video(session, url)
            if "/@" in page_url:
                handle = "@" + page_url.split("/@", 1)[1].split("/")[0]
                page_url = ""
        elif parts and parts[0] == "channel" and len(parts) > 1 and CHANNEL_ID_RE.match(parts[1]):
            channel_id = parts[1]
        elif parts and parts[0].startswith("@"):
            handle = parts[0]
        elif parts:
            page_url = "https://www.youtube.com/" + "/".join(parts[:2])
        else:
            raise YouTubeError("That link doesn't point to a channel.")
    elif HANDLE_RE.match(text):
        handle = "@" + text.lstrip("@")
    else:
        raise YouTubeError("Paste a YouTube channel link, @handle or channel ID (starts with UC).")

    info: ChannelInfo | None = None
    confirmed = False
    if api_key and (handle or channel_id):
        info = await _api_lookup(session, api_key, handle=handle, channel_id=channel_id)
        confirmed = info is not None

    if info is None:
        if handle:
            page_url = "https://www.youtube.com/" + quote(handle, safe="@")
        elif channel_id:
            page_url = f"https://www.youtube.com/channel/{channel_id}"
        try:
            info = await _scrape_channel_page(session, page_url)
            confirmed = True
        except YouTubeError:
            if not channel_id:
                raise
            info = ChannelInfo(channel_id=channel_id)

    if handle and not info.handle:
        info.handle = handle

    # The feed title is the authoritative channel name, and proves the channel exists.
    try:
        feed = await fetch_feed(session, info.channel_id)
        confirmed = True
        if feed.title:
            info.name = feed.title
    except YouTubeError as exc:
        log.warning("Feed check for %s failed during lookup: %s", info.channel_id, exc)
    if not confirmed:
        raise YouTubeError("Couldn't confirm that channel exists on YouTube. Check the ID and try again.")
    if not info.name:
        info.name = info.handle or info.channel_id
    return info
