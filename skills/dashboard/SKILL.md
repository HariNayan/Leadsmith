---
name: dashboard
description: Open the Leadsmith dashboard (local web app at http://127.0.0.1:4600) to manage the business profile, leads, review/approve drafts, send from Gmail and watch agent runs. Use when the user says "open the dashboard", "show my leads/pipeline", "open Leadsmith" or wants a UI.
---

# Open the dashboard

1. Start it in the background (it keeps running after this turn) with the Bash tool:

   ```bash
   uv run --directory "${CLAUDE_PLUGIN_ROOT}" leadsmith dashboard --no-open
   ```

   Run it with `run_in_background: true`. If port 4600 is busy, add `--port 4700`.
2. Tell the user to open **http://127.0.0.1:4600** (offer to open it in the browser pane if one is available).
3. Explain the flow in one line: **Business** (profile) → **Leads** (Find buyers, Add companies, Import CSV) → **Review** (edit and approve) → **Outreach** (preview and send from Gmail, follow-ups, replies). **Activity** shows live agent runs; **Settings** holds Gmail and sending.

Buttons in the dashboard run Claude Code in the background on the user's plan. Approving and sending happen only by the user's click in the dashboard.
