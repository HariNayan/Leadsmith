"""Personalised PDF decks, rendered by the Chrome/Edge already on the machine.

A deck is a short document made for one company: their gap (next to a screenshot of their own site), what you'd
make, a sample of the actual work, the plan, your proof (with your own work images) and the next step. It carries
their logo and colour next to yours. Every image is embedded, so the PDF and the dashboard preview look the same.
"""

from __future__ import annotations

import base64
import html
import json
import os
import platform
import re
import shutil
import subprocess
import time
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

LAYOUTS = ("cover", "insight", "concept", "sample", "plan", "proof", "next")
MAX_SLIDE_WORDS = 90  # a slide is read in seconds
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".svg": "image/svg+xml", ".gif": "image/gif", ".ico": "image/x-icon"}


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


# ── your brand and work: ~/.leadsmith/portfolio ─────────────────────────────

def portfolio_dir() -> Path:
    return config.HOME / "portfolio"


def _data_uri(path: Path, max_bytes: int = 2_500_000) -> str | None:
    mime = IMAGE_TYPES.get(path.suffix.lower())
    try:
        if not mime or not path.is_file() or path.stat().st_size > max_bytes:
            return None
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"
    except OSError:
        return None


def portfolio() -> dict[str, Any]:
    """Your logo (logo.png/.svg/.jpg) and your work images, with captions from captions.txt ("file.jpg: caption")
    or the file name. Shown on the cover and the proof slide of every deck."""
    folder = portfolio_dir()
    if not folder.is_dir():
        return {"logo": None, "work": []}
    captions: dict[str, str] = {}
    try:
        for line in (folder / "captions.txt").read_text(encoding="utf-8").splitlines():
            name, sep, caption = line.partition(":")
            if sep and caption.strip():
                captions[name.strip().lower()] = caption.strip()
    except OSError:
        pass
    logo, work = None, []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in IMAGE_TYPES:
            continue
        if path.stem.lower() == "logo":
            logo = path
            continue
        caption = captions.get(path.name.lower()) or re.sub(r"[-_]+", " ", path.stem).strip().capitalize()
        work.append({"path": path, "caption": caption})
    return {"logo": logo, "work": work}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9]{2,}", (text or "").lower())}


def pick_work(deck_text: str, work: list[dict[str, Any]], n: int = 3) -> list[dict[str, Any]]:
    """The work images whose captions are closest to what this deck is about (the first ones when none match)."""
    words = _words(deck_text)
    ranked = sorted(enumerate(work), key=lambda iw: (-len(words & _words(iw[1]["caption"])), iw[0]))
    return [w for _, w in ranked[:n]]


# ── their brand: logo, colour and a screenshot of their site ───────────────

def _hex(value: str | None) -> str | None:
    m = re.fullmatch(r"\s*#?([0-9a-f]{6}|[0-9a-f]{3})\s*", value or "", re.I)
    if not m:
        return None
    h = m.group(1)
    return "#" + ("".join(c * 2 for c in h) if len(h) == 3 else h).lower()


def _luminance(color: str) -> float:
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def usable_accent(color: str | None) -> str | None:
    """A brand colour that reads on white and on the dark cover: not near-white, not near-black, not grey."""
    color = _hex(color)
    if not color:
        return None
    lum = _luminance(color)
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    if lum > 0.55 or lum < 0.03 or max(r, g, b) - min(r, g, b) < 30:
        return None
    return color


