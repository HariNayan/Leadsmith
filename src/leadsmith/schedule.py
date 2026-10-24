"""When an email may go out: the prospect's business hours, on weekdays, in their time zone.

A cold email that lands at 3 AM or on a Saturday reads like a bot. Every send (except a reply to
someone who wrote to you) waits for the recipient's sending window, and follow-up gaps are counted
in business days.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config

# Lower-case place names -> IANA zone. Cities first (more precise), then states/regions, then countries.
CITIES = {
    "san francisco": "America/Los_Angeles", "sf": "America/Los_Angeles", "bay area": "America/Los_Angeles",
    "palo alto": "America/Los_Angeles", "mountain view": "America/Los_Angeles", "menlo park": "America/Los_Angeles",
    "san jose": "America/Los_Angeles", "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
    "san diego": "America/Los_Angeles", "seattle": "America/Los_Angeles", "portland": "America/Los_Angeles",
    "oakland": "America/Los_Angeles", "berkeley": "America/Los_Angeles", "vancouver": "America/Vancouver",
    "denver": "America/Denver", "boulder": "America/Denver", "salt lake city": "America/Denver", "phoenix": "America/Phoenix",
    "austin": "America/Chicago", "dallas": "America/Chicago", "houston": "America/Chicago", "chicago": "America/Chicago",
    "minneapolis": "America/Chicago", "nashville": "America/Chicago",
    "new york": "America/New_York", "nyc": "America/New_York", "brooklyn": "America/New_York", "boston": "America/New_York",
    "cambridge, ma": "America/New_York", "miami": "America/New_York", "atlanta": "America/New_York",
    "washington": "America/New_York", "philadelphia": "America/New_York", "pittsburgh": "America/New_York",
    "toronto": "America/Toronto", "montreal": "America/Toronto", "ottawa": "America/Toronto",
    "london": "Europe/London", "manchester": "Europe/London", "edinburgh": "Europe/London", "dublin": "Europe/Dublin",
    "paris": "Europe/Paris", "berlin": "Europe/Berlin", "munich": "Europe/Berlin", "hamburg": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam", "madrid": "Europe/Madrid", "barcelona": "Europe/Madrid", "lisbon": "Europe/Lisbon",
    "stockholm": "Europe/Stockholm", "copenhagen": "Europe/Copenhagen", "oslo": "Europe/Oslo", "helsinki": "Europe/Helsinki",
    "zurich": "Europe/Zurich", "geneva": "Europe/Zurich", "vienna": "Europe/Vienna", "warsaw": "Europe/Warsaw",
    "prague": "Europe/Prague", "milan": "Europe/Rome", "rome": "Europe/Rome", "athens": "Europe/Athens",
    "kyiv": "Europe/Kyiv", "kiev": "Europe/Kyiv", "tallinn": "Europe/Tallinn", "istanbul": "Europe/Istanbul",
    "tel aviv": "Asia/Jerusalem", "dubai": "Asia/Dubai", "abu dhabi": "Asia/Dubai", "riyadh": "Asia/Riyadh",
    "bangalore": "Asia/Kolkata", "bengaluru": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata", "gurgaon": "Asia/Kolkata", "gurugram": "Asia/Kolkata", "noida": "Asia/Kolkata",
    "pune": "Asia/Kolkata", "hyderabad": "Asia/Kolkata", "chennai": "Asia/Kolkata", "kolkata": "Asia/Kolkata",
    "ahmedabad": "Asia/Kolkata", "jaipur": "Asia/Kolkata", "surat": "Asia/Kolkata", "kochi": "Asia/Kolkata",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong", "tokyo": "Asia/Tokyo", "seoul": "Asia/Seoul",
    "shanghai": "Asia/Shanghai", "beijing": "Asia/Shanghai", "shenzhen": "Asia/Shanghai", "jakarta": "Asia/Jakarta",
    "manila": "Asia/Manila", "bangkok": "Asia/Bangkok", "kuala lumpur": "Asia/Kuala_Lumpur", "ho chi minh": "Asia/Ho_Chi_Minh",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne", "brisbane": "Australia/Brisbane",
    "perth": "Australia/Perth", "auckland": "Pacific/Auckland", "sao paulo": "America/Sao_Paulo",
    "são paulo": "America/Sao_Paulo", "mexico city": "America/Mexico_City", "buenos aires": "America/Argentina/Buenos_Aires",
    "bogota": "America/Bogota", "santiago": "America/Santiago", "lagos": "Africa/Lagos", "nairobi": "Africa/Nairobi",
    "cape town": "Africa/Johannesburg", "johannesburg": "Africa/Johannesburg", "cairo": "Africa/Cairo",
}
REGIONS = {
    "california": "America/Los_Angeles", ", ca": "America/Los_Angeles", "washington state": "America/Los_Angeles",
    "oregon": "America/Los_Angeles", "colorado": "America/Denver", "utah": "America/Denver", "arizona": "America/Phoenix",
    "texas": "America/Chicago", ", tx": "America/Chicago", "illinois": "America/Chicago", "new york": "America/New_York",
    ", ny": "America/New_York", "massachusetts": "America/New_York", ", ma": "America/New_York", "florida": "America/New_York",
    "georgia": "America/New_York", "pennsylvania": "America/New_York", "new jersey": "America/New_York",
    "ontario": "America/Toronto", "quebec": "America/Toronto", "british columbia": "America/Vancouver",
    "maharashtra": "Asia/Kolkata", "karnataka": "Asia/Kolkata", "gujarat": "Asia/Kolkata", "tamil nadu": "Asia/Kolkata",
}
COUNTRIES = {
    "united states": "America/New_York", "usa": "America/New_York", "us": "America/New_York", "america": "America/New_York",
    "canada": "America/Toronto", "united kingdom": "Europe/London", "uk": "Europe/London", "england": "Europe/London",
    "scotland": "Europe/London", "ireland": "Europe/Dublin", "germany": "Europe/Berlin", "france": "Europe/Paris",
    "netherlands": "Europe/Amsterdam", "spain": "Europe/Madrid", "portugal": "Europe/Lisbon", "italy": "Europe/Rome",
    "sweden": "Europe/Stockholm", "denmark": "Europe/Copenhagen", "norway": "Europe/Oslo", "finland": "Europe/Helsinki",
    "switzerland": "Europe/Zurich", "austria": "Europe/Vienna", "poland": "Europe/Warsaw", "belgium": "Europe/Brussels",
    "czech": "Europe/Prague", "greece": "Europe/Athens", "ukraine": "Europe/Kyiv", "estonia": "Europe/Tallinn",
    "romania": "Europe/Bucharest", "turkey": "Europe/Istanbul", "israel": "Asia/Jerusalem", "uae": "Asia/Dubai",
    "united arab emirates": "Asia/Dubai", "saudi arabia": "Asia/Riyadh", "india": "Asia/Kolkata",
    "singapore": "Asia/Singapore", "japan": "Asia/Tokyo", "south korea": "Asia/Seoul", "korea": "Asia/Seoul",
    "china": "Asia/Shanghai", "indonesia": "Asia/Jakarta", "philippines": "Asia/Manila", "thailand": "Asia/Bangkok",
    "malaysia": "Asia/Kuala_Lumpur", "vietnam": "Asia/Ho_Chi_Minh", "australia": "Australia/Sydney",
    "new zealand": "Pacific/Auckland", "brazil": "America/Sao_Paulo", "mexico": "America/Mexico_City",
    "argentina": "America/Argentina/Buenos_Aires", "colombia": "America/Bogota", "chile": "America/Santiago",
    "nigeria": "Africa/Lagos", "kenya": "Africa/Nairobi", "south africa": "Africa/Johannesburg", "egypt": "Africa/Cairo",
    "pakistan": "Asia/Karachi", "bangladesh": "Asia/Dhaka", "sri lanka": "Asia/Colombo", "nepal": "Asia/Kathmandu",
}


def guess_timezone(location: str | None) -> str | None:
    """'Pune, India' -> 'Asia/Kolkata'. None when the place isn't recognised."""
    text = f" {(location or '').lower()} "
    if not text.strip():
        return None
    for table in (CITIES, REGIONS, COUNTRIES):
        for name in sorted(table, key=len, reverse=True):  # "new york" before "york"
            if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", text):
                return table[name]
    return None


