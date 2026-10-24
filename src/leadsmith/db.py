"""The pipeline store: one SQLite file shared by every agent.

Signals are stored as observations (one row per fact, with source and URL)
rather than columns, so a new kind of signal never needs a migration and
every claim in an email can be traced back to where it came from.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    domain        TEXT PRIMARY KEY,
    name          TEXT,
    industry      TEXT,
    employees     INTEGER,
    location      TEXT,
    linkedin_url  TEXT,
    summary       TEXT,
    fit_score     INTEGER,
    intent_score  INTEGER,
    why_now       TEXT,
    angle         TEXT,
    research      TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    source        TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    domain       TEXT NOT NULL,
    type         TEXT NOT NULL,
    title        TEXT NOT NULL,
    detail       TEXT,
    url          TEXT,
    source       TEXT,
    observed_at  TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (domain, type, title)
);

CREATE TABLE IF NOT EXISTS contacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    domain        TEXT NOT NULL,
    first_name    TEXT,
    last_name     TEXT,
    title         TEXT,
    email         TEXT,
    email_status  TEXT,
    linkedin_url  TEXT,
    persona       TEXT,
    notes         TEXT,
    source        TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS contacts_email ON contacts(lower(email)) WHERE email IS NOT NULL AND email != '';

CREATE TABLE IF NOT EXISTS drafts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id       INTEGER NOT NULL REFERENCES contacts(id),
    step             INTEGER NOT NULL DEFAULT 1,
    channel          TEXT NOT NULL DEFAULT 'email',
    subject          TEXT,
    body             TEXT NOT NULL,
    send_after_days  INTEGER NOT NULL DEFAULT 0,
    attach_deck      INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'draft',
    lint             TEXT,
    message_id       TEXT,
    sent_at          TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (contact_id, step, channel)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER,
    domain      TEXT,
    type        TEXT NOT NULL,
    detail      TEXT,
    at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
    value       TEXT PRIMARY KEY,
    reason      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decks (
    domain      TEXT PRIMARY KEY,
    title       TEXT,
    slides      TEXT NOT NULL,
    pdf_path    TEXT,
    error       TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',
    log          TEXT NOT NULL DEFAULT '',
    result       TEXT,
    cost_usd     REAL,
    started_at   TEXT,
    finished_at  TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ideas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT NOT NULL,
    letter      TEXT NOT NULL,
    title       TEXT NOT NULL,
    pitch       TEXT,
    why         TEXT,
    format      TEXT,
    status      TEXT NOT NULL DEFAULT 'offered',
    picked_by   INTEGER,
    picked_at   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS step_templates (
    step        INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL DEFAULT 'ai',
    subject     TEXT,
    body        TEXT,
    instruction TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cache (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
"""

ACCOUNT_STATUSES = ("new", "researched", "ready", "contacted", "replied", "meeting", "won", "lost", "disqualified")
CONTACT_STATUSES = ("new", "drafted", "contacted", "replied", "meeting", "bounced", "unsubscribed", "disqualified")
DRAFT_STATUSES = ("draft", "approved", "sent", "rejected", "cancelled")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_domain(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^[a-z]+://", "", value)
    value = value.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    if value.startswith("www."):
        value = value[4:]
    return value


_ready: set[str] = set()

# column -> DDL, for databases created by an older version
MIGRATIONS = {
    ("drafts", "attach_deck"): "ALTER TABLE drafts ADD COLUMN attach_deck INTEGER NOT NULL DEFAULT 0",
    ("contacts", "last_reply"): "ALTER TABLE contacts ADD COLUMN last_reply TEXT",
    ("accounts", "timezone"): "ALTER TABLE accounts ADD COLUMN timezone TEXT",
    ("accounts", "feedback"): "ALTER TABLE accounts ADD COLUMN feedback TEXT",
    ("accounts", "feedback_reason"): "ALTER TABLE accounts ADD COLUMN feedback_reason TEXT",
    ("accounts", "feedback_at"): "ALTER TABLE accounts ADD COLUMN feedback_at TEXT",
    ("contacts", "reply_pick"): "ALTER TABLE contacts ADD COLUMN reply_pick TEXT",
    ("contacts", "reply_kind"): "ALTER TABLE contacts ADD COLUMN reply_kind TEXT",
    ("contacts", "last_reply_id"): "ALTER TABLE contacts ADD COLUMN last_reply_id TEXT",
    ("drafts", "kind"): "ALTER TABLE drafts ADD COLUMN kind TEXT NOT NULL DEFAULT 'sequence'",
}


def _migrate(conn: sqlite3.Connection) -> None:
    path = str(config.DB_PATH)
    if path in _ready:
        return
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    for (table, column), ddl in MIGRATIONS.items():
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
    conn.commit()
    _ready.add(path)


NO_WEBSITE = ".no-website"
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com", "live.com",
             "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "yandex.com", "zoho.com",
             "zohomail.in", "mail.com", "rediffmail.com"}


