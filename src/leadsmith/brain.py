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
