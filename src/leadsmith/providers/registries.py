"""Official company registers, free and without keys.

- UK: Companies House (companies_house.py): registered office and active directors
- France: recherche-entreprises.api.gouv.fr: head-office address, activity and directors (sole traders too)
- Worldwide: GLEIF, the global LEI register: legal and headquarters address of any entity with an LEI
Other countries have no free open register API: `search_hint` says where to look with WebSearch instead.
"""

from __future__ import annotations

import re
from typing import Any

from . import companies_house
from .base import request_json

FRANCE = "https://recherche-entreprises.api.gouv.fr/search"
GLEIF = "https://api.gleif.org/api/v1"

# Where to find company records by web search, for countries without a free register API.
SEARCH_HINTS = {
    "DE": 'site:northdata.de "<name>" (Handelsregister: address, managing directors)',
    "AT": 'site:firmenabc.at OR site:northdata.de "<name>"',
    "CH": 'site:zefix.ch OR site:moneyhouse.ch "<name>"',
    "NL": 'site:openkvk.nl OR site:drimble.nl "<name>"',
    "ES": 'site:infoempresa.com OR site:einforma.com "<name>"',
    "IT": 'site:registroimprese.it OR site:ufficiocamerale.it "<name>"',
    "IN": 'site:zaubacorp.com OR site:tofler.in OR site:thecompanycheck.com "<name>" (MCA: registered address, directors)',
    "US": 'site:opencorporates.com "<name>" or the state Secretary of State business search',
    "CA": 'site:opencorporates.com OR site:canadacompanyregistry.com "<name>"',
    "AU": 'site:abr.business.gov.au "<name>" (ABN Lookup: state and postcode)',
    "NZ": 'site:opencorporates.com "<name>" (NZ Companies Office)',
    "SG": 'site:sgpbusiness.com OR site:opencorporates.com "<name>" (ACRA)',
    "AE": 'site:opencorporates.com "<name>" or the free-zone authority directory',
}
DEFAULT_HINT = 'site:opencorporates.com "<name>" (covers 140+ registers)'


def _gb(code: str | None) -> bool:
    return (code or "").upper() in ("GB", "UK", "UNITED KINGDOM", "ENGLAND", "SCOTLAND", "WALES")


async def france(query: str, limit: int = 5) -> list[dict[str, Any]]:
    data = await request_json("GET", FRANCE, provider="French company register",
                              params={"q": query, "per_page": min(limit, 10)}, cache_hours=24)
    out = []
    for item in data.get("results") or []:
        siege = item.get("siege") or {}
        siren = item.get("siren")
        out.append({
            "name": item.get("nom_complet"), "siren": siren,
            "active": item.get("etat_administratif") == "A", "created": item.get("date_creation"),
            "activity_code": item.get("activite_principale"),
            "address": siege.get("adresse"), "postcode": siege.get("code_postal"), "city": siege.get("libelle_commune"),
            "directors": [" ".join(p for p in (d.get("prenoms"), d.get("nom")) if p) or d.get("denomination")
                          for d in (item.get("dirigeants") or [])][:6],
            "url": f"https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}" if siren else None,
        })
    return out


def _gleif_address(addr: dict[str, Any] | None) -> str:
    addr = addr or {}
    parts = [*(addr.get("addressLines") or []), addr.get("postalCode"), addr.get("city"), addr.get("region"),
             addr.get("country")]
    return ", ".join(str(p) for p in parts if p)


def same_name(query: str, legal_name: str) -> bool:
    """GLEIF's fuzzy search returns look-alikes ("Trendtech Ltd" for "Trendytech"): keep only names that contain
    every word of the query, ignoring legal suffixes and punctuation."""
    def words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
                if w not in {"ltd", "limited", "llc", "llp", "inc", "gmbh", "sas", "sarl", "pvt", "private", "the", "co"}}
    wanted = words(query)
    return bool(wanted) and wanted <= words(legal_name)


async def gleif(name: str, limit: int = 3) -> list[dict[str, Any]]:
    hits = await request_json("GET", f"{GLEIF}/fuzzycompletions", provider="GLEIF",
                              params={"field": "entity.legalName", "q": name}, cache_hours=24)
    ids = []
    for hit in hits.get("data") or []:
        lei = ((((hit.get("relationships") or {}).get("lei-records") or {}).get("data")) or {}).get("id")
        if lei and lei not in ids and same_name(name, (hit.get("attributes") or {}).get("value") or ""):
            ids.append(lei)
    out = []
    for lei in ids[:limit]:
        record = await request_json("GET", f"{GLEIF}/lei-records/{lei}", provider="GLEIF", cache_hours=24)
        attrs = (record.get("data") or {}).get("attributes") or {}
        entity = attrs.get("entity") or {}
        out.append({
            "name": (entity.get("legalName") or {}).get("name"), "lei": lei,
            "status": entity.get("status"), "jurisdiction": entity.get("jurisdiction"),
            "legal_address": _gleif_address(entity.get("legalAddress")),
            "headquarters": _gleif_address(entity.get("headquartersAddress")),
            "registered_as": entity.get("registeredAs"),
            "url": f"https://search.gleif.org/#/record/{lei}",
        })
    return out


async def lookup(company: str | None = None, person: str | None = None, number: str | None = None,
                 country: str | None = None) -> dict[str, Any]:
    if not (company or person or number):
        raise ValueError("Pass a company name, a person's name or a company number.")
    code = (country or "").strip().upper()
    if _gb(code):
        code = "GB"
    result: dict[str, Any] = {"country": code or None}
    if code == "GB":
        result.update(await companies_house.lookup(company, person, number))
        result["register"] = "UK Companies House"
    elif code == "FR":
        result["register"] = "French national register (INSEE/INPI via recherche-entreprises)"
        result["companies"] = await france(number or company or person or "")
        result["how_to_read"] = ("Searching a person's name also finds sole traders (entrepreneurs individuels) and "
                                 "companies they direct. The head-office address of a sole trader is often their home "
                                 "town. Confirm the match by the director's name and activity. Cite `url`.")
    else:
        result["register"] = "GLEIF (worldwide LEI register: only entities that have an LEI)"
    if company and code != "FR":
        try:
            result["lei_records"] = await gleif(company)
        except Exception:  # noqa: BLE001 - GLEIF is a bonus here, never the reason a lookup fails
            result["lei_records"] = []
    if code not in ("GB", "FR"):
        result["search_hint"] = (SEARCH_HINTS.get(code, DEFAULT_HINT)).replace("<name>", company or person or "")
        result["how_to_read"] = (
            "Small businesses rarely have an LEI, so an empty `lei_records` proves nothing. Use `search_hint` with "
            "WebSearch to find the national register's record (search snippets are enough; cite the page). "
            "A registered address can be an accountant or a virtual office: it proves where the company is "
            "registered, not where the person works. Run `geocode` on an address to get its district and city.")
    return result
