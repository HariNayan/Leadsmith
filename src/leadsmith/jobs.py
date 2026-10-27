"""Background agent runs, the engine behind the dashboard buttons.

Each job runs the user's chosen coding agent headless (Claude Code, Codex, OpenCode or Gemini CLI, see
engines.py) on their own plan, with the Leadsmith MCP tools plus the agent's own web search. Jobs can research
and draft; they can never approve or send. That stays a human click in the dashboard.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from typing import Any

from . import config, db, engines
from .engines import PLUGIN_ROOT

MAX_LOG = 200_000

HEADLESS_NOTE = """You are running unattended, started from the Leadsmith dashboard.
- Never ask the user questions. Make sensible choices from the business profile and state assumptions in your final summary.
- Use the Leadsmith MCP tools, plus your web search and web fetch tools (if you have them) for discovery and finding people.
- Save everything with the Leadsmith tools; the user works from the dashboard, not this transcript.
- You cannot approve or send anything. The user reviews drafts in the dashboard.
- Genuine and specific, never generic: only real companies with a working website; a source URL and date behind
  every signal and claim; no invented names, emails, numbers or quotes; every email names something true about that
  company. If you can't find enough that genuinely fit, return fewer and say why. Don't pad the list.
- Before your summary, call quality_check with the domains you worked on and fix what it reports.
- Finish with a short summary: what you saved (accounts, contacts, drafts), what quality_check still flags, and
  anything that blocked you."""

# Engines without sub-agents get the playbooks inline and play each role themselves.
ROLES = {
    "find_buyers": ["skills/find-buyers/SKILL.md", "agents/signal-scout.md", "agents/account-researcher.md",
                    "agents/people-finder.md", "agents/outreach-writer.md"],
    "research": ["agents/account-researcher.md", "agents/people-finder.md", "agents/outreach-writer.md"],
    "write": ["agents/outreach-writer.md"],
    "ideas": ["agents/outreach-writer.md"],
    "emails": ["agents/people-finder.md"],
    "rewrite": ["agents/outreach-writer.md"],
}
SOLO_NOTE = """You don't have sub-agents. Wherever these instructions say to run or dispatch an agent (signal-scout,
account-researcher, people-finder, outreach-writer, with or without a "leadsmith:" prefix), do that role's work yourself,
one role after another, following its playbook below. Skip any step about reviewing drafts with the user, approving or
sending. Your tool list may show the Leadsmith tools with a prefix (leadsmith_..., mcp_leadsmith_...): they are the
tools named in the playbooks. If you have no web search tool, rely on search_news, find_yc_companies, find_hn_hiring,
scan_website and find_emails_on_page."""


def _playbooks(kind: str) -> str:
    docs = []
    for rel in ROLES.get(kind, []):
        try:
            text = (PLUGIN_ROOT / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        docs.append(re.sub(r"\A---.*?---\s*", "", text, flags=re.S).strip())
    return "\n\n".join([SOLO_NOTE, *(f"=== Playbook: {rel} ===\n{doc}" for rel, doc in zip(ROLES[kind], docs))]) if docs else ""


def full_prompt(kind: str, prompt: str, engine: engines.Engine) -> str:
    extra = "" if engine.subagents else _playbooks(kind)
    return "\n\n---\n\n".join(part for part in (HEADLESS_NOTE, prompt, extra) if part)

PROMPTS: dict[str, tuple[str, str]] = {
    # kind -> (title template, prompt template)
    "find_buyers": (
        "Find {count} buyers{focus_title}",
        "Run the leadsmith:find-buyers workflow for {count} accounts.{focus_line}\n"
        "Phases: signal-scout → account-researcher and people-finder in parallel → outreach-writer. "
        "Stop once drafts are saved (skip the review and send steps; the user reviews in the dashboard).",
    ),
    "research": (
        "Research & write: {domains_short}",
        "Target accounts: {domains}.\nFor each: save_account (skip ones check_known says are suppressed), "
        "then run account-researcher and people-finder in parallel, then outreach-writer to draft the 3-step "
        "sequence for every contact with an email.{extra}",
    ),
    "write": (
        "Write outreach: {domains_short}",
        "Run the outreach-writer agent for these researched accounts: {domains}. "
        "Rewrite any existing unsent drafts if they can be better.{extra}",
    ),
    "emails": (
        "Find published emails: {domains_short}",
        "For every contact at these accounts ({domains}) whose email_status is \"guessed\" or who has no email, "
        "look for a published address before keeping a guess, following step 4 of the people-finder agent: "
        "the company site, LinkedIn search snippets (never fetch LinkedIn pages), then the person's own website, "
        "GitHub (github_profile), YouTube About, newsletter or talk pages (find_emails_on_page). When you find one, "
        "save_contact with the new email, email_status \"public\" and the page URL in notes. If the address changed "
        "and the contact has unsent drafts, they keep working: drafts follow the contact. Report found vs still guessed.{extra}",
    ),
    "ideas": (
        "New ideas: {domains_short}",
        "For these accounts ({domains}): write 3 fresh ideas with save_ideas (different from the current ones, "
        "keep any picked idea), then rewrite the unsent drafts around them in the email style brain_read gives "
        "(email_style), following the outreach-writer agent. Don't touch sent emails.{extra}",
    ),
    "reply": (
        "Draft reply to {person}",
        "{person} at {company} replied (contact id {contact_id}). Their latest message:\n---\n{reply_text}\n---\n"
        "{picked_line}Draft the user's answer and save it with save_reply_draft(contact_id={contact_id}, body=...). "
        "First read brain_read (offer, pricing, booking link, voice) and get_account('{domain}') (research, ideas, emails sent). "
        "Answer their actual questions in order; if they picked an idea, say concretely what you'll deliver for it, "
        "the rough timeline and the one thing you need from them. If a price, date or result isn't in the brain, don't invent it: "
        "offer a short call (use the booking link from company.md if there is one) or ask one clarifying question. "
        "Under 140 words, plain text, warm and direct, matching voice.md. Don't send anything.{extra}",
    ),
    "rewrite": (
        "Regenerate emails: {person}",
        "Rewrite the unsent emails for {person} at {company} (contact id {contact_id}, account {domain}): {steps}. "
        "First read brain_read (voice, email_style, step templates) and get_account('{domain}') (research, signals, ideas, "
        "and every email already sent to them). {sent_line}Write fresh, better versions of those steps, following the "
        "outreach-writer agent and the email style; don't just reword the old text. Save each with save_draft(contact_id="
        "{contact_id}, step=...) using the same step numbers. Don't touch sent emails. The new versions go back to the user "
        "for review; you can't approve or send.{extra}",
    ),
    "deck": (
        "Build decks: {domains_short}",
        "For each account ({domains}) create a personalised 4-page PDF deck with save_deck, using get_account "
        "research, saved signals and only proof from the brain: 1) their situation and what's changing, "
        "2) the problem and its cost, 3) how we solve it + proof, 4) a clear, low-friction next step.{extra}",
    ),
    "autofill": (
        "Fill business profile from {website}",
        "Fill in the business profile for the business at {website}. Use scan_website and WebFetch on its key pages "
        "(home, product, pricing, customers, about). Draft company.md, icp.md, personas.md and voice.md with "
        "brain_write, keeping each template's headings. Use only facts you found; where something isn't knowable "
        "(real results, banned words, sender name), leave the TODO line so the user fills it in.{extra}",
    ),
    "custom": ("{title}", "{prompt}"),
}


def _fmt(kind: str, params: dict[str, Any]) -> tuple[str, str]:
    title_t, prompt_t = PROMPTS[kind]
    domains = [db.normalize_domain(d) for d in params.get("domains") or [] if d.strip()]
    focus = (params.get("focus") or "").strip()
    values = {
        "count": int(params.get("count") or 10),
        "focus_title": f": {focus[:40]}" if focus else "",
        "focus_line": f"\nFocus from the user: {focus}" if focus else "",
        "domains": ", ".join(domains),
        "domains_short": ", ".join(domains[:3]) + (f" +{len(domains) - 3}" if len(domains) > 3 else ""),
        "website": db.normalize_domain(params.get("website") or ""),
        "extra": f"\nExtra instructions from the user: {params['extra']}" if params.get("extra") else "",
        "title": (params.get("title") or params.get("prompt") or "Custom run")[:60],
        "prompt": params.get("prompt") or "",
    }
    if kind in ("research", "write", "deck", "emails", "ideas") and not domains:
        raise ValueError("Pick at least one company.")
    if kind == "reply":
        contact = db.get_contact(int(params.get("contact_id") or 0))
        if not contact or not contact.get("last_reply"):
            raise ValueError("No reply from this person to answer yet.")
        with db.connect() as conn:
            acct = db.one(conn.execute("SELECT name FROM accounts WHERE domain = ?", (contact["domain"],))) or {}
            picked = db.one(conn.execute("SELECT letter, title FROM ideas WHERE domain = ? AND status = 'picked'", (contact["domain"],)))
        person = " ".join(p for p in (contact.get("first_name"), contact.get("last_name")) if p)
        values.update(person=person, company=acct.get("name") or contact["domain"], contact_id=contact["id"],
                      domain=contact["domain"], reply_text=contact["last_reply"][:1500],
                      picked_line=f"They picked idea {picked['letter']}) {picked['title']}.\n" if picked else "")
        return PROMPTS["reply"][0].format(**values), PROMPTS["reply"][1].format(**values)
    if kind == "rewrite":
        contact = db.get_contact(int(params.get("contact_id") or 0))
        if not contact:
            raise ValueError("Pick a person to regenerate emails for.")
        with db.connect() as conn:
            acct = db.one(conn.execute("SELECT name FROM accounts WHERE domain = ?", (contact["domain"],))) or {}
            steps = db.rows(conn.execute(
                "SELECT step, status FROM drafts WHERE contact_id = ? AND channel = 'email' AND kind = 'sequence'"
                " ORDER BY step", (contact["id"],)))
        unsent = [s["step"] for s in steps if s["status"] in ("draft", "approved", "rejected")]
        if not unsent:
            raise ValueError("This person has no unsent emails to regenerate.")
        sent = [s["step"] for s in steps if s["status"] == "sent"]
        person = " ".join(p for p in (contact.get("first_name"), contact.get("last_name")) if p) or contact.get("email") or "this person"
        values.update(person=person, company=acct.get("name") or contact["domain"], contact_id=contact["id"],
                      domain=contact["domain"], steps=", ".join(f"step {n}" for n in unsent),
                      sent_line=(f"Step {', '.join(map(str, sent))} already went out, so the new emails are follow-ups in "
                                 "that same thread and must read naturally after it. ") if sent else "")
        return PROMPTS["rewrite"][0].format(**values), PROMPTS["rewrite"][1].format(**values)
    if kind == "autofill" and not values["website"]:
        raise ValueError("Enter your website.")
    if kind == "custom" and not values["prompt"].strip():
        raise ValueError("Type what you want Leadsmith to do.")
    return title_t.format(**values), prompt_t.format(**values)


def _append(job_id: int, text: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET log = substr(log || ?, -?) WHERE id = ?", (text, MAX_LOG, job_id))


def _run(job_id: int, kind: str, prompt: str, engine: engines.Engine, session: dict[str, Any] | None) -> None:
    """`session` is the connection's shared conversation: {"session_id": ...}. The run continues it when it has an
    id, and records the id the engine reports, so the next run in this connection continues the same conversation."""
    resume = (session or {}).get("session_id")
    try:
        cmd, extra_env = engine.command(engines.current_model(), resume)
    except OSError as exc:
        _finish(job_id, "failed", f"Could not prepare {engine.label}: {exc}", None)
        return
    env = {**os.environ, **extra_env, "LEADSMITH_HOME": str(config.HOME)}
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (db.now(), job_id))
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", env=env, cwd=str(config.HOME),
                                creationflags=engines.NO_WINDOW)
    except OSError as exc:
        _finish(job_id, "failed", f"Could not start {engine.label}: {exc}", None)
        return
    _PROCS[job_id] = proc
    assert proc.stdin and proc.stdout
    proc.stdin.write(full_prompt(kind, prompt, engine))
    proc.stdin.close()
    result_text, cost, error, seen_session = "", 0.0, None, None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            _append(job_id, line + "\n")
            continue
        if not isinstance(event, dict):
            continue
        if not seen_session and (sid := engine.session_of(event)):
            seen_session = sid
            with db.connect() as conn:
                conn.execute("UPDATE jobs SET session_id = ? WHERE id = ?", (sid, job_id))
            if session is not None:
                session["session_id"] = sid
        parsed = engine.parse(event)
        if parsed.log:
            _append(job_id, parsed.log)
        if parsed.result is not None:
            result_text = parsed.result
        result_text += parsed.result_part
        cost += parsed.cost
        error = parsed.error or error
    proc.wait()
    stderr = (proc.stderr.read() if proc.stderr else "").strip()
    for pipe in (proc.stdout, proc.stderr):
        if pipe:
            pipe.close()
    _PROCS.pop(job_id, None)
    result_text = result_text.strip() or None
    spent = cost or None
    if _CANCELLED.pop(job_id, False):
        _finish(job_id, "cancelled", result_text or "Stopped by you.", spent)
    elif proc.returncode != 0 or error:
        message = error or result_text or explain_failure(stderr, engine.label) or f"{engine.label} exited with code {proc.returncode}"
        if any(word in message.lower() for word in ("auth", "login", "log in", "sign in", "credential", "api key")):
            message += f"\n{engine.sign_in}"
        _finish(job_id, "failed", message, spent)
    else:
        _finish(job_id, "done", result_text, spent)


def _finish(job_id: int, status: str, result: str | None, cost: float | None) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = ?, result = ?, cost_usd = ?, finished_at = ? WHERE id = ?",
                     (status, result, cost, db.now(), job_id))


_PROCS: dict[int, subprocess.Popen] = {}
_CANCELLED: dict[int, bool] = {}


# Common reasons an engine stops, in plain words. Matched against its error output.
FAILURE_HINTS = [
    (r"\b503\b|high demand|overloaded|UNAVAILABLE", "The model's servers are busy right now (high demand). "
     "This is on the provider's side: try again in a few minutes, or choose another model in Settings."),
    (r"\b429\b|rate.?limit|quota|RESOURCE_EXHAUSTED|usage limit", "You've hit a usage or rate limit on this plan. "
     "Wait for it to reset, or use Run with another agent to finish this task now."),
    (r"ENOTFOUND|ECONNREFUSED|ETIMEDOUT|fetch failed|getaddrinfo|network", "Couldn't reach the model provider. "
     "Check your internet connection and try again."),
]


def explain_failure(stderr: str, label: str) -> str:
    """The useful part of an engine's error output: a plain-English reason plus the original message, without
    stack traces or startup warnings."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("at ") and not re.match(r"(?i)warning:|yolo mode|ripgrep|skill conflict", ln)]
    if not lines:
        return ""
    hint = next((text for pattern, text in FAILURE_HINTS if re.search(pattern, stderr, re.I)), "")
    detail = next((ln for ln in lines if re.search(r"(?i)error|failed|denied|invalid|not found", ln)), lines[-1])
    detail = re.sub(r"\s+", " ", detail)[:400]
    return f"{label}: {hint}\n\nDetails: {detail}" if hint else f"{label} stopped with an error.\n\nDetails: {detail}"