def no_website_key(name: str) -> str:
    """A stable key for a business that has no website (e.g. a shop that only uses Gmail)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60]
    return f"{slug or 'unnamed'}{NO_WEBSITE}"


def has_website(domain: str) -> bool:
    return bool(domain) and not domain.endswith(NO_WEBSITE)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    config.HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]


def one(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    return dict(row) if row else None


# ── cache ────────────────────────────────────────────────────────────────

def cache_get(key: str) -> Any | None:
    with connect() as conn:
        row = one(conn.execute("SELECT value, expires_at FROM cache WHERE key = ?", (key,)))
    if not row or row["expires_at"] < now():
        return None
    return json.loads(row["value"])


def cache_set(key: str, value: Any, ttl_hours: float) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).replace(microsecond=0).isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), expires),
        )


# ── accounts and signals ─────────────────────────────────────────────────

ACCOUNT_FIELDS = (
    "name", "industry", "employees", "location", "timezone", "linkedin_url", "summary", "fit_score",
    "intent_score", "why_now", "angle", "research", "status", "source",
)


def upsert_account(domain: str, **fields: Any) -> tuple[dict[str, Any], bool]:
    domain = normalize_domain(domain)
    if not domain:
        raise ValueError("domain is required")
    values = {k: v for k, v in fields.items() if k in ACCOUNT_FIELDS and v not in (None, "")}
    if "status" in values and values["status"] not in ACCOUNT_STATUSES:
        raise ValueError(f"status must be one of {ACCOUNT_STATUSES}")
    stamp = now()
    with connect() as conn:
        existing = one(conn.execute("SELECT domain FROM accounts WHERE domain = ?", (domain,)))
        if existing:
            if values:
                sets = ", ".join(f"{k} = ?" for k in values)
                conn.execute(
                    f"UPDATE accounts SET {sets}, updated_at = ? WHERE domain = ?",
                    (*values.values(), stamp, domain),
                )
        else:
            cols = ["domain", *values.keys(), "created_at", "updated_at"]
            conn.execute(
                f"INSERT INTO accounts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (domain, *values.values(), stamp, stamp),
            )
        account = one(conn.execute("SELECT * FROM accounts WHERE domain = ?", (domain,)))
    return account or {}, not existing


def add_signal(domain: str, type: str, title: str, detail: str | None = None, url: str | None = None,
               source: str | None = None, observed_at: str | None = None) -> bool:
    domain = normalize_domain(domain)
    with connect() as conn:
        if not one(conn.execute("SELECT domain FROM accounts WHERE domain = ?", (domain,))):
            stamp = now()
            conn.execute(
                "INSERT INTO accounts (domain, source, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (domain, source, stamp, stamp),
            )
        cur = conn.execute(
            "INSERT OR IGNORE INTO signals (domain, type, title, detail, url, source, observed_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (domain, type.strip().lower(), title.strip(), detail, url, source, observed_at, now()),
        )
        return cur.rowcount > 0


def known(domains: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with connect() as conn:
        for raw in domains:
            domain = normalize_domain(raw)
            account = one(conn.execute(
                "SELECT status, fit_score, intent_score, updated_at FROM accounts WHERE domain = ?", (domain,)
            ))
            suppressed = one(conn.execute("SELECT reason FROM suppression WHERE value = ?", (domain,)))
            result[domain] = {
                "in_pipeline": bool(account),
                "status": account["status"] if account else None,
                "suppressed": bool(suppressed),
                "suppressed_reason": suppressed["reason"] if suppressed else None,
            }
    return result


def pipeline(status: str | None = None, min_score: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = """
        SELECT a.domain, a.name, a.status, a.fit_score, a.intent_score, a.why_now, a.updated_at, a.feedback, a.source,
               a.location, a.timezone,
               (SELECT COUNT(*) FROM signals s WHERE s.domain = a.domain) AS signals,
               (SELECT COUNT(*) FROM contacts c WHERE c.domain = a.domain) AS contacts,
               (SELECT COUNT(*) FROM drafts d JOIN contacts c ON c.id = d.contact_id
                 WHERE c.domain = a.domain AND d.status = 'draft') AS drafts_to_review
        FROM accounts a WHERE 1 = 1
    """
    params: list[Any] = []
    if status:
        sql += " AND a.status = ?"
        params.append(status)
    if min_score is not None:
        sql += " AND COALESCE(a.fit_score, 0) + COALESCE(a.intent_score, 0) >= ?"
        params.append(min_score)
    sql += " ORDER BY COALESCE(a.fit_score, 0) + COALESCE(a.intent_score, 0) DESC, a.updated_at DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return rows(conn.execute(sql, params))


def dossier(domain: str) -> dict[str, Any] | None:
    domain = normalize_domain(domain)
    with connect() as conn:
        account = one(conn.execute("SELECT * FROM accounts WHERE domain = ?", (domain,)))
        if not account:
            return None
        account["signals"] = rows(conn.execute(
            "SELECT type, title, detail, url, source, observed_at FROM signals WHERE domain = ? ORDER BY observed_at DESC",
            (domain,),
        ))
        contacts = rows(conn.execute("SELECT * FROM contacts WHERE domain = ? ORDER BY id", (domain,)))
        for contact in contacts:
            contact["drafts"] = rows(conn.execute(
                "SELECT id, step, channel, subject, body, status, send_after_days, attach_deck, sent_at, lint FROM drafts"
                " WHERE contact_id = ? ORDER BY step", (contact["id"],),
            ))
        account["contacts"] = contacts
        account["deck"] = one(conn.execute("SELECT title, pdf_path, error, updated_at FROM decks WHERE domain = ?", (domain,)))
        account["ideas"] = rows(conn.execute(
            "SELECT id, letter, title, pitch, why, format, status, picked_by, picked_at FROM ideas WHERE domain = ? ORDER BY letter",
            (domain,),
        ))
        account["events"] = rows(conn.execute(
            "SELECT type, detail, at FROM events WHERE domain = ? ORDER BY at DESC LIMIT 20", (domain,)
        ))
    return account


# ── contacts and drafts ──────────────────────────────────────────────────

CONTACT_FIELDS = ("first_name", "last_name", "title", "email", "email_status", "linkedin_url",
                  "persona", "notes", "source", "status")


def upsert_contact(domain: str, **fields: Any) -> tuple[dict[str, Any], bool]:
    domain = normalize_domain(domain)
    values = {k: v for k, v in fields.items() if k in CONTACT_FIELDS and v not in (None, "")}
    if "email" in values:
        values["email"] = values["email"].strip().lower()
    stamp = now()
    with connect() as conn:
        if not one(conn.execute("SELECT domain FROM accounts WHERE domain = ?", (domain,))):
            conn.execute(
                "INSERT INTO accounts (domain, created_at, updated_at) VALUES (?, ?, ?)", (domain, stamp, stamp)
            )
        existing = None
        if values.get("email"):
            existing = one(conn.execute("SELECT id FROM contacts WHERE lower(email) = ?", (values["email"],)))
        if not existing and values.get("first_name"):
            existing = one(conn.execute(
                "SELECT id FROM contacts WHERE domain = ? AND lower(first_name) = lower(?)"
                " AND lower(COALESCE(last_name, '')) = lower(?)",
                (domain, values["first_name"], values.get("last_name", "")),
            ))
        if existing:
            if values:
                sets = ", ".join(f"{k} = ?" for k in values)
                conn.execute(f"UPDATE contacts SET {sets}, updated_at = ? WHERE id = ?",
                             (*values.values(), stamp, existing["id"]))
            contact_id = existing["id"]
        else:
            cols = ["domain", *values.keys(), "created_at", "updated_at"]
            cur = conn.execute(
                f"INSERT INTO contacts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (domain, *values.values(), stamp, stamp),
            )
            contact_id = cur.lastrowid
        contact = one(conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)))
    return contact or {}, not existing


def set_contact_email(contact_id: int, email: str | None, status: str, note: str | None = None) -> dict[str, Any]:
    """Replace one person's email (e.g. after verification). Refuses an address another contact already has."""
    contact = get_contact(contact_id)
    if not contact:
        raise ValueError(f"No contact {contact_id}")
    email = (email or "").strip().lower() or contact.get("email")
    with connect() as conn:
        other = one(conn.execute("SELECT id FROM contacts WHERE lower(email) = ? AND id != ?", (email or "", contact_id)))
        if other:
            raise ValueError(f"{email} already belongs to another person in your leads.")
        notes = contact.get("notes") or ""
        if note:
            notes = f"{notes}\n{note}".strip()
        conn.execute("UPDATE contacts SET email = ?, email_status = ?, notes = ?, updated_at = ? WHERE id = ?",
                     (email, status, notes or None, now(), contact_id))
    log_event("email:" + status, note, contact_id, contact["domain"])
    return get_contact(contact_id) or {}


