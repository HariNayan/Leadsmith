"""Free account research straight from a company's own website and job board.

No API keys. Reads the homepage and a few well-known pages, detects the tech
stack, socials, pricing and freshness, and pulls open roles from the public
Greenhouse, Lever and Ashby job-board APIs (the ATS most startups use).
"""

from __future__ import annotations

import asyncio
import html as htmllib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx

from ..db import normalize_domain
from . import techdetect
from .base import BROWSER_UA, fetch_text

EXTRA_PAGES = ("/about", "/team", "/pricing", "/customers", "/careers", "/jobs", "/blog")
SOCIAL_PATTERNS = {
    "linkedin": r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[A-Za-z0-9_\-%.]+",
    "twitter": r"https?://(?:www\.)?(?:twitter|x)\.com/(?!share|intent|home)[A-Za-z0-9_]{2,}",
    "github": r"https?://(?:www\.)?github\.com/[A-Za-z0-9_\-]+",
    "youtube": r"https?://(?:www\.)?youtube\.com/(?:@|c/|channel/)[A-Za-z0-9_\-]+",
    "instagram": r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+",
    "facebook": r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.\-]+",
}
ATS_PATTERNS = {
    "greenhouse": r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_\-]+)",
    "lever": r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.\-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([A-Za-z0-9_.\-%]+)",
    "workable": r"apply\.workable\.com/([A-Za-z0-9_\-]+)",
    "recruitee": r"([A-Za-z0-9_\-]+)\.recruitee\.com",
    "personio": r"([A-Za-z0-9_\-]+)\.jobs\.personio\.(?:de|com)",
    "bamboohr": r"([A-Za-z0-9_\-]+)\.bamboohr\.com/(?:careers|jobs)",
}
AI_CRAWLERS = ("GPTBot", "ClaudeBot", "anthropic-ai", "PerplexityBot", "Google-Extended", "CCBot")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def visible_text(html: str, limit: int = 2500) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", htmllib.unescape(html)).strip()
    return text[:limit]


