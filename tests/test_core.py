"""Offline tests: no network, no keys. Run with `uv run python -m unittest -v`."""

import os
import tempfile
import unittest

os.environ["LEADSMITH_HOME"] = tempfile.mkdtemp(prefix="leadsmith-test-")

from leadsmith import db, lint  # noqa: E402
from leadsmith.providers import techdetect, website  # noqa: E402


class DomainTests(unittest.TestCase):
    def test_normalize(self):
        for raw in ("https://www.Acme.com/about?x=1", "acme.com", "http://acme.com:8080/", "WWW.ACME.COM"):
            self.assertEqual(db.normalize_domain(raw), "acme.com")


class PipelineTests(unittest.TestCase):
    def test_account_signal_contact_draft_flow(self):
        account, created = db.upsert_account("https://flow.io", name="Flow", fit_score=7)
        self.assertTrue(created)
        _, created_again = db.upsert_account("flow.io", intent_score=6)
        self.assertFalse(created_again)
        self.assertTrue(db.add_signal("flow.io", "hiring", "Hiring 2 SDRs", source="ashby"))
        self.assertFalse(db.add_signal("flow.io", "hiring", "Hiring 2 SDRs"))
        contact, _ = db.upsert_contact("flow.io", first_name="Ana", last_name="Ruiz", email="Ana@Flow.io")
        same, created = db.upsert_contact("flow.io", first_name="Ana", email="ana@flow.io", title="CEO")
        self.assertFalse(created)
        self.assertEqual(contact["id"], same["id"])
        draft = db.upsert_draft(contact["id"], 1, "hi", "body", "email", 0, {"ok": True})
        self.assertEqual(draft["status"], "draft")
        dossier = db.dossier("flow.io")
        self.assertEqual(dossier["fit_score"], 7)
        self.assertEqual(dossier["intent_score"], 6)
        self.assertEqual(len(dossier["signals"]), 1)
        self.assertEqual(dossier["contacts"][0]["drafts"][0]["subject"], "hi")

    def test_suppress_cancels_drafts_by_email_domain(self):
        contact, _ = db.upsert_contact("other.app", first_name="Lee", email="lee@blocked.dev")
        draft = db.upsert_draft(contact["id"], 1, "s", "b", "email", 0, {"ok": True})
        db.set_status("draft", draft["id"], "approved")
        db.suppress("blocked.dev", "test")
        self.assertTrue(db.is_suppressed("lee@blocked.dev"))
        statuses = {d["id"]: d["status"] for d in db.drafts(None)}
        self.assertEqual(statuses[draft["id"]], "cancelled")

    def test_sent_draft_is_immutable(self):
        contact, _ = db.upsert_contact("sent.io", first_name="Kim", email="kim@sent.io")
        draft = db.upsert_draft(contact["id"], 1, "s", "b", "email", 0, {"ok": True})
        db.set_status("draft", draft["id"], "sent")
        with self.assertRaises(ValueError):
            db.upsert_draft(contact["id"], 1, "s2", "b2", "email", 0, {"ok": True})
        with self.assertRaises(ValueError):
            db.set_status("draft", draft["id"], "approved")


class LintTests(unittest.TestCase):
    GOOD = ("Hi Sam,\n\nNoticed Acme opened a Berlin office in September. Teams that expand that fast usually "
            "rebuild onboarding before anything else.\n\nIs onboarding for the Berlin team on your plate this quarter?")

    def test_good_email_passes(self):
        result = lint.check("berlin team", self.GOOD, 1, "Acme", "Sam")
        self.assertTrue(result["ok"], result)

    def test_bad_email_fails(self):
        bad = "I hope this email finds you well! We leverage AI. Click here: https://x.com {{first_name}}"
        result = lint.check("RE: Special Offer", bad, 1)
        rules = {i["rule"] for i in result["issues"] if i["level"] == "FAIL"}
        self.assertFalse(result["ok"])
        self.assertTrue({"ai-phrases", "spam-words", "links", "placeholders", "subject"} <= rules, rules)


class EmailPatternTests(unittest.TestCase):
    def test_infer_pattern_ignores_generic(self):
        from leadsmith.providers import emails

        self.assertEqual(emails.infer_pattern(["hello@x.io", "jane.doe@x.io"]), "{f}.{l}")
        self.assertEqual(emails.infer_pattern(["support@x.io", "karri@x.io"]), "{f}")
        self.assertIsNone(emails.infer_pattern(["info@x.io", "sales@x.io"]))


