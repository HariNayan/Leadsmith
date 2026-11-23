# AGENTS.md - Leadsmith codebase guide for AI agents

Read this fully before changing anything. It tells you what the project is, how it is laid out, the rules the owner
has set, and the traps that have already caught people. If something here disagrees with the code, the code wins:
verify, then fix this file.

Last verified against the code: 2026-11-24 (version 0.2.0, 166 tests passing).

---

## 1. What Leadsmith is

A **local, free-sources outreach app**. The user describes who they want to sell to; Leadsmith runs the user's *own*
coding agent (Claude Code, Codex, OpenCode or Gemini CLI) headless to find leads, research them, build a personal PDF
deck, and write a 3-step email sequence. A human reviews and approves in a local dashboard; Leadsmith sends from the
user's **own Gmail** (app password) and detects replies and bounces over IMAP.

- Repo: github.com/HariNayan/Leadsmith, branch `main`. Python >= 3.10, MIT.
- Dashboard: `http://127.0.0.1:4600`. Start: `Start Leadsmith.cmd` (Windows) or `uv run leadsmith`.
- Data lives **outside the repo** in `~/.leadsmith` (override with `LEADSMITH_HOME`): `leadsmith.db` (SQLite, WAL),
  `.env` (settings and secrets), `brain/*.md` (business profile), `decks/`, `exports/`, `portfolio/`.
  Old `~/.gtm-brain` / `GTM_HOME` are migrated automatically.
- It is also a Claude Code **plugin** (`.claude-plugin/`, `agents/`, `skills/`) exposing an MCP server (`leadsmith serve`).
- Product was renamed from "GTM Brain" on 2026-10-25. **Never reintroduce "GTM" wording.**

## 2. Owner's rules (do not break these)

1. **Free and local-first.** No paid data providers (Apollo, Exa, TheirStack, Hunter were explicitly dropped). The only
   paid thing is the optional BetterContact "Verify" button, triggered by a human click; agents may never call it.
   No AI API key is required; the engine is the user's own CLI.
2. **Approving and sending are human-only.** Agents can write drafts, never approve or send. Keep it that way.
3. **Every first email has the deck attached.** Never drop decks to save cost, and never A/B test "offer the deck".
   Look for savings elsewhere.
4. **Keep the high daily sending limit.** Do not lower it. Manage risk through bounce / spam checks instead.
5. **Nothing is tuned to one niche.** Many different companies will use Leadsmith. Proof of need, the fit rubric and
   source choice come from each workspace's own profile; learning uses only that workspace's own replies; examples in
   agent instructions stay industry-neutral.
6. **Must work for people without a business email**: personal `@gmail.com` senders, and leads with no website
   (stored under a `<slug>.no-website` key).
7. **The user answers interested leads personally.** Do not build reply-answer kits (price or portfolio replies).
8. **Cost choices:** one agent per run by default (`USE_SUBAGENTS=false`); Find buyers only finds and verifies, the
   user picks leads and clicks Write emails; decks run on `DECK_ENGINE` (OpenCode by default, falls back to the main
   engine).
9. **UI is judged harshly.** Follow Attio / CRM conventions, light and dark theme. No generic "AI-looking" design.
   **Check UI changes visually before reporting them done.**
10. **"Don't add anything" means exactly that:** make only the change asked for.

## 3. Repository layout

