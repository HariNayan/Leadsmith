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

# Research-note wording: it describes the prospect to a third party instead of talking to them.
REPORT_SPEAK = [
    "signaling", "signalling", "which signals", "this signals", "a timely opening", "timely opening", "a good moment to",
    "a great moment to", "the perfect time to", "making this a good", "makes this a good",
    "creating a timely", "clear story to tell", "positioned to benefit", "well-positioned", "per your careers page",
]
# Opening with news they already know (their own job ad, their own hiring) instead of what it means for them.
NEWS_OPENER_RE = re.compile(
    r"careers page|job (?:post|posting|listing|opening|ad)s?\b|(?:\b(?:is|are)|'re) (?:actively |now )?hiring\b|"
    r"\bis expanding (?:its|the)\b|\bhas (?:an )?open roles?\b|\blists (?:a|an|two|three|several|new)\b", re.I)
GREETING_RE = re.compile(r"^\s*(?:hi|hey|hello|dear)\s+([^,\n]{1,60}),", re.I)

ASKS = ("interest", "call", "sample")
CALL_ASK_RE = re.compile(r"\b(?:call|chat|meeting|meet|zoom|calendar|book (?:a )?time|hop on|\d+[- ]?min(?:ute)?s?)\b", re.I)
SAMPLE_ASK_RE = re.compile(r"\b(?:want me to|shall i|should i|happy to|can i|could i)\s+(?:make|send|draft|sketch|"
                           r"put together|mock up|cut|write|record|build)\b|\bfree sample\b", re.I)


def ask_setting() -> str:
    from . import config  # local: the checks themselves need no settings

    value = (config.get("EMAIL_ASK") or "interest").strip().lower()
    return value if value in ASKS else "interest"


def ask_kind(text: str) -> str | None:
    """How an email ends: 'call', 'sample' (offers to make something), 'interest' (any other question), or None."""
    questions = [s for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip().endswith("?")]
    if not questions:
        return None
    last = questions[-1]
    if CALL_ASK_RE.search(last):
        return "call"
    if SAMPLE_ASK_RE.search(last):
        return "sample"
    return "interest"


def _sentences(text: str) -> list[str]:
    lines = (text or "").strip().splitlines()
    if lines and (GREETING_RE.match(lines[0]) or re.fullmatch(r"\s*(?:hi|hey|hello|dear|greetings)\b[^.?\n]{0,60}", lines[0], re.I)):
        lines = lines[1:]
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", "\n".join(lines)) if s.strip()]