def last_session(engine_key: str) -> dict[str, Any] | None:
    """The most recent conversation this engine had in Leadsmith, for "continue previous session"."""
    with db.connect() as conn:
        return db.one(conn.execute(
            "SELECT session_id, title, status, COALESCE(finished_at, started_at, created_at) AS at FROM jobs"
            " WHERE engine = ? AND session_id IS NOT NULL ORDER BY id DESC LIMIT 1", (engine_key,)))


def _session_busy(session_id: str) -> bool:
    with db.connect() as conn:
        return bool(conn.execute("SELECT 1 FROM jobs WHERE session_id = ? AND status IN ('queued', 'running')",
                                 (session_id,)).fetchone())


def start(kind: str, params: dict[str, Any], engine_key: str | None = None,
          session: dict[str, Any] | None = None) -> dict[str, Any]:
    if kind not in PROMPTS:
        raise ValueError(f"Unknown job kind {kind}")
    engine = engines.ENGINES.get(engine_key or "") or engines.current()
    if not engine.path():
        raise ValueError(f"{engine.label} isn't installed. {engine.install}")
    title, prompt = _fmt(kind, params)
    return _launch(kind, title, prompt, engine, session)


def _launch(kind: str, title: str, prompt: str, engine: engines.Engine, session: dict[str, Any] | None) -> dict[str, Any]:
    with db.connect() as conn:
        cur = conn.execute("INSERT INTO jobs (kind, title, prompt, engine, created_at) VALUES (?, ?, ?, ?, ?)",
                           (kind, title, prompt, engine.key, db.now()))
        job_id = cur.lastrowid
    # Two runs can't write to one conversation at once: while the session is busy, this run gets its own.
    shared = session if not (session and session.get("session_id") and _session_busy(session["session_id"])) else None
    threading.Thread(target=_run, args=(job_id, kind, prompt, engine, shared), daemon=True).start()
    return get(job_id) or {}