```
src/leadsmith/
  cli.py          entry: dashboard | serve | init | doctor
  config.py       paths (HOME, DB_PATH...), SETTINGS table, load_env / save_env, normalize_model
  db.py           SQLite schema, migrations, ALL data access (accounts, signals, contacts, drafts, ideas...)
  dashboard.py    Starlette app: REST API (/api/*) + LocalOnly guard + scheduler loop. Humans approve/send here.
  server.py       FastMCP server: the tools agents call. apply_agent_mode() removes tools per run.
  jobs.py         Agent job runner: prompts (PROMPTS), run chain, subprocess streaming, top-up rounds
  engines.py      Adapters for Claude / Codex / OpenCode / Gemini: command line, sandbox flags, output parsing
  mailer.py       Gmail SMTP send, IMAP reply + bounce detection, daily limit, warm-up, bounce guard
  lint.py         Mechanical email checks (FAIL / WARN) run on every draft
  brain.py        Business-profile files, offers, profile questions, "enhance my request"
  templates.py    User's fixed email templates: {placeholders}, [First Name] normalisation, render()
  decks.py        Personal PDF decks: slide normalising, HTML render, headless Chrome/Edge -> PDF
  schedule.py     Prospect time zone guess, sending window, business-day maths
  compliance.py   Region flag (EU/UK/CH/CA) - a prompt to check, not a block
  providers/      Free data sources (see 6). base.py = shared HTTP client + cache
  templates/      brain/*.md and env.example copied into ~/.leadsmith on first run
  web/            index.html, app.js (vanilla JS, ~1900 lines, no framework/build), app.css
agents/           4 Claude Code sub-agent playbooks (signal-scout, people-finder, account-researcher, outreach-writer)
skills/           4 Claude Code skills (setup, find-buyers, outreach, dashboard)
tests/            test_core.py, test_quality.py (unittest, offline)
.claude-plugin/   plugin.json, marketplace.json
.github/workflows/test.yml   CI: unittest on ubuntu, windows, macos
README.md  COMPLIANCE.md  NOTICE.md  LICENSE  uv.lock  pyproject.toml
```

Dependencies are deliberately few: `mcp>=2.3,<3`, `httpx`, `starlette`, `uvicorn`, `tzdata`. Do not add one without
a strong reason. There is no frontend build step: edit `web/app.js` / `app.css` directly.

## 4. How the pieces fit

```
Browser UI (web/app.js)  --REST-->  dashboard.py  --->  db.py (SQLite)
                                        |  jobs.start()
                                        v
                               jobs.py  --subprocess-->  engine CLI (claude / codex / opencode / gemini)
                                                              |  MCP over stdio
                                                              v
                                                 server.py (`leadsmith serve`)  --->  db.py, providers/
Human approves in UI  --->  mailer.send_approved()  --SMTP-->  Gmail     mailer.check_replies() <--IMAP-- Gmail
```

- **Two processes share one DB.** The dashboard process and one MCP server process per agent run. SQLite with WAL;
  `db.connect()` opens a short-lived connection per call and commits on exit.
- **Job kinds** (`jobs.PROMPTS`): `find_buyers`, `find_more`, `research`, `write`, `emails`, `ideas`, `ideas_enhance`,
  `reply`, `rewrite`, `deck`, `autofill`, `profile`, `custom`. `jobs.start()` builds a **chain** of jobs (find ->
  top-up rounds -> deck -> write) run one after another in a daemon thread. Optional stages (decks, top-ups) do not
  block later ones. If the deck engine fails, the main engine rebuilds the decks.
- **Agent run = one headless CLI process** fed the prompt on stdin; output is parsed per engine into a log; a stall
  limit (15 min no output) and exit-grace logic kill hung CLIs.
- **Scheduler loop** (`dashboard._scheduler_loop`): every 30 min while the dashboard is open, checks replies and sends
  due **follow-ups only** (steps > 1, already approved by a human), never later than 2 days overdue. Waits 5 min after
  start so nothing goes out unseen.

## 5. Data model (db.py)

Tables: `accounts` (PK `domain`), `signals`, `contacts`, `drafts`, `events`, `suppression`, `decks`, `jobs`, `ideas`,
`step_templates`, `cache`, `rejected`. Migrations are an additive `MIGRATIONS` dict (column -> `ALTER TABLE`), run
once per process in `_migrate()`. **Only add columns; never rename or drop.** Tests and real users have old DBs.

- Account statuses: new, researched, ready, contacted, replied, meeting, won, lost, disqualified.
- Contact statuses: new, drafted, contacted, replied, meeting, bounced, unsubscribed, disqualified.
- Draft statuses: draft, approved, sent, rejected, cancelled, plus the transient `sending` (set by `mailer._claim`;
  not in `DRAFT_STATUSES`). Draft `kind` is `sequence` or `reply`; `channel` is `email` or `linkedin`.
  `UNIQUE(contact_id, step, channel)`; `step` 1 = first email, 2 and 3 = follow-ups.
- Email trust labels on `contacts.email_status`: `published`, `public`, `manual`, `imported`, `guessed`, `invalid`...
  Guessed addresses are held unless `ALLOW_GUESSED_EMAILS`. Recruiting inboxes (hiring@, careers@) are never emailed.
