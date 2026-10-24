"""Google News RSS — free company news and funding signals, no key needed."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

from .base import ProviderError, fetch_text

FUNDING_TERMS = '("raises" OR "raised" OR "funding" OR "seed round" OR "Series A" OR "Series B" OR "Series C")'


async def google_news(query: str, days_back: int = 30, limit: int = 15, region: str = "US:en") -> list[dict]:
    country, _, lang = region.partition(":")
    q = quote_plus(f"{query} when:{max(1, days_back)}d")
    url = f"https://news.google.com/rss/search?q={q}&hl={lang or 'en'}-{country}&gl={country}&ceid={country}:{lang or 'en'}"
    status, _, text = await fetch_text(url, timeout=20)
    if status != 200 or not text:
        raise ProviderError(f"Google News: could not fetch results (HTTP {status})")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ProviderError("Google News: unreadable response") from exc
    items = []
    for item in root.iter("item"):
        published = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(published).date().isoformat() if published else None
        except (TypeError, ValueError):
            pass
        source = item.find("source")
        items.append({
            "title": item.findtext("title"),
            "url": item.findtext("link"),
            "published": published,
            "source": source.text if source is not None else None,
        })
        if len(items) >= limit:
            break
    return items
