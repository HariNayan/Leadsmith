"""Leadsmith MCP server: the tools every agent shares.

Claude (the agents in this plugin) does the thinking; this server provides
data, memory and guard rails. No AI API key is needed: everything runs on
the user's own Claude Code plan.
"""

import csv
import logging
import re
from datetime import datetime
from typing import Any, Awaitable, Literal

from mcp.server.mcpserver import MCPServer

from . import compliance, config, db, lint, mailer, schedule, templates
from .brain import read_brain, write_brain
from . import decks
from .providers import emails, hn, news, website, yc
from .providers.base import ProviderError

INSTRUCTIONS = """Leadsmith finds buyers and drafts outreach for the user's business.
Always call brain_read first: it holds the user's company, ICP, personas and voice.
All data tools are free (no API keys): find_yc_companies, find_hn_hiring, search_news, scan_website, get_open_jobs, find_emails_on_page, github_profile, email_candidates. Look for a published email (find_emails_on_page, github_profile, search snippets) before guessing one with email_candidates. Combine them with your built-in WebSearch and WebFetch for anything else (lists, LinkedIn profiles via search, team pages).
Save everything you learn (save_account, save_signal, save_contact, save_draft) so other agents and future sessions reuse it, and call check_known before researching to avoid duplicates.
Leads whose domain ends in ".no-website" are businesses without a website (often local businesses using a Gmail address). Research them with WebSearch/WebFetch (Google Maps listings, Justdial, IndiaMART, Instagram, Facebook, LinkedIn, news) instead of scan_website, and keep any Gmail address they publish: that is their real contact email.
Never invent facts: every claim in outreach must come from a saved signal or the brain.
Nothing is sent without the user's explicit approval (review_drafts) and SEND_ENABLED=true."""

mcp = MCPServer("leadsmith", instructions=INSTRUCTIONS, version="0.2.0", log_level="WARNING")
logging.getLogger("httpx").setLevel(logging.WARNING)


NO_WEBSITE_HINT = {
    "reachable": False,
    "no_website": True,
    "note": "This business has no website. Research it with WebSearch/WebFetch: Google Maps listing, Justdial, "
            "IndiaMART, Instagram/Facebook page, LinkedIn, local news. Publicly listed Gmail addresses are fine to use.",
}


async def guard(work: Awaitable[Any]) -> Any:
    """Turn provider/validation failures into a readable result instead of a crash."""
    try:
        return await work
    except (ProviderError, ValueError, KeyError) as exc:
        return {"error": str(exc)}


# ── setup & brain ───────────────────────────────────────────────────────

@mcp.tool()
def leadsmith_status() -> dict:
    """Show setup status: Gmail/sending settings, brain completeness and pipeline counts."""
    brain = read_brain()
    return {
        "workspace": str(config.HOME),
        "dashboard": "Run `uv run leadsmith dashboard` in the plugin folder, then open http://127.0.0.1:4600",
        "data_tools": "all free, no API keys; plus WebSearch/WebFetch",
        "email": mailer.mail_status(),
        "brain_files_needing_input": [name for name, f in brain.items() if not f["complete"]],
        "pipeline": db.counts(),
    }


@mcp.tool()
def brain_read() -> dict:
    """Read the business profile: company & offer, ICP & buying signals, personas, voice rules. Call this before any prospecting or writing."""
    result: dict = {name: {"complete": f["complete"], "content": f["content"]} for name, f in read_brain().items()}
    feedback = db.lead_feedback()
    if feedback:
        result["lead_feedback"] = {
            "how_to_use": "The user's own verdicts on leads you found. Find more like the 'good' ones; avoid the patterns "
                          "behind the 'bad' ones and respect their reasons. These override your own fit scoring.",
            "verdicts": feedback,
        }
    style = (config.get("EMAIL_STYLE") or "idea_menu").strip()
    result["email_style"] = {
        "style": style,
        "how_to_use": ("Write every first email as a PERSONAL PITCH, following the 'Style: personal pitch' section of the "
                       "outreach-writer playbook: build the account's deck first (save_deck), then save step 1 with "
                       "attach_deck=true. This overrides any 'Offer ideas' line in voice.md."
                       if style == "personal_pitch" else
                       "Write first emails as an IDEA MENU (three ideas, A/B/C), following the outreach-writer playbook. "
                       "voice.md may still say 'Offer ideas: no' to use a classic sequence instead."),
    }
    custom = templates.customised()
    if custom:
        result["step_templates"] = {
            "how_to_use": "The user's own rules per sequence step. mode 'fixed': call render_step_template(contact_id, step) "
                          "and save that text (only fill the placeholders it lists). mode 'ai': write the step yourself, "
                          "and follow its 'instruction' on top of voice.md.",
            "steps": [{k: t[k] for k in ("step", "name", "mode", "subject", "body", "instruction")} for t in custom],
        }
    return result


