---
name: outreach
description: Daily outreach run for Leadsmith - check replies, review pending drafts, preview and send approved emails (and due follow-ups) from the user's Gmail, and update deal stages. Use when the user says "send", "check replies", "run my outreach", "follow up", "pipeline update", or marks a meeting/won/lost.
---

# Daily outreach run

## 1. Replies first
If Gmail is configured (`leadsmith_status` → email), call `check_replies`. For each reply, tell the user who replied (they read and answer in Gmail; follow-ups to that person are already cancelled). Offer to draft a reply if they paste the message. Opt-out reply? `suppress` that email.

## 2. Pending drafts
`list_drafts` status "draft". If any, present them as in find-buyers step 4 and get explicit approval before `review_drafts`.

## 3. Send
1. Always preview first: `send_approved_emails` with `dry_run=true`. Show recipients, subjects and the first lines, plus anything skipped and why (e.g. a follow-up whose wait hasn't passed).
2. Ask: "Send these N emails from <gmail address>?" Only after a clear yes, call `send_approved_emails` with `dry_run=false`.
3. If sending is off or Gmail isn't set up, point them to the dashboard's **Settings** page (the dashboard skill opens it): App Password from https://myaccount.google.com/apppasswords, then switch on Sending. Or offer `export_csv` instead.

## 4. Pipeline
Show `list_pipeline` top accounts by stage. When the user reports outcomes ("booked a call with Acme", "lost X"), use `update_status` (meeting / won / lost) with a note.

## Rules
- Never send without a dry-run preview and the user's explicit yes in this conversation.
- Respect the daily limit; never try to work around it.
- Don't approve drafts the user hasn't seen.
