"""Hacker News "Ask HN: Who is hiring?" — free, no key. Hundreds of companies
post every month with role, location and website: a strong hiring signal for
tech-buying ICPs."""

from __future__ import annotations

import html as htmllib
import re
from typing import Any

from ..db import normalize_domain
from .base import request_json

ALGOLIA = "https://hn.algolia.com/api/v1"


async def _threads(months: int) -> list[dict[str, Any]]:
    data = await request_json(
        "GET", f"{ALGOLIA}/search_by_date", provider="Hacker News",
        params={"tags": "story,author_whoishiring", "hitsPerPage": 12}, cache_hours=6,
    )
    threads = [h for h in data.get("hits", []) if (h.get("title") or "").startswith("Ask HN: Who is hiring?")]
    return threads[: max(1, months)]


def _parse(comment_html: str) -> dict[str, Any]:
    first, _, rest = comment_html.partition("<p>")
    links = [htmllib.unescape(u) for u in re.findall(r'href="([^"]+)"', comment_html)]
    header = re.sub(r"<[^>]+>", "", htmllib.unescape(first)).strip()
    parts = [p.strip() for p in header.split("|") if p.strip()]
    domain = next((normalize_domain(u) for u in links if "ycombinator.com" not in u), None)
    body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", htmllib.unescape(rest))).strip()
    return {"company": parts[0] if parts else None, "header": header[:300], "domain": domain,
            "links": links[:5], "text": body[:700]}


async def who_is_hiring(keywords: list[str] | None = None, months: int = 1, limit: int = 40) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for thread in await _threads(months):
        story_id = thread["objectID"]
        for keyword in keywords or [""]:
            data = await request_json(
                "GET", f"{ALGOLIA}/search", provider="Hacker News",
                params={"tags": f"comment,story_{story_id}", "query": keyword, "hitsPerPage": min(limit, 100)},
                cache_hours=6,
            )
            for hit in data.get("hits", []):
                if str(hit.get("parent_id")) != str(story_id) or hit["objectID"] in seen:
                    continue
                seen.add(hit["objectID"])
                item = _parse(hit.get("comment_text") or "")
                item.update({"posted": (hit.get("created_at") or "")[:10], "thread": thread.get("title"),
                             "hn_url": f"https://news.ycombinator.com/item?id={hit['objectID']}"})
                out.append(item)
                if len(out) >= limit:
                    return out
    return out
