"""Y Combinator company directory — free, no key. ~5,000 active startups with
team size, industries, regions and an is-hiring badge."""

from __future__ import annotations

from typing import Any

from ..db import normalize_domain
from .base import request_json

BASE = "https://api.ycombinator.com/v0.1/companies"


def _match(values: list[str], wanted: list[str] | None) -> bool:
    if not wanted:
        return True
    hay = " ".join(values).lower()
    return any(w.lower() in hay for w in wanted)


async def search(
    query: str | None = None,
    industries: list[str] | None = None,
    regions: list[str] | None = None,
    hiring_only: bool = False,
    min_team: int | None = None,
    max_team: int | None = None,
    limit: int = 30,
    max_pages: int = 8,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        data = await request_json(
            "GET", BASE, provider="YC directory",
            params={"q": query, "page": page, "isHiring": "true" if hiring_only else None},
            cache_hours=24,
        )
        for c in data.get("companies") or []:
            team = c.get("teamSize") or 0
            hiring = "isHiring" in (c.get("badges") or [])
            if c.get("status") not in (None, "Active"):
                continue
            if hiring_only and not hiring:
                continue
            if min_team and team < min_team or max_team and team and team > max_team:
                continue
            if not _match((c.get("industries") or []) + (c.get("tags") or []), industries):
                continue
            if not _match((c.get("regions") or []) + (c.get("locations") or []), regions):
                continue
            results.append({
                "company": c.get("name"),
                "domain": normalize_domain(c.get("website") or "") or None,
                "one_liner": c.get("oneLiner"),
                "team_size": team or None,
                "batch": c.get("batch"),
                "industries": c.get("industries"),
                "tags": c.get("tags"),
                "locations": c.get("locations"),
                "hiring": hiring,
                "yc_url": c.get("url"),
            })
            if len(results) >= limit:
                return results
        if page >= (data.get("totalPages") or 0):
            break
    return results
