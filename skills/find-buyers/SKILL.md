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
Dispatch the writer for accounts that now have research and at least one contact with an email (one agent per ~5 accounts).

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
