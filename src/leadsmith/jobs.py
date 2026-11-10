"""Background agent runs, the engine behind the dashboard buttons.

Each job runs the user's chosen coding agent headless (Claude Code, Codex, OpenCode or Gemini CLI, see
engines.py) on their own plan, with the Leadsmith MCP tools plus the agent's own web search. Jobs can research
and draft; they can never approve or send. That stays a human click in the dashboard.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import config, db, engines
from .engines import PLUGIN_ROOT

MAX_LOG = 200_000
POLL_SECONDS = 5
EXIT_GRACE_SECONDS = 30          # after the engine says it's done, how long it gets to exit by itself
IDLE_AFTER_ANSWER_SECONDS = 240  # a final answer followed by this much silence means the run is over
STALL_SECONDS = 15 * 60          # no output at all for this long: stop the run instead of waiting all night

HEADLESS_NOTE = """You are running unattended, started from the Leadsmith dashboard.
- Never ask the user questions. Make sensible choices from the business profile and state assumptions in your final summary.
- Use the Leadsmith MCP tools, plus your web search and web fetch tools (if you have them) for discovery and finding people.
- Save everything with the Leadsmith tools; the user works from the dashboard, not this transcript.
- You cannot approve or send anything. The user reviews drafts in the dashboard.
- Genuine and specific, never generic: only real companies with a working website; a source URL and date behind
  every signal and claim; no invented names, emails, numbers or quotes; every email names something true about that
  company, and every new lead has sourced proof of need (one of the profile's buying signals). Don't pad the list
  with weak leads, but the number asked for is the goal: keep searching while untried sources and wordings remain.
  The playbook's minimum (6+ plays, 25+ searches, 5x the target screened) is a floor, never a reason to stop. When
  exact matches run out, save labelled near-fits as the playbook describes. If still short, say what you tried.
- Work fast: call independent tools in parallel, use the fastest sources for this kind of customer, and save
  candidates you rule out for a lasting reason with save_rejected so no run checks them again.
- Before your summary, call quality_check with the domains you worked on and fix what it reports.
- Finish with a short summary: what you saved (accounts, contacts, drafts), what quality_check still flags, and
  anything that blocked you."""

# Runs without sub-agents (the default) get the playbooks inline and play each role themselves.
# A find run stops before writing, so it doesn't carry the writer's playbook.
ROLES = {
    "find_buyers": ["skills/find-buyers/SKILL.md", "agents/signal-scout.md", "agents/account-researcher.md",
                    "agents/people-finder.md"],
    "find_more": ["skills/find-buyers/SKILL.md", "agents/signal-scout.md", "agents/account-researcher.md",
                  "agents/people-finder.md"],
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
    extra = "" if engines.uses_subagents(engine) else _playbooks(kind)
    return "\n\n---\n\n".join(part for part in (HEADLESS_NOTE, engine.prompt_note(), prompt, extra) if part)

PROMPTS: dict[str, tuple[str, str]] = {
    # kind -> (title template, prompt template)
    "find_buyers": (
        "Find {count} buyers{focus_title}",
        "Find {count} new accounts, following the find-buyers workflow.{focus_line}\n"
        "Phases for you: signal-scout (discover, verify, score, save) → account-researcher (research brief) and "
        "people-finder (the right people and their emails). Then stop: don't write ideas, emails or decks yourself. "
        "The next steps of this task start automatically when you finish: a deck step builds the decks and a writer "
        "writes the emails for every account you saved, and the user reviews them in the dashboard.",
    ),
    "find_more": (
        "Find more buyers: {need} to go",
        "Keep going with the same search: the user asked for {count} new accounts and {have} are saved so far "
        "({saved}). Find {need} more NEW accounts for the same brief, following the find-buyers workflow.{focus_line}\n"
        "What the previous round tried and why it stopped:\n---\n{previous}\n---\n"
        "Don't repeat it: use sources from the signal-scout's table that weren't tried, different query wording, "
        "synonyms and neighbouring niches, other pages of results, and lookalikes of the accounts already saved. "
        "Candidates rejected earlier are remembered (check_known shows them as rejected). If strict matches really "
        "run out, save near-fits as the playbook describes (label exactly what differs, rank them lower). Then "
        "research them and find the people, and stop: don't write ideas, emails or decks.",
    ),
    "research": (
        "Research & write: {domains_short}",
        "Target accounts: {domains}.\nFor each: save_account (skip ones check_known says are suppressed), "
        "then run account-researcher and people-finder, then {research_tail}{extra}",
    ),
    "write": (
        "Write outreach: {domains_short}",
        "Run the outreach-writer agent for these researched accounts: {domains}. "
        "Rewrite any existing unsent drafts if they can be better.{deck_note}{brief_line}{extra}",
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
        "(email_style), following the outreach-writer agent. Don't touch sent emails.{deck_note}{brief_line}{extra}",
    ),
    "reply": (
        "Draft reply to {person}",
        "{person} at {company} replied (contact id {contact_id}). Their latest message:\n---\n{reply_text}\n---\n"
        "{picked_line}Draft the user's answer and save it with save_reply_draft(contact_id={contact_id}, body=...). "
        "First read brain_read (offer, pricing, booking link, voice) and get_account('{domain}') (research, ideas, emails sent). "
        "Answer their actual questions in order; if they picked an idea, say concretely what you'll deliver for it, "
        "the rough timeline and the one thing you need from them. If a price, date or result isn't in the brain, don't invent it: "
        "offer a short call (use the booking link from company.md if there is one) or ask one clarifying question. "
        "Under 140 words, plain text, warm and direct, matching voice.md. Don't send anything.{brief_line}{extra}",
    ),
    "rewrite": (
        "Regenerate emails: {person}",
        "Rewrite the unsent emails for {person} at {company} (contact id {contact_id}, account {domain}): {steps}. "
        "First read brain_read (voice, email_style, step templates) and get_account('{domain}') (research, signals, ideas, "
        "and every email already sent to them). {sent_line}Write fresh, better versions of those steps, following the "
        "outreach-writer agent and the email style; don't just reword the old text. Save each with save_draft(contact_id="
        "{contact_id}, step=...) using the same step numbers. Don't touch sent emails. The new versions go back to the user "
        "for review; you can't approve or send.{deck_note}{brief_line}{extra}",
    ),
    "deck": (
        "Build decks: {domains_short}",
        "Build a personalised 4-page PDF deck for each of these accounts: {domains}.\n"
        "First call brain_read (company, proof, voice, email_style). Then for each account call get_account (research, "
        "signals, ideas, contacts, and `deck`). Skip an account whose `deck` already has a `pdf_path`, unless the "
        "user asked to rebuild.\n{deck_slides}{brief_line}\n"
        "Use only facts from the account's saved signals and research and proof written in company.md: no invented "
        "numbers, clients or results. Name the company and its real product or work. {deck_voice}\n"
        "Call save_deck exactly like this, once per account, with the finished content:\n"
        "save_deck({{domain: \"acme.com\", title: \"Acme × <your company>: <the idea in a few words>\", slides: [\n"
        "  {{heading: \"<one line>\", body: \"<1-2 sentences>\", bullets: [\"<point>\", \"<point>\"], "
        "highlight: \"<optional big stat or quote>\", kicker: \"<optional small label>\"}},\n"
        "  ... 4 slides in all ...]}})\n"
        "Every slide needs a heading and a body or bullets; the field names must be exactly these. Never save a test or "
        "placeholder deck to find out the format: it's given above, and it overwrites the prospect's real deck. If "
        "save_deck returns an `error`, fix what it says and save again. Check it returned a `pdf`. Don't write or "
        "change any emails, and don't read or open other files.{extra}",
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


DECK_NOTE = ("\nDecks are built by a separate deck step (on the deck engine), not by you: never call save_deck. "
             "Where get_account shows the account's `deck` with a `pdf_path`, attach it to step 1 (attach_deck=true) "
             "and mention it as the email style says. Where there is no PDF, write the email without any deck sentence.")

DECK_VOICE = (
    "The slides are for the prospect, not for the user: write them as a finished, polished document. Every fact "
    "must come from a saved signal or company.md, but never show your sourcing: no URLs or links, no parenthetical "
    "citations or dates of when something was found or saved, and no internal words like \"signal\", \"saved\", "
    "\"account research\", \"research brief\", \"per our notes\" or \"inferred\". Say facts the way a person would, "
    "e.g. \"your 8 October upload, Using OpenCode securely with Docker\", not \"New upload: ..., 2026-10-08 "
    "(youtube.com/watch?v=...)\". Write the user's company name exactly as company.md writes it.")

DECK_SLIDES = {
    "personal_pitch": (
        "If the account has no ideas yet, first save_ideas with the single strongest idea first (plus up to two "
        "alternatives), each answering a saved signal, so the emails and the deck pitch the same thing. Slides: "
        "1) their moment (cite a saved signal and why it matters now), 2) what you'd make for them (the first idea, "
        "made specific to their product or work), 3) the concrete plan: scope, format, timeline, with proof from "
        "company.md, 4) the next step: a short call."),
    "idea_menu": (
        "If the account has no ideas yet, first save_ideas with three ideas (A, B, C), each answering a saved signal. "
        "Slides: 1) their situation (cite saved signals), 2) the three ideas, 3) the strongest idea in detail with "
        "proof from company.md, 4) the next step: reply with A, B or C."),
}


def _brief_line(domains: list[str]) -> str:
    """The user's search brief(s) these accounts were found for: what to pitch them."""
    if not domains:
        return ""
    with db.connect() as conn:
        found = db.rows(conn.execute(
            f"SELECT domain, brief FROM accounts WHERE domain IN ({','.join('?' * len(domains))}) AND brief IS NOT NULL"
            " AND brief != ''", domains))
    if not found:
        return ""
    briefs: dict[str, list[str]] = {}
    for row in found:
        briefs.setdefault(row["brief"], []).append(row["domain"])
    parts = [f"For {', '.join(ds)}:\n{text}" for text, ds in briefs.items()]
    return ("\n\nWHAT TO PITCH. These accounts were found for the user's own search brief below. It says what the "
            "user is offering them in this campaign, and it wins over the default offer in company.md: pitch the "
            "brief's services and angle, in the emails and in the deck. Use company.md only for the sender, voice "
            "and proof that is relevant to this offer; leave out proof and services from company.md that don't match "
            "the brief (if none matches, cite no proof). Skip the brief's research instructions (rankings, lists): "
            "they were for the search.\n\n" + "\n\n".join(parts))


def _style() -> str:
    style = (config.get("EMAIL_STYLE") or "idea_menu").strip()
    return style if style in DECK_SLIDES else "idea_menu"


def decks_wanted() -> bool:
    """Every first email gets a deck in the personal-pitch style, and in the idea-menu style when voice.md asks."""
    if _style() == "personal_pitch":
        return True
    try:
        voice = (config.BRAIN_DIR / "voice.md").read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r"(?im)^\s*-\s*4-page PDF deck per account:\s*yes", voice))


