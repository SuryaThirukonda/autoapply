"""goat.mail.gmail: pure helpers plus the polling loop against a fake Gmail client."""
from __future__ import annotations

import base64
import logging

import pytest

from goat.mail import gmail


GREENHOUSE_BODY = """
<html><head><style>.x{color:red}</style></head><body>
<p>Hi Surya,</p>
<p>Someone requested a security code to continue your application with Acme Robotics.</p>
<p>Your security code is <strong>Y4hx2CrV</strong></p>
<p>This code expires in 10 minutes. If you did not request it, you can ignore this message.</p>
<p>Reference AB12CD34EF is for internal use. Visit https://boards.greenhouse.io/acme/track/Qz8Lm2Np for status.</p>
<p>Thursday November Security Password Download Continue</p>
</body></html>
"""


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _msg(mid: str, ts: int, sender: str, subject: str, body: str, html: bool = False, snippet: str = "") -> dict:
    return {
        "id": mid,
        "threadId": "t" + mid,
        "internalDate": str(ts * 1000),
        "snippet": snippet,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Tue, 14 Oct 2026 10:00:00 +0000"},
            ],
            "parts": [
                {"mimeType": "text/html" if html else "text/plain", "body": {"data": _b64url(body)}},
            ],
        },
    }


class FakeMailClient:
    """Returns `batches[i]` on the i-th list call (last batch repeats); get by id."""

    def __init__(self, batches: list[list[dict]]):
        self.batches = batches
        self.list_calls = 0
        self.get_calls: list[str] = []
        self.queries: list[str] = []
        self._by_id = {m["id"]: m for b in batches for m in b}

    def list_messages(self, q: str, max_results: int = 10) -> list[dict]:
        self.queries.append(q)
        batch = self.batches[min(self.list_calls, len(self.batches) - 1)]
        self.list_calls += 1
        return [{"id": m["id"], "threadId": m["threadId"]} for m in batch[:max_results]]

    def get_message(self, msg_id: str) -> dict:
        self.get_calls.append(msg_id)
        return self._by_id[msg_id]


# ---------------------------------------------------------------- pure helpers

def test_extract_code_greenhouse_realistic_body():
    text = gmail.strip_html(GREENHOUSE_BODY)
    assert gmail.extract_code(text, "greenhouse") == "Y4hx2CrV"


def test_extract_code_prefers_mixed_token_and_skips_words_and_urls():
    text = "Security Password Thursday. code: ABCDEFGH https://x.io/Ab12Cd34 then ZZ99yy11 later."
    # plain word next to "code" is accepted only when nothing mixed exists; mixed wins here
    assert gmail.extract_code(text, "greenhouse") == "ZZ99yy11"
    assert gmail.extract_code("Security Password Thursday continue", "greenhouse") is None


def test_extract_code_six_digits_near_code_word():
    text = "Order 123456 shipped. Your verification code is 987654. Zip 778401."
    assert gmail.extract_code(text, "workday") == "987654"
    assert gmail.extract_code("nothing here 12345", "oracle") is None


def test_is_forbidden():
    assert gmail.is_forbidden("Reset your password", "")
    assert gmail.is_forbidden("Chase: new login", "")
    assert gmail.is_forbidden("Your invoice", "see attached")
    assert gmail.is_forbidden("hello", "we noticed a sign-in attempt")
    assert not gmail.is_forbidden("Your Greenhouse security code", "Your security code is ...")


