"""The business profile: four markdown files every agent reads before acting."""

from __future__ import annotations

import re
from pathlib import Path

from . import config

BRAIN_FILES = ("company", "icp", "personas", "voice")


def _path(name: str) -> Path:
    if name not in BRAIN_FILES:
        raise ValueError(f"brain file must be one of {BRAIN_FILES}")
    return config.BRAIN_DIR / f"{name}.md"


def read_brain() -> dict[str, dict[str, object]]:
    config.ensure_home()
    result: dict[str, dict[str, object]] = {}
    for name in BRAIN_FILES:
        path = _path(name)
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        todos = content.count("TODO")
        result[name] = {"path": str(path), "complete": bool(content.strip()) and todos == 0,
                        "todos_left": todos, "content": content}
    return result


def write_brain(name: str, content: str) -> Path:
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


# ── questions: the user answers in plain words, an agent drafts the profile, the user saves it ─────────

FILE_LABELS = {"company": "Company & offer", "icp": "Ideal customer", "personas": "Personas", "voice": "Voice & rules"}

# Asked every time, in this order. Written for any business: no industry in them.
CORE_QUESTIONS = [
    {"id": "lead_offer", "question": "What do you want to sell in your outreach right now?",
     "help": "If you offer several things, say which one leads. Every email pitches this first.",
     "placeholder": "e.g. Our main offer is X for Y. Z is an add-on we only mention if it fits."},
    {"id": "offers", "question": "For each thing you sell: what does the client get, what changes for their customers, "
                                 "how long does it take, and what's the smallest first project?",
     "help": "One paragraph per offer. Rough is fine.",
     "placeholder": "e.g. X: they get ..., so their customers ... Takes 2 weeks. Smallest first project: ..."},
    {"id": "proof", "question": "Which past clients, results or work can emails mention?",
     "help": "Names, numbers, links. Emails quote these exactly and never invent others, so anything you leave out "
             "is never mentioned. Say which offer each one proves.",
     "placeholder": "e.g. Client A (offer X): result in numbers. Client B (offer Y): what we made, link."},
    {"id": "who", "question": "Who is it for?",
     "help": "The kind of company, its size, where it is, and what it's doing when it needs you (launching, hiring, "
             "raising money, opening a location...).",
     "placeholder": "e.g. 5-50 person companies in the UK and US that just launched and have no ..."},
    {"id": "decider", "question": "Who do you write to?",
     "help": "The job title of the person who decides, and what they care about most.",
     "placeholder": "e.g. The founder at small companies, the head of marketing at bigger ones. They care about ..."},
    {"id": "voice", "question": "How should your emails sound?",
     "help": "Your name and sign-off, words you never want used, and one email you'd be happy to send (or one that "
             "got a reply).",
     "placeholder": "e.g. Sign as Sam. Never say 'synergy'. An email that worked: ..."},
]


def _todo_question(line: str) -> tuple[str, str] | None:
    """('Sign-off', 'e.g. "Priya"') from a template line like '- Sign-off: TODO (e.g. "Priya")'."""
    text = re.sub(r"<!--.*?-->", "", line).strip().lstrip("-*0123456789. ").strip()
    if "TODO" not in text:
        return None
    label, _, rest = text.partition(":")
    if "TODO" in label:  # "- TODO e.g. Fewer than 5 employees": no label, the hint is the question
        hint = re.sub(r"^TODO\s*", "", label).strip(" .")
        return (hint, "") if hint else None
    hint = re.sub(r"^\s*TODO\s*", "", rest).strip(" ()")
    return label.strip(), hint


def profile_questions() -> dict[str, object]:
    """The core questions, plus one short question per TODO still open in the profile."""
    missing, seen = [], set()
    for name, info in read_brain().items():
        section = ""
        for line in str(info["content"]).splitlines():
            if line.startswith("#"):
                section = line.lstrip("# ").strip()
                continue
            found = _todo_question(line)
            if not found:
                continue
            label, hint = found
            key = (name, label.lower())
            if key in seen:
                continue
            seen.add(key)
            missing.append({"id": f"todo:{name}:{len(missing)}", "file": name,
                            "question": label[:1].upper() + label[1:].rstrip("?:") + "?",
                            "help": f"{FILE_LABELS[name]} · {section}" + (f" · {hint}" if hint else ""),
                            "line": line.strip()})
    return {"questions": CORE_QUESTIONS, "missing": missing}


