"""Where a business or person is, anywhere in the world. Free, no keys.

- geocode: any address, postcode or place name -> neighbourhood, borough/district, city, region, country
  (OpenStreetMap Nominatim: max 1 request a second, results cached, attribution required)
- site_address: the postal address a business publishes on its own website: structured data (schema.org
  PostalAddress) and its legal/contact pages (Impressum, mentions légales, aviso legal, note legali, contact, about)
"""

from __future__ import annotations

import asyncio
import html as htmllib
import json
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .. import db
from .base import ProviderError, _cache_key, fetch_text

NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "leadsmith/0.2 (local, self-hosted prospecting tool)"
_LOCK = asyncio.Lock()
_LAST = [0.0]

# Pages where businesses publish their address, in the languages it's most often required by law.
LEGAL_WORDS = ("impressum", "imprint", "legal-notice", "legal", "mentions-legales", "mentions", "aviso-legal",
               "note-legali", "colofon", "kontakt", "contact", "contacto", "contatti", "about", "ueber-uns", "uber-uns",
               "a-propos", "chi-siamo", "sobre", "terms", "agb", "datenschutz", "privacy")

# Postcode shapes that, next to a street or a city name, usually mean an address.
POSTCODES = [
    r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b",            # UK
    r"\b[A-Z]\d[A-Z] ?\d[A-Z]\d\b",                      # Canada
    r"\b\d{4} ?[A-Z]{2}\b",                              # Netherlands
    r"\b(?:[A-Z]{2}|[A-Z][a-z]+),? \d{5}(?:-\d{4})?\b",  # US: "CA 94107" / "Texas 75001"
    r"\b\d{5}\s+[A-ZÄÖÜÉÈ][\w'’.-]+",                    # DE/FR/ES/IT/…: "10961 Berlin", "75011 Paris"
    r"\b\d{3} ?\d{3}\b",                                 # India PIN, Singapore
    r"\b\d{4}\s+[A-ZÄÖÜÉ][\w-]+",                        # AT/CH/BE/DK/NO/AU: "8001 Zürich"
]
_POSTCODE_RE = re.compile("|".join(f"(?:{p})" for p in POSTCODES))
_NOISE = re.compile(r"(?i)©|€|\$|£|₹|\b(?:tel|phone|fax|copyright|all rights|rights reserved|droits|rechte|vat|"
                    r"ust-?id|iban|bic|price|isbn)\b")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def _addr_fields(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "neighbourhood": a.get("neighbourhood") or a.get("quarter") or a.get("suburb"),
        "borough": a.get("borough") or a.get("city_district") or a.get("suburb"),
        "city": a.get("city") or a.get("town") or a.get("village") or a.get("municipality"),
        "region": a.get("state") or a.get("state_district") or a.get("region"),
        "postcode": a.get("postcode"), "country": a.get("country"), "country_code": (a.get("country_code") or "").upper(),
    }


def simpler_queries(query: str) -> list[str]:
    """Shorter versions of a long address that Nominatim can match: it fails on flat numbers, building names and
    landmarks, but finds the postcode + locality or the last few parts."""
    parts = [p.strip() for p in re.split(r",|\n", query) if p.strip()]
    tries = []
    code = next((m.group() for m in _POSTCODE_RE.finditer(query) if not _YEAR.fullmatch(m.group().split()[0])), None)
    if code:
        country = parts[-1] if len(parts) > 1 and not re.search(r"\d", parts[-1]) else ""
        tries.append(", ".join(p for p in (code, country) if p))
    if len(parts) > 3:
        tries.append(", ".join(parts[-3:]))
    if len(parts) > 2:
        tries.append(", ".join(parts[-2:]))
    return [t for t in dict.fromkeys(tries) if t.lower() != query.lower()]


async def geocode(query: str) -> dict[str, Any]:
    """Geocode, retrying with simpler forms of a long address. The result says which form matched."""
    result = await _geocode_once(query)
    if result["found"]:
        return result
    for simpler in simpler_queries(query):
        retry = await _geocode_once(simpler)
        if retry["found"]:
            return {**retry, "query": query, "matched_on": simpler,
                    "note": "Matched on part of the address, so trust the city and district, not the street."}
    return result


async def _geocode_once(query: str) -> dict[str, Any]:
    query = re.sub(r"\s+", " ", (query or "")).strip()
    if len(query) < 3:
        raise ValueError("Give an address, postcode or place name to look up.")
    key = _cache_key("geocode", query.lower())
    hit = db.cache_get(key)
    if hit is not None:
        return hit
    async with _LOCK:  # Nominatim's usage policy: at most one request per second
        wait = 1.1 - (time.monotonic() - _LAST[0])
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": NOMINATIM_UA,
                                                               "Accept-Language": "en"}) as client:
                response = await client.get(NOMINATIM, params={"q": query, "format": "jsonv2", "addressdetails": 1,
                                                               "limit": 3})
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenStreetMap: network error ({exc.__class__.__name__})") from exc
        finally:
            _LAST[0] = time.monotonic()
    if response.status_code in (403, 429):
        raise ProviderError("OpenStreetMap: too many lookups right now. Wait a minute.")
    if response.status_code >= 400:
        raise ProviderError(f"OpenStreetMap: HTTP {response.status_code}")
    matches = []
    for item in response.json() or []:
        matches.append({"display": item.get("display_name"), **_addr_fields(item.get("address") or {}),
                        "kind": f"{item.get('category') or item.get('class')}/{item.get('type')}",
                        "osm_url": f"https://www.openstreetmap.org/{item.get('osm_type')}/{item.get('osm_id')}"})
    result = {"query": query, "found": bool(matches), "best": matches[0] if matches else None, "others": matches[1:],
              "source": "© OpenStreetMap contributors (Nominatim)",
              "note": None if matches else "No match. Try fewer words: the postcode and city, or the street and city."}
    db.cache_set(key, result, 24 * 30)
    return result


