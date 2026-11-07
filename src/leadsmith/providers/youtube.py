"""YouTube, free and without an API key.

WebFetch only sees YouTube's navigation text, so creator leads used to stall: no subscriber count, no country, no way
to check what their videos look like. This reads the data YouTube embeds in its own pages instead:

- search_channels: find creator channels for a topic (YouTube's channel filter)
- channel: profile (description, country, subscribers, links, emails in the description) and recent uploads
- video_frames: real frames from a video (YouTube's own storyboard sprites), so the agent can SEE the editing style:
  screen recording, iPad handwriting, static slides, talking head or animation
"""

from __future__ import annotations

import html as htmllib
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

from .. import db
from .base import BROWSER_UA, ProviderError, _cache_key

YT = "https://www.youtube.com"
HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
COOKIES = {"CONSENT": "YES+1", "SOCS": "CAI"}  # skips the EU cookie wall, which has no data in it
CHANNELS_ONLY = "EgIQAg%3D%3D"  # YouTube's "Type: Channel" search filter
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}


async def _get(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS, cookies=COOKIES) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise ProviderError(f"YouTube: network error ({exc.__class__.__name__})") from exc
    if response.status_code == 404:
        raise ProviderError(f"YouTube: not found ({url})")
    if response.status_code == 429:
        raise ProviderError("YouTube: too many requests right now. Wait a few minutes.")
    if response.status_code >= 400:
        raise ProviderError(f"YouTube: HTTP {response.status_code} for {url}")
    return response


def embedded_json(html: str, name: str) -> dict[str, Any] | None:
    """The JSON YouTube assigns to `var <name> = {...};` in a page."""
    match = re.search(rf"(?:var\s+)?{re.escape(name)}\s*=\s*\{{", html)
    if not match:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(html, match.end() - 1)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def walk(node: Any, key: str) -> Iterator[dict[str, Any]]:
    """Every value stored under `key`, anywhere in the tree."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for k, v in item.items():
                if k == key and isinstance(v, dict):
                    yield v
                stack.append(v)
        elif isinstance(item, list):
            stack.extend(reversed(item))


def text(node: Any) -> str:
    """YouTube's text shapes: "x", {"simpleText": "x"}, {"runs": [{"text": "x"}]} or {"content": "x"}."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return str(node["simpleText"])
    if "content" in node:
        return str(node["content"])
    return "".join(str(r.get("text", "")) for r in node.get("runs", []) if isinstance(r, dict))


def channel_path(value: str) -> str:
    """A channel URL path from a handle ("@x" or "x"), a channel id ("UC…") or any channel/video-free URL."""
    value = value.strip()
    match = re.search(r"youtube\.com/((?:@[\w.-]+)|(?:channel/UC[\w-]{22})|(?:c/[\w.-]+)|(?:user/[\w.-]+))", value)
    if match:
        return "/" + match.group(1)
    if re.fullmatch(r"UC[\w-]{22}", value):
        return f"/channel/{value}"
    handle = value.lstrip("@").strip("/")
    if not re.fullmatch(r"[\w.-]{2,100}", handle):
        raise ValueError(f"Not a YouTube channel: {value!r}. Pass a handle like @name, a channel id or a channel URL.")
    return f"/@{handle}"


def video_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[\w-]{11}", value):
        return value
    match = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})", value)
    if not match:
        raise ValueError(f"Not a YouTube video: {value!r}. Pass a video URL or its 11-character id.")
    return match.group(1)


