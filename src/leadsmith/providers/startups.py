"""Startup discovery from public startup sites, free and without keys.

- Wellfound: startups by location, and startups hiring a role (with their job posts: a hiring signal). Each comes with
  its website, one-line pitch, size, markets and badges, from the data Wellfound puts in its own pages.
- BetaList: new startups by market, with their description and real website (through BetaList's "visit" link).
- Product Hunt: the latest launches from its public feed (its website blocks automated visitors, so websites are
  found by searching the product name).
F6S blocks automated visitors and AltHunt is a directory of other startup directories: the agents use those through
web search (see the signal-scout playbook).
"""

from __future__ import annotations

import asyncio
import html as htmllib
import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from .. import db
from .base import BROWSER_UA, ProviderError, _cache_key

HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
SIZES = {"SIZE_1_10": "1-10", "SIZE_11_50": "11-50", "SIZE_51_200": "51-200", "SIZE_201_500": "201-500",
         "SIZE_501_1000": "501-1000", "SIZE_1001_5000": "1001-5000", "SIZE_5000_PLUS": "5000+"}


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


async def _get(client: httpx.AsyncClient, url: str, provider: str) -> httpx.Response:
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ProviderError(f"{provider}: network error ({exc.__class__.__name__})") from exc
    if response.status_code in (403, 429) or "Just a moment" in response.text[:3000]:
        raise ProviderError(f"{provider}: the site is blocking automated visits right now. Use web search for it instead.")
    if response.status_code >= 400:
        raise ProviderError(f"{provider}: HTTP {response.status_code} for {url}")
    return response


def _cached(key: str) -> Any:
    return db.cache_get(key)


# ── Wellfound ────────────────────────────────────────────────────────────

def apollo_state(page: str) -> dict[str, Any]:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return {}
    return (((data.get("props") or {}).get("pageProps") or {}).get("apolloState") or {}).get("data") or {}


def _names(value: Any, state: dict[str, Any]) -> list[str]:
    """Display names from a list of tags or refs ({"__ref": "MarketTag:fintech"} or {"displayName": ...})."""
    out = []
    for item in value or []:
        node = state.get(item.get("__ref"), item) if isinstance(item, dict) else {}
        name = node.get("displayName") or node.get("name") or node.get("label")
        if name:
            out.append(str(name))
    return out


def parse_wellfound(state: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = {k: v for k, v in state.items() if k.startswith("JobListingSearchResult")}
    out = []
    for key, s in state.items():
        if not key.startswith("StartupResult"):
            continue
        refs = [ref.get("__ref") for value in s.values() if isinstance(value, list)
                for ref in value if isinstance(ref, dict) and str(ref.get("__ref", "")).startswith("JobListingSearchResult")]
        posts = []
        for ref in refs:
            j = jobs.get(ref) or {}
            posted = j.get("liveStartAt")
            posts.append({
                "title": j.get("title"),
                "posted": datetime.fromtimestamp(posted, timezone.utc).date().isoformat() if isinstance(posted, (int, float)) else None,
                "remote": j.get("remote"), "locations": j.get("locationNames") or [],
                "pay": j.get("compensation") or None,
                "url": f"https://wellfound.com/jobs/{j.get('id')}-{j.get('slug')}" if j.get("id") else None,
            })
        badges = [b.get("__ref", "").split(":")[1].split("-")[0].replace("_", " ").lower()
                  for b in s.get("badges") or [] if isinstance(b, dict) and ":" in b.get("__ref", "")]
        counts = s.get("jobListingCounts") or {}
        out.append({
            "name": s.get("name"), "website": s.get("companyUrl"),
            "domain": db.normalize_domain(s.get("companyUrl") or "") or None,
            "pitch": s.get("highConcept"), "description": (s.get("description") or "")[:400] or None,
            "size": SIZES.get(s.get("companySize") or "", s.get("companySize")),
            "markets": _names(s.get("marketTaggings"), state)[:6], "locations": _names(s.get("locationTaggings"), state)[:4],
            "badges": badges, "open_jobs": counts.get("total") if isinstance(counts, dict) else None,
            "jobs": [p for p in posts if p["title"]][:5],
            "wellfound_url": f"https://wellfound.com/company/{s.get('slug')}" if s.get("slug") else None,
        })
    return out


async def wellfound(role: str | None = None, location: str | None = None, page: int = 1) -> dict[str, Any]:
    if role:
        path = f"/role/l/{slug(role)}/{slug(location)}" if location else f"/role/{slug(role)}"
    else:
        path = f"/startups/location/{slug(location)}" if location else "/startups"
    url = "https://wellfound.com" + path + (f"?page={page}" if page > 1 else "")
    key = _cache_key("wellfound", url)
    hit = _cached(key)
    if hit is not None:
        return hit
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=HEADERS) as client:
        response = await _get(client, url, "Wellfound")
    startups = parse_wellfound(apollo_state(response.text))
    landed = str(response.url)
    result = {
        "url": landed, "startups": startups, "count": len(startups),
        "note": ("Wellfound didn't recognise that role or place and showed its general list instead: try the role as "
                 "Wellfound names it (e.g. 'marketing-manager', 'software-engineer') or a bigger city."
                 if not landed.rstrip("/").endswith(path.rstrip("/")) else
                 "Startups hiring for this role, with their job posts: a hiring signal (cite the job url and date). "
                 "Wellfound leaves out websites on these results and blocks its company pages, so find each "
                 "startup's website with a web search of its name and pitch. page=2, 3… for more."
                 if role else "Big, well-known companies come first on place pages: check size against the ICP. "
                              "page=2, 3… for more."),
    }
    db.cache_set(key, result, 12)
    return result