def _norm(sentence: str) -> str:
    s = re.sub(r"\d+", "#", sentence.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z# ]", " ", s)).strip()


def _overlap(a: list[str], b: list[str]) -> float:
    """Share of the shorter sentence's words that the other has in the same order. A template sentence keeps its
    skeleton when only the names change ("We plan creator campaigns and produce X videos for Y")."""
    from difflib import SequenceMatcher

    if not a or not b:
        return 0.0
    shorter = min(len(a), len(b))
    if len(set(a) & set(b)) < 0.5 * min(len(set(a)), len(set(b))):  # cheap first cut: clearly different
        return 0.0
    matched = sum(block.size for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    return matched / shorter


def shared_sentences(body: str, others: list[str], threshold: float = 0.7) -> tuple[list[str], int]:
    """The sentences of `body` that (nearly) repeat sentences of the most similar other email, and how many
    sentences `body` has that count. Short lines and the closing question are left out: those may repeat."""
    mine = [s for s in _sentences(body) if len(s.split()) >= 4 and not s.endswith("?")]
    if not mine or not others:
        return [], len(mine)
    mine_n = [_norm(s).split() for s in mine]
    best: list[str] = []
    for other in others:
        theirs = [_norm(s).split() for s in _sentences(other) if len(s.split()) >= 4]
        if not theirs:
            continue
        hits = []
        for raw, a in zip(mine, mine_n):
            if any(_overlap(a, b) >= threshold for b in theirs):
                hits.append(raw)
        if len(hits) > len(best):
            best = hits
            if len(best) == len(mine):
                break
    return best, len(mine)


PLACEHOLDER_RE =re.compile(r"\{\{[^}]*\}\}|\{[a-z_][a-z0-9_]*\}|\[(?:first ?name|name|company|your name|insert[^\]]*)\]|<[A-Z_ ]{3,}>", re.I)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9'’\-]+", text)


ATTACH_RE = re.compile(r"\b(attached|attaching|i've attached|i have attached|see the attachment|enclosed)\b", re.I)


MENU_RE = re.compile(r"^\s*A\s*[).:\-]\s*\S.*\n(?:.*\n)*?\s*B\s*[).:\-]\s*\S", re.M)


def has_idea_menu(text: str) -> bool:
    """An 'A) ... B) ...' list of options."""
    return bool(MENU_RE.search(text or ""))


def mentions_company(text: str, company: str) -> bool:
    """Does the email name the company? Accepts the name with or without suffixes, e.g. 'Acme' for 'Acme Inc.'"""
    lower = (text or "").lower()
    name = re.sub(r"\(.*?\)", "", company or "").strip().lower()
    core = re.sub(r"\b(inc|ltd|llc|gmbh|pvt|co|corp|limited|technologies|labs|hq)\b\.?", "", name).strip(" ,.-")
    candidates = {name, core, core.split(".")[0], core.replace(" ", "")}
    return any(c and len(c) >= 3 and c in lower for c in candidates)


def claims_attachment(text: str) -> bool:
    return bool(ATTACH_RE.search(text or ""))


def check(subject: str | None, body: str, step: int = 1, company: str | None = None,
          first_name: str | None = None, kind: str = "sequence", attached: bool | None = None,
          style: str | None = None, others: list[str] | None = None, first_email: str | None = None,
          ask: str | None = None) -> dict[str, Any]:
    """`attached`: whether a deck PDF really goes with this email (None = unknown, not checked).
    `style`: the user's email style; in 'personal_pitch' a first email must not offer an A/B/C menu.
    `others`: the same step's emails to other people, to catch copy-paste templates.
    `first_email`: for a follow-up, email 1's body, so the follow-up adds something new.
    `ask`: the user's ask setting (interest, call or sample)."""
    issues: list[dict[str, str]] = []

    def add(level: str, rule: str, message: str) -> None:
        issues.append({"level": level, "rule": rule, "message": message})

    text = body or ""
    lower = text.lower()
    _deliverability_checks(add, subject, text, lower, step, company, first_name, kind, attached, style)
    if kind != "reply":
        _craft_checks(add, text, lower, step, first_name, others, first_email, ask)
    fails = sum(1 for i in issues if i["level"] == "FAIL")
    return {"ok": fails == 0, "words": len(_words(text)), "fails": fails,
            "warnings": len(issues) - fails, "issues": issues}


def _craft_checks(add, text: str, lower: str, step: int, first_name: str | None, others: list[str] | None,
                  first_email: str | None, ask: str | None) -> None:
    """What makes a cold email read as written for one person: no research-note wording, no news repeated back,
    a real greeting, not copied from other emails, follow-ups that add something, the chosen ask."""
    cold = True
    report = sorted({p for p in REPORT_SPEAK if p in lower})
    if cold and report:
        add("FAIL", "report-speak", f"Reads like a research note, not a person: {', '.join(report[:4])}. Say what you "
                                    "noticed and what it means for them, in plain words.")
    if cold and step == 1:
        opening_line = next((s for s in _sentences(text) if s), "")
        if NEWS_OPENER_RE.search(opening_line):
            add("WARN", "news-opener", "Opens by repeating their own news (a job ad, hiring). They know it. Open with "
                                       "the gap it reveals and what that costs them now.")
    if re.match(r"\s*(?:hi|hey|hello|dear|greetings)(?: there| all| team| sir| madam)?\s*[,!.]?\s*\n", text, re.I):
        add("WARN", "greeting", "Greets nobody by name, so it reads as bulk mail. Write to a named person.")
    if step == 1 and text.count("?") == 2:
        add("WARN", "questions", "Two questions. End with one easy question; a second ask (\"could you forward this?\") "
                                 "makes them choose, and they usually answer neither.")
    greeting = GREETING_RE.match(text)
    if cold and greeting:
        name = greeting.group(1).strip()
        if re.search(r"\bteam\b|\ball\b|\bthere\b|\bfolks\b", name, re.I):
            add("WARN", "greeting", f"\"{name}\" greets nobody, so it reads as bulk mail. Write to a named person.")
        elif len(name.split()) >= 2 and (not first_name or name.lower() != first_name.strip().lower()):
            add("WARN", "greeting", f"Greet by first name only (\"Hi {name.split()[0]},\"), not the full name.")
    if cold and others:
        shared, total = shared_sentences(text, others)
        if len(shared) >= 3 and len(shared) >= 0.6 * total:
            add("FAIL", "template", f"{len(shared)} of {total} sentences match another email almost word for word, "
                                    f"e.g. \"{shared[0][:90]}\". Write this one for this person.")
        elif len(shared) >= 2:
            add("WARN", "template", f"{len(shared)} sentences match another email, e.g. \"{shared[0][:90]}\". Vary them.")
    if cold and first_email and step > 1:
        repeated, _ = shared_sentences(text, [first_email])
        if repeated:
            add("WARN", "repeats", f"Repeats email 1 (\"{repeated[0][:80]}\"). A follow-up should add something new: a "
                                   "detail, a sample, the proof or a smaller first step.")
    if cold and step == 1 and ask in ASKS:
        actual = ask_kind(text)
        if ask == "interest" and actual == "call":
            add("WARN", "ask", "Asks for a call, but your ask is set to interest: ask if it's useful instead, the "
                               "easiest yes for a stranger.")
        elif ask == "sample" and actual not in ("sample", None):
            add("WARN", "ask", "Your ask is set to sample: offer to make the smallest first piece for them.")


def _deliverability_checks(add, subject: str | None, text: str, lower: str, step: int, company: str | None,
                           first_name: str | None, kind: str, attached: bool | None, style: str | None) -> None:
    if style == "personal_pitch" and step == 1 and kind != "reply" and has_idea_menu(text):
        add("FAIL", "style", "Your email style is Personal pitch: no A/B/C options. Write one specific observation, "
                             "what you do for buyers like theirs, the deck line and one short ask.")
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

    if step == 1 and kind != "reply" and company and not mentions_company(text + " " + (subject or ""), company):
        add("WARN", "generic", f"Doesn't mention {company} or anything specific to them, so it could go to anyone. "
                               "Name their product, launch or news.")
