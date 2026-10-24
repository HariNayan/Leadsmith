---
name: setup
description: Set up Leadsmith for a new user - fill in the business profile (company, ICP, buying signals, personas, voice) through a short interview pre-filled from their website, and explain the dashboard, Gmail and sending settings. Use on first run, when brain files have TODOs, or when the user says "set up", "onboard", "update my ICP".
---

# Leadsmith setup

Goal: in about 10 minutes, the four brain files hold the user's real details and the user knows how to use the dashboard.

## 1. Check status
Call `leadsmith_status`. Tell the user briefly:
- No API keys or paid data are needed. Agents use free sources (company websites, public job boards, Google News, the YC directory, Hacker News "Who is hiring") and Claude's own web search, all on their Claude plan.
- Which brain files still need input.
- Gmail sending status (off by default).

## 2. Pre-fill from their website
Ask for their company website. Run `scan_website` on it (and `WebFetch` the product, pricing and customers pages), then draft company.md from what you find.

## 3. Interview (one topic at a time, 2-4 questions each)
Use `brain_read` to see the templates and remaining TODOs. Ask concise questions and offer your best guess from the website for them to confirm:
1. **Company & offer**: one-liner, the problem in the buyer's words, the outcome, price range, 1-3 real results with numbers, alternatives, the ask (call/demo/audit).
2. **ICP**: industries, size, locations, business model, enabling tech; buying signals that mean "now", weighted 1-5; disqualifiers; customers and competitors to exclude.
3. **Personas**: 1-2 personas with titles, seniority, what they're measured on, top pains, common objections.
4. **Voice**: sender name/role/sign-off, tone, language, banned words, decks yes/no, and ideally 1-3 emails that got replies.

Never invent proof points or customers. Leave a TODO if they don't know yet.

## 4. Save and hand over
- Write each file with `brain_write`, keeping the template's headings. Add customer and competitor domains to the do-not-contact list with `suppress`.
- Show a 5-line summary of the ICP and signals.
- Point them to the dashboard (the **dashboard** skill starts it). From there:
  - **Leads → Find buyers**: run the agents.
  - **Review**: approve emails.
  - **Settings**: connect Gmail (App Password from https://myaccount.google.com/apppasswords) and turn sending on.
- Never ask for passwords in chat. They enter them in the dashboard's Settings page.