# ── BetaList ─────────────────────────────────────────────────────────────

def parse_betalist(page: str) -> list[str]:
    seen: list[str] = []
    for path in re.findall(r'href="(/startups/[a-z0-9-]+)"', page):
        if path not in seen:
            seen.append(path)
    return seen


async def _betalist_one(client: httpx.AsyncClient, path: str, sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        item: dict[str, Any] = {"betalist_url": "https://betalist.com" + path}
        try:
            page = (await client.get("https://betalist.com" + path)).text
            title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
            desc = re.search(r'<meta (?:property|name)="(?:og:)?description" content="([^"]*)"', page)
            # og:title is "Name: tagline" (or "Name - tagline | BetaList"): keep the name.
            raw = htmllib.unescape(title.group(1)) if title else path.rsplit("/", 1)[1]
            item["name"] = re.split(r"\s*(?::|\s-\s|\|)\s*", raw, maxsplit=1)[0].strip()
            item["description"] = htmllib.unescape(desc.group(1))[:300] if desc else None
            visit = await client.get("https://betalist.com" + path + "/visit")
            if visit.status_code < 400 and "betalist.com" not in str(visit.url):
                item["website"] = str(visit.url).split("?")[0]
                item["domain"] = db.normalize_domain(item["website"])
        except httpx.HTTPError:
            pass
        return item


async def betalist(market: str | None = None, limit: int = 20) -> dict[str, Any]:
    url = f"https://betalist.com/markets/{slug(market)}" if market else "https://betalist.com/"
    key = _cache_key("betalist", url, limit)
    hit = _cached(key)
    if hit is not None:
        return hit
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS) as client:
        response = await _get(client, url, "BetaList")
        paths = parse_betalist(response.text)[: max(1, min(limit, 40))]
        sem = asyncio.Semaphore(5)
        startups = await asyncio.gather(*(_betalist_one(client, p, sem) for p in paths))
    result = {"url": str(response.url), "startups": list(startups), "count": len(startups),
              "note": "New startups, most recent first. Markets are BetaList's own (e.g. saas, fintech, "
                      "artificial-intelligence, e-commerce, health, education, developer-tools)."}
    db.cache_set(key, result, 12)
    return result


# ── Product Hunt ─────────────────────────────────────────────────────────

def parse_ph_feed(xml: str) -> list[dict[str, Any]]:
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        def tag(name: str) -> str:
            m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", entry, re.S)
            return htmllib.unescape(m.group(1)).strip() if m else ""
        link = re.search(r'<link[^>]+href="([^"]+)"', entry)
        content = re.sub(r"<[^>]+>", " ", tag("content"))
        tagline = re.sub(r"\s+", " ", content.split("Discussion")[0]).strip()
        out.append({"name": tag("title"), "tagline": tagline[:200], "launched": tag("published")[:10],
                    "maker": tag("name") or None, "product_hunt_url": link.group(1) if link else None})
    return out


async def product_hunt(query: str | None = None, limit: int = 30) -> dict[str, Any]:
    key = _cache_key("producthunt-feed")
    launches = _cached(key)
    if launches is None:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=HEADERS) as client:
            response = await _get(client, "https://www.producthunt.com/feed", "Product Hunt")
        launches = parse_ph_feed(response.text)
        db.cache_set(key, launches, 3)
    if query:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        launches = [l for l in launches if any(w in (l["name"] + " " + l["tagline"]).lower() for w in words)]
    return {"launches": launches[: max(1, min(limit, 50))], "count": len(launches),
            "note": "The latest launches (the public feed holds about the last 50). Product Hunt blocks automated "
                    "visits to product pages, so find each product's website with a web search of its name and "
                    "tagline. For older or topic launches, web search `site:producthunt.com/products <topic>`."}