def get_contact(contact_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return one(conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)))


def upsert_draft(contact_id: int, step: int, subject: str | None, body: str, channel: str,
                 send_after_days: int, lint: dict[str, Any]) -> dict[str, Any]:
    stamp = now()
    with connect() as conn:
        existing = one(conn.execute(
            "SELECT id, status FROM drafts WHERE contact_id = ? AND step = ? AND channel = ?",
            (contact_id, step, channel),
        ))
        if existing and existing["status"] == "sent":
            raise ValueError(f"step {step} for this contact was already sent; it can't be rewritten")
        if existing:
            conn.execute(
                "UPDATE drafts SET subject = ?, body = ?, send_after_days = ?, lint = ?, status = 'draft',"
                " updated_at = ? WHERE id = ?",
                (subject, body, send_after_days, json.dumps(lint), stamp, existing["id"]),
            )
            draft_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO drafts (contact_id, step, channel, subject, body, send_after_days, lint,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (contact_id, step, channel, subject, body, send_after_days, json.dumps(lint), stamp, stamp),
            )
            draft_id = cur.lastrowid
        conn.execute(
            "UPDATE contacts SET status = 'drafted', updated_at = ? WHERE id = ? AND status = 'new'",
            (stamp, contact_id),
        )
        return one(conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,))) or {}


