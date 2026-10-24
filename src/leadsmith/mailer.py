"""Send approved drafts through your own Gmail, and detect replies.

Safety rails, all on by default:
- only drafts a human approved (status 'approved') are ever sent
- SEND_ENABLED must be true in ~/.leadsmith/.env, else everything is a dry run
- DAILY_SEND_LIMIT caps sends per day (default 25) to protect your domain
- suppressed emails/domains and contacts who replied are skipped
- follow-ups go out only after the previous step was sent, the wait has passed,
  and there's been no reply; they thread under the first email
"""

from __future__ import annotations

import imaplib
import re
import smtplib
import ssl
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any

from . import config, db, decks, schedule

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
IMAP_HOST = "imap.gmail.com"


def mail_status() -> dict[str, Any]:
    limit = daily_limit()
    return {
        "gmail_address": config.get("GMAIL_ADDRESS"),
        "app_password_set": bool(config.get("GMAIL_APP_PASSWORD")),
        "send_enabled": config.get_bool("SEND_ENABLED"),
        "daily_limit": limit["today"],
        "daily_limit_max": limit["max"],
        "warmup": limit["warmup"],
        "sent_today": sent_today(),
        "test_recipient": test_recipient(),
        "gap_seconds": send_gap_seconds(),
    }


def daily_limit() -> dict[str, Any]:
    """Today's cap. With warm-up on, a new sender starts small and grows each day up to the maximum,
    because a fresh Gmail address that suddenly sends 25 cold emails looks like a spammer."""
    maximum = max(0, config.get_int("DAILY_SEND_LIMIT", 25))
    if not config.get_bool("WARMUP", True):
        return {"today": maximum, "max": maximum, "warmup": None}
    start = max(1, config.get_int("WARMUP_START", 5))
    step = max(0, config.get_int("WARMUP_STEP", 3))
    with db.connect() as conn:
        first = conn.execute("SELECT MIN(at) FROM events WHERE type = 'sent'").fetchone()[0]
    local_today = datetime.now().astimezone().date()
    day = (local_today - datetime.fromisoformat(first).astimezone().date()).days if first else 0
    today = warmup_limit(day, start, step, maximum)
    return {"today": today, "max": maximum,
            "warmup": {"day": day + 1, "start": start, "step": step, "done": today >= maximum}}


def warmup_limit(days_since_first_send: int, start: int, step: int, maximum: int) -> int:
    return min(maximum, start + step * max(0, days_since_first_send))


def sent_today() -> int:
    # "Today" is the user's local day (e.g. midnight in India), stored times are UTC.
    local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    start = local_midnight.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM events WHERE type = 'sent' AND at >= ?", (start,)).fetchone()[0]