- `events` is the audit log AND the source for daily limits and bounce rate (`sent`, `bounce`, `reply`, `opt_out`...).
- Domain keys: always pass user or agent input through `db.normalize_domain()`; no-website leads use
  `db.no_website_key(name)`. Deduplication (`find_duplicate`, `known`) matches by site, subdomain, name, LinkedIn.

## 6. Providers (all free, no keys) - `src/leadsmith/providers/`

`base` (HTTP + cache), `website` (homepage scan, ATS job boards), `techdetect`, `emails` (pattern + MX), `news`
(Google News RSS), `yc`, `hn` (Who is hiring), `startups` (Wellfound, BetaList, Product Hunt), `youtube`, `places`
(OpenStreetMap / site addresses), `registries` + `companies_house` (UK, France, GLEIF), `bettercontact` (paid, human
click only). Each is async; MCP tool wrappers live in `server.py`. Respect rate limits (Nominatim: 1 req/s) and cache.

## 7. MCP tools agents can call (server.py)

Discovery: `find_yc_companies`, `find_startups`, `find_hn_hiring`, `search_news`, `scan_website`, `get_open_jobs`,
`email_candidates`, `find_emails_on_page`, `find_youtube_creators`, `youtube_channel`, `youtube_video_frames`,
`find_business_address`, `geocode`, `company_registry_lookup`, `github_profile`.
Saving: `save_account`, `save_signal`, `save_contact`, `save_draft`, `save_reply_draft`, `save_ideas`, `save_deck`,
`save_rejected`, `suppress`, `update_status`. Reading and checks: `leadsmith_status`, `brain_read`, `list_pipeline`,
`get_account`, `list_drafts`, `check_known`, `quality_check`, `source_stats`, `lint_email`, `render_step_template`,
`check_replies`, `export_csv`. Profile: `brain_propose`, `brain_write`.
**Human-only (removed in agent runs via `LEADSMITH_AGENT_RUN=1`):** `review_drafts`, `send_approved_emails`.
Deck runs get an even smaller set (`SCOPES` in `server.py`). When adding a tool that can approve, send, spend money or
overwrite user data, decide deliberately whether agents may have it and update `AGENT_BLOCKED` / `SCOPES`.

## 8. Safety invariants (tests enforce many of these)

- Only drafts with status `approved` are sent, one claim at a time (`_claim`, `_SEND_LOCK`).
- Sending needs Gmail connected, `SEND_ENABLED=true`, a passing reply check first, no bounce pause, and daily budget
  left (`DAILY_SEND_LIMIT` + warm-up). `TEST_RECIPIENT` redirects every email to the tester and tags `[TEST]`.
- Skipped automatically: replied / bounced / unsubscribed / disqualified contacts, suppressed emails or domains,
  invalid, guessed (unless allowed), recruiting inboxes, outside the prospect's sending hours.
- **A first email carries its deck.** Approving a step-1 email auto-ticks `attach_deck` when a PDF deck exists
  (`dashboard._review`), and `mailer.send_approved` holds any approved step-1 email whose deck exists but isn't
  attached, instead of sending it bare. Reason: drafts written before the deck was built kept `attach_deck=0`.
- Bounce guard: pauses sending when more than 2% of recent emails bounce (min 3 bounces) until the human resumes.
- Opt-out line plus `List-Unsubscribe` header on every email. Out-of-office and ticket auto-replies do not stop
  follow-ups; real replies, "no" and "unsubscribe" do.
- **Dashboard security (`dashboard.LocalOnly`):** only `127.0.0.1:<port>` / `localhost:<port>` Host headers; every
  non-GET request must carry header `X-Leadsmith-Client: 1`. Keep both when adding routes. Secrets are never sent to
  the browser (`config.public_settings` returns only `is_set`).
- **Frontend escaping:** wrap every dynamic value in `esc()` before putting it in `innerHTML`. `md()` already escapes.
  Lead data comes from scraped web pages and must be treated as hostile.
- Lead quality: leads need sourced proof of need; the agent prompt (`HEADLESS_NOTE`) says the KIND of lead the user's
  brief asks for is a hard requirement. Don't weaken this.