def prospect_brand(domain: str) -> dict[str, Any]:
    """Their theme colour and logo, read from their homepage (cached for two weeks). Empty when unreachable."""
    if not db.has_website(domain):
        return {}
    key = f"brand:{domain}"
    cached = db.cache_get(key)
    if cached is not None:
        return cached
    brand: dict[str, Any] = {}
    try:
        import httpx

        with httpx.Client(timeout=8, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; deck-builder)"}) as client:
            page = client.get(f"https://{domain}")
            text = page.text[:400_000]
            meta = re.search(r'<meta[^>]+name=["\']theme-color["\'][^>]*content=["\']([^"\']+)', text, re.I) or \
                re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']theme-color', text, re.I)
            brand["color"] = usable_accent(meta.group(1)) if meta else None
            hrefs = []
            for tag in re.findall(r'<link[^>]+rel=["\'][^"\']*(?:apple-touch-icon|icon)[^"\']*["\'][^>]*>', text, re.I):
                href = re.search(r'href=["\']([^"\']+)', tag)
                if href:
                    hrefs.append((0 if "apple-touch" in tag.lower() else 1 if ".svg" in href.group(1) else 2, href.group(1)))
            for _, href in sorted(hrefs) + [(9, "/apple-touch-icon.png")]:
                url = str(page.url.join(href))
                try:
                    got = client.get(url)
                except httpx.HTTPError:
                    continue
                mime = got.headers.get("content-type", "").split(";")[0].strip()
                if got.status_code == 200 and mime.startswith("image/") and 200 < len(got.content) < 400_000:
                    brand["logo"] = f"data:{mime};base64,{base64.b64encode(got.content).decode()}"
                    break
    except Exception:  # noqa: BLE001 - a deck without their logo is still a good deck
        brand = {}
    db.cache_set(key, brand, 24 * 14 if brand else 24)  # unreachable today: try again tomorrow
    return brand


def site_screenshot(domain: str) -> str | None:
    """A 1280x800 screenshot of their homepage, taken with the local Chrome (cached for two weeks)."""
    if not db.has_website(domain):
        return None
    folder = config.DECK_DIR / "assets"
    path = folder / f"{domain}-site.png"
    fresh = path.exists() and time.time() - path.stat().st_mtime < 14 * 86400
    if not fresh:
        browser = find_browser()
        if not browser:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
                            "--window-size=1280,800", "--virtual-time-budget=6000", f"--screenshot={path}",
                            f"https://{domain}"], check=False, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
    return _data_uri(path, 4_000_000) if path.exists() and path.stat().st_size > 5_000 else None


# ── slides ─────────────────────────────────────────────────────────────────

SLIDE_FORMAT = (
    'Each slide is {"layout": one of cover, insight, concept, sample, plan, proof, next; "heading": str; '
    '"body": str (1-2 sentences); "bullets": [str] (up to 4); "highlight": str (optional big number, fact or quote); '
    '"kicker": str (optional small label); "frames": [str] (sample slides: 3-4 pieces of the actual work, e.g. '
    '"Shot 1: close-up of the sole on wet rock"); "steps": [str] (plan slides: 3-5 steps, e.g. "Week 1: shortlist and '
    'storyboard")}; 3 to 6 slides, every slide with a heading and a body, bullets, frames or steps.')

# Field names agents guess when they can't see the tool's schema (OpenCode's code mode hides it).
_HEADING_KEYS = ("heading", "title", "headline", "h1", "header")
_BODY_KEYS = ("body", "text", "description", "subtitle", "subheading", "paragraph", "summary")
_BULLET_KEYS = ("bullets", "points", "items", "list", "bullet_points", "content")
_FRAME_KEYS = ("frames", "storyboard", "shots", "script", "sample", "scenes")
_STEP_KEYS = ("steps", "timeline", "phases", "milestones", "schedule")
_PLACEHOLDER = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$|^(lorem ipsum|todo|tbd|placeholder|xxx+)\b|^<[^<>]+>$", re.I)


