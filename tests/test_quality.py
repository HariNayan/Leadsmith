"""Offline tests for reply reading, sending safety, the profile questions, email and deck quality, and the
what-works numbers. No network, no keys."""

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("LEADSMITH_HOME", tempfile.mkdtemp(prefix="leadsmith-test-"))

from leadsmith import config, db, mailer  # noqa: E402

# Real automatic answers (names changed), and real answers from people.
OOO_DATES = ("Hi,\n\nI'm offline between 9 - 15 November! I'll be back at my desk 16th November and will pick up "
             "any outstanding emails then.\n\nBest,\nNicole")
OOO_CONFERENCE = ("Happy Friday!  Thank you for your email. I am attending a conference today and may be delayed in "
                  "responding to your message.  I appreciate your patience and will respond as soon as possible.")
TICKET = ("Dear Harinayan, We would like to acknowledge that we have received your request and a ticket has been "
          "created. We strive to respond to all inquiries within 1-2 business days.")
AUTO_TRIAGE = ("Hey there,\n\nThis is an automatic reply. This is necessary because I get an incredible amount of "
               "email, and I need to prioritize my time.")
HUMAN_YES = ("Hi,\nThanks for reaching out and for taking the time to put together a proposal. It sounds interesting. "
             "Before arranging a call, would you mind sharing your website and a portfolio?")
HUMAN_COSTS = "Thanks for the message - We have had a look through what you sent over. Let's get an idea of the costs."
HUMAN_CRITIC = "Very broad spectrum, the deck doesn't elaborate on anything. This is obviously bulk sent to organisations correct?"