RERUN_NOTE = ("\n\nNote: an earlier attempt at this task stopped part-way (for example the agent hit a usage limit). "
              "Some of it may already be saved: check with get_account / list_drafts first, keep what's good, and finish "
              "the rest instead of starting over or saving duplicates.")


def rerun(job_id: int, engine_key: str, session: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a finished, failed or stopped task again, on the given (possibly different) engine."""
    old = get(job_id)
    if not old:
        raise ValueError("That run no longer exists.")
    if old["status"] in ("queued", "running"):
        raise ValueError("That run is still going.")
    engine = engines.ENGINES.get(engine_key) or engines.current()
    if not engine.path():
        raise ValueError(f"{engine.label} isn't installed. {engine.install}")
    prompt = old["prompt"] if RERUN_NOTE.strip() in old["prompt"] else old["prompt"] + RERUN_NOTE
    return _launch(old["kind"], old["title"], prompt, engine, session)


def cancel(job_id: int) -> bool:
    proc = _PROCS.get(job_id)
    if not proc:
        return False
    _CANCELLED[job_id] = True
    if os.name == "nt":
        # npm-installed CLIs run through a .cmd shim; end the whole process tree, not just the shim.
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.terminate()
    return True


def get(job_id: int) -> dict[str, Any] | None:
    with db.connect() as conn:
        return db.one(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)))


def recent(limit: int = 30) -> list[dict[str, Any]]:
    with db.connect() as conn:
        return db.rows(conn.execute(
            "SELECT id, kind, title, status, cost_usd, engine, session_id, started_at, finished_at, created_at,"
            " substr(result, 1, 400) AS result FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ))


def mark_orphans() -> None:
    """Jobs left 'running' by a previous dashboard process can't be resumed."""
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'failed', result = COALESCE(result, 'Dashboard was closed while running.')"
                     " WHERE status IN ('queued', 'running')")