REPLY_STEP_BASE = 10  # replies are steps 10, 11, ... so they never collide with the sequence's 1-3


def save_reply_draft(contact_id: int, body: str, lint: dict[str, Any]) -> dict[str, Any]:
    """Draft your answer to someone who replied. Reuses an unsent reply draft; otherwise the next reply step."""
    stamp = now()
    with connect() as conn:
        open_draft = one(conn.execute(
            "SELECT id FROM drafts WHERE contact_id = ? AND kind = 'reply' AND status IN ('draft', 'approved', 'rejected', 'cancelled')"
            " ORDER BY step DESC LIMIT 1", (contact_id,)))
        first = one(conn.execute("SELECT subject FROM drafts WHERE contact_id = ? AND step = 1 AND channel = 'email'", (contact_id,)))
        subject = (first or {}).get("subject")
        if open_draft:
            conn.execute("UPDATE drafts SET body = ?, subject = ?, lint = ?, status = 'draft', updated_at = ? WHERE id = ?",
                         (body, subject, json.dumps(lint), stamp, open_draft["id"]))
            draft_id = open_draft["id"]
        else:
            top = conn.execute("SELECT MAX(step) FROM drafts WHERE contact_id = ? AND kind = 'reply'", (contact_id,)).fetchone()[0]
            step = (top or REPLY_STEP_BASE - 1) + 1
            cur = conn.execute(
                "INSERT INTO drafts (contact_id, step, channel, kind, subject, body, send_after_days, lint, created_at, updated_at)"
                " VALUES (?, ?, 'email', 'reply', ?, ?, 0, ?, ?, ?)",
                (contact_id, step, subject, body, json.dumps(lint), stamp, stamp))
            draft_id = cur.lastrowid
        return one(conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,))) or {}