def test_sender_allowed():
    assert gmail.sender_allowed("Greenhouse <no-reply@us.greenhouse-mail.io>", "greenhouse", "boards.greenhouse.io")
    assert gmail.sender_allowed("no-reply@greenhouse.io", "greenhouse", "boards.greenhouse.io")
    assert not gmail.sender_allowed("no-reply@greenhouse-mail.io.evil.com", "greenhouse", "boards.greenhouse.io")
    assert not gmail.sender_allowed("hr@acme.com", "greenhouse", "boards.greenhouse.io")
    assert gmail.sender_allowed("acme@myworkday.com", "workday", "acme.wd5.myworkdayjobs.com")
    # unknown portal: From must share the portal host's registrable domain
    assert gmail.sender_allowed("careers@mail.acme.com", "custom", "jobs.acme.com")
    assert not gmail.sender_allowed("careers@acme-mail.com", "custom", "jobs.acme.com")
    assert not gmail.sender_allowed("", "greenhouse", "boards.greenhouse.io")


def test_link_host_ok_workday_tenants():
    me = "acme.wd5.myworkdayjobs.com"
    assert gmail.link_host_ok("https://acme.wd5.myworkdayjobs.com/en-US/acme/verify?t=1", me)
    assert gmail.link_host_ok("https://acme.wd5.myworkdayjobs.com/en-US/acme/verify?t=1", me, "workday")
    assert not gmail.link_host_ok("https://other.wd1.myworkdayjobs.com/verify", me)
    assert not gmail.link_host_ok("https://other.wd1.myworkdayjobs.com/verify", me, "workday")
    assert not gmail.link_host_ok("https://myworkdayjobs.com.evil.net/verify", me)
    assert not gmail.link_host_ok("not a url", me)


def test_link_host_ok_company_domain_allows_siblings():
    assert gmail.link_host_ok("https://www.acme.com/account/confirm", "careers.acme.com")
    assert gmail.link_host_ok("https://acme.com/confirm", "careers.acme.com")
    assert not gmail.link_host_ok("https://acme.co/confirm", "careers.acme.com")


def test_registrable_domain():
    assert gmail.registrable_domain("acme.wd5.myworkdayjobs.com") == "myworkdayjobs.com"
    assert gmail.registrable_domain("jobs.acme.co.uk") == "acme.co.uk"
    assert gmail.registrable_domain("acme.com") == "acme.com"
    assert gmail.registrable_domain("ACME.COM:443") == "acme.com"


def test_extract_links():
    raw = '<a href="https://a.io/verify?x=1&amp;y=2">go</a> see https://b.io/p. and https://a.io/verify?x=1&amp;y=2'
    assert gmail.extract_links(raw) == ["https://a.io/verify?x=1&y=2", "https://b.io/p"]


def test_build_code_query_is_narrow():
    q = gmail.build_code_query("greenhouse", "boards.greenhouse.io", 1700000000)
    assert q.startswith("from:(greenhouse-mail.io OR greenhouse.io) after:1700000000 newer_than:1d")
    assert 'subject:"Security code"' in q
    q2 = gmail.build_code_query("custom", "jobs.acme.com", 5)
    # a portal known only by its host also takes the career-site platforms' senders (Qualcomm on Eightfold, 2026-10-21)
    assert q2.startswith("from:(acme.com OR eightfold.ai") and " after:5 " in q2


# ---------------------------------------------------------------- polling with a fake client

def test_fetch_code_picks_newest_and_hides_code(caplog):
    caplog.set_level(logging.DEBUG, logger="goat.mail")
    older = _msg("m1", 1_700_000_100, "Greenhouse <no-reply@us.greenhouse-mail.io>",
                 "Your Greenhouse security code", "Your security code is Ab12Cd34. It expires soon.")
    newer = _msg("m2", 1_700_000_200, "Greenhouse <no-reply@us.greenhouse-mail.io>",
                 "Your Greenhouse security code", GREENHOUSE_BODY, html=True)
    too_old = _msg("m0", 1_699_999_000, "Greenhouse <no-reply@us.greenhouse-mail.io>",
                   "Your Greenhouse security code", "Your security code is Zz99Yy88.")
    spoof = _msg("m3", 1_700_000_300, "Greenhouse <no-reply@greenhouse-mail.io.evil.com>",
                 "Your Greenhouse security code", "Your security code is Ev1lC0de.")
    reset = _msg("m4", 1_700_000_400, "Greenhouse <no-reply@us.greenhouse-mail.io>",
                 "Reset your password", "Your security code is Rs3tC0de.")
    client = FakeMailClient([[older, newer, too_old, spoof, reset]])

    handle = gmail.fetch_code("greenhouse", "boards.greenhouse.io", 1_700_000_000,
                              timeout_s=0, poll_s=0, client=client)
    assert handle is not None
    assert handle.message_id == "m2"
    assert handle.length() == 8
    assert str(handle) == "CodeHandle(len=8, msg=m2)"
    assert "Y4hx2CrV" not in str(handle) and "Y4hx2CrV" not in repr(handle)
    assert "Y4hx2CrV" not in caplog.text and "Ab12Cd34" not in caplog.text
    assert "Ev1lC0de" not in caplog.text and "Rs3tC0de" not in caplog.text

    typed: list[str] = []
    assert handle.enter(typed.append) == "entered"
    assert typed == ["Y4hx2CrV"]
    assert client.list_calls == 1