def _already_have() -> str:
    """The companies already in the pipeline, so a search doesn't spend its effort finding them again."""
    domains = db.known_domains(200)
    if not domains:
        return ""
    return ("\nAlready in my pipeline (do NOT find or save these again; I want NEW companies, so keep searching until you "
            f"have the number asked for): {', '.join(domains)}")


def _fmt(kind: str, params: dict[str, Any]) -> tuple[str, str]:
    title_t, prompt_t = PROMPTS[kind]
    domains = [db.normalize_domain(d) for d in params.get("domains") or [] if d.strip()]
    focus = (params.get("focus") or "").strip()
    values = {
        "count": int(params.get("count") or 10),
        "focus_title": f": {focus[:40]}" if focus else "",
        "focus_line": (f"\nFocus from the user: {focus}" if focus else "") + _already_have(),
        "domains": ", ".join(domains),
        "domains_short": ", ".join(domains[:3]) + (f" +{len(domains) - 3}" if len(domains) > 3 else ""),
        "website": db.normalize_domain(params.get("website") or ""),
        "extra": f"\nExtra instructions from the user: {params['extra']}" if params.get("extra") else "",
        "title": (params.get("title") or params.get("prompt") or "Custom run")[:60],
        "prompt": params.get("prompt") or "",
        "research_tail": ("stop: the decks and emails are made by the next steps of this task." if params.get("_chained")
                          else "outreach-writer to draft the 3-step sequence for every contact with an email."),
        "deck_note": DECK_NOTE if decks_wanted() else "",
        "brief_line": _brief_line(domains),
        "need": params.get("need") or "", "have": params.get("have") or 0, "saved": params.get("saved") or "",
        "previous": (params.get("previous") or "(no summary)")[-1500:],
        "deck_slides": DECK_SLIDES[_style()],
        "deck_voice": DECK_VOICE,
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
                      picked_line=f"They picked idea {picked['letter']}) {picked['title']}.\n" if picked else "",
                      brief_line=_brief_line([contact["domain"]]))
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
                      brief_line=_brief_line([contact["domain"]]),
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


