"""Shared HTTP plumbing for every data provider: one client, caching, clear errors."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from .. import db

USER_AGENT = "Mozilla/5.0 (compatible; leadsmith/0.2)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


class ProviderError(Exception):
    """An error worth showing to the user as-is."""


def _cache_key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


async def request_json(
    method: str,
    url: str,
    *,
    provider: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    cache_hours: float = 0,
    timeout: float = 30,
) -> Any:
    """Call a JSON API. Cached responses never hit the network (or your credits) again."""
    key = _cache_key(method, url, params, body) if cache_hours else None
    if key:
        hit = db.cache_get(key)
        if hit is not None:
            return hit
    params = {k: v for k, v in (params or {}).items() if v not in (None, "", [])}
    body = {k: v for k, v in (body or {}).items() if v not in (None, "", [])} if body is not None else None
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.request(method, url, headers=headers, params=params, json=body)
    except httpx.HTTPError as exc:
        raise ProviderError(f"{provider}: network error ({exc.__class__.__name__}: {exc})") from exc
    if response.status_code in (401, 403):
        raise ProviderError(
            f"{provider}: the API key was rejected or your plan doesn't include this endpoint "
            f"(HTTP {response.status_code}). {response.text[:300]}"
        )
    if response.status_code == 429:
        raise ProviderError(f"{provider}: rate limit or credit limit reached. Try again later. {response.text[:200]}")
    if response.status_code >= 400:
        raise ProviderError(f"{provider}: HTTP {response.status_code}: {response.text[:400]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError(f"{provider}: response was not JSON") from exc
    if key:
        db.cache_set(key, data, cache_hours)
    return data


async def fetch_text(url: str, *, timeout: float = 15, max_bytes: int = 1_500_000) -> tuple[int, str, str]:
    """GET a web page. Returns (status, final_url, text). Never raises for HTTP errors."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.8"},
        ) as client:
            response = await client.get(url)
            return response.status_code, str(response.url), response.text[:max_bytes]
    except httpx.HTTPError:
        return 0, url, ""
