"""Your own wording for each sequence step.

Each step is either written by the AI (optionally with an extra instruction from you) or uses your fixed template.
Fixed templates take {placeholders}; the ones we know are filled from the lead, and any others ({personal_line},
{observation}...) are left for the writer agent to fill with one sourced sentence each.
"""
from __future__ import annotations

import re
from typing import Any

from . import db

STEPS = (1, 2, 3)
STEP_NAMES = {1: "First email (day 0)", 2: "Follow-up (day 3)", 3: "Last note (day 7)"}

PLACEHOLDERS = {
    "first_name": "Their first name (\"there\" if unknown)",
    "last_name": "Their last name",
    "title": "Their job title",
    "company": "Company name",
    "domain": "Company website, e.g. acme.io",
    "why_now": "The reason to reach out now, from research",
    "ideas": "The A/B/C idea menu, one per line",
    "idea_a": "Idea A (title: pitch)",
    "idea_b": "Idea B",
    "idea_c": "Idea C",
    "picked_idea": "The idea they picked, if any",
}
FIELD_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def all_steps() -> list[dict[str, Any]]:
    saved = {t["step"]: t for t in db.step_templates()}
    blank = {"mode": "ai", "subject": None, "body": None, "instruction": None, "updated_at": None}
    return [{**blank, **saved.get(step, {}), "step": step, "name": STEP_NAMES[step]} for step in STEPS]


def customised() -> list[dict[str, Any]]:
    """Only the steps the user changed: a fixed template, or an AI step with an extra instruction."""
    return [t for t in all_steps() if t["mode"] == "fixed" or (t.get("instruction") or "").strip()]


def values_for(contact: dict[str, Any], account: dict[str, Any], ideas: list[dict[str, Any]]) -> dict[str, str]:
    lines = {i["letter"].upper(): f"{i['title']}: {i['pitch']}" if i.get("pitch") else i["title"] for i in ideas}
    picked = next((i for i in ideas if i.get("status") == "picked"), None)
    return {
        "first_name": (contact.get("first_name") or "").strip() or "there",
        "last_name": contact.get("last_name") or "",
        "title": contact.get("title") or "",
        "company": account.get("name") or contact.get("domain") or "",
        "domain": contact.get("domain") if db.has_website(contact.get("domain") or "") else "",
        "why_now": account.get("why_now") or "",
        "ideas": "\n".join(f"{letter}) {text}" for letter, text in sorted(lines.items())),
        "idea_a": lines.get("A", ""), "idea_b": lines.get("B", ""), "idea_c": lines.get("C", ""),
        "picked_idea": f"{picked['letter']}) {picked['title']}" if picked else "",
    }


def fill(text: str | None, values: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """Returns (filled text, placeholders left for the writer, known placeholders that came out empty)."""
    left: list[str] = []
    empty: list[str] = []

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in PLACEHOLDERS:
            if key not in left:
                left.append(key)
            return m.group(0)
        if not values.get(key) and key not in empty:
            empty.append(key)
        return values.get(key, "")

    return FIELD_RE.sub(sub, text or ""), left, empty


def render(contact_id: int, step: int) -> dict[str, Any]:
    template = next((t for t in all_steps() if t["step"] == step), None)
    if not template or template["mode"] != "fixed":
        return {"step": step, "mode": "ai", "instruction": (template or {}).get("instruction")}
    with db.connect() as conn:
        contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not contact:
            raise ValueError(f"No contact {contact_id}")
        contact = dict(contact)
        account = dict(conn.execute("SELECT * FROM accounts WHERE domain = ?", (contact["domain"],)).fetchone() or {})
        ideas = db.rows(conn.execute("SELECT letter, title, pitch, status FROM ideas WHERE domain = ? ORDER BY letter",
                                     (contact["domain"],)))
    values = values_for(contact, account, ideas)
    subject, left_s, empty_s = fill(template.get("subject"), values)
    body, left_b, empty_b = fill(template.get("body"), values)
    return {
        "step": step, "mode": "fixed", "subject": subject.strip() or None, "body": body.strip(),
        "fill_these": list(dict.fromkeys(left_s + left_b)),
        "empty": list(dict.fromkeys(empty_s + empty_b)),
    }
