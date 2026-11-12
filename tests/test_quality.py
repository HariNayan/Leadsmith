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
        self.assertEqual([m["question"] for m in voice], ["Sign-off?", "Add your own banned words?"])
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


if __name__ == "__main__":
    unittest.main()