def _due(draft: dict[str, Any], conn) -> tuple[bool, str, dict[str, Any] | None]:
    """Is this approved draft ready to go? Returns (due, reason, first_step_row)."""
    contact = db.one(conn.execute("SELECT * FROM contacts WHERE id = ?", (draft["contact_id"],)))
    if not contact or not contact.get("email"):
        return False, "contact has no email", None
    if draft.get("kind") == "reply":
        # Answering someone who wrote to you: no sending window, no sequence order. Same thread as email 1.
        if contact["status"] in ("bounced", "unsubscribed") or db.is_suppressed(contact["email"]):
            return False, f"contact status is {contact['status']}", None
        first = db.one(conn.execute(
            "SELECT * FROM drafts WHERE contact_id = ? AND step = 1 AND channel = 'email' AND status = 'sent'", (draft["contact_id"],)))
        return True, "ready", first
    if contact["status"] in ("replied", "meeting", "bounced", "unsubscribed", "disqualified"):
        return False, f"contact status is {contact['status']}", None
    if db.is_suppressed(contact["email"]):
        return False, "email or domain is on the suppression list", None
    if (contact.get("email_status") or "").lower() in ("invalid", "undeliverable"):
        return False, "email failed verification", None
    account = db.one(conn.execute("SELECT location, timezone FROM accounts WHERE domain = ?", (contact["domain"],)))
    zone, _ = schedule.prospect_zone(account)
    now = datetime.now(timezone.utc)
    if draft["step"] == 1:
        if not schedule.in_window(now, zone):
            return False, f"waits for sending hours: {schedule.describe(schedule.next_window_start(now, zone), zone)}", None
        return True, "ready", None
    first = db.one(conn.execute(
        "SELECT * FROM drafts WHERE contact_id = ? AND step = 1 AND channel = 'email'", (draft["contact_id"],)
    ))
    prev = db.one(conn.execute(
        "SELECT * FROM drafts WHERE contact_id = ? AND step = ? AND channel = 'email'",
        (draft["contact_id"], draft["step"] - 1),
    ))
    if not first or first["status"] != "sent" or not prev or prev["status"] != "sent" or not prev.get("sent_at"):
        return False, "previous step not sent yet", None
    due_at = follow_up_due_at(draft, first, prev, zone)
    if now < due_at:
        return False, f"follow-up waits until {schedule.describe(schedule.next_window_start(due_at, zone), zone)}", None
    if not schedule.in_window(now, zone):
        return False, f"waits for sending hours: {schedule.describe(schedule.next_window_start(now, zone), zone)}", None
    return True, "ready", first


MIN_GAP_DAYS = 2  # never two emails to the same person closer together than this


def follow_up_due_at(draft: dict[str, Any], first: dict[str, Any], prev: dict[str, Any], zone=None) -> datetime:
    """When a follow-up may go out: its planned day after email 1, and never sooner than the
    planned gap after the previous email actually went out. After the dashboard was closed for
    days, follow-ups therefore resume one at a time, properly spaced, instead of all at once."""
    offset = draft.get("send_after_days") or (3 if draft["step"] == 2 else 7)
    prev_offset = prev.get("send_after_days") or 0
    gap = max(MIN_GAP_DAYS, offset - prev_offset)
    zone = zone or timezone.utc
    # Business days, in the prospect's time zone: a Friday email's "3 days later" is Wednesday.
    planned = schedule.add_business_days(datetime.fromisoformat(first["sent_at"]), offset, zone)
    after_prev = schedule.add_business_days(datetime.fromisoformat(prev["sent_at"]), gap, zone)
    return max(planned, after_prev)


def _signature() -> str:
    lines = []
    sig = config.get("SENDER_SIGNATURE")
    if sig:
        lines.append(sig.replace("\\n", "\n"))
    footer = config.get("OPT_OUT_LINE", "If this isn't relevant, just reply \"no\" and I won't follow up.")
    if footer:
        lines.append(footer)
    address = config.get("POSTAL_ADDRESS")
    if address:
        lines.append(address)
    return "\n\n".join(lines)


def test_recipient() -> str | None:
    """Test mode: when set, every send goes to this address instead of the prospect."""
    value = (config.get("TEST_RECIPIENT") or "").strip().lower()
    return value if "@" in value else None


def _subject(text: str, test: bool) -> str:
    return f"[TEST] {text}" if test and not text.startswith("[TEST]") else text


