"""The local dashboard: http://127.0.0.1:4600

Business → Leads → Review → Outreach, plus Activity and Settings. Buttons that
need thinking start a background Claude Code job (jobs.py). Approving and
sending only ever happen here, by a human click.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import compliance, config, db, decks, jobs, lint, mailer, templates
from .brain import BRAIN_FILES, read_brain, suggest_searches, write_brain

WEB_DIR = Path(__file__).parent / "web"
FREE_MAIL = db.FREE_MAIL

_claude_cache: dict[str, Any] = {"at": 0.0, "value": None}
_scheduler: dict[str, Any] = {"last_run": None, "last_result": None}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def ok(data: Any) -> JSONResponse:
    return JSONResponse(data)


async def body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def claude_status(force: bool = False) -> dict[str, Any]:
    # A signed-in result is kept for a minute; a failed check (e.g. Claude Code still starting up
    # after a reboot) is retried after a few seconds instead of showing "signed out" for a minute.
    cached = _claude_cache["value"]
    max_age = 60 if cached and cached.get("logged_in") else 5
    if force or not cached or time.time() - _claude_cache["at"] > max_age:
        _claude_cache.update(at=time.time(), value=jobs.claude_status())
    return _claude_cache["value"]


# ── overview, brain ─────────────────────────────────────────────────────

async def overview(request: Request) -> Response:
    brain = read_brain()
    with db.connect() as conn:
        due = conn.execute("SELECT COUNT(*) FROM drafts WHERE status = 'approved'").fetchone()[0]
        running = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')").fetchone()[0]
        replied = conn.execute("SELECT COUNT(*) FROM contacts WHERE status IN ('replied', 'meeting')").fetchone()[0]
    return ok({
        "workspace": str(config.HOME),
        "claude": await asyncio.to_thread(claude_status),
        "mail": mailer.mail_status(),
        "brain": {k: {"complete": v["complete"], "todos": v["todos_left"]} for k, v in brain.items()},
        "counts": db.counts(),
        "approved_queue": due,
        "replied": replied,
        "jobs_running": running,
        "scheduler": _scheduler,
        "browser": bool(decks.find_browser()),
        "bettercontact": bool(config.get("BETTERCONTACT_API_KEY")),
    })


async def get_brain(request: Request) -> Response:
    return ok({k: {"content": v["content"], "complete": v["complete"], "todos": v["todos_left"]}
               for k, v in read_brain().items()})


async def suggestions(request: Request) -> Response:
    return ok(suggest_searches())


async def put_brain(request: Request) -> Response:
    name = request.path_params["name"]
    if name not in BRAIN_FILES:
        raise ApiError("Unknown brain file", 404)
    content = (await body(request)).get("content", "")
    write_brain(name, content)
    return ok({"saved": name, "complete": read_brain()[name]["complete"]})


# ── leads ───────────────────────────────────────────────────────────────

async def list_accounts(request: Request) -> Response:
    status = request.query_params.get("status") or None
    q = (request.query_params.get("q") or "").lower().strip()
    accounts = db.pipeline(status, None, 1000)
    for a in accounts:
        a["region"] = compliance.region(a)
    if q:
        accounts = [a for a in accounts if q in (a.get("domain") or "") or q in (a.get("name") or "").lower()
                    or q in (a.get("why_now") or "").lower()]
    return ok({"accounts": accounts})


async def add_accounts(request: Request) -> Response:
    data = await body(request)
    raw = data.get("domains") or []
    if isinstance(raw, str):
        # One entry per line. Commas separate entries only in a plain list of domains
        # ("a.com, b.io"), never inside a business name ("Patel Tiles, Surat").
        lines = []
        for line in raw.splitlines():
            parts = [p.strip() for p in line.split(",")]
            lines += parts if all(p and " " not in p and "." in p for p in parts) else [line]
        raw = lines
    names: dict[str, str | None] = {}
    for line in (r.strip() for r in raw):
        if not line:
            continue
        if "@" in line:  # an email: use its company domain, unless it's Gmail/Yahoo etc.
            mail_domain = line.split("@")[-1].lower()
            key = db.no_website_key(line.split("@")[0]) if mail_domain in FREE_MAIL else db.normalize_domain(mail_domain)
            names[key] = None
        elif "." in line and " " not in line:
            names[db.normalize_domain(line)] = None
        else:  # a business name with no website, e.g. "Sharma Sweets Pune"
            names[db.no_website_key(line)] = line
    domains = list(names)
    if not domains:
        raise ApiError("Enter at least one website, business name or email.")
    known = db.known(domains)
    added = []
    for d in domains:
        if known[d]["suppressed"]:
            continue
        db.upsert_account(d, name=names[d], source="manual")
        added.append(d)
    job = jobs.start("research", {"domains": added, "extra": data.get("extra")}) if data.get("research") and added else None
    return ok({"added": added, "skipped_suppressed": [d for d in domains if known[d]["suppressed"]], "job": job})


async def get_account(request: Request) -> Response:
    account = db.dossier(request.path_params["domain"])
    if not account:
        raise ApiError("Not found", 404)
    account["region"] = compliance.region(account)
    for contact in account["contacts"]:
        for d in contact["drafts"]:
            d["lint"] = json.loads(d["lint"]) if d.get("lint") else None
    return ok(account)


async def account_status(request: Request) -> Response:
    status = (await body(request)).get("status")
    db.set_status("account", request.path_params["domain"], status)
    db.log_event(f"status:{status}", "from dashboard", domain=request.path_params["domain"])
    return ok({"status": status})


async def add_contact(request: Request) -> Response:
    """Add a person by hand. Any email works, including @gmail.com test addresses."""
    data = await body(request)
    first = (data.get("first_name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    if not first:
        raise ApiError("Enter a first name.")
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise ApiError("That email address doesn't look right.")
    if email and db.is_suppressed(email):
        raise ApiError(f"{email} is on your do-not-contact list.")
    contact, created = db.upsert_contact(
        request.path_params["domain"], first_name=first, last_name=(data.get("last_name") or "").strip() or None,
        title=(data.get("title") or "").strip() or None, email=email or None,
        email_status="manual" if email else None, source="added by hand",
    )
    return ok({"contact": contact, "created": created})


async def get_templates(request: Request) -> Response:
    sample = None
    with db.connect() as conn:
        row = conn.execute("SELECT c.id FROM contacts c JOIN ideas i ON i.domain = c.domain WHERE c.email IS NOT NULL"
                           " ORDER BY c.id DESC LIMIT 1").fetchone() or conn.execute(
            "SELECT id FROM contacts WHERE email IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            contact = dict(conn.execute("SELECT * FROM contacts WHERE id = ?", (row[0],)).fetchone())
            account = dict(conn.execute("SELECT * FROM accounts WHERE domain = ?", (contact["domain"],)).fetchone() or {})
            ideas = db.rows(conn.execute("SELECT letter, title, pitch, status FROM ideas WHERE domain = ? ORDER BY letter",
                                         (contact["domain"],)))
            sample = {"who": f"{contact.get('first_name') or contact.get('email')} at {account.get('name') or contact['domain']}",
                      "values": templates.values_for(contact, account, ideas)}
    return ok({"steps": templates.all_steps(), "placeholders": templates.PLACEHOLDERS, "sample": sample})


async def put_template(request: Request) -> Response:
    data = await body(request)
    step = int(request.path_params["step"])
    if step not in templates.STEPS:
        raise ApiError("Unknown step", 404)
    try:
        return ok(db.save_step_template(step, data.get("mode") or "ai", data.get("subject"), data.get("body"),
                                        data.get("instruction")))
    except ValueError as e:
        raise ApiError(str(e)) from e


async def sources(request: Request) -> Response:
    return ok({"sources": db.source_stats()})


async def account_feedback(request: Request) -> Response:
    data = await body(request)
    return ok(db.set_feedback(request.path_params["domain"], data.get("verdict") or None, data.get("reason")))


async def suppress(request: Request) -> Response:
    value = (await body(request)).get("value") or request.path_params.get("domain")
    if not value:
        raise ApiError("Nothing to suppress")
    return ok({"suppressed": db.suppress(value, (await body(request)).get("reason") or "dashboard")})


def _pick(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value and value.strip():
            return value.strip()
    return ""


async def import_csv(request: Request) -> Response:
    text = (await body(request)).get("csv") or ""
    if not text.strip():
        raise ApiError("The file is empty.")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    accounts, contacts, skipped = set(), 0, 0
    for raw in reader:
        row = {(k or "").strip().lower().replace("_", " "): (v or "") for k, v in raw.items()}
        email = _pick(row, "email", "email address", "work email", "e-mail").lower()
        domain = db.normalize_domain(_pick(row, "domain", "website", "company domain", "company website", "url",
                                           "company url", "website url"))
        company = _pick(row, "company", "company name", "business", "business name", "organization", "organisation",
                        "account", "account name", "shop", "store")
        first, last = _pick(row, "first name", "firstname", "given name"), _pick(row, "last name", "lastname", "surname")
        full = _pick(row, "name", "full name", "contact", "contact name", "owner", "owner name")
        email_domain = email.split("@")[1] if "@" in email else ""
        if not domain and email_domain and email_domain not in FREE_MAIL:
            domain = email_domain
        if not domain and (company or full or email):
            # No website (common for small businesses on Gmail): keep the lead, keyed by its name.
            domain = db.no_website_key(company or full or email.split("@")[0])
        if not domain or "." not in domain or (email and db.is_suppressed(email)) or db.known([domain])[domain]["suppressed"]:
            skipped += 1
            continue
        db.upsert_account(domain, name=company or full or None, source="csv")
        accounts.add(domain)
        if full and not first:
            first, _, last = full.partition(" ")
        if first or email:
            db.upsert_contact(
                domain, first_name=first or email.split("@")[0], last_name=last or None,
                title=_pick(row, "title", "job title", "position", "role") or None, email=email or None,
                email_status="imported" if email else None,
                linkedin_url=_pick(row, "linkedin", "linkedin url", "linkedin profile", "person linkedin url",
                                   "profile url") or None,
                source="csv",
            )
            contacts += 1
    return ok({"accounts": len(accounts), "contacts": contacts, "skipped": skipped, "domains": sorted(accounts)})


# ── review ──────────────────────────────────────────────────────────────

async def list_drafts(request: Request) -> Response:
    status = request.query_params.get("status") or None
    domain = request.query_params.get("domain") or None
    drafts = db.drafts(status, 2000, domain)
    with db.connect() as conn:
        deck_domains = {r[0] for r in conn.execute("SELECT domain FROM decks WHERE pdf_path IS NOT NULL")}
        accounts = {r["domain"]: r for r in db.rows(conn.execute(
            "SELECT domain, why_now, fit_score, intent_score, location, timezone FROM accounts"))}
    ideas = _ideas_by_domain({d["domain"] for d in drafts})
    with db.connect() as conn:
        # Follow-ups reply in email 1's thread, even when email 1 is already sent and not in this list.
        first_subjects = {r["contact_id"]: r["subject"] for r in db.rows(conn.execute(
            "SELECT contact_id, subject FROM drafts WHERE step = 1 AND channel = 'email'"
        ))}
    for d in drafts:
        d["has_deck"] = d["domain"] in deck_domains
        d["why_now"] = (accounts.get(d["domain"]) or {}).get("why_now")
        d["first_subject"] = first_subjects.get(d["contact_id"])
        d["region"] = compliance.region(accounts.get(d["domain"]) or {"domain": d["domain"]}, d.get("email"))
    return ok({"drafts": drafts, "ideas": ideas})


def _ideas_by_domain(domains: set[str]) -> dict[str, list[dict[str, Any]]]:
    if not domains:
        return {}
    marks = ",".join("?" * len(domains))
    with db.connect() as conn:
        found = db.rows(conn.execute(
            f"SELECT id, domain, letter, title, pitch, why, format, status, picked_by FROM ideas WHERE domain IN ({marks}) ORDER BY letter",
            tuple(domains),
        ))
    out: dict[str, list[dict[str, Any]]] = {}
    for idea in found:
        out.setdefault(idea["domain"], []).append(idea)
    return out


async def pick_idea(request: Request) -> Response:
    data = await body(request)
    contact_id = int(data["contact_id"]) if data.get("contact_id") else None
    idea = db.pick_idea(int(request.path_params["id"]), contact_id)
    if not idea:
        raise ApiError("Idea not found", 404)
    if contact_id:
        with db.connect() as conn:
            conn.execute("UPDATE contacts SET reply_pick = ? WHERE id = ?", (idea["letter"], contact_id))
    return ok({"picked": idea})


async def update_draft(request: Request) -> Response:
    draft_id = int(request.path_params["id"])
    data = await body(request)
    with db.connect() as conn:
        draft = db.one(conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)))
        if not draft:
            raise ApiError("Draft not found", 404)
        if draft["status"] == "sent":
            raise ApiError("This email was already sent.")
        contact = db.one(conn.execute("SELECT * FROM contacts WHERE id = ?", (draft["contact_id"],))) or {}
        account = db.one(conn.execute("SELECT name FROM accounts WHERE domain = ?", (contact.get("domain"),))) or {}
        subject = data.get("subject", draft["subject"])
        text = data.get("body", draft["body"])
        attach = int(bool(data.get("attach_deck", draft["attach_deck"])))
        result = lint.check(subject, text, draft["step"], account.get("name"), contact.get("first_name"),
                            kind=draft.get("kind") or "sequence")
        content_changed = subject != draft["subject"] or text != draft["body"]
        status = "draft" if content_changed else draft["status"]
        conn.execute("UPDATE drafts SET subject = ?, body = ?, attach_deck = ?, lint = ?, status = ?, updated_at = ?"
                     " WHERE id = ?", (subject, text, attach, json.dumps(result), status, db.now(), draft_id))
    return ok({"id": draft_id, "lint": result, "status": status})


async def draft_action(request: Request) -> Response:
    draft_id = int(request.path_params["id"])
    action = request.path_params["action"]
    if action == "test":
        return ok(await asyncio.to_thread(mailer.send_test, draft_id))
    return ok(_review([draft_id], action))


async def bulk_drafts(request: Request) -> Response:
    data = await body(request)
    ids = [int(i) for i in data.get("ids") or []]
    return ok(_review(ids, data.get("action", "")))


def _review(ids: list[int], action: str) -> dict[str, Any]:
    if action not in ("approve", "reject", "reopen"):
        raise ApiError("Unknown action")
    target = {"approve": "approved", "reject": "rejected", "reopen": "draft"}[action]
    done, refused = [], []
    for draft in db.drafts(None, 100_000):
        if draft["id"] not in ids:
            continue
        if action == "approve" and draft["lint"] and not draft["lint"].get("ok", True):
            refused.append({"id": draft["id"], "reason": "Fix the red issues first."})
        elif action == "approve" and not draft.get("email") and draft["channel"] == "email":
            refused.append({"id": draft["id"], "reason": "This contact has no email address."})
        elif draft["status"] in ("sent", "cancelled"):
            refused.append({"id": draft["id"], "reason": f"Already {draft['status']}."})
        else:
            db.set_status("draft", draft["id"], target)
            done.append(draft["id"])
    return {"done": done, "refused": refused}


# ── outreach ────────────────────────────────────────────────────────────

async def outreach(request: Request) -> Response:
    with db.connect() as conn:
        rows = db.rows(conn.execute("""
            SELECT c.id, c.first_name, c.last_name, c.title, c.email, c.email_status, c.status, c.domain,
                   c.last_reply, c.reply_pick, c.reply_kind, a.name AS company
            FROM contacts c LEFT JOIN accounts a ON a.domain = c.domain
            WHERE EXISTS (SELECT 1 FROM drafts d WHERE d.contact_id = c.id AND d.status IN ('approved', 'sent'))
               OR c.status IN ('contacted', 'replied', 'meeting', 'bounced')
            ORDER BY c.updated_at DESC
        """))
        for row in rows:
            steps = db.rows(conn.execute(
                "SELECT id, step, kind, subject, status, sent_at, send_after_days FROM drafts"
                " WHERE contact_id = ? AND channel = 'email' ORDER BY step", (row["id"],)
            ))
            first_sent = next((s["sent_at"] for s in steps if s["step"] == 1 and s["sent_at"]), None)
            for s in steps:
                if first_sent and s["status"] == "approved" and s["step"] > 1:
                    s["due"] = (datetime.fromisoformat(first_sent) + timedelta(days=s["send_after_days"] or 3)).date().isoformat()
            row["steps"] = steps
            row["last_sent"] = max((s["sent_at"] for s in steps if s["sent_at"]), default=None)
    return ok({"contacts": rows, "ideas": _ideas_by_domain({r["domain"] for r in rows}),
               "mail": mailer.mail_status(), "scheduler": _scheduler})


async def send(request: Request) -> Response:
    data = await body(request)
    limit = int(data.get("limit") or 25)
    if bool(data.get("dry_run", True)):
        return ok(await asyncio.to_thread(mailer.send_approved, limit, True))
    # Real sends are spaced out (default one a minute), so they run in the background.
    return ok(mailer.start_background_send(limit))


async def send_status(request: Request) -> Response:
    return ok({k: v for k, v in mailer.PROGRESS.items()})


async def send_stop(request: Request) -> Response:
    return ok({"stopping": mailer.stop_send()})


async def replies(request: Request) -> Response:
    return ok(await asyncio.to_thread(mailer.check_replies, int((await body(request)).get("days") or 14)))


async def verify_contact(request: Request) -> Response:
    """One BetterContact lookup for one person. Only from a click; a pending lookup is resumed, never resubmitted."""
    from .providers import bettercontact

    if not bettercontact.enabled():
        raise ApiError("Add a BetterContact API key in Settings first.")
    contact_id = int(request.path_params["id"])
    contact = db.get_contact(contact_id)
    if not contact:
        raise ApiError("Unknown person", 404)
    with db.connect() as conn:
        account = db.one(conn.execute("SELECT name FROM accounts WHERE domain = ?", (contact["domain"],))) or {}
    key = f"bettercontact:{contact_id}"
    pending = db.cache_get(key)
    try:
        if pending:
            result = await asyncio.to_thread(bettercontact.fetch, pending) or {
                "done": False, "request_id": pending, "note": "BetterContact is still working on it. Try again in a minute."}
        else:
            result = await asyncio.to_thread(
                bettercontact.find_email, contact.get("first_name") or "", contact.get("last_name") or "",
                contact["domain"] if db.has_website(contact["domain"]) else None, account.get("name"),
                contact.get("linkedin_url"))
    except (RuntimeError, ValueError) as e:
        raise ApiError(str(e)) from e
    except httpx.HTTPError as e:
        raise ApiError(f"Couldn't reach BetterContact: {e}") from e
    if not result["done"]:
        db.cache_set(key, result["request_id"], 24)
        return ok(result)
    db.cache_set(key, "", -1)
    if result["verified"] and result["email"]:
        if db.is_suppressed(result["email"]):
            raise ApiError(f"BetterContact found {result['email']}, but it's on your do-not-contact list.")
        try:
            db.set_contact_email(contact_id, result["email"], "verified", f"Verified by BetterContact ({result['status']})")
        except ValueError as e:
            raise ApiError(str(e)) from e
    else:
        db.log_event("email:unverified", f"BetterContact: {result['status']}", contact_id, contact["domain"])
    return ok(result)


async def contact_status(request: Request) -> Response:
    status = (await body(request)).get("status")
    contact_id = int(request.path_params["id"])
    db.set_status("contact", contact_id, status)
    contact = db.get_contact(contact_id) or {}
    db.log_event(f"status:{status}", "from dashboard", contact_id, contact.get("domain"))
    if status in ("meeting", "replied", "unsubscribed", "disqualified", "bounced"):
        with db.connect() as conn:
            conn.execute("UPDATE drafts SET status = 'cancelled' WHERE contact_id = ? AND status IN ('draft', 'approved')",
                         (contact_id,))
    if status == "meeting" and contact.get("domain"):
        db.set_status("account", contact["domain"], "meeting")
    if status == "unsubscribed" and contact.get("email"):
        db.suppress(contact["email"], "unsubscribed")
    return ok({"status": status})


# ── jobs, settings, files ───────────────────────────────────────────────

async def list_jobs(request: Request) -> Response:
    return ok({"jobs": jobs.recent(40)})


async def create_job(request: Request) -> Response:
    data = await body(request)
    claude = claude_status(force=True)
    if not claude.get("logged_in"):
        raise ApiError(claude.get("help") or "Claude Code isn't ready.")
    return ok(jobs.start(data.pop("kind", ""), data))


async def get_job(request: Request) -> Response:
    job = jobs.get(int(request.path_params["id"]))
    if not job:
        raise ApiError("Not found", 404)
    return ok(job)


async def cancel_job(request: Request) -> Response:
    return ok({"cancelled": jobs.cancel(int(request.path_params["id"]))})


async def get_settings(request: Request) -> Response:
    return ok({"settings": config.public_settings(), "claude": await asyncio.to_thread(claude_status, True),
               "browser": decks.find_browser(), "workspace": str(config.HOME), "env_file": str(config.ENV_FILE)})


async def put_settings(request: Request) -> Response:
    data = await body(request)
    updates: dict[str, str] = {}
    for key, value in data.items():
        if key not in config.SETTINGS:
            continue
        kind = config.SETTINGS[key][1]
        if kind == "secret" and not str(value).strip():
            continue
        if kind == "bool":
            value = "true" if value in (True, "true", "on", "1", 1) else "false"
        if kind == "int":
            try:
                value = str(max(0, int(value)))
            except (TypeError, ValueError):
                raise ApiError(f"{config.SETTINGS[key][0]} must be a number.")
        if key == "CLAUDE_MODEL":
            value = config.normalize_model(str(value))
        updates[key] = str(value).strip()
    config.save_env(updates)
    return ok({"saved": sorted(updates)})


async def test_gmail(request: Request) -> Response:
    return ok({"result": await asyncio.to_thread(mailer.test_login)})


async def deck_file(request: Request) -> Response:
    deck = decks.get(request.path_params["domain"])
    if not deck:
        raise ApiError("No deck yet", 404)
    if deck.get("pdf_path") and Path(deck["pdf_path"]).exists():
        return FileResponse(deck["pdf_path"], media_type="application/pdf")
    html_path = config.DECK_DIR / f"{db.normalize_domain(request.path_params['domain'])}.html"
    if html_path.exists():
        return FileResponse(html_path, media_type="text/html")
    raise ApiError("Deck file missing", 404)


async def export(request: Request) -> Response:
    from .server import export_csv

    kind = request.path_params["kind"]
    if kind not in ("sequences", "contacts", "accounts"):
        raise ApiError("Unknown export", 404)
    result = export_csv(kind, request.query_params.get("status") or None)
    if not result.get("path"):
        raise ApiError("Nothing to export yet.")
    return FileResponse(result["path"], media_type="text/csv", filename=Path(result["path"]).name)


async def index(request: Request) -> Response:
    """Serve the page with versioned asset URLs, so a browser never keeps an old app.js after an update."""
    from starlette.responses import HTMLResponse

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "app.css"):
        version = int((WEB_DIR / asset).stat().st_mtime)
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={version}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# ── app ─────────────────────────────────────────────────────────────────

def _wrap(handler):
    async def run(request: Request) -> Response:
        try:
            return await handler(request)
        except ApiError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return run


class LocalOnly:
    """Block DNS-rebinding and cross-site requests: this app holds your Gmail session."""

    def __init__(self, app, port: int):
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            bad_host = headers.get("host") not in self.hosts
            unsafe = scope["method"] not in ("GET", "HEAD") and headers.get("x-leadsmith-client") != "1"
            if bad_host or unsafe:
                await JSONResponse({"error": "Forbidden"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


AUTO_MAX_OVERDUE_DAYS = 2  # automatic follow-ups never send anything later than this


async def _scheduler_loop() -> None:
    """While the dashboard is open: detect replies and send due follow-ups every 30 minutes."""
    # Give you time to look first: after the dashboard was closed for days, nothing goes out unseen.
    await asyncio.sleep(5 * 60)
    while True:
        status = mailer.mail_status()
        if config.get_bool("AUTO_FOLLOWUPS", True) and status["send_enabled"] and status["app_password_set"]:
            try:
                found = await asyncio.to_thread(mailer.check_replies, 14)
                sent = await asyncio.to_thread(mailer.send_approved, 25, False, True, AUTO_MAX_OVERDUE_DAYS)
                _scheduler.update(last_run=db.now(), last_result={
                    "replies": len(found.get("replies", [])), "bounces": len(found.get("bounces", [])),
                    "followups_sent": len(sent.get("sent", [])), "overdue_waiting": len(sent.get("overdue", [])), "error": found.get("error") or sent.get("error"),
                })
            except Exception as exc:  # noqa: BLE001 - keep the loop alive, show the error
                _scheduler.update(last_run=db.now(), last_result={"error": str(exc)})
        await asyncio.sleep(30 * 60)


def create_app(port: int = 4600):
    routes = [
        Route("/", index),
        Route("/api/overview", _wrap(overview)),
        Route("/api/brain", _wrap(get_brain)),
        Route("/api/brain/{name}", _wrap(put_brain), methods=["PUT"]),
        Route("/api/suggestions", _wrap(suggestions)),
        Route("/api/sources", _wrap(sources)),
        Route("/api/templates", _wrap(get_templates)),
        Route("/api/templates/{step}", _wrap(put_template), methods=["PUT"]),
        Route("/api/accounts", _wrap(list_accounts)),
        Route("/api/accounts", _wrap(add_accounts), methods=["POST"]),
        Route("/api/accounts/{domain}", _wrap(get_account)),
        Route("/api/accounts/{domain}/status", _wrap(account_status), methods=["POST"]),
        Route("/api/accounts/{domain}/suppress", _wrap(suppress), methods=["POST"]),
        Route("/api/accounts/{domain}/contacts", _wrap(add_contact), methods=["POST"]),
        Route("/api/accounts/{domain}/feedback", _wrap(account_feedback), methods=["POST"]),
        Route("/api/suppress", _wrap(suppress), methods=["POST"]),
        Route("/api/import", _wrap(import_csv), methods=["POST"]),
        Route("/api/drafts", _wrap(list_drafts)),
        Route("/api/drafts/bulk", _wrap(bulk_drafts), methods=["POST"]),
        Route("/api/drafts/{id:int}", _wrap(update_draft), methods=["PUT"]),
        Route("/api/drafts/{id:int}/{action}", _wrap(draft_action), methods=["POST"]),
        Route("/api/outreach", _wrap(outreach)),
        Route("/api/ideas/{id:int}/pick", _wrap(pick_idea), methods=["POST"]),
        Route("/api/send", _wrap(send), methods=["POST"]),
        Route("/api/send/status", _wrap(send_status)),
        Route("/api/send/stop", _wrap(send_stop), methods=["POST"]),
        Route("/api/replies", _wrap(replies), methods=["POST"]),
        Route("/api/contacts/{id:int}/verify", _wrap(verify_contact), methods=["POST"]),
        Route("/api/contacts/{id:int}/status", _wrap(contact_status), methods=["POST"]),
        Route("/api/jobs", _wrap(list_jobs)),
        Route("/api/jobs", _wrap(create_job), methods=["POST"]),
        Route("/api/jobs/{id:int}", _wrap(get_job)),
        Route("/api/jobs/{id:int}/cancel", _wrap(cancel_job), methods=["POST"]),
        Route("/api/settings", _wrap(get_settings)),
        Route("/api/settings", _wrap(put_settings), methods=["PUT"]),
        Route("/api/settings/test-gmail", _wrap(test_gmail), methods=["POST"]),
        Route("/api/decks/{domain}", _wrap(deck_file)),
        Route("/api/export/{kind}", _wrap(export)),
        Mount("/static", StaticFiles(directory=WEB_DIR), name="static"),
    ]

    @asynccontextmanager
    async def lifespan(app):
        jobs.mark_orphans()
        with db.connect() as conn:  # a send interrupted mid-way goes back to the queue for you to check
            stuck = conn.execute("UPDATE drafts SET status = 'approved' WHERE status = 'sending'").rowcount
        if stuck:
            db.log_event("recovered", f"{stuck} interrupted send(s) back in the queue; check Gmail Sent before sending again")
        task = asyncio.create_task(_scheduler_loop())
        try:
            yield
        finally:
            task.cancel()

    app = Starlette(routes=routes, lifespan=lifespan)
    return LocalOnly(app, port)


def run(port: int = 4600, open_browser: bool = True) -> None:
    import threading
    import webbrowser

    import uvicorn

    config.load_env()
    config.ensure_home()
    url = f"http://127.0.0.1:{port}"
    print(f"Leadsmith dashboard: {url}   (workspace {config.HOME})  Ctrl+C to stop")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(port), host="127.0.0.1", port=port, log_level="warning")
