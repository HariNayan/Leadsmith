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


class EngineTests(unittest.TestCase):
    """Each engine is started headless with the Leadsmith tools, never with shell/edit or the send tools."""

    def test_every_engine_blocks_sending_and_writing(self):
        import json as _json
        from unittest import mock

        from leadsmith import config, engines

        # OpenCode 1's config format (OpenCode 2 blocks these server-side: see OpenCodeV2Tests).
        v1 = mock.patch.object(type(engines.ENGINES["opencode"]), "version", lambda self: (1, 0, 0))
        v1.start()
        self.addCleanup(v1.stop)
        for key, engine in engines.ENGINES.items():
            with mock.patch.object(engine, "path", return_value=key):
                cmd, env = engine.command("")
            blob = " ".join(cmd) + " " + " ".join(env.values())
            for tool in ("review_drafts", "send_approved_emails"):
                self.assertIn(tool, blob + (config.HOME / ".gemini" / "settings.json").read_text() if key == "gemini" else blob, key)
        for session in (None, "abc"):  # new and continued sessions are both read-only
            self.assertIn('sandbox_mode="read-only"', engines.ENGINES["codex"].command("", session)[0])
        oc = _json.loads(engines.ENGINES["opencode"].command("")[1]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual((oc["permission"]["bash"], oc["permission"]["edit"]), ("deny", "deny"))
        gem = _json.loads((config.HOME / ".gemini" / "settings.json").read_text())
        self.assertIn("run_shell_command", gem["tools"]["exclude"])
        claude = engines.ENGINES["claude"].command("")[0]
        self.assertIn("Bash", claude[claude.index("--disallowedTools") + 1])

    def test_parsers_read_each_cli_output(self):
        from leadsmith import engines

        codex = engines.ENGINES["codex"]
        started = codex.parse({"type": "item.started", "item": {"type": "mcp_tool_call", "tool": "leadsmith_status", "arguments": {}}})
        self.assertTrue(started.log.startswith("→ leadsmith_status") or started.log.startswith("→ status"))
        self.assertEqual(codex.parse({"type": "item.completed", "item": {"type": "agent_message", "text": "8"}}).result, "8")

        oc = engines.ENGINES["opencode"]
        tool = oc.parse({"type": "tool_use", "part": {"tool": "leadsmith_save_account", "state": {"status": "completed", "input": {"domain": "acme.io"}}}})
        self.assertEqual(tool.log, "→ save_account acme.io\n")
        self.assertEqual(oc.parse({"type": "step_finish", "part": {"cost": 0.25}}).cost, 0.25)

        gem = engines.ENGINES["gemini"]
        self.assertIn("→ save_signal", gem.parse({"type": "tool_use", "tool_name": "mcp_leadsmith_save_signal", "parameters": {}}).log)
        self.assertEqual(gem.parse({"type": "message", "role": "assistant", "content": "Saved 3"}).result_part, "Saved 3")

        claude = engines.ENGINES["claude"]
        done = claude.parse({"type": "result", "result": "ok", "total_cost_usd": 0.5, "is_error": False})
        self.assertEqual((done.result, done.cost, done.error), ("ok", 0.5, None))

    def test_engines_without_subagents_get_the_playbooks(self):
        from leadsmith import engines, jobs

        solo = jobs.full_prompt("find_buyers", "Find 5 buyers.", engines.ENGINES["codex"])
        self.assertIn("You don't have sub-agents", solo)
        self.assertIn("Signal Scout", solo)
        self.assertNotIn("name: signal-scout", solo)  # frontmatter stripped
        self.assertNotIn("Outreach Writer", solo)  # a find run stops before writing
        # Claude Code is one agent by default too; with sub-agents switched on it gets no inline playbooks.
        claude = engines.ENGINES["claude"]
        self.assertIn("Playbook", jobs.full_prompt("find_buyers", "Find 5 buyers.", claude))
        cmd, _ = claude.command("")
        blocked = cmd[cmd.index("--disallowedTools") + 1].split(",")
        self.assertIn("Agent", blocked)
        with mock.patch.object(engines.config, "get_bool", side_effect=lambda k, d=False: k == "USE_SUBAGENTS" or d):
            self.assertNotIn("Playbook", jobs.full_prompt("find_buyers", "Find 5 buyers.", claude))
            cmd, _ = claude.command("")
            self.assertIn("Agent", cmd[cmd.index("--allowedTools") + 1].split(","))

    def test_model_lists_for_the_dropdown(self):
        import json as _json
        import tempfile as _tmp
        from pathlib import Path
        from unittest import mock

        from leadsmith import engines

        self.assertIn("sonnet", [m["id"] for m in engines.ENGINES["claude"].models()])
        self.assertIn("flash", [m["id"] for m in engines.ENGINES["gemini"].models()])
        home = Path(_tmp.mkdtemp())
        (home / ".codex").mkdir()
        (home / ".codex" / "models_cache.json").write_text(_json.dumps({"models": [
            {"slug": "gpt-b", "display_name": "GPT-B", "visibility": "list", "priority": 2},
            {"slug": "gpt-a", "display_name": "GPT-A", "visibility": "list", "priority": 1, "description": "Fast."},
            {"slug": "internal", "visibility": "hide"}]}))
        with mock.patch.object(engines.Path, "home", return_value=home):
            codex = engines.ENGINES["codex"].models()
        self.assertEqual([m["id"] for m in codex], ["gpt-a", "gpt-b"])  # hidden ones left out, in Codex's order
        self.assertEqual(codex[0]["label"], "GPT-A: Fast")
        out = mock.Mock(stdout="opencode/big-pickle\nnot a model line\ngoogle/gemini-3-pro\n")
        with mock.patch.object(engines, "_run_quiet", return_value=out):
            self.assertEqual([m["id"] for m in engines.ENGINES["opencode"].models()], ["opencode/big-pickle", "google/gemini-3-pro"])

    def test_engine_setting_and_old_model_name(self):
        from unittest import mock

        from leadsmith import engines

        with mock.patch.dict(os.environ, {"AGENT_ENGINE": "codex", "AGENT_MODEL": "", "CLAUDE_MODEL": "opus"}):
            self.assertEqual((engines.current().key, engines.current_model()), ("codex", ""))
        with mock.patch.dict(os.environ, {"AGENT_ENGINE": "", "AGENT_MODEL": "", "CLAUDE_MODEL": "opus"}):
            self.assertEqual((engines.current().key, engines.current_model()), ("claude", "opus"))
        with mock.patch.dict(os.environ, {"AGENT_ENGINE": "nonsense"}):
            self.assertEqual(engines.current().key, "claude")


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


class EngineConnectTests(unittest.TestCase):
    """No agent runs or is even checked until the user connects one, and only one at a time."""

    def setUp(self):
        from starlette.testclient import TestClient

        from leadsmith import dashboard

        dashboard._connected["engine"] = None
        self.dash = dashboard
        self.c = TestClient(dashboard.create_app(4600), base_url="http://127.0.0.1:4600", headers={"X-Leadsmith-Client": "1"})

    def tearDown(self):
        self.dash._connected["engine"] = None

    def test_nothing_is_started_until_the_user_connects(self):
        from unittest import mock

        from leadsmith import engines

        with mock.patch.object(engines.Engine, "status", side_effect=AssertionError("must not start an engine")) as status:
            overview = self.c.get("/api/overview").json()
            settings = self.c.get("/api/settings").json()
        status.assert_not_called()
        self.assertFalse(overview["engine"]["connected"])
        self.assertEqual(len(settings["engines"]), 4)
        r = self.c.post("/api/jobs", json={"kind": "ideas", "domains": ["acme.io"]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("Connect an agent", r.json()["error"])

    def test_connect_one_engine_then_disconnect(self):
        from unittest import mock

        from leadsmith import engines

        ready = {"key": "codex", "label": "Codex", "plan": "your ChatGPT plan", "installed": True, "logged_in": True}
        with mock.patch.object(engines.Codex, "status", return_value=ready), \
             mock.patch.dict(os.environ, {"AGENT_ENGINE": "claude"}), \
             mock.patch.object(self.dash.config, "save_env") as save:
            self.assertTrue(self.c.post("/api/engines/connect", json={"engine": "codex"}).json()["connected"])
            save.assert_called_once()
        self.assertEqual(self.c.get("/api/overview").json()["engine"]["key"], "codex")
        signed_out = {**ready, "logged_in": False}
        with mock.patch.object(engines.Gemini, "status", return_value=signed_out):
            self.assertEqual(self.c.post("/api/engines/connect", json={"engine": "gemini"}).status_code, 400)
        self.assertEqual(self.c.get("/api/overview").json()["engine"]["key"], "codex")  # still the one connected
        self.c.post("/api/engines/disconnect", json={})
        self.assertFalse(self.c.get("/api/overview").json()["engine"]["connected"])


class SessionTests(unittest.TestCase):
    """A connection holds one conversation: runs continue it, and "continue previous" picks up the last one."""

    def fake_engine(self):
        import sys as _sys

        from leadsmith import engines

        class Echo(engines.Engine):
            # Prints the session it was asked to continue, or a new id, then a result.
            def path(self):
                return _sys.executable

            def command(self, model, session=None):
                code = ("import json,sys,uuid; s=sys.argv[1] or 'new-'+uuid.uuid4().hex[:6]; "
                        "print(json.dumps({'type':'system','session_id':s})); print(json.dumps({'type':'done','text':s}))")
                return [_sys.executable, "-c", code, session or ""], {}

            def session_of(self, event):
                return event.get("session_id")

            def parse(self, event):
                return engines.Parsed(result=event.get("text")) if event.get("type") == "done" else engines.Parsed()

        return Echo("echo", "Echo", "echo", "", "", "your echo plan", subagents=True)

    def wait(self, job_id):
        import time as _time

        from leadsmith import jobs

        for _ in range(100):
            job = jobs.get(job_id)
            if job["status"] not in ("queued", "running"):
                return job
            _time.sleep(0.1)
        self.fail("job didn't finish")

    def test_runs_in_one_connection_share_the_session(self):
        from unittest import mock

        from leadsmith import engines, jobs

        with mock.patch.dict(engines.ENGINES, {"echo": self.fake_engine()}):
            conn = {"session_id": None}
            first = self.wait(jobs.start("custom", {"prompt": "one"}, "echo", conn)["id"])
            self.assertTrue(first["result"].startswith("new-"))
            self.assertEqual(conn["session_id"], first["result"])
            second = self.wait(jobs.start("custom", {"prompt": "two"}, "echo", conn)["id"])
            self.assertEqual(second["result"], first["result"])  # continued, not a new conversation
            self.assertEqual(jobs.last_session("echo")["session_id"], first["result"])
            other = self.wait(jobs.start("custom", {"prompt": "fresh"}, "echo", {"session_id": None})["id"])
            self.assertNotEqual(other["result"], first["result"])

    def test_plans_are_read_from_each_cli_file(self):
        import base64 as _b64
        import json as _json
        import tempfile as _tmp
        from pathlib import Path
        from unittest import mock

        from leadsmith import engines

        home = Path(_tmp.mkdtemp())
        (home / ".claude.json").write_text(_json.dumps({"oauthAccount": {"organizationType": "claude_max"}}))
        claims = _b64.urlsafe_b64encode(_json.dumps({"https://api.openai.com/auth": {
            "chatgpt_plan_type": "plus", "chatgpt_subscription_active_until": "2999-01-31T00:00:00+00:00"}}).encode()).decode().rstrip("=")
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(_json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": f"x.{claims}.y"}}))
        (home / ".local" / "share" / "opencode").mkdir(parents=True)
        (home / ".local" / "share" / "opencode" / "auth.json").write_text(_json.dumps({"anthropic": {"type": "oauth"}}))
        (home / ".gemini").mkdir()
        (home / ".gemini" / "settings.json").write_text(_json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}))
        with mock.patch.object(engines.Path, "home", return_value=home):
            plans = {k: e.account() for k, e in engines.ENGINES.items()}
        self.assertEqual(plans["claude"]["plan"], "Claude Max")
        self.assertEqual((plans["codex"]["plan"], plans["codex"]["detail"]), ("ChatGPT Plus", "Active until 31 Jan 2999"))
        self.assertEqual((plans["opencode"]["plan"], plans["opencode"]["detail"]), ("Anthropic", "OpenCode providers · subscription"))
        self.assertEqual(plans["gemini"]["plan"], "Google account")

    def test_continue_needs_a_previous_session(self):
        from unittest import mock

        from starlette.testclient import TestClient

        from leadsmith import dashboard, engines, jobs

        client = TestClient(dashboard.create_app(4600), base_url="http://127.0.0.1:4600", headers={"X-Leadsmith-Client": "1"})
        ready = {"key": "gemini", "label": "Gemini CLI", "plan": "x", "installed": True, "logged_in": True}
        with mock.patch.object(engines.Gemini, "status", return_value=ready), \
             mock.patch.object(engines.Gemini, "models", return_value=[]), \
             mock.patch.object(dashboard.config, "save_env"):
            with mock.patch.object(jobs, "last_session", return_value=None):
                self.assertEqual(client.post("/api/engines/connect", json={"engine": "gemini", "session": "continue"}).status_code, 400)
            prev = {"session_id": "abc", "title": "Find 5 buyers", "status": "done", "at": "2026-10-25T10:00:00+00:00"}
            with mock.patch.object(jobs, "last_session", return_value=prev):
                r = client.post("/api/engines/connect", json={"engine": "gemini", "session": "continue"}).json()
        self.assertEqual((r["session_mode"], r["session_id"]), ("continue", "abc"))
        dashboard._connected["engine"] = None


class PersonalPitchTests(unittest.TestCase):
    """The personal-pitch style mentions an attached deck, so that claim must always be true."""

    BODY = ("Hi Dana,\n\nThe new Kestrel trail shoe looks great, and the weeks before a drop are when video does the most work.\n\n"
            "We film footwear brands on real trails, so buyers can see grip and fit before they order.\n\n"
            "I've attached a 4-page deck made for Kestrel with a two-day shoot plan.\n\nWould a short call this week help?")

    def setUp(self):
        import uuid

        self.domain = f"kestrel-{uuid.uuid4().hex[:5]}.io"
        db.upsert_account(self.domain, name="Kestrel")
        self.contact, _ = db.upsert_contact(self.domain, first_name="Dana", email=f"dana@{self.domain}")

    def test_cannot_save_or_approve_a_false_attachment_claim(self):
        from unittest import mock

        from leadsmith import dashboard, decks, server

        r = server.save_draft(self.contact["id"], 1, self.BODY, "Kestrel trail shoe, before the drop", attach_deck=True)
        self.assertIn("no PDF deck", r["error"])  # nothing to attach yet
        saved = server.save_draft(self.contact["id"], 1, self.BODY, "Kestrel trail shoe, before the drop")
        self.assertEqual(saved["lint"]["issues"][0]["rule"], "attachment")
        with mock.patch.object(decks, "pdf_ready", return_value=True):
            good = server.save_draft(self.contact["id"], 1, self.BODY, "Kestrel trail shoe, before the drop", attach_deck=True)
            self.assertTrue(good["lint"]["ok"])
            self.assertEqual(dashboard._review([good["draft_id"]], "approve")["done"], [good["draft_id"]])
        db.set_status("draft", good["draft_id"], "draft")
        refused = dashboard._review([good["draft_id"]], "approve")["refused"]  # the deck PDF has gone missing
        self.assertIn("attached", refused[0]["reason"])

    def test_mailer_refuses_to_send_without_the_promised_attachment(self):
        from leadsmith import mailer

        draft = {"id": 1, "step": 1, "subject": "hi", "body": self.BODY, "attach_deck": 1}
        with self.assertRaises(ValueError):
            mailer._build(draft, {**self.contact, "email": f"dana@{self.domain}"}, None)

    def test_brain_read_tells_the_writer_which_style(self):
        from unittest import mock

        from leadsmith import server

        with mock.patch.dict(os.environ, {"EMAIL_STYLE": "personal_pitch"}):
            self.assertEqual(server.brain_read()["email_style"]["style"], "personal_pitch")
        with mock.patch.dict(os.environ, {"EMAIL_STYLE": ""}):
            self.assertEqual(server.brain_read()["email_style"]["style"], "idea_menu")


class RegenerateTests(unittest.TestCase):
    def setUp(self):
        import uuid

        self.domain = f"regen-{uuid.uuid4().hex[:5]}.io"
        db.upsert_account(self.domain, name="Regen Co")
        self.contact, _ = db.upsert_contact(self.domain, first_name="Ravi", last_name="K", email=f"ravi@{self.domain}")
        ok = {"ok": True, "issues": []}
        self.ids = [db.upsert_draft(self.contact["id"], n, "hi" if n == 1 else None, f"body {n}", "email", 0, ok)["id"] for n in (1, 2, 3)]
        with db.connect() as conn:
            conn.execute("UPDATE drafts SET status = 'sent', sent_at = ? WHERE id = ?", (db.now(), self.ids[0]))
            conn.execute("UPDATE drafts SET status = 'approved' WHERE id IN (?, ?)", (self.ids[1], self.ids[2]))

    def test_prompt_targets_only_unsent_steps(self):
        from leadsmith import jobs

        title, prompt = jobs._fmt("rewrite", {"contact_id": self.contact["id"], "extra": "shorter"})
        self.assertEqual(title, "Regenerate emails: Ravi K")
        self.assertIn("step 2, step 3", prompt)
        self.assertIn("Step 1 already went out", prompt)
        self.assertIn("shorter", prompt)

    def test_regenerate_takes_approved_emails_off_the_send_queue(self):
        from unittest import mock

        from starlette.testclient import TestClient

        from leadsmith import dashboard, jobs

        client = TestClient(dashboard.create_app(4600), base_url="http://127.0.0.1:4600", headers={"X-Leadsmith-Client": "1"})
        dashboard._connected["engine"] = {"key": "claude", "label": "Claude Code", "session_id": None}
        try:
            with mock.patch.object(jobs, "start", return_value={"id": 1, "title": "Regenerate"}) as start:
                r = client.post("/api/jobs", json={"kind": "rewrite", "contact_id": self.contact["id"]}).json()
            start.assert_called_once()
        finally:
            dashboard._connected["engine"] = None
        self.assertEqual(r["paused"], 2)
        with db.connect() as conn:
            statuses = [conn.execute("SELECT status FROM drafts WHERE id = ?", (i,)).fetchone()[0] for i in self.ids]
        self.assertEqual(statuses, ["sent", "draft", "draft"])


class EditEmailTests(unittest.TestCase):
    def test_edit_email_validates_and_sends_approved_emails_back_to_review(self):
        import uuid

        from starlette.testclient import TestClient

        from leadsmith import dashboard

        client = TestClient(dashboard.create_app(4600), base_url="http://127.0.0.1:4600", headers={"X-Leadsmith-Client": "1"})
        tag = uuid.uuid4().hex[:5]
        domain = f"edit-{tag}.io"
        db.upsert_account(domain, name="Edit Co")
        c, _ = db.upsert_contact(domain, first_name="Meera", email=f"meera@{domain}", email_status="guessed")
        other, _ = db.upsert_contact(domain, first_name="Ola", email=f"ola@{domain}")
        draft = db.upsert_draft(c["id"], 1, "hi", "Hi Meera, quick one.", "email", 0, {"ok": True, "issues": []})
        db.set_status("draft", draft["id"], "approved")
        url = f"/api/contacts/{c['id']}/email"
        self.assertEqual(client.put(url, json={"email": "not an email"}).status_code, 400)
        self.assertIn("already belongs", client.put(url, json={"email": f"ola@{domain}"}).json()["error"])
        db.suppress(f"blocked@{domain}", "test")
        self.assertIn("do-not-contact", client.put(url, json={"email": f"blocked@{domain}"}).json()["error"])
        r = client.put(url, json={"email": f"Meera.R@{domain}"}).json()
        self.assertEqual((r["email"], r["paused"]), (f"meera.r@{domain}", 1))
        updated = db.get_contact(c["id"])
        self.assertEqual((updated["email"], updated["email_status"]), (f"meera.r@{domain}", "manual"))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM drafts WHERE id = ?", (draft["id"],)).fetchone()[0], "draft")


class RerunTests(unittest.TestCase):
    def fake(self, key, fail):
        import sys as _sys

        from leadsmith import engines

        class Fake(engines.Engine):
            def path(self):
                return _sys.executable

            def command(self, model, session=None):
                code = ("import json; print(json.dumps({'type':'error','message':\"You've hit your usage limit.\"}))" if fail
                        else "import json; print(json.dumps({'type':'done','text':'finished'}))")
                return [_sys.executable, "-c", code], {}

            def parse(self, event):
                if event.get("type") == "error":
                    return engines.Parsed(error=event["message"])
                return engines.Parsed(result=event.get("text"))

        return Fake(key, key.title(), key, "", "", "x", subagents=True)

    def wait(self, job_id):
        import time as _time

        from leadsmith import jobs

        for _ in range(100):
            job = jobs.get(job_id)
            if job["status"] not in ("queued", "running"):
                return job
            _time.sleep(0.1)
        self.fail("job didn't finish")

    def test_a_failed_run_can_be_rerun_on_another_agent(self):
        from unittest import mock

        from leadsmith import engines, jobs

        with mock.patch.dict(engines.ENGINES, {"limited": self.fake("limited", True), "spare": self.fake("spare", False)}):
            failed = self.wait(jobs.start("custom", {"prompt": "Find 3 buyers"}, "limited")["id"])
            self.assertEqual(failed["status"], "failed")
            self.assertIn("usage limit", failed["result"])
            again = self.wait(jobs.rerun(failed["id"], "spare")["id"])
            self.assertEqual((again["status"], again["engine"], again["kind"], again["title"]), ("done", "spare", "custom", failed["title"]))
            self.assertIn("Find 3 buyers", again["prompt"])
            self.assertEqual(again["prompt"].count("stopped part-way"), 1)
            third = self.wait(jobs.rerun(again["id"], "spare")["id"])
            self.assertEqual(third["prompt"].count("stopped part-way"), 1)  # the note isn't stacked


class QualityTests(unittest.TestCase):
    def test_enhance_turns_a_vague_request_into_a_brief_from_the_profile(self):
        from leadsmith import brain

        icp = ("## Firmographics\n- Industries / keywords: dental clinics, physiotherapy\n- Company size (employees): 5-50\n"
               "- Locations: India\n## Buying signals\n- 5 — Opening a new branch\n- 3 — Hiring a front-desk manager\n"
               "## Disqualifiers\n- Hospital chains\n- TODO e.g. Competitors\n")
        personas = "- Titles to search: Owner, Clinic director\n"
        original = {n: brain.read_brain()[n]["content"] for n in ("icp", "personas")}
        try:
            brain.write_brain("icp", icp)
            brain.write_brain("personas", personas)
            out = brain.enhance_request("find me some leads", count=7)
        finally:
            for n, text in original.items():
                brain.write_brain(n, text)
        text = out["text"]
        self.assertIn("Goal: find me some leads", text)
        self.assertIn("How many: 7 companies", text)
        self.assertIn("dental clinics", text)
        self.assertIn("Opening a new branch; Hiring a front-desk manager", text)  # strongest signal first
        self.assertIn("Skip: Hospital chains", text)
        self.assertNotIn("TODO", text)
        self.assertIn("Owner, Clinic director", text)

    def test_quality_report_flags_unsourced_and_generic_work(self):
        import uuid

        tag = uuid.uuid4().hex[:5]
        good, bad = f"good-{tag}.io", f"bad-{tag}.io"
        db.upsert_account(good, name="Goodco", why_now="Raised a seed round last week")
        db.add_signal(good, "funding", "Seed round", "$3M", f"https://news.example/{tag}", "news", "2026-10-20")
        db.add_signal(good, "need", "Hiring their first marketer", None, f"https://{good}/jobs", "website", "2026-10-21")
        c, _ = db.upsert_contact(good, first_name="Ana", email=f"ana@{good}", email_status="public")
        db.upsert_draft(c["id"], 1, "goodco seed round", "Hi Ana, congrats on Goodco's seed round.", "email", 0, {"ok": True, "issues": []})
        db.upsert_account(bad, name="Badco")
        db.add_signal(bad, "news", "Something happened", None, None, "web", None)
        c2, _ = db.upsert_contact(bad, first_name="Bo", email=f"bo@{bad}")
        db.upsert_draft(c2["id"], 1, "quick question", "Hi Bo, we help teams grow faster.", "email", 0, {"ok": True, "issues": []})
        report = db.quality_report([good, bad])
        problems = {(i["domain"], i["level"]) for i in report["issues"]}
        self.assertNotIn((good, "FAIL"), problems)
        bad_issues = " | ".join(i["issue"] for i in report["issues"] if i["domain"] == bad)
        self.assertIn("No signal with a source URL", bad_issues)
        self.assertIn("No why_now", bad_issues)
        self.assertIn("never names the company", bad_issues)
        self.assertFalse(report["ok"])

    def test_lint_warns_when_a_first_email_could_go_to_anyone(self):
        generic = lint.check("quick question", "Hi Bo, we help teams grow faster. Worth a chat?", 1, "Badco Inc.", "Bo")
        self.assertIn("generic", [i["rule"] for i in generic["issues"]])
        specific = lint.check("badco launch", "Hi Bo, saw the Badco launch on Monday. Worth a chat?", 1, "Badco Inc.", "Bo")
        self.assertNotIn("generic", [i["rule"] for i in specific["issues"]])


class StyleEnforcementTests(unittest.TestCase):
    MENU = "Hi Alex,\n\nSaw the bootcamp hit a million views.\n\nA) a 60-second film\nB) five problem loops\nC) a tour\n\nWhich first?"

    def test_personal_pitch_fails_an_idea_menu_and_idea_menu_style_allows_it(self):
        self.assertTrue(lint.has_idea_menu(self.MENU))
        self.assertFalse(lint.has_idea_menu("Hi Alex, I've attached a deck with a plan (option A in the deck)."))
        pitch = lint.check("bootcamp video", self.MENU, 1, "Alex", "Alex", style="personal_pitch")
        self.assertIn("style", [i["rule"] for i in pitch["issues"] if i["level"] == "FAIL"])
        self.assertNotIn("style", [i["rule"] for i in lint.check("bootcamp video", self.MENU, 1, "Alex", "Alex", style="idea_menu")["issues"]])

    def test_agent_sees_only_the_pitch_structure_from_voice_md(self):
        from unittest import mock

        from leadsmith import server

        voice = ("# Voice\n## Tone\n- Warm, direct.\n## Sequence\n- Step 1 (day 0): one observation, then 3 ideas (A/B/C).\n"
                 "## Extras\n- Offer ideas: yes (A/B/C)\n- 4-page PDF deck per account: no\n## Never\n- Invent facts.\n")
        seen = server._voice_for_pitch(voice)
        self.assertNotIn("3 ideas (A/B/C)", seen)
        self.assertIn("No A/B/C options", seen)
        self.assertIn("- Offer ideas: no", seen)
        self.assertIn("deck per account: yes", seen)
        self.assertIn("- Warm, direct.", seen)  # tone and rules are kept
        self.assertIn("- Invent facts.", seen)
        with mock.patch.dict(os.environ, {"EMAIL_STYLE": "idea_menu"}):
            self.assertNotIn("set by Email style", server.brain_read()["voice"]["content"])


class AttachedPreviewTests(unittest.TestCase):
    def test_send_preview_works_for_an_email_with_a_deck_attached(self):
        import tempfile as _tmp
        import uuid
        from pathlib import Path
        from unittest import mock

        from leadsmith import mailer

        tag = uuid.uuid4().hex[:5]
        domain = f"deck-{tag}.io"
        db.upsert_account(domain, name="Deckco", timezone="UTC")
        c, _ = db.upsert_contact(domain, first_name="Dee", email=f"dee@{domain}")
        pdf = Path(_tmp.mkdtemp()) / "deck.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        with db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO decks (domain, title, slides, pdf_path, error, updated_at) VALUES (?, ?, '[]', ?, NULL, ?)",
                         (domain, "Deckco plan", str(pdf), db.now()))
        d = db.upsert_draft(c["id"], 1, "deckco launch", "Hi Dee, I've attached a 4-page deck made for Deckco.", "email", 0, {"ok": True, "issues": []})
        with db.connect() as conn:
            conn.execute("UPDATE drafts SET attach_deck = 1, status = 'approved' WHERE id = ?", (d["id"],))
        env = {"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "x", "SEND_HOURS": "0-24", "SEND_WEEKDAYS_ONLY": "false",
               "WARMUP": "false", "TEST_RECIPIENT": ""}
        with mock.patch.dict(os.environ, env):
            report = mailer.send_approved(50, True)  # dry run: the preview the Review & send button shows
        mine = [e for e in report["sent"] if e.get("draft_id") == d["id"]]
        self.assertEqual(len(mine), 1)
        self.assertIn("I've attached a 4-page deck", mine[0]["preview"])
        self.assertEqual(mine[0]["attachments"], ["Deckco plan.pdf"])


class PreviewWhileSendingTests(unittest.TestCase):
    def test_preview_works_while_a_batch_is_sending_but_a_second_send_waits(self):
        from unittest import mock

        from leadsmith import mailer

        env = {"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "x", "SEND_ENABLED": "true", "TEST_RECIPIENT": ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(mailer, "check_replies", return_value={"replies": [], "bounces": []}):
            self.assertTrue(mailer._SEND_LOCK.acquire(timeout=1))  # a real batch is going out
            try:
                preview = mailer.send_approved(10, True)
                self.assertNotIn("error", preview)
                self.assertIn("sending right now", preview.get("note", ""))
                second = mailer.send_approved(10, False)
                self.assertIn("already sending", second["error"])
            finally:
                mailer._SEND_LOCK.release()
            self.assertNotIn("note", mailer.send_approved(10, True))


class DuplicateLeadTests(unittest.TestCase):
    def setUp(self):
        import uuid

        self.tag = uuid.uuid4().hex[:6]
        self.site = f"northwind{self.tag}.com"
        db.upsert_account(self.site, name=f"Northwind {self.tag} Inc.", linkedin_url=f"https://linkedin.com/company/nw{self.tag}")

    def test_find_duplicate_catches_other_spellings_of_the_same_company(self):
        t = self.tag
        why = lambda domain, name=None, li=None: (db.find_duplicate(domain, name, li) or {}).get("why", "")  # noqa: E731
        self.assertIn("same domain", why(f"www.northwind{t}.com"))
        self.assertIn("same website", why(f"app.northwind{t}.com"))
        self.assertIn("same brand", why(f"northwind{t}.in"))  # no name given: the domain brand is enough
        self.assertIn("same company name", why(f"northwind{t}.in", f"Northwind {t}"))
        self.assertIn("same company name", why(f"nw-labs-{t}.io", f"Northwind {t}"))
        self.assertIn("LinkedIn", why(f"other-{t}.io", None, f"https://linkedin.com/company/nw{t}/"))
        # a different company that merely shares a word is not a repeat
        self.assertEqual(why(f"northwind{t}.in", "Totally Different Ltd"), "")
        self.assertEqual(why(f"brandnew-{t}.io", f"Brand New {t}"), "")

    def test_save_account_rejects_repeats_when_discovering_and_keeps_updates_working(self):
        from leadsmith import server

        t = self.tag
        repeat = server.save_account(f"northwind{t}.in", name=f"Northwind {t}", new_lead=True)
        self.assertTrue(repeat["duplicate"])
        self.assertFalse(repeat["created"])
        self.assertEqual(repeat["duplicate_of"]["domain"], self.site)
        self.assertIsNone(db.find_duplicate(f"northwind{t}.in", "Totally Different Ltd"))  # nothing was written for it
        with db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM accounts WHERE domain = ?", (f"northwind{t}.in",)).fetchone())
        # the exact domain is rejected too when it's a freshly found lead...
        self.assertTrue(server.save_account(self.site, name="Northwind", new_lead=True)["duplicate"])
        # ...but research updating a company you already have still works
        updated = server.save_account(self.site, why_now="Opened a new office")
        self.assertFalse(updated.get("duplicate"))
        self.assertEqual(updated["account"]["why_now"], "Opened a new office")
        # a genuinely new company is saved
        fresh = server.save_account(f"fresh-{t}.io", name=f"Fresh {t}", new_lead=True)
        self.assertTrue(fresh["created"])

    def test_check_known_and_manual_add_report_repeats(self):
        from starlette.testclient import TestClient

        from leadsmith import dashboard, server

        t = self.tag
        known = server.check_known([f"app.northwind{t}.com", f"brandnew-{t}.io"], [f"Northwind {t}", f"Brand New {t}"])
        self.assertTrue(known[f"app.northwind{t}.com"]["in_pipeline"])
        self.assertEqual(known[f"app.northwind{t}.com"]["duplicate_of"]["domain"], self.site)
        self.assertFalse(known[f"brandnew-{t}.io"]["in_pipeline"])
        client = TestClient(dashboard.create_app(4600), base_url="http://127.0.0.1:4600", headers={"X-Leadsmith-Client": "1"})
        r = client.post("/api/accounts", json={"domains": f"app.northwind{t}.com\nbrandnew-{t}.io"}).json()
        self.assertEqual(r["added"], [f"brandnew-{t}.io"])
        self.assertEqual(r["skipped_duplicates"][0]["matches"], self.site)

    def test_find_buyers_prompt_lists_what_is_already_in_the_pipeline(self):
        from leadsmith import jobs

        title, prompt = jobs._fmt("find_buyers", {"count": 5})
        self.assertIn("Already in my pipeline", prompt)
        self.assertIn(self.site, prompt)


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


import json  # noqa: E402
from unittest import mock  # noqa: E402


class HungEngineTests(unittest.TestCase):
    """An engine CLI that finishes its work but never exits must not leave the run 'running' forever."""

    def engine(self, events):
        import sys as _sys

        from leadsmith import engines

        class Hangs(engines.Engine):
            def path(self):
                return _sys.executable

            def command(self, model, session=None):
                lines = "".join(f"print({json.dumps(json.dumps(e))}, flush=True); " for e in events)
                return [_sys.executable, "-c", f"import time; {lines}time.sleep(600)"], {}

        return Hangs("hangs", "Hangs", "hangs", "", "", "", subagents=True)

    def run_job(self, events, **timings):
        import time as _time
        from unittest import mock

        from leadsmith import engines, jobs

        with mock.patch.dict(engines.ENGINES, {"hangs": self.engine(events)}), \
                mock.patch.multiple(jobs, POLL_SECONDS=0.2, **timings):
            job_id = jobs.start("custom", {"prompt": "go"}, "hangs")["id"]
            for _ in range(150):
                job = jobs.get(job_id)
                if job["status"] not in ("queued", "running"):
                    return job
                _time.sleep(0.1)
        self.fail("the run stayed 'running' after the engine finished")

    def test_closes_after_the_engine_reports_done(self):
        from leadsmith import engines

        with mock.patch.object(engines.Engine, "parse",
                               lambda self, e: engines.Parsed(result=e.get("text"), done=e.get("type") == "end")):
            job = self.run_job([{"type": "end", "text": "Saved 10 accounts."}], EXIT_GRACE_SECONDS=0.5)
        self.assertEqual((job["status"], job["result"]), ("done", "Saved 10 accounts."))
        self.assertIn("didn't close by itself", job["log"])

    def test_claude_answer_then_silence_counts_as_done(self):
        from leadsmith import engines

        answer = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Saved 10 accounts."}]}}
        with mock.patch.object(engines.Engine, "parse", engines.Claude.parse):
            job = self.run_job([answer], IDLE_AFTER_ANSWER_SECONDS=0.5)
        self.assertEqual((job["status"], job["result"]), ("done", "Saved 10 accounts."))

    def test_claude_text_before_a_tool_call_is_not_final(self):
        from leadsmith import engines

        claude = engines.ENGINES["claude"]
        mid = claude.parse({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Now searching."}, {"type": "tool_use", "name": "WebSearch", "input": {}}]}})
        sub = claude.parse({"type": "assistant", "parent_tool_use_id": "x",
                            "message": {"content": [{"type": "text", "text": "subagent done"}]}})
        self.assertFalse(mid.final or sub.final)
        self.assertTrue(claude.parse({"type": "result", "result": "ok"}).done)


class CompaniesHouseTests(unittest.TestCase):
    """Offline: Companies House page shapes, London postcodes and virtual offices."""

    def test_place(self):
        from leadsmith.providers.companies_house import place

        self.assertEqual(place("71-75 Shelton Street, Covent Garden, London, United Kingdom, WC2H 9JQ"),
                         {"postcode": "WC2H 9JQ", "london": "yes", "virtual_office": True})
        self.assertEqual(place("12 Mare Street, London, E8 4RP")["virtual_office"], False)
        self.assertEqual(place("1 High Street, Kingston upon Thames, KT1 1AA")["london"], "maybe")
        self.assertEqual(place("59 Bath Street, Glasgow, G2 2DH")["london"], "no")

    def test_search_and_profile(self):
        from leadsmith.providers import companies_house as ch

        results = """<li class="type-company"><h3><a class="govuk-link" href="/company/09718346" title="View company">
            LONDON APP DEVELOPER LTD</a></h3><p class="meta crumbtrail"> 09718346 - Incorporated on 5 August 2015 </p>
            <p>71-75 Shelton Street, Covent Garden, London, United Kingdom, WC2H 9JQ</p></li>"""
        row = ch.parse_results(results, "company")[0]
        self.assertEqual((row["number"], row["incorporated"], row["virtual_office"]), ("09718346", "5 August 2015", True))
        people = """<li class="type-officer"><h3><a class="govuk-link" href="/officers/Vqx/appointments">Mark Alan WINTERBOTTOM</a></h3>
            <p class="meta crumbtrail">Total number of appointments 1 - Born January 1989</p><p>8 Victoria Street, Denton, Manchester, M34 3JQ</p></li>"""
        person = ch.parse_results(people, "officer")[0]
        self.assertEqual((person["born"], person["london"]), ("January 1989", "no"))
        officers = """<span id="officer-name-1"><a>WINTERBOTTOM, Mark Alan</a></span>
            <dd id="officer-address-value-1">12 Mare Street, London, E8 4RP</dd><span id="officer-status-tag-1">Active</span>
            <dd id="officer-role-1">Director</dd><dd id="officer-date-of-birth-1">January 1989</dd>
            <span id="officer-name-2"><a>OLD, Person</a></span><span id="officer-status-tag-2">Resigned</span>"""
        parsed = ch.parse_officers(officers)
        self.assertEqual([o["active"] for o in parsed], [True, False])
        self.assertEqual(parsed[0]["name"], "WINTERBOTTOM, Mark Alan")
        self.assertEqual(parsed[0]["correspondence_address"], "12 Mare Street, London, E8 4RP")


class OpenCodeV2Tests(unittest.TestCase):
    """OpenCode 2: config file + private server, no shell or file access, sign-in parsing, tools in Code Mode."""

    def test_v2_command_and_safety(self):
        import json as _json

        from leadsmith import config, engines

        oc = engines.ENGINES["opencode"]
        with mock.patch.object(type(oc), "version", lambda self: (2, 2, 5)):
            cmd, env = oc.command("opencode/mimo-v2.6-flash-free")
            self.assertIn("--standalone", cmd)
            self.assertEqual(cmd[cmd.index("--model") + 1], "opencode/mimo-v2.6-flash-free")
            self.assertEqual(env, {})
            # The config lives in its own empty run folder, never in the workspace (OpenCode merges parent configs).
            self.assertEqual(oc.workdir(), config.HOME / "opencode-run")
            self.assertFalse((config.HOME / "opencode.json").exists())
            cfg = _json.loads((oc.workdir() / "opencode.json").read_text(encoding="utf-8"))
            self.assertIn("leadsmith", cfg["mcp"]["servers"])
            self.assertEqual(cfg["mcp"]["servers"]["leadsmith"]["environment"]["LEADSMITH_AGENT_RUN"], "1")
            for tool in ("shell", "bash", "edit", "question", "external_directory"):
                self.assertEqual(cfg["permission"][tool], "deny", tool)
            # Denying `read` makes OpenCode refuse its free models; reading outside the run folder is denied instead.
            self.assertNotIn("read", cfg["permission"])
            self.assertIn("tools.leadsmith", oc.prompt_note())
        with mock.patch.object(type(oc), "version", lambda self: (1, 4, 0)):
            cmd, env = oc.command("")
            self.assertNotIn("--standalone", cmd)
            self.assertIn("leadsmith", _json.loads(env["OPENCODE_CONFIG_CONTENT"])["mcp"])
            self.assertEqual(oc.prompt_note(), "")
            self.assertEqual(oc.workdir(), config.HOME)

    def test_signed_in_reads_the_provider_list(self):
        from types import SimpleNamespace

        from leadsmith import engines

        oc = engines.ENGINES["opencode"]
        for text, expected in (("Cloudflare Workers AI  API key                     stored\n", True),
                               ("\x1b[1m2 credentials\x1b[0m\n", True), ("No credentials found\n", False)):
            with mock.patch.object(engines, "_run_quiet", return_value=SimpleNamespace(stdout=text)):
                self.assertEqual(oc.signed_in("opencode")[0], expected, text)

    def test_agent_runs_never_see_approve_or_send(self):
        import asyncio
        import importlib

        from leadsmith import server

        server = importlib.reload(server)
        names = lambda: {t.name for t in asyncio.run(server.mcp.list_tools())}  # noqa: E731
        self.assertIn("send_approved_emails", names())
        with mock.patch.dict(os.environ, {"LEADSMITH_AGENT_RUN": "1"}):
            server.apply_agent_mode()
        self.assertFalse({"send_approved_emails", "review_drafts"} & names())
        self.assertIn("save_deck", names())
        importlib.reload(server)


class StartupSourceTests(unittest.TestCase):
    """Offline: the page shapes Wellfound, BetaList and Product Hunt serve."""

    def test_wellfound_startups_and_job_posts(self):
        from leadsmith.providers import startups

        state = {
            "StartupResult:1": {"__typename": "StartupResult", "id": "1", "name": "Zoniqx", "slug": "zoniqx",
                                "companyUrl": "https://www.zoniqx.com", "highConcept": "Tokenization for real assets",
                                "companySize": "SIZE_11_50", "badges": [{"__ref": "Badge:ACTIVELY_HIRING"}, {"__ref": "Badge:B2B-1"}],
                                "marketTaggings": [{"__ref": "MarketTag:fintech"}],
                                "highlightedJobListings": [{"__ref": "JobListingSearchResult:9"}]},
            "MarketTag:fintech": {"displayName": "Fintech"},
            "JobListingSearchResult:9": {"id": "9", "slug": "marketing-manager", "title": "Marketing Manager",
                                         "liveStartAt": 1792627200, "remote": True, "locationNames": ["India"]},
        }
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": {"apolloState": {"data": state}}}})}</script>'
        found = startups.parse_wellfound(startups.apollo_state(html))
        self.assertEqual(len(found), 1)
        z = found[0]
        self.assertEqual((z["name"], z["domain"], z["size"], z["markets"]), ("Zoniqx", "zoniqx.com", "11-50", ["Fintech"]))
        self.assertEqual(z["badges"], ["actively hiring", "b2b"])
        self.assertEqual(z["jobs"][0]["title"], "Marketing Manager")
        self.assertEqual(z["jobs"][0]["url"], "https://wellfound.com/jobs/9-marketing-manager")
        self.assertTrue(z["jobs"][0]["posted"].startswith("2026-"))

    def test_betalist_and_product_hunt(self):
        from leadsmith.providers import startups

        page = '<a href="/startups/kelbado-lms"></a><a href="/startups/kelbado-lms"></a><a href="/startups/revenue-scout">x</a>'
        self.assertEqual(startups.parse_betalist(page), ["/startups/kelbado-lms", "/startups/revenue-scout"])
        feed = ("<feed><entry><published>2026-11-05T17:53:45-08:00</published>"
                '<link rel="alternate" type="text/html" href="https://www.producthunt.com/products/cursor-craft"/>'
                "<title>Cursor Craft</title><content type=\"html\">&lt;p&gt; Custom cursors for Mac &lt;/p&gt;&lt;p&gt;"
                "&lt;a href=\"x\"&gt;Discussion&lt;/a&gt;&lt;/p&gt;</content><author><name>Birang</name></author></entry></feed>")
        launch = startups.parse_ph_feed(feed)[0]
        self.assertEqual((launch["name"], launch["tagline"], launch["launched"], launch["maker"]),
                         ("Cursor Craft", "Custom cursors for Mac", "2026-11-05", "Birang"))

    def test_startup_source_labels(self):
        from leadsmith import db

        for raw, label in (("Product Hunt launches", "product-hunt"), ("producthunt", "product-hunt"),
                           ("wellfound: hiring SDRs", "wellfound"), ("althunt", "launch-directories"), ("f6s", "f6s")):
            self.assertEqual(db.normalize_source(raw), label, raw)


class LeadQualityTests(unittest.TestCase):
    """Proof of need, rubric fit, fixed source labels, rejected memory, published-only emails, learning, imports."""

    def test_source_labels_are_fixed(self):
        from leadsmith import db, server

        self.assertEqual(db.normalize_source("websearch"), "web-search")
        self.assertEqual(db.normalize_source("LinkedIn: founders Berlin"), "linkedin-search")
        self.assertEqual(db.normalize_source("hiring-boards"), "job-boards")
        self.assertIsNone(db.normalize_source("my special play"))
        bad = server.save_account("labels-a.com", name="A", source="my special play", new_lead=True)
        self.assertFalse(bad["saved"])
        self.assertIn("Use exactly one of", bad["error"])
        no_reason = server.save_account("labels-b.com", name="B", source="youtube", fit_score=8, new_lead=True)
        self.assertIn("fit_reason", no_reason["error"])
        ok = server.save_account("labels-c.com", name="C", source="YouTube", fit_score=8, new_lead=True,
                                 fit_reason="met: course business, India · unknown: price")
        self.assertEqual((ok["account"]["source"], ok["account"]["fit_reason"][:4]), ("youtube", "met:"))

    def test_proof_of_need_is_required_for_new_discovered_leads(self):
        from leadsmith import db

        db.upsert_account("need-a.com", name="Need A", source="launch-news", why_now="Launched in October")
        db.add_signal("need-a.com", "launch", "Launched v2", url="https://need-a.com/blog", observed_at="2026-10-01")
        report = db.quality_report(["need-a.com"])
        self.assertTrue(any("proof of need" in i["issue"] for i in report["issues"] if i["level"] == "FAIL"))
        db.add_signal("need-a.com", "need", "Hiring their first marketer", url="https://need-a.com/jobs",
                      observed_at="2026-10-20")
        self.assertFalse(any("proof of need" in i["issue"] for i in db.quality_report(["need-a.com"])["issues"]))
        db.upsert_account("imported-a.com", name="Imported", source="import", why_now="x")
        self.assertFalse(any("proof of need" in i["issue"] for i in db.quality_report(["imported-a.com"])["issues"]))

    def test_rejected_candidates_are_remembered(self):
        from leadsmith import db

        self.assertTrue(db.save_rejected("tooBig.example", "Too Big Inc", "500+ employees, ICP is under 50")["saved"])
        seen = db.known(["toobig.example", "fresh.example"])
        self.assertTrue(seen["toobig.example"]["rejected"])
        self.assertIn("500+ employees", seen["toobig.example"]["rejected_reason"])
        self.assertFalse(seen["fresh.example"]["rejected"])
        with db.connect() as conn:  # after REJECTED_DAYS it's checked again
            conn.execute("UPDATE rejected SET created_at = '2020-01-01T00:00:00+00:00' WHERE domain = 'toobig.example'")
        self.assertFalse(db.known(["toobig.example"])["toobig.example"]["rejected"])

    def test_guessed_addresses_are_not_written_or_sent(self):
        from leadsmith import config, db, mailer, server

        contact, _ = db.upsert_contact("guessco.com", first_name="Gus", email="gus@guessco.com", email_status="guessed")
        refused = server.save_draft(contact["id"], 1, "Hi Gus, a note about Guessco.", subject="guessco idea")
        self.assertIn("guessed address", refused["error"])
        with db.connect() as conn:
            due, reason, _ = mailer._due({"contact_id": contact["id"], "step": 1, "kind": "sequence"}, conn)
        self.assertFalse(due)
        self.assertIn("guessed", reason)
        config.save_env({"ALLOW_GUESSED_EMAILS": "true"})
        try:
            self.assertIn("draft_id", server.save_draft(contact["id"], 1, "Hi Gus, a note about Guessco.",
                                                        subject="guessco idea"))
        finally:
            config.save_env({"ALLOW_GUESSED_EMAILS": "false"})

    def test_results_so_far_learns_per_workspace(self):
        from leadsmith import db, server

        result = server.brain_read()["results_so_far"]
        self.assertIn("learning", result)
        contact, _ = db.upsert_contact("winner-co.com", first_name="Wyn", email="wyn@winner-co.com", email_status="public")
        db.upsert_account("winner-co.com", name="Winner Co", industry="Dental clinics", source="local-listings")
        with db.connect() as conn:
            conn.execute("UPDATE contacts SET reply_kind = 'reply', status = 'replied' WHERE id = ?", (contact["id"],))
        replied = {a["domain"] for a in db.results_so_far()["replied"]}
        self.assertIn("winner-co.com", replied)

    def test_linkedin_connections_export_imports(self):
        from leadsmith import dashboard

        export = ("Notes:\n\"When exporting your connection data, you may notice that some of the email addresses are "
                  "missing.\"\n\nFirst Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
                  "Asha,Rao,https://www.linkedin.com/in/asha-rao,,Acme Dental,Owner,01 Nov 2026\n")
        body = dashboard.csv_body(export)
        self.assertTrue(body.startswith("First Name,Last Name,URL"))

        class Req:
            async def json(self):
                return {"csv": export}

        import asyncio

        with mock.patch.object(dashboard, "body", side_effect=lambda r: {"csv": export}):
            result = _json_body(asyncio.run(dashboard.import_csv(Req())))
        self.assertEqual(result["accounts"], 1)
        domain = result["domains"][0]
        self.assertTrue(domain.startswith("acme-dental"), domain)  # the profile link isn't taken as the website
        from leadsmith import db

        contact = next(c for c in db.dossier(domain)["contacts"] if c["first_name"] == "Asha")
        self.assertEqual(contact["linkedin_url"], "https://www.linkedin.com/in/asha-rao")


def _json_body(response):
    return json.loads(response.body)


class DeckFormatTests(unittest.TestCase):
    """Decks go to prospects: guessed field names are mapped, and blank or placeholder slides are refused."""

    def test_guessed_field_names_are_mapped(self):
        from leadsmith import decks

        slides, problem = decks.normalize_slides([
            {"title": "Your moment", "content": ["India is your second market.", "You launched in April.", "Hiring 30"]},
            {"headline": "What we'd make", "text": "A creator series.", "points": ["6 creators", "Reels and Shorts"]},
            {"heading": "The plan", "body": "Storyboard first.", "highlight": "6 weeks"},
            {"h1": "Next step", "description": "A 15-minute call."},
        ])
        self.assertIsNone(problem)
        self.assertEqual(slides[0], {"heading": "Your moment", "body": "India is your second market.",
                                     "bullets": ["You launched in April.", "Hiring 30"]})
        self.assertEqual(slides[1]["bullets"], ["6 creators", "Reels and Shorts"])
        self.assertEqual(slides[3]["heading"], "Next step")

    def test_blank_and_placeholder_decks_are_refused(self):
        from leadsmith import decks

        _, problem = decks.normalize_slides([{"kicker": "x"}] * 4)
        self.assertIn("no heading", problem)
        _, problem = decks.normalize_slides([{"heading": "HEADING_A", "body": "BODY_A"}] * 4)
        self.assertIn("placeholder", problem)
        _, problem = decks.normalize_slides([{"heading": "Only one", "body": "x"}])
        self.assertIn("3 to 6 slides", problem)
        result = decks.build("probe.example", "TITLE_B", [{"heading": "A", "body": "B"}] * 4)
        self.assertFalse(result["saved"])
        self.assertIsNone(decks.get("probe.example"))  # nothing was overwritten


class DeckRunTests(unittest.TestCase):
    """A deck run: done means done (even with an error exit code), no web search, and only the deck tools."""

    def test_finished_work_with_error_exit_code_is_done(self):
        import sys as _sys
        import time as _time

        from leadsmith import engines, jobs

        class ExitsOne(engines.Engine):
            def path(self):
                return _sys.executable

            def command(self, model, session=None):
                events = [{"type": "text", "text": "All 10 decks built."}, {"type": "finish"}]
                code = "".join(f"print({json.dumps(json.dumps(e))}, flush=True); " for e in events)
                return [_sys.executable, "-c", code + "raise SystemExit(1)"], {}

            def parse(self, event):
                return engines.Parsed(result=event.get("text"), done=event.get("type") == "finish")

        with mock.patch.dict(engines.ENGINES, {"exits1": ExitsOne("exits1", "ExitsOne", "x", "", "", "")}):
            job_id = jobs.start("custom", {"prompt": "go"}, "exits1")["id"]
            for _ in range(100):
                job = jobs.get(job_id)
                if job["status"] not in ("queued", "running"):
                    break
                _time.sleep(0.1)
        self.assertEqual((job["status"], job["result"]), ("done", "All 10 decks built."))
        self.assertIn("counted as done", job["log"])

    def test_opencode_signals_the_end_of_its_last_step(self):
        from leadsmith import engines

        oc = engines.ENGINES["opencode"]
        self.assertTrue(oc.parse({"type": "step_finish", "part": {"reason": "stop", "cost": 0}}).done)
        self.assertFalse(oc.parse({"type": "step_finish", "part": {"reason": "tool-calls"}}).done)

    def test_deck_runs_have_no_web_search(self):
        import json as _json

        from leadsmith import config, engines

        claude = engines.ENGINES["claude"]
        cmd, _ = claude.command("", scope="deck")
        self.assertIn("WebSearch", cmd[cmd.index("--disallowedTools") + 1].split(","))
        self.assertNotIn("WebSearch", cmd[cmd.index("--allowedTools") + 1].split(","))
        deck_cfg = _json.loads((config.HOME / ".dashboard-mcp-deck.json").read_text(encoding="utf-8"))
        self.assertEqual(deck_cfg["mcpServers"]["leadsmith"]["env"]["LEADSMITH_TOOL_SCOPE"], "deck")
        normal, _ = claude.command("")
        self.assertIn("WebSearch", normal[normal.index("--allowedTools") + 1].split(","))
        codex, _ = engines.ENGINES["codex"].command("", scope="deck")
        self.assertIn('web_search="disabled"', codex)
        oc = engines.ENGINES["opencode"]
        with mock.patch.object(type(oc), "version", lambda self: (2, 2, 5)):
            oc.command("", scope="deck")
            self.assertEqual(oc.workdir("deck"), config.HOME / "opencode-run-deck")  # never overwrites a normal run's
            cfg = _json.loads((oc.workdir("deck") / "opencode.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["permission"]["websearch"], "deny")
            self.assertEqual(cfg["mcp"]["servers"]["leadsmith"]["environment"]["LEADSMITH_TOOL_SCOPE"], "deck")

    def test_deck_runs_only_get_deck_tools(self):
        import asyncio
        import importlib

        from leadsmith import server

        server = importlib.reload(server)
        with mock.patch.dict(os.environ, {"LEADSMITH_AGENT_RUN": "1", "LEADSMITH_TOOL_SCOPE": "deck"}):
            server.apply_agent_mode()
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        self.assertEqual(names, server.SCOPES["deck"])
        self.assertNotIn("save_contact", names)
        importlib.reload(server)


class CheaperRunsTests(unittest.TestCase):
    """Find runs stop before writing; decks go to the deck engine; a silent run is stopped."""

    def test_the_search_brief_reaches_decks_emails_and_rewrites(self):
        from leadsmith import db, jobs

        brief = "Startups entering India. We offer Indian creator and influencer campaigns."
        db.upsert_account("brief-co.com", name="Brief Co")
        db.set_brief(["brief-co.com"], brief)
        db.set_brief(["brief-co.com"], "a later, different search")  # an account keeps the brief it was found for
        self.assertEqual(db.dossier("brief-co.com")["brief"], brief)
        for kind in ("write", "deck", "ideas"):
            _, prompt = jobs._fmt(kind, {"domains": ["brief-co.com"]})
            self.assertIn("WHAT TO PITCH", prompt, kind)
            self.assertIn("Indian creator and influencer campaigns", prompt, kind)
        contact, _ = db.upsert_contact("brief-co.com", first_name="Ria", email="ria@brief-co.com", email_status="public")
        db.upsert_draft(contact["id"], 1, "s", "Hi Ria", "email", 0, {"ok": True, "issues": []})
        _, rewrite = jobs._fmt("rewrite", {"contact_id": contact["id"]})
        self.assertIn("Indian creator and influencer campaigns", rewrite)
        _, plain = jobs._fmt("write", {"domains": ["no-brief.example"]})
        self.assertNotIn("WHAT TO PITCH", plain)

    def test_a_short_search_tops_up_until_the_target(self):
        import time as _time

        from leadsmith import config, db, engines, jobs

        ran, rounds = [], {"n": 0}

        # Fixed timestamps far from any other test's accounts, so "found since the search started" counts only these.
        def fake_run(job_id, kind, prompt, engine, session, model=None):
            if kind == "find_buyers":
                with db.connect() as conn:
                    conn.execute("UPDATE jobs SET started_at = ? WHERE id = ?", ("3001-01-01T00:00:00+00:00", job_id))
            if kind in ("find_buyers", "find_more"):  # each round finds 2, the user asked for 5
                rounds["n"] += 1
                with db.connect() as conn:
                    for i in range(2):
                        conn.execute("INSERT OR REPLACE INTO accounts (domain, name, created_at, updated_at) VALUES "
                                     "(?, 'T', ?, ?)", (f"topup-{rounds['n']}-{i}.com", f"3001-01-01T00:0{rounds['n']}:0{i}+00:00",
                                                        f"3001-01-01T00:0{rounds['n']}:0{i}+00:00"))
            ran.append((kind, prompt))
            jobs._finish(job_id, "done", f"round {rounds['n']}: tried YC and Wellfound", None)
            return True

        config.save_env({"EMAIL_STYLE": "idea_menu"})
        with mock.patch.object(jobs, "_run", fake_run), \
                mock.patch.object(type(engines.ENGINES["claude"]), "path", lambda self: "claude"), \
                mock.patch.object(jobs, "decks_wanted", return_value=False):
            first = jobs.start("find_buyers", {"count": 5, "focus": "fintech startups in Berlin"}, "claude")
            for _ in range(80):
                if ran and ran[-1][0] == "write":
                    break
                _time.sleep(0.05)
        self.assertEqual([k for k, _ in ran], ["find_buyers", "find_more", "find_more", "write"])
        second = ran[1][1]
        self.assertIn("asked for 5", second)
        self.assertIn("3 more NEW accounts", second)  # 2 of 5 found in round 1
        self.assertIn("round 1: tried YC and Wellfound", second)  # it knows what was tried
        self.assertIn("fintech startups in Berlin", second)
        self.assertIn("topup-3-1.com", ran[-1][1])  # emails cover every round's accounts
        self.assertIn("topup-1-0.com", ran[-1][1])

    def test_top_up_is_skipped_when_the_target_is_reached(self):
        import time as _time

        from leadsmith import db, engines, jobs

        ran = []

        def fake_run(job_id, kind, prompt, engine, session, model=None):
            if kind == "find_buyers":
                with db.connect() as conn:
                    conn.execute("UPDATE jobs SET started_at = ? WHERE id = ?", ("2999-01-01T00:00:00+00:00", job_id))
                with db.connect() as conn:
                    for i in range(3):
                        conn.execute("INSERT OR REPLACE INTO accounts (domain, name, created_at, updated_at) VALUES "
                                     "(?, 'E', '2999-01-01T00:00:01+00:00', '2999-01-01T00:00:01+00:00')", (f"enough-{i}.com",))
            ran.append(kind)
            jobs._finish(job_id, "done", "x", None)
            return True

        with mock.patch.object(jobs, "_run", fake_run), \
                mock.patch.object(type(engines.ENGINES["claude"]), "path", lambda self: "claude"), \
                mock.patch.object(jobs, "decks_wanted", return_value=False):
            first = jobs.start("find_buyers", {"count": 3}, "claude")
            for _ in range(80):
                if ran and ran[-1] == "write":
                    break
                _time.sleep(0.05)
        self.assertEqual(ran, ["find_buyers", "write"])
        self.assertIn("Not needed", jobs.get(first["id"] + 1)["result"])

    def test_find_then_decks_then_emails_automatically(self):
        import time as _time

        from leadsmith import config, db, engines, jobs

        _, prompt = jobs._fmt("find_buyers", {"count": 5, "focus": "creators"})
        self.assertIn("don't write ideas, emails or decks yourself", prompt)
        ran = []

        def fake_run(job_id, kind, prompt, engine, session, model=None):
            if kind == "find_buyers":  # the search saves two new accounts
                with db.connect() as conn:
                    conn.execute("UPDATE jobs SET started_at = ? WHERE id = ?", (db.now(), job_id))
                db.upsert_account("autochain-one.com", name="One")
                db.upsert_account("autochain-two.com", name="Two")
            ran.append((kind, engine.key, prompt))
            jobs._finish(job_id, "done", "x", None)
            return True

        config.save_env({"EMAIL_STYLE": "personal_pitch"})
        try:
            with mock.patch.object(jobs, "_run", fake_run), \
                    mock.patch.object(engines, "deck_engine", return_value=(engines.ENGINES["codex"], "", None)), \
                    mock.patch.object(type(engines.ENGINES["claude"]), "path", lambda self: "claude"):
                first = jobs.start("find_buyers", {"count": 2}, "claude")
                for _ in range(60):
                    if len(ran) == 3:
                        break
                    _time.sleep(0.05)
        finally:
            config.save_env({"EMAIL_STYLE": ""})
        self.assertEqual([(k, e) for k, e, _ in ran], [("find_buyers", "claude"), ("deck", "codex"), ("write", "claude")])
        for _, _, step_prompt in ran[1:]:  # decks and emails are for exactly the accounts the search saved
            self.assertIn("autochain-one.com", step_prompt)
            self.assertIn("autochain-two.com", step_prompt)
        with db.connect() as conn:  # the email run in Runs shows the accounts too
            write_job = db.one(conn.execute("SELECT prompt FROM jobs WHERE id > ? AND kind = 'write' ORDER BY id LIMIT 1",
                                            (first["id"],)))
        self.assertIn("autochain-one.com", write_job["prompt"])

    def test_deck_slides_are_written_for_the_prospect(self):
        from leadsmith import jobs

        _, prompt = jobs._fmt("deck", {"domains": ["acme.com"]})
        for rule in ("no URLs or links", "\"account research\"", "for the prospect"):
            self.assertIn(rule, prompt)

    def test_deck_engine_and_fallback(self):
        from leadsmith import config, engines

        opencode = engines.ENGINES["opencode"]
        with mock.patch.object(type(opencode), "path", lambda self: "opencode" if self.key == "opencode" else "x"):
            engine, model, note = engines.deck_engine()
            self.assertEqual((engine.key, model, note), ("opencode", "", None))  # never a Claude model name
        with mock.patch.object(type(opencode), "path", lambda self: None if self.key == "opencode" else "x"):
            engine, _, note = engines.deck_engine()
            self.assertEqual(engine.key, config.get("AGENT_ENGINE") or "claude")
            self.assertIn("isn't installed", note)

    def test_write_with_decks_runs_as_a_chain(self):
        import time as _time

        from leadsmith import config, engines, jobs

        ran = []

        def fake_run(job_id, kind, prompt, engine, session, model=None):
            ran.append((kind, engine.key, session is not None))
            failed = kind == "deck" and engine.key == "codex"  # the deck engine hits its limit
            jobs._finish(job_id, "failed" if failed else "done", "Free usage limit reached" if failed else "x", None)
            return not failed

        config.save_env({"EMAIL_STYLE": "personal_pitch"})
        try:
            with mock.patch.object(jobs, "_run", fake_run), \
                    mock.patch.object(engines, "deck_engine", return_value=(engines.ENGINES["codex"], "", "fallback note")), \
                    mock.patch.object(type(engines.ENGINES["claude"]), "path", lambda self: "claude"):
                first = jobs.start("write", {"domains": ["acme.com"]}, "claude", {"session_id": "s1"})
                for _ in range(50):
                    if len(ran) == 3:
                        break
                    _time.sleep(0.05)
        finally:
            config.save_env({"EMAIL_STYLE": ""})
        # The deck step on the deck engine failed, so the main engine built the decks, then the emails were written.
        self.assertEqual(ran, [("deck", "codex", False), ("deck", "claude", False), ("write", "claude", True)])
        self.assertEqual(first["kind"], "deck")
        write_job = jobs.get(first["id"] + 1)
        self.assertIn("never call save_deck", write_job["prompt"])

    def test_stalled_run_is_stopped(self):
        import sys as _sys
        import time as _time

        from leadsmith import engines, jobs

        class Silent(engines.Engine):
            def path(self):
                return _sys.executable

            def command(self, model, session=None):
                return [_sys.executable, "-c", "import time; time.sleep(600)"], {}

            def parse(self, event):
                return engines.Parsed()

        with mock.patch.dict(engines.ENGINES, {"silent": Silent("silent", "Silent", "x", "", "", "")}), \
                mock.patch.multiple(jobs, POLL_SECONDS=0.2, STALL_SECONDS=0.6):
            job_id = jobs.start("custom", {"prompt": "go"}, "silent")["id"]
            for _ in range(100):
                job = jobs.get(job_id)
                if job["status"] not in ("queued", "running"):
                    break
                _time.sleep(0.1)
        self.assertEqual(job["status"], "failed")
        self.assertIn("made no progress", job["result"])


class LinkedInSearchSourceTests(unittest.TestCase):
    def test_linkedin_labels_group_together_and_profiles_dedupe(self):
        from leadsmith import db

        db.upsert_account("priya-sharma.no-website", name="Priya Sharma", source="linkedin-search",
                          linkedin_url="https://www.linkedin.com/in/priya-sharma-data/")
        db.upsert_account("ravi-teaches.com", name="Ravi Teaches", source="linkedin: data creators Pune")
        stats = {s["source"]: s["leads"] for s in db.source_stats()}
        self.assertGreaterEqual(stats.get("linkedin-search", 0), 2)
        self.assertNotIn("linkedin", stats)
        db.upsert_account("coursecraft.io", name="CourseCraft", source="reddit: r/elearning help request")
        db.upsert_account("lessonly.app", name="Lessonly", source="reddit-search")
        self.assertEqual({s["source"]: s["leads"] for s in db.source_stats()}.get("reddit-search"), 2)
        db.upsert_account("helloworld-avani.no-website", name="Avani", source="instagram: via Elev8or")
        db.upsert_account("thenadcoder.com", name="The Nad Coder", source="ig")
        self.assertEqual({s["source"]: s["leads"] for s in db.source_stats()}.get("instagram-search"), 2)
        db.upsert_account("alexandbooks.com", name="Alex & Books", source="twitter: thread reader")
        db.upsert_account("buildpublic.io", name="Build Public", source="x-search")
        self.assertEqual({s["source"]: s["leads"] for s in db.source_stats()}.get("x-search"), 2)
        db.upsert_account("teenskills.com", name="Teen Skills", source="hiring-boards: course video editor")
        db.upsert_account("adscourse.co", name="Ads Course", source="hiring-boards")
        # The old "hiring-boards" label now counts under the fixed label "job-boards".
        self.assertEqual({s["source"]: s["leads"] for s in db.source_stats()}.get("job-boards"), 2)
        # The same person found again under a website: caught by their profile URL.
        for variant in ("https://linkedin.com/in/priya-sharma-data", "http://in.linkedin.com/in/Priya-Sharma-Data?trk=x"):
            clash = db.find_duplicate("priyasharma.io", "Priya S. Academy", variant)
            self.assertEqual((clash or {}).get("why"), "the same LinkedIn page", variant)
        self.assertIsNone(db.find_duplicate("other.io", "Other", "https://www.linkedin.com/in/priya-sharma-dat"))


class BatchedReplyCheckTests(unittest.TestCase):
    """check_replies asks Gmail a fixed number of questions however many contacts there are."""

    class FakeIMAP:
        INBOX = {  # number -> (From header, raw message)
            b"1": ("Newsletter <news@shop.com>", b"From: news@shop.com\r\n\r\nSale!"),
            b"2": ("Asha <asha@acme.com>", b"From: asha@acme.com\r\nSubject: Re: hi\r\nMessage-ID: <r1@x>\r\n\r\nSounds good, call Tuesday?"),
            b"3": ("Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                   b"From: mailer-daemon@googlemail.com\r\n\r\nYour message wasn't delivered to bad@nowhere.io because the address couldn't be found."),
            b"4": ("Raj <RAJ@Beta.com>", b"From: RAJ@Beta.com\r\nSubject: Re: hi\r\nMessage-ID: <r2@x>\r\n\r\nPlease remove me from your list."),
        }

        def __init__(self, *a, **k):
            self.searches = 0

        def login(self, *a):
            pass

        def select(self, *a, **k):
            pass

        def logout(self):
            pass

        def search(self, charset, *criteria):
            self.searches += 1
            if '"mailer-daemon"' in criteria:
                return "OK", [b"3"]
            return "OK", [b" ".join(self.INBOX)]

        def fetch(self, ids, what):
            out = []
            for n in ids.split(b","):
                frm, raw = self.INBOX[n]
                if "HEADER.FIELDS (FROM)" in what:
                    data = f"From: {frm}\r\n\r\n".encode()
                elif "HEADER.FIELDS (SUBJECT" in what:
                    data = raw.split(b"\r\n\r\n")[0]
                else:
                    data = raw
                out += [(n + b" (BODY[] {%d}" % len(data), data), b")"]
            return "OK", out

    def test_two_searches_for_everyone(self):
        from leadsmith import config, db, mailer

        for i, email in enumerate(["asha@acme.com", "raj@beta.com", "bad@nowhere.io", "quiet@gamma.com", "a@nowhere.io"]):
            contact, _ = db.upsert_contact(f"batch{i}.com", first_name=f"P{i}", email=email)
            db.set_status("contact", contact["id"], "contacted")
        fake = self.FakeIMAP()
        config.save_env({"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "x"})
        try:
            with mock.patch.object(mailer.imaplib, "IMAP4_SSL", return_value=fake):
                result = mailer.check_replies(30)
        finally:
            config.save_env({"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""})
        self.assertEqual(fake.searches, 2)
        self.assertEqual([b["email"] for b in result["bounces"]], ["bad@nowhere.io"])  # not a@nowhere.io
        kinds = {r["email"]: r["kind"] for r in result["replies"]}
        self.assertEqual(kinds, {"asha@acme.com": "reply", "raj@beta.com": "optout"})  # sender case ignored
        self.assertTrue(db.is_suppressed("raj@beta.com"))


class ExtraRecipientTests(unittest.TestCase):
    """Several addresses on one person's emails, and sending a person back to review from Outreach."""

    def call(self, handler, contact_id, payload):
        import asyncio

        from leadsmith import dashboard

        class Req:
            path_params = {"id": contact_id}

        with mock.patch.object(dashboard, "body", side_effect=lambda r: payload):
            return json.loads(asyncio.run(handler(Req())).body)

    def test_extra_addresses_get_the_same_email(self):
        from leadsmith import dashboard, db, mailer

        contact, _ = db.upsert_contact("multi-co.com", first_name="Ana", email="ana@multi-co.com", email_status="public")
        draft = db.upsert_draft(contact["id"], 1, "hello", "Hi Ana, a note about Multi Co.", "email", 0, {"ok": True, "issues": []})
        db.set_status("draft", draft["id"], "approved")
        result = self.call(dashboard.edit_contact_recipients, contact["id"],
                           {"emails": ["hello@multi-co.com", "ANA@multi-co.com", "hello@multi-co.com"]})
        self.assertEqual(result["extra_emails"], ["hello@multi-co.com"])  # main address and duplicates dropped
        self.assertEqual(result["paused"], 1)  # approved for other recipients: back to review
        self.assertEqual(db.get_contact(contact["id"])["extra_emails"], "hello@multi-co.com")
        msg = mailer._build({"subject": "hello", "body": "Hi Ana", "attach_deck": 0}, db.get_contact(contact["id"]), None)
        self.assertIn("ana@multi-co.com", msg["To"])
        self.assertIn("hello@multi-co.com", msg["To"])
        db.suppress("hello@multi-co.com", "test")  # a do-not-contact address is never emailed
        msg = mailer._build({"subject": "hello", "body": "Hi Ana", "attach_deck": 0}, db.get_contact(contact["id"]), None)
        self.assertNotIn("hello@multi-co.com", msg["To"])
        with self.assertRaises(dashboard.ApiError) as bad:
            self.call(dashboard.edit_contact_recipients, contact["id"], {"emails": ["not-an-email"]})
        self.assertIn("doesn't look like an email", str(bad.exception))

    def test_back_to_review_from_outreach(self):
        from leadsmith import dashboard, db

        contact, _ = db.upsert_contact("back-co.com", first_name="Bo", email="bo@back-co.com", email_status="public")
        ids = []
        for step in (1, 2):
            d = db.upsert_draft(contact["id"], step, "s", "Hi Bo", "email", 0, {"ok": True, "issues": []})
            db.set_status("draft", d["id"], "approved")
            ids.append(d["id"])
        with db.connect() as conn:
            conn.execute("UPDATE drafts SET status = 'sent', sent_at = ? WHERE id = ?", (db.now(), ids[0]))
        result = self.call(dashboard.contact_back_to_review, contact["id"], {})
        self.assertEqual(result["moved"], 1)  # the sent email stays sent
        with db.connect() as conn:
            statuses = [r[0] for r in conn.execute("SELECT status FROM drafts WHERE contact_id = ? ORDER BY step", (contact["id"],))]
        self.assertEqual(statuses, ["sent", "draft"])

    def test_replies_and_bounces_from_extra_addresses(self):
        from leadsmith import config, db, mailer

        class Inbox(BatchedReplyCheckTests.FakeIMAP):
            INBOX = {
                b"1": ("Ops <ops@extra-co.com>", b"From: ops@extra-co.com\r\nSubject: Re: hi\r\nMessage-ID: <x1@y>\r\n\r\nYes, let's talk Monday."),
                b"2": ("Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                       b"From: mailer-daemon@googlemail.com\r\n\r\nYour message wasn't delivered to old@bounce-co.com."),
            }

            def search(self, charset, *criteria):
                self.searches += 1
                return ("OK", [b"2"]) if '"mailer-daemon"' in criteria else ("OK", [b" ".join(self.INBOX)])

        replier, _ = db.upsert_contact("extra-co.com", first_name="Ed", email="ed@extra-co.com", email_status="public")
        db.set_extra_emails(replier["id"], ["ops@extra-co.com"])
        keeper, _ = db.upsert_contact("bounce-co.com", first_name="Kim", email="kim@bounce-co.com", email_status="public")
        db.set_extra_emails(keeper["id"], ["old@bounce-co.com", "team@bounce-co.com"])
        for c in (replier, keeper):
            db.set_status("contact", c["id"], "contacted")
        config.save_env({"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "x"})
        try:
            with mock.patch.object(mailer.imaplib, "IMAP4_SSL", return_value=Inbox()):
                result = mailer.check_replies(30)
        finally:
            config.save_env({"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""})
        self.assertIn(replier["id"], {r["contact_id"] for r in result["replies"]})  # a reply from the extra address counts
        self.assertEqual(db.get_contact(replier["id"])["status"], "replied")
        kim = db.get_contact(keeper["id"])
        self.assertEqual(db.extra_emails(kim), ["team@bounce-co.com"])  # only the bounced extra is dropped
        self.assertEqual(kim["status"], "contacted")  # the person's sequence carries on


class GlobalLocationTests(unittest.TestCase):
    """Offline: addresses on business sites in any country, simpler geocoding queries, register name matching."""

    def test_addresses_from_legal_pages_and_structured_data(self):
        from leadsmith.providers import places

        impressum = """<footer><p>© 2026 Jonas Keil. Alle Rechte vorbehalten.</p></footer>
            <h2>Impressum</h2><p>Jonas Keil<br>Torstraße 105–107<br>10119 Berlin</p><p>Tel: +49 30 1234567</p>"""
        self.assertEqual(places.text_addresses(impressum), ["Torstraße 105–107, 10119 Berlin"])
        india = "<p>Address: Flat 302, Vaastu Pleasant, Sai Baba Temple Road, Bengaluru, Karnataka 560037</p>"
        self.assertEqual(len(places.text_addresses(india)), 1)
        jsonld = ('<script type="application/ld+json">{"@type":"Organization","address":{"@type":"PostalAddress",'
                  '"streetAddress":"101 Rose Street South Lane","postalCode":"EH2 3JG","addressLocality":"Edinburgh",'
                  '"addressCountry":"GB"}}</script>')
        self.assertEqual(places.jsonld_addresses(jsonld), ["101 Rose Street South Lane, EH2 3JG, Edinburgh, GB"])
        links = places.legal_links('<a href="/impressum">Impressum</a><a href="/blog">Blog</a>'
                                   '<a href="https://other.com/contact">x</a><a href="/mentions-legales">Mentions</a>',
                                   "https://jonaskeil.com/")
        self.assertEqual(links, ["https://jonaskeil.com/impressum", "https://jonaskeil.com/mentions-legales"])

    def test_simpler_geocoding_queries(self):
        from leadsmith.providers import places

        tries = places.simpler_queries("Flat 302, Vaastu Pleasant, Green Garden Layout, Bengaluru, Karnataka 560037, India")
        self.assertIn("560037, India", tries)
        self.assertIn("Bengaluru, Karnataka 560037, India", tries)

    def test_register_names_must_really_match(self):
        from leadsmith.providers.registries import same_name

        self.assertTrue(same_name("London App Developer", "London App Developer Ltd."))
        self.assertFalse(same_name("Trendytech", "TRENDTECH LTD"))


class YouTubeParsingTests(unittest.TestCase):
    """Offline: the shapes YouTube embeds in its pages, so a layout change shows up here and not mid-run."""

    def setUp(self):
        from leadsmith.providers import youtube
        self.yt = youtube

    def test_ids_and_paths(self):
        yt = self.yt
        self.assertEqual(yt.channel_path("@ankitbansal6"), "/@ankitbansal6")
        self.assertEqual(yt.channel_path("https://www.youtube.com/@ankitbansal6/videos"), "/@ankitbansal6")
        self.assertEqual(yt.channel_path("UCk7NcgnqCmui1AV7MTXZwOw"), "/channel/UCk7NcgnqCmui1AV7MTXZwOw")
        self.assertEqual(yt.video_id("https://youtu.be/huaBtkhPGvw?t=3"), "huaBtkhPGvw")
        self.assertEqual(yt.video_id("https://www.youtube.com/shorts/huaBtkhPGvw"), "huaBtkhPGvw")
        with self.assertRaises(ValueError):
            yt.video_id("https://example.com")
        self.assertEqual(yt.count("189K subscribers"), 189000)
        self.assertEqual(yt.count("1.01M subscribers"), 1010000)

    def test_embedded_json_and_search(self):
        data = {"contents": [{"channelRenderer": {
            "channelId": "UCk7NcgnqCmui1AV7MTXZwOw", "title": {"simpleText": "Ankit Bansal"},
            "subscriberCountText": {"simpleText": "@ankitbansal6"}, "videoCountText": {"simpleText": "189K subscribers"},
            "descriptionSnippet": {"runs": [{"text": "SQL and "}, {"text": "data engineering"}]},
            "navigationEndpoint": {"browseEndpoint": {"canonicalBaseUrl": "/@ankitbansal6"}}}}]}
        html = f"<script>var ytInitialData = {__import__('json').dumps(data)};</script>"
        found = self.yt.parse_search(self.yt.embedded_json(html, "ytInitialData"))
        self.assertEqual(found[0]["handle"], "@ankitbansal6")
        self.assertEqual(found[0]["subscribers_count"], 189000)
        self.assertEqual(found[0]["description"], "SQL and data engineering")

    def test_about_feed_and_cadence(self):
        from datetime import datetime, timezone

        about = {"x": {"aboutChannelViewModel": {
            "description": "Business: hello@namastesql.com", "country": "India", "channelId": "UCk7NcgnqCmui1AV7MTXZwOw",
            "subscriberCountText": "189K subscribers", "canonicalChannelUrl": "http://www.youtube.com/@ankitbansal6",
            "links": [{"channelExternalLinkViewModel": {"title": {"content": "NamasteSQL"}, "link": {"content": "namastesql.com"}}}]}}}
        info = self.yt.parse_about(about, '<meta property="og:title" content="Ankit Bansal">')
        self.assertEqual((info["country"], info["name"]), ("India", "Ankit Bansal"))
        self.assertEqual(info["emails_in_description"], ["hello@namastesql.com"])
        self.assertEqual(info["links"], [{"title": "NamasteSQL", "url": "namastesql.com"}])
        feed = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"
          xmlns:media="http://search.yahoo.com/mrss/">
          <entry><yt:videoId>huaBtkhPGvw</yt:videoId><title>AI Data Agents Full Course</title>
            <link rel="alternate" href="https://www.youtube.com/watch?v=huaBtkhPGvw"/><published>2026-11-01T10:00:00+00:00</published>
            <media:group><media:description>Live class</media:description><media:community><media:statistics views="1400"/></media:community></media:group></entry>
          <entry><yt:videoId>abcdefghijk</yt:videoId><title>Tip</title>
            <link rel="alternate" href="https://www.youtube.com/shorts/abcdefghijk"/><published>2026-06-01T10:00:00+00:00</published></entry>
        </feed>"""
        uploads = self.yt.parse_feed(feed)
        self.assertEqual([u["short"] for u in uploads], [False, True])
        self.assertEqual(uploads[0]["views"], 1400)
        activity = self.yt.cadence(uploads, datetime(2026, 11, 4, tzinfo=timezone.utc))
        self.assertEqual((activity["uploads_last_30_days"], activity["uploads_last_90_days"], activity["shorts_in_latest"]), (1, 1, 1))
        self.assertEqual(activity["days_since_last_upload"], 3)

    def test_storyboard_sheets_skip_the_intro(self):
        spec = ("https://i.ytimg.com/sb/huaBtkhPGvw/storyboard3_L$L/$N.jpg?sqp=abc|48#27#100#10#10#0#default#rs$A"
                "|160#90#679#5#5#10000#M$M#rs$B|320#180#679#3#3#10000#M$M#rs$C")
        sheets = self.yt.storyboard_sheets(spec, 6774, 2)
        self.assertEqual(len(sheets), 2)
        self.assertIn("storyboard3_L2/M11.jpg", sheets[0]["url"])  # 76 sheets of 9 frames: 15% in
        self.assertTrue(sheets[0]["url"].endswith("&sigh=rs$C"))
        self.assertEqual(sheets[0]["covers"], "16:30-18:00")
        self.assertEqual(self.yt.storyboard_sheets("", 100), [])


class IdeasSuggestionTests(unittest.TestCase):
    def test_suggestion_improves_ideas_and_emails(self):
        from leadsmith import jobs
        title, prompt = jobs._fmt("ideas", {"domains": ["acme.com"], "extra": "focus on their hiring push"})
        self.assertTrue(title.startswith("Improve ideas"))
        self.assertIn("focus on their hiring push", prompt)
        self.assertIn("save_ideas", prompt)
        self.assertIn("unsent draft", prompt)

    def test_no_suggestion_writes_fresh_ideas(self):
        from leadsmith import jobs
        title, prompt = jobs._fmt("ideas", {"domains": ["acme.com"], "extra": "  "})
        self.assertTrue(title.startswith("New ideas"))
        self.assertIn("fresh ideas", prompt)


if __name__ == "__main__":
    unittest.main()