def jsonld_addresses(page: str) -> list[str]:
    """schema.org PostalAddress values from a page's JSON-LD."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "PostalAddress" in kinds:
                parts = [node.get(k) for k in ("streetAddress", "postalCode", "addressLocality", "addressRegion",
                                               "addressCountry")]
                parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
                text = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
                if text:
                    found.append(text)
            elif isinstance(node.get("address"), str) and node.get("address").strip():
                found.append(node["address"].strip())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            walk(json.loads(htmllib.unescape(block.strip())))
        except ValueError:
            continue
    return list(dict.fromkeys(found))


def page_lines(page: str) -> list[str]:
    page = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|address|dd|dt|span)>", "\n", page)
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", page))
    return [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]


def text_addresses(page: str, limit: int = 6) -> list[str]:
    """Address-looking snippets: a line with a postcode, joined with the line before it (usually the street)."""
    lines = page_lines(page)
    out: list[str] = []
    for i, line in enumerate(lines):
        if len(line) > 160 or _NOISE.search(line):
            continue
        # A year is not a postcode: "2026 Jonas Keil" mustn't read as "8001 Zürich".
        if not any(not _YEAR.fullmatch(m.group().split()[0]) for m in _POSTCODE_RE.finditer(line)):
            continue
        if not re.search(r"[A-Za-zÀ-ÿ]{3,}", line):
            continue
        prev = lines[i - 1] if i and len(lines[i - 1]) <= 80 and not _NOISE.search(lines[i - 1]) else ""
        street_like = re.search(r"\d", prev) or re.search(r"(?i)str(\.|asse|aße)|street|road|rue|via|calle|avenue|lane|marg|nagar", prev)
        snippet = f"{prev}, {line}" if prev and street_like else line
        snippet = re.sub(r"\s*,\s*", ", ", snippet).strip(", ")
        if snippet not in out:
            out.append(snippet)
        if len(out) >= limit:
            break
    return out


def legal_links(page: str, base_url: str, limit: int = 5) -> list[str]:
    host = urlparse(base_url).netloc.replace("www.", "")
    scored: dict[str, int] = {}
    for href, label in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
        url = urljoin(base_url, htmllib.unescape(href))
        if urlparse(url).netloc.replace("www.", "") != host:
            continue
        hay = (url + " " + re.sub(r"<[^>]+>", " ", label)).lower().replace("é", "e").replace("ü", "u")
        for rank, word in enumerate(LEGAL_WORDS):
            if word in hay:
                scored[url.split("?")[0]] = min(scored.get(url.split("?")[0], 99), rank)
                break
    return [u for u, _ in sorted(scored.items(), key=lambda kv: kv[1])][:limit]


async def site_address(domain: str, geocode_top: int = 3) -> dict[str, Any]:
    """The address a business publishes on its own site, with the page it's on, geocoded."""
    site = domain if domain.startswith("http") else f"https://{domain}"
    status, final, home = await fetch_text(site)
    if not home:
        raise ProviderError(f"{domain}: the site didn't load (HTTP {status or 'no response'}).")
    pages = [(final, home)]
    for url in legal_links(home, final):
        code, url_final, text = await fetch_text(url)
        if text and code < 400:
            pages.append((url_final, text))
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url, text in pages:
        for address in jsonld_addresses(text):
            if address.lower() not in seen:
                seen.add(address.lower())
                candidates.append({"address": address, "from": url, "how": "structured data (schema.org)"})
        for address in text_addresses(text):
            if address.lower() not in seen:
                seen.add(address.lower())
                candidates.append({"address": address, "from": url, "how": "page text"})
    # Structured data and legal pages first: they're the addresses businesses must keep accurate.
    candidates.sort(key=lambda c: (c["how"] != "structured data (schema.org)",
                                   not any(w in c["from"].lower() for w in LEGAL_WORDS[:9])))
    for cand in candidates[:geocode_top]:
        try:
            place = await geocode(cand["address"])
        except ProviderError:
            continue
        cand["place"] = place["best"]
    return {
        "domain": domain, "pages_checked": [u for u, _ in pages], "addresses": candidates[:8],
        "how_to_read": "Each address is quoted from the page in `from`. `place` is where OpenStreetMap puts it "
                       "(borough/district, city, country). Page-text matches can be noise: only cite an address that "
                       "reads as the business's own (an Impressum, legal notice or contact block). A registered or "
                       "legal address can be an accountant or a virtual office, not where the person works.",
        "found": bool(candidates),
    }