def _build(draft: dict[str, Any], contact: dict[str, Any], first: dict[str, Any] | None) -> EmailMessage:
    sender = config.get("GMAIL_ADDRESS") or ""
    test_to = test_recipient()
    name = " ".join(p for p in (contact.get("first_name"), contact.get("last_name")) if p)
    msg = EmailMessage()
    msg["From"] = formataddr((config.get("SENDER_NAME", ""), sender))
    msg["To"] = test_to or formataddr((name, contact["email"]))
    if first:
        subject = _subject(first["subject"] or "", bool(test_to))
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if first.get("message_id"):
            msg["In-Reply-To"] = first["message_id"]
            msg["References"] = first["message_id"]
    else:
        msg["Subject"] = _subject(draft["subject"] or "", bool(test_to))
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or None)
    if sender:
        msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    body = draft["body"].rstrip()
    if test_to:
        body = f"[Test mode: in live mode this goes to {name} <{contact['email']}>]\n\n{body}"
    signature = _signature()
    msg.set_content(f"{body}\n\n{signature}\n" if signature else f"{body}\n")
    if draft.get("attach_deck"):
        deck = decks.get(contact["domain"])
        if deck and deck.get("pdf_path") and Path(deck["pdf_path"]).exists():
            data = Path(deck["pdf_path"]).read_bytes()
            filename = f"{(deck.get('title') or contact['domain'])[:60]}.pdf".replace("/", "-")
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    return msg


_SEND_LOCK = threading.Lock()
PROGRESS: dict[str, Any] = {"running": False}   # what the dashboard shows while a batch goes out
STOP = threading.Event()                        # set by the Stop button


def send_gap_seconds() -> float:
    return float(max(0, min(3600, config.get_int("SEND_GAP_SECONDS", 60))))


def start_background_send(limit: int = 50) -> dict[str, Any]:
    """Send the due queue in a background thread (spaced out), so the page doesn't wait minutes."""
    if PROGRESS.get("running") or _SEND_LOCK.locked():
        return {"started": False, "error": "A send is already running."}
    STOP.clear()
    PROGRESS.update(running=True, total=None, sent=0, failed=0, next_at=None, started_at=db.now(), finished_at=None,
                    last_error=None, stopped=False, gap_seconds=send_gap_seconds())

    def run() -> None:
        try:
            report = send_approved(limit, False)
            if report.get("error"):
                PROGRESS.update(last_error=report["error"])
            PROGRESS["report"] = {"sent": len(report.get("sent", [])), "skipped": len(report.get("skipped", []))}
        except Exception as exc:  # noqa: BLE001 - surface any failure in the dashboard
            PROGRESS.update(last_error=str(exc))
        finally:
            PROGRESS.update(running=False, next_at=None, finished_at=PROGRESS.get("finished_at") or db.now())

    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


def stop_send() -> bool:
    if not PROGRESS.get("running"):
        return False
    STOP.set()
    return True  # one sender at a time in this process; the DB claim covers other processes


def _claim(draft_id: int) -> bool:
    """Atomically take a draft for sending. Only one sender anywhere can win."""
    with db.connect() as conn:
        cur = conn.execute("UPDATE drafts SET status = 'sending', updated_at = ? WHERE id = ? AND status = 'approved'",
                           (db.now(), draft_id))
        return cur.rowcount == 1


def _release(draft_id: int) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE drafts SET status = 'approved' WHERE id = ? AND status = 'sending'", (draft_id,))


