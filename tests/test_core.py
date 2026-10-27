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
        self.assertNotIn("Playbook", jobs.full_prompt("find_buyers", "Find 5 buyers.", engines.ENGINES["claude"]))

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
