"""UK Companies House, free and without an API key: read from its public website.

Most UK creators, coaches and small businesses trade through a limited company, so this verifies who runs a business
and where it's registered. The registered office is often an accountant or a mail-forwarding service, not where the
person works: a known virtual-office address is flagged, and a match never proves a borough on its own.
"""

from __future__ import annotations

import html as htmllib
import re
from typing import Any

import httpx

from .. import db
from .base import BROWSER_UA, ProviderError, _cache_key

BASE = "https://find-and-update.company-information.service.gov.uk"

# Postcode areas fully inside Greater London. Outer areas (BR, CR, EN, HA, IG, KT, RM, SM, TW, UB, DA) straddle it.
LONDON_AREAS = {"E", "EC", "N", "NW", "SE", "SW", "W", "WC"}
OUTER_LONDON_AREAS = {"BR", "CR", "DA", "EN", "HA", "IG", "KT", "RM", "SM", "TW", "UB"}

# Mail-forwarding / formation-agent addresses used by thousands of companies. They prove nothing about location.
VIRTUAL_OFFICES = [
    "71-75 shelton street", "20-22 wenlock road", "128 city road", "124 city road", "160 city road", "kemp house",
    "27 old gloucester street", "86-90 paul street", "167-169 great portland street", "3rd floor 207 regent street",
    "85 great portland street", "1 kings avenue", "45 albemarle street", "275 new north road", "483 green lanes",
    "4 old park lane", "office 7, 35-37 ludgate hill", "suite 1, 7 ely place", "101 new cavendish street",
    "2nd floor college house", "17 hanover square", "60 tottenham court road", "unit 2 grand union studios",
]


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def postcode(address: str) -> str | None:
    match = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", (address or "").upper())
    return f"{match.group(1)} {match.group(2)}" if match else None


def place(address: str) -> dict[str, Any]:
    """What an address says about location: postcode, London yes/maybe/no, and whether it's a virtual office."""
    code = postcode(address)
    area = re.match(r"[A-Z]+", code).group() if code else ""
    lowered = re.sub(r"\s+", " ", (address or "").lower().replace(",", ""))
    virtual = any(v.replace(",", "") in lowered for v in VIRTUAL_OFFICES)
    if area in LONDON_AREAS:
        london = "yes"
    elif area in OUTER_LONDON_AREAS or (not code and "london" in lowered):
        london = "maybe"
    else:
        london = "no"
    return {"postcode": code, "london": london, "virtual_office": virtual}


async def _get(path: str, params: dict[str, Any] | None = None) -> str:
    key = _cache_key("companies-house", path, params)
    hit = db.cache_get(key)
    if hit is not None:
        return hit
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": BROWSER_UA}) as client:
            response = await client.get(BASE + path, params=params)
    except httpx.HTTPError as exc:
        raise ProviderError(f"Companies House: network error ({exc.__class__.__name__})") from exc
    if response.status_code == 404:
        raise ProviderError(f"Companies House: nothing at {path}")
    if response.status_code == 429:
        raise ProviderError("Companies House: too many requests. Wait a minute.")
    if response.status_code >= 400:
        raise ProviderError(f"Companies House: HTTP {response.status_code}")
    db.cache_set(key, response.text, 24)
    return response.text


def parse_results(page: str, kind: str) -> list[dict[str, Any]]:
    """Rows of a company or officer search."""
    out = []
    for item in re.findall(rf'<li class="type-{kind}">(.*?)</li>', page, re.S):
        link = re.search(r'href="(/(?:company|officers)/[^"]+)"[^>]*>(.*?)</a>', item, re.S)
        paras = [_clean(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", item, re.S)]
        if not link:
            continue
        address = paras[1] if len(paras) > 1 else ""
        row: dict[str, Any] = {"name": _clean(link.group(2)), "address": address, **place(address)}
        meta = paras[0] if paras else ""
        if kind == "company":
            row["number"] = link.group(1).rsplit("/", 1)[-1]
            row["incorporated"] = (re.search(r"Incorporated on (.+)$", meta) or [None, None])[1]
            row["dissolved"] = "Dissolved" in meta
        else:
            row["officer_id"] = link.group(1).split("/")[2]
            row["born"] = (re.search(r"Born (\w+ \d{4})", meta) or [None, None])[1]
            row["appointments"] = int((re.search(r"appointments (\d+)", meta) or [0, 0])[1])
        out.append(row)
    return out


def parse_company(page: str) -> dict[str, Any]:
    def field(name: str) -> str | None:
        match = re.search(rf'id="{name}"[^>]*>(.*?)</', page, re.S)
        return _clean(match.group(1)) if match else None

    address = field("roa-address") or ""
    return {
        "name": _clean((re.search(r'<p class="heading-xlarge"[^>]*>(.*?)</p>', page, re.S)
                        or re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S) or [None, ""])[1]) or None,
        "number": (field("company-number") or "").replace("Company number", "").strip() or None,
        "status": field("company-status"), "type": field("company-type"),
        "incorporated": field("company-creation-date"),
        "registered_office": address, **place(address),
        "sic": [field(f"sic{i}") for i in range(4) if field(f"sic{i}")],
    }


def parse_officers(page: str) -> list[dict[str, Any]]:
    out = []
    for n in sorted({int(i) for i in re.findall(r'id="officer-name-(\d+)"', page)}):
        def field(name: str) -> str | None:
            match = re.search(rf'id="officer-{name}-{n}"[^>]*>(.*?)</(?:dd|span|a|div|p)>', page, re.S)
            return _clean(match.group(1)) if match else None

        status = field("status-tag") or ""
        out.append({"name": field("name"), "role": field("role"), "active": status.lower() == "active",
                    "appointed": field("appointed-on"), "resigned": field("resigned-on"), "born": field("date-of-birth"),
                    "nationality": field("nationality"), "country_of_residence": field("country-of-residence"),
                    "correspondence_address": field("address-value")})
    return out


async def lookup(company: str | None = None, person: str | None = None, number: str | None = None,
                 limit: int = 5) -> dict[str, Any]:
    if not (company or person or number):
        raise ValueError("Pass a company name, a person's name or a company number.")
    result: dict[str, Any] = {}
    if number:
        number = number.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{8}", number):
            raise ValueError(f"{number!r} isn't a company number (8 characters, e.g. 09718346).")
        profile = parse_company(await _get(f"/company/{number}"))
        profile["officers"] = [o for o in parse_officers(await _get(f"/company/{number}/officers")) if o["active"]]
        profile["url"] = f"{BASE}/company/{number}"
        result["company"] = profile
    if company:
        result["companies"] = [r for r in parse_results(await _get("/search/companies", {"q": company}), "company")
                               if not r["dissolved"]][:limit]
    if person:
        result["officers"] = parse_results(await _get("/search/officers", {"q": person}), "officer")[:limit]
    result["how_to_read"] = (
        "A company search matches names loosely: confirm it's the right business by calling again with `number` and "
        "checking the person you're researching is an active officer. `london` comes from the postcode. A "
        "`virtual_office` address (a mail-forwarding service) and an accountant's address only prove the company is "
        "registered there, not where the person works. Never cite either as their borough. An officer's "
        "correspondence address is often the same service. Cite the company URL as the source.")
    return result