def send_approved(limit: int = 10, dry_run: bool = True, followups_only: bool = False,
                  max_overdue_days: float | None = None, gap_seconds: float | None = None) -> dict[str, Any]:
    """Send approved drafts that are due.

    max_overdue_days: automatic runs pass a small number so a follow-up that became due while the
    dashboard was closed waits for you on the Outreach page instead of going out unseen.
    """
    status = mail_status()
    live = not dry_run and status["send_enabled"]
    if not dry_run and not status["send_enabled"]:
        return {"sent": [], "error": "Sending is off. Turn it on in Settings. Nothing was sent."}
    if live and not (status["gmail_address"] and status["app_password_set"]):
        return {"sent": [], "error": "Connect Gmail in Settings first. Nothing was sent."}
    if not _SEND_LOCK.acquire(timeout=1):
        return {"sent": [], "skipped": [], "error": "Another send is already running. Try again in a minute."}
    try:
        report: dict[str, Any] = {"dry_run": not live, "sent": [], "skipped": [], "overdue": []}
        # Never follow up someone who answered while the dashboard was closed.
        if status["app_password_set"] and (live or dry_run):
            try:
                found = check_replies(30)
                report["replies_found"] = len(found.get("replies", []))
            except Exception as exc:  # noqa: BLE001 - a failed check must block live sends, not crash
                if live:
                    return {**report, "error": f"Couldn't check Gmail for replies first, so nothing was sent: {exc}"}
        budget = max(0, status["daily_limit"] - sent_today())
        report["daily_budget_left"] = budget
        limit = min(limit, budget)
        if limit <= 0:
            report["note"] = "Daily send limit reached."
            return report

        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            approved = db.rows(conn.execute(
                "SELECT * FROM drafts WHERE status = 'approved' AND channel = 'email'"
                + (" AND step > 1 AND kind = 'sequence'" if followups_only else "") + " ORDER BY step, id"
            ))
            queue, seen_contacts = [], set()
            for draft in approved:
                contact = db.one(conn.execute("SELECT * FROM contacts WHERE id = ?", (draft["contact_id"],)))
                if draft["contact_id"] in seen_contacts:
                    report["skipped"].append({"draft_id": draft["id"], "to": (contact or {}).get("email"),
                                              "reason": "one email per person per send"})
                    continue
                due, reason, first = _due(draft, conn)
                if not due:
                    report["skipped"].append({"draft_id": draft["id"], "to": (contact or {}).get("email"), "reason": reason})
                    continue
                overdue_days = 0.0
                if draft["step"] > 1 and draft.get("kind", "sequence") == "sequence":
                    prev = db.one(conn.execute("SELECT * FROM drafts WHERE contact_id = ? AND step = ? AND channel = 'email'",
                                               (draft["contact_id"], draft["step"] - 1)))
                    acct = db.one(conn.execute("SELECT location, timezone FROM accounts WHERE domain = ?", (contact["domain"],)))
                    overdue_days = (now - follow_up_due_at(draft, first, prev, schedule.prospect_zone(acct)[0])).total_seconds() / 86400
                if max_overdue_days is not None and overdue_days > max_overdue_days:
                    report["overdue"].append({"draft_id": draft["id"], "to": contact["email"], "step": draft["step"],
                                              "days_late": round(overdue_days, 1)})
                    report["skipped"].append({"draft_id": draft["id"], "to": contact["email"],
                                              "reason": f"{round(overdue_days)} days late: waiting for you on Outreach"})
                    continue
                seen_contacts.add(draft["contact_id"])
                queue.append((draft, contact, first, overdue_days))
                if len(queue) >= limit:
                    break

        if not queue:
            return report

        gap = send_gap_seconds() if gap_seconds is None else max(0.0, gap_seconds)
        if live:
            if not PROGRESS.get("running"):
                STOP.clear()  # a Stop from an earlier batch must not cut this one short
            PROGRESS.update(running=True, total=len(queue), sent=0, failed=0, next_at=None, started_at=db.now(),
                            finished_at=None, last_error=None, gap_seconds=gap)
        try:
            for index, (draft, contact, first, overdue_days) in enumerate(queue):
                msg = _build(draft, contact, first)
                entry = {"draft_id": draft["id"], "step": draft["step"], "to": msg["To"], "prospect": contact["email"],
                         "test_mode": bool(test_recipient()), "subject": msg["Subject"],
                         "days_late": round(overdue_days, 1) if overdue_days >= 1 else 0}
                if not live:
                    entry["preview"] = msg.get_content()[:600]
                    report["sent"].append(entry)
                    continue
                if index > 0 and gap > 0:
                    # Space emails out. Stop() wakes this immediately; the next email then isn't sent.
                    PROGRESS["next_at"] = (datetime.now(timezone.utc) + timedelta(seconds=gap)).replace(microsecond=0).isoformat()
                    if STOP.wait(gap):
                        report["stopped"] = True
                        break
                    PROGRESS["next_at"] = None
                with db.connect() as conn:
                    acct = db.one(conn.execute("SELECT location, timezone FROM accounts WHERE domain = ?", (contact["domain"],)))
                zone, _ = schedule.prospect_zone(acct)
                if draft.get("kind") != "reply" and not schedule.in_window(datetime.now(timezone.utc), zone):
                    report["skipped"].append({**entry, "reason": "sending hours closed during the batch; it waits for the next window"})
                    continue
                if not _claim(draft["id"]):
                    report["skipped"].append({**entry, "reason": "already being sent by another run"})
                    continue
                try:
                    # A fresh connection per email: a long, spaced-out batch never hits an idle timeout.
                    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30) as server:
                        server.login(config.get("GMAIL_ADDRESS") or "", (config.get("GMAIL_APP_PASSWORD") or "").replace(" ", ""))
                        server.send_message(msg)
                except (smtplib.SMTPException, OSError) as exc:
                    _release(draft["id"])
                    entry["error"] = str(exc)
                    report["skipped"].append(entry)
                    PROGRESS["failed"] = PROGRESS.get("failed", 0) + 1
                    PROGRESS["last_error"] = str(exc)
                    continue
                stamp = db.now()
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE drafts SET status = 'sent', sent_at = ?, message_id = ?, updated_at = ? WHERE id = ?",
                        (stamp, msg["Message-ID"], stamp, draft["id"]),
                    )
                    conn.execute("UPDATE contacts SET status = 'contacted', updated_at = ? WHERE id = ? AND status IN ('new', 'drafted')",
                                 (stamp, contact["id"]))
                    conn.execute("UPDATE accounts SET status = 'contacted', updated_at = ? WHERE domain = ?"
                                 " AND status IN ('new', 'researched', 'ready')", (stamp, contact["domain"]))
                db.log_event("sent", ("reply: " if draft.get("kind") == "reply" else f"step {draft['step']}: ") + f"{msg['Subject']}" + (f" (test, to {msg['To']})" if entry["test_mode"] else ""),
                             contact["id"], contact["domain"])
                report["sent"].append(entry)
                PROGRESS["sent"] = PROGRESS.get("sent", 0) + 1
        finally:
            if live:
                PROGRESS.update(running=False, next_at=None, finished_at=db.now(), stopped=bool(report.get("stopped")))
        return report
    finally:
        _SEND_LOCK.release()