@mcp.tool()
def brain_write(file: Literal["company", "icp", "personas", "voice"], content: str) -> dict:
    """Replace one business-profile file with new markdown content. Only use with content the user gave or confirmed.

    Args:
        file: which brain file to write
        content: the full new markdown content of that file
    """
    path = write_brain(file, content)
    return {"saved": str(path), "complete": read_brain()[file]["complete"]}


# ── Agent 1: signal discovery (all free) ────────────────────────────────

@mcp.tool()
async def find_yc_companies(
    query: str | None = None,
    industries: list[str] | None = None,
    regions: list[str] | None = None,
    hiring_only: bool = False,
    min_team: int | None = None,
    max_team: int | None = None,
    limit: int = 30,
) -> dict:
    """Search the Y Combinator startup directory (free): one-liner, team size, batch, industries, location, hiring badge.

    Args:
        query: keywords, e.g. "logistics", "developer tools", "sales"
        industries: keep companies whose industries/tags contain any of these, e.g. ["Fintech", "B2B"]
        regions: keep companies in these regions/cities, e.g. ["India", "London"]
        hiring_only: only companies with open roles (a strong growth signal)
        min_team: minimum team size
        max_team: maximum team size
        limit: max companies
    """
    async def run() -> dict:
        companies = await yc.search(query, industries, regions, hiring_only, min_team, max_team, limit)
        return {"companies": companies, "count": len(companies)}
    return await guard(run())


@mcp.tool()
async def find_hn_hiring(keywords: list[str] | None = None, months: int = 1, limit: int = 40) -> dict:
    """Companies posting in Hacker News "Who is hiring?" (free, monthly): company, roles, location, website and post text.

    Args:
        keywords: filter posts, e.g. ["sales", "growth", "fintech", "remote"]
        months: how many monthly threads to scan (1-3)
        limit: max posts
    """
    async def run() -> dict:
        posts = await hn.who_is_hiring(keywords, max(1, min(months, 3)), limit)
        return {"posts": posts, "count": len(posts)}
    return await guard(run())


@mcp.tool()
async def search_news(query: str, days_back: int = 30, limit: int = 15, funding_only: bool = False,
                      region: str = "US:en") -> dict:
    """Search recent news (free, Google News). Use for funding rounds, launches, expansions, leadership changes, competitor moves.

    Args:
        query: e.g. "Acme Corp" or "fintech India" or '"appointed" "VP of Sales"'
        days_back: how far back to look
        limit: max articles
        funding_only: restrict to funding announcements
        region: "COUNTRY:lang", e.g. "US:en", "IN:en", "GB:en"
    """
    async def run() -> dict:
        q = f"{query} {news.FUNDING_TERMS}" if funding_only else query
        return {"articles": await news.google_news(q, days_back, limit, region)}
    return await guard(run())


# ── Agent 3: account research (free) ────────────────────────────────────

@mcp.tool()
async def scan_website(domain: str) -> dict:
    """Free deep scan of a company's website: what they do, about/team/pricing text, tech stack, socials, public emails, content freshness, AI-crawler blocking and open jobs from their ATS."""
    if not db.has_website(db.normalize_domain(domain)):
        return NO_WEBSITE_HINT
    return await guard(website.scan(domain))


@mcp.tool()
async def get_open_jobs(domain: str) -> dict:
    """Free: a company's open roles from its public Greenhouse/Lever/Ashby job board, grouped by function, newest first."""
    if not db.has_website(db.normalize_domain(domain)):
        return NO_WEBSITE_HINT
    return await guard(website.open_jobs(domain))


# ── Agent 2: people (free) ──────────────────────────────────────────────