def _run(job_id: int, kind: str, prompt: str, engine: engines.Engine, session: dict[str, Any] | None,
         model: str | None = None) -> bool:
    """Run one job to the end. Returns True when it finished ("done").
    `session` is the connection's shared conversation: {"session_id": ...}. The run continues it when it has an
    id, and records the id the engine reports, so the next run in this connection continues the same conversation."""
    resume = (session or {}).get("session_id")
    scope = "deck" if kind == "deck" else ""  # a deck run only gets the deck tools, and no web search
    scoped = {"scope": scope} if scope else {}
    try:
        cmd, extra_env = engine.command(engines.model_for(engine) if model is None else model, resume, **scoped)
    except OSError as exc:
        _finish(job_id, "failed", f"Could not prepare {engine.label}: {exc}", None)
        return False
    env = {**os.environ, **extra_env, "LEADSMITH_HOME": str(config.HOME)}
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (db.now(), job_id))
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", env=env, cwd=str(engine.workdir(**scoped)),
                                creationflags=engines.NO_WINDOW)
    except OSError as exc:
        _finish(job_id, "failed", f"Could not start {engine.label}: {exc}", None)
        return False
    _PROCS[job_id] = proc
    assert proc.stdin and proc.stdout
    proc.stdin.write(full_prompt(kind, prompt, engine))
    proc.stdin.close()
    # Both pipes are read on their own threads: the run loop can then notice a CLI that answered but never exits,
    # and a chatty stderr can never fill its pipe and freeze the engine.
    lines: queue.Queue[str | None] = queue.Queue()
    errors: list[str] = []
    threading.Thread(target=_pump, args=(proc.stdout, lines.put, True), daemon=True).start()
    err_thread = threading.Thread(target=_pump, args=(proc.stderr, errors.append), daemon=True)
    err_thread.start()
    result_text, cost, error, seen_session = "", 0.0, None, None
    done_at = final_at = None  # when the engine said it's over / gave an answer with nothing after it
    hung = stalled = False
    last_output = time.monotonic()
    while True:
        try:
            line = lines.get(timeout=POLL_SECONDS)
        except queue.Empty:
            quiet = time.monotonic()
            if (done_at and quiet - done_at > EXIT_GRACE_SECONDS) or (final_at and quiet - final_at > IDLE_AFTER_ANSWER_SECONDS):
                hung = True  # the work is finished; the CLI just didn't exit
                _kill_tree(proc)
                break
            if quiet - last_output > STALL_SECONDS:
                stalled = True  # nothing at all for too long: a web request hung, or the PC slept
                _kill_tree(proc)
                break
            continue
        last_output = time.monotonic()
        if line is None:
            break
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
        final_at = time.monotonic() if parsed.final else None  # any other output means it's still working
        if parsed.done:
            done_at = time.monotonic()
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.wait()
    err_thread.join(timeout=5)
    stderr = "".join(errors).strip()
    for pipe in (proc.stdout, proc.stderr):
        if pipe:
            try:
                pipe.close()
            except OSError:
                pass
    _PROCS.pop(job_id, None)
    result_text = result_text.strip() or None
    spent = cost or None
    if _CANCELLED.pop(job_id, False):
        _finish(job_id, "cancelled", result_text or "Stopped by you.", spent)
        return False
    if stalled:
        minutes = round(STALL_SECONDS / 60)
        _finish(job_id, "failed", f"Stopped: {engine.label} made no progress for {minutes} minutes (a web request hung, "
                "the internet dropped, or the PC went to sleep). Everything it saved so far is kept. Click Rerun to "
                "finish the rest.", spent)
        return False
    if hung and not error:
        _append(job_id, f"\n({engine.label} finished but didn't close by itself; Leadsmith closed it.)\n")
        _finish(job_id, "done", result_text, spent)
        return True
    if proc.returncode != 0 and not error and done_at and result_text:
        # The engine said its work was finished and reported no error, but still exited with an error code (OpenCode
        # does this after a tool failed along the way, e.g. web search). The work is done: don't redo it.
        _append(job_id, f"\n({engine.label} finished its work but exited with code {proc.returncode}; "
                        "counted as done.)\n")
        _finish(job_id, "done", result_text, spent)
        return True
    if proc.returncode != 0 or error:
        message = error or result_text or explain_failure(stderr, engine.label) or f"{engine.label} exited with code {proc.returncode}"
        if any(word in message.lower() for word in ("auth", "login", "log in", "sign in", "credential", "api key")):
            message += f"\n{engine.sign_in}"
        _finish(job_id, "failed", message, spent)
        return False
    _finish(job_id, "done", result_text, spent)
    return True


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
    if kind == "deck":
        title, prompt = _fmt("deck", params)
        return _launch_chain([_deck_stage(title, prompt)], session)
    if kind == "find_buyers":
        title, prompt = _fmt(kind, params)
        return _launch_chain(_find_chain(title, prompt, engine), session)
    if kind in ("research", "write") and decks_wanted():
        # Decks are built on the deck engine (OpenCode by default), between research and writing.
        stages: list[Stage] = []
        if kind == "research":
            title, prompt = _fmt("research", {**params, "_chained": True})
            stages.append(Stage("research", title, prompt, engine))
        stages.append(_deck_stage(*_fmt("deck", params)))
        title, prompt = _fmt("write", params)
        stages.append(Stage("write", title, prompt, engine))
        return _launch_chain(stages, session)
    title, prompt = _fmt(kind, params)
    return _launch(kind, title, prompt, engine, session)


