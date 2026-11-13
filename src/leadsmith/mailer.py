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

from . import config, db, decks, lint, schedule

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
IMAP_HOST = "imap.gmail.com"


BOUNCE_LIMIT = 0.02       # above 2% of recent emails bouncing, inbox providers start distrusting the sender
BOUNCE_MIN = 3            # ...and at least this many bounces, so one bad address in a small batch doesn't pause
BOUNCE_WINDOW_DAYS = 14


def bounce_health() -> dict[str, Any]:
    """Bounces against emails sent in the last two weeks (or since sending was last resumed). Test-mode sends to
    yourself don't count."""
    since = (datetime.now(timezone.utc) - timedelta(days=BOUNCE_WINDOW_DAYS)).replace(microsecond=0).isoformat()
    resumed = config.get("SEND_RESUMED_AT") or ""  # only what happened after the resume click counts
    with db.connect() as conn:
        sent = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'sent' AND at >= ? AND at > ?"
                            " AND detail NOT LIKE '%(test, to %'", (since, resumed)).fetchone()[0]
        bounced = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'bounce' AND at >= ? AND at > ?",
                               (since, resumed)).fetchone()[0]
    since = max(since, resumed)
    rate = bounced / sent if sent else 0.0
    return {"since": since, "sent": sent, "bounced": bounced, "rate": round(rate, 4),
            "over": bounced >= BOUNCE_MIN and rate > BOUNCE_LIMIT}


def paused_reason() -> str | None:
    return config.get("SEND_PAUSED") or None


def check_bounce_guard() -> str | None:
    """Pause all sending when too many recent emails bounced. Gmail can suspend a sender with a high bounce rate,
    and it doesn't tell @gmail.com senders about spam reports, so bounces are the warning sign Leadsmith can see.
    Returns the pause reason, if sending is paused."""
    if paused_reason():
        return paused_reason()
    health = bounce_health()
    if not health["over"]:
        return None
    reason = (f"{health['bounced']} of the last {health['sent']} emails bounced ({health['rate']:.1%}, above "
              f"{BOUNCE_LIMIT:.0%}). Sending is paused so your Gmail keeps its reputation. Check the bounced addresses, "
              "then resume.")
    config.save_env({"SEND_PAUSED": reason})
    db.log_event("send_paused", reason)
    return reason


def resume_sending() -> dict[str, Any]:
    """Your click after checking the bounces. The bounce count starts again from now."""
    config.save_env({"SEND_PAUSED": "", "SEND_RESUMED_AT": db.now()})
    db.log_event("send_resumed", "sending resumed by you")
    return {"paused": None}


