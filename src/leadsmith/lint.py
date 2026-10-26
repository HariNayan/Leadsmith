"""Mechanical checks on cold-email copy, run on every draft before a human sees it.

FAIL = must fix before approval. WARN = probably fix. The rules encode what
gets cold email ignored or filtered: length, AI-sounding phrases, spam
triggers, fake threads, unfilled placeholders. Rule ideas drawn from Harvey
and emelia-io/claude-outreach (both MIT); see NOTICE.md.
"""

from __future__ import annotations

import re
from typing import Any

# step -> (target words, hard cap)
WORD_LIMITS = {1: (75, 110), 2: (70, 100), 3: (45, 70)}

SPAM_TRIGGERS = [
    "100% free", "act now", "apply now", "buy now", "call now", "cash", "cheap", "click below", "click here",
    "congratulations", "dear friend", "dear sir", "discount", "don't miss", "double your", "earn extra",
    "exclusive deal", "exclusive offer", "expires", "free trial", "guarantee", "increase sales", "last chance",
    "limited offer", "limited time", "make money", "no cost", "no obligation", "no risk", "order now",
    "risk-free", "risk free", "special offer", "special promotion", "this is not spam", "to whom it may concern",
    "urgent", "winner", "% off", "$$$",
]

AI_SLOP = [
    "i hope this email finds you well", "hope this finds you well", "hope you're doing well", "hope you are well",
    "i wanted to reach out", "reaching out because", "just wanted to", "just following up", "just checking in",
    "circling back", "touching base", "i'd love to", "i would love to", "i know you're busy",
    "in today's fast-paced", "in today's world", "game-changer", "game changer", "unlock", "supercharge",
    "elevate", "seamless", "seamlessly", "leverage", "delve", "navigate the landscape", "revolutionize",
    "cutting-edge", "best-in-class", "world-class", "synergy", "skyrocket", "10x", "unleash", "empower",
    "picture this", "imagine if", "what if i told you", "worth a quick chat", "worth a quick call",
    "hop on a call", "pick your brain", "no-brainer", "streamline", "transformative",
    "in a world where", "rest assured", "i came across your profile",
]

ACRONYMS = {
    "EMEA", "APAC", "LATAM", "DACH", "ANZ", "MENA", "NAMER", "HTTP", "HTTPS", "GDPR", "HIPAA", "SOC2", "CCPA",
    "SaaS", "B2B2C", "KYC", "AML", "OKRS", "KPIS", "NASDAQ", "NYSE", "AWS", "GCP", "SDK", "SQL", "JSON", "HTML",
    "REST", "GRPC", "LLM", "LLMS", "RFP", "RFPS", "ERP", "HRIS", "ATS", "UGC", "SMB", "SMBS", "NGO", "ESG",
}

PLACEHOLDER_RE =re.compile(r"\{\{[^}]*\}\}|\{[a-z_][a-z0-9_]*\}|\[(?:first ?name|name|company|your name|insert[^\]]*)\]|<[A-Z_ ]{3,}>", re.I)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9'’\-]+", text)


ATTACH_RE = re.compile(r"\b(attached|attaching|i've attached|i have attached|see the attachment|enclosed)\b", re.I)


def claims_attachment(text: str) -> bool:
    return bool(ATTACH_RE.search(text or ""))