def _lines(value: Any, limit: int) -> list[str]:
    """A list of short texts from a list of strings, of {title, text}-like objects, or from one string."""
    if isinstance(value, str):
        value = [v.strip(" -•*") for v in value.splitlines()]
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            label = next((str(item[k]) for k in ("title", "label", "name", "week", "when", "shot", "step") if item.get(k)), "")
            text = next((str(item[k]) for k in ("text", "detail", "description", "body", "what", "action") if item.get(k)), "")
            item = f"{label}: {text}" if label and text else label or text
        item = str(item or "").strip()
        if item:
            out.append(item)
    return out[:limit]


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
        layout = str(raw.get("layout") or raw.get("type") or "").strip().lower()
        slide = {"heading": str(heading or "").strip(), "body": str(body or "").strip(),
                 "bullets": [str(b).strip() for b in (bullets or []) if str(b).strip()][:4],
                 "highlight": str(raw.get("highlight") or raw.get("stat") or "").strip(),
                 "kicker": str(raw.get("kicker") or "").strip(),
                 "layout": layout if layout in LAYOUTS else "",
                 "frames": _lines(pick(_FRAME_KEYS), 4), "steps": _lines(pick(_STEP_KEYS), 5)}
        if not slide["heading"]:
            return [], f"Slide {n} has no heading. {SLIDE_FORMAT}"
        if not (slide["body"] or slide["bullets"] or slide["frames"] or slide["steps"]):
            return [], f"Slide {n} has no body or bullets. {SLIDE_FORMAT}"
        texts = [slide["heading"], slide["body"], slide["highlight"], *slide["bullets"], *slide["frames"], *slide["steps"]]
        fake = next((t for t in texts if t and _PLACEHOLDER.search(t.strip())), None)
        if fake:
            return [], (f"Slide {n} has placeholder text ({fake!r}). Write the real content: decks go to prospects, "
                        "so never save a test deck.")
        words = sum(len(t.split()) for t in texts if t)
        if words > MAX_SLIDE_WORDS:
            return [], (f"Slide {n} has {words} words; keep each slide under {MAX_SLIDE_WORDS}. A slide is read in "
                        "seconds: one point, said once.")
        out.append({k: v for k, v in slide.items() if v})
    return out, None


def layout_of(slide: dict[str, Any], index: int, count: int) -> str:
    """The slide's layout: as given, or from its position and content."""
    if slide.get("layout") in LAYOUTS:
        return slide["layout"]
    if index == 0:
        return "cover"
    if slide.get("frames"):
        return "sample"
    if slide.get("steps"):
        return "plan"
    if index == count - 1:
        return "next"
    if index == 1:
        return "insight"
    return "concept"


def _split(line: str) -> tuple[str, str]:
    label, sep, rest = line.partition(":")
    return (label.strip(), rest.strip()) if sep and len(label.split()) <= 5 else ("", line.strip())


def slide_text(slide: dict[str, Any]) -> str:
    return " ".join([slide.get("heading", ""), slide.get("body", ""), slide.get("highlight", ""),
                     *slide.get("bullets", []), *slide.get("frames", []), *slide.get("steps", [])])


# ── rendering ──────────────────────────────────────────────────────────────

