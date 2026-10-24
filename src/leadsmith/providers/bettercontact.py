"""BetterContact: optional, paid, off unless you add an API key.

A waterfall across many email providers that returns a verified address. Leadsmith only calls it when you click
"Verify" on one person, never from the agents, so you decide where every credit goes. BetterContact charges only
when it finds a usable email (nothing for not-found or undeliverable). Docs: https://doc.bettercontact.rocks
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from .. import config

API = "https://app.bettercontact.rocks/api/v2"
GOOD = {"deliverable", "catch_all_safe"}
"""Statuses we treat as verified. Plain catch_all means the server accepts everything, so it stays 'guessed'."""


def enabled() -> bool:
    return bool(config.get("BETTERCONTACT_API_KEY"))


def _headers() -> dict[str, str]:
    return {"X-API-Key": config.get("BETTERCONTACT_API_KEY"), "Content-Type": "application/json"}


def credits() -> dict[str, Any]:
    r = httpx.get(f"{API}/account", headers=_headers(), timeout=20)
    if r.status_code in (401, 403):
        raise RuntimeError("BetterContact rejected the API key.")
    r.raise_for_status()
    return {"credits_left": r.json().get("credits_left")}


def find_email(first_name: str, last_name: str, domain: str | None, company: str | None = None,
               linkedin_url: str | None = None, wait_seconds: int = 120, poll_every: float = 4.0) -> dict[str, Any]:
    """Submit one lead and wait for the waterfall to finish. Returns email, status and credits."""
    if not enabled():
        raise RuntimeError("Add a BetterContact API key in Settings first.")
    if not (first_name and last_name):
        raise ValueError("BetterContact needs a first and last name.")
    lead: dict[str, Any] = {"first_name": first_name, "last_name": last_name}
    if domain:
        lead["company_domain"] = domain
    if company:
        lead["company"] = company
    if linkedin_url:
        lead["linkedin_url"] = linkedin_url
    if not (domain or company):
        raise ValueError("BetterContact needs a company or website.")
    r = httpx.post(f"{API}/async", headers=_headers(), json={"data": [lead], "enrich_email_address": True,
                                                              "enrich_phone_number": False}, timeout=30)
    if r.status_code in (401, 403):
        raise RuntimeError("BetterContact rejected the API key.")
    if r.status_code == 402:
        raise RuntimeError("BetterContact says you're out of credits.")
    r.raise_for_status()
    request_id = r.json().get("id")
    if not request_id:
        raise RuntimeError(f"BetterContact didn't return a request id: {r.text[:200]}")
    deadline = time.monotonic() + wait_seconds
    while True:
        time.sleep(poll_every)
        g = httpx.get(f"{API}/async/{request_id}", headers=_headers(), timeout=30)
        result = g.json() if g.status_code == 200 and g.content else {}
        if result.get("status") == "terminated":
            return parse(result, request_id)
        if time.monotonic() > deadline:
            # Don't resubmit: a second POST would spend credits again. The result can be fetched later by id.
            return {"done": False, "request_id": request_id,
                    "note": "BetterContact is still working on it. Try Verify again in a minute."}


def fetch(request_id: str) -> dict[str, Any] | None:
    g = httpx.get(f"{API}/async/{request_id}", headers=_headers(), timeout=30)
    result = g.json() if g.status_code == 200 and g.content else {}
    return parse(result, request_id) if result.get("status") == "terminated" else None


def parse(result: dict[str, Any], request_id: str) -> dict[str, Any]:
    lead = (result.get("data") or [{}])[0] or {}
    status = lead.get("contact_email_address_status") or "not_found"
    return {
        "done": True, "request_id": request_id,
        "email": (lead.get("contact_email_address") or "").strip().lower() or None,
        "status": status, "verified": status in GOOD,
        "linkedin_url": lead.get("contact_linkedin_profile_url"),
        "credits_consumed": result.get("credits_consumed"), "credits_left": result.get("credits_left"),
    }
