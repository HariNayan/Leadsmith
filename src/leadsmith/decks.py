"""Personalised 4-page PDF decks, rendered by the Chrome/Edge already on the machine."""

from __future__ import annotations

import html
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import config, db

BROWSER_CANDIDATES = {
    "Windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ],
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
}


def find_browser() -> str | None:
    if config.get("CHROME_PATH") and Path(config.get("CHROME_PATH")).exists():  # type: ignore[arg-type]
        return config.get("CHROME_PATH")
    for path in BROWSER_CANDIDATES.get(platform.system(), []):
        if Path(path).exists():
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def render_html(title: str, company: str, slides: list[dict[str, Any]]) -> str:
    accent = config.get("DECK_COLOR") or "#4f46e5"
    sender = config.get("SENDER_NAME") or ""
    pages = []
    for i, slide in enumerate(slides[:6], start=1):
        bullets = "".join(f"<li>{_esc(b)}</li>" for b in (slide.get("bullets") or [])[:5])
        highlight = f'<div class="highlight">{_esc(slide.get("highlight"))}</div>' if slide.get("highlight") else ""
        body = f'<p class="body">{_esc(slide.get("body"))}</p>' if slide.get("body") else ""
        kicker = _esc(slide.get("kicker") or ("For " + company if i == 1 else ""))
        pages.append(f"""
        <section class="page{' cover' if i == 1 else ''}">
          <div class="kicker">{kicker}</div>
          <h1>{_esc(slide.get("heading"))}</h1>
          {body}{highlight}
          {f'<ul>{bullets}</ul>' if bullets else ''}
          <footer><span>{_esc(title)}</span><span>{_esc(sender)} · {i}/{min(len(slides), 6)}</span></footer>
        </section>""")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>