def parse_search(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for ch in walk(data, "channelRenderer"):
        cid = ch.get("channelId")
        if not cid:
            continue
        base = (((ch.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("canonicalBaseUrl") or "")
        counts = [text(ch.get(k)) for k in ("subscriberCountText", "videoCountText")]
        subscribers = next((c for c in counts if "subscriber" in c.lower()), "")
        handle = next((c for c in counts if c.startswith("@")), "") or (base[1:] if base.startswith("/@") else "")
        out.append({
            "channel_id": cid, "name": text(ch.get("title")), "handle": handle or None,
            "url": f"{YT}/{handle}" if handle else f"{YT}/channel/{cid}",
            "subscribers": subscribers or None, "subscribers_count": count(subscribers),
            "description": text(ch.get("descriptionSnippet"))[:300] or None,
        })
    return out


def count(label: str | None) -> int | None:
    """'189K subscribers' -> 189000."""
    match = re.search(r"([\d.,]+)\s*([KMB])?", label or "", re.I)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(number * {"k": 1e3, "m": 1e6, "b": 1e9}.get((match.group(2) or "").lower(), 1))


async def search_channels(query: str, limit: int = 20, region: str | None = None) -> list[dict[str, Any]]:
    key = _cache_key("yt-search", query, region, limit)
    hit = db.cache_get(key)
    if hit is not None:
        return hit
    params = {"search_query": query, "sp": CHANNELS_ONLY.replace("%3D", "=")}
    if region:
        params["gl"] = region.split(":")[0].upper()
    page = await _get(f"{YT}/results", params)
    data = embedded_json(page.text, "ytInitialData")
    if not data:
        raise ProviderError("YouTube: the search page had no results data (layout changed or blocked).")
    found = parse_search(data)[: max(1, min(limit, 50))]
    db.cache_set(key, found, 12)
    return found


def parse_about(data: dict[str, Any] | None, html: str) -> dict[str, Any]:
    about = next(walk(data or {}, "aboutChannelViewModel"), {}) if data else {}
    links = []
    for link in walk(about, "channelExternalLinkViewModel"):
        links.append({"title": text(link.get("title")), "url": text(link.get("link"))})
    meta = re.search(r'<meta name="description" content="([^"]*)"', html)
    title = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    description = about.get("description") or (meta.group(1) if meta else "")
    cid = about.get("channelId") or (re.search(r'"(?:externalId|channelId)":"(UC[\w-]{22})"', html) or [None, None])[1]
    return {
        "channel_id": cid,
        "name": htmllib.unescape(title.group(1)) if title else None,
        "url": about.get("canonicalChannelUrl") or None,
        "country": about.get("country") or None,
        "subscribers": text(about.get("subscriberCountText")) or None,
        "subscribers_count": count(text(about.get("subscriberCountText"))),
        "videos": text(about.get("videoCountText")) or None,
        "views": text(about.get("viewCountText")) or None,
        "joined": text(about.get("joinedDateText")).replace("Joined ", "") or None,
        "description": htmllib.unescape(description)[:1500],
        "links": links,
        "emails_in_description": sorted(set(e.lower() for e in EMAIL_RE.findall(description))),
    }


def parse_feed(xml: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for entry in root.findall("a:entry", NS):
        vid = entry.findtext("yt:videoId", "", NS)
        link = (entry.find("a:link", NS).get("href") if entry.find("a:link", NS) is not None else "") or ""
        stats = entry.find("media:group/media:community/media:statistics", NS)
        out.append({
            "video_id": vid, "title": entry.findtext("a:title", "", NS),
            "url": link or f"{YT}/watch?v={vid}", "short": "/shorts/" in link,
            "published": (entry.findtext("a:published", "", NS) or "")[:10],
            "views": int(stats.get("views")) if stats is not None and (stats.get("views") or "").isdigit() else None,
            "description": (entry.findtext("media:group/media:description", "", NS) or "")[:200],
        })
    return out


def cadence(uploads: list[dict[str, Any]], today: datetime | None = None) -> dict[str, Any]:
    """How actively they post, from the latest uploads (the feed holds the newest 15)."""
    today = today or datetime.now(timezone.utc)
    dates = [datetime.fromisoformat(u["published"]).replace(tzinfo=timezone.utc) for u in uploads if u.get("published")]
    last = max(dates) if dates else None
    return {
        "last_upload": last.date().isoformat() if last else None,
        "days_since_last_upload": (today - last).days if last else None,
        "uploads_last_30_days": sum(d >= today - timedelta(days=30) for d in dates),
        "uploads_last_90_days": sum(d >= today - timedelta(days=90) for d in dates),
        "shorts_in_latest": sum(bool(u.get("short")) for u in uploads),
        "latest_checked": len(uploads),
    }


async def channel(handle_or_url: str) -> dict[str, Any]:
    path = channel_path(handle_or_url)
    key = _cache_key("yt-channel", path)
    hit = db.cache_get(key)
    if hit is not None:
        return hit
    page = await _get(f"{YT}{path}/about")
    info = parse_about(embedded_json(page.text, "ytInitialData"), page.text)
    if not info["channel_id"]:
        raise ProviderError(f"YouTube: couldn't read the channel at {YT}{path}.")
    info["url"] = info["url"] or f"{YT}{path}"
    feed = await _get(f"{YT}/feeds/videos.xml", {"channel_id": info["channel_id"]})
    uploads = parse_feed(feed.text)
    info["activity"] = cadence(uploads)
    info["recent_uploads"] = uploads[:12]
    long_form = [u for u in uploads if not u["short"]]
    info["next_step"] = ("Call youtube_video_frames on one or two of these (prefer a long lesson, not a Short) to see "
                         "the editing style before you judge it." if long_form or uploads else
                         "No public uploads found.")
    db.cache_set(key, info, 12)
    return info


def storyboard_sheets(spec: str, length_seconds: int, want: int = 2) -> list[dict[str, Any]]:
    """URLs of `want` storyboard sprite sheets spread through the video, from the best-quality level.

    spec = "<base url with $L and $N>|<level 0>|<level 1>|…", level = "w#h#frames#cols#rows#interval_ms#name#sigh".
    """
    parts = spec.split("|")
    base, levels = parts[0], [p.split("#") for p in parts[1:] if p.count("#") >= 7]
    if not levels:
        return []
    level = len(levels) - 1
    width, height, frames, cols, rows, interval, name, sigh = levels[level][:8]
    per_sheet = max(1, int(cols) * int(rows))
    total = max(1, math.ceil(int(frames) / per_sheet))
    step_s = (int(interval) / 1000) if int(interval) else (length_seconds / max(1, int(frames)))
    # Skip the intro (often a branded animation that isn't the lesson itself): sample from 15% and 60% in.
    picks = sorted({min(total - 1, int(total * f)) for f in ((0.15, 0.6) if want >= 2 else (0.4,))})
    sheets = []
    for index in picks:
        url = base.replace("$L", str(level)).replace("$N", name.replace("$M", str(index)))
        url += ("&" if "?" in url else "?") + f"sigh={sigh}"
        start = index * per_sheet * step_s
        end = min(length_seconds, (index + 1) * per_sheet * step_s)
        sheets.append({"url": url, "grid": f"{cols}x{rows}", "frame_size": f"{width}x{height}",
                       "covers": f"{_clock(start)}-{_clock(end)}", "seconds_between_frames": round(step_s, 1)})
    return sheets


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"


async def video_frames(video: str, sheets: int = 2) -> tuple[dict[str, Any], list[tuple[bytes, str]]]:
    """Video facts plus image bytes: storyboard sheets (grids of real frames), or the thumbnail as a fallback."""
    vid = video_id(video)
    page = await _get(f"{YT}/watch", {"v": vid})
    player = embedded_json(page.text, "ytInitialPlayerResponse") or {}
    details = player.get("videoDetails") or {}
    if not details:
        raise ProviderError(f"YouTube: couldn't read video {vid} (private, removed or age-restricted).")
    micro = ((player.get("microformat") or {}).get("playerMicroformatRenderer") or {})
    length = int(details.get("lengthSeconds") or 0)
    info: dict[str, Any] = {
        "video_id": vid, "url": f"{YT}/watch?v={vid}", "title": details.get("title"),
        "channel": details.get("author"), "channel_id": details.get("channelId"),
        "length": _clock(length), "views": int(details["viewCount"]) if str(details.get("viewCount", "")).isdigit() else None,
        "published": (micro.get("publishDate") or "")[:10] or None,
        "description": (details.get("shortDescription") or "")[:800],
        "keywords": (details.get("keywords") or [])[:15],
    }
    spec = (((player.get("storyboards") or {}).get("playerStoryboardSpecRenderer") or {}).get("spec") or "")
    images: list[tuple[bytes, str]] = []
    plan = storyboard_sheets(spec, length, max(1, min(sheets, 3))) if spec else []
    for sheet in plan:
        try:
            img = await _get(sheet["url"])
        except ProviderError:
            continue
        kind = (img.headers.get("content-type") or "image/jpeg").split("/")[-1].split(";")[0]
        images.append((img.content, "jpeg" if kind == "jpg" else kind))
    if images:
        info["frames"] = [{k: s[k] for k in ("grid", "covers", "seconds_between_frames")} for s in plan[: len(images)]]
        info["how_to_read"] = ("Each image is a grid of real frames from this video, left to right, top to bottom, at the "
                               "times in `frames`. Judge the format from them: screen recording, iPad/handwritten notes, "
                               "static slides, talking head, whiteboard, or produced animation/motion graphics.")
    else:
        for quality in ("maxresdefault", "hqdefault"):
            try:
                img = await _get(f"https://i.ytimg.com/vi/{vid}/{quality}.jpg")
            except ProviderError:
                continue
            images.append((img.content, "jpeg"))
            break
        info["how_to_read"] = ("No frames were available, only the thumbnail. A thumbnail is designed artwork, so it "
                               "doesn't prove the lesson's editing style: say the style is unverified.")
    return info, images