@dataclass
class Stage:
    """One step of a task that runs as a chain of jobs, one after another."""
    kind: str
    title: str
    prompt: str
    engine: engines.Engine
    model: str | None = None   # None = the engine's usual model
    note: str | None = None    # shown at the top of its log, e.g. why the deck engine fell back
    optional: bool = False     # when it fails, the next steps still run (a failed deck only means no attachment)
    build: Any = None          # (domains) -> (title, prompt): written when the chain's first step has saved accounts


def _find_chain(title: str, prompt: str, engine: engines.Engine) -> list[Stage]:
    """Find → decks (on the deck engine) → emails, all automatic. The later steps learn which accounts the search
    saved only when it's done, so their prompts are written then."""
    stages = [Stage("find_buyers", title, prompt, engine)]
    waiting = "(written when the search finishes, for the accounts it saved)"
    count, focus = _brief_of(prompt)

    def top_up(domains: list[str], previous: str) -> tuple[str, str] | None:
        if len(domains) >= count:
            return None
        return _fmt("find_more", {"count": count, "focus": focus, "have": len(domains), "need": count - len(domains),
                                  "saved": ", ".join(domains) or "none yet", "previous": previous})

    for _ in range(TOP_UP_ROUNDS):
        # Only runs when the search came back short; if it fails, the accounts already found still get written to.
        stages.append(Stage("find_more", "Find more buyers (if needed)", "(written if the search found fewer than "
                            "asked)", engine, optional=True, build=top_up))
    def for_found(kind: str):
        def build(domains: list[str], previous: str) -> tuple[str, str]:
            db.set_brief(domains, focus)  # the offer this search was for travels with each account
            return _fmt(kind, {"domains": domains})
        return build

    if decks_wanted():
        deck = _deck_stage("Build decks: new leads", waiting)
        deck.build = for_found("deck")
        stages.append(deck)
    stages.append(Stage("write", "Write outreach: new leads", waiting, engine, build=for_found("write")))
    return stages


