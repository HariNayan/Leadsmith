"""Free email finding: company email pattern + MX check (no paid finder).

Guessed emails are labelled "guessed" so the user knows the risk. Bounces are
detected later by check_replies and mark the contact as bounced.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..db import normalize_domain
from .base import request_json

GENERIC = {
    "info", "hello", "hi", "hey", "contact", "sales", "support", "help", "team", "admin", "office", "careers",
    "jobs", "press", "media", "privacy", "security", "legal", "billing", "accounts", "marketing", "partners",
    "partnerships", "investors", "ir", "noreply", "no-reply", "webmaster", "mail", "enquiries", "inquiries",
    "feedback", "founders", "community", "events", "abuse", "dpo", "gdpr", "compliance", "hr", "recruiting",
}
PATTERNS = ["{f}", "{f}.{l}", "{f}{l}", "{fi}{l}", "{f}_{l}", "{fi}.{l}", "{f}{li}", "{l}"]


def _clean(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", name.lower())


async def mx_records(domain: str) -> list[str]:
    data = await request_json("GET", "https://dns.google/resolve", provider="DNS",
                              params={"name": normalize_domain(domain), "type": "MX"}, cache_hours=24 * 7)
    return [a["data"].split()[-1].rstrip(".") for a in data.get("Answer") or [] if a.get("type") == 15]


def infer_pattern(known_emails: list[str]) -> str | None:
    """Guess the company's pattern from public, personal-looking emails."""
    for email in known_emails or []:
        local = email.split("@")[0].lower()
        if local in GENERIC or any(ch.isdigit() for ch in local):
            continue
        if re.fullmatch(r"[a-z]+\.[a-z]+", local):
            return "{f}.{l}"
        if re.fullmatch(r"[a-z]+_[a-z]+", local):
            return "{f}_{l}"
        if re.fullmatch(r"[a-z]\.[a-z]+", local):
            return "{fi}.{l}"
        if re.fullmatch(r"[a-z]{2,12}", local):
            return "{f}"
    return None


async def candidates(domain: str, first_name: str, last_name: str | None = None,
                     known_emails: list[str] | None = None) -> dict[str, Any]:
    domain = normalize_domain(domain)
    f, l = _clean(first_name), _clean(last_name or "")
    mx = await mx_records(domain)
    provider = "google" if any("google" in m for m in mx) else "microsoft" if any("outlook" in m for m in mx) else (
        "other" if mx else None)
    inferred = infer_pattern(known_emails or [])
    order = ([inferred] if inferred else []) + [p for p in PATTERNS if p != inferred]
    out = []
    for pattern in order:
        if "{l}" in pattern or "{li}" in pattern:
            if not l:
                continue
        local = pattern.format(f=f, l=l, fi=f[:1], li=l[:1])
        if local and f:
            out.append({"email": f"{local}@{domain}", "pattern": pattern,
                        "confidence": "medium" if pattern == inferred else "low"})
    return {
        "domain": domain,
        "accepts_email": bool(mx),
        "mail_provider": provider,
        "inferred_pattern": inferred,
        "candidates": out[:4],
        "note": ("No MX records: this domain can't receive email." if not mx else
                 "Guesses are unverified: save the best one with email_status 'guessed'. "
                 "Bounces are detected by check_replies."),
    }