def send_test(draft_id: int) -> dict[str, Any]:
    """Send one draft to your own inbox, exactly as the prospect would see it."""
    address, password = config.get("GMAIL_ADDRESS"), config.get("GMAIL_APP_PASSWORD")
    if not (address and password):
        return {"error": "Add your Gmail address and app password in Settings first."}
    with db.connect() as conn:
        draft = db.one(conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)))
        if not draft:
            return {"error": f"No draft {draft_id}"}
        contact = db.one(conn.execute("SELECT * FROM contacts WHERE id = ?", (draft["contact_id"],)))
        first = None
        if draft["step"] > 1:
            first = db.one(conn.execute(
                "SELECT * FROM drafts WHERE contact_id = ? AND step = 1 AND channel = 'email'", (draft["contact_id"],)
            ))
    msg = _build(draft, {**contact, "email": address}, first)
    msg.replace_header("Subject", f"[TEST to {contact.get('email') or 'prospect'}] {msg['Subject']}")
    for header in ("In-Reply-To", "References"):
        del msg[header]
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30) as server:
            server.login(address, password.replace(" ", ""))
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        return {"error": f"Gmail refused the login or message: {exc}"}
    return {"sent_to": address, "subject": msg["Subject"]}


OPT_OUT_RE = re.compile(
    r"\b(unsubscribe|opt[- ]?out|remove me|take me off|stop (?:emailing|contacting|sending|messaging)|"
    r"(?:don'?t|do not|please don'?t) (?:email|contact|message) (?:me|us)|no more emails|leave me alone)\b", re.I)
