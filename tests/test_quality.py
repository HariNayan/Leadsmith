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


if __name__ == "__main__":
    unittest.main()