def mail_status() -> dict[str, Any]:
    limit = daily_limit()
    return {
        "paused": paused_reason(),
        "bounces": bounce_health(),
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
    if db.is_recruiting_address(contact.get("email")):
        return False, "recruiting inbox (hiring@, careers@): find a buyer's own address", None
    if db.is_guessed(contact.get("email_status")) and not config.get_bool("ALLOW_GUESSED_EMAILS", False):
        return False, "guessed address: find a published one (or allow guessed addresses in Settings)", None
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
    if contact.get("resume_after") and now.date().isoformat() < contact["resume_after"]:
        return False, f"out of office: follow-up waits until {contact['resume_after']}", None
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
    # The person's main address plus any extra addresses (same email, same thread), minus do-not-contact ones.
    extras = [e for e in db.extra_emails(contact) if not db.is_suppressed(e)]
    msg["To"] = test_to or ", ".join([formataddr((name, contact["email"])), *extras])
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
        body = f"[Test mode: in live mode this goes to {name} <{contact['email']}>{''.join(', ' + e for e in extras)}]\n\n{body}"
    signature = _signature()
    msg.set_content(f"{body}\n\n{signature}\n" if signature else f"{body}\n")
    if lint.claims_attachment(draft["body"]) and not (draft.get("attach_deck") and decks.pdf_ready(contact["domain"])):
        raise ValueError("This email says a deck is attached, but the deck PDF is missing. Rebuild the deck or edit the email.")
    if draft.get("attach_deck"):
        deck = decks.get(contact["domain"])
        if deck and deck.get("pdf_path") and Path(deck["pdf_path"]).exists():
            data = Path(deck["pdf_path"]).read_bytes()
            filename = f"{(deck.get('title') or contact['domain'])[:60]}.pdf".replace("/", "-")
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    return msg


def message_text(msg: EmailMessage) -> str:
    """The plain-text body, whether or not a deck is attached (an attached email is multipart)."""
    body = msg.get_body(preferencelist=("plain",))
    return body.get_content() if body is not None else ""


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
        return {"sent": [], "error": f"Sending is turned off (SEND_ENABLED=false in {config.ENV_FILE}). Nothing was sent."}
    if live and not (status["gmail_address"] and status["app_password_set"]):
        return {"sent": [], "error": "Connect Gmail in Settings first. Nothing was sent."}
    # Only real sends need the lock. A preview never sends or claims anything, so it must work even while a
    # batch is going out (one email a minute) or automatic follow-ups are sending.
    held = False
    if live:
        if not _SEND_LOCK.acquire(timeout=1):
            return {"sent": [], "skipped": [], "error": "A batch is already sending (one email a minute). "
                                                        "Watch it on the Outreach page, or try again when it finishes."}
        held = True
    try:
        report: dict[str, Any] = {"dry_run": not live, "sent": [], "skipped": [], "overdue": []}
        if not live and (PROGRESS.get("running") or _SEND_LOCK.locked()):
            report["note"] = "A batch is sending right now. Anything it hasn't sent yet is shown here too."
        # Never follow up someone who answered while the dashboard was closed.
        if status["app_password_set"] and (live or dry_run):
            try:
                found = check_replies(30)
                report["replies_found"] = len(found.get("replies", []))
            except Exception as exc:  # noqa: BLE001 - a failed check must block live sends, not crash
                if live:
                    return {**report, "error": f"Couldn't check Gmail for replies first, so nothing was sent: {exc}"}
        if live and check_bounce_guard():
            return {**report, "error": f"Sending is paused: {paused_reason()} Nothing was sent.", "paused": paused_reason()}
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
                try:
                    msg = _build(draft, contact, first)
                except ValueError as exc:  # e.g. it says a deck is attached but the PDF is gone: skip, never send
                    report["skipped"].append({"draft_id": draft["id"], "step": draft["step"], "to": contact["email"],
                                              "reason": str(exc)})
                    PROGRESS["failed"] = PROGRESS.get("failed", 0) + 1
                    PROGRESS["last_error"] = str(exc)
                    continue
                entry = {"draft_id": draft["id"], "step": draft["step"], "to": msg["To"], "prospect": contact["email"],
                         "test_mode": bool(test_recipient()), "subject": msg["Subject"],
                         "days_late": round(overdue_days, 1) if overdue_days >= 1 else 0}
                if not live:
                    entry["preview"] = message_text(msg)[:600]
                    entry["attachments"] = [part.get_filename() for part in msg.iter_attachments()]
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
        if held:
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
    try:
        msg = _build(draft, {**contact, "email": address}, first)
    except ValueError as exc:
        return {"error": str(exc)}
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


# Automatic answers: out-of-office notes, support-desk tickets, "I get too much email" auto-replies. They aren't a
# person answering, so they never count as a reply and never stop the follow-ups.
AUTO_SUBJECT_RE = re.compile(
    r"^\s*(?:\[?auto(?:matic)?[- ]?(?:reply|response|antwort)|autoreply|automatische antwort|out of (?:the )?office|"
    r"ooo\b|away from|absence|abwesen|r[ée]ponse automatique|respuesta autom[aá]tica|risposta automatica|on leave|"
    r"(?:on )?(?:vacation|holiday)\b)|\b(?:ticket|case|request)\s*(?:#|no\.?\s*|number\s*|id\s*)\d{3,}|"
    r"\[#\d{3,}\]|\bticket\s+\d{3,}|we(?:'ve| have) received your", re.I)
AUTO_BODY_RE = re.compile(
    r"\b(?:this is an? automat(?:ic|ed)(?:ally generated)? (?:reply|response|message|e-?mail)|automatic reply|"
    r"auto-?reply|out of (?:the )?office|"
    r"i(?:'m| am) (?:currently )?(?:out of (?:the )?office|away from (?:my )?(?:desk|e-?mail|the office)|offline|"
    r"on (?:annual |parental |maternity |paternity |sick )?leave|on (?:vacation|holiday)|travell?ing until)|"
    r"(?:away|offline|out) (?:until|between)\b|limited access to (?:my )?e-?mail|"
    r"(?:will|'ll) be back (?:at my desk|in the office|on)|(?:back|return) to (?:my desk|the office) on|"
    r"(?:may|might) be (?:slow|delayed) (?:in )?respond|"
    r"(?:a )?(?:support )?ticket (?:has been|was) (?:created|opened|raised|logged)|"
    r"we(?:'ve| have) received your (?:request|message|e-?mail|enquiry|inquiry) and|"
    r"(?:support|help ?desk) ticket|ticket (?:number|id|#)|do not reply to this (?:e-?mail|message)|"
    r"this mailbox is (?:not|un)monitored)", re.I)

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"))}
_MONTHS.update({m[:3]: n for m, n in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DATE_RES = (
    re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*[-–]\s*\d{{1,2}}(?:st|nd|rd|th)?)?\s+(?:of\s+)?{_MONTH}(?:,?\s+(\d{{4}}))?\b", re.I),
    re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", re.I),
)


def return_date(text: str, today: Any = None) -> Any:
    """The day someone who sent an out-of-office note is back: the latest date in the next 90 days that the note
    mentions ("back on 16th November", "offline between 9 - 15 November", "until Nov 20"). None if it names none."""
    from datetime import date

    today = today or datetime.now().date()
    found = []
    for pattern in _DATE_RES:
        for m in pattern.finditer(text or ""):
            groups = m.groups()
            day_s, month_s, year_s = (groups[0], groups[1], groups[2]) if pattern is _DATE_RES[0] else (groups[1], groups[0], groups[2])
            month = _MONTHS.get(month_s.lower().rstrip("."))
            if not month:
                continue
            year = int(year_s) if year_s else today.year
            try:
                when = date(year, month, int(day_s))
            except ValueError:
                continue
            if not year_s and when < today - timedelta(days=7):
                try:
                    when = date(year + 1, month, int(day_s))  # "back on 3 January", written in December
                except ValueError:
                    continue
            if today <= when <= today + timedelta(days=90):
                found.append(when)
    return max(found) if found else None


def classify_reply(text: str, subject: str = "", auto_header: bool = False) -> str:
    """'auto' (an out-of-office note, ticket or other automatic answer: the follow-ups carry on), 'optout' (asked to
    stop: never email again), 'not_interested', or 'reply'."""
    body = (text or "").strip()
    bare = re.sub(r"[^a-z ]", "", body.lower()).strip()
    if auto_header or AUTO_SUBJECT_RE.search(subject or ""):
        return "auto"
    if bare in BARE_NO:
        return "optout" if bare not in ("no thanks", "no thank you") else "not_interested"
    if AUTO_BODY_RE.search(body[:1500]):
        return "auto"
    if OPT_OUT_RE.search(body) or OPT_OUT_RE.search(subject or ""):
        return "optout"
    if NOT_INTERESTED_RE.search(body):
        return "not_interested"
    return "reply"


def resume_date_for(text: str) -> str | None:
    """When follow-ups to someone who sent an automatic answer may continue: the day after the return date their
    note gives. None when it gives none (the follow-ups then keep their normal schedule)."""
    back = return_date(text)
    return (back + timedelta(days=1)).isoformat() if back else None


def _reply_headers(imap: imaplib.IMAP4_SSL, msg_id: bytes) -> tuple[str, str, bool]:
    """(subject, Message-ID, sent automatically?) of a message, read without marking it as read. Mail servers mark
    out-of-office notes and auto-responders with Auto-Submitted, X-Autoreply or Precedence: auto_reply."""
    import email
    import email.header

    try:
        status, parts = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID AUTO-SUBMITTED X-AUTOREPLY "
                                           "X-AUTORESPOND PRECEDENCE)])")
        raw = next((p[1] for p in parts if isinstance(p, tuple)), b"") if status == "OK" else b""
    except (imaplib.IMAP4.error, OSError):
        return "", "", False
    headers = email.message_from_bytes(raw)
    subject = str(email.header.make_header(email.header.decode_header(headers.get("Subject", "")))) if headers.get("Subject") else ""
    auto = ((headers.get("Auto-Submitted") or "no").strip().lower() != "no" or bool(headers.get("X-Autoreply"))
            or bool(headers.get("X-Autorespond")) or (headers.get("Precedence") or "").strip().lower() == "auto_reply")
    return subject, (headers.get("Message-ID") or "").strip(), auto


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


FETCH_CHUNK = 500  # message numbers per FETCH command, so a busy inbox never makes one huge command


def _fetch_parts(imap: imaplib.IMAP4_SSL, ids: list[bytes], what: str) -> list[tuple[bytes, bytes]]:
    """(message number, data) for each message, fetched in chunks. Read-only: PEEK never marks mail as read."""
    out: list[tuple[bytes, bytes]] = []
    for start in range(0, len(ids), FETCH_CHUNK):
        status, parts = imap.fetch(b",".join(ids[start:start + FETCH_CHUNK]), what)
        if status != "OK":
            continue
        for part in parts or []:
            if isinstance(part, tuple) and len(part) >= 2:
                number = re.match(rb"\s*(\d+)", part[0])
                if number:
                    out.append((number.group(1), part[1] or b""))
    return out


def _bounce_text(imap: imaplib.IMAP4_SSL, since: str) -> str:
    """All bounce notices since the date, as one lower-case text to look addresses up in. Only the first 30 KB of
    each: the failed address is in the notice at the top, and the rest is usually the original email with its deck."""
    status, data = imap.search(None, "FROM", '"mailer-daemon"', "SINCE", since)
    ids = data[0].split() if status == "OK" and data and data[0] else []
    parts = _fetch_parts(imap, ids, "(BODY.PEEK[]<0.30000>)")
    return "\n".join(raw.decode("utf-8", "replace") for _, raw in parts).lower()


def _messages_by_sender(imap: imaplib.IMAP4_SSL, since: str) -> dict[str, list[bytes]]:
    """Every inbox message since the date, grouped by sender address (oldest first), from one search plus the From
    headers only."""
    from email.utils import getaddresses

    status, data = imap.search(None, "SINCE", since)
    ids = data[0].split() if status == "OK" and data and data[0] else []
    senders: dict[str, list[bytes]] = {}
    for number, raw in _fetch_parts(imap, ids, "(BODY.PEEK[HEADER.FIELDS (FROM)])"):
        header = raw.decode("utf-8", "replace").partition(":")[2]
        for _, addr in getaddresses([header]):
            if "@" in addr:
                senders.setdefault(addr.strip().lower(), []).append(number)
    for numbers in senders.values():
        numbers.sort(key=int)
    return senders


def check_replies(days: int = 14) -> dict[str, Any]:
    address, password = config.get("GMAIL_ADDRESS"), config.get("GMAIL_APP_PASSWORD")
    if not (address and password):
        return {"error": f"Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in {config.ENV_FILE} first."}
    with db.connect() as conn:
        contacted = db.rows(conn.execute(
            "SELECT id, email, domain, first_name, last_name, last_reply_id, extra_emails FROM contacts"
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
        # Two questions to Gmail for everyone, instead of two per contact: with 100+ contacts that was 200+ searches
        # and over a minute before every preview. Matching to contacts then happens here.
        bounced_text = _bounce_text(imap, since)
        by_sender = _messages_by_sender(imap, since)
        test_to = test_recipient()
        def bounced(address: str) -> bool:
            # Whole address only: a bounce for "ka@site.com" is not a bounce for "a@site.com".
            return address in bounced_text and bool(re.search(rf"(?<![\w.+-]){re.escape(address)}(?![\w-])", bounced_text))

        for contact in contacted:
            email_lower = contact["email"].lower()
            extras = db.extra_emails(contact)
            dead = [e for e in extras if bounced(e)]
            if dead:  # an extra address bounced: stop sending to it, keep the person's sequence going
                db.set_extra_emails(contact["id"], [e for e in extras if e not in dead])
                for address in dead:
                    db.log_event("bounce", f"{address} (extra address, removed)", contact["id"], contact["domain"])
                    bounces.append({"contact_id": contact["id"], "email": address})
                extras = [e for e in extras if e not in dead]
            if bounced(email_lower):
                stamp = db.now()
                with db.connect() as conn:
                    conn.execute("UPDATE contacts SET status = 'bounced', email_status = 'invalid', updated_at = ?"
                                 " WHERE id = ?", (stamp, contact["id"]))
                    conn.execute("UPDATE drafts SET status = 'cancelled', updated_at = ? WHERE contact_id = ?"
                                 " AND status IN ('draft', 'approved')", (stamp, contact["id"]))
                db.log_event("bounce", contact["email"], contact["id"], contact["domain"])
                bounces.append({"contact_id": contact["id"], "email": contact["email"]})
                continue
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
                found = data[0].split() if status == "OK" and data and data[0] else []
            else:
                # A reply from any of the person's addresses counts (the main one or an extra one).
                found = sorted({n for a in (email_lower, *extras) for n in by_sender.get(a, [])}, key=int)
            if not found:
                continue
            latest = found[-1]
            replier = next((a for a in (email_lower, *extras) if latest in by_sender.get(a, [])), email_lower)
            reply_subject, reply_msgid, auto_header = _reply_headers(imap, latest)
            if reply_msgid and reply_msgid == contact.get("last_reply_id"):
                continue  # nothing new since we last looked
            reply_text = _reply_text(imap, latest)
            reply_kind = classify_reply(reply_text, reply_subject, auto_header)
            if reply_kind == "auto":
                # Out of office, a ticket, an auto-responder: no person answered. The sequence carries on, after
                # their return date when the note gives one.
                resume = resume_date_for(reply_text)
                with db.connect() as conn:
                    conn.execute("UPDATE contacts SET last_reply = ?, reply_kind = 'auto', last_reply_id = ?, resume_after = ?,"
                                 " updated_at = ? WHERE id = ?",
                                 (reply_text[:2000] or reply_subject or None, reply_msgid or None, resume, db.now(), contact["id"]))
                db.log_event("auto_reply", (f"automatic answer; follow-ups wait until {resume}" if resume
                                            else "automatic answer; follow-ups continue"), contact["id"], contact["domain"])
                replies.append({"contact_id": contact["id"], "email": contact["email"], "kind": "auto", "resume_after": resume,
                                "name": f"{contact['first_name'] or ''} {contact['last_name'] or ''}".strip(), "picked": None})
                continue
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
            db.log_event("reply", f"{len(found)} message(s) in inbox", contact["id"], contact["domain"])
            if reply_kind == "optout":
                db.suppress(replier, "asked to stop (reply)")
                if replier != email_lower:  # the person's emails stop too: they all went in one thread
                    db.suppress(contact["email"], f"asked to stop (reply from {replier})")
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
    paused = check_bounce_guard() if bounces else paused_reason()
    return {"replies": replies, "bounces": bounces, "checked": len(contacted), "paused": paused,
            "note": "Pending follow-ups for people who answered were cancelled. Automatic answers (out of office, "
                    "tickets) don't count: those follow-ups carry on. Read and answer the replies in Gmail."}


def mark_reply(contact_id: int, kind: str) -> dict[str, Any]:
    """Correct how a reply was read. 'auto': it was an automatic answer, so the sequence resumes: emails that were
    cancelled when it arrived go back to Review (nothing is re-approved by itself). 'reply': it was a real person,
    so the follow-ups stop."""
    contact = db.get_contact(contact_id)
    if not contact:
        raise ValueError("Unknown person")
    stamp = db.now()
    with db.connect() as conn:
        if kind == "auto":
            if contact["status"] not in ("replied", "contacted"):
                raise ValueError(f"This person is {contact['status']}, so their follow-ups can't resume.")
            resume = resume_date_for(contact.get("last_reply") or "")
            conn.execute("UPDATE contacts SET status = 'contacted', reply_kind = 'auto', reply_pick = NULL, resume_after = ?,"
                         " updated_at = ? WHERE id = ?", (resume, stamp, contact_id))
            back = conn.execute("UPDATE drafts SET status = 'draft', updated_at = ? WHERE contact_id = ? AND status = 'cancelled'"
                                " AND sent_at IS NULL AND kind = 'sequence'", (stamp, contact_id)).rowcount
            detail = f"marked as an automatic answer by you; {back} follow-up(s) back in Review"
        elif kind == "reply":
            conn.execute("UPDATE contacts SET status = 'replied', reply_kind = 'reply', resume_after = NULL, updated_at = ?"
                         " WHERE id = ?", (stamp, contact_id))
            back = conn.execute("UPDATE drafts SET status = 'cancelled', updated_at = ? WHERE contact_id = ?"
                                " AND status IN ('draft', 'approved') AND kind = 'sequence'", (stamp, contact_id)).rowcount
            detail = f"marked as a real reply by you; {back} follow-up(s) stopped"
        else:
            raise ValueError("kind must be auto or reply")
    db.log_event("reply_marked", detail, contact_id, contact["domain"])
    return {"contact_id": contact_id, "kind": kind, "changed": back}


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