def check(subject: str | None, body: str, step: int = 1, company: str | None = None,
          first_name: str | None = None, kind: str = "sequence", attached: bool | None = None) -> dict[str, Any]:
    """`attached`: whether a deck PDF really goes with this email (None = unknown, not checked)."""
    issues: list[dict[str, str]] = []

    def add(level: str, rule: str, message: str) -> None:
        issues.append({"level": level, "rule": rule, "message": message})

    text = body or ""
    lower = text.lower()
    if attached is False and claims_attachment(text):
        add("FAIL", "attachment", "The email says something is attached, but no deck is attached. "
                                  "Attach the deck (it must have rendered as a PDF) or remove that line.")
    words = len(_words(text))
    target, cap = WORD_LIMITS.get(step, WORD_LIMITS[3])
    if kind == "reply":  # answering someone who wrote to you: room for real answers, no cold-email rules about CTAs
        target, cap, step = 140, 220, 2
    # An idea menu ("A) … B) … C) …") needs a little more room than a plain first email.
    idea_lines = re.findall(r"^\s*[A-E]\)\s+\S", text, re.M)
    if len(idea_lines) >= 2:
        target, cap = target + 30, cap + 25

    if words > cap:
        add("FAIL", "length", f"{words} words; step {step} must stay under {cap} (aim for ~{target}).")
    elif words > target:
        add("WARN", "length", f"{words} words; aim for ~{target} on step {step}.")
    elif words < 20:
        add("WARN", "length", f"Only {words} words; make sure it still says why you, why them, why now.")

    if "—" in text or " -- " in text:
        add("WARN", "em-dash", "Em dashes read as AI-written. Use a period or comma.")
    if "!" in text:
        add("WARN", "exclamation", "Drop the exclamation marks.")
    if text.count("?") > 2:
        add("WARN", "questions", "More than two questions. Ask one clear question.")
    if text.count("?") == 0 and step == 1:
        add("WARN", "cta", "No question. End with one easy-to-answer question.")
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    opening = re.sub(r"^(hi|hey|hello)\s+[^,\n]+,\s*", "", first_line, flags=re.I).strip()
    if not opening and len(text.strip().splitlines()) > 1:
        opening = next((ln.strip() for ln in text.strip().splitlines()[1:] if ln.strip()), "")
    if re.match(r"^(i|we|my|our)\b", opening, re.I):
        add("WARN", "self-first", "Opens with I/We/My. Start with them, not you.")
    if re.search(r"^\s*([-*•]|\d+\.)\s", text, re.M):
        add("WARN", "formatting", "Bullets or lists look like marketing. Use plain sentences.")
    if re.search(r"\*\*|__|<b>|<strong>", text):
        add("WARN", "formatting", "No bold or HTML. Plain text only.")

    links = re.findall(r"https?://\S+", text)
    if step == 1 and links:
        add("FAIL", "links", "No links in the first email (hurts deliverability).")
    elif len(links) > 1:
        add("FAIL", "links", f"{len(links)} links. Keep at most one.")
    if re.findall(r"\b[A-Z]{4,}\b", text):
        caps = [w for w in re.findall(r"\b[A-Z]{4,}\b", text) if w not in ACRONYMS]
        if caps:
            add("WARN", "caps", f"ALL-CAPS words look like shouting: {', '.join(sorted(set(caps))[:5])}")

    slop = sorted({p for p in AI_SLOP if p in lower})
    if slop:
        add("FAIL", "ai-phrases", f"Generic/AI-sounding phrases: {', '.join(slop[:8])}")
    spam = sorted({t for t in SPAM_TRIGGERS if t in lower})
    if spam:
        add("FAIL", "spam-words", f"Spam-filter triggers: {', '.join(spam)}")
    placeholders = PLACEHOLDER_RE.findall(text + " " + (subject or ""))
    if placeholders:
        add("FAIL", "placeholders", f"Unfilled placeholders: {', '.join(sorted(set(placeholders))[:5])}")

    if company and step == 1 and company.lower() not in lower and (first_name or "").lower() not in lower:
        add("WARN", "personalization", f"Doesn't mention {company} or the person. Is the first line specific to them?")

    if step == 1:
        if not subject or not subject.strip():
            add("FAIL", "subject", "Step 1 needs a subject line.")
        else:
            s = subject.strip()
            if len(s) > 60:
                add("FAIL", "subject", f"Subject is {len(s)} characters. Keep it under 45.")
            elif len(s) > 45:
                add("WARN", "subject", f"Subject is {len(s)} characters. Aim for 2-6 words.")
            if re.match(r"^(re|fwd?):", s, re.I):
                add("FAIL", "subject", "Fake Re:/Fwd: on a first email is deceptive and gets flagged.")
            if "!" in s or s.count("?") > 1:
                add("WARN", "subject", "Keep punctuation out of the subject.")
            spam_s = [t for t in SPAM_TRIGGERS if t in s.lower()]
            if spam_s:
                add("FAIL", "subject", f"Spam trigger in subject: {', '.join(spam_s)}")
            title_case = [w for w in s.split() if w[:1].isupper()]
            if len(s.split()) > 2 and len(title_case) == len(s.split()):
                add("WARN", "subject", "Title Case subjects look like newsletters. Use lowercase or sentence case.")

    fails = sum(1 for i in issues if i["level"] == "FAIL")
    return {"ok": fails == 0, "words": words, "fails": fails,
            "warnings": len(issues) - fails, "issues": issues}