def test_fetch_code_polls_until_mail_arrives_then_times_out():
    sleeps: list[float] = []
    arriving = _msg("m9", 1_700_000_050, "no-reply@us.greenhouse-mail.io",
                    "Your Greenhouse security code", "Your security code is Qw3rTy78")
    client = FakeMailClient([[], [], [arriving]])
    h = gmail.fetch_code("greenhouse", "boards.greenhouse.io", 1_700_000_000,
                         timeout_s=100, poll_s=4, client=client, _sleep=sleeps.append)
    assert h is not None and h.message_id == "m9"
    assert client.list_calls == 3 and sleeps == [4, 4]

    empty = FakeMailClient([[]])
    h2 = gmail.fetch_code("greenhouse", "boards.greenhouse.io", 1_700_000_000,
                          timeout_s=0, poll_s=4, client=empty, _sleep=sleeps.append)
    assert h2 is None and empty.list_calls == 1


def test_fetch_verification_link_host_rules():
    me = "acme.wd5.myworkdayjobs.com"
    body = ('Welcome! <a href="https://acme.wd5.myworkdayjobs.com/en-US/acme/login">Sign in</a> or '
            '<a href="https://acme.wd5.myworkdayjobs.com/en-US/acme/verify?token=abc">Verify your email</a>')
    good = _msg("w1", 1_700_000_100, "acme@myworkday.com", "Verify your email", body, html=True)
    res = gmail.fetch_verification_link("workday", me, 1_700_000_000, timeout_s=0, poll_s=0,
                                        client=FakeMailClient([[good]]))
    assert res.status == "verified_link"
    assert res.url == "https://acme.wd5.myworkdayjobs.com/en-US/acme/verify?token=abc"
    assert res.message_id == "w1"

    other = _msg("w2", 1_700_000_100, "acme@myworkday.com", "Verify your email",
                 "https://other.wd1.myworkdayjobs.com/en-US/other/verify?token=abc")
    res2 = gmail.fetch_verification_link("workday", me, 1_700_000_000, timeout_s=0, poll_s=0,
                                         client=FakeMailClient([[other]]))
    assert res2 == gmail.LinkResult("host_mismatch", None, "w2")

    res3 = gmail.fetch_verification_link("workday", me, 1_700_000_000, timeout_s=0, poll_s=0,
                                         client=FakeMailClient([[]]))
    assert res3 == gmail.LinkResult("not_received", None, None)