def blocked_crawlers(robots: str) -> list[str]:
    """AI crawlers that robots.txt blocks from the whole site."""
    groups: list[tuple[list[str], list[str]]] = []
    agents: list[str] = []
    rules: list[str] = []
    last_was_agent = False
    for raw in robots.splitlines():
        line = raw.split("#")[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key.lower() == "user-agent":
            if not last_was_agent and agents:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            last_was_agent = True
        else:
            last_was_agent = False
            if key.lower() == "disallow":
                rules.append(value)
    if agents:
        groups.append((agents, rules))
    blocked = [
        bot for bot in AI_CRAWLERS
        if any(bot.lower() in group_agents and "/" in group_rules for group_agents, group_rules in groups)
    ]
    if any("*" in group_agents and "/" in group_rules for group_agents, group_rules in groups):
        blocked.append("* (all crawlers)")
    return blocked


def _title(html: str) -> str | None:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return re.sub(r"\s+", " ", htmllib.unescape(m.group(1))).strip() if m else None


def _role_summary(jobs: list[dict[str, Any]]) -> dict[str, int]:
    """Count open roles by function so a 700-role board still reads as a signal."""
    buckets = {
        "sales": r"\b(account executive|sales|sdr|bdr|business development|ae)\b",
        "marketing": r"\b(marketing|growth|demand gen|content|brand|seo|community)\b",
        "customer success/support": r"\b(customer success|support|account manager|onboarding|implementation)\b",
        "engineering": r"\b(engineer|developer|sre|devops|architect|software)\b",
        "data/ai": r"\b(data|machine learning|ml|ai|analytics|scientist)\b",
        "product/design": r"\b(product manager|product designer|designer|ux|ui)\b",
        "finance/legal/ops": r"\b(finance|accountant|legal|counsel|operations|revops|people|recruit|hr)\b",
        "leadership": r"\b(head of|vp|vice president|director|chief|cxo|cto|cmo|cro|coo)\b",
    }
    counts: dict[str, int] = {}
    for job in jobs:
        title = (job.get("title") or "").lower()
        for name, pattern in buckets.items():
            if re.search(pattern, title):
                counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _meta(html: str, name: str) -> str | None:
    for pattern in (
        rf'<meta[^>]+(?:name|property)=["\']{name}["\'][^>]*content=["\']([^"\']*)',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\']{name}["\']',
    ):
        m = re.search(pattern, html, re.I)
        if m:
            return htmllib.unescape(m.group(1)).strip() or None
    return None


async def _ats_jobs(client: httpx.AsyncClient, ats: str, token: str) -> list[dict[str, Any]] | None:
    """Fetch open roles from a public ATS job board. None = board not found."""
    try:
        if ats == "greenhouse":
            r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
            if r.status_code != 200:
                return None
            return [
                {"title": j.get("title"), "location": (j.get("location") or {}).get("name"),
                 "updated": (j.get("updated_at") or "")[:10] or None, "url": j.get("absolute_url")}
                for j in r.json().get("jobs", [])
            ]
        if ats == "lever":
            r = await client.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
            if r.status_code != 200 or not isinstance(r.json(), list):
                return None
            out = []
            for j in r.json():
                created = j.get("createdAt")
                out.append({
                    "title": j.get("text"),
                    "location": (j.get("categories") or {}).get("location"),
                    "team": (j.get("categories") or {}).get("team"),
                    "updated": datetime.fromtimestamp(created / 1000, timezone.utc).date().isoformat() if created else None,
                    "url": j.get("hostedUrl"),
                })
            return out
        if ats == "ashby":
            r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
            if r.status_code != 200:
                return None
            return [
                {"title": j.get("title"), "location": j.get("location"), "team": j.get("department"),
                 "updated": (j.get("publishedAt") or "")[:10] or None, "url": j.get("jobUrl")}
                for j in r.json().get("jobs", [])
            ]
    except (httpx.HTTPError, ValueError):
        return None
    return None


async def open_jobs(domain: str, html_hints: str = "") -> dict[str, Any]:
    """Find a company's public job board (Greenhouse, Lever, Ashby) and list open roles."""
    domain = normalize_domain(domain)
    candidates: list[tuple[str, str]] = []
    for ats, pattern in ATS_PATTERNS.items():
        for token in re.findall(pattern, html_hints, re.I):
            if (ats, token) not in candidates:
                candidates.append((ats, token))
    detected_other = [c for c in candidates if c[0] not in ("greenhouse", "lever", "ashby")]
    fetchable = [c for c in candidates if c[0] in ("greenhouse", "lever", "ashby")]
    stem = domain.split(".")[0]
    for ats in ("greenhouse", "lever", "ashby"):
        if (ats, stem) not in fetchable:
            fetchable.append((ats, stem))
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": BROWSER_UA}) as client:
        for ats, token in fetchable:
            jobs = await _ats_jobs(client, ats, token)
            if jobs is not None and (jobs or (ats, token) in candidates):
                return {"ats": ats, "board": token, "open_roles": len(jobs), "by_function": _role_summary(jobs),
                        "newest": sorted(jobs, key=lambda j: j.get("updated") or "", reverse=True)[:40]}
    if detected_other:
        ats, token = detected_other[0]
        return {"ats": ats, "board": token, "open_roles": None, "newest": [],
                "note": f"Uses {ats}; roles aren't readable via a public API. Open the careers page to see them."}
    return {"ats": None, "open_roles": None, "newest": [],
            "note": "No public Greenhouse/Lever/Ashby board found. Check the careers page with WebFetch."}


async def scan(domain: str) -> dict[str, Any]:
    domain = normalize_domain(domain)
    status, final_url, home = await fetch_text(f"https://{domain}")
    if status == 0 or not home:
        status, final_url, home = await fetch_text(f"http://{domain}")
    if not home:
        return {"domain": domain, "reachable": False, "note": "Website did not respond."}

    pages = await asyncio.gather(*(fetch_text(urljoin(final_url, p), timeout=12) for p in EXTRA_PAGES))
    found_pages = {
        path: url for path, (code, url, body) in zip(EXTRA_PAGES, pages)
        if code == 200 and body and normalize_domain(url) == normalize_domain(final_url)
        and url.rstrip("/") != final_url.rstrip("/")
    }
    all_html = home + " ".join(body for _, _, body in pages)

    robots_status, _, robots = await fetch_text(urljoin(final_url, "/robots.txt"), timeout=10)
    blocked_ai = blocked_crawlers(robots) if robots_status == 200 else []

    sitemap_status, _, sitemap = await fetch_text(urljoin(final_url, "/sitemap.xml"), timeout=12)
    lastmods = sorted(re.findall(r"<lastmod>([0-9]{4}-[0-9]{2}-[0-9]{2})", sitemap)) if sitemap_status == 200 else []

    socials: dict[str, str] = {}
    for name, pattern in SOCIAL_PATTERNS.items():
        m = re.search(pattern, all_html, re.I)
        if m:
            socials[name] = m.group(0)

    emails = sorted({
        e.lower() for e in EMAIL_RE.findall(all_html)
        if normalize_domain(e.split("@")[1]).endswith(domain) and not e.lower().endswith((".png", ".jpg", ".svg", ".webp"))
    })[:10]

    jobs = await open_jobs(domain, all_html)
    pricing_text = None
    if "/pricing" in found_pages:
        idx = EXTRA_PAGES.index("/pricing")
        pricing_text = visible_text(pages[idx][2], 1200)
    about_text = None
    if "/about" in found_pages:
        about_text = visible_text(pages[EXTRA_PAGES.index("/about")][2], 1500)
    team_text = None
    if "/team" in found_pages:
        team_text = visible_text(pages[EXTRA_PAGES.index("/team")][2], 2000)

    return {
        "domain": domain,
        "reachable": True,
        "url": final_url,
        "title": _title(home),
        "description": _meta(home, "description") or _meta(home, "og:description"),
        "homepage_text": visible_text(home, 2500),
        "about_text": about_text,
        "team_text": team_text,
        "pricing_text": pricing_text,
        "pages_found": found_pages,
        "tech_stack": techdetect.detect(home),
        "socials": socials,
        "public_emails": emails,
        "has_structured_data": "application/ld+json" in home,
        "blocks_ai_crawlers": blocked_ai,
        "sitemap_pages": sitemap.count("<loc>") if sitemap_status == 200 else None,
        "content_last_updated": lastmods[-1] if lastmods else None,
        "hiring": jobs,
    }
