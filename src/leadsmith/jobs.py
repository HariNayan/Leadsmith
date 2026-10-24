"""Background runs of Claude Code, the engine behind the dashboard buttons.

Each job runs `claude -p` headless on the user's own Claude plan, with this
plugin loaded (agents + skills + MCP tools) plus Claude's built-in WebSearch
and WebFetch. Jobs can research and draft; they can never approve or send.
That stays a human click in the dashboard.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from . import config, db

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
# Jobs attach the MCP server straight from this Python (instant start, same code as the dashboard)
# and load agents + skills from the plugin folder. --strict-mcp-config drops the plugin's own uv-launched copy.
TOOL_PREFIX = "mcp__leadsmith__"
ALLOWED_TOOLS = [f"{TOOL_PREFIX}*", "WebSearch", "WebFetch", "Agent", "Task", "Skill", "TodoWrite"]
BLOCKED_TOOLS = [f"{TOOL_PREFIX}{name}" for name in ("review_drafts", "send_approved_emails")] + [
    "Bash", "Edit", "Write", "NotebookEdit",
]
MAX_LOG = 200_000

HEADLESS_NOTE = """You are running unattended, started from the Leadsmith dashboard.
- Never ask the user questions. Make sensible choices from the business profile and state assumptions in your final summary.
- Use the leadsmith agents and MCP tools, plus WebSearch/WebFetch for discovery and finding people.
- Save everything with the leadsmith tools; the user works from the dashboard, not this transcript.
- You cannot approve or send anything. The user reviews drafts in the dashboard.
- Finish with a short summary: what you saved (accounts, contacts, drafts) and anything that blocked you."""

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
        "keep any picked idea), then rewrite the unsent drafts so email 1 offers the new A/B/C menu, following the "
        "outreach-writer agent. Don't touch sent emails.{extra}",
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


def claude_path() -> str | None:
    return shutil.which("claude")


def claude_status() -> dict[str, Any]:
    path = claude_path()
    if not path:
        return {"installed": False, "logged_in": False,
                "help": "Install Claude Code: https://claude.com/claude-code, then run `claude` once to sign in."}
    try:
        out = subprocess.run([path, "auth", "status", "--json"], capture_output=True, text=True, timeout=20)
        info = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        info = {}
    logged_in = bool(info.get("loggedIn"))
    return {"installed": True, "logged_in": logged_in, "auth_method": info.get("authMethod"),
            "help": None if logged_in else "Open a terminal, run `claude`, and sign in with /login."}


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
    if kind == "autofill" and not values["website"]:
        raise ValueError("Enter your website.")
    if kind == "custom" and not values["prompt"].strip():
        raise ValueError("Type what you want Leadsmith to do.")
    return title_t.format(**values), prompt_t.format(**values)


def _append(job_id: int, text: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET log = substr(log || ?, -?) WHERE id = ?", (text, MAX_LOG, job_id))


def _short(value: Any, limit: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe(event: dict[str, Any]) -> str:
    """Turn one stream-json event into a readable log line (or nothing)."""
    kind = event.get("type")
    indent = "    " if event.get("parent_tool_use_id") else ""
    if kind == "assistant":
        lines = []
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                lines.append(f"{indent}{block['text'].strip()}")
            elif block.get("type") == "tool_use":
                name = block.get("name", "").replace(TOOL_PREFIX, "")
                args = block.get("input") or {}
                if name in ("Agent", "Task"):
                    lines.append(f"{indent}▶ agent {args.get('subagent_type', '')}: {_short(args.get('description', ''))}")
                else:
                    detail = args.get("domain") or args.get("query") or args.get("url") or args.get("website") or args
                    lines.append(f"{indent}→ {name} {_short(detail)}")
        return "\n".join(lines) + "\n" if lines else ""
    if kind == "result":
        return ""
    return ""


def _run(job_id: int, prompt: str) -> None:
    path = claude_path()
    mcp_config = config.HOME / ".dashboard-mcp.json"
    mcp_config.write_text(json.dumps({"mcpServers": {"leadsmith": {
        "command": sys.executable, "args": ["-m", "leadsmith.cli", "serve"], "env": {"LEADSMITH_HOME": str(config.HOME)},
    }}}), encoding="utf-8")
    cmd = [path or "claude", "-p", "--plugin-dir", str(PLUGIN_ROOT), "--mcp-config", str(mcp_config),
           "--strict-mcp-config", "--output-format", "stream-json", "--verbose",
           "--permission-mode", "dontAsk", "--allowedTools", ",".join(ALLOWED_TOOLS),
           "--disallowedTools", ",".join(BLOCKED_TOOLS)]
    try:
        model = config.normalize_model(config.get("CLAUDE_MODEL"))
    except ValueError:
        model = ""  # an unusable value falls back to the Claude Code default instead of failing the run
    if model:
        cmd += ["--model", model]
    env = {**os.environ, "LEADSMITH_HOME": str(config.HOME)}
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (db.now(), job_id))
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", env=env, cwd=str(config.HOME))
    except OSError as exc:
        _finish(job_id, "failed", f"Could not start Claude Code: {exc}", None)
        return
    _PROCS[job_id] = proc
    assert proc.stdin and proc.stdout
    proc.stdin.write(f"{HEADLESS_NOTE}\n\n---\n\n{prompt}")
    proc.stdin.close()
    result_text, cost, failed = None, None, False
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            _append(job_id, line + "\n")
            continue
        if event.get("type") == "result":
            result_text = event.get("result")
            cost = event.get("total_cost_usd")
            failed = bool(event.get("is_error"))
        else:
            text = _describe(event)
            if text:
                _append(job_id, text)
    proc.wait()
    stderr = (proc.stderr.read() if proc.stderr else "").strip()
    _PROCS.pop(job_id, None)
    if _CANCELLED.pop(job_id, False):
        _finish(job_id, "cancelled", result_text or "Stopped by you.", cost)
    elif proc.returncode != 0 or failed:
        message = result_text or stderr or f"Claude Code exited with code {proc.returncode}"
        if "auth" in message.lower() or "login" in message.lower():
            message += "\nSign in: open a terminal, run `claude`, then /login."
        _finish(job_id, "failed", message, cost)
    else:
        _finish(job_id, "done", result_text, cost)


def _finish(job_id: int, status: str, result: str | None, cost: float | None) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = ?, result = ?, cost_usd = ?, finished_at = ? WHERE id = ?",
                     (status, result, cost, db.now(), job_id))


_PROCS: dict[int, subprocess.Popen] = {}
_CANCELLED: dict[int, bool] = {}


def start(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    if kind not in PROMPTS:
        raise ValueError(f"Unknown job kind {kind}")
    if not claude_path():
        raise ValueError("Claude Code isn't installed. Get it at https://claude.com/claude-code and sign in.")
    title, prompt = _fmt(kind, params)
    with db.connect() as conn:
        cur = conn.execute("INSERT INTO jobs (kind, title, prompt, created_at) VALUES (?, ?, ?, ?)",
                           (kind, title, prompt, db.now()))
        job_id = cur.lastrowid
    threading.Thread(target=_run, args=(job_id, prompt), daemon=True).start()
    return get(job_id) or {}


def cancel(job_id: int) -> bool:
    proc = _PROCS.get(job_id)
    if not proc:
        return False
    _CANCELLED[job_id] = True
    proc.terminate()
    return True


def get(job_id: int) -> dict[str, Any] | None:
    with db.connect() as conn:
        return db.one(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)))


def recent(limit: int = 30) -> list[dict[str, Any]]:
    with db.connect() as conn:
        return db.rows(conn.execute(
            "SELECT id, kind, title, status, cost_usd, started_at, finished_at, created_at,"
            " substr(result, 1, 400) AS result FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ))


def mark_orphans() -> None:
    """Jobs left 'running' by a previous dashboard process can't be resumed."""
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'failed', result = COALESCE(result, 'Dashboard was closed while running.')"
                     " WHERE status IN ('queued', 'running')")