def test_find_confirmation_email():
    ok = _msg("c1", 1_700_000_100, "Acme Recruiting <no-reply@us.greenhouse-mail.io>",
              "Thank you for applying to Acme Robotics", "We received your application.",
              snippet="We received your application to Acme Robotics")
    newer = _msg("c2", 1_700_000_200, "Acme Recruiting <no-reply@us.greenhouse-mail.io>",
                 "Your application was received", "", snippet="Acme Robotics received your application")
    bad = _msg("c3", 1_700_000_300, "billing@acme.com", "Acme invoice received", "", snippet="payment")
    client = FakeMailClient([[ok, newer, bad]])
    assert gmail.find_confirmation_email("Acme Robotics, Inc.", 1_700_000_000, "greenhouse", client=client) == "c2"
    assert client.queries[0].endswith("after:1700000000 (Acme OR Robotics)") and "Inc" not in client.queries[0]
    assert gmail.find_confirmation_email("Acme Robotics", 1_700_000_000, None, client=FakeMailClient([[]])) is None


def test_a_confirmation_that_names_only_part_of_the_company_still_matches_by_sender():
    """ICF International (2026-10-18): 'Thank you for applying!' from icf@myworkday.com never says 'International'."""
    icf = _msg("i1", 1_700_000_100, "ICF Human Resources <icf@myworkday.com>", "Thank you for applying!",
               "Thanks for applying to the Software Engineer Intern role.", snippet="Thanks for applying")
    other = _msg("i2", 1_700_000_200, "Jobs <no-reply@international-careers.com>", "Thank you for applying",
                 "Thanks for applying to International Paper.", snippet="International Paper")
    assert gmail.find_confirmation_email("ICF International", 1_700_000_000, None, client=FakeMailClient([[icf, other]])) == "i1"
    assert gmail.find_confirmation_email("ICF International", 1_700_000_000, None, client=FakeMailClient([[other]])) is None


def test_gmail_client_without_token_raises_mail_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(gmail.SETTINGS.mail, "token_json", str(tmp_path / "missing_token.json"))
    with pytest.raises(gmail.MailUnavailable):
        gmail.GmailClient().list_messages("x")


def test_other_workday_tenant_email_does_not_hide_ours():
    """Penn State's verification email arrived first; Brunswick's arrived later. The Brunswick run
    must take Brunswick's link, and a code from another tenant is never accepted."""
    me = "brunswick.wd1.myworkdayjobs.com"
    psu = _msg("p1", 1_700_000_300, "psu@otp.workday.com", "Verify your candidate account",
               "https://psu.wd1.myworkdayjobs.com/en-US/psu/verify?token=x")
    ours = _msg("b1", 1_700_000_200, "brunswick@otp.workday.com", "Verify your candidate account",
                "https://brunswick.wd1.myworkdayjobs.com/en-US/search/verify?token=y")
    res = gmail.fetch_verification_link("workday", me, 1_700_000_000, timeout_s=0, poll_s=0,
                                        client=FakeMailClient([[psu, ours]]))
    assert res.status == "verified_link" and res.message_id == "b1"
    assert gmail.tenant_mismatch("psu@otp.workday.com", me)
    assert not gmail.tenant_mismatch("brunswick@otp.workday.com", me)
    assert not gmail.tenant_mismatch("no-reply@myworkday.com", me)
    assert not gmail.sender_allowed("psu@otp.workday.com", "workday", me)


def test_oracle_confirm_your_identity_subject_and_pod_check():
    q = gmail.build_code_query("oracle", "hctz.fa.us2.oraclecloud.com", 1_700_000_000)
    assert "Identity" in q and "Confirm" in q
    sender = "hctz.fa.sender@workflow.email.us-phoenix-1.ocs.oraclecloud.com"
    assert gmail.sender_allowed(sender, "oracle", "hctz.fa.us2.oraclecloud.com")
    assert not gmail.sender_allowed("ebxs.fa.sender@workflow.email.us-phoenix-1.ocs.oraclecloud.com", "oracle", "hctz.fa.us2.oraclecloud.com")