## 9. Engines (engines.py) - sandbox rules per CLI

| Engine | Key flags / config | Notes |
|---|---|---|
| Claude Code | `-p`, `--permission-mode dontAsk`, `--allowedTools`/`--disallowedTools`, `--strict-mcp-config` | Bash/Edit/Write blocked; plugin loaded via `--plugin-dir` |
| Codex | `exec --json`, `sandbox_mode="read-only"`, MCP tool approval `approve` | web_search live (disabled for deck runs) |
| OpenCode | v2: `--standalone` + `opencode.json` in the run folder; v1: `OPENCODE_CONFIG_CONTENT` | v2 ignores per-run config; MCP tools only via `execute` as `tools.leadsmith.X`; shell tool is `shell`; runs in an empty folder so `.env` is out of reach; `opencode mcp list` always says none (debug with `--print-logs`) |
| Gemini CLI | `--approval-mode yolo` + settings.json `tools.exclude` (shell, write_file, replace) | read tools remain available |

Free OpenCode models share one account-wide usage limit. Deck model chosen by the user: `opencode/mimo-v2.6-flash-free`.
Adding an engine: subclass `Engine`, implement `command()`, `parse()`, `session_of()`, register in `ENGINES`, and keep
the "no approve/send, no shell, no file write" guarantees.

## 10. Settings (config.SETTINGS -> `~/.leadsmith/.env`)

Gmail: `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `SENDER_NAME`, `SENDER_SIGNATURE`, `POSTAL_ADDRESS`, `OPT_OUT_LINE`.
Sending: `SEND_ENABLED`, `DAILY_SEND_LIMIT` (code default 25), `WARMUP` / `WARMUP_START` / `WARMUP_STEP`,
`SEND_GAP_SECONDS`, `SEND_HOURS` ("9-17" in the prospect's time), `SEND_WEEKDAYS_ONLY`, `PROSPECT_TIMEZONE`,
`AUTO_FOLLOWUPS`, `ALLOW_GUESSED_EMAILS`, `TEST_RECIPIENT`. Runtime-written: `SEND_PAUSED`, `SEND_RESUMED_AT`.
Writing: `EMAIL_STYLE` (`idea_menu` | `personal_pitch`), `EMAIL_ASK` (`interest` | `call` | `sample`).
Engine: `AGENT_ENGINE`, `AGENT_MODEL`, `USE_SUBAGENTS`, `DECK_ENGINE`, `DECK_MODEL`, `DECK_COLOR`,
`BETTERCONTACT_API_KEY`. Real environment variables win over `.env`. To add a setting: add to `SETTINGS`, add to the
settings page in `app.js` (the `field("KEY")` list), and add validation in `dashboard.put_settings` if needed.

## 11. Business profile ("brain")

Four markdown files in `~/.leadsmith/brain/`: `company.md`, `icp.md`, `personas.md`, `voice.md` (templates in
`src/leadsmith/templates/brain/`). Unfilled `TODO` lines count as gaps. Agents read them via `brain_read`; they may
only *propose* changes (`brain_propose`, human accepts) except the autofill job. The profile's "Offers" section drives
what email 1 pitches; `lint` / `save_draft` fail an email that does not pitch the lead offer.

## 12. Emails, templates, decks

- Two styles: `idea_menu` (3 ideas A/B/C the prospect answers with a letter) and `personal_pitch` (the gap noticed, the
  exact offer, a deck made for them, one chosen ask).
- `lint.check()` runs on every save: word limits per step, spam triggers, AI-sounding phrases, unfilled placeholders,
  nameless greetings, two questions, copied sentences, false "attached" claims. FAIL blocks approval.
- Fixed templates (`templates.py`): placeholders `{first_name}`, `{company}`, `{why_now}`, `{ideas}`... are filled from
  the lead; unknown ones (`{personal_line}`) are filled by the writer agent. `[First Name]`, `{{first_name}}` and
  `{First Name}` are normalised to `{first_name}` style.
- Decks (`decks.py`): 3-6 slides, 7 layouts (cover, insight, concept, sample, plan, proof, next), max 90 words per
  slide, rendered by the machine's Chrome/Edge to PDF. Blank or placeholder slides are refused. The PDF is attached to
  the email at send time.

## 13. Frontend (web/)

Single-page, hash routes: `#/home`, `#/business`, `#/leads`, `#/review`, `#/outreach`, `#/activity`, `#/settings`.
Plain JS with template literals and one `render()` per page. Every API call sends `X-Leadsmith-Client: 1`. `index()` in
`dashboard.py` appends `?v=<mtime>` to assets, so no manual cache busting. If a page is newer than the running server
the UI says to restart Leadsmith. **Restart the dashboard after any Python change**; it does not hot-reload.