@page {{ size: 13.333in 7.5in; margin: 0; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: "Segoe UI", -apple-system, Helvetica, Arial, sans-serif; color: #111827; }}
.page {{ width: 13.333in; height: 7.5in; padding: 0.9in 1in 0.8in; position: relative; page-break-after: always;
        display: flex; flex-direction: column; justify-content: center; background: #fff; }}
.page:last-child {{ page-break-after: auto; }}
.cover {{ background: {accent}; color: #fff; }}
.kicker {{ text-transform: uppercase; letter-spacing: .14em; font-size: 15px; font-weight: 600; color: {accent}; margin-bottom: 18px; }}
.cover .kicker {{ color: rgba(255,255,255,.8); }}
h1 {{ font-size: 46px; line-height: 1.12; margin: 0 0 22px; max-width: 10.5in; letter-spacing: -0.01em; }}
.body {{ font-size: 22px; line-height: 1.5; max-width: 9.5in; color: #374151; margin: 0 0 18px; }}
.cover .body {{ color: rgba(255,255,255,.9); }}
.highlight {{ font-size: 34px; font-weight: 700; color: {accent}; margin: 6px 0 18px; }}
.cover .highlight {{ color: #fff; }}
ul {{ margin: 6px 0 0; padding-left: 26px; font-size: 22px; line-height: 1.6; max-width: 10in; }}
li {{ margin-bottom: 6px; }}
footer {{ position: absolute; left: 1in; right: 1in; bottom: .45in; display: flex; justify-content: space-between;
          font-size: 13px; color: #9ca3af; }}
.cover footer {{ color: rgba(255,255,255,.7); }}
</style></head><body>{''.join(pages)}</body></html>"""


SLIDE_FORMAT = ('Each slide is {"heading": str, "body": str (1-2 sentences), "bullets": [str] (up to 4), '
                '"highlight": str (optional big stat or quote), "kicker": str (optional small label)}; 3 to 6 slides, '
                'every slide with a heading and a body or bullets.')

# Field names agents guess when they can't see the tool's schema (OpenCode's code mode hides it).
_HEADING_KEYS = ("heading", "title", "headline", "h1", "header")
_BODY_KEYS = ("body", "text", "description", "subtitle", "subheading", "paragraph", "summary")
_BULLET_KEYS = ("bullets", "points", "items", "list", "bullet_points", "content")
_PLACEHOLDER = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$|^(lorem ipsum|todo|tbd|placeholder|xxx+)\b", re.I)


def normalize_slides(slides: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Slides in the renderer's format, or an error saying exactly what's wrong. A deck is shown to a prospect, so a
    slide without a heading or any text, or with placeholder text, is refused rather than rendered blank."""
    if not isinstance(slides, list) or not slides:
        return [], f"slides must be a list. {SLIDE_FORMAT}"
    if not 3 <= len(slides) <= 6:
        return [], f"A deck has 3 to 6 slides (got {len(slides)}). {SLIDE_FORMAT}"
    out = []
    for n, raw in enumerate(slides, start=1):
        if not isinstance(raw, dict):
            return [], f"Slide {n} isn't an object. {SLIDE_FORMAT}"
        pick = lambda keys: next((raw[k] for k in keys if raw.get(k)), None)  # noqa: E731
        heading, body, bullets = pick(_HEADING_KEYS), pick(_BODY_KEYS), pick(_BULLET_KEYS)
        if isinstance(body, list):  # a list of lines is bullets
            bullets, body = bullets or body, None
        if isinstance(bullets, str):
            bullets = [b.strip(" -•*") for b in bullets.splitlines() if b.strip(" -•*")]
        if isinstance(bullets, list) and not body and bullets and not raw.get("bullets"):
            body, bullets = str(bullets[0]), bullets[1:]  # "content": [...] → first line as the body
        slide = {"heading": str(heading or "").strip(), "body": str(body or "").strip(),
                 "bullets": [str(b).strip() for b in (bullets or []) if str(b).strip()][:4],
                 "highlight": str(raw.get("highlight") or "").strip(), "kicker": str(raw.get("kicker") or "").strip()}
        if not slide["heading"]:
            return [], f"Slide {n} has no heading. {SLIDE_FORMAT}"
        if not slide["body"] and not slide["bullets"]:
            return [], f"Slide {n} has no body or bullets. {SLIDE_FORMAT}"
        texts = [slide["heading"], slide["body"], slide["highlight"], *slide["bullets"]]
        fake = next((t for t in texts if t and _PLACEHOLDER.search(t.strip())), None)
        if fake:
            return [], (f"Slide {n} has placeholder text ({fake!r}). Write the real content: decks go to prospects, "
                        "so never save a test deck.")
        out.append({k: v for k, v in slide.items() if v})
    return out, None


def build(domain: str, title: str, slides: list[dict[str, Any]]) -> dict[str, Any]:
    slides, problem = normalize_slides(slides)
    if problem:
        return {"error": problem, "saved": False}
    if not (title or "").strip() or _PLACEHOLDER.search(title.strip()):
        return {"error": "Give the deck a real title, e.g. \"<Company> × <your company>: <the idea>\".", "saved": False}
    domain = db.normalize_domain(domain)
    account = db.dossier(domain) or {}
    company = account.get("name") or domain
    config.DECK_DIR.mkdir(parents=True, exist_ok=True)
    html_path = config.DECK_DIR / f"{domain}.html"
    pdf_path = config.DECK_DIR / f"{domain}.pdf"
    html_path.write_text(render_html(title, company, slides), encoding="utf-8")
    error = None
    browser = find_browser()
    if not browser:
        error = "No Chrome/Edge/Chromium found. Install Chrome or set CHROME_PATH; the HTML deck was still saved."
    else:
        try:
            subprocess.run(
                [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--print-to-pdf-no-header",
                 f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()],
                check=False, capture_output=True, timeout=90,
            )
            if not pdf_path.exists():
                error = "The browser did not produce a PDF."
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = f"PDF rendering failed: {exc}"
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO decks (domain, title, slides, pdf_path, error, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (domain, title, json.dumps(slides), str(pdf_path) if not error else None, error, db.now()),
        )
    return {"domain": domain, "pdf": str(pdf_path) if not error else None, "html": str(html_path), "error": error}


def pdf_ready(domain: str) -> bool:
    """True only when the account's deck rendered as a PDF that's still on disk, i.e. it can really be attached."""
    deck = get(domain)
    return bool(deck and deck.get("pdf_path") and Path(deck["pdf_path"]).exists())


def get(domain: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        deck = db.one(conn.execute("SELECT * FROM decks WHERE domain = ?", (db.normalize_domain(domain),)))
    if deck:
        deck["slides"] = json.loads(deck["slides"])
    return deck