NOT_INTERESTED_RE = re.compile(
    r"\b(not interested|no thanks|no,? thank you|not a (?:good )?fit|not for us|we(?:'re| are) (?:all )?(?:set|good)|"
    r"not at this time|not right now|we(?:'ll| will) pass|pass on this|already have (?:a|an|someone)|no need)\b", re.I)
BARE_NO = {"no", "nope", "stop", "unsubscribe", "remove", "no thanks", "no thank you"}


def classify_reply(text: str, subject: str = "") -> str:
    """'optout' (asked to stop: never email again), 'not_interested', or 'reply'."""
    body = (text or "").strip()
    bare = re.sub(r"[^a-z ]", "", body.lower()).strip()
    if bare in BARE_NO or OPT_OUT_RE.search(body) or OPT_OUT_RE.search(subject or ""):
        return "optout" if bare not in ("no thanks", "no thank you") else "not_interested"
    if NOT_INTERESTED_RE.search(body):
        return "not_interested"
    return "reply"


def _reply_headers(imap: imaplib.IMAP4_SSL, msg_id: bytes) -> tuple[str, str]:
    """(subject, Message-ID) of a message, read without marking it as read."""
    import email
    import email.header

    try:
        status, parts = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID)])")
        raw = next((p[1] for p in parts if isinstance(p, tuple)), b"") if status == "OK" else b""
    except (imaplib.IMAP4.error, OSError):
        return "", ""
    headers = email.message_from_bytes(raw)
    subject = str(email.header.make_header(email.header.decode_header(headers.get("Subject", "")))) if headers.get("Subject") else ""
    return subject, (headers.get("Message-ID") or "").strip()