TOP_UP_ROUNDS = 2  # extra search rounds when a search saves fewer accounts than asked


def _brief_of(prompt: str) -> tuple[int, str]:
    """(count, focus) back from a find prompt, for top-up rounds and reruns."""
    count = int((re.search(r"Find (\d+) new accounts", prompt) or [None, 10])[1])
    focus = prompt.split("Focus from the user:", 1)[1].split("\nAlready in my pipeline", 1)[0].strip() \
        if "Focus from the user:" in prompt else ""
    return count, focus


def found_by(job_id: int) -> list[str]:
    """The accounts a search saved from when it started (its first round) until now: the steps after it run
    once every round is done."""
    job = get(job_id) or {}
    if not job.get("started_at"):
        return []
    with db.connect() as conn:
        return [r["domain"] for r in db.rows(conn.execute(
            "SELECT domain FROM accounts WHERE created_at >= ? ORDER BY created_at", (job["started_at"],)))]


def _deck_stage(title: str, prompt: str) -> Stage:
    engine, model, note = engines.deck_engine()
    return Stage("deck", title, prompt, engine, model, note, optional=True)


def _launch(kind: str, title: str, prompt: str, engine: engines.Engine, session: dict[str, Any] | None) -> dict[str, Any]:
    return _launch_chain([Stage(kind, title, prompt, engine)], session)