@mcp.tool()
async def email_candidates(domain: str, first_name: str, last_name: str | None = None,
                           known_emails: list[str] | None = None) -> dict:
    """Likely email addresses for a person (free): checks the domain accepts mail (MX) and infers the company's pattern from known public emails.
    Use after you found the person's real name. Save the top candidate with email_status "guessed".

    Args:
        domain: company domain
        first_name: person's first name
        last_name: person's last name
        known_emails: public emails at the company (from scan_website public_emails) to infer the pattern
    """
    if not db.has_website(db.normalize_domain(domain)):
        return {"candidates": [], "note": "This business has no website, so there is no company email pattern. "
                "Use the email it publishes (often a Gmail address) on Google Maps, Justdial, Instagram or its posts."}
    return await guard(emails.candidates(domain, first_name, last_name, known_emails))


@mcp.tool()
async def find_emails_on_page(url: str, first_name: str | None = None, last_name: str | None = None) -> dict:
    """Read the email addresses published on a page (plain, mailto and disguised like "jane [at] acme [dot] com").
    Use on a person's website or contact page, YouTube About page, newsletter, talk/podcast page or a company page.
    Each result says whether it matches the person's name. LinkedIn profiles aren't fetched: their emails are
    only visible to logged-in connections.

    Args:
        url: page to read
        first_name: the person you're looking for, to flag matching addresses
        last_name: their last name
    """
    return await guard(emails.emails_on_page(url, first_name, last_name))


@mcp.tool()
async def github_profile(username_or_url: str) -> dict:
    """A GitHub user's public profile: email (if they made it public), website, X handle, company and bio. Free."""
    return await guard(emails.github_email(username_or_url))


# ── shared memory: the pipeline ─────────────────────────────────────────

@mcp.tool()
def source_stats() -> dict:
    """Which discovery sources ("plays") have produced leads the user liked and that replied. Check it before choosing
    plays: favour sources with replies or 'good' verdicts; a source with several leads and no good verdicts or replies
    deserves less of your effort."""
    return {"sources": db.source_stats()}


@mcp.tool()
def check_known(domains: list[str]) -> dict:
    """Before researching, check which domains are already in the pipeline or on the do-not-contact list."""
    return db.known(domains)


@mcp.tool()
def save_account(
    domain: str,
    name: str | None = None,
    industry: str | None = None,
    employees: int | None = None,
    location: str | None = None,
    timezone: str | None = None,
    linkedin_url: str | None = None,
    summary: str | None = None,
    fit_score: int | None = None,
    intent_score: int | None = None,
    why_now: str | None = None,
    angle: str | None = None,
    research: str | None = None,
    status: Literal["new", "researched", "ready", "contacted", "replied", "meeting", "won", "lost", "disqualified"] | None = None,
    source: str | None = None,
) -> dict:
    """Create or update a target account. Only the fields you pass are changed.

    Args:
        domain: company domain (the key)
        fit_score: 0-10, how well they match the ICP
        intent_score: 0-10, how strong and recent the buying signals are
        why_now: one sentence: the timely reason to reach out, citing signals
        angle: the outreach angle (pain + proof) the writer should use
        research: markdown research brief (from the account researcher)
        status: pipeline stage
        source: where the account came from, e.g. "hn-hiring" or "yc"
        location: city and country of the people you'll email, e.g. "San Francisco, US"
        timezone: their IANA time zone if you know it, e.g. "America/Los_Angeles" (else guessed from location); emails only go out in their business hours
    """
    account, created = db.upsert_account(
        domain, name=name, industry=industry, employees=employees, location=location,
        timezone=schedule.valid_zone(timezone) or schedule.guess_timezone(location), linkedin_url=linkedin_url,
        summary=summary, fit_score=fit_score, intent_score=intent_score, why_now=why_now, angle=angle,
        research=research, status=status, source=source,
    )
    account.pop("research", None)
    return {"created": created, "account": account}


@mcp.tool()
def save_signal(domain: str, type: str, title: str, detail: str | None = None, url: str | None = None,
                source: str | None = None, observed_at: str | None = None) -> dict:
    """Record one buying signal as evidence (deduplicated). Writers may only cite saved signals.

    Args:
        domain: company domain
        type: hiring, funding, leadership_change, launch, expansion, tech_change, competitor, news, website, intent, other
        title: short fact, e.g. "Hiring 3 SDRs"
        detail: supporting detail or quote
        url: source link
        source: provider, e.g. "greenhouse", "google-news", "apollo"
        observed_at: date of the event (YYYY-MM-DD)
    """
    created = db.add_signal(domain, type, title, detail, url, source, observed_at)
    return {"saved": created, "duplicate": not created}