def _reply_text(imap: imaplib.IMAP4_SSL, msg_id: bytes) -> str:
    """The new text of a reply: plain-text body without the quoted original or signature-like tail."""
    import email
    import html as htmllib
    import re

    try:
        status, parts = imap.fetch(msg_id, "(BODY.PEEK[])")
        raw = next((p[1] for p in parts if isinstance(p, tuple)), None) if status == "OK" else None
    except (imaplib.IMAP4.error, OSError):
        return ""
    if not raw:
        return ""
    msg = email.message_from_bytes(raw)
    text = ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype in ("text/plain", "text/html") and not part.get_filename():
            payload = part.get_payload(decode=True) or b""
            body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if ctype == "text/html":
                body = htmllib.unescape(re.sub(r"(?s)<[^>]+>", " ", re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", "", body)))
            text = body
            if ctype == "text/plain":
                break
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(">") or re.match(r"\s*On .{5,200} wrote:\s*$", line) or line.strip() == "--":
            break
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def check_replies(days: int = 14) -> dict[str, Any]:
    address, password = config.get("GMAIL_ADDRESS"), config.get("GMAIL_APP_PASSWORD")
    if not (address and password):
        return {"error": f"Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in {config.ENV_FILE} first."}
    with db.connect() as conn:
        contacted = db.rows(conn.execute(
            "SELECT id, email, domain, first_name, last_name, last_reply_id FROM contacts"
            " WHERE status IN ('contacted', 'replied') AND email IS NOT NULL"
        ))
    if not contacted:
        return {"replies": [], "bounces": [], "checked": 0}
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    replies, bounces = [], []
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(address, password.replace(" ", ""))
        imap.select("INBOX", readonly=True)
        for contact in contacted:
            status, data = imap.search(None, "FROM", '"mailer-daemon"', "BODY", f'"{contact["email"]}"', "SINCE", since)
            if status == "OK" and data and data[0].split():
                stamp = db.now()
                with db.connect() as conn:
                    conn.execute("UPDATE contacts SET status = 'bounced', email_status = 'invalid', updated_at = ?"
                                 " WHERE id = ?", (stamp, contact["id"]))
                    conn.execute("UPDATE drafts SET status = 'cancelled', updated_at = ? WHERE contact_id = ?"
                                 " AND status IN ('draft', 'approved')", (stamp, contact["id"]))
                db.log_event("bounce", contact["email"], contact["id"], contact["domain"])
                bounces.append({"contact_id": contact["id"], "email": contact["email"]})
                continue
            test_to = test_recipient()
            if test_to:
                # Test mode: the "prospect" is your test inbox, so match its reply by the thread's subject.
                with db.connect() as conn:
                    first = db.one(conn.execute(
                        "SELECT subject FROM drafts WHERE contact_id = ? AND step = 1 AND status = 'sent'", (contact["id"],)
                    ))
                subject = ((first or {}).get("subject") or "").replace('"', "")
                if not subject:
                    continue
                try:
                    status, data = imap.search(None, "FROM", f'"{test_to}"', "SUBJECT", f'"{subject}"', "SINCE", since)
                except imaplib.IMAP4.error:
                    continue
            else:
                status, data = imap.search(None, "FROM", f'"{contact["email"]}"', "SINCE", since)
            if status != "OK" or not data or not data[0].split():
                continue
            latest = data[0].split()[-1]
            reply_subject, reply_msgid = _reply_headers(imap, latest)
            if reply_msgid and reply_msgid == contact.get("last_reply_id"):
                continue  # nothing new since we last looked
            reply_text = _reply_text(imap, latest)
            reply_kind = classify_reply(reply_text, reply_subject)
            with db.connect() as conn:
                ideas = db.rows(conn.execute(
                    "SELECT id, letter, title FROM ideas WHERE domain = ? ORDER BY letter", (contact["domain"],)
                ))
            guess = db.guess_pick(reply_text, ideas)
            stamp = db.now()
            with db.connect() as conn:
                new_status = {"optout": "unsubscribed", "not_interested": "disqualified"}.get(reply_kind, "replied")
                conn.execute("UPDATE contacts SET status = ?, last_reply = ?, reply_pick = ?, reply_kind = ?, last_reply_id = ?,"
                             " updated_at = ? WHERE id = ?",
                             (new_status, reply_text[:2000] or None, guess["letter"] if guess and reply_kind == "reply" else None,
                              reply_kind, reply_msgid or None, stamp, contact["id"]))
                conn.execute("UPDATE accounts SET status = 'replied', updated_at = ? WHERE domain = ?"
                             " AND status = 'contacted'", (stamp, contact["domain"]))
                conn.execute("UPDATE drafts SET status = 'cancelled', updated_at = ? WHERE contact_id = ?"
                             " AND status IN ('draft', 'approved')", (stamp, contact["id"]))
            db.log_event("reply", f"{len(data[0].split())} message(s) in inbox", contact["id"], contact["domain"])
            if reply_kind == "optout":
                db.suppress(contact["email"], "asked to stop (reply)")
                db.log_event("opt_out", "asked to stop in a reply; added to do-not-contact", contact["id"], contact["domain"])
            elif reply_kind == "not_interested":
                db.log_event("not_interested", "said not interested", contact["id"], contact["domain"])
            replies.append({"contact_id": contact["id"], "email": contact["email"], "kind": reply_kind,
                            "name": f"{contact['first_name'] or ''} {contact['last_name'] or ''}".strip(),
                            "picked": guess["letter"] if guess else None})
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return {"replies": replies, "bounces": bounces, "checked": len(contacted),
            "note": "Pending follow-ups for these contacts were cancelled. Read and answer the replies in Gmail."}


def test_login() -> str:
    address, password = config.get("GMAIL_ADDRESS"), config.get("GMAIL_APP_PASSWORD")
    if not (address and password):
        return "not configured"
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=20) as server:
            server.login(address, password.replace(" ", ""))
        return "ok"
    except (smtplib.SMTPException, OSError) as exc:
        return f"failed: {exc}"