def test_fetch_password_reset_link_prefers_the_reset_url():
    me = "alliance.wd3.myworkdayjobs.com"
    body = ('<a href="https://alliance.wd3.myworkdayjobs.com/en-US/nissanjobs/login">Sign in</a> '
            '<a href="https://alliance.wd3.myworkdayjobs.com/en-US/nissanjobs/resetPassword?token=xyz">Reset your password</a>')
    good = _msg("r1", 1_700_000_100, "alliance@myworkday.com", "Reset your password", body, html=True)
    res = gmail.fetch_password_reset_link("workday", me, 1_700_000_000, timeout_s=0, poll_s=0, client=FakeMailClient([[good]]))
    assert res.status == "reset_link"
    assert res.url == "https://alliance.wd3.myworkdayjobs.com/en-US/nissanjobs/resetPassword?token=xyz"
    none = gmail.fetch_password_reset_link("workday", me, 1_700_000_000, timeout_s=0, poll_s=0, client=FakeMailClient([[]]))
    assert none == gmail.LinkResult("not_received", None, None)


def test_a_stale_connection_is_rebuilt_and_retried(monkeypatch):
    """The cached Gmail service went stale while idle and the next call raised an SSLError (2026-10-18): the client
    rebuilds the service and retries; a permanent error (4xx) is not retried."""
    import ssl
    from types import SimpleNamespace
    built = []
    calls = {"n": 0}

    def make_service():
        built.append(1)

        class Req:
            def execute(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ssl.SSLError("EOF occurred in violation of protocol")
                return {"messages": [{"id": "m1"}]}
        msgs = SimpleNamespace(list=lambda **k: Req(), get=lambda **k: Req())
        return SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: msgs))
    monkeypatch.setattr(gmail, "_build_service", make_service)
    monkeypatch.setattr(gmail.time, "sleep", lambda s: None)
    c = gmail.GmailClient()
    assert c.list_messages("from:x") == [{"id": "m1"}]
    assert len(built) == 2 and calls["n"] == 2

    class Http4xx(Exception):
        resp = SimpleNamespace(status=403)

    def make_forbidden():
        class Req:
            def execute(self):
                raise Http4xx("forbidden")
        msgs = SimpleNamespace(list=lambda **k: Req())
        return SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: msgs))
    monkeypatch.setattr(gmail, "_build_service", make_forbidden)
    c2 = gmail.GmailClient()
    import pytest as _pt
    with _pt.raises(gmail.MailUnavailable):
        c2.list_messages("from:x")


