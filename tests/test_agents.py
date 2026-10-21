"""Offline tests: SMTP, IMAP, HTTP and DNS are all faked. Run: python -m unittest discover -s tests"""
import datetime as dt
import email
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from email import policy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENV = {"OUTREACH_EMAIL": "hello@sender.example", "OUTREACH_PASSWORD": "x", "OUTREACH_FROM_NAME": "Sam Sender",
       "OUTREACH_SMTP_HOST": "smtp.example", "OUTREACH_IMAP_HOST": "imap.example",
       "OUTREACH_LEGAL_NAME": "Sender Studio Ltd", "OUTREACH_COMPANY_NUMBER": "00000000",
       "OUTREACH_ADDRESS": "1 Test Street, Testtown, TT1 1TT", "COMPANIES_HOUSE_API_KEY": "k",
       "OUTREACH_PUBLIC_URL": "https://demo.example", "OUTREACH_APPROVE_FIRST": "0",
       "OUTREACH_DATA_DIR": tempfile.mkdtemp()}
os.environ.update(ENV)

from outreach_agents import builder, companies, config, contacts, postman, reader, scout, server, writer  # noqa: E402
from outreach_agents.store import Store  # noqa: E402

MONDAY_10AM = dt.datetime(2026, 10, 19, 10, 0)


def prospect(store, **kw):
    row = dict(name="Acme Plumbing", website="https://acme.example", email="info@acme.example",
               sector="craft=plumber", city="Leeds", legal_name="ACME PLUMBING LIMITED", status="ready")
    row.update(kw)
    pid = store.add_prospect(**row)
    return store.prospect(pid)


class Base(unittest.TestCase):
    def setUp(self):
        config.reload()
        self.store = Store()

    def tearDown(self):
        self.store.db.close()


class ContactsTest(Base):
    def test_rank_keeps_role_addresses_on_their_own_domain(self):
        got = contacts.rank(["Jo@Acme.example", "info@acme.example", "noreply@acme.example", "x@gmail.com",
                             "sales@other.example"], "acme.example")
        self.assertEqual(got, ["info@acme.example", "jo@acme.example"])

    def test_company_numbers_found_on_a_page(self):
        self.assertEqual(companies.numbers_in("Registered in England, Company No. 1234567."), ["01234567"])


class ScoutTest(Base):
    def test_chains_and_platform_sites_are_skipped(self):
        el = lambda i, web, name="Shop": {"type": "node", "id": i, "tags": {"name": name, "website": web,  # noqa: E731
                                                                              "shop": "bakery"}}
        data = {"elements": [el(1, "https://chain.example/stores/leeds"), el(2, "https://chain.example"),
                             el(3, "https://chain.example"), el(4, "https://facebook.com/bakery"),
                             el(5, "https://indie.example", "Indie Bakery")]}
        self.assertEqual([b["name"] for b in scout.parse_osm(data, "Leeds")], ["Indie Bakery"])

    def _check(self, final_url="https://www.indie.example/", verified=True, suppressed=False):
        c = contacts.Contacts(url=final_url, emails=["hello@indie.example"], numbers=["01234567"], reachable=True)
        if suppressed:
            self.store.suppress("hello@indie.example", "test")
        prof = ({"company_number": "01234567", "company_name": "INDIE LTD"}, "number on site") if verified else (None, "no match")
        with mock.patch.object(contacts, "scan", return_value=c), \
                mock.patch.object(companies, "verify", return_value=prof), \
                mock.patch.object(contacts, "accepts_mail", return_value=True):
            return scout.check(self.store, {"name": "Indie", "website": "https://indie.example", "osm_id": "n1"})

    def test_verified_company_is_ready(self):
        self.assertEqual(self._check(), "ready")

    def test_redirect_to_another_company_is_rejected(self):
        self.assertEqual(self._check(final_url="https://bigfirm.example/"), "rejected")
        self.assertIn("another company", self.store.by_status("rejected")[0]["reject_reason"])

    def test_not_a_limited_company_is_rejected(self):
        self.assertEqual(self._check(verified=False), "rejected")

    def test_suppressed_address_is_never_ready(self):
        self.assertEqual(self._check(suppressed=True), "rejected")