@mcp.tool()
def save_contact(domain: str, first_name: str, last_name: str | None = None, title: str | None = None,
                 email: str | None = None, email_status: str | None = None, linkedin_url: str | None = None,
                 persona: str | None = None, notes: str | None = None, source: str | None = None) -> dict:
    """Create or update a person at an account (deduplicated by email, then name). Returns the contact id used for drafts."""
    if email and db.is_suppressed(email):
        return {"error": f"{email} is on the do-not-contact list."}
    contact, created = db.upsert_contact(
        domain, first_name=first_name, last_name=last_name, title=title, email=email, email_status=email_status,
        linkedin_url=linkedin_url, persona=persona, notes=notes, source=source,
    )
    return {"created": created, "contact": contact}


@mcp.tool()
def lint_email(body: str, subject: str | None = None, step: int = 1, company: str | None = None,
               first_name: str | None = None) -> dict:
    """Check cold-email copy for length, AI-sounding phrases, spam triggers, links, placeholders and subject problems."""
    return lint.check(subject, body, step, company, first_name)


@mcp.tool()
def render_step_template(contact_id: int, step: int) -> dict:
    """Fill the user's fixed template for one sequence step with this contact's details.

    Returns mode 'ai' (with any extra instruction) when the step has no fixed template: write it yourself then.
    For mode 'fixed', save the returned subject/body with save_draft as-is, except: replace each {placeholder} named in
    `fill_these` with one short sentence backed by research, and if `empty` lists fields with no value, smooth the
    sentence around them. Don't rewrite the user's wording.
    """
    return templates.render(contact_id, step)


@mcp.tool()
def save_draft(contact_id: int, step: int, body: str, subject: str | None = None,
               channel: Literal["email", "linkedin"] = "email", send_after_days: int | None = None,
               attach_deck: bool = False) -> dict:
    """Save one step of a sequence for a contact as a draft for the user to review. Runs lint_email automatically.

    Args:
        contact_id: from save_contact
        step: 1 = first email, 2 and 3 = follow-ups
        body: plain-text body without signature (signature and opt-out line are added at send time)
        subject: required for step 1; follow-ups reuse it as "Re:" in the same thread
        channel: email or linkedin (linkedin drafts are for the user to send by hand)
        send_after_days: days after step 1 to send (default 0 / 3 / 7)
        attach_deck: attach the account's PDF deck (save_deck first; it must have rendered as a PDF). Only then may
                     the body say a deck is attached.
    """
    contact = db.get_contact(contact_id)
    if not contact:
        return {"error": f"No contact with id {contact_id}. Call save_contact first."}
    if attach_deck and not decks.pdf_ready(contact["domain"]):
        return {"error": "There's no PDF deck for this account to attach. Call save_deck first (and check it rendered "
                         "as a PDF), or save the email without attach_deck and without mentioning an attachment."}
    account = db.dossier(contact["domain"]) or {}
    result = lint.check(subject, body, step, account.get("name"), contact.get("first_name"), attached=bool(attach_deck))
    delay = send_after_days if send_after_days is not None else {1: 0, 2: 3}.get(step, 7)
    try:
        draft = db.upsert_draft(contact_id, step, subject, body, channel, delay, result)
    except ValueError as exc:
        return {"error": str(exc)}
    with db.connect() as conn:
        conn.execute("UPDATE drafts SET attach_deck = ? WHERE id = ?", (int(bool(attach_deck)), draft["id"]))
    return {"draft_id": draft["id"], "status": draft["status"], "lint": result,
            "hint": "Fix every FAIL and re-save before showing it to the user." if not result["ok"] else None}


@mcp.tool()
def save_reply_draft(contact_id: int, body: str) -> dict:
    """Save your answer to a prospect who replied, for the user to review. It goes in the same thread as email 1.
    Answer what they asked, move to one concrete next step, and never invent prices, dates or results that
    aren't in the brain. Plain text, greet by first name, no signature (added at send time).

    Args:
        contact_id: the person who replied
        body: the reply text
    """
    contact = db.get_contact(contact_id)
    if not contact:
        return {"error": f"No contact {contact_id}"}
    account = db.dossier(contact["domain"]) or {}
    result = lint.check(None, body, 2, account.get("name"), contact.get("first_name"), kind="reply")
    draft = db.save_reply_draft(contact_id, body, result)
    return {"draft_id": draft["id"], "lint": result,
            "hint": "Fix every FAIL and save again." if not result["ok"] else "Saved for review."}