def test_gmail_calls_never_overlap_across_threads():
    """2026-10-19 02:54: two threads retried on the one shared connection after an SSLError and the agent died with
    an access violation. Calls now run one at a time."""
    import threading
    import time as _t
    from goat.mail import gmail as g

    inside, peak = [0], [0]

    class Req:
        def execute(self):
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
            _t.sleep(0.05)
            inside[0] -= 1
            return {"messages": []}

    class Svc:
        def users(self):
            return self

        def messages(self):
            return self

        def list(self, **kw):
            return Req()

    c = g.GmailClient()
    c._service = Svc()
    ts = [threading.Thread(target=c.list_messages, args=("q",)) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert peak[0] == 1


def test_a_rate_limited_403_is_retried_and_a_poll_survives_a_gmail_hiccup(monkeypatch):
    """2026-10-19 04:02: Gmail answered 403 rateLimitExceeded; the reset-link poll failed outright and Nike held."""
    from goat.mail import gmail as g

    class Resp:
        status = 403

    class RateLimited(Exception):
        resp = Resp()

        def __str__(self):
            return '<HttpError 403 "rateLimitExceeded">'

    class Forbidden(Exception):
        resp = Resp()

        def __str__(self):
            return '<HttpError 403 "insufficientPermissions">'

    assert g.GmailClient._transient(RateLimited())
    assert not g.GmailClient._transient(Forbidden())

    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class Client:
        def list_messages(self, q, n=10):
            calls["n"] += 1
            if calls["n"] == 1:
                raise g.MailUnavailable("Gmail list failed (HttpError)")
            return []

        def get_message(self, mid):
            raise AssertionError("nothing listed")

    clock = {"t": 0.0}
    monkeypatch.setattr(g.time, "monotonic", lambda: clock["t"])

    def fake_sleep(s):
        clock["t"] += s

    assert g._poll_newest(Client(), "q", "workday", "x.wd1.myworkdayjobs.com", 0, 30, 5, fake_sleep) is None
    assert calls["n"] >= 2                      # kept polling after the failed listing

    class Down:
        def list_messages(self, q, n=10):
            raise g.MailUnavailable("Gmail list failed (HttpError)")

    clock["t"] = 0.0
    import pytest as _pytest
    with _pytest.raises(g.MailUnavailable):     # never listed once: say so instead of "not received"
        g._poll_newest(Down(), "q", "workday", "x.wd1.myworkdayjobs.com", 0, 12, 5, fake_sleep)


def test_an_oracle_code_from_the_employers_own_domain_is_found():
    """2026-10-19: American Express's Oracle access code came from careers@recruitment.americanexpress.com; goat only
    searched oraclecloud.com / oracle.com, waited out the code and held the job."""
    from goat.mail import gmail as g
    assert g.company_sender_tokens("American Express")[0] == "americanexpress"
    assert g.company_sender_tokens("CSX Corporation") == ["csx"]
    q = g.build_code_query("oracle", "egug.fa.us2.oraclecloud.com", 0, g.company_sender_tokens("American Express"))
    assert "americanexpress" in q
    frm = "Amex Careers <careers@recruitment.americanexpress.com>"
    assert not g.sender_allowed(frm, "oracle", "egug.fa.us2.oraclecloud.com")
    assert g.sender_allowed(frm, "oracle", "egug.fa.us2.oraclecloud.com", ["americanexpress"])
    assert not g.sender_allowed("Promo <deals@americanexpressoffers.example>", "oracle", "egug.fa.us2.oraclecloud.com", ["americanexpress"])



def test_a_workday_tenant_mailing_from_its_own_domain_is_found():
    """Invesco (2026-10-20): 'Verify your candidate account' came from Workday@invesco.com, outside Workday's domains."""
    me = "invesco.wd1.myworkdayjobs.com"
    body = '<a href="https://invesco.wd1.myworkdayjobs.com/invesco/activate/abc123?redirect=x">Verify</a>'
    m = _msg("v1", 1_700_000_100, "Workday@invesco.com", "Verify your candidate account", body, html=True)
    client = FakeMailClient([[m]])
    res = gmail.fetch_verification_link("workday", me, 1_700_000_000, timeout_s=0, poll_s=0, client=client)
    assert res.status == "verified_link" and "activate/abc123" in res.url
    assert "invesco" in client.queries[0]
    assert gmail.workday_tenant_tokens("workday", "wd5.myworkdayjobs.com") == []
    assert gmail.workday_tenant_tokens("greenhouse", "boards.greenhouse.io") == []
    # another company's mail is still refused
    other = _msg("v2", 1_700_000_100, "Workday@acme.com", "Verify your candidate account", body, html=True)
    res2 = gmail.fetch_verification_link("workday", me, 1_700_000_000, timeout_s=0, poll_s=0, client=FakeMailClient([[other]]))
    assert res2.status != "verified_link"



def test_a_reset_email_whose_link_names_a_bank_tenant_is_read():
    """Q2 (2026-10-20): the link's tenant 'q2ebanking' contains 'bank' and the reset email was dropped."""
    me = "q2ebanking.wd5.myworkdayjobs.com"
    link = "https://q2ebanking.wd5.myworkdayjobs.com/Q2/passwordreset/abc123/?redirect=x"
    m = _msg("r1", 1_700_000_100, '"Workday Notification" <q2ebanking@otp.workday.com>', "Reset your password for your candidate account",
             f'<a href="{link}">Reset</a>', html=True, snippet=f"Click this link to reset your password {link}")
    res = gmail.fetch_password_reset_link("workday", me, 1_700_000_000, timeout_s=0, poll_s=0, client=FakeMailClient([[m]]))
    assert res.status == "reset_link" and "passwordreset" in res.url
    # a real bank email stays unread
    assert gmail.is_forbidden("Your Chase statement is ready", "Your bank statement")


def test_a_tenant_mailing_under_its_parents_name_is_read_when_its_link_is_ours():
    """Dow Jones (2026-10-20): the reset came from newscorp@otp.workday.com with a link on dowjones.wd1."""
    me = "dowjones.wd1.myworkdayjobs.com"
    link = "https://dowjones.wd1.myworkdayjobs.com/DowJones/passwordreset/xyz/"
    m = _msg("r2", 1_700_000_100, '"workday.hr newscorp" <newscorp@otp.workday.com>', "Reset your password for your candidate account",
             f'<a href="{link}">Reset</a>', html=True)
    res = gmail.fetch_password_reset_link("workday", me, 1_700_000_000, timeout_s=0, poll_s=0, client=FakeMailClient([[m]]))
    assert res.status == "reset_link"
    # another tenant's link is still refused
    other = _msg("r3", 1_700_000_100, '"x" <newscorp@otp.workday.com>', "Reset your password for your candidate account",
                 '<a href="https://newscorp.wd1.myworkdayjobs.com/x/passwordreset/1/">Reset</a>', html=True)
    res2 = gmail.fetch_password_reset_link("workday", me, 1_700_000_000, timeout_s=0, poll_s=0, client=FakeMailClient([[other]]))
    assert res2.status != "reset_link"



def test_an_oracle_code_from_the_employers_initials_domain_is_found():
    """Texas Instruments (2026-10-20): its Oracle code came from ti_myhiring_no-reply@recruiting.ti.com and the run never
    found it, asked Surya, and asked him again."""
    from goat.mail import gmail as g
    toks = g.company_sender_tokens("Texas Instruments")
    assert toks == ["texasinstruments", "ti"]
    assert "gdit" in g.company_sender_tokens("General Dynamics Information Technology")
    assert g.company_sender_tokens("The Toro Company") == ["toro"]
    assert g.company_sender_tokens("The Cigna Group") == ["cigna"]
    q = g.build_code_query("oracle", "edbz.fa.us2.oraclecloud.com", 0, toks)
    assert " ti" in q or "(ti" in q or "OR ti)" in q
    frm = "TI <ti_myhiring_no-reply@recruiting.ti.com>"
    assert g.sender_allowed(frm, "oracle", "edbz.fa.us2.oraclecloud.com", toks)
    assert not g.sender_allowed("Promo <deals@tickets.example.com>", "oracle", "edbz.fa.us2.oraclecloud.com", toks)


def test_a_platform_sender_counts_for_a_portal_known_only_by_its_host():
    """Qualcomm (2026-10-21): the Eightfold site's code came from confirm@eightfold.ai; the search looked only for qualcomm."""
    from goat.mail import gmail
    assert gmail.sender_allowed("Eightfold <confirm@eightfold.ai>", "other", "careers.qualcomm.com", ["qualcomm"])
    assert not gmail.sender_allowed("Deals <promo@shop.example.com>", "other", "careers.qualcomm.com", ["qualcomm"])
    q = gmail.build_code_query("other", "careers.qualcomm.com", 1792579000, ["qualcomm"])
    assert "eightfold.ai" in q and "qualcomm.com" in q



def test_a_distinctive_first_word_finds_an_employer_that_mails_under_its_short_name():
    """Citizens Financial Group (Oracle, 2026-10-21): the code mail never matched 'citizensfinancial' or 'cfg'."""
    from goat.mail.gmail import company_sender_tokens, sender_allowed
    toks = company_sender_tokens("Citizens Financial Group")
    assert "citizens" in toks, toks
    assert sender_allowed("Citizens Careers <careers@citizensbank.com>", "oracle", "hcgn.fa.us2.oraclecloud.com", toks)
    assert not sender_allowed("Deals <x@citizensoffers.example>", "oracle", "hcgn.fa.us2.oraclecloud.com", toks)
    assert sender_allowed("Talent Team <no-reply@notifications.example.com>", "oracle", "hcgn.fa.us2.oraclecloud.com", toks) is False
    assert "american" not in company_sender_tokens("American Express")



def test_oracle_fusion_mail_on_the_employer_domain_is_found():
    """Berkshire Hathaway Energy (Oracle, 2026-10-22): the code came from noreply_fusion_essf@oci.brkenergy.com."""
    from goat.mail.gmail import build_code_query, company_sender_tokens, sender_allowed
    host = "fa-essf-saasfaprod1.fa.ocs.oraclecloud.com"
    toks = company_sender_tokens("Berkshire Hathaway Energy")
    q = build_code_query("oracle", host, 1792649600, toks)
    assert "noreply_fusion_essf " in q or "noreply_fusion_essf)" in q, q
    assert "@" not in q.split("after:")[0], q
    assert sender_allowed("noreply_fusion_essf@oci.brkenergy.com", "oracle", host, toks)
    assert not sender_allowed("offers@brkenergy-deals.example", "oracle", host, toks)


def test_a_reset_mailed_from_the_employers_own_domain_is_found():
    """PACCAR (2026-10-22): its SuccessFactors password resets come from DoNotReply@paccar.com with a link on the
    SuccessFactors host; only SuccessFactors' own senders were searched and the run parked 'no reset email'."""
    me = "career5.successfactors.eu"
    link = "https://career5.successfactors.eu/career?company=paccarinc&career_ns=password_reset&token=abc"
    m = _msg("p1", 1_700_000_100, "PACCAR Recruiting <DoNotReply@paccar.com>", "Password Reset", f'<a href="{link}">Reset</a>', html=True)
    assert gmail.fetch_password_reset_link("successfactors", me, 1_700_000_000, timeout_s=0, poll_s=0,
                                           client=FakeMailClient([[m]])).status == "not_received"
    res = gmail.fetch_password_reset_link("successfactors", me, 1_700_000_000, timeout_s=0, poll_s=0,
                                          client=FakeMailClient([[m]]), company="Paccar")
    assert res.status == "reset_link" and res.url == link
    assert "paccar" in gmail.build_reset_query("successfactors", me, 1_700_000_000, gmail.company_sender_tokens("PACCAR"))


def test_a_letters_only_greenhouse_code_beats_the_word_after_code():
    """Neuralink (2026-10-23): the 8-letter mixed-case code sat more than 40 characters from "code" and 'resubmit' was typed."""
    body = ("Copy and paste this code into the security code field on your application:\n# qWErTyUi\n\n"
            "After you enter the code, resubmit your application.\n\n© 2026 Greenhouse")
    assert gmail.extract_code(body, "greenhouse") == "qWErTyUi"


def test_an_oracle_code_from_a_short_acronym_domain_is_found():
    """UL Solutions (2026-10-24): its Oracle code came from ULTalentAcquisition@mail.ta.comms.ul.com; 'ul' was too short to be
    a token, and the run waited 10 minutes and asked Surya for a code that was in the inbox."""
    from goat.mail import gmail as g
    toks = g.company_sender_tokens("UL Solutions")
    assert "ul" in toks
    host = "fa-eups-saasfaprod1.fa.ocs.oraclecloud.com"
    assert " ul" in g.build_code_query("oracle", host, 0, toks) or "OR ul" in g.build_code_query("oracle", host, 0, toks)
    assert g.sender_allowed("UL Solutions Talent Acquisition <ULTalentAcquisition@mail.ta.comms.ul.com>", "oracle", host, toks)
    assert not g.sender_allowed("Deals <promo@ultimate-deals.example.com>", "oracle", host, toks)
    assert "iat" in g.company_sender_tokens("IAT Insurance Group")
    assert g.company_sender_tokens("CSX Corporation") == ["csx"]           # no duplicate