class AutoReplyTests(unittest.TestCase):
    def test_automatic_answers_are_not_replies(self):
        for text in (OOO_DATES, OOO_CONFERENCE, TICKET, AUTO_TRIAGE):
            self.assertEqual(mailer.classify_reply(text), "auto", text[:40])
        for text in (HUMAN_YES, HUMAN_COSTS, HUMAN_CRITIC):
            self.assertEqual(mailer.classify_reply(text), "reply", text[:40])
        self.assertEqual(mailer.classify_reply("no"), "optout")
        self.assertEqual(mailer.classify_reply("Thanks", "Automatic reply: Kestrel trail shoe"), "auto")
        self.assertEqual(mailer.classify_reply("Thanks", "[Ticket #64041] Re: your note"), "auto")
        self.assertEqual(mailer.classify_reply("Sure, send it", "Re: Request 2026 plan"), "reply")
        self.assertEqual(mailer.classify_reply("Sounds good", "Re: hi", auto_header=True), "auto")

    def test_return_date(self):
        today = date(2026, 11, 13)
        self.assertEqual(mailer.return_date(OOO_DATES, today), date(2026, 11, 16))
        self.assertEqual(mailer.return_date("Away until Nov 20, 2026.", today), date(2026, 11, 20))
        self.assertEqual(mailer.return_date("Back on 3 January.", date(2026, 12, 20)), date(2027, 1, 3))
        self.assertIsNone(mailer.return_date(OOO_CONFERENCE, today))
        self.assertIsNone(mailer.return_date("We met on 2 March.", today))  # past dates aren't return dates

    def _sequence(self, domain, email):
        contact, _ = db.upsert_contact(domain, first_name="Nia", email=email)
        first = db.upsert_draft(contact["id"], 1, "hi", "body one", "email", 0, {"ok": True})
        second = db.upsert_draft(contact["id"], 2, None, "body two", "email", 3, {"ok": True})
        sent_at = (datetime.now(timezone.utc) - timedelta(days=20)).replace(microsecond=0).isoformat()
        with db.connect() as conn:
            conn.execute("UPDATE drafts SET status = 'sent', sent_at = ?, message_id = '<m1@x>' WHERE id = ?", (sent_at, first["id"]))
            conn.execute("UPDATE drafts SET status = 'approved' WHERE id = ?", (second["id"],))
            conn.execute("UPDATE contacts SET status = 'contacted' WHERE id = ?", (contact["id"],))
        return contact, second

    def test_out_of_office_keeps_the_sequence_and_waits_for_the_return_date(self):
        contact, second = self._sequence("ooo-co.com", "nia@ooo-co.com")
        back = (date.today() + timedelta(days=5)).strftime("%d %B")
        raw = (f"From: nia@ooo-co.com\r\nSubject: Out of Office: hi\r\nMessage-ID: <a1@x>\r\nAuto-Submitted: auto-replied"
               f"\r\n\r\nI'm away until {back}.").encode()

        class FakeIMAP:
            def __init__(self, *a, **k): pass
            def login(self, *a): pass
            def select(self, *a, **k): pass
            def logout(self): pass
            def search(self, charset, *criteria):
                return "OK", [b"" if '"mailer-daemon"' in criteria else b"1"]
            def fetch(self, ids, what):
                data = (b"From: nia@ooo-co.com\r\n\r\n" if "HEADER.FIELDS (FROM)" in what else
                        raw.split(b"\r\n\r\n")[0] if "HEADER.FIELDS (SUBJECT" in what else raw)
                return "OK", [(b"1 (BODY[] {%d}" % len(data), data), b")"]

        config.save_env({"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "x"})
        try:
            with mock.patch.object(mailer.imaplib, "IMAP4_SSL", return_value=FakeIMAP()):
                result = mailer.check_replies(30)
        finally:
            config.save_env({"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""})
        mine = [r for r in result["replies"] if r["contact_id"] == contact["id"]]
        self.assertEqual(mine[0]["kind"], "auto")
        after = db.get_contact(contact["id"])
        self.assertEqual(after["status"], "contacted")  # not "replied"
        self.assertEqual(after["resume_after"], (date.today() + timedelta(days=6)).isoformat())
        with db.connect() as conn:
            self.assertEqual(db.one(conn.execute("SELECT status FROM drafts WHERE id = ?", (second["id"],)))["status"], "approved")
            draft = db.one(conn.execute("SELECT * FROM drafts WHERE id = ?", (second["id"],)))
            due, reason, _ = mailer._due(draft, conn)
        self.assertFalse(due)
        self.assertIn("out of office", reason)

    def test_resume_and_real_reply_corrections(self):
        contact, second = self._sequence("ticket-co.com", "sam@ticket-co.com")
        with db.connect() as conn:  # how an older version stored a ticket: as a reply that stopped the sequence
            conn.execute("UPDATE contacts SET status = 'replied', reply_kind = 'reply', last_reply = ? WHERE id = ?",
                         (TICKET, contact["id"]))
            conn.execute("UPDATE drafts SET status = 'cancelled' WHERE id = ?", (second["id"],))
        result = mailer.mark_reply(contact["id"], "auto")
        self.assertEqual(result["changed"], 1)
        self.assertEqual(db.get_contact(contact["id"])["status"], "contacted")
        statuses = {d["id"]: d["status"] for d in db.drafts(None, 10000)}
        self.assertEqual(statuses[second["id"]], "draft")  # back in Review, never straight to the send queue
        mailer.mark_reply(contact["id"], "reply")
        self.assertEqual(db.get_contact(contact["id"])["status"], "replied")
        self.assertEqual({d["id"]: d["status"] for d in db.drafts(None, 10000)}[second["id"]], "cancelled")


class SendingSafetyTests(unittest.TestCase):
    def setUp(self):
        with db.connect() as conn:  # other tests send and bounce too
            conn.execute("DELETE FROM events WHERE type IN ('sent', 'bounce')")
        config.save_env({"SEND_PAUSED": "", "SEND_RESUMED_AT": ""})

    def test_unproven_and_shared_addresses(self):
        for status in ("guessed", "guessed (pattern)", "public_unconfirmed", "pattern match", "likely"):
            self.assertTrue(db.is_guessed(status), status)
        for status in ("public", "published", "verified", "manual", "imported", None, ""):
            self.assertFalse(db.is_guessed(status), status)
        for email in ("team@acme.com", "info@acme.com", "Hello@acme.com", "no-reply@acme.com", "customer.service@x.io"):
            self.assertTrue(db.is_role_address(email), email)
        for email in ("dana@acme.com", "tim@pvt.space", "harinayan.work@gmail.com", None):
            self.assertFalse(db.is_role_address(email), email)

    def test_shared_inbox_is_flagged_for_the_agents(self):
        db.upsert_account("shared-co.com", name="Shared Co", why_now="launched")
        db.upsert_contact("shared-co.com", first_name="Shared", email="team@shared-co.com", email_status="public")
        issues = db.quality_report(["shared-co.com"])["issues"]
        self.assertTrue(any("shared inbox" in i["issue"] for i in issues))

    def test_bounces_pause_sending_until_you_resume(self):
        config.save_env({"SEND_PAUSED": "", "SEND_RESUMED_AT": ""})
        for i in range(40):
            db.log_event("sent", f"step 1: hello {i}")
        for i in range(2):
            db.log_event("bounce", f"bad{i}@nowhere.io")
        self.assertIsNone(mailer.check_bounce_guard())  # 2 bounces: below the minimum
        db.log_event("bounce", "bad2@nowhere.io")
        reason = mailer.check_bounce_guard()  # 3 of 40 = 7.5%
        self.assertIn("paused", reason)
        self.assertEqual(mailer.mail_status()["paused"], reason)
        config.save_env({"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "x", "SEND_ENABLED": "true"})
        try:
            with mock.patch.object(mailer, "check_replies", return_value={"replies": [], "bounces": []}):
                report = mailer.send_approved(5, dry_run=False)
        finally:
            config.save_env({"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""})
        self.assertIn("paused", report["error"])
        mailer.resume_sending()
        self.assertIsNone(mailer.paused_reason())
        self.assertEqual(mailer.bounce_health()["bounced"], 0)  # counted again from the resume
        self.assertIsNone(mailer.check_bounce_guard())

    def test_test_mode_sends_dont_count(self):
        db.log_event("sent", "step 1: hi (test, to me@gmail.com)")
        self.assertEqual(mailer.bounce_health()["sent"], 0)
        db.log_event("sent", "step 1: hi")
        self.assertEqual(mailer.bounce_health()["sent"], 1)


class ProfileQuestionTests(unittest.TestCase):
    def setUp(self):
        from leadsmith import brain
        self.brain = brain
        config.ensure_home()
        self.saved = {n: i["content"] for n, i in brain.read_brain().items()}

    def tearDown(self):
        for name, content in self.saved.items():
            self.brain.write_brain(name, content)
            self.brain.discard_proposal(name)

    def test_core_questions_then_one_per_open_todo(self):
        self.brain.write_brain("voice", "# Voice\n\n## Sender\n- Name: Sam\n- Sign-off: TODO (e.g. \"Priya\")\n\n## Never\n- TODO add your own banned words\n")
        result = self.brain.profile_questions()
        self.assertEqual([q["id"] for q in result["questions"]][:3], ["lead_offer", "offers", "proof"])
        voice = [m for m in result["missing"] if m["file"] == "voice"]
        self.assertEqual([m["question"] for m in voice], ["Sign-off?", "Add your own banned words."])
        self.brain.write_brain("personas", "# Personas\n<!-- notes\n with a TODO inside -->\n## Persona 1: TODO name\n"
                                           "- Top pains:\n  1. TODO\n  2. TODO\n- \"We use X\" → TODO\n")
        personas = [m["question"] for m in self.brain.profile_questions()["missing"] if m["file"] == "personas"]
        self.assertEqual(personas, ["Top pains?", 'Your honest answer to "We use X"'])
        self.assertIn('e.g. "Priya"', voice[0]["help"])
        text = " ".join(q["question"] + q["help"] for q in result["questions"]).lower()
        for niche in ("video", "saas", "course", "creator", "solar"):  # works for any business
            self.assertNotIn(niche, text)

    def test_suggestions_change_nothing_until_saved(self):
        before = self.brain.read_brain()["company"]["content"]
        self.brain.propose("company", "# Company & offer\n\n## Offers\n- Lead offer: Audits\n")
        self.assertEqual(self.brain.read_brain()["company"]["content"], before)
        found = self.brain.proposals()
        self.assertEqual([p["file"] for p in found], ["company"])
        self.assertIn("Lead offer: Audits", found[0]["after"])
        self.brain.accept_proposal("company", found[0]["after"] + "- Edited by me\n")
        self.assertIn("Edited by me", self.brain.read_brain()["company"]["content"])
        self.assertEqual(self.brain.proposals(), [])
        self.brain.propose("voice", "# Voice\n")
        self.brain.discard_proposal("voice")
        self.assertEqual(self.brain.proposals(), [])

    def test_profile_run_only_suggests(self):
        from leadsmith import jobs, server
        title, prompt = jobs._fmt("profile", {"answers": [
            {"question": "What do you want to sell?", "answer": "Audits first, retainers later"},
            {"question": "Who is it for?", "answer": "  "}]})
        self.assertIn("Audits first, retainers later", prompt)
        self.assertNotIn("Who is it for?", prompt)  # blank answers are skipped
        self.assertIn("brain_propose", prompt)
        self.assertIn("never call brain_write", prompt)
        self.assertEqual(server.SCOPES["profile"], {"leadsmith_status", "brain_read", "brain_propose"})
        with self.assertRaises(ValueError):
            jobs._fmt("profile", {"answers": []})


NOVEMBER = ("Hi Nicolas,\n\nAepsy’s careers page lists a Performance Marketing Manager and a Psychology Community Manager "
            "in Zurich, signaling active work on acquisition and community growth.\n\nI’m Harinayan at Kernelhaus. We plan "
            "creator campaigns and produce social videos and explainers that help people understand a service before they "
            "use it.\n\nI’ve attached a 4-page deck with a clinically reviewable creator concept for choosing and booking a "
            "psychologist.\n\nWould a short call this week help?")
NOVEMBER_2 = ("Hi Karim,\n\nCerrion is expanding its marketing team, with Zurich roles in growth marketing.\n\nI’m Harinayan "
              "at Kernelhaus. We plan creator campaigns and produce social videos and explainers for technical products."
              "\n\nI’ve attached a 4-page deck with a creator-led detection-to-action story for Cerrion.\n\nWould a short call "
              "this week help?")
NOVEMBER_3 = ("Hi Manuel,\n\nEnact Intelligence emerged from stealth with $30M in pre-seed funding.\n\nI’m Harinayan at "
              "Kernelhaus. We plan creator campaigns and produce product videos and explainers that make technical ideas "
              "visible.\n\nI’ve attached a 4-page deck with a creator-led concept for Enact’s force-and-touch robotics."
              "\n\nWould a short call this week help?")
OCTOBER = ("Hi Kevin,\n\nKevin Powell's CSS Demystified ships Writing resilient CSS late November, and teams buying seats "
           "decide before then.\n\nThe bulk licence page still sells seats from a checkout form, with no film of what a "
           "team gets.\n\nKernelhaus makes product and explainer films, so buyers see the value before they ask for a "
           "call.\n\nI've attached a 4-page deck for The Cascade with a 45-second plan.\n\nWould this help before the launch?")


class EmailQualityTests(unittest.TestCase):
    def issues(self, result):
        return {(i["level"], i["rule"]) for i in result["issues"]}

    def test_research_note_wording_fails(self):
        from leadsmith import lint
        found = self.issues(lint.check("Aepsy growth", NOVEMBER, 1, "Aepsy", "Nicolas", attached=True))
        self.assertIn(("FAIL", "report-speak"), found)
        self.assertIn(("WARN", "news-opener"), found)

    def test_the_october_email_passes(self):
        from leadsmith import lint
        result = lint.check("Team licences before November", OCTOBER, 1, "Kevin Powell", "Kevin", attached=True,
                            others=[NOVEMBER, NOVEMBER_2], ask="interest")
        self.assertTrue(result["ok"], result["issues"])
        self.assertNotIn("template", {i["rule"] for i in result["issues"]})

    def test_copied_sentences_across_people(self):
        from leadsmith import lint
        result = lint.check("Enact robotics", NOVEMBER_3, 1, "Enact Intelligence", "Manuel", attached=True,
                            others=[NOVEMBER_2, OCTOBER])
        self.assertIn(("FAIL", "template"), self.issues(result))  # who-we-are and deck lines copied from Cerrion's
        shared, total = lint.shared_sentences(NOVEMBER_3, [NOVEMBER_2])
        self.assertGreaterEqual(len(shared), 3)
        self.assertEqual(lint.shared_sentences(OCTOBER, [NOVEMBER_2])[0], [])

    def test_follow_up_must_add_something(self):
        from leadsmith import lint
        follow = "Hi Kevin,\n\nThe bulk licence page still sells seats from a checkout form, with no film of what a team gets. Any thoughts?"
        self.assertIn(("WARN", "repeats"), self.issues(lint.check(None, follow, 2, "Kevin Powell", "Kevin", first_email=OCTOBER)))

    def test_greetings(self):
        from leadsmith import lint
        team = lint.check("x", "Hi Guardian Flame team,\n\nThe launch page has no video. Would this help?", 1, "Guardian Flame")
        full = lint.check("x", "Hi Nicole Marguerite Gray,\n\nThe sales page lists 12 modules. Would this help?", 1, "Five to Nine", "Nicole")
        self.assertIn(("WARN", "greeting"), self.issues(team))
        self.assertIn(("WARN", "greeting"), self.issues(full))
        fine = lint.check("x", "Hi Nicole,\n\nThe sales page lists 12 modules. Would this help?", 1, "Five to Nine", "Nicole")
        self.assertNotIn(("WARN", "greeting"), self.issues(fine))

    def test_the_ask(self):
        from leadsmith import lint
        self.assertEqual(lint.ask_kind(NOVEMBER), "call")
        self.assertEqual(lint.ask_kind(OCTOBER), "interest")
        self.assertEqual(lint.ask_kind("Want me to cut the first 30 seconds so you can see it?"), "sample")
        self.assertIsNone(lint.ask_kind("No question here."))
        self.assertIn(("WARN", "ask"), self.issues(lint.check("Aepsy", NOVEMBER, 1, "Aepsy", ask="interest")))
        self.assertNotIn(("WARN", "ask"), self.issues(lint.check("Aepsy", NOVEMBER, 1, "Aepsy", ask="call")))


class OfferProofTests(unittest.TestCase):
    COMPANY = ("# Company & offer\n\n## Offers\n- Lead offer: Demo films\n\n### Offer 1: Demo films\n"
               "- What the client gets: a 60-90 second product demo with script and storyboard\n"
               "- Result for their customers: understand the product before a call\n"
               "- Proof for this offer: Slashy (YC S25), two demo videos\n- Timeline: 2 weeks\n"
               "- Smallest first project: a 30-second teaser\n\n### Offer 2: Creator campaigns\n"
               "- What the client gets: creator shortlist, campaign plan, creator videos\n"
               "- Result for their customers: hear about the product from creators they trust\n"
               "- Proof for this offer: none yet\n- Timeline: 4 weeks\n- Smallest first project: a creator shortlist\n\n"
               "## Proof\n- Slashy (YC S25): two demo videos\n")

    def setUp(self):
        from leadsmith import brain
        self.brain = brain
        config.ensure_home()
        self.saved = brain.read_brain()["company"]["content"]
        brain.write_brain("company", self.COMPANY)

    def tearDown(self):
        self.brain.write_brain("company", self.saved)

    def test_offers_and_their_proof(self):
        info = self.brain.offers()
        self.assertEqual([o["name"] for o in info["offers"]], ["Demo films", "Creator campaigns"])
        self.assertEqual(info["offers"][1]["proof"], None)  # "none yet"
        self.assertEqual(self.brain.offer_for()["offer"], "Demo films")  # no brief: the lead offer
        campaign = self.brain.offer_for("Find 10 startups interested in launching creator marketing campaigns on Instagram")
        self.assertEqual(campaign["offer"], "Creator campaigns")
        self.assertFalse(campaign["has_proof"])

    def test_the_writer_is_told_what_it_may_claim(self):
        from leadsmith import jobs
        db.upsert_account("campaign-lead.com", name="Campaign Lead")
        db.set_brief(["campaign-lead.com"], "Find startups that want creator campaigns with influencers")
        line = jobs._offer_line(["campaign-lead.com"])
        self.assertIn("Creator campaigns", line)
        self.assertIn("NO proof", line)
        db.upsert_account("demo-lead.com", name="Demo Lead")
        line = jobs._offer_line(["demo-lead.com"])
        self.assertIn("Slashy", line)
        _, prompt = jobs._fmt("write", {"domains": ["campaign-lead.com"]})
        self.assertIn("NO proof", prompt)


class IdeaSwapTests(unittest.TestCase):
    def test_an_idea_reused_for_another_company_is_flagged(self):
        from leadsmith import server
        db.upsert_account("first-co.com", name="First Co")
        db.upsert_account("second-co.com", name="Second Co")
        idea = {"title": "Creator-led short-form series", "pitch": "We plan the campaign, pick creators and shoot short videos",
                "why": "hiring", "format": "6-8 videos"}
        server.save_ideas("first-co.com", [idea])
        result = server.save_ideas("second-co.com", [idea, {"title": "Animated walkthrough of the Second Co checkout",
                                                            "pitch": "Two films of the checkout flow for the pricing page"}])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("first-co.com", result["warnings"][0])
        self.assertIn("Swap test", result["hint"])

    def test_first_emails_are_tagged(self):
        import json as _json
        from leadsmith import server
        contact, _ = db.upsert_contact("tagged-co.com", first_name="Ada", email="ada@tagged-co.com", email_status="public")
        db.upsert_account("tagged-co.com", name="Tagged Co")
        saved = server.save_draft(contact["id"], 1, "Hi Ada,\n\nThe Tagged Co pricing page hides the product. Would a "
                                  "walkthrough help?", subject="tagged co pricing", opener="gap")
        with db.connect() as conn:
            tags = _json.loads(conn.execute("SELECT tags FROM drafts WHERE id = ?", (saved["draft_id"],)).fetchone()[0])
        self.assertEqual(tags["opener"], "gap")
        self.assertEqual(tags["ask"], "interest")


class WhatWorksTests(unittest.TestCase):
    def test_reply_rates_count_people_only(self):
        import json as _json
        with db.connect() as conn:
            conn.execute("DELETE FROM drafts WHERE status = 'sent' AND step = 1")
        rows = [("works-a.com", "gap", "Would this help?", "replied", "reply", HUMAN_YES),
                ("works-b.com", "gap", "Would this help?", "contacted", None, None),
                ("works-c.com", "news", "Would a short call this week help?", "replied", "reply", OOO_DATES),  # stored before
                ("works-d.com", "news", "Would a short call this week help?", "contacted", "auto", TICKET),
                ("works-e.com", None, "Would a short call this week help?", "disqualified", "not_interested", "No thanks")]
        for domain, opener, ask, status, kind, reply in rows:
            contact, _ = db.upsert_contact(domain, first_name="P", email=f"p@{domain}")
            draft = db.upsert_draft(contact["id"], 1, "s", f"Hi P,\n\nThe page has a gap. {ask}", "email", 0, {"ok": True})
            with db.connect() as conn:
                conn.execute("UPDATE drafts SET status = 'sent', sent_at = '2026-11-10T10:00:00+00:00', tags = ?, attach_deck = 1"
                             " WHERE id = ?", (_json.dumps({"opener": opener}) if opener else None, draft["id"]))
                conn.execute("UPDATE contacts SET status = ?, reply_kind = ?, last_reply = ? WHERE id = ?",
                             (status, kind, reply, contact["id"]))
        stats = db.email_stats()
        self.assertEqual((stats["sent"], stats["replied"]), (5, 2))  # the out-of-office and the ticket don't count
        opener = {c["value"]: c for c in stats["dimensions"]["opener"]}
        self.assertEqual((opener["gap"]["sent"], opener["gap"]["replied"], opener["gap"]["rate"]), (2, 1, 0.5))
        self.assertEqual(opener["news"]["replied"], 0)
        self.assertEqual(opener["untagged"]["replied"], 1)  # "no thanks" is a person replying
        self.assertEqual(opener["untagged"]["positive"], 0)
        ask = {c["value"]: c for c in stats["dimensions"]["ask"]}
        self.assertEqual((ask["interest"]["sent"], ask["call"]["sent"]), (2, 3))
        self.assertFalse(ask["call"]["enough"])
        self.assertEqual(stats["dimensions"]["month"][0]["value"], "2026-11")
        self.assertIn("by_opener", db.results_so_far()["emails"])


GOOD_DECK = [
    {"layout": "cover", "heading": "A 45-second payouts walkthrough for Lumen", "body": "So developers see a payout land."},
    {"layout": "insight", "heading": "The Lumen payouts page explains, but never shows", "highlight": "0 videos",
     "body": "Docs and a pricing table, and nothing showing a payout landing in under a minute."},
    {"layout": "concept", "heading": "What we'd make", "body": "One film, cut three ways.",
     "bullets": ["45-second hero film", "Three 15-second launch clips", "Captions for sound-off"]},
    {"layout": "sample", "heading": "The first three shots", "frames": [
        "Shot 1: a developer pastes one API call", "Shot 2: the status flips to sent", {"title": "Shot 3", "text": "the money lands"}]},
    {"layout": "plan", "heading": "Two weeks", "body": "One walkthrough call from you.",
     "steps": ["Week 1: script and storyboard", {"label": "Week 2", "detail": "production and cutdowns"}]},
    {"layout": "proof", "heading": "Work we've done", "bullets": ["Launch demos for Slashy (YC S25)"]},
]


class DeckTests(unittest.TestCase):
    def setUp(self):
        from leadsmith import decks
        self.decks = decks
        db.upsert_account("lumen-pay.com", name="Lumen")
        patches = [mock.patch.object(decks, "prospect_brand", return_value={"color": "#0b6e4f", "logo": None}),
                   mock.patch.object(decks, "site_screenshot", return_value="data:image/png;base64,AAAA"),
                   mock.patch.object(decks, "find_browser", return_value=None)]  # no Chrome in tests: HTML only
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_layouts_frames_and_steps(self):
        slides, problem = self.decks.normalize_slides(GOOD_DECK)
        self.assertIsNone(problem)
        self.assertEqual(slides[3]["frames"][2], "Shot 3: the money lands")
        self.assertEqual(slides[4]["steps"], ["Week 1: script and storyboard", "Week 2: production and cutdowns"])
        self.assertEqual([self.decks.layout_of(s, i, 6) for i, s in enumerate(slides)],
                         ["cover", "insight", "concept", "sample", "plan", "proof"])
        # without layouts, position and content decide
        plain = [{"heading": "a", "body": "b"}, {"heading": "c", "body": "d"}, {"heading": "e", "frames": ["f"]},
                 {"heading": "g", "steps": ["h"]}, {"heading": "i", "body": "j"}]
        self.assertEqual([self.decks.layout_of(s, i, 5) for i, s in enumerate(plain)],
                         ["cover", "insight", "sample", "plan", "next"])

    def test_wordy_and_placeholder_slides_are_refused(self):
        _, problem = self.decks.normalize_slides([GOOD_DECK[0], {"heading": "Long", "body": "word " * 95}, GOOD_DECK[2]])
        self.assertIn("under 90", problem)
        _, problem = self.decks.normalize_slides([GOOD_DECK[0], {"heading": "Next", "body": "<the smallest first project>"},
                                                  GOOD_DECK[2]])
        self.assertIn("placeholder", problem)

    def test_a_deck_needs_a_sample_and_the_company(self):
        no_sample = [s for s in GOOD_DECK if s["layout"] != "sample"]
        self.assertIn("sample slide", self.decks.build("lumen-pay.com", "Lumen × Studio", no_sample)["error"])
        anonymous = [{**s, "heading": s["heading"].replace("Lumen", "the")} for s in GOOD_DECK]
        self.assertIn("never names Lumen", self.decks.build("lumen-pay.com", "Payouts film", anonymous)["error"])

    def test_render_uses_their_brand_your_work_and_their_site(self):
        folder = self.decks.portfolio_dir()
        folder.mkdir(parents=True, exist_ok=True)
        png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                            "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
        (folder / "logo.png").write_bytes(png)
        (folder / "payouts-demo.png").write_bytes(png)
        (folder / "bakery-reel.png").write_bytes(png)
        (folder / "captions.txt").write_text("payouts-demo.png: Payouts demo film for a fintech\n", encoding="utf-8")
        try:
            result = self.decks.build("lumen-pay.com", "Lumen × Studio: payouts walkthrough", GOOD_DECK)
            self.assertIsNone(result["pdf"])  # no browser in this test
            self.assertEqual(result["pages"], 6)
            self.assertEqual(result["sample"], "The first three shots")
            self.assertTrue(result["used"]["their_site"] and result["used"]["your_logo"])
            page = (config.DECK_DIR / "lumen-pay.com.html").read_text(encoding="utf-8")
            for part in ('class="page cover"', 'class="page sample"', 'class="timeline"', 'class="browser"',
                         "--accent: #0b6e4f", "Payouts demo film for a fintech", "data:image/png;base64,"):
                self.assertIn(part, page)
            self.assertLess(page.index("Payouts demo film"), page.index("Bakery reel"))  # closest caption first
            self.assertIn("zoom:.2", self.decks.preview_html("lumen-pay.com"))
            deck = db.dossier("lumen-pay.com")["deck"]
            self.assertEqual(deck["pages"], 6)
            self.assertIn("Shot 1", deck["sample"])
        finally:
            for name in ("logo.png", "payouts-demo.png", "bakery-reel.png", "captions.txt"):
                (folder / name).unlink(missing_ok=True)

    def test_a_deck_copied_from_another_company_is_refused(self):
        db.upsert_account("other-pay.com", name="Otherpay")
        copied = [{**s, "heading": s["heading"].replace("Lumen", "Otherpay")} for s in GOOD_DECK]
        for s in copied:
            if s.get("body"):
                s["body"] += " Built around the plan for the launch week and the first three weeks after it."
        with db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO decks (domain, title, slides, pdf_path, error, updated_at) VALUES (?, ?, ?, NULL, NULL, ?)",
                         ("other-pay.com", "x", __import__("json").dumps(self.decks.normalize_slides(copied)[0]), db.now()))
        self.addCleanup(self._forget_other_deck)
        mine = [{**s, "heading": s["heading"].replace("Otherpay", "Lumen")} for s in copied]
        result = self.decks.build("lumen-pay.com", "Lumen × Studio", mine)
        self.assertIn("nearly repeat the deck made for other-pay.com", result["error"])

    @staticmethod
    def _forget_other_deck():
        with db.connect() as conn:
            conn.execute("DELETE FROM decks WHERE domain = 'other-pay.com'")

    def test_brand_colours_that_wouldnt_read_are_dropped(self):
        self.assertEqual(self.decks.usable_accent("#0B6E4F"), "#0b6e4f")
        for colour in ("#ffffff", "#fafafa", "#000000", "#777777", "not a colour", None):
            self.assertIsNone(self.decks.usable_accent(colour), colour)


if __name__ == "__main__":
    unittest.main()