## 14. Commands

```powershell
uv sync                                         # stop the dashboard first (Windows file locks)
uv run leadsmith                                # dashboard (also: --port 4700, --no-open)
uv run leadsmith doctor                         # check engines, free sources, Chrome, Gmail, profile
uv run leadsmith init                           # create ~/.leadsmith from templates
uv run python -m unittest discover -s tests -v  # full suite, offline, ~75 s
uvx ruff check src tests --no-cache             # lint (ruff is not a project dependency)
```

The user's shell is **PowerShell 5.1**: no `&&` in commands you hand them (use `;` or separate lines).
Git for Windows is at `C:\Program Files\Git\cmd\git.exe` and is not on PATH in some shells; call it by full path.

## 15. Testing rules

- `tests/` are offline `unittest` files. They set `LEADSMITH_HOME` to a temp folder **before importing leadsmith** -
  keep that first, or tests will touch the real `~/.leadsmith`.
- Network, SMTP and IMAP are faked (`smtplib.SMTP_SSL`, `mailer.check_replies` are monkey-patched). Never write a test
  that sends real email or calls a live site.
- Add a test with every behaviour change; the suite is the safety net for sending logic.
- `jobs._brief_of()` parses the find prompt with the regex `Find (\d+) new accounts`. If you reword that prompt in
  `PROMPTS["find_buyers"]`, update the regex too (top-up rounds depend on it).

## 16. Git conventions

- One commit per area, subject in the repo's history style: `Area: what changed` (for example `Sending: ...`,
  `Decks: ...`, `Emails: ...`). Use the repo's configured git identity. **No co-author trailers and no AI / Claude
  names in commits.** Commit only when the user asks.
- Never commit `.env`, `*.db`, exports, or anything under `~/.leadsmith`.

## 17. Known issues (found in review 2026-11-24, not yet fixed unless noted in git log)

1. Bounce guard (`mailer.bounce_health`) counts only since the last Resume click, so it can hide a real 14-day rate
   above 2%. Should always use the true trailing window.
2. Rare duplicate send: if SMTP accepts the message but the call times out (or the DB write fails and the app restarts),
   `_release` / startup recovery returns the draft to `approved` and it can send again. Check Gmail Sent first.
3. Closing the dashboard does not kill running agent processes (`jobs._PROCS` has no shutdown hook; Windows children
   use `CREATE_NO_WINDOW`).
4. `brain_write` is available in all unattended agent runs; only the autofill job needs it.
5. Codex and Gemini run with their working directory next to `.env`.
6. `dashboard.contact_apply_templates` updates drafts by id without a status guard.
7. `templates.normalize_placeholders` turns literal `[1]` / `[link]` into fields.
8. Bounce detection cannot tell delay notices from failures; reply detection counts mail from before the first send.
9. `jobs.found_by()` takes every account created since the search began (includes parallel or manual ones).
10. `db.normalize_domain` keeps backslashes; `schedule.guess_timezone` has loose matches; expired `cache` rows are never
    purged.

## 18. Rules of thumb for the next agent

- Read before you edit: find the function in the module map above, read it and its tests first.
- Keep changes small and in the style of the surrounding code (type hints, short docstrings that explain *why*,
  plain-English user-facing messages - the audience is non-technical).
- User-visible wording is plain and short; error messages say what to do next.
- Do not invent new top-level folders, frameworks or build tools. Do not add dependencies casually.
- Do not weaken a safety invariant in section 8 to make a feature easier. If a request conflicts with one, stop and ask.
- Never print or log secrets (`GMAIL_APP_PASSWORD`, API keys). Never read `~/.leadsmith/.env` contents into output.
- When unsure whether something is intentional, check `git log` for the area, then ask the user.