class WriterTest(Base):
    def test_checks_and_tidy(self):
        s, b = writer.tidy("Hello!", "Hi,\n\n" + "word " * 60 + "\n\nAny thoughts?\n\nSam")
        self.assertEqual(writer.why_not(s, b), "")
        self.assertIn("banned", writer.why_not("A game-changer", b))

    def test_falls_back_to_template_when_the_model_fails(self):
        with mock.patch.object(writer.llm, "chat_json", side_effect=RuntimeError("down")):
            s, b = writer.first({"name": "Acme Plumbing", "sector": "craft=plumber"})
        self.assertEqual(b.count(writer.LINK), 1)
        self.assertIn("Acme Plumbing", s)

    def test_footer_names_the_sender_and_the_opt_out(self):
        f = writer.footer("https://demo.example/optout?t=abc", "ACME PLUMBING LIMITED")
        for part in ("Sender Studio Ltd", "00000000", "1 Test Street", "optout?t=abc", "Acme Plumbing Ltd"):
            self.assertIn(part, f)


class FakeSMTP:
    sent = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, **k):
        pass

    def login(self, *a):
        pass

    def send_message(self, m):
        FakeSMTP.sent.append(m)
        return {}


class PostmanTest(Base):
    def setUp(self):
        super().setUp()
        FakeSMTP.sent = []
        self.p = [mock.patch.object(postman.smtplib, "SMTP", FakeSMTP), mock.patch.object(postman, "on_spamhaus", return_value=False),
                  mock.patch.object(writer.llm, "chat_json", side_effect=RuntimeError("offline"))]
        for p in self.p:
            p.start()
        self.store.save_demo("acme-abcd", business_name="Acme Plumbing", site_url="https://acme.example",
                             welcome="Hi", suggestions=[], chunks=[], status="live")

    def tearDown(self):
        for p in self.p:
            p.stop()
        super().tearDown()

    def test_ramp_and_cap(self):
        self.assertEqual(postman.quota([], "2026-10-19"), 5)
        many = [f"2026-09-{d:02d}" for d in range(1, 30)]
        self.assertEqual(postman.quota(many, "2026-10-19"), config.settings.daily_cap)

    def test_sends_with_identity_opt_out_and_unsubscribe_header(self):
        prospect(self.store, demo_slug="acme-abcd")
        out = postman.tick(self.store, MONDAY_10AM)
        self.assertTrue(out.startswith("sent to Acme Plumbing"), out)
        m = FakeSMTP.sent[0]
        body = m.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("https://demo.example/demo/acme-abcd", body)
        self.assertIn("Sender Studio Ltd", body)
        self.assertIn("/optout?t=", body)
        self.assertIn("optout?t=", m["List-Unsubscribe"])
        self.assertEqual(self.store.by_status("contacted")[0]["step"], 1)

    def test_suppression_checked_right_before_sending(self):
        p = prospect(self.store, demo_slug="acme-abcd")
        mail = postman.compose(self.store, p)
        self.store.suppress("info@acme.example", "opted out a second ago")
        with self.assertRaises(postman.SendError):
            postman.send_prospect(self.store, p, mail)
        self.assertEqual(FakeSMTP.sent, [])
        self.assertEqual(self.store.prospect(p["id"])["status"], "opted_out")

    def test_never_sends_without_the_footer(self):
        p = prospect(self.store, demo_slug="acme-abcd")
        with self.assertRaises(postman.SendError):
            postman.send_prospect(self.store, p, {"subject": "x", "body": "no footer", "step": 0})
        self.assertEqual(FakeSMTP.sent, [])

    def test_nothing_outside_sending_hours_or_without_identity(self):
        prospect(self.store, demo_slug="acme-abcd")
        self.assertEqual(postman.tick(self.store, dt.datetime(2026, 10, 18, 10, 0)), "outside sending hours")  # Sunday
        with mock.patch.dict(os.environ, {"OUTREACH_ADDRESS": ""}):
            config.reload()
            self.assertTrue(postman.tick(self.store, MONDAY_10AM).startswith("setup needed"))
        self.assertEqual(FakeSMTP.sent, [])

    def test_first_emails_wait_for_approval(self):
        with mock.patch.dict(os.environ, {"OUTREACH_APPROVE_FIRST": "2"}):
            config.reload()
            p = prospect(self.store, demo_slug="acme-abcd")
            postman.tick(self.store, MONDAY_10AM)
            self.assertEqual(FakeSMTP.sent, [])
            self.assertEqual(len(postman.drafts(self.store)), 1)
            postman.approve(self.store, p["id"])
            self.assertTrue(postman.tick(self.store, MONDAY_10AM).startswith("sent"))

    def test_provider_limit_pauses_until_tomorrow(self):
        prospect(self.store, demo_slug="acme-abcd")
        err = postman.SendError("limit", "limit")
        with mock.patch.object(postman, "smtp_send", side_effect=err):
            postman.tick(self.store, MONDAY_10AM)
        self.assertTrue(postman.tick(self.store, MONDAY_10AM).startswith("paused"))


