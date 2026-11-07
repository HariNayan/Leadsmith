---
name: find-buyers
description: Run the full Leadsmith pipeline - find companies showing buying signals, identify the right people, research each account, and draft personalised outreach for review. Use when the user says "find me buyers", "find leads/prospects", "build a list", "who should I reach out to", or gives target domains to research and write to.
argument-hint: "[how many / which segment or signal / specific domains]"
---

# Find buyers

You orchestrate four specialist agents that share one memory (the Leadsmith MCP tools). Keep the user posted with one short line per phase.

## 0. Preflight
- `leadsmith_status`. If brain files still have TODOs in company/icp/personas, say so and run the **setup** skill first (or, if the user wants to go now, ask the 3 essentials: what you sell, who buys it, what signals mean "now").
- Parse the request: target count (default 10 accounts), segment, signals, geography, any specific domains. All data sources are free (no keys); the agents combine the leadsmith tools with WebSearch/WebFetch.

## 1. Discover → `leadsmith:signal-scout`
Skip if the user gave specific domains (just `save_account` them and go to step 2).
Otherwise dispatch the signal-scout agent with the goal, count and any constraints. When it returns, show the ranked table and continue with the top N by fit + intent (no need to ask unless the list is clearly off-target).

## 2. Research and people, in parallel
For the selected domains, dispatch at the same time:
- `leadsmith:account-researcher`, one agent per ~3 accounts
- `leadsmith:people-finder`, one agent per ~5 accounts

## 3. Write → `leadsmith:outreach-writer`
Skip this step when your instructions say the run only finds and verifies leads (the dashboard's Find buyers does: the user picks the good leads and starts writing separately). Otherwise dispatch the writer for accounts that now have research and at least one contact with an email (one agent per ~5 accounts).

## Quality gate
Before showing anything, call `quality_check` with the domains you worked on. Fix every FAIL: find a real, dated source, write a specific why_now, or rewrite the email so it names something true about that company. If you can't fix it, drop that company. Fewer genuine results beat a padded list. Mention any WARNs you couldn't fix.

## 4. Review with the user
Call `list_drafts` (status "draft") and present them compactly, grouped by account:
- account, why now (1 line), contact (name, title, email status)
- step 1 subject + body in full; steps 2-3 subjects (offer to show bodies)
- any lint warnings

Then ask the user what to do: approve all, approve some (by id), or request edits. Apply edits by re-saving with `save_draft`. Only call `review_drafts` with `approve` for drafts the user explicitly approved.

## 5. Next steps
Tell the user what's ready and the options:
- **Review and send in the dashboard** (the dashboard skill opens it): edit, approve, send a test to yourself, attach decks, then send from Gmail. Follow-ups go out automatically while it's open.
- **Send from chat**: the outreach skill (preview first, then send approved emails).
- **Export** for Instantly/Smartlead/Lemlist/HubSpot: `export_csv` kind "sequences", status "approved".
- **Run again daily**: new signals appear every day.

## Rules
- Nothing is sent from this skill.
- If a source returns nothing, try another play (WebSearch is always available). Don't stop.
- If a phase finds nothing, say why and suggest a different play rather than padding with weak accounts.
- A detail the user asked for that you can't verify (a city, a price) is not a reason to drop a lead whose fit and need are verified: save it, label what's unverified, and list those gaps in your summary. Only a lead whose evidence contradicts the brief, or that has no evidence of fit or need, gets dropped.
- Everything comes from this user's profile and results, not from assumptions about an industry: Leadsmith serves many kinds of business.
- **Proof of need**: every new lead needs a `need` signal: sourced evidence of one of the profile's buying signals. No evidence, no lead.
- **Fit is a rubric** of the profile's own criteria (`fit_reason` lists met / missed / unknown), and `source` is one of the fixed labels in the signal-scout playbook.
- **Learn from this workspace**: `brain_read` → `results_so_far` and `lead_feedback` say what replied and what the user liked. Start from the winners (lookalikes) when there are any.
- **Remember rejections**: `save_rejected` for candidates screened out for a lasting reason; `check_known` skips them later.
- **Pick the fastest sources for the kind of customer** (the signal-scout's table), screen wide with cheap checks, run independent checks in parallel, and research deeply only the shortlist.
- **Published emails**: people-finder looks for a published address first. Guessed addresses aren't written to or sent unless the user allows it in Settings, so a contact with only a guess usually won't get an email.
- A place in the brief (a city, region or country anywhere): follow the signal-scout's place section: search with the place in the query, prove location and label it confirmed / likely / unknown.
- LinkedIn, Reddit, Instagram and X are searched through Google, directories and archives only, never by fetching or logging in (signal-scout sections). Never mention a pseudonymous post or an anonymous job post in an email.
- **The number asked for is the goal.** Keep searching while untried sources and wordings remain; the signal-scout's minimum (6+ plays, 25+ searches, 5× the target screened) is a floor, not a stopping point. When exact matches run out, save labelled near-fits (they miss one soft requirement; never a hard one). This applies when you do the scouting yourself too.