class SendSafetyTests(unittest.TestCase):
    """Replays the incident: dashboard closed 10 days, then Send clicked while auto follow-ups also ran."""

    def setUp(self):
        import smtplib
        import threading
        from datetime import datetime, timedelta, timezone

        from leadsmith import config, mailer

        self.mailer = mailer
        self.sent: list[str] = []
        lock = threading.Lock()
        outbox = self.sent

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def login(self, *a): pass
            def send_message(self, msg):
                import time
                time.sleep(0.05)  # widen the race window
                with lock:
                    outbox.append(f"{msg['To']}|{msg['Subject']}")
            def quit(self): pass

        self._orig_smtp, self._orig_replies = smtplib.SMTP_SSL, mailer.check_replies
        smtplib.SMTP_SSL = FakeSMTP
        mailer.check_replies = lambda days=14: {"replies": [], "bounces": []}
        config.ensure_home()  # create .env from the template first, like a real install
        config.save_env({"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "abcd", "SEND_ENABLED": "true",
                         "TEST_RECIPIENT": "", "DAILY_SEND_LIMIT": "100",
                         "SEND_HOURS": "0-24", "SEND_WEEKDAYS_ONLY": "false",
                         "WARMUP": "false"})  # neither the clock nor the warm-up is what these tests check
        ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).replace(microsecond=0).isoformat()
        import uuid

        tag = uuid.uuid4().hex[:6]
        self.addr = f"lee-{tag}@late.io"
        self.contact, _ = db.upsert_contact(f"late-{tag}.io", first_name=f"Lee{tag}", email=self.addr)
        for step, offset in ((1, 0), (2, 3), (3, 7)):
            d = db.upsert_draft(self.contact["id"], step, "hello late" if step == 1 else None, f"body {step}", "email", offset, {"ok": True})
            if step == 1:
                with db.connect() as conn:
                    conn.execute("UPDATE drafts SET status = 'sent', sent_at = ?, message_id = '<m1@x>' WHERE id = ?", (ten_days_ago, d["id"]))
                    conn.execute("UPDATE contacts SET status = 'contacted' WHERE id = ?", (self.contact["id"],))
            else:
                db.set_status("draft", d["id"], "approved")

    def tearDown(self):
        import smtplib

        from leadsmith import config

        smtplib.SMTP_SSL, self.mailer.check_replies = self._orig_smtp, self._orig_replies
        config.save_env({"SEND_ENABLED": "false", "GMAIL_APP_PASSWORD": "", "GMAIL_ADDRESS": ""})

    def test_concurrent_sends_never_duplicate_and_follow_ups_stay_spaced(self):
        import threading

        results = []
        threads = [threading.Thread(target=lambda: results.append(self.mailer.send_approved(25, False))) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        mine = [s for s in self.sent if self.addr in s]
        self.assertEqual(mine, [f"Lee{self.contact['first_name'][3:]} <{self.addr}>|Re: hello late"], mine)  # step 2 once, step 3 not yet

        # clicking Send again right away sends nothing: step 3 waits 4 days after step 2
        again = self.mailer.send_approved(25, False)
        self.assertEqual([s for s in self.sent if self.addr in s], mine)
        self.assertTrue(any("waits until" in s.get("reason", "") for s in again["skipped"]), again["skipped"])

    def _three_new_people(self):
        import uuid

        ids = []
        for n in range(3):
            tag = uuid.uuid4().hex[:6]
            contact, _ = db.upsert_contact(f"gap-{tag}.io", first_name=f"Gap{tag}", email=f"p{n}-{tag}@gap.io")
            d = db.upsert_draft(contact["id"], 1, f"hi {tag}", "body", "email", 0, {"ok": True})
            db.set_status("draft", d["id"], "approved")
            ids.append(contact["email"])
        return ids

    def test_batch_emails_are_spaced_out(self):
        import time

        emails = self._three_new_people()
        stamps = []
        original = self.mailer._build

        def build_and_time(*a, **k):
            stamps.append(time.monotonic())
            return original(*a, **k)

        self.mailer._build = build_and_time
        try:
            self.mailer.send_approved(25, False, gap_seconds=0.3)
        finally:
            self.mailer._build = original
        sent_to_them = [s for s in self.sent if any(e in s for e in emails)]
        self.assertEqual(len(sent_to_them), 3)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(g >= 0.28 for g in gaps[-2:]), gaps)

    def test_stop_halts_a_background_batch(self):
        import time

        from leadsmith import config

        emails = self._three_new_people()
        config.save_env({"SEND_GAP_SECONDS": "5"})
        try:
            self.assertTrue(self.mailer.start_background_send(25)["started"])
            for _ in range(100):  # wait for the first email to go out
                if self.mailer.PROGRESS.get("sent"):
                    break
                time.sleep(0.05)
            self.assertTrue(self.mailer.stop_send())
            for _ in range(100):
                if not self.mailer.PROGRESS.get("running"):
                    break
                time.sleep(0.05)
        finally:
            config.save_env({"SEND_GAP_SECONDS": "60"})
        self.assertFalse(self.mailer.PROGRESS["running"])
        self.assertEqual(self.mailer.PROGRESS["sent"], 1)
        with db.connect() as conn:
            left = conn.execute("SELECT COUNT(*) FROM drafts d JOIN contacts c ON c.id = d.contact_id "
                                "WHERE d.status = 'approved' AND c.email IN (?, ?, ?)", tuple(emails)).fetchone()[0]
        self.assertEqual(left, 2)  # the rest stay approved for next time

    def test_reply_draft_sends_outside_hours_in_the_thread(self):
        from leadsmith import config, jobs

        with db.connect() as conn:
            conn.execute("UPDATE contacts SET status = 'replied', last_reply = 'B please, what is the timeline?' WHERE id = ?",
                         (self.contact["id"],))
            conn.execute("UPDATE drafts SET status = 'cancelled' WHERE contact_id = ? AND step > 1", (self.contact["id"],))
        draft = db.save_reply_draft(self.contact["id"], "Hi Lee,\n\nFor B I can deliver in about a week.", {"ok": True})
        self.assertEqual((draft["kind"], draft["step"], draft["subject"]), ("reply", 10, "hello late"))
        db.set_status("draft", draft["id"], "approved")

        config.save_env({"SEND_HOURS": "3-4", "SEND_WEEKDAYS_ONLY": "true"})  # the cold window is shut
        try:
            auto = self.mailer.send_approved(25, False, True, 2)  # automatic runs never send replies
            self.assertEqual([x for x in self.sent if self.addr in x], [])
            self.mailer.send_approved(25, False)
        finally:
            config.save_env({"SEND_HOURS": "0-24", "SEND_WEEKDAYS_ONLY": "false"})
        self.assertEqual([x for x in self.sent if self.addr in x], [f"Lee{self.contact['first_name'][3:]} <{self.addr}>|Re: hello late"])
        self.assertEqual(db.get_contact(self.contact["id"])["status"], "replied")
        title, prompt = jobs._fmt("reply", {"contact_id": self.contact["id"]})
        self.assertIn("B please, what is the timeline?", prompt)
        self.assertIn(f"contact_id={self.contact['id']}", prompt)

    def test_automatic_runs_hold_back_late_follow_ups(self):
        report = self.mailer.send_approved(25, False, True, 2)
        self.assertEqual([s for s in self.sent if self.addr in s], [])
        self.assertIn(2, [o["step"] for o in report["overdue"] if o["to"] == self.addr])