@mcp.tool()
def save_ideas(domain: str, ideas: list[dict]) -> dict:
    """Save the 3 tailored ideas the prospect will pick from (lettered A, B, C in order). Replaces earlier offered
    ideas for the account; an idea the prospect already picked is kept. Write them before the emails.

    Args:
        domain: account domain
        ideas: 3 items, each {"title": str (≤8 words, concrete), "pitch": str (one sentence: what you'd make/do for them),
               "why": str (the signal or research fact it answers, with source), "format": str (e.g. "45-sec homepage video")}
    """
    if not ideas:
        return {"error": "ideas is empty"}
    saved = db.save_ideas(domain, ideas)
    return {"ideas": saved, "hint": "Offer these in email 1 as 'A) … B) … C) …' and ask which one they'd like first."}


@mcp.tool()
def save_deck(domain: str, title: str, slides: list[dict]) -> dict:
    """Create a personalised PDF deck for an account (rendered with the local Chrome/Edge). The user can attach it to an email in the dashboard.

    Args:
        domain: account domain
        title: deck title, e.g. "Acme × YourCo: filling the Q4 pipeline"
        slides: 4 slides, each {"heading": str, "body": str (1-2 sentences), "bullets": [str] (max 4), "highlight": str (optional big stat/quote), "kicker": str (optional small label)}
    """
    if not slides:
        return {"error": "slides is empty"}
    return decks.build(domain, title, slides)


@mcp.tool()
def list_pipeline(status: str | None = None, min_score: int | None = None, limit: int = 50) -> dict:
    """List accounts ranked by fit + intent, with signal, contact and pending-draft counts."""
    return {"accounts": db.pipeline(status, min_score, limit), "totals": db.counts()}


@mcp.tool()
def get_account(domain: str) -> dict:
    """Everything known about one account: scores, research, signals, contacts, drafts and history."""
    return db.dossier(domain) or {"error": f"{domain} is not in the pipeline."}


@mcp.tool()
def list_drafts(status: Literal["draft", "approved", "sent", "rejected", "cancelled"] | None = "draft",
                limit: int = 100) -> dict:
    """List drafts with contact and lint results, e.g. to show the user what's waiting for review."""
    return {"drafts": db.drafts(status, limit)}


@mcp.tool()
def review_drafts(draft_ids: list[int], action: Literal["approve", "reject"]) -> dict:
    """Approve or reject drafts. ONLY call with action="approve" after the user explicitly approved these exact drafts in this conversation. Drafts with lint FAILs can't be approved."""
    done, refused = [], []
    for draft in db.drafts(None, 10_000):
        if draft["id"] not in draft_ids:
            continue
        if action == "approve" and draft["lint"] and not draft["lint"].get("ok", True):
            refused.append({"draft_id": draft["id"], "reason": "lint FAILs; rewrite and save_draft again"})
            continue
        if draft["status"] not in ("draft", "approved", "rejected"):
            refused.append({"draft_id": draft["id"], "reason": f"status is {draft['status']}"})
            continue
        db.set_status("draft", draft["id"], "approved" if action == "approve" else "rejected")
        done.append(draft["id"])
    return {action + "d": done, "refused": refused}


@mcp.tool()
def update_status(kind: Literal["account", "contact"], id: str, status: str, note: str | None = None) -> dict:
    """Move an account (id = domain) or contact (id = contact id) to a new stage, e.g. meeting, won, lost, disqualified."""
    changed = db.set_status(kind, id if kind == "account" else int(id), status)
    if changed:
        db.log_event(f"status:{status}", note, contact_id=int(id) if kind == "contact" else None,
                     domain=id if kind == "account" else None)
    return {"updated": bool(changed)}


@mcp.tool()
def suppress(value: str, reason: str | None = None) -> dict:
    """Add an email or a whole domain to the do-not-contact list and cancel its pending drafts (use for opt-outs, customers, competitors)."""
    return {"suppressed": db.suppress(value, reason)}


IMPORT_COLUMNS = ["email", "first_name", "last_name", "company", "title", "website", "linkedin_url", "reason"]
"""Instantly and Smartlead recognise these names as standard fields, so the file imports without column mapping.
Every other column (ideas, steps, timezone) arrives as a custom variable you can merge into a template."""