def render_html(title: str, company: str, slides: list[dict[str, Any]], brand: dict[str, Any] | None = None,
                mine: dict[str, Any] | None = None, domain: str = "") -> str:
    brand, mine = brand or {}, mine or {}
    accent = brand.get("color") or usable_accent(config.get("DECK_COLOR")) or "#4f46e5"
    sender = config.get("SENDER_NAME") or ""
    my_company = mine.get("company") or ""
    count = min(len(slides), 6)
    their_logo = f'<img class="logo" src="{brand["logo"]}" alt="">' if brand.get("logo") else \
        f'<span class="logo letter">{_esc((company or "?")[:1].upper())}</span>'
    my_logo = f'<img class="logo" src="{mine["logo"]}" alt="">' if mine.get("logo") else ""
    pages = []
    for i, slide in enumerate(slides[:6]):
        layout = layout_of(slide, i, count)
        h = f'<h1>{_esc(slide.get("heading"))}</h1>'
        kicker = f'<div class="kicker">{_esc(slide.get("kicker") or ("For " + company if layout == "cover" else ""))}</div>'
        body = f'<p class="body">{_esc(slide.get("body"))}</p>' if slide.get("body") else ""
        bullets = "".join(f"<li>{_esc(b)}</li>" for b in slide.get("bullets", []))
        bullets = f"<ul>{bullets}</ul>" if bullets else ""
        stat = f'<div class="stat">{_esc(slide.get("highlight"))}</div>' if slide.get("highlight") else ""
        if layout == "cover":
            inner = f"""<div class="brands">{their_logo}{f'<span class="x">×</span>{my_logo}' if my_logo else ''}</div>
              <div class="cover-main">{kicker}{h}{body}</div>
              <div class="cover-foot"><span>{_esc(sender)}{(' · ' + _esc(my_company)) if my_company else ''}</span><span>Prepared for {_esc(company)}</span></div>"""
        elif layout == "insight":
            shot = brand.get("screenshot") if slide.get("site", True) else None
            side = (f'<figure class="browser"><div class="bar"><i></i><i></i><i></i><span>{_esc(domain)}</span></div>'
                    f'<img src="{shot}" alt=""></figure>') if shot else (f'<div class="panel">{stat}</div>' if stat else "")
            inner = f'<div class="split"><div>{kicker}{h}{stat if shot else ""}{body}{bullets}</div><div class="side">{side}</div></div>'
        elif layout == "concept":
            cards = "".join(f'<div class="card"><b>{n}</b><span>{_esc(b)}</span></div>'
                            for n, b in enumerate(slide.get("bullets", []), start=1))
            inner = f'{kicker}{h}{body}{stat}<div class="cards">{cards}</div>'
        elif layout == "sample":
            frames = "".join(
                f'<div class="frame"><span class="tag">{_esc(_split(f)[0] or n)}</span><p>{_esc(_split(f)[1])}</p></div>'
                for n, f in enumerate(slide.get("frames") or slide.get("bullets", []), start=1))
            inner = f'{kicker or "<div class=kicker>A first look</div>"}{h}{body}<div class="frames">{frames}</div>'
        elif layout == "plan":
            steps = "".join(f'<li><i></i><b>{_esc(_split(s)[0] or f"Step {n}")}</b><span>{_esc(_split(s)[1])}</span></li>'
                            for n, s in enumerate(slide.get("steps") or slide.get("bullets", []), start=1))
            inner = f'{kicker}{h}{body}<ol class="timeline">{steps}</ol>{stat}'
        elif layout == "proof":
            work = mine.get("work") or []
            gallery = "".join(f'<figure><img src="{w["uri"]}" alt=""><figcaption>{_esc(w["caption"])}</figcaption></figure>'
                              for w in work)
            quote = f'<blockquote>{_esc(slide.get("highlight"))}</blockquote>' if slide.get("highlight") else ""
            inner = (f'<div class="split proof"><div>{kicker}{h}{body}{bullets}{quote}</div>'
                     f'<div class="gallery n{len(work)}">{gallery}</div></div>') if work else f"{kicker}{h}{body}{bullets}{quote}"
        else:  # next
            inner = f"""{kicker}{h}{body}{bullets}
              <div class="cta"><b>{_esc(slide.get("highlight") or "Just reply to this email")}</b><span>{_esc(sender)}{(' · ' + _esc(my_company)) if my_company else ''}</span></div>"""
        foot = "" if layout == "cover" else \
            f'<footer><span>{_esc(company)}{(" × " + _esc(my_company)) if my_company else ""}</span><span>{i + 1} / {count}</span></footer>'
        pages.append(f'<section class="page {layout}">{inner}{foot}</section>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>
@page {{ size: 13.333in 7.5in; margin: 0; }}
* {{ box-sizing: border-box; }}
:root {{ --accent: {accent}; --ink: #15171c; --muted: #596070; --soft: #f3f4f7; --line: #e3e5ea; }}
body {{ margin: 0; font-family: "Segoe UI", Inter, -apple-system, Helvetica, Arial, sans-serif; color: var(--ink);
       -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
.page {{ width: 13.333in; height: 7.5in; padding: .8in .95in .9in; position: relative; page-break-after: always;
        display: flex; flex-direction: column; justify-content: center; background: #fff; overflow: hidden; }}
.page:last-child {{ page-break-after: auto; }}
.page::before {{ content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 10px; background: var(--accent); }}
.kicker {{ text-transform: uppercase; letter-spacing: .14em; font-size: 14px; font-weight: 700; color: var(--accent); margin-bottom: 14px; }}
.kicker:empty {{ display: none; }}
h1 {{ font-size: 42px; line-height: 1.12; margin: 0 0 18px; max-width: 10.6in; letter-spacing: -0.015em; font-weight: 700; }}
.body {{ font-size: 21px; line-height: 1.5; max-width: 9.6in; color: var(--muted); margin: 0 0 16px; }}
ul {{ margin: 6px 0 0; padding-left: 24px; font-size: 20px; line-height: 1.55; max-width: 10in; }}
li {{ margin-bottom: 6px; }} li::marker {{ color: var(--accent); }}
.stat {{ font-size: 54px; font-weight: 800; color: var(--accent); line-height: 1.05; margin: 4px 0 16px; letter-spacing: -0.02em; }}
footer {{ position: absolute; left: .95in; right: .95in; bottom: .4in; display: flex; justify-content: space-between;
          font-size: 12.5px; color: #9aa0ab; letter-spacing: .02em; }}
.logo {{ height: 56px; width: auto; max-width: 180px; object-fit: contain; border-radius: 12px; background: #fff; padding: 6px; }}
.logo.letter {{ display: inline-grid; place-items: center; width: 56px; font: 700 26px/1 inherit; color: var(--ink); }}
/* cover */
.cover {{ background: #101217; color: #fff; justify-content: space-between; padding-top: .7in; padding-bottom: .6in; }}
.cover::before {{ width: 100%; height: 10px; bottom: auto; }}
.cover .brands {{ display: flex; align-items: center; gap: 16px; }}
.cover .x {{ font-size: 26px; color: rgba(255,255,255,.5); }}
.cover h1 {{ font-size: 54px; max-width: 10.4in; }}
.cover .body {{ color: rgba(255,255,255,.78); font-size: 22px; }}
.cover .kicker {{ color: color-mix(in srgb, var(--accent) 55%, #fff); }}
.cover-foot {{ display: flex; justify-content: space-between; font-size: 14px; color: rgba(255,255,255,.6); }}
/* insight */
.split {{ display: grid; grid-template-columns: 1.05fr 1fr; gap: .55in; align-items: center; }}
.browser {{ margin: 0; border-radius: 12px; overflow: hidden; border: 1px solid var(--line); box-shadow: 0 18px 50px rgba(16,18,23,.16); background: #fff; }}
.browser .bar {{ display: flex; align-items: center; gap: 6px; padding: 9px 12px; background: var(--soft); border-bottom: 1px solid var(--line); font-size: 12px; color: var(--muted); }}
.browser .bar i {{ width: 9px; height: 9px; border-radius: 50%; background: #d4d7de; }}
.browser .bar span {{ margin-left: 8px; }}
.browser img {{ display: block; width: 100%; height: auto; }}
.panel {{ background: var(--soft); border-radius: 16px; padding: .5in; }}
.panel .stat {{ font-size: 64px; margin: 0; }}
/* concept */
.cards {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; margin-top: 8px; }}
.cards .card {{ display: flex; gap: 14px; align-items: flex-start; background: var(--soft); border-radius: 12px; padding: 18px 20px; font-size: 19px; line-height: 1.45; }}
.cards b {{ flex: none; width: 30px; height: 30px; border-radius: 50%; background: var(--accent); color: #fff; display: grid; place-items: center; font-size: 15px; }}
/* sample */
.frames {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(0, 1fr)); gap: 16px; margin-top: 10px; }}
.frame {{ aspect-ratio: 16 / 10; border-radius: 12px; padding: 16px 18px; display: flex; flex-direction: column; justify-content: space-between;
          background: linear-gradient(160deg, #191c23, #2a2f3a); color: #fff; box-shadow: 0 10px 30px rgba(16,18,23,.14); }}
.frame .tag {{ align-self: flex-start; font-size: 12.5px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
               background: var(--accent); border-radius: 999px; padding: 4px 11px; }}
.frame p {{ margin: 0; font-size: 19px; line-height: 1.4; font-weight: 600; }}
/* plan */
.timeline {{ list-style: none; margin: 18px 0 0; padding: 0; display: grid; grid-auto-flow: column; grid-auto-columns: 1fr; gap: 18px; position: relative; }}
.timeline::before {{ content: ""; position: absolute; top: 10px; left: 10px; right: 10px; height: 2px; background: var(--line); }}
.timeline li {{ position: relative; padding-top: 34px; display: grid; gap: 6px; margin: 0; }}
.timeline li i {{ position: absolute; top: 0; left: 0; width: 22px; height: 22px; border-radius: 50%; background: #fff; border: 5px solid var(--accent); }}
.timeline b {{ font-size: 18px; }}
.timeline span {{ font-size: 16.5px; line-height: 1.45; color: var(--muted); }}
/* proof */
.gallery {{ display: grid; gap: 12px; grid-template-columns: 1fr 1fr; }}
.gallery.n1 {{ grid-template-columns: 1fr; }}
.gallery.n3 figure:first-child {{ grid-column: span 2; }}
.gallery figure {{ margin: 0; }}
.gallery img {{ width: 100%; aspect-ratio: 16 / 10; object-fit: cover; border-radius: 10px; border: 1px solid var(--line); display: block; }}
.gallery figcaption {{ font-size: 13px; color: var(--muted); margin-top: 6px; }}
blockquote {{ margin: 18px 0 0; padding: 14px 20px; border-left: 4px solid var(--accent); background: var(--soft); border-radius: 0 10px 10px 0; font-size: 19px; line-height: 1.5; }}
/* next */
.cta {{ margin-top: 26px; display: inline-flex; flex-direction: column; gap: 4px; align-self: flex-start; background: var(--accent); color: #fff; border-radius: 14px; padding: 18px 26px; }}
.cta b {{ font-size: 22px; }} .cta span {{ font-size: 14px; opacity: .85; }}
</style></head><body>{''.join(pages)}</body></html>"""


# ── checks before a deck goes to a prospect ────────────────────────────────

def _repeats_other_decks(domain: str, slides: list[dict[str, Any]]) -> tuple[list[int], str | None]:
    """Slide numbers that nearly repeat a slide of one other company's deck (company names left out)."""
    from . import lint

    with db.connect() as conn:
        others = db.rows(conn.execute("SELECT d.domain, d.slides, a.name FROM decks d LEFT JOIN accounts a ON a.domain ="
                                      " d.domain WHERE d.domain != ? ORDER BY d.updated_at DESC LIMIT 60", (domain,)))
    names = {domain.split(".")[0]} | {o["domain"].split(".")[0] for o in others} | \
        {(o["name"] or "").lower() for o in others if o["name"]}
    account = db.dossier(domain) or {}
    if account.get("name"):
        names.add(account["name"].lower())

    def norm(slide: dict[str, Any]) -> list[str]:
        text = slide_text(slide).lower()
        for name in names:
            if len(name) >= 3:
                text = text.replace(name, " ")
        return lint._norm(text).split()

    count = len(slides)
    mine = [(n, norm(s)) for n, s in enumerate(slides, start=1) if layout_of(s, n - 1, count) not in ("cover", "next")]
    worst: tuple[list[int], str | None] = ([], None)
    for other in others:
        try:
            theirs = [norm(s) for s in json.loads(other["slides"] or "[]")]
        except ValueError:
            continue
        hits = [n for n, a in mine if len(a) >= 8 and any(lint._overlap(a, b) >= 0.75 for b in theirs if len(b) >= 8)]
        if len(hits) > len(worst[0]):
            worst = (hits, other["domain"])
    return worst


def check_deck(domain: str, company: str, title: str, slides: list[dict[str, Any]]) -> str | None:
    """Why this deck isn't ready for the prospect, or None."""
    from . import lint

    count = len(slides)
    layouts = [layout_of(s, i, count) for i, s in enumerate(slides)]
    if count >= 4 and "sample" not in layouts:
        return ('Add a sample slide (layout "sample", with 3-4 "frames"): a small piece of the actual work made for them, '
                "e.g. three storyboard shots, the first lines of a script or the first items of the plan. Showing the "
                "work beats describing it.")
    text = " ".join([title, *(slide_text(s) for s in slides)])
    if company and not lint.mentions_company(text, company):
        return f"The deck never names {company}. Name the company and its real product or work on the slides."
    repeated, other = _repeats_other_decks(domain, slides)
    if len(repeated) >= 2:
        return (f"Slides {', '.join(map(str, repeated))} nearly repeat the deck made for {other}. Write them for this "
                "company: its gap, its product, its plan.")
    return None


def build(domain: str, title: str, slides: list[dict[str, Any]]) -> dict[str, Any]:
    slides, problem = normalize_slides(slides)
    if problem:
        return {"error": problem, "saved": False}
    if not (title or "").strip() or _PLACEHOLDER.search(title.strip()):
        return {"error": "Give the deck a real title, e.g. \"<Company> × <your company>: <the idea>\".", "saved": False}
    domain = db.normalize_domain(domain)
    account = db.dossier(domain) or {}
    company = account.get("name") or domain.split(".")[0].capitalize()
    problem = check_deck(domain, account.get("name") or "", title, slides)
    if problem:
        return {"error": problem, "saved": False}
    config.DECK_DIR.mkdir(parents=True, exist_ok=True)
    html_path = config.DECK_DIR / f"{domain}.html"
    pdf_path = config.DECK_DIR / f"{domain}.pdf"
    brand = dict(prospect_brand(domain))
    if any(layout_of(s, i, len(slides)) == "insight" for i, s in enumerate(slides)):
        brand["screenshot"] = site_screenshot(domain)
    own = portfolio()
    work = pick_work(" ".join(slide_text(s) for s in slides), own["work"]) \
        if any(layout_of(s, i, len(slides)) == "proof" for i, s in enumerate(slides)) else []
    mine = {"logo": _data_uri(own["logo"]) if own["logo"] else None, "company": _my_company(),
            "work": [{"uri": uri, "caption": w["caption"]} for w in work if (uri := _data_uri(w["path"]))]}
    html_path.write_text(render_html(title, company, slides, brand, mine, domain), encoding="utf-8")
    error = None
    browser = find_browser()
    if not browser:
        error = "No Chrome/Edge/Chromium found. Install Chrome or set CHROME_PATH; the HTML deck was still saved."
    else:
        try:
            pdf_path.unlink(missing_ok=True)
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
    sample = next((s for i, s in enumerate(slides) if layout_of(s, i, len(slides)) == "sample"), None)
    return {"domain": domain, "pdf": str(pdf_path) if not error else None, "html": str(html_path), "error": error,
            "pages": len(slides), "sample": sample["heading"] if sample else None,
            "used": {"their_logo": bool(brand.get("logo")), "their_colour": bool(brand.get("color")),
                     "their_site": bool(brand.get("screenshot")), "your_logo": bool(mine["logo"]),
                     "your_work": len(mine["work"])}}


def _my_company() -> str:
    """The user's company name, from company.md ("- Company name: ...")."""
    try:
        text = (config.BRAIN_DIR / "company.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^\s*-\s*Company name:\s*(.+)$", text, re.M | re.I)
    name = re.sub(r"\s*\(.*?\)", "", m.group(1)).strip() if m else ""
    return "" if not name or "TODO" in name else name


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


def preview_html(domain: str) -> str | None:
    """The deck's pages side by side, small, for the Review screen."""
    path = config.DECK_DIR / f"{db.normalize_domain(domain)}.html"
    if not path.exists():
        return None
    style = ("<style>html,body{background:transparent;overflow-y:hidden}body{display:flex;gap:40px;padding:30px 50px;"
             "margin:0;zoom:.2;width:max-content}.page{flex:none;page-break-after:auto;border-radius:24px;"
             "box-shadow:0 0 0 3px rgba(0,0,0,.08)}</style>")
    return path.read_text(encoding="utf-8").replace("</head>", style + "</head>", 1)
