"""Flags leads in places with stricter cold-email rules (EU/EEA, UK, Switzerland, Canada).

A flag is a prompt to check, not a block and not legal advice. Region comes from the lead's time zone (saved, or
guessed from its location), then from the website or email country domain.
"""
from __future__ import annotations

from typing import Any

from . import schedule

EU_ZONES = {
    "Europe/Amsterdam", "Europe/Athens", "Europe/Berlin", "Europe/Bratislava", "Europe/Brussels", "Europe/Bucharest",
    "Europe/Budapest", "Europe/Copenhagen", "Europe/Dublin", "Europe/Helsinki", "Europe/Lisbon", "Europe/Ljubljana",
    "Europe/Luxembourg", "Europe/Madrid", "Europe/Malta", "Europe/Paris", "Europe/Prague", "Europe/Riga", "Europe/Rome",
    "Europe/Sofia", "Europe/Stockholm", "Europe/Tallinn", "Europe/Vienna", "Europe/Vilnius", "Europe/Warsaw",
    "Europe/Zagreb", "Europe/Mariehamn", "Asia/Nicosia", "Europe/Nicosia", "Asia/Famagusta", "Atlantic/Canary",
    "Atlantic/Madeira", "Atlantic/Azores", "Africa/Ceuta",
    # EEA: same GDPR rules
    "Europe/Oslo", "Atlantic/Reykjavik", "Europe/Vaduz",
}
UK_ZONES = {"Europe/London", "Europe/Belfast", "Europe/Guernsey", "Europe/Jersey", "Europe/Isle_of_Man"}
CH_ZONES = {"Europe/Zurich"}
CA_ZONES = {"America/Toronto", "America/Montreal", "America/Vancouver", "America/Edmonton", "America/Winnipeg",
            "America/Halifax", "America/St_Johns", "America/Regina", "America/Moncton", "America/Whitehorse",
            "America/Yellowknife", "America/Iqaluit", "America/Glace_Bay", "America/Goose_Bay"}
EU_TLDS = {"at", "be", "bg", "hr", "cy", "cz", "dk", "ee", "fi", "fr", "de", "gr", "hu", "ie", "it", "lv", "lt", "lu",
           "mt", "nl", "pl", "pt", "ro", "sk", "si", "es", "se", "eu", "no", "is", "li"}

RULES = {
    "EU": ("EU (GDPR)",
           "GDPR plus each country's ePrivacy law. Many EU countries allow B2B cold email under legitimate interest "
           "with a clear opt-out, but some (Germany, for one) expect prior consent. If they ask, say where you got "
           "their details, and honour any objection straight away."),
    "UK": ("UK (PECR)",
           "UK GDPR and PECR. Emailing someone at their company address is allowed without prior consent if you say "
           "who you are and offer an easy opt-out. Sole traders and partnerships count as individuals and need consent."),
    "CH": ("Switzerland",
           "Swiss unfair-competition law needs prior consent for mass marketing email. A single, relevant email to one "
           "person is lower risk; always say who you are and offer an easy opt-out."),
    "CA": ("Canada (CASL)",
           "CASL needs consent. Implied consent can cover a business address they published themselves (with no "
           "'no unsolicited email' note) when your email is relevant to their role. Include your name, postal address "
           "and an unsubscribe."),
}


def region(account: dict[str, Any] | None, email: str | None = None) -> dict[str, str] | None:
    account = account or {}
    zone = schedule.valid_zone(account.get("timezone")) and account.get("timezone") \
        or schedule.guess_timezone(account.get("location"))
    code = None
    if zone:
        code = ("EU" if zone in EU_ZONES else "UK" if zone in UK_ZONES else "CH" if zone in CH_ZONES
                else "CA" if zone in CA_ZONES else None)
    if not code and not zone:
        for host in (account.get("domain") or "", (email or "").split("@")[-1]):
            tld = host.rsplit(".", 1)[-1].lower() if "." in host else ""
            code = "EU" if tld in EU_TLDS else "UK" if tld == "uk" else "CH" if tld == "ch" else "CA" if tld == "ca" else None
            if code:
                break
    if not code:
        return None
    label, rule = RULES[code]
    return {"code": code, "label": label, "rule": rule}