def drafts(status: str | None = None, limit: int = 100, domain: str | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT d.id, d.step, d.channel, d.subject, d.body, d.status, d.send_after_days, d.lint, d.sent_at, d.kind,
               d.attach_deck, d.updated_at,
               c.id AS contact_id, c.first_name, c.last_name, c.title, c.email, c.email_status, c.domain,
               c.notes AS contact_notes, c.linkedin_url,
               a.name AS company
        FROM drafts d JOIN contacts c ON c.id = d.contact_id LEFT JOIN accounts a ON a.domain = c.domain
        WHERE 1 = 1
    """
    params: list[Any] = []
    if status:
        sql += " AND d.status = ?"
        params.append(status)
    if domain:
        sql += " AND c.domain = ?"
        params.append(normalize_domain(domain))
    sql += " ORDER BY c.domain, c.id, d.step LIMIT ?"
    params.append(limit)
    with connect() as conn:
        result = rows(conn.execute(sql, params))
    for row in result:
        row["lint"] = json.loads(row["lint"]) if row.get("lint") else None
    return result


def set_status(kind: str, ident: str | int, status: str) -> int:
    table, key, allowed = {
        "account": ("accounts", "domain", ACCOUNT_STATUSES),
        "contact": ("contacts", "id", CONTACT_STATUSES),
        "draft": ("drafts", "id", DRAFT_STATUSES),
    }[kind]
    if status not in allowed:
        raise ValueError(f"{kind} status must be one of {allowed}")
    if kind == "account":
        ident = normalize_domain(str(ident))
    with connect() as conn:
        if kind == "draft" and status != "sent":
            current = one(conn.execute("SELECT status FROM drafts WHERE id = ?", (ident,)))
            if current and current["status"] == "sent":
                raise ValueError(f"draft {ident} was already sent")
        cur = conn.execute(f"UPDATE {table} SET status = ?, updated_at = ? WHERE {key} = ?", (status, now(), ident))
        return cur.rowcount


def log_event(type: str, detail: str | None = None, contact_id: int | None = None, domain: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO events (contact_id, domain, type, detail, at) VALUES (?, ?, ?, ?, ?)",
            (contact_id, normalize_domain(domain) if domain else None, type, detail, now()),
        )


def suppress(value: str, reason: str | None = None) -> str:
    value = value.strip().lower()
    if "@" not in value:
        value = normalize_domain(value)
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO suppression (value, reason, created_at) VALUES (?, ?, ?)",
            (value, reason, now()),
        )
        if "@" in value:
            conn.execute("UPDATE contacts SET status = 'unsubscribed' WHERE lower(email) = ?", (value,))
            conn.execute(
                "UPDATE drafts SET status = 'cancelled' WHERE status IN ('draft', 'approved') AND contact_id IN"
                " (SELECT id FROM contacts WHERE lower(email) = ?)", (value,),
            )
        else:
            conn.execute("UPDATE accounts SET status = 'disqualified' WHERE domain = ?", (value,))
            conn.execute(
                "UPDATE drafts SET status = 'cancelled' WHERE status IN ('draft', 'approved') AND contact_id IN"
                " (SELECT id FROM contacts WHERE domain = ? OR lower(email) LIKE ?)", (value, f"%@{value}"),
            )
    return value


def is_suppressed(email: str) -> bool:
    email = email.strip().lower()
    domain = email.split("@")[-1]
    with connect() as conn:
        return bool(one(conn.execute("SELECT 1 FROM suppression WHERE value IN (?, ?)", (email, domain))))


# ── ideas: the menu a prospect picks from ───────────────────────────────

IDEA_LETTERS = "ABCDE"


def save_ideas(domain: str, ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace the offered ideas for an account. An idea someone already picked is kept."""
    domain = normalize_domain(domain)
    stamp = now()
    with connect() as conn:
        if not one(conn.execute("SELECT domain FROM accounts WHERE domain = ?", (domain,))):
            conn.execute("INSERT INTO accounts (domain, created_at, updated_at) VALUES (?, ?, ?)", (domain, stamp, stamp))
        picked = rows(conn.execute("SELECT letter FROM ideas WHERE domain = ? AND status = 'picked'", (domain,)))
        conn.execute("DELETE FROM ideas WHERE domain = ? AND status != 'picked'", (domain,))
        free = [l for l in IDEA_LETTERS if l not in {p["letter"] for p in picked}]
        for idea, letter in zip(ideas[: len(free)], free):
            title = str(idea.get("title") or "").strip()
            if not title:
                continue
            conn.execute(
                "INSERT INTO ideas (domain, letter, title, pitch, why, format, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (domain, letter, title[:140], idea.get("pitch"), idea.get("why"), idea.get("format"), stamp, stamp),
            )
        return rows(conn.execute("SELECT id, letter, title, pitch, why, format, status FROM ideas WHERE domain = ? ORDER BY letter", (domain,)))


def pick_idea(idea_id: int, contact_id: int | None = None) -> dict[str, Any] | None:
    stamp = now()
    with connect() as conn:
        idea = one(conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)))
        if not idea:
            return None
        conn.execute("UPDATE ideas SET status = 'offered', picked_by = NULL, picked_at = NULL WHERE domain = ? AND status = 'picked'",
                     (idea["domain"],))
        conn.execute("UPDATE ideas SET status = 'picked', picked_by = ?, picked_at = ?, updated_at = ? WHERE id = ?",
                     (contact_id, stamp, stamp, idea_id))
    log_event("idea_picked", f"{idea['letter']}) {idea['title']}", contact_id, idea["domain"])
    return {**idea, "status": "picked"}