def raw_mail(text: str, **headers) -> email.message.EmailMessage:
    m = email.message.EmailMessage()
    m["From"] = headers.pop("From", "Jo <info@acme.example>")
    m["Subject"] = headers.pop("Subject", "Re: Quick idea")
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(text)
    return email.message_from_bytes(m.as_bytes(), policy=policy.default)


class ReaderTest(Base):
    def test_unsubscribe_reply_suppresses_instantly(self):
        p = prospect(self.store, status="contacted")
        self.assertTrue(reader.handle(self.store, raw_mail("Please remove me from your list")).startswith("opted out"))
        self.assertTrue(self.store.suppressed("info@acme.example"))
        self.assertEqual(self.store.prospect(p["id"])["status"], "opted_out")

    def test_hard_bounce_suppresses(self):
        prospect(self.store, status="contacted")
        m = raw_mail("Status: 5.1.1 user unknown", From="Mail Delivery <mailer-daemon@x.example>",
                     X_Failed_Recipients="info@acme.example")
        self.assertEqual(reader.handle(self.store, m), "hard bounce info@acme.example")
        self.assertTrue(self.store.suppressed("info@acme.example"))

    def test_out_of_office_waits_a_week(self):
        p = prospect(self.store, status="contacted", next_send_at="2026-10-20T09:00:00+00:00")
        reader.handle(self.store, raw_mail("I'm away", Subject="Automatic reply: Quick idea"))
        self.assertGreater(self.store.prospect(p["id"])["next_send_at"], "2026-10-25")

    def test_interested_reply_stops_the_sequence_and_notifies(self):
        p = prospect(self.store, status="contacted")
        with mock.patch.object(reader.llm, "chat_json", return_value={"label": "interested", "summary": "wants a call",
                                                                      "draft": "Thanks"}), \
                mock.patch.object(reader, "notify") as n:
            self.assertEqual(reader.handle(self.store, raw_mail("Looks good, call me")), "interested: Acme Plumbing")
        n.assert_called_once()
        self.assertEqual(self.store.prospect(p["id"])["status"], "replied")


class BuilderTest(Base):
    def test_answers_only_from_the_right_chunk(self):
        chunks = [{"url": "u1", "title": "Opening hours", "content": "We are open Monday to Friday 8am to 6pm."},
                  {"url": "u2", "title": "Boilers", "content": "We fit and service gas boilers across Leeds."}]
        self.store.save_demo("acme-abcd", business_name="Acme", site_url="https://acme.example", welcome="Hi",
                             suggestions=[], chunks=chunks, status="live")
        self.assertEqual(builder.top_chunks(chunks, "Do you service boilers?")[0]["url"], "u2")
        with mock.patch.object(builder.llm, "chat", return_value="Yes, we do.") as chat:
            out = builder.answer(self.store, "acme-abcd", "Do you service boilers?")
        self.assertIn("gas boilers", chat.call_args.args[0])
        self.assertEqual(out["answer"], "Yes, we do.")
        self.assertFalse(builder.answer(self.store, "acme-abcd", "zebra crossing")["answered"])


class ServerTest(Base):
    def test_opening_the_link_never_unsubscribes_but_the_button_does(self):
        p = prospect(self.store)
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.store))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/optout?t={p['optout_token']}"
        open_ = urllib.request.build_opener(urllib.request.ProxyHandler({})).open   # local server: no proxy
        try:
            page = open_(url).read().decode()   # what an email security scanner does
            self.assertIn("Yes, unsubscribe me", page)
            self.assertFalse(self.store.suppressed("info@acme.example"))
            open_(urllib.request.Request(url, data=b"List-Unsubscribe=One-Click", method="POST"))
            self.assertTrue(self.store.suppressed("info@acme.example"))
        finally:
            httpd.shutdown()
            httpd.server_close()


class StoreTest(Base):
    def test_domain_wide_suppression_and_duplicate_sites(self):
        self.store.suppress("@acme.example", "whole company asked")
        self.assertTrue(self.store.suppressed("anyone@acme.example"))
        self.assertIsNotNone(self.store.add_prospect(name="A", website="https://a.example"))
        self.assertIsNone(self.store.add_prospect(name="A again", website="https://a.example"))
        self.store.set_state("k", {"a": 1})
        self.assertEqual(self.store.get_state("k"), {"a": 1})
        self.assertEqual(json.loads(json.dumps(self.store.get_state("missing", []))), [])


if __name__ == "__main__":
    unittest.main()
