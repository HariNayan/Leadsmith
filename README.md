# Leadsmith

**Find buyers, write outreach they reply to, and send it from your Gmail, from a local dashboard powered by your own Claude Code.**

No API keys. No paid data providers. No SaaS subscription. Everything runs on your computer and on your Claude plan.
(One optional paid add-on, BetterContact email verification, stays off unless you add a key.)

```
 ┌─ 1 Business ─┐   ┌─ 2 Leads ───────────┐   ┌─ 3 Review ─────────┐   ┌─ 4 Outreach ──────────┐
 │ your offer,  │ → │ Find buyers · Add   │ → │ edit · approve ·   │ → │ preview · send from   │
 │ ICP, signals,│   │ companies · Import  │   │ send test to me ·  │   │ Gmail · auto follow-  │
 │ personas,    │   │ CSV → scored on fit │   │ attach PDF deck    │   │ ups · reply & bounce  │
 │ voice        │   │ + intent            │   │                    │   │ detection             │
 └──────────────┘   └─────────────────────┘   └────────────────────┘   └───────────────────────┘
                              ▲ every button runs your Claude Code in the background
          Agent 1 Signal Scout → Agent 2 People Finder + Agent 3 Account Researcher → Agent 4 Outreach Writer
```

## How it works

- **The engine is Claude Code**, running headless on your own plan, like [4ruple](https://basementprotocol.com/4ruple). Dashboard buttons start a background run; you watch it live under **Activity**.
- **Four agents share one memory** (a local SQLite pipeline plus your business profile):
  1. **Signal Scout** finds companies that fit your ICP *and* show a reason to buy now: hiring, funding, new leaders, launches, tech.
  2. **People Finder** finds the decision maker and champion, and their likely email.
  3. **Account Researcher** writes a short, sourced brief and the outreach angle.
  4. **Outreach Writer** writes a 3-step sequence in your voice, plus an optional LinkedIn note and a 4-page PDF deck.
- **Free data only:**

  | Source | Gives you |
  |---|---|
  | Company websites | what they sell, pricing, team page, tech stack (60+ tools), socials, public emails, content freshness |
  | Greenhouse / Lever / Ashby job boards | every open role, grouped by function: the strongest "need it now" signal |
  | Google News | funding rounds, launches, leadership changes, expansions |
  | Y Combinator directory | ~5,000 startups by industry, region, team size and hiring status |
  | Hacker News "Who is hiring" | hundreds of hiring companies every month, with role and website |
  | Claude's WebSearch / WebFetch | everything else: lists, directories, LinkedIn profiles via search, press, podcasts |
  | DNS (MX) + email patterns | a likely email per person, labelled `guessed` until it's proven |
  | Your CSV | import leads from Apollo, Sales Navigator, Clay, a CRM or a spreadsheet |

- **You stay in control.** Agents can research and draft but can never approve or send. Approval and sending are your clicks in the dashboard.
- **It learns from you.** Mark a lead **Good fit** or **Not a fit** (with a reason), and the scout reads those verdicts before every search. **Leads → Sources** shows which discovery plays produced leads you liked and replies, and the scout leans on those.
- **Your words where you want them.** In **Business → Email steps**, each step is either written fresh by the AI (with an extra instruction if you like) or your own template, using `{first_name}`, `{company}`, `{ideas}` and so on. Any other `{name}`, like `{personal_line}`, is one sourced sentence the AI fills in.
- **Replies handled.** Replies are matched to the prospect. "B" picks idea B, "no" or "unsubscribe" suppresses them, and **Draft reply** writes your answer for review.

| Optional add-on | What it does | Cost |
|---|---|---|
| [BetterContact](https://bettercontact.rocks) | A **Verify** button on guessed emails: a waterfall across many providers returns a verified address | Your BetterContact credits, charged only when it finds a usable email. Off unless `BETTERCONTACT_API_KEY` is set. One person per click; agents never spend credits. |

## Install

You need **[Claude Code](https://claude.com/claude-code)** (signed in) and **[uv](https://docs.astral.sh/uv/getting-started/installation/)**. uv installs Python for you. Chrome or Edge is optional, for PDF decks.

```bash
# Windows (PowerShell):  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
git clone https://github.com/HariNayan/Leadsmith.git
```

```bash
cd Leadsmith
```

```bash
uv sync
```

Open the dashboard (run this from inside the `leadsmith` folder, and keep the terminal open while you use it):

```bash
uv run leadsmith
```

From any other folder, point uv at the project instead. This works in PowerShell, cmd and bash:

```bash
uv run --directory C:/path/to/leadsmith leadsmith
```

On Windows you can instead double-click **`Start Leadsmith.cmd`** in the project folder.

It opens **http://127.0.0.1:4600**. Closing the terminal stops the dashboard. Check your setup any time with:

```bash
uv run leadsmith doctor
```

### Also use it inside Claude Code (optional)

The repo is a Claude Code plugin too, so you can chat with the same agents and tools:

```
/plugin marketplace add HariNayan/Leadsmith
/plugin install leadsmith@leadsmith
```

Or, from a clone: `claude --plugin-dir ./leadsmith`. Then try `/leadsmith:setup`, `/leadsmith:find-buyers 10 fintechs in India hiring sales` or `/leadsmith:dashboard`.

## First run (10 minutes)

1. **Business**: enter your website and click **Auto-fill**, then replace the remaining `TODO`s with real details (proof, buyer signals, voice).
2. **Settings**: connect Gmail. Turn on 2-Step Verification, create an App Password at <https://myaccount.google.com/apppasswords>, paste it, then click **Test connection**. Add your sender name, signature and postal address.
3. **Leads → Find buyers**: e.g. *"10 seed to Series A fintechs in India hiring their first sales rep"*.
4. **Review**: edit, **Send test to me**, then **Approve**.
5. **Outreach**: turn **Sending** on in Settings, then **Preview & send**. Follow-ups go out in the same thread when they're due and stop automatically when someone replies.

## Safety rails

- Nothing sends unless you approved the draft, **Sending** is on, and you confirmed the preview.
- Drafts with red lint issues can't be approved. The linter blocks AI-sounding phrases, spam triggers, links in first emails, fake `Re:` subjects, unfilled `{{placeholders}}` and over-long copy.
- Agents may only cite facts with a saved source URL, or your own business profile.
- Sends only inside business hours in the **prospect's** time zone (default 9–17, weekdays), one email a minute, with follow-ups counted in business days.
- **Warm-up**: a new sender starts at 5 emails a day and adds 3 a day up to your daily limit (default 25).
- A do-not-contact list, an opt-out line, a `List-Unsubscribe` header and your postal address on every email. Opt-out replies and bounces are suppressed automatically.
- Guessed emails are clearly labelled. Send to small batches first; bounces hurt your domain.
- The dashboard only listens on `127.0.0.1` and rejects cross-site and DNS-rebinding requests.
- Leads that look like they're in the **EU/EEA, UK, Switzerland or Canada** get a flag with that region's rule in one line.
- You are responsible for following email and privacy law where you and your recipients are. Read **[COMPLIANCE.md](COMPLIANCE.md)** (a summary, not legal advice).

## Where your data lives

```
~/.leadsmith/     (Windows: C:\Users\<you>\.leadsmith)
├── .env          Gmail + sending settings (edited from the dashboard)
├── brain/        company.md · icp.md · personas.md · voice.md
├── leadsmith.db  accounts, signals, contacts, drafts, decks, runs (SQLite)
├── decks/        generated PDF decks
└── exports/      CSV exports (Instantly, Smartlead, Lemlist, HubSpot…)
```

**Exports** use the column names Instantly and Smartlead map automatically (`email, first_name, last_name, company, title, website, linkedin_url`). After those come `reason` (why now), `idea_a/b/c`, `step1_subject`, `step1_body`, `step2_body` and `step3_body`, plus `timezone` and `strict_region`, all usable as custom variables. Opted-out and bounced people are left out.

Set `LEADSMITH_HOME` to keep a separate workspace per client or product. Coming from GTM Brain (the old name)? Your `~/.gtm-brain` folder is moved to `~/.leadsmith` on first start, with nothing lost.

## Commands

| Command | What it does |
|---|---|
| `uv run leadsmith` | open the dashboard (`--port 4700`, `--no-open`) |
| `uv run leadsmith doctor` | check Claude Code, free sources, PDF renderer, Gmail, profile |
| `uv run leadsmith serve` | MCP server over stdio (what Claude Code launches) |
| `uv run leadsmith init` | create the workspace without opening the dashboard |

## Development

```bash
uv run python -m unittest discover -s tests -v
```

```bash
claude plugin validate .
```

Layout: `src/leadsmith/` (`server.py` MCP tools · `dashboard.py` + `web/` UI · `jobs.py` Claude Code runner · `providers/` free data · `mailer.py` Gmail · `decks.py` PDF · `lint.py` copy checks), `agents/`, `skills/`.

## Credits

Inspired by [4ruple](https://basementprotocol.com/4ruple) (the local-app, your-own-Claude approach) and by open-source sales and outreach projects; see [NOTICE.md](NOTICE.md).

## License

MIT