def _launch_chain(stages: list[Stage], session: dict[str, Any] | None) -> dict[str, Any]:
    """Create every step's run up front (so they all show in the dashboard as queued), then run them in order.
    A failed or stopped step cancels the steps after it, unless it was optional."""
    ids = []
    with db.connect() as conn:
        for stage in stages:
            cur = conn.execute("INSERT INTO jobs (kind, title, prompt, engine, created_at) VALUES (?, ?, ?, ?, ?)",
                               (stage.kind, stage.title, stage.prompt, stage.engine.key, db.now()))
            ids.append(cur.lastrowid)
    # Two runs can't write to one conversation at once: while the session is busy, this run gets its own.
    shared = session if not (session and session.get("session_id") and _session_busy(session["session_id"])) else None

    main = next((s.engine for s in stages if s.kind != "deck"), engines.current())

    def run_all() -> None:
        blocked_by = None
        for job_id, stage in zip(ids, stages):
            if blocked_by:
                _finish(job_id, "cancelled", f"Not started: the step before it ({blocked_by}) didn't finish.", None)
                continue
            if stage.build:
                domains = found_by(ids[0])
                if not domains and stage.kind != "find_more":
                    _finish(job_id, "cancelled", "Nothing to do: the search didn't save any new accounts.", None)
                    continue
                previous = next((get(i) or {} for i in reversed(ids[:ids.index(job_id)])
                                 if (get(i) or {}).get("kind") in ("find_buyers", "find_more")
                                 and (get(i) or {}).get("result")), {}).get("result") or ""
                built = stage.build(domains, previous)
                if built is None:
                    _finish(job_id, "done", f"Not needed: the search already found {len(domains)} accounts.", None)
                    continue
                stage.title, stage.prompt = built
                with db.connect() as conn:
                    conn.execute("UPDATE jobs SET title = ?, prompt = ? WHERE id = ?", (stage.title, stage.prompt, job_id))
            if stage.note:
                _append(job_id, f"({stage.note})\n")
            # The deck step starts its own conversation (often on another engine); every other step continues the
            # connection's.
            ok = _run(job_id, stage.kind, stage.prompt, stage.engine, None if stage.kind == "deck" else shared,
                      stage.model)
            if not ok and stage.kind == "deck" and stage.engine.key != main.key and main.path():
                # Decks make the emails stand out: when the deck engine fails (e.g. a free model's limit), the main
                # engine builds them before the emails are written. Accounts that already got a deck are skipped.
                failed = get(job_id) or {}
                note = (f"{stage.engine.label} couldn't build the decks ({(failed.get('result') or 'failed')[:160]}), "
                        f"so {main.label} is building them.")
                with db.connect() as conn:
                    retry = conn.execute("INSERT INTO jobs (kind, title, prompt, engine, created_at) VALUES (?, ?, ?, ?, ?)",
                                         ("deck", f"{stage.title} (on {main.label})", stage.prompt, main.key,
                                          db.now())).lastrowid
                _append(retry, f"({note})\n")
                ok = _run(retry, "deck", stage.prompt, main, None, engines.model_for(main))
            if not ok and not stage.optional:
                blocked_by = stage.title

    threading.Thread(target=run_all, daemon=True).start()
    return get(ids[0]) or {}


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
    prompt = old["prompt"] if RERUN_NOTE.strip() in old["prompt"] else old["prompt"] + RERUN_NOTE
    if old["kind"] == "deck":  # decks always go to the deck engine
        return _launch_chain([_deck_stage(old["title"], prompt)], session)
    engine = engines.ENGINES.get(engine_key) or engines.current()
    if not engine.path():
        raise ValueError(f"{engine.label} isn't installed. {engine.install}")
    if old["kind"] == "find_buyers":  # a rerun search still goes on to decks and emails
        return _launch_chain(_find_chain(old["title"], prompt, engine), session)
    return _launch(old["kind"], old["title"], prompt, engine, session)


def cancel(job_id: int) -> bool:
    proc = _PROCS.get(job_id)
    if not proc:
        return False
    _CANCELLED[job_id] = True
    _kill_tree(proc)
    return True


def _kill_tree(proc: subprocess.Popen) -> None:
    """End the engine and everything it started (its MCP server, sub-processes)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # npm-installed CLIs run through a .cmd shim; end the whole process tree, not just the shim.
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=engines.NO_WINDOW)
    else:
        proc.terminate()


def _pump(pipe, put, signal_end: bool = False) -> None:
    """Copy a pipe line by line; with signal_end, put None when it closes."""
    try:
        for line in pipe:
            put(line)
    except (OSError, ValueError):
        pass
    finally:
        if signal_end:
            put(None)


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