def _proposal_path(name: str) -> Path:
    _path(name)  # validates the name
    return config.BRAIN_DIR / ".proposed" / f"{name}.md"


def propose(name: str, content: str) -> Path:
    """Save an agent's suggested version of one profile file. The real file only changes when the user saves it."""
    if not (content or "").strip():
        raise ValueError("The suggested file is empty.")
    path = _proposal_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def proposals() -> list[dict[str, object]]:
    current = read_brain()
    out = []
    for name in BRAIN_FILES:
        path = _proposal_path(name)
        if path.exists():
            after = path.read_text(encoding="utf-8")
            before = str(current[name]["content"])
            out.append({"file": name, "label": FILE_LABELS[name], "before": before, "after": after,
                        "todos_before": before.count("TODO"), "todos_after": after.count("TODO"),
                        "unchanged": before.strip() == after.strip()})
    return out


def accept_proposal(name: str, content: str | None = None) -> dict[str, object]:
    """The user's Save: the (possibly edited) suggestion becomes the profile file."""
    path = _proposal_path(name)
    if content is None:
        if not path.exists():
            raise ValueError("There's no suggestion for this file.")
        content = path.read_text(encoding="utf-8")
    write_brain(name, content)
    path.unlink(missing_ok=True)
    return {"saved": name, "complete": read_brain()[name]["complete"]}


def discard_proposal(name: str) -> dict[str, object]:
    _proposal_path(name).unlink(missing_ok=True)
    return {"discarded": name}


# ── search suggestions ──────────────────────────────────────────────────

def _clean(value: str | None) -> str | None:
    """A profile value, minus template hints. None if it's still a TODO."""
    if not value or "TODO" in value:
        return None
    value = re.sub(r"\s*\((?:e\.g\.|eg|titles?:)[^)]*\)", "", value, flags=re.I)  # drop "(e.g. ...)" hints
    value = re.sub(r"<!--.*?-->", "", value).strip(" .;-")
    return value or None


def _field(text: str, label: str) -> str | None:
    m = re.search(rf"^\s*-\s*{re.escape(label)}[^:\n]*:\s*(.+)$", text, re.I | re.M)
    return _clean(m.group(1)) if m else None


def _brief(value: str | None, max_items: int = 0) -> str:
    """Drop bracketed notes and, optionally, keep only the first few comma-separated items."""
    text = re.sub(r"\s*\([^)]*\)", "", value or "").strip(" .;,-")
    if max_items:
        items = [i.strip() for i in re.split(r",|\bor\b|/", text) if i.strip()]
        text = ", ".join(items[:max_items])
    return text


def _lower_first(text: str) -> str:
    """'Raised funding recently' -> 'raised funding recently', but keep 'AI', 'YC' etc."""
    return text if len(text) > 1 and text[:2].isupper() else text[:1].lower() + text[1:]


def _signals(icp: str) -> list[tuple[int, str]]:
    out = []
    for weight, text in re.findall(r"^\s*-\s*([1-5])\s*[—–-]+\s*(.+)$", icp, re.M):
        text = _clean(text)
        if text:
            out.append((int(weight), re.split(r"\s*\(", text)[0].strip()))
    return sorted(out, key=lambda s: -s[0])


def _section_items(text: str, heading: str) -> list[str]:
    """The filled-in bullet points under a '## heading' (TODO lines and template hints left out)."""
    m = re.search(rf"^##\s*{re.escape(heading)}[^\n]*\n(.*?)(?=^##|\Z)", text, re.I | re.M | re.S)
    items = [_clean(line) for line in re.findall(r"^\s*-\s*(.+)$", m.group(1), re.M)] if m else []
    return [i for i in items if i]