class SendingWindowTests(unittest.TestCase):
    def test_guesses_zones_from_locations(self):
        from leadsmith import schedule

        self.assertEqual(schedule.guess_timezone("Pune, India"), "Asia/Kolkata")
        self.assertEqual(schedule.guess_timezone("San Francisco, CA"), "America/Los_Angeles")
        self.assertEqual(schedule.guess_timezone("Brooklyn, NY"), "America/New_York")
        self.assertIsNone(schedule.guess_timezone("Remote"))

    def test_weekend_and_night_are_closed(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        from leadsmith import config, schedule

        config.save_env({"SEND_HOURS": "9-17", "SEND_WEEKDAYS_ONLY": "true"})
        sf = ZoneInfo("America/Los_Angeles")
        saturday_afternoon = datetime(2026, 10, 17, 21, 26, tzinfo=timezone.utc)  # the incident, Sat 14:26 in SF
        self.assertFalse(schedule.in_window(saturday_afternoon, sf))
        monday_9am = schedule.next_window_start(saturday_afternoon, sf).astimezone(sf)
        self.assertEqual((monday_9am.strftime("%a"), monday_9am.hour), ("Mon", 9))
        self.assertTrue(schedule.in_window(datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc), sf))  # Mon 10:00 SF
        self.assertFalse(schedule.in_window(datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc), sf))  # Tue 02:00 SF

    def test_follow_up_gaps_count_business_days(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        from leadsmith import schedule

        ny = ZoneInfo("America/New_York")
        friday = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(schedule.add_business_days(friday, 3, ny).astimezone(ny).strftime("%a"), "Wed")


class OptOutTests(unittest.TestCase):
    def test_reply_sorting(self):
        from leadsmith import mailer

        cases = {"no": "optout", "Please remove me from your list": "optout", "stop emailing me": "optout",
                 "No thanks": "not_interested", "We are all set with video": "not_interested",
                 "B sounds great": "reply", "Who else do you work with? No rush.": "reply"}
        for text, want in cases.items():
            self.assertEqual(mailer.classify_reply(text), want, text)
        self.assertEqual(mailer.classify_reply("", "Unsubscribe"), "optout")

    def test_every_email_has_an_unsubscribe_header(self):
        from leadsmith import config, mailer

        config.save_env({"GMAIL_ADDRESS": "me@gmail.com", "TEST_RECIPIENT": ""})
        contact, _ = db.upsert_contact("unsub.io", first_name="Uma", email="uma@unsub.io")
        msg = mailer._build({"subject": "hi", "body": "Hello", "attach_deck": 0}, contact, None)
        self.assertEqual(msg["List-Unsubscribe"], "<mailto:me@gmail.com?subject=unsubscribe>")


class FeedbackTests(unittest.TestCase):
    def test_not_a_fit_disqualifies_and_reaches_the_agents(self):
        from leadsmith import server

        db.upsert_account("toobig.io", name="TooBig", status="researched", source="yc")
        contact, _ = db.upsert_contact("toobig.io", first_name="Tia", email="tia@toobig.io")
        draft = db.upsert_draft(contact["id"], 1, "hi", "body", "email", 0, {"ok": True})
        db.set_feedback("toobig.io", "bad", "too big, has an in-house video team")
        dossier = db.dossier("toobig.io")
        self.assertEqual((dossier["status"], dossier["feedback"]), ("disqualified", "bad"))
        self.assertEqual({d["id"]: d["status"] for d in db.drafts(None)}[draft["id"]], "cancelled")
        verdicts = server.brain_read()["lead_feedback"]["verdicts"]
        self.assertIn(("TooBig", "bad", "too big, has an in-house video team"),
                      [(v["name"], v["verdict"], v["reason"]) for v in verdicts])

    def test_clearing_feedback(self):
        db.upsert_account("maybe.io", name="Maybe")
        db.set_feedback("maybe.io", "good", "right stage")
        db.set_feedback("maybe.io", None)
        self.assertIsNone(db.dossier("maybe.io")["feedback"])


class SourceStatsTests(unittest.TestCase):
    def test_groups_sources_and_counts_outcomes(self):
        import uuid

        tag = uuid.uuid4().hex[:5]
        for n, (source, verdict) in enumerate([(f"play{tag} batch one", "good"), (f"play{tag}: second", None), (f"other{tag}", "bad")]):
            db.upsert_account(f"s{n}-{tag}.io", name=f"S{n}", source=source)
            if verdict:
                db.set_feedback(f"s{n}-{tag}.io", verdict)
        stats = {r["source"]: r for r in db.source_stats()}
        self.assertEqual((stats[f"play{tag}"]["leads"], stats[f"play{tag}"]["good"]), (2, 1))
        self.assertEqual(stats[f"other{tag}"]["bad"], 1)


class ExportTests(unittest.TestCase):
    def test_sequence_export_uses_import_columns_and_skips_replies(self):
        import csv
        import uuid

        from leadsmith import server

        tag = uuid.uuid4().hex[:5]
        domain = f"exp-{tag}.io"
        db.upsert_account(domain, name="Exp", why_now="Raised a seed round")
        db.save_ideas(domain, [{"title": "Walkthrough", "pitch": "45s video"}, {"title": "Clips"}])
        c, _ = db.upsert_contact(domain, first_name="Ana", email=f"ana@{domain}")
        db.upsert_draft(c["id"], 1, "ideas", "Hi Ana", "email", 0, {"ok": True})
        db.upsert_draft(c["id"], 2, None, "Follow up", "email", 0, {"ok": True})
        db.save_reply_draft(c["id"], "Thanks!", {"ok": True})
        with db.connect() as conn:
            conn.execute("UPDATE drafts SET status = 'approved' WHERE contact_id = ?", (c["id"],))
        result = server.export_csv("sequences", "approved")
        self.assertEqual(result["columns"][:8], server.IMPORT_COLUMNS)
        with open(result["path"], encoding="utf-8") as fh:
            row = next(r for r in csv.DictReader(fh) if r["email"] == f"ana@{domain}")
        self.assertEqual((row["website"], row["reason"]), (f"https://{domain}", "Raised a seed round"))
        self.assertEqual((row["idea_a"], row["step1_subject"], row["step2_body"]), ("Walkthrough: 45s video", "ideas", "Follow up"))
        self.assertNotIn("Thanks!", row.values())


class StepTemplateTests(unittest.TestCase):
    def tearDown(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM step_templates")

    def test_fixed_template_fills_lead_fields_and_leaves_custom_ones(self):
        import uuid

        from leadsmith import templates

        tag = uuid.uuid4().hex[:5]
        domain = f"tpl-{tag}.io"
        db.upsert_account(domain, name="Tpl Co", why_now="Launched v2")
        db.save_ideas(domain, [{"title": "Walkthrough", "pitch": "45s video"}, {"title": "Clips"}])
        c, _ = db.upsert_contact(domain, first_name="", email=f"x@{domain}")
        db.save_step_template(1, "fixed", "ideas for {company}", "Hi {first_name},\n{personal_line}\n{ideas}\n{idea_c}")
        out = templates.render(c["id"], 1)
        self.assertEqual(out["subject"], "ideas for Tpl Co")
        self.assertIn("Hi there,", out["body"])
        self.assertIn("A) Walkthrough: 45s video\nB) Clips", out["body"])
        self.assertEqual((out["fill_these"], out["empty"]), (["personal_line"], ["idea_c"]))

    def test_ai_steps_pass_instruction_and_only_changed_steps_reach_the_agent(self):
        from leadsmith import templates

        db.save_step_template(2, "ai", instruction="Add a one-line case study")
        self.assertEqual([t["step"] for t in templates.customised()], [2])
        self.assertEqual(templates.render(1, 2), {"step": 2, "mode": "ai", "instruction": "Add a one-line case study"})
        with self.assertRaises(ValueError):
            db.save_step_template(3, "fixed", body="  ")


class BetterContactTests(unittest.TestCase):
    def test_off_without_key_and_parses_a_finished_lookup(self):
        from unittest import mock

        from leadsmith.providers import bettercontact

        with mock.patch.dict(os.environ, {"BETTERCONTACT_API_KEY": ""}):
            self.assertFalse(bettercontact.enabled())
            with self.assertRaises(RuntimeError):
                bettercontact.find_email("Ana", "Ruiz", "acme.io")

        class Resp:
            def __init__(self, code, data):
                self.status_code, self._data, self.content, self.text = code, data, b"x" if data else b"", ""

            def json(self):
                return self._data

            def raise_for_status(self):
                pass

        done = {"status": "terminated", "credits_consumed": 1, "credits_left": 99,
                "data": [{"contact_email_address": "Ana@Acme.io", "contact_email_address_status": "deliverable"}]}
        gets = iter([Resp(202, None), Resp(200, done)])
        with mock.patch.dict(os.environ, {"BETTERCONTACT_API_KEY": "k"}), \
             mock.patch.object(bettercontact.httpx, "post", return_value=Resp(201, {"id": "req1"})) as post, \
             mock.patch.object(bettercontact.httpx, "get", side_effect=lambda *a, **k: next(gets)), \
             mock.patch.object(bettercontact.time, "sleep"):
            r = bettercontact.find_email("Ana", "Ruiz", "acme.io")
        self.assertEqual(post.call_count, 1)
        self.assertEqual((r["email"], r["verified"], r["credits_left"]), ("ana@acme.io", True, 99))
        self.assertFalse(bettercontact.parse({"data": [{"contact_email_address_status": "catch_all"}]}, "x")["verified"])

    def test_set_contact_email_refuses_someone_elses_address(self):
        import uuid

        tag = uuid.uuid4().hex[:5]
        a, _ = db.upsert_contact(f"v-{tag}.io", first_name="A", email=f"a@v-{tag}.io")
        b, _ = db.upsert_contact(f"v-{tag}.io", first_name="B", email=f"b@v-{tag}.io", email_status="guessed")
        with self.assertRaises(ValueError):
            db.set_contact_email(b["id"], f"a@v-{tag}.io", "verified")
        self.assertEqual(db.set_contact_email(b["id"], f"bee@v-{tag}.io", "verified")["email_status"], "verified")


class RenameMigrationTests(unittest.TestCase):
    def test_old_gtm_brain_folder_and_database_move_to_leadsmith(self):
        from pathlib import Path
        from unittest import mock

        from leadsmith import config

        home = Path(tempfile.mkdtemp())
        old = home / ".gtm-brain"
        (old / "brain").mkdir(parents=True)
        (old / "gtm.db").write_text("data")
        (old / ".env").write_text("GMAIL_ADDRESS=me@gmail.com")
        with mock.patch.dict(os.environ, {"LEADSMITH_HOME": "", "GTM_HOME": ""}), \
             mock.patch.object(config.Path, "home", return_value=home):
            moved = config._home()
            self.assertEqual(moved, home / ".leadsmith")
            self.assertEqual((moved / "leadsmith.db").read_text(), "data")
            self.assertTrue((moved / ".env").exists() and (moved / "brain").is_dir())
            self.assertFalse(old.exists())
            self.assertEqual(config._home(), moved)  # second start: nothing to do


class ComplianceTests(unittest.TestCase):
    def test_region_from_zone_location_then_country_domain(self):
        from leadsmith import compliance

        code = lambda account, email=None: (compliance.region(account, email) or {}).get("code")  # noqa: E731
        self.assertEqual(code({"timezone": "Europe/Berlin"}), "EU")
        self.assertEqual(code({"location": "London, UK"}), "UK")
        self.assertEqual(code({"timezone": "Europe/Zurich"}), "CH")
        self.assertEqual(code({"location": "Toronto, Canada"}), "CA")
        self.assertEqual(code({"domain": "acme.de"}), "EU")
        self.assertEqual(code({"domain": "acme.io"}, "ana@acme.co.uk"), "UK")
        # a known zone outside the strict regions wins over a European-looking domain
        self.assertIsNone(code({"timezone": "America/New_York", "domain": "acme.de"}))
        self.assertIsNone(code({"location": "Pune, India", "domain": "acme.io"}))


class WarmupTests(unittest.TestCase):
    def test_ramp_grows_daily_up_to_the_maximum(self):
        from leadsmith import mailer

        self.assertEqual([mailer.warmup_limit(d, 5, 3, 25) for d in (0, 1, 2, 6, 7, 30)], [5, 8, 11, 23, 25, 25])

    def test_warmup_off_uses_the_maximum(self):
        from leadsmith import config, mailer

        config.save_env({"WARMUP": "false", "DAILY_SEND_LIMIT": "40"})
        try:
            self.assertEqual(mailer.daily_limit()["today"], 40)
        finally:
            config.save_env({"WARMUP": "true", "DAILY_SEND_LIMIT": "100"})


class IdeaMenuTests(unittest.TestCase):
    def test_save_pick_and_regenerate_keeps_picked(self):
        first = db.save_ideas("menu.io", [{"title": "Walkthrough video"}, {"title": "Launch clips"}, {"title": "Explainer"}])
        self.assertEqual([i["letter"] for i in first], ["A", "B", "C"])
        db.pick_idea(first[1]["id"])
        again = db.save_ideas("menu.io", [{"title": "Case study film"}, {"title": "Founder story"}, {"title": "Ad cutdowns"}])
        picked = [i for i in again if i["status"] == "picked"]
        self.assertEqual([(i["letter"], i["title"]) for i in picked], [("B", "Launch clips")])
        self.assertEqual(sorted(i["letter"] for i in again), ["A", "B", "C", "D"])

    def test_pick_moves_to_new_idea(self):
        ideas = db.save_ideas("move.io", [{"title": "One"}, {"title": "Two"}])
        db.pick_idea(ideas[0]["id"])
        db.pick_idea(ideas[1]["id"])
        statuses = {i["letter"]: i["status"] for i in db.dossier("move.io")["ideas"]}
        self.assertEqual(statuses, {"A": "offered", "B": "picked"})

    def test_reply_pick_guessing(self):
        ideas = db.save_ideas("guess.io", [{"title": "Signals walkthrough"}, {"title": "Launch clips"}, {"title": "dbt explainer"}])
        cases = {"B please!": "B", "Option C sounds good": "C", "Love the launch clips idea": "B", "Let's go with A.": "A",
                 "Thanks, a quick question about pricing": None, "A or B": None, "Not now, thanks": None}
        for reply, want in cases.items():
            self.assertEqual((db.guess_pick(reply, ideas) or {}).get("letter"), want, reply)

    def test_idea_menu_email_passes_lint(self):
        body = ("Hi Rick,\n\nSaw Databench hit #4 on Product Hunt with only static screenshots.\n\n"
                "Three things I could make for you:\nA) Signals agent in action: a 45-sec homepage walkthrough\n"
                "B) Launch cutdowns: three vertical clips for LinkedIn and X\nC) dbt plus human explainer: a 60-sec diagram\n\n"
                "Which one should I make first: A, B or C?")
        self.assertTrue(lint.check("databench in motion", body, 1, "Databench", "Rick")["ok"])


class PublishedEmailTests(unittest.TestCase):
    def test_extracts_plain_mailto_and_disguised(self):
        from leadsmith.providers import emails

        text = ('Write to <a href="mailto:Ana.Ruiz@Acme.io">me</a>, or jane [at] acme [dot] com, or ravi (at) sweets.co.in. '
                "logo@2x.png placeholder@example.com")
        self.assertEqual(emails.extract_emails(text), ["ana.ruiz@acme.io", "jane@acme.com", "ravi@sweets.co.in"])
        # spelled-out form works, ordinary prose doesn't
        self.assertEqual(emails.extract_emails("mail priya at studio dot in"), ["priya@studio.in"])
        self.assertEqual(emails.extract_emails("He was director at Eventbrite. Simon now runs Datasette."), [])

    def test_linkedin_is_never_fetched(self):
        import asyncio

        from leadsmith.providers import emails

        result = asyncio.run(emails.emails_on_page("https://www.linkedin.com/in/someone", "Some", "One"))
        self.assertEqual(result["emails"], [])
        self.assertIn("logged-in", result["note"])

    def test_name_matching(self):
        from leadsmith.providers import emails

        self.assertTrue(emails._name_match("karri@linear.app", "Karri", "Saarinen"))
        self.assertTrue(emails._name_match("ksaarinen@linear.app", "Karri", "Saarinen"))
        self.assertFalse(emails._name_match("hello@linear.app", "Karri", "Saarinen"))


class SettingsTests(unittest.TestCase):
    def test_save_env_keeps_comments_and_hides_secrets(self):
        from leadsmith import config

        config.ensure_home()
        config.save_env({"SENDER_NAME": "Priya", "GMAIL_APP_PASSWORD": "abcd efgh", "SENDER_SIGNATURE": "Priya\nFounder"})
        text = config.ENV_FILE.read_text(encoding="utf-8")
        self.assertIn("# Leadsmith settings", text)
        self.assertIn("SENDER_NAME=Priya", text)
        self.assertIn("SENDER_SIGNATURE=Priya\\nFounder", text)
        public = config.public_settings()
        self.assertEqual(public["GMAIL_APP_PASSWORD"]["value"], "")
        self.assertTrue(public["GMAIL_APP_PASSWORD"]["is_set"])


class SuggestionTests(unittest.TestCase):
    def test_template_profile_asks_for_details(self):
        from leadsmith import brain, config

        config.ensure_home()
        for name in ("icp", "company", "personas"):
            brain.write_brain(name, (config.TEMPLATES / "brain" / f"{name}.md").read_text(encoding="utf-8"))
        result = brain.suggest_searches()
        self.assertEqual(result["suggestions"], [])
        self.assertIn("Business profile", result["missing"])

    def test_filled_profile_gives_signal_searches(self):
        from leadsmith import brain

        brain.write_brain("icp", "# ICP\n## Firmographics (fit)\n- Industries / keywords: restaurants and cafes\n"
                                 "- Company size (employees): 5-50\n- Locations: Pune, India\n"
                                 "- Tech they use that makes us a fit: Zomato, Swiggy\n"
                                 "## Buying signals (intent) — weight 1-5\n- 5 — Opened a new branch in the last 90 days\n"
                                 "- 3 — Website is outdated or missing (no online menu)\n- 4 — TODO e.g. something\n")
        brain.write_brain("personas", "## Persona 1: Owner\n- Titles to search: Owner, Founder, Manager\n")
        result = brain.suggest_searches()
        queries = [s["query"] for s in result["suggestions"]]
        self.assertIsNone(result["missing"])
        self.assertTrue(queries[0].startswith("restaurants and cafes in Pune, India with 5-50 employees showing: opened a new branch"), queries[0])
        self.assertTrue(all("Reach the Owner, Founder, Manager" in q for q in queries))
        self.assertTrue(any("that use Zomato, Swiggy" in q for q in queries))
        self.assertFalse(any("TODO" in q or "e.g." in q for q in queries))

    def test_long_profile_values_become_short_searches(self):
        from leadsmith import brain

        brain.write_brain("icp", "## Firmographics (fit)\n- Industries / keywords: early-stage startups shipping a product "
                                 "(B2B SaaS, AI tools); creators with real audiences (YouTube, Shorts)\n"
                                 "- Company size (employees): 1-50 for startups (assumption: small teams); solo creators\n"
                                 "## Buying signals\n- 5 — Raised funding recently\n")
        brain.write_brain("personas", "- Titles to search: Founder, Co-founder, CEO, Head of Marketing, Head of Growth\n")
        queries = [s["query"] for s in brain.suggest_searches()["suggestions"]]
        self.assertEqual(queries[0], "early-stage startups shipping a product with 1-50 employees showing: raised funding "
                                     "recently. Reach the Founder, Co-founder, CEO")
        self.assertTrue(any(q.startswith("creators with real audiences") for q in queries))
        self.assertTrue(all(len(q) < 200 for q in queries), queries)


class ModelNameTests(unittest.TestCase):
    def test_friendly_model_names_become_valid_ids(self):
        from leadsmith import config

        cases = {"sonnet 5.5": "claude-sonnet-5-5", "Sonnet 5.5": "claude-sonnet-5-5", "claude opus 5.5": "claude-opus-5-5",
                 "opus": "opus", "Haiku": "haiku", "claude-sonnet-5-5": "claude-sonnet-5-5", "": "", "fable 5.1": "claude-fable-5-1"}
        for raw, expected in cases.items():
            self.assertEqual(config.normalize_model(raw), expected, raw)
        with self.assertRaises(ValueError):
            config.normalize_model("best model please")


class LintAcronymTests(unittest.TestCase):
    def test_business_acronyms_are_not_shouting(self):
        result = lint.check("emea team", "Hi Jo,\n\nSaw the new EMEA office and the GDPR work. Is onboarding next?", 1)
        self.assertNotIn("caps", {i["rule"] for i in result["issues"]})


class GmailFriendlyTests(unittest.TestCase):
    def client(self):
        from starlette.testclient import TestClient

        from leadsmith.dashboard import create_app

        return TestClient(create_app(4600), base_url="http://127.0.0.1:4600", headers={"X-Leadsmith-Client": "1"})

    def test_csv_keeps_gmail_only_businesses(self):
        csv_text = ("Business,Name,Email,Website\n"
                    "Sharma Sweets,Ravi Sharma,sharmasweets@gmail.com,\n"
                    "Acme,Ana Ruiz,ana@acme-csv.io,\n"
                    "Globex,Li Wei,li@gmail.com,globex-csv.com\n")
        r = self.client().post("/api/import", json={"csv": csv_text}).json()
        self.assertEqual(r["skipped"], 0, r)
        self.assertIn("sharma-sweets.no-website", r["domains"])
        self.assertIn("acme-csv.io", r["domains"])
        self.assertIn("globex-csv.com", r["domains"])
        sharma = db.dossier("sharma-sweets.no-website")
        self.assertEqual(sharma["contacts"][0]["email"], "sharmasweets@gmail.com")

    def test_add_companies_accepts_names_and_gmail(self):
        r = self.client().post("/api/accounts", json={"domains": "Patel Tiles, Surat\nowner.bakery@gmail.com\nfoo-add.io"}).json()
        self.assertEqual(sorted(r["added"]), sorted(["patel-tiles-surat.no-website", "owner-bakery.no-website", "foo-add.io"]))

    def test_dashboard_rejects_cross_site_posts(self):
        from starlette.testclient import TestClient

        from leadsmith.dashboard import create_app

        bare = TestClient(create_app(4600), base_url="http://127.0.0.1:4600")
        self.assertEqual(bare.post("/api/send", json={}).status_code, 403)


class TestModeTests(unittest.TestCase):
    def test_test_mode_redirects_and_marks(self):
        from leadsmith import config, mailer

        config.save_env({"TEST_RECIPIENT": "me.test@gmail.com", "GMAIL_ADDRESS": "me@gmail.com"})
        try:
            contact, _ = db.upsert_contact("tm.io", first_name="Pat", last_name="Lee", email="pat@tm.io")
            draft = {"subject": "hello there", "body": "Hi Pat", "attach_deck": 0}
            msg = mailer._build(draft, contact, None)
            self.assertEqual(msg["To"], "me.test@gmail.com")
            self.assertTrue(msg["Subject"].startswith("[TEST] "))
            self.assertIn("pat@tm.io", msg.get_content())
            follow = mailer._build({"subject": None, "body": "Again", "attach_deck": 0}, contact,
                                   {"subject": "hello there", "message_id": "<x@y>"})
            self.assertEqual(follow["Subject"], "Re: [TEST] hello there")
        finally:
            config.save_env({"TEST_RECIPIENT": ""})
        self.assertIsNone(mailer.test_recipient())


class FreeProviderParsingTests(unittest.TestCase):
    def test_robots_blocking(self):
        robots = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\nDisallow: /admin\n"
        self.assertEqual(website.blocked_crawlers(robots), ["GPTBot"])

    def test_techdetect_is_specific(self):
        html = '<script src="https://js.hs-scripts.com/1.js"></script><p>We integrate with WooCommerce</p>'
        found = techdetect.detect(html)
        self.assertIn("HubSpot", found.get("marketing", []))
        self.assertNotIn("ecommerce", found)


if __name__ == "__main__":
    unittest.main()