# ── published emails: find a real address before guessing one ──────────────

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Disguised addresses. Bracketed "at" is unambiguous: "jane [at] acme [dot] com", "jane(at)acme.com".
# A plain word "at" only counts with a spelled-out "dot" ("jane at acme dot com"), so prose like
# "director at Eventbrite. Simon" is never read as an address. A real "." must touch both sides.
_LABEL = r"[A-Za-z0-9\-]+"
_SPELLED_DOT = r"(?:\s*\[\s*dot\s*\]\s*|\s*\(\s*dot\s*\)\s*|\s*\{\s*dot\s*\}\s*|\s+dot\s+)"
_BRACKET_AT = r"(?:\s*\[\s*at\s*\]\s*|\s*\(\s*at\s*\)\s*|\s*\{\s*at\s*\}\s*)"
OBFUSCATED_RES = (
    re.compile(rf"\b([A-Za-z0-9._%+\-]{{2,}}){_BRACKET_AT}({_LABEL}(?:(?:{_SPELLED_DOT}|\.){_LABEL})+)", re.I),
    re.compile(rf"\b([A-Za-z0-9._%+\-]{{2,}})\s+at\s+({_LABEL}(?:{_SPELLED_DOT}{_LABEL})+)", re.I),
)
JUNK_DOMAINS = ("example.com", "sentry.io", "wixpress.com", "domain.com", "email.com", "yourdomain.com", "sentry-next.wixpress.com")
NO_FETCH_HOSTS = ("linkedin.com",)


def _deobfuscate(local: str, rest: str) -> str | None:
    domain = re.sub(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+)\s*", ".", rest, flags=re.I)
    domain = re.sub(r"\s+", "", domain).strip(".").lower()
    email = f"{local.lower()}@{domain}"
    return email if EMAIL_RE.fullmatch(email) and "." in domain else None


def extract_emails(text: str) -> list[str]:
    """Plain, mailto: and disguised addresses, minus images and placeholders."""
    import html as htmllib

    text = htmllib.unescape(text or "")
    found = [e.lower().rstrip(".") for e in EMAIL_RE.findall(text)]
    for pattern in OBFUSCATED_RES:
        found += [e for e in (_deobfuscate(a, b) for a, b in pattern.findall(text)) if e]
    clean = []
    for email in found:
        domain = email.split("@")[1]
        if domain.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")) or domain in JUNK_DOMAINS:
            continue
        if email not in clean:
            clean.append(email)
    return clean


def _name_match(email: str, first: str | None, last: str | None) -> bool:
    local = _clean(email.split("@")[0])
    f, l = _clean(first or ""), _clean(last or "")
    return bool((f and len(f) > 2 and f in local) or (l and len(l) > 2 and l in local))


async def emails_on_page(url: str, first_name: str | None = None, last_name: str | None = None) -> dict[str, Any]:
    from .base import fetch_text

    url = url if url.startswith("http") else f"https://{url}"
    host = normalize_domain(url)
    if any(host == h or host.endswith("." + h) for h in NO_FETCH_HOSTS):
        return {"url": url, "emails": [], "note": (
            "LinkedIn only shows email addresses to logged-in 1st-degree connections, so profiles aren't fetched. "
            "Use the profile's search-result snippet (headline/About) and look for the person's website, GitHub, "
            "YouTube About page, X bio, newsletter or talk/podcast pages instead.")}
    status, final_url, body = await fetch_text(url, timeout=15)
    if status != 200 or not body:
        return {"url": url, "emails": [], "note": f"Could not read the page (HTTP {status})."}
    emails = extract_emails(body)
    return {
        "url": final_url,
        "emails": [{"email": e, "matches_person": _name_match(e, first_name, last_name),
                    "generic": e.split("@")[0] in GENERIC} for e in emails[:20]],
        "note": None if emails else "No email address published on this page.",
    }


async def github_email(username_or_url: str) -> dict[str, Any]:
    """A GitHub user's public profile email, website and socials (free API, 60 requests/hour)."""
    m = re.search(r"github\.com/([A-Za-z0-9-]+)", username_or_url)
    user = m.group(1) if m else username_or_url.strip().lstrip("@")
    data = await request_json("GET", f"https://api.github.com/users/{user}", provider="GitHub",
                              headers={"Accept": "application/vnd.github+json"}, cache_hours=24 * 7)
    return {
        "username": data.get("login"),
        "name": data.get("name"),
        "email": data.get("email"),
        "company": data.get("company"),
        "website": data.get("blog") or None,
        "twitter": data.get("twitter_username"),
        "bio": data.get("bio"),
        "note": None if data.get("email") else "No public email on this GitHub profile. Try their website (above).",
    }