@mcp.tool()
def export_csv(kind: Literal["sequences", "contacts", "accounts"] = "sequences",
               status: str | None = None) -> dict:
    """Export to CSV shaped for Instantly / Smartlead / Lemlist / HubSpot imports.

    Args:
        kind: sequences (one row per person: import columns, reason, ideas, then each step's subject/body),
              contacts (people with the import columns), or accounts (companies with scores)
        status: filter (draft status for sequences, e.g. "approved"; account status for accounts)
    """
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.EXPORT_DIR / f"{kind}-{stamp}.csv"
    with db.connect() as conn:
        accounts = {r["domain"]: r for r in db.rows(conn.execute("SELECT * FROM accounts"))}
        ideas: dict[str, list[dict[str, Any]]] = {}
        for idea in db.rows(conn.execute("SELECT domain, letter, title, pitch, status FROM ideas ORDER BY letter")):
            ideas.setdefault(idea["domain"], []).append(idea)
        contacts = {r["id"]: r for r in db.rows(conn.execute("SELECT * FROM contacts"))}

    def person_row(contact: dict[str, Any]) -> dict[str, Any]:
        account = accounts.get(contact["domain"]) or {}
        row = {
            "email": contact.get("email") or "", "first_name": contact.get("first_name") or "",
            "last_name": contact.get("last_name") or "", "company": account.get("name") or contact["domain"],
            "title": contact.get("title") or "",
            "website": f"https://{contact['domain']}" if db.has_website(contact["domain"]) else "",
            "linkedin_url": contact.get("linkedin_url") or "", "reason": account.get("why_now") or "",
        }
        for idea in ideas.get(contact["domain"], [])[:3]:
            row[f"idea_{idea['letter'].lower()}"] = f"{idea['title']}: {idea['pitch'] or ''}".strip(": ")
            if idea["status"] == "picked":
                row["picked_idea"] = f"{idea['letter']}) {idea['title']}"
        flag = compliance.region(account or {"domain": contact["domain"]}, contact.get("email"))
        row.update(email_status=contact.get("email_status") or "", timezone=account.get("timezone") or "",
                   domain=contact["domain"], strict_region=flag["code"] if flag else "")
        return row

    if kind == "sequences":
        by_contact: dict[int, dict[str, Any]] = {}
        for d in db.drafts(status, 100_000):
            contact = contacts.get(d["contact_id"])
            if d["channel"] != "email" or d.get("kind") == "reply" or not contact or not contact.get("email"):
                continue
            if contact["status"] in ("unsubscribed", "bounced") or db.is_suppressed(contact["email"]):
                continue
            row = by_contact.setdefault(d["contact_id"], person_row(contact))
            if d["step"] == 1:
                row["step1_subject"] = d["subject"] or ""
            row[f"step{d['step']}_body"] = d["body"]
        records = list(by_contact.values())
    elif kind == "contacts":
        records = [person_row(c) | {"status": c["status"]} for c in contacts.values()
                   if c.get("email") and not db.is_suppressed(c["email"])]
    else:
        records = db.pipeline(status, None, 100_000)
    if not records:
        return {"exported": 0, "note": "Nothing matched."}
    seen = {k for r in records for k in r}
    first = [c for c in IMPORT_COLUMNS if c in seen] if kind != "accounts" else []
    ideas_cols = sorted(k for k in seen if k.startswith("idea_")) + (["picked_idea"] if "picked_idea" in seen else [])
    steps = sorted((k for k in seen if k.startswith("step")), key=lambda k: (int(re.search(r"\d+", k).group()), k != "step1_subject"))
    rest = sorted(seen - set(first) - set(ideas_cols) - set(steps))
    fields = first + ideas_cols + steps + rest
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    return {"exported": len(records), "path": str(path), "columns": fields}


# ── sending ─────────────────────────────────────────────────────────────

@mcp.tool()
def send_approved_emails(limit: int = 10, dry_run: bool = True) -> dict:
    """Send approved drafts that are due, through the user's Gmail. Defaults to a dry run that previews exactly what would go out.
    Only call with dry_run=false after the user explicitly says to send. Requires SEND_ENABLED=true; respects DAILY_SEND_LIMIT."""
    return mailer.send_approved(limit, dry_run)


@mcp.tool()
def check_replies(days: int = 14) -> dict:
    """Check Gmail for replies from contacted people; marks them replied and cancels their pending follow-ups."""
    return mailer.check_replies(days)


def main() -> None:
    config.load_env()
    config.ensure_home()
    mcp.run()


if __name__ == "__main__":
    main()