def guess_pick(reply_text: str, ideas: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Which offered idea does a reply choose? Looks for a lone letter ("B", "option b", "b)") or the idea's title."""
    text = (reply_text or "").strip()
    if not text or not ideas:
        return None
    letters = {i["letter"].upper(): i for i in ideas}
    lowered = text.lower()
    for idea in ideas:
        if idea["title"] and idea["title"].lower() in lowered:
            return idea
    head = text[:300]
    hits: set[str] = set()
    # explicit: "option b", "idea C", "go with B", "let's do c"
    hits |= {h.upper() for h in re.findall(r"(?i)\b(?:option|idea|choice|go with|let'?s do|pick|choose)\s+([a-e])\b", head)}
    # marked letter: "B)", "(c)", "B." "B!" "B," or a capital letter alone on a line
    hits |= set(re.findall(r"(?:^|[\s(\[])([A-E])(?:\)|\]|[.!,:](?=\s|$)|\s*$)", head, re.M))
    hits |= {h.upper() for h in re.findall(r"(?i)[(\[]([a-e])[)\]]", head)}
    # a short reply that starts with the letter: "B please", "c sounds great" (but not "A quick question about...")
    first = re.match(r"\s*([A-Ea-e])\b(.*)", head)
    if first and len(head.split()) <= 4 and not re.match(r"\s+[a-z]+\s+[a-z]+\s+[a-z]+", first.group(2)):
        hits.add(first.group(1).upper())
    hits &= set(letters)
    return letters[hits.pop()] if len(hits) == 1 else None


def set_feedback(domain: str, verdict: str | None, reason: str | None = None) -> dict[str, Any]:
    """Your verdict on a lead: 'good', 'bad', or None to clear. 'bad' also takes it out of the active list."""
    domain = normalize_domain(domain)
    if verdict not in ("good", "bad", None):
        raise ValueError("verdict must be good, bad or empty")
    stamp = now()
    with connect() as conn:
        conn.execute("UPDATE accounts SET feedback = ?, feedback_reason = ?, feedback_at = ?, updated_at = ? WHERE domain = ?",
                     (verdict, (reason or "").strip() or None, stamp if verdict else None, stamp, domain))
        if verdict == "bad":
            conn.execute("UPDATE accounts SET status = 'disqualified' WHERE domain = ? AND status IN ('new', 'researched', 'ready')",
                         (domain,))
            conn.execute("UPDATE drafts SET status = 'cancelled' WHERE status IN ('draft', 'approved') AND contact_id IN"
                         " (SELECT id FROM contacts WHERE domain = ?)", (domain,))
    log_event(f"feedback:{verdict or 'cleared'}", reason, domain=domain)
    return {"domain": domain, "feedback": verdict, "reason": reason}


def lead_feedback(limit: int = 40) -> list[dict[str, Any]]:
    """Recent verdicts the agents learn from: which kinds of companies the user wants, and which they don't."""
    with connect() as conn:
        return rows(conn.execute(
            "SELECT domain, name, industry, employees, location, source, why_now, feedback AS verdict, feedback_reason AS reason"
            " FROM accounts WHERE feedback IS NOT NULL ORDER BY feedback_at DESC LIMIT ?", (limit,)))


SOURCE_ALIASES = {"web": "websearch", "web-search": "websearch", "google": "websearch", "hn": "hn-hiring",
                  "ycombinator": "yc", "ph": "producthunt", "product": "producthunt", "funding": "funding-news"}


def source_stats() -> list[dict[str, Any]]:
    """How each discovery source performs: leads found, your verdicts, emails sent, replies, meetings.
    Sources are grouped by their first word ("yc", "hn-hiring", "funding news: fintech" -> "funding news")."""
    with connect() as conn:
        accounts = rows(conn.execute("SELECT domain, source, feedback, status FROM accounts"))
        contacted = {r["domain"] for r in rows(conn.execute(
            "SELECT DISTINCT c.domain FROM drafts d JOIN contacts c ON c.id = d.contact_id WHERE d.status = 'sent'"))}
        replied = {r["domain"] for r in rows(conn.execute(
            "SELECT DISTINCT domain FROM contacts WHERE status IN ('replied', 'meeting') OR reply_kind = 'reply'"))}
    stats: dict[str, dict[str, Any]] = {}
    for a in accounts:
        raw = (a["source"] or "unknown").strip().lower()
        key = re.split(r"[\s:(+]", raw)[0].strip() or "unknown"
        key = SOURCE_ALIASES.get(key, key)
        row = stats.setdefault(key, {"source": key, "leads": 0, "good": 0, "bad": 0, "contacted": 0, "replied": 0, "meetings": 0})
        row["leads"] += 1
        row["good"] += a["feedback"] == "good"
        row["bad"] += a["feedback"] == "bad"
        row["contacted"] += a["domain"] in contacted
        row["replied"] += a["domain"] in replied
        row["meetings"] += a["status"] in ("meeting", "won")
    for row in stats.values():
        row["reply_rate"] = round(row["replied"] / row["contacted"], 2) if row["contacted"] else None
        rated = row["good"] + row["bad"]
        row["good_rate"] = round(row["good"] / rated, 2) if rated else None
    return sorted(stats.values(), key=lambda r: (-(r["replied"]), -(r["good"]), -r["leads"]))


def step_templates() -> list[dict[str, Any]]:
    with connect() as conn:
        return rows(conn.execute("SELECT * FROM step_templates ORDER BY step"))


def save_step_template(step: int, mode: str, subject: str | None = None, body: str | None = None,
                       instruction: str | None = None) -> dict[str, Any]:
    if mode not in ("ai", "fixed"):
        raise ValueError("mode must be ai or fixed")
    if mode == "fixed" and not (body or "").strip():
        raise ValueError("A fixed template needs a body.")
    clean = lambda v: (v or "").strip() or None  # noqa: E731
    with connect() as conn:
        conn.execute(
            "INSERT INTO step_templates (step, mode, subject, body, instruction, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(step) DO UPDATE SET mode = excluded.mode, subject = excluded.subject, body = excluded.body,"
            " instruction = excluded.instruction, updated_at = excluded.updated_at",
            (step, mode, clean(subject), clean(body), clean(instruction), now()))
    log_event("template:saved", f"step {step}: {mode}")
    return {"step": step, "mode": mode}


def counts() -> dict[str, Any]:
    with connect() as conn:
        return {
            "accounts": conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
            "signals": conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0],
            "contacts": conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "drafts_by_status": {
                r["status"]: r["n"] for r in rows(conn.execute("SELECT status, COUNT(*) AS n FROM drafts GROUP BY status"))
            },
            "accounts_by_status": {
                r["status"]: r["n"] for r in rows(conn.execute("SELECT status, COUNT(*) AS n FROM accounts GROUP BY status"))
            },
        }