def enhance_request(text: str, count: int | None = None) -> dict[str, object]:
    """Turn a short request ("find me some leads") into a precise brief from the business profile, so the agents
    don't have to guess. No AI call: instant, and it only uses what the user wrote in their profile."""
    text = (text or "").strip()
    brain = {name: str(info["content"]) for name, info in read_brain().items()}
    icp, personas, company = brain["icp"], brain["personas"], brain["company"]
    found = re.search(r"\b(\d{1,3})\b", text)
    count = str(count) if count else (found.group(1) if found else None)
    fields = {
        "industries": _field(icp, "Industries"), "size": _field(icp, "Company size"), "where": _field(icp, "Locations"),
        "model": _field(icp, "Business model"), "tech": _field(icp, "Tech they use"),
        "titles": _field(personas, "Titles to search"), "product": _field(company, "Product / service"),
    }
    signals = _signals(icp)
    skip = _section_items(icp, "Disqualifiers")
    lines = [f"Goal: {text}" if text else f"Goal: find {count or 10} new companies that fit my ideal customer"]
    lines.append(f"How many: {count or 10} companies (fewer is fine if that's all that genuinely fit).")
    fit = [f"{k}: {_brief(v)}" for k, v in (("Industries", fields["industries"]), ("Size", fields["size"]),
                                             ("Locations", fields["where"]), ("Business model", fields["model"]),
                                             ("Uses", fields["tech"])) if v]
    if fit:
        lines.append("Must fit: " + "; ".join(fit) + ".")
    if signals:
        lines.append("Reason to buy now (strongest first, last 90 days): " + "; ".join(s for _, s in signals[:4]) + ".")
    if skip:
        lines.append("Skip: " + "; ".join(skip[:4]) + "; and anyone already in my pipeline.")
    if fields["titles"]:
        lines.append(f"People: find the {_brief(fields['titles'], 3)}, with a published email where possible.")
    if fields["product"]:
        lines.append(f"Why they'd care: we offer {_brief(fields['product'])}.")
    lines.append("Quality bar: real companies with a working website only; a source link and date for every signal; "
                 "nothing invented. Return fewer results rather than weak ones.")
    used = sum(bool(v) for v in fields.values()) + bool(signals) + bool(skip)
    return {"text": "\n".join(lines), "used_profile": used > 0,
            "missing": None if used else "Your Business profile is mostly empty, so there was little to add. "
                                         "Fill in Ideal customer and Personas for sharper results."}


def suggest_searches(limit: int = 4) -> dict[str, object]:
    """Ready-made Find-buyers searches built from the business profile. No AI call, instant."""
    brain = {name: str(info["content"]) for name, info in read_brain().items()}
    icp, personas, company = brain["icp"], brain["personas"], brain["company"]
    industries = _field(icp, "Industries")
    size = _field(icp, "Company size")
    where = _field(icp, "Locations")
    tech = _field(icp, "Tech they use")
    titles = _field(personas, "Titles to search")
    product = _field(company, "Product / service")
    signals = _signals(icp)

    # Profiles are written for agents, so they can be long. Searches should read like one sentence.
    segments = [_brief(s) for s in re.split(r";", industries or "") if _brief(s)]
    if not segments and product:
        segments = [f"companies that need {_brief(product)}"]
    if not segments:
        return {"suggestions": [], "missing": "Add your industries (Ideal customer) or what you sell (Company & offer) "
                                             "to your Business profile to get suggested searches."}
    size_range = re.search(r"\d+\s*[-–]\s*\d+|\d+\s*\+", size or "")
    where_s = f" in {_brief(where, 3)}" if where else ""
    size_s = f" with {size_range.group(0).replace(' ', '')} employees" if size_range else ""
    reach = f". Reach the {_brief(titles, 3)}" if titles else ""

    items: list[dict[str, str]] = []
    for i, (_, signal) in enumerate(signals[:3]):
        segment = segments[i % len(segments)]
        short = signal if len(signal) <= 42 else signal[:40].rstrip() + "…"
        items.append({"label": short, "query": f"{segment}{where_s}{size_s} showing: {_lower_first(signal)}{reach}"})
    if tech:
        items.append({"label": "Uses your target tech",
                      "query": f"{segments[0]}{where_s}{size_s} that use {_brief(tech, 3)}{reach}"})
    for segment in segments[1:3]:
        label = segment if len(segment) <= 42 else segment[:40].rstrip() + "…"
        items.append({"label": label, "query": f"{segment}{where_s}{size_s} that fit my ideal customer{reach}"})
    items.append({"label": "Best ICP fit", "query": f"{segments[0]}{where_s}{size_s} that fit my ideal customer best{reach}"})
    seen, unique = set(), []
    for item in items:
        if item["query"] not in seen:
            seen.add(item["query"])
            unique.append(item)
    return {"suggestions": unique[:limit], "missing": None}