def valid_zone(name: str | None) -> str | None:
    if not name:
        return None
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return None


def your_zone() -> tzinfo:
    """The sender's zone: the setting if valid, else this computer's local offset."""
    configured = valid_zone(config.get("PROSPECT_TIMEZONE"))
    if configured:
        return ZoneInfo(configured)
    return datetime.now().astimezone().tzinfo or timezone.utc


def prospect_zone(account: dict[str, Any] | None) -> tuple[tzinfo, str]:
    """(zone, how we know): the account's saved zone, a guess from its location, or your default."""
    account = account or {}
    saved = valid_zone(account.get("timezone"))
    if saved:
        return ZoneInfo(saved), saved
    guessed = guess_timezone(account.get("location"))
    if guessed:
        return ZoneInfo(guessed), f"{guessed} (from location)"
    zone = your_zone()
    return zone, "your time zone (their location is unknown)"


def window() -> tuple[int, int, bool]:
    """(start hour, end hour, weekdays only) from settings, e.g. '9-17'."""
    m = re.match(r"\s*(\d{1,2})\s*[-–to]+\s*(\d{1,2})\s*$", config.get("SEND_HOURS") or "9-17")
    start, end = (int(m.group(1)), int(m.group(2))) if m else (9, 17)
    start, end = max(0, min(start, 23)), max(1, min(end, 24))
    if end <= start:
        start, end = 9, 17
    return start, end, config.get_bool("SEND_WEEKDAYS_ONLY", True)


def in_window(now_utc: datetime, zone: tzinfo) -> bool:
    start, end, weekdays = window()
    local = now_utc.astimezone(zone)
    if weekdays and local.weekday() >= 5:
        return False
    return start <= local.hour < end


def next_window_start(now_utc: datetime, zone: tzinfo) -> datetime:
    """The next moment (UTC) the window is open for this zone. Returns now if it already is."""
    if in_window(now_utc, zone):
        return now_utc
    start, _, weekdays = window()
    local = now_utc.astimezone(zone)
    candidate = local.replace(hour=start, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    while weekdays and candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def add_business_days(moment_utc: datetime, days: int, zone: tzinfo) -> datetime:
    """moment + N working days in the prospect's zone (Saturday and Sunday don't count)."""
    local = moment_utc.astimezone(zone)
    added = 0
    while added < days:
        local += timedelta(days=1)
        if local.weekday() < 5:
            added += 1
    return local.astimezone(timezone.utc)


def describe(moment_utc: datetime, zone: tzinfo) -> str:
    """'Mon 09:00 their time (Tue 19:30 yours)'."""
    theirs = moment_utc.astimezone(zone).strftime("%a %d %b %H:%M")
    yours = moment_utc.astimezone(datetime.now().astimezone().tzinfo).strftime("%a %H:%M")
    return f"{theirs} their time ({yours} yours)"
