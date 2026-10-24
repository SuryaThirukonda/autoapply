"""Surya's 2026-10-15 rules: blank and conditional answers, default No on employer/affiliation
questions, free-response questions drafted (never asked), auto consent/signature, questionnaire
seeding with phrase matches and per-company exceptions, and editable remembered answers."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from goat import answers, db, ledger
from goat.agent.tools import RunContext, ToolDispatcher, is_affiliation_question, is_free_response
from goat.budget import Budget
from goat.cli import seed_questionnaire

QUESTIONNAIRE = """
identity:
  identity.middle_name:
    question: "Legal middle name"
    answer: "(leave blank)"
    type: blank
    aliases: ["middle name"]
availability:
  availability.relocate_own_expense:
    question: "Relocate at own expense: Yes only if the posting lists pay"
    answer: "Yes / No"
    type: conditional:posting_lists_pay
    match: [["relocat", "at your own expense"]]
  availability.passport:
    question: "Passport?"
    answer: null
preferences:
  preferences.applied_before:
    question: "Applied before?"
    answer: "Yes / No"
    type: conditional:ledger_company
    match: ["previously applied"]
consent:
  consent.signature_date:
    question: "Date beside a signature"
    answer: "today"
    type: conditional:today
    aliases: ["today's date"]
relationships:
  relationships.know_anyone_at_company:
    question: "Know anyone at the company?"
    answer: "No"
    ask_for_companies: ["Southwest Airlines"]
    match: [["know anyone", "work"]]
experience:
  experience.years_professional:
    question: "Professional experience"
    answer: "1-2 years; 1 - 2 years; 1"
    type: choice_chain
    match: ["years of professional experience"]
"""


@pytest.fixture()
def store(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    p = tmp_path / "q.yaml"
    p.write_text(QUESTIONNAIRE, encoding="utf-8")
    out = seed_questionnaire(p)
    return out


def _job(company="Acme", listing="L1"):
    with db.tx() as con:
        con.execute("INSERT OR IGNORE INTO listings(id,company,title,url,ats) VALUES(?,?,?,?, 'greenhouse')",
                    (listing, company, "Software Engineering Intern", f"https://boards.greenhouse.io/{company.lower()}/jobs/1"))
        cur = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES(?,'filling','no_account',1,1)", (listing,))
        jid = cur.lastrowid
    return dict(db.connect().execute("SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=?", (jid,)).fetchone())


class StubEx:
    url = "https://boards.greenhouse.io/acme/jobs/1"

    async def screenshot_png(self):
        raise RuntimeError("no browser in this test")


class FakeClassifier:
    def __init__(self, answer="No"):
        self.answer = answer
        self.calls = []

    async def yes_no_from_profile(self, job_id, question, options, facts):
        self.calls.append((question, facts))
        return self.answer, "profile lists no such employer"

    async def map_question(self, *a, **k):
        return None, 0.0


def _dispatcher(job, posting_text="", classifier=None):
    ctx = RunContext(job={**job, "resume_file": None}, ex=StubEx(), budget=Budget(job["id"], "greenhouse"),
                     classifier=classifier or FakeClassifier())
    ctx.posting_text = posting_text
    return ToolDispatcher(ctx), ctx


# --------------------------------------------------------------------------- seeding
def test_seed_honors_type_match_and_company_exceptions(store):
    assert "availability.passport" in store["still_null"]
    mid = answers.get("identity.middle_name")
    assert mid.is_blank and mid.has_value and answers.concrete_value(mid) == ("", "leave this field empty")
    assert answers.lookup("Are you willing to relocate at your own expense?").key == "availability.relocate_own_expense"
    sw = answers.lookup("Do you know anyone who works at Southwest Airlines?", "Southwest Airlines")
    assert sw.key == "relationships.know_anyone_at_company" and not sw.has_value      # Surya decides for Southwest
    other = answers.lookup("Do you know anyone who works at Acme?", "Acme")
    assert other.has_value and other.answer == "No"
    # Re-seeding is idempotent and an edited match list replaces the old one.
    again = seed_questionnaire_text(store, QUESTIONNAIRE.replace('["previously applied"]', '["applied here before"]'))
    assert again["saved"] == []
    assert answers.lookup("Have you previously applied?") is None
    assert answers.lookup("Have you applied here before?").key == "preferences.applied_before"


def seed_questionnaire_text(_store, text):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "q.yaml"
        p.write_text(text, encoding="utf-8")
        return seed_questionnaire(p)


# --------------------------------------------------------------------------- conditional values
def test_posting_pay_detection():
    assert answers.posting_pay("Compensation: $25.00 - $30.00 hourly")
    assert answers.posting_pay("The base salary range is $85,000 - $110,000 per year.")
    assert answers.posting_pay("We raised $40M in Series B funding") is None
    assert answers.posting_pay("Great benefits and a 401(k)") is None


def test_conditional_answers(store):
    reloc = answers.get("availability.relocate_own_expense")
    assert answers.concrete_value(reloc, posting_text="Pay: $22/hr")[0] == "Yes"
    assert answers.concrete_value(reloc, posting_text="Competitive pay and benefits")[0] == "No"
    today = answers.concrete_value(answers.get("consent.signature_date"))[0]
    assert today == date.today().strftime("%m/%d/%Y")
    ab = answers.get("preferences.applied_before")
    assert answers.concrete_value(ab, company="Point72")[0] == "No"
    ledger.record_application("Lx", "Point72", "Data Intern", "https://point72.test/1", "goat")
    assert answers.concrete_value(ab, company="Point72")[0] == "Yes"
    assert answers.concrete_value(ab, company="Point72", listing_id="Lx")[0] == "No"   # this very listing does not count


@pytest.mark.asyncio
async def test_fill_value_comes_from_the_posting(store):
    job = _job()
    d, _ = _dispatcher(job, posting_text="Hourly pay: $24/hr")
    val, src, err = d._resolve_value("availability.relocate_own_expense", None, None, "Relocate at own expense?")
    assert (val, err) == ("Yes", None) and src.startswith("standing:availability.relocate_own_expense")
    d2, _ = _dispatcher(job, posting_text="No pay information")
    assert d2._resolve_value("availability.relocate_own_expense", None, None, "x")[0] == "No"
    assert d2._resolve_value("identity.middle_name", None, None, "Middle name")[0] == ""


# --------------------------------------------------------------------------- default No / draft, never ask
def test_question_family_detection():
    assert is_affiliation_question("Are you currently employed by a mortgage servicer?", [])
    assert is_affiliation_question("Have you ever worked for Acme or its subsidiaries?", ["Yes", "No"])
    assert is_affiliation_question("Is any relative employed here?", ["Select...", "Yes", "No"])
    assert not is_affiliation_question("Do you have experience working with Python?", ["Yes", "No"])
    assert not is_affiliation_question("Years of experience in the insurance industry", ["0", "1-2", "3+"])
    assert is_free_response("Why do you want to work at Acme?", [], "text")
    assert is_free_response("Additional comments", [], "textarea")
    assert not is_free_response("Emergency contact name", [], "text")


@pytest.mark.asyncio
async def test_affiliation_question_settles_no_and_is_remembered(store):
    job = _job()
    clf = FakeClassifier("No")
    d, _ = _dispatcher(job, classifier=clf)
    out, _ = await d.t_lookup_answer("Are you currently employed by a bank or credit union?", ["Yes", "No"])
    assert out["answer"] == "No" and out["via"] == "profile_default_no"
    assert clf.calls == []          # deterministic since 2026-10-18: the question names nobody from the profile
    again = answers.lookup("Are you currently employed by a bank or credit union?")
    assert again.answer == "No" and again.source == "default_no"


@pytest.mark.asyncio
async def test_company_exception_asks_instead_of_defaulting(store):
    job = _job("Southwest Airlines", "L2")
    d, _ = _dispatcher(job)
    out, _ = await d.t_lookup_answer("Do you know anyone who currently works at Southwest Airlines?", ["Yes", "No"])
    assert out.get("miss") and "ask_user" in out["hint"]


@pytest.mark.asyncio
async def test_ask_user_drafts_prose_and_settles_affiliations(store):
    job = _job()
    d, _ = _dispatcher(job)
    out, _ = await d.t_ask_user([
        {"question": "Why are you interested in this role?", "options": [], "field_type": "textarea"},
        {"question": "Are you employed by a competitor of Acme?", "options": ["Yes", "No"], "field_type": "radio"},
    ])
    assert out["asked"] == []
    assert out["draft_yourself"] == ["Why are you interested in this role?"]
    assert out["auto_answered"][0]["answer"] == "No"
    assert db.connect().execute("SELECT COUNT(*) n FROM pending_questions").fetchone()["n"] == 0


# --------------------------------------------------------------------------- consents and signature (browser)
CONSENT_FORM = """<!doctype html><html><body><form>
<label><input type="checkbox" id="c1" name="cert"> I certify that the information provided is true and complete.</label>
<label><input type="checkbox" id="c2" name="priv"> I have read and agree to the Privacy Policy</label>
<label><input type="checkbox" id="c3" name="mkt"> Send me marketing emails and job alerts</label>
<label><input type="checkbox" id="c4" name="talent"> Join our talent community</label>
<label for="sig">Electronic Signature</label><input id="sig" name="sig" type="text">
<label for="dt">Today's Date</label><input id="dt" name="dt" type="text">
</form></body></html>"""


@pytest.mark.asyncio
async def test_prefill_ticks_consents_signs_and_dates(tmp_path):
    from goat.adapters import Adapter
    from goat.browser import snapshot
    from goat.browser.executor import Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    answers.seed_from_config(__import__("goat.config", fromlist=["SETTINGS"]).SETTINGS.config_dir)
    answers.save_answer("consent.signature_date", "today", answer_type="conditional:today", source="questionnaire")
    page = tmp_path / "consent.html"
    page.write_text(CONSENT_FORM, encoding="utf-8")
    url = page.resolve().as_uri()
    job = _job()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        res = await Adapter().prefill(ex, snap, job, 1)
        checked = await ex.page.evaluate("() => ['c1','c2','c3','c4'].map(id => document.getElementById(id).checked)")
        assert checked == [True, True, False, False]
        assert await ex.page.input_value("#sig") == "Surya Thirukonda"
        assert await ex.page.input_value("#dt") == date.today().strftime("%m/%d/%Y")
        srcs = {r["source"] for r in db.connect().execute("SELECT source FROM decisions WHERE job_id=?", (job["id"],))}
        assert {"standing:consent.truthfulness_attestation", "standing:consent.privacy_notice", "standing:consent.signature_date"} <= srcs
        assert res.failed == 0
    finally:
        await ex.close(close_browser=True)


# --------------------------------------------------------------------------- dashboard edits
def test_dashboard_edit_and_forget(store):
    from goat import dashboard
    rows = dashboard.api_answers({"kind": ["questionnaire"]})
    assert any(r["key"] == "experience.years_professional" for r in rows)
    out = dashboard.edit_answer({"key": "experience.years_professional", "answer": "Less than 1 year"})
    assert out["ok"]
    a = answers.get("experience.years_professional")
    assert a.answer == "Less than 1 year" and a.source == "dashboard" and a.answer_type == "choice_chain"
    out = dashboard.edit_answer({"key": "experience.years_professional", "forget": True})
    assert out["ok"] and answers.get("experience.years_professional") is None
    # nothing is deleted: both versions remain in the table
    n = db.connect().execute("SELECT COUNT(*) n FROM answers WHERE key='experience.years_professional'").fetchone()["n"]
    assert n == 2


# --------------------------------------------------------------------------- Workday date widget
WORKDAY_DATE = """<!doctype html><html><body>
<div data-automation-id="formField-startDate"><label for="m">Start Date</label>
<div data-automation-id="dateInputWrapper">
<input id="m" data-automation-id="dateSectionMonth-input" aria-label="Month" size="2">
<input data-automation-id="dateSectionDay-input" aria-label="Day" size="2">
<input data-automation-id="dateSectionYear-input" aria-label="Year" size="4">
</div></div></body></html>"""


@pytest.mark.asyncio
async def test_set_date_fills_segmented_widget(tmp_path):
    from goat.browser import fill, snapshot
    from goat.browser.executor import Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    page = tmp_path / "date.html"
    page.write_text(WORKDAY_DATE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        month = next(i for i in snap.items if "Start Date" in (i.get("label") or "") or i.get("name") == "m" or "Month" in (i.get("label") or ""))
        r = await fill.set_date(ex, month["ref"], "05/18/2027")
        vals = await ex.page.evaluate("() => Array.from(document.querySelectorAll('input')).map(i => i.value)")
        assert r.ok, r.error
        assert vals == ["05", "18", "2027"]
    finally:
        await ex.close(close_browser=True)


# --------------------------------------------------------------------------- consent questions, Workday posting text
def test_consent_question_mapping():
    from goat.agent.tools import consent_question_key
    assert consent_question_key("Do you consent to Acme collecting your biometric information?") == "consent.privacy_notice"
    assert consent_question_key("Do you authorize a background check?") == "compliance.background_check_consent"
    assert consent_question_key("Would you like to receive marketing emails about new job opportunities?") == "consent.marketing_email"
    assert consent_question_key("Do you agree to receive SMS text messages about your application?") == "consent.sms"
    assert consent_question_key("Are you willing to relocate?") is None


@pytest.mark.asyncio
async def test_consent_question_is_not_asked(tmp_path):
    from goat.config import SETTINGS
    db.reset_for_tests(tmp_path / "t.db")
    answers.seed_from_config(SETTINGS.config_dir)
    job = _job()
    d, _ = _dispatcher(job)
    out, _ = await d.t_ask_user([{"question": "Do you consent to Acme and its service providers collecting your biometric information?",
                                  "options": ["Yes, I Consent", "No, I Do Not Consent"], "field_type": "select"}])
    assert out["asked"] == [] and out["auto_answered"][0]["key"] == "consent.privacy_notice"
    out2, _ = await d.t_lookup_answer("Do you consent to Acme collecting your biometric information?", ["Yes, I Consent", "No, I Do Not Consent"])
    assert out2["answer"] == "Yes" and out2["via"] == "consent_rule"


def test_workday_api_url():
    from goat.scraper import workday_api_url
    u = "https://assurant.wd1.myworkdayjobs.com/en-US/Assurant_Careers/job/Illinois-Virtual/Summer-2027-Intern_R-115717/apply/applyManually"
    assert workday_api_url(u) == "https://assurant.wd1.myworkdayjobs.com/wday/cxs/assurant/Assurant_Careers/job/Illinois-Virtual/Summer-2027-Intern_R-115717"
    assert workday_api_url("https://boards.greenhouse.io/acme/jobs/1") is None


def test_best_option_handles_spelling_and_consent_style_options():
    from goat.browser.fill import best_option
    O = lambda *t: [{"text": x} for x in t]
    deg = O("Select One", "Associates", "Bachelors", "Master's", "PhD")
    assert best_option(deg, ["Bachelor of Science", "Bachelor's Degree", "Bachelor's", "Undergraduate"])["text"] == "Bachelors"
    sms = O("Select One", "I agree to receive SMS/MMS messages from Acme about my application.", "Please do not send me SMS/MMS messages.")
    assert best_option(sms, ["Yes"])["text"].startswith("I agree")
    assert best_option(sms, ["No"])["text"].startswith("Please do not")
    assert best_option(O("Yes", "No"), ["No"])["text"] == "No"
    # never guesses between two affirmative options
    assert best_option(O("I agree by email", "I agree by text", "No thanks"), ["Yes"]) is None


# --------------------------------------------------------------------------- portal error pages
def test_portal_error_detection():
    from goat.gate import portal_error
    assert portal_error("Something went wrong Please refresh the page and then try again. Error Code: VPS|073c6fcd")
    assert portal_error("502 Bad Gateway")
    assert portal_error("Please complete all required fields before continuing.") is None


@pytest.mark.asyncio
async def test_park_on_portal_error_is_immediate_and_retried_later(store):
    from goat import states
    from goat.budget import Parked
    from goat.worker import PORTAL_RETRY_AFTER_S, requeue_portal_errors

    class ErrorPage(StubEx):
        async def page_text(self, n=30000):
            return "Review Something went wrong Please refresh the page and then try again. Error Code: VPS|abc"

    job = _job()
    ctx = RunContext(job={**job, "resume_file": None}, ex=ErrorPage(), budget=Budget(job["id"], "greenhouse"), classifier=FakeClassifier())
    d = ToolDispatcher(ctx)
    with pytest.raises(Parked) as e:
        await d.t_park("application page error: Workday error page")
    assert e.value.state == "hold" and str(e.value.reason).startswith("portal_error:")
    with db.tx() as con:
        con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=?, attempts=1 WHERE id=?",
                    (states.HOLD, e.value.reason, 1000, job["id"]))
    assert requeue_portal_errors(now=1000 + PORTAL_RETRY_AFTER_S - 1) == 0          # too soon
    assert requeue_portal_errors(now=1000 + PORTAL_RETRY_AFTER_S + 1) == 1
    row = db.connect().execute("SELECT status, hold_reason, tab_target_id FROM jobs WHERE id=?", (job["id"],)).fetchone()
    assert row["status"] == states.QUEUED and row["hold_reason"] is None and row["tab_target_id"] is None


@pytest.mark.asyncio
async def test_park_waits_on_questions_still_open(store):
    from goat.budget import Parked

    class PlainPage(StubEx):
        async def page_text(self, n=30000):
            return "Please provide your permanent address."

    job = _job()
    ctx = RunContext(job={**job, "resume_file": None}, ex=PlainPage(), budget=Budget(job["id"], "greenhouse"), classifier=FakeClassifier())
    ctx.asked_keys = ["q.some_open_question", "identity.middle_name"]     # the second one has a stored answer
    ctx.park_attempts = 4                                                  # past the gate re-check
    with pytest.raises(Parked) as e:
        await ToolDispatcher(ctx).t_park("no_standing_answer: permanent address")
    assert e.value.blocked_on == ["q.some_open_question"]


def test_field_map_covers_location_and_combined_address():
    from goat.adapters import GENERIC_FIELD_MAP

    def key_for(label):
        return next((k for p, k, _ in GENERIC_FIELD_MAP if p.search(label)), None)
    assert key_for("Location (City)") == "identity.location_city"
    assert key_for("Please provide your permanent address (city, state, zip code).") == "identity.city_state_zip"
    assert key_for("City") == "identity.city"


# --------------------------------------------------------------------------- react-select preference order
FAKE_REACT_SELECT = """<!doctype html><html><body>
<label for="inp">Are you Hispanic/Latino?</label>
<div class="select__control" id="ctl"><div id="sv">Select...</div><input id="inp" data-goat-ref="e900"></div>
<script>
  const ctl = document.getElementById('ctl');
  const opts = [{label: 'Yes', value: 1}, {label: 'No', value: 2}, {label: 'Decline To Self Identify', value: 3}];
  ctl['__reactFiber$test'] = {memoizedProps: {options: opts, selectOption: (o) => {
    document.getElementById('sv').textContent = o.label; window.picked = o.label; }}, return: null};
</script></body></html>"""


@pytest.mark.asyncio
async def test_react_select_takes_the_first_preferred_answer_not_the_first_option(tmp_path):
    from goat.browser import fill
    from goat.browser.executor import Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    page = tmp_path / "rs.html"
    page.write_text(FAKE_REACT_SELECT, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        r = await fill.choose(ex, "e900", ["decline", "Decline to self-identify", "I do not wish to answer", "No"])
        assert r.ok, r.error
        assert await ex.page.evaluate("() => window.picked") == "Decline To Self Identify"
    finally:
        await ex.close(close_browser=True)


# --------------------------------------------------------------------------- checkbox answers (Danaher, Workday)
DISCLOSURE_FORM = """<!doctype html><html><body><form>
<fieldset><legend>Please select the race/ethnicity which most accurately describes how you identify yourself.</legend>
<label><input type="checkbox" id="r1"> Asian (United States of America)</label>
<label><input type="checkbox" id="r2"> Not Specified (United States of America)</label>
<label><input type="checkbox" id="r3"> White (United States of America)</label></fieldset>
<label><input type="checkbox" id="t1"> Yes, I have read and consent to the terms and conditions.</label>
</form></body></html>"""


@pytest.mark.asyncio
async def test_checkbox_answers_decline_and_yes_lists(tmp_path):
    from goat.browser import snapshot
    from goat.browser.executor import Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    decline = "decline; Decline to self-identify; I don't wish to answer; Prefer not to say; Not Specified"
    answers.save_answer("self_id.race_ethnicity", decline, answer_type="choice_chain", source="questionnaire")
    answers.save_answer("bank.terms-to-continue", "Yes; I agree; Accept; I acknowledge", answer_type="text", source="seed")
    page = tmp_path / "disc.html"
    page.write_text(DISCLOSURE_FORM, encoding="utf-8")
    url = page.resolve().as_uri()
    job = _job()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        ctx = RunContext(job={**job, "resume_file": None}, ex=ex, budget=Budget(job["id"], "workday"), classifier=FakeClassifier())
        ctx.last_snapshot = snap
        refs = {}
        for i in snap.items:
            for name in ("Asian", "Not Specified", "terms and conditions"):
                if name in (i.get("label") or ""):
                    refs[name] = i["ref"]
        for g in snap.groups:
            for o in g.get("options", []):
                for name in ("Asian", "Not Specified"):
                    if name in (o.get("label") or ""):
                        refs[name] = o["ref"]
        d = ToolDispatcher(ctx)
        out, _ = await d.t_fill_batch([
            {"ref": refs["Not Specified"], "answer_key": "self_id.race_ethnicity", "label": "race"},
            {"ref": refs["Asian"], "answer_key": "self_id.race_ethnicity", "label": "race"},
            {"ref": refs["terms and conditions"], "answer_key": "bank.terms-to-continue", "label": "terms"},
        ])
        status = {r["ref"]: r["status"] for r in out["results"]}
        assert status == {refs["Not Specified"]: "set", refs["Asian"]: "failed", refs["terms and conditions"]: "set"}, out
        checked = await ex.page.evaluate("() => ['r1','r2','r3','t1'].map(id => document.getElementById(id).checked)")
        assert checked == [False, True, False, True]
    finally:
        await ex.close(close_browser=True)


def test_negated_option_still_refused_for_partial_matches():
    """'willing' appears inside 'Not willing to relocate' but must not tick it."""
    import goat.agent.tools as t
    lab = answers.norm("Not willing to relocate")
    parts = [answers.norm(p) for p in "Yes; willing".split(";")]
    assert not any(lab.startswith(x) for x in parts if x)
    assert t.NEGATED.match("Not willing to relocate")


# --------------------------------------------------------------------------- sign-in pages never pass the gate
SIGNIN_PAGE = """<!doctype html><html><body><form>
<label for="em">Email Address*</label><input id="em" type="email" value="applicant@example.com" required>
<label for="pw">Password*</label><input id="pw" type="password" required>
<button type="submit">Sign In</button></form></body></html>"""


@pytest.mark.asyncio
async def test_submit_and_gate_refuse_sign_in_pages(tmp_path):
    from goat.browser import snapshot
    from goat.browser.executor import Executor, HostPolicy
    from goat.gate import run_gate
    db.reset_for_tests(tmp_path / "t.db")
    page = tmp_path / "signin.html"
    page.write_text(SIGNIN_PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    job = _job()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        ctx = RunContext(job={**job, "resume_file": None}, ex=ex, budget=Budget(job["id"], "workday"), classifier=FakeClassifier())
        d = ToolDispatcher(ctx)
        out, _ = await d.t_submit("Sign In")
        assert "navigation" in out["error"]
        out2, _ = await d.t_submit(None)
        assert "password field" in out2["error"]
        snap = await snapshot.take(ex)
        g = run_gate(job=job, snapshot=snap, page_text=await ex.page_text(5000), expected_resume=None, free_text_answers=[], portal="workday")
        assert g.result == "blocked" and "sign-in" in g.reasons[0]
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_satisfied_eligibility_parks_bounce(store):
    class Chooser(StubEx):
        async def page_text(self, n=30000):
            return "Start Your Application Autofill with Resume Apply Manually Use My Last Application"

    job = _job()
    d, ctx = _dispatcher(job)
    ctx.ex = Chooser()
    for reason in ("eligibility: The requirements are satisfied; page remains on the application chooser",
                   "eligibility: requires 40 hours per week for 10 weeks beginning May 24, 2027; earliest start is May 18, 2027"):
        ctx.park_attempts = 0
        out, _ = await d.t_park(reason)
        assert "not parked" in out["error"]


WORKDAY_SIGNIN = """<!doctype html><html><body style="margin:0">
<label for="em">Email Address*</label><input id="em" type="email" value="applicant@example.com"><br>
<label for="pw">Password*</label><input id="pw" type="password"><br>
<div style="position:relative;width:200px;height:40px">
  <button id="si" style="position:absolute;left:0;top:0;width:200px;height:40px">Sign In</button>
  <button id="ov" aria-label="Submit" style="position:absolute;left:0;top:0;width:200px;height:40px;opacity:0"
          onclick="window.clicked='overlay'">Submit</button>
</div></body></html>"""


@pytest.mark.asyncio
async def test_workday_sign_in_overlay_is_clickable(tmp_path):
    """Workday lays a transparent 'Submit' button over 'Sign In'. On a page with a password box that is
    the account form, so the click guard lets it through; elsewhere 'Submit' stays guarded."""
    from goat.browser import snapshot
    from goat.browser.executor import ClickRefused, Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    page = tmp_path / "wd_signin.html"
    page.write_text(WORKDAY_SIGNIN, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        assert not any(i.get("submitish") for i in snap.items)
        sign_in = next(i for i in snap.items if (i.get("label") or "") == "Sign In")
        await ex.click_ref(sign_in["ref"])                       # must not raise ClickRefused
        assert await ex.page.evaluate("() => window.clicked") == "overlay"
        # Without the password box the same 'Submit' button is a guarded submit control.
        await ex.page.evaluate("() => { document.getElementById('pw').remove(); document.getElementById('em').value='a'; }")
        await ex.page.evaluate("() => { for (let k=0;k<3;k++){ const i=document.createElement('input'); i.value='x'; document.body.appendChild(i);} }")
        with pytest.raises(ClickRefused):
            await ex.click_ref(sign_in["ref"])
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_stalled_portal_parks_as_portal_error_after_one_retry(store):
    from goat.budget import Parked

    class Spinner(StubEx):
        async def page_text(self, n=30000):
            return "Loading..."

    job = _job()
    d, ctx = _dispatcher(job)
    ctx.ex = Spinner()
    out, _ = await d.t_park("portal navigation failed to load after Apply Manually")
    assert "reload once" in out["error"]
    with pytest.raises(Parked) as e:
        await d.t_park("automation_prohibited: portal navigation failed to load after Apply Manually")
    assert e.value.reason.startswith("portal_error:")
    ctx.park_attempts = 4
    with pytest.raises(Parked) as e2:
        await d.t_park("automation_prohibited: something odd happened")
    assert e2.value.reason.startswith("stuck:")


WORKDAY_MONTH_YEAR = """<!doctype html><html><body style="margin:20px">
<div data-automation-id="formField-startDate"><label>From*</label>
<div data-automation-id="dateInputWrapper" role="group" style="position:relative;display:inline-block">
  <div data-automation-id="dateSectionMonth-display" style="position:relative;z-index:1;display:inline-block;width:17px;height:24px;background:#eee">MM</div> /
  <input id="m" data-goat-ref="e901" data-automation-id="dateSectionMonth-input" role="spinbutton" aria-label="Month"
         style="position:absolute;left:4px;top:11px;width:1px;height:1px;border:0;padding:0;z-index:0">
  <div data-automation-id="dateSectionYear-display" style="position:relative;z-index:1;display:inline-block;width:34px;height:24px;background:#eee">YYYY</div>
  <input id="y" data-automation-id="dateSectionYear-input" role="spinbutton" aria-label="Year"
         style="position:absolute;left:40px;top:11px;width:1px;height:1px;border:0;padding:0;z-index:0">
</div></div></body></html>"""


@pytest.mark.asyncio
async def test_set_date_focuses_zero_size_workday_sections(tmp_path):
    from goat.browser import fill
    from goat.browser.executor import Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    page = tmp_path / "wd_my.html"
    page.write_text(WORKDAY_MONTH_YEAR, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        r = await fill.set_date(ex, "e901", "02/2026")
        vals = await ex.page.evaluate("() => [document.getElementById('m').value, document.getElementById('y').value]")
        assert vals == ["02", "2026"], (vals, r.error)
        assert r.ok, r.error
    finally:
        await ex.close(close_browser=True)


WORKDAY_BUTTON_LISTBOX = """<!doctype html><html><body>
<div data-automation-id="formField-source"><label for="src">How Did You Hear About Us?*</label>
<button id="src" data-goat-ref="e950" aria-haspopup="listbox" type="button">Select One</button></div>
<script>
  const btn = document.getElementById('src');
  btn.addEventListener('click', () => {
    if (document.getElementById('lb')) return;
    const ul = document.createElement('ul'); ul.id = 'lb'; ul.setAttribute('role', 'listbox');
    for (const t of ['Select One', 'Facebook', 'Handshake', 'Indeed', 'LinkedIn', 'Other']) {
      const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = t;
      li.style.cssText = 'display:block;height:24px';
      li.addEventListener('click', () => { btn.textContent = t; ul.remove(); });
      ul.appendChild(li);
    }
    document.body.appendChild(ul);
  });
  // Like Workday: typing while the list is open closes it.
  document.addEventListener('keydown', (e) => { if (e.key.length === 1) { const l = document.getElementById('lb'); if (l) l.remove(); } });
</script></body></html>"""


@pytest.mark.asyncio
async def test_choose_picks_from_open_list_before_typing(tmp_path):
    from goat.browser import fill
    from goat.browser.executor import Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    page = tmp_path / "wd_lb.html"
    page.write_text(WORKDAY_BUTTON_LISTBOX, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        r = await fill.choose(ex, "e950", ["LinkedIn", "Job Board", "Agency / Search Firm", "Other"])
        assert r.ok, r.error
        assert await ex.page.inner_text("#src") == "LinkedIn"
    finally:
        await ex.close(close_browser=True)


def test_host_policy_ignores_default_ports():
    from goat.browser.executor import HostPolicy
    pol = HostPolicy(["https://hctz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/2506642"])
    assert pol.allowed("https://hctz.fa.us2.oraclecloud.com:443/hcmUI/CandidateExperience/en/sites/CX_1001/job/2506642/apply")
    assert pol.allowed("https://other.fa.us2.oraclecloud.com:443/x")          # known ATS suffix
    assert not pol.allowed("https://evil.example.com:443/x")


WORKDAY_MULTISELECT = """<!doctype html><html><body>
<div data-automation-id="formField-source"><label for="srch">How Did You Hear About Us?*</label>
<div data-automation-id="multiSelectContainer">
  <div data-automation-id="multiselectInputContainer"><input id="srch" placeholder="Search" aria-required="true"></div>
  <ul data-automation-id="selectedItemList" role="listbox"><li data-automation-id="menuItem">
    <div data-automation-id="selectedItem" aria-label="Indeed, press delete to clear value.">Indeed</div></li></ul>
</div></div></body></html>"""


@pytest.mark.asyncio
async def test_workday_multiselect_with_a_selection_is_not_empty(tmp_path):
    from goat.browser import snapshot
    from goat.browser.executor import Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    page = tmp_path / "wd_ms.html"
    page.write_text(WORKDAY_MULTISELECT, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        srch = next(i for i in snap.items if "Hear" in (i.get("label") or ""))
        assert srch.get("value") == "Indeed"
        assert not snap.errors.get("empty_required"), snap.errors
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_eligibility_local_is_satisfied_but_own_car_is_not(store):
    from goat.budget import Parked
    job = _job()
    d, ctx = _dispatcher(job)

    class Page(StubEx):
        async def page_text(self, n=30000):
            return "Apply"
    ctx.ex = Page()
    out, _ = await d.t_park("eligibility: Posting requires candidates to be local to Milwaukee, WI during the internship")
    assert "not parked" in out["error"]
    ctx.park_attempts = 0
    # Midland States Bank (Workday, 2026-10-24, $18 an hour): held for living in Sugar Land
    ctx.posting_text = "Intern - Security Analyst St. Louis, MO Rate of Pay: $18 an hour"
    out, _ = await d.t_park("eligibility: The posting requires candidates to reside locally in the St. Louis, MO area and report "
                            "to the St. Louis office; Surya lives in Sugar Land, Texas.")
    assert "not parked" in out["error"], out
    ctx.park_attempts = 0
    # Midland States Bank IT (2026-10-24): 'residing locally'
    out, _ = await d.t_park("eligibility: posting requires residing locally in Effingham, IL; Surya lives in Texas and the role is "
                            "local-only.")
    assert "not parked" in out["error"], out
    ctx.park_attempts = 0
    with pytest.raises(Parked):
        await d.t_park("eligibility: requires candidates in the St. Louis area who have their own transportation")


def test_best_option_accepts_parenthetical_qualifier_only():
    from goat.browser.fill import best_option
    O = lambda *t: [{"text": x} for x in t]
    deg = O("Associate of Science (A.S.)", "Bachelor of Science (B.S.)", "Bachelor of Science in Engineering (B.S.E.)")
    assert best_option(deg, ["Bachelor of Science"])["text"] == "Bachelor of Science (B.S.)"
    campus = O("Texas A&M University - College Station", "Texas A&M University - Commerce")
    assert best_option(campus, ["Texas A&M University"]) is None


WORKDAY_LONG_LISTBOX = """<!doctype html><html><body>
<div data-automation-id="formField-state"><label for="st">State*</label>
<button id="st" data-goat-ref="e960" aria-haspopup="listbox" type="button">Select One</button></div>
<div style="height:40px"></div><p id="below">below the list</p>
<script>
  const states = ['Alabama','Alaska','Arizona','Arkansas','California','Colorado','Connecticut','Delaware','Florida','Georgia',
    'Hawaii','Idaho','Illinois','Indiana','Iowa','Kansas','Kentucky','Louisiana','Maine','Maryland','Massachusetts','Michigan',
    'Minnesota','Mississippi','Missouri','Montana','Nebraska','Nevada','New Hampshire','New Jersey','New Mexico','New York',
    'North Carolina','North Dakota','Ohio','Oklahoma','Oregon','Pennsylvania','Rhode Island','South Carolina','South Dakota',
    'Tennessee','Texas','Utah','Vermont'];
  const btn = document.getElementById('st');
  btn.addEventListener('click', () => {
    if (document.getElementById('lb')) return;
    const ul = document.createElement('ul'); ul.id = 'lb'; ul.setAttribute('role', 'listbox');
    ul.style.cssText = 'position:absolute;top:60px;left:10px;height:120px;width:220px;overflow-y:auto;margin:0;padding:0;background:#fff;border:1px solid #888';
    for (const t of states) {
      const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = t;
      li.style.cssText = 'display:block;height:24px;list-style:none';
      li.addEventListener('click', () => { btn.textContent = t; ul.remove(); });
      ul.appendChild(li);
    }
    document.body.appendChild(ul);
  });
</script></body></html>"""


@pytest.mark.asyncio
async def test_choose_scrolls_a_long_list_to_the_option(tmp_path):
    from goat.browser import fill
    from goat.browser.executor import Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    page = tmp_path / "wd_long.html"
    page.write_text(WORKDAY_LONG_LISTBOX, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        r = await fill.choose(ex, "e960", ["Texas", "TX"])
        assert r.ok, r.error
        assert await ex.page.inner_text("#st") == "Texas"
    finally:
        await ex.close(close_browser=True)


# --------------------------------------------------------------------------- bundled consents, social sign-in, slow pages
def test_bundled_processing_consent_is_ticked_but_pure_marketing_is_not():
    from goat.adapters import consent_key
    assert consent_key("I agree to creating this account to apply for positions with T-Mobile") == "consent.privacy_notice"
    assert consent_key("I consent to the processing of my personal information and to receive information about future opportunities") == "consent.privacy_notice"
    assert consent_key("Send me job alerts and marketing emails") is None
    # his never-join rule: the key whose answer is No, so the box is never ticked (LLNL, 2026-10-21)
    assert consent_key("Join our talent community") == "policy.may_join_talent_community"


@pytest.mark.asyncio
async def test_social_sign_in_click_is_refused(store):
    class Snap:
        def find(self, ref):
            return {"ref": ref, "tag": "button", "label": "Sign in with Google"}
    job = _job()
    d, ctx = _dispatcher(job)
    ctx.last_snapshot = Snap()
    out, _ = await d.t_click("e3", "Sign in with Google")
    assert "Sign in with email" in out["error"]


LATE_PAGE = """<!doctype html><html><body><button>Back to Job Posting</button>
<script>setTimeout(() => { document.body.insertAdjacentHTML('beforeend',
  '<form><label for="em">Email Address*</label><input id="em" type="email" required>' +
  '<label><input type="checkbox" id="acct"> I agree to creating this account to apply for positions with Acme</label></form>'); }, 3000);</script>
</body></html>"""


@pytest.mark.asyncio
async def test_snapshot_waits_for_late_workday_form_and_ticks_its_consent(tmp_path):
    from goat.browser.executor import Executor, HostPolicy
    from goat.config import SETTINGS
    db.reset_for_tests(tmp_path / "t.db")
    answers.seed_from_config(SETTINGS.config_dir)
    folder = tmp_path / "apply"
    folder.mkdir()
    page = folder / "late.html"
    page.write_text(LATE_PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    job = _job()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        ctx = RunContext(job={**job, "resume_file": None}, ex=ex, budget=Budget(job["id"], "workday"), classifier=FakeClassifier())
        out, _ = await ToolDispatcher(ctx).t_snapshot()
        assert "Email Address" in out["outline"]
        assert await ex.page.evaluate("() => document.getElementById('acct').checked") is True
        srcs = [r["source"] for r in db.connect().execute("SELECT source FROM decisions WHERE job_id=?", (job["id"],))]
        assert "standing:consent.privacy_notice" in srcs
    finally:
        await ex.close(close_browser=True)


async def test_a_single_choice_picker_is_taken_not_asked(store):
    job = _job()
    ctx = RunContext(job={**job, "resume_file": None}, ex=StubEx(), budget=Budget(job["id"], "greenhouse"), classifier=None)
    out, _ = await ToolDispatcher(ctx).t_ask_user([{"question": "Please enter your relevant employment above using the + Add link", "options": ["Thank you"],
                                                    "field_type": "select", "ref": "e9"}])
    assert out["asked"] == [] and out["auto_answered"][0]["answer"] == "Thank you"


def test_decline_never_maps_to_no():
    from goat.browser.fill import best_option
    opts = [{"text": "Yes"}, {"text": "No"}, {"text": "I decline to self-identify"}]
    assert best_option(opts, ["decline", "Decline to self-identify", "I decline to self-identify"])["text"] == "I decline to self-identify"
    assert best_option([{"text": "Yes"}, {"text": "No"}], ["decline", "Decline to self-identify"]) is None


async def test_self_id_pickers_ignore_model_supplied_options(store, monkeypatch):
    from goat.browser import fill as fillmod
    job = _job()
    from goat import answers as answers_mod
    answers_mod.save_answer("self_id.race_ethnicity", "decline; Decline to self-identify; I do not wish to answer", answer_type="choice_chain", source="seed")
    ctx = RunContext(job={**job, "resume_file": None}, ex=StubEx(), budget=Budget(job["id"], "greenhouse"), classifier=None)
    seen = {}

    async def fake_choose(ex, ref, wants, **kw):
        seen["wants"] = list(wants)
        from goat.browser.fill import FieldResult
        return FieldResult(ref, True, wants[0])
    monkeypatch.setattr(fillmod, "choose", fake_choose)
    await ToolDispatcher(ctx).t_choose("e5", "self_id.race_ethnicity", ["Asian (United States of America)"], "Race/Ethnicity", None)
    assert "Asian (United States of America)" not in seen["wants"] and seen["wants"][0].lower().startswith("decline")


REVIEW_PAGE = """<html><body><h1>Review</h1>
<p>completed step 2 of 6 My Experience</p>
<h2>My Information</h2><div>Legal Name</div><div>Surya Thirukonda</div>
<h2>My Experience</h2><div>Work Experience</div><div>No Response</div>
<div>Education 1</div><div>Field of Study</div><div>Computer Science</div>
<button>Back</button><button>Submit</button></body></html>"""


async def test_workday_review_with_no_work_experience_is_blocked(tmp_path):
    from goat.browser import snapshot
    from goat.browser.executor import Executor, HostPolicy
    from goat.gate import run_gate
    db.reset_for_tests(tmp_path / "t.db")
    page = tmp_path / "review.html"
    page.write_text(REVIEW_PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    job = _job()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        g = run_gate(job=job, snapshot=snap, page_text=await ex.page_text(5000), expected_resume=None, free_text_answers=[], portal="workday")
        assert g.result == "blocked" and "Work Experience: No Response" in g.reasons[0] and "go_to_step" in g.reasons[0]
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_work_entry_field_never_takes_a_resume_literal(store):
    """T-Mobile (2026-10-19): the model typed 'Aggie Data Science Club' with source 'resume' over the stored company
    'Southwest Airlines' of work entry 5, and Workday then kept only that entry."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher(_job())
    s = Snapshot(url="u", title="t", headings=["My Experience", "Work Experience"], items=[
        {"ref": "e87", "tag": "input", "type": "text", "label": "Company*", "value": "Southwest Airlines"},
        {"ref": "e88", "tag": "input", "type": "text", "label": "Job Title*", "value": "Data Analyst"}], groups=[], errors={}, challenge={})
    for i in s.items:
        s.refs[i["ref"]] = i
    ctx.last_snapshot = s
    out, _ = await d.t_fill_batch([{"ref": "e87", "value": "Aggie Data Science Club", "source": "resume", "label": "Company"},
                                   {"ref": "e88", "value": "Data Analyst at the club", "source": "resume", "label": "Title"}])
    res = {r["ref"]: r for r in out["results"]}
    assert res["e87"]["status"] == "failed" and "experience.work.N.company" in res["e87"]["error"]
    assert res["e88"]["status"] == "failed"          # the control's own label says Job Title



def test_structured_answers_are_never_wordings_to_pick():
    from goat.agent.tools import _plain_wordings
    assert _plain_wordings("{'name': 'AWS Certified Cloud Practitioner', 'issuer': 'Amazon Web Services'}") == []
    assert _plain_wordings("AWS Certified Cloud Practitioner; Cloud Practitioner; Cloud Practitioner") == \
        ["AWS Certified Cloud Practitioner", "Cloud Practitioner"]
    assert _plain_wordings(None) == []



def test_a_picker_rule_is_never_text_for_a_box():
    """Ryan (2026-10-19): 'please list up to three office locations' got the rule text of bank.office-preference and
    the 'All offered locations' head of availability.location_ranking."""
    from goat.agent.tools import RULE_TEXT, concrete_entries, text_from_chain
    assert RULE_TEXT.search("Select every location offered. If only one can be chosen, pick San Francisco.")
    assert concrete_entries("Select every location offered (multi-select); if only one can be chosen, San Francisco") == []
    chain = "All offered locations; Dallas; Houston; Austin; Dallas, TX; Houston, TX; Austin, TX; New York"
    assert concrete_entries(chain)[:3] == ["Dallas", "Houston", "Austin"]
    label = "If you are open to locations outside of the one you applied, please list up to three"
    assert text_from_chain(concrete_entries(chain), label) == "Dallas, Houston, Austin"
    assert text_from_chain(concrete_entries(chain), "Preferred office") == "Dallas"
    assert text_from_chain(["Sugar Land, Texas, United States", "Sugar Land, TX"], "City") == "Sugar Land, Texas, United States"
    assert not RULE_TEXT.search("Houston, TX") and not RULE_TEXT.search("Within 30 days")



def test_a_phone_extension_box_never_gets_the_phone_number():
    """Ryan (2026-10-19): Workday's 'Phone Extension' took the whole number through the 'phone' label rule."""
    from goat.adapters import GENERIC_FIELD_MAP
    rule = next(r for r in GENERIC_FIELD_MAP if r[1] == "identity.phone")
    assert rule[0].search("Phone Number*") and rule[0].search("Mobile phone") and rule[0].search("Telephone")
    assert not rule[0].search("Phone Extension") and not rule[0].search("Phone Ext.")



def test_a_notice_acknowledgement_is_answered_by_his_standing_consent():
    """Penn State (2026-10-19): 'Do you understand the information about security-clearance situations that may cause
    delays?' with 'Yes, I understand the above information...' / 'No, I do not understand...' is an acknowledgement."""
    from goat.agent.tools import acknowledgement_key
    from goat.browser.fill import best_option
    opts = ["Select One", "Yes, I understand the above information regarding security clearances.",
            "No, I do not understand. I have questions about the above information, and I can contact the ARL Security Office."]
    key = acknowledgement_key("Do you understand the information about security-clearance situations that may cause delays?", opts)
    assert key and key.startswith(("consent.", "compliance."))
    assert acknowledgement_key("Security clearance delay situations", opts)                    # the model's paraphrase too
    assert best_option([{"text": o} for o in opts], ["Yes"])["text"].startswith("Yes, I understand")
    assert acknowledgement_key("Have you worked for Penn State before?", ["Yes", "No"]) is None
    assert acknowledgement_key("Would you like to join our talent community?", ["Yes, I agree to join", "No"]) is None
    assert acknowledgement_key("Are you a US citizen?", ["Yes, I am", "No"]) is None



def test_a_box_for_the_postings_locations_gets_the_posting_locations():
    """Northrop Grumman (2026-10-19): 'Which available location(s) on the posting are you open to relocating to?
    Please only select locations that are listed on the posting' got 'Anywhere in the United States'."""
    from goat.agent.tools import LOCATION_KEY, POSTING_LOCATIONS, posting_locations
    from goat.agent.prompts import job_brief
    lab = ("Which available location(s) on the posting are you open to relocating to? Please only select locations that are "
           "listed on the posting. Type city and states below (e.g. Palmdale, CA).")
    assert POSTING_LOCATIONS.search(lab)
    assert POSTING_LOCATIONS.search("Which of the locations listed in the job posting would you accept?")
    assert not POSTING_LOCATIONS.search("Are you willing to relocate?")
    assert not POSTING_LOCATIONS.search("Preferred work location")
    assert not POSTING_LOCATIONS.search("If you are open to locations outside of the one you applied, please list up to three")
    assert posting_locations({"locations": '["Aurora, CO"]'}) == ["Aurora, CO"]
    assert posting_locations({"locations": '["Remote", "Palmdale, CA", "Redondo Beach, CA"]'}) == ["Palmdale, CA", "Redondo Beach, CA"]
    assert posting_locations({}) == [] and posting_locations({"locations": "[]"}) == []
    assert LOCATION_KEY.search("availability.relocation_scope") and LOCATION_KEY.search("availability.office_preference")
    assert "posting locations: Aurora, CO" in job_brief({"company": "Northrop Grumman", "locations": '["Aurora, CO"]'}, [])


@pytest.mark.asyncio
async def test_fill_batch_types_the_posting_locations(store, monkeypatch):
    from goat.browser.snapshot import Snapshot
    lab = ("Which available location(s) on the posting are you open to relocating to? Please only select locations that are "
           "listed on the posting. Type city and states below (e.g. Palmdale, CA).")
    d, ctx = _dispatcher({"id": 216, "company": "Northrop Grumman", "ats": "workday", "locations": '["Aurora, CO"]'})
    snap = Snapshot(url="u", title="t", headings=[], items=[{"ref": "e59", "tag": "textarea", "label": lab, "value": ""}],
                    groups=[], errors={}, challenge={})
    for i in snap.items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    typed = {}

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        out = []
        for p in plan:
            typed[p["ref"]] = p["value"]
            out.append(FieldResult(p["ref"], True, p["value"], ""))
        return out
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("availability.relocation_scope", "Anywhere in the United States", answer_type="text", source="questionnaire")
    await d.t_fill_batch([{"ref": "e59", "answer_key": "availability.relocation_scope", "value": None, "source": None, "label": lab}])
    assert typed.get("e59") == "Aurora, CO", typed



@pytest.mark.asyncio
async def test_a_park_past_its_budget_on_a_passing_review_page_is_bounced(store, monkeypatch):
    """Northrop Grumman (2026-10-19): four parks went to a cleared portal error, then the run parked from Review over an
    earlier attempt's certification problem; the gate would have passed."""
    from types import SimpleNamespace
    from goat.agent import tools as toolsmod
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher({"id": 216, "company": "Northrop Grumman", "ats": "workday"})
    snap = Snapshot(url="u", title="t", headings=["Review"], items=[{"ref": "e3", "tag": "button", "label": "Submit", "submitish": True}],
                    groups=[], errors={}, challenge={})
    for i in snap.items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    ctx.park_attempts = 4

    async def page_text(n=30000):
        return "Review My Information My Experience Submit"

    async def take(ex):
        return snap
    ctx.ex.page_text = page_text
    monkeypatch.setattr(toolsmod.snapmod, "take", take)
    monkeypatch.setattr(toolsmod, "run_gate", lambda **k: SimpleNamespace(passed=True, result="pass_shadow", reasons=[], as_dict=lambda: {}))
    out, _ = await d.t_park("no_standing_answer: required Certifications field has no accurate option (prior attempt)")
    assert "PASSES" in out["error"]
    out, _ = await d.t_park("no_standing_answer: required Certifications field has no accurate option (prior attempt)")
    assert "PASSES" in out["error"]
    with pytest.raises(toolsmod.Parked):          # twice past the budget, then the park stands
        await d.t_park("no_standing_answer: required Certifications field has no accurate option (prior attempt)")


@pytest.mark.asyncio
async def test_a_ranked_chain_presses_the_first_offered_toggle(store, monkeypatch):
    """Vertiv (2026-10-19): Oracle's 'How did you come to know about this opportunity?' pills read 'Employee Referral',
    'Career Site', 'LinkedIn/Job Portal', 'Recruitment Agency', 'Other'; the chain 'LinkedIn; Job Board; ...' was refused."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher({"id": 24, "company": "Vertiv", "ats": "oracle"})
    g = "How did you come to know about this opportunity?"
    labels = ["Employee Referral", "Career Site", "LinkedIn/Job Portal", "Recruitment Agency", "Other"]
    items = [{"ref": f"e{20 + n}", "tag": "toggle", "label": lab, "group": g, "pressed": False} for n, lab in enumerate(labels)]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    pressed = []

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        pressed.extend(p["ref"] for p in plan)
        return [FieldResult(p["ref"], True, "pressed", "") for p in plan]
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("bank.hear-about", "LinkedIn; Job Board; Agency / Search Firm; Other", answer_type="choice_chain", source="questionnaire")
    await d.t_fill_batch([{"ref": "e20", "answer_key": "bank.hear-about", "value": None, "source": None, "label": g}])
    assert pressed == ["e22"], pressed



@pytest.mark.asyncio
async def test_a_certification_is_never_picked_without_his_certification_key(store):
    """TIAA (2026-10-19): 'Cloud Guild: AWS Cloud Practitioner - TIAA' was picked with source 'resume'."""
    d, ctx = _dispatcher({"id": 34, "company": "TIAA", "ats": "workday"})
    out, _ = await d._t_choose("e121", None, ["Cloud Guild: AWS Cloud Practitioner - TIAA"], "Certification", "resume")
    assert "background.certifications_variants" in out["error"]



def test_a_junior_or_senior_standing_requirement_is_satisfied():
    """Nissan (2026-10-19): 'students entering their Junior or Senior year at the start of the upcoming academic term'
    was held because his year on forms is Sophomore."""
    from goat.agent.tools import ToolDispatcher
    r = ("eligibility: Nissan requires students entering their Junior or Senior year at the start of the upcoming academic "
         "term; Surya's standing academic classification is Sophomore.")
    assert ToolDispatcher.SATISFIED.search(r) and not ToolDispatcher.DISQUALIFIERS.search(r)


@pytest.mark.asyncio
async def test_go_to_step_allows_the_trip_the_gate_asks_for(store):
    """Aptiv (2026-10-19): the Review gate said 'call go_to_step('My Experience')' while go_to_step refused a third trip."""
    d, ctx = _dispatcher({"id": 50, "company": "Aptiv", "ats": "workday"})
    ctx.control_failures["step:my experience"] = 2
    ctx.gate_result = {"result": "blocked", "reasons": ["the Review page does not show work entry 2 (DeKalb Capital Management): "
                                                        "call go_to_step('My Experience'), then fill_sections"]}
    async def page_text(n=4000):
        return "current step 1 of 5\nMy Experience\n"
    ctx.ex.page_text = page_text
    out, _ = await d.t_go_to_step("My Experience")
    assert "already gone back" not in str(out), out
    ctx.gate_result = None
    ctx.control_failures["step:my experience"] = 2
    out, _ = await d.t_go_to_step("My Experience")
    assert "already gone back" in out["error"]



def test_a_stale_account_title_on_a_form_page_is_recognised():
    """C&S Wholesale and Itron (2026-10-19): title 'Forgot Password - Logo' over the My Information form."""
    from goat.agent.tools import FORM_STEP_HEADING, STALE_ACCOUNT_TITLE
    assert STALE_ACCOUNT_TITLE.search("Forgot Password - Logo") and STALE_ACCOUNT_TITLE.search("Create Account - Logo")
    assert not STALE_ACCOUNT_TITLE.search("IS Internship - Data Analytics & Data Science")
    assert FORM_STEP_HEADING.search("My Information") and FORM_STEP_HEADING.search("Application Questions 1 of 2")
    assert not FORM_STEP_HEADING.search("Create Account")



@pytest.mark.asyncio
async def test_a_graduation_window_naming_fall_2027_is_no_co_op_disqualifier(store):
    """Arconic (2026-10-19): 'expected graduation date in Fall 2027, Spring/Summer 2028, or Spring/Summer 2029' was held
    because 'Fall 2027' is in the co-op disqualifier list."""
    d, ctx = _dispatcher({"id": 91, "company": "Arconic", "ats": "oracle"})
    async def page_text(n=30000):
        return "Apply"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("eligibility: The posting requires an expected graduation date in Fall 2027, Spring/Summer 2028, "
                            "or Spring/Summer 2029. Surya graduates May 2028 and meets this requirement. Proceeding.")
    assert "SATISFIED" in out["error"]



def test_a_one_sided_graduation_bound():
    """Motorola (2026-10-19): 'graduation on or after December 2027' was read as a window ending in December 2027."""
    from goat.agent.tools import ToolDispatcher as T
    assert not T.grad_window_excludes("eligibility: Posting requires graduation on or after December 2027; Surya graduates May 2028.")
    assert not T.grad_window_excludes("eligibility: graduating December 2027 or later")
    assert T.grad_window_excludes("eligibility: graduation on or after December 2028")
    assert T.grad_window_excludes("eligibility: must graduate by December 2027")
    assert not T.grad_window_excludes("eligibility: must graduate by June 2028")
    assert T.grad_window_excludes("eligibility: graduation between December 2028 and May 2029")



@pytest.mark.asyncio
async def test_a_relocation_needs_you_park_is_bounced(store):
    """AutoZone (2026-10-19): 'needs_you: requires residence in Memphis; relocation assistance depends on family housing'."""
    d, ctx = _dispatcher({"id": 101, "company": "AutoZone", "ats": "oracle"})
    async def page_text(n=30000):
        return "Apply"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("needs_you: This application requires residence in Memphis during the internship; relocation "
                            "assistance eligibility is unclear (it depends on family housing availability in Memphis).")
    assert "relocates" in out["error"]



@pytest.mark.asyncio
async def test_a_no_answer_ticks_the_no_box_of_a_checkbox_group(store, monkeypatch):
    """American Century (2026-10-19): 'Grant Thornton / FORVIS / Deloitte and Touche / No' checkboxes, answer No."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher({"id": 87, "company": "American Century Investments", "ats": "workday"})
    g = "Are you currently employed by, or have you previously worked for, any of the listed firms?"
    items = [{"ref": f"e{44 + n}", "tag": "input", "type": "checkbox", "label": lab, "group": g, "checked": False}
             for n, lab in enumerate(["Grant Thornton", "FORVIS", "Deloitte and Touche", "No"])]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    sent = {}

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        out = []
        for p_ in plan:
            sent[p_["ref"]] = p_["value"]
            out.append(FieldResult(p_["ref"], True, "checked" if str(p_["value"]) == "true" else "unchecked", ""))
        return out
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("bank.prior-employee", "No", answer_type="choice", source="seed")
    await d.t_fill_batch([{"ref": "e47", "answer_key": "bank.prior-employee", "value": None, "source": None, "label": g}])
    await d.t_fill_batch([{"ref": "e44", "answer_key": "bank.prior-employee", "value": None, "source": None, "label": g}])
    assert str(sent.get("e47")) == "true", sent
    assert str(sent.get("e44")).lower() in ("no", "false"), sent



@pytest.mark.asyncio
async def test_a_bot_check_park_without_a_challenge_is_bounced(store, monkeypatch):
    """Walmart (2026-10-20): 'bot_check_question: ... encountered no robot prompt; continue application?'."""
    from goat.agent import tools as toolsmod
    d, ctx = _dispatcher({"id": 135, "company": "Walmart", "ats": "workday"})
    async def page_text(n=30000):
        return "My Information"
    ctx.ex.page_text = page_text

    async def no_challenge(ex):
        return {"challenge": False, "kinds": []}
    monkeypatch.setattr(toolsmod, "detect_challenge", no_challenge)
    out, _ = await d.t_park("bot_check_question: Workday sign-up encountered no robot prompt; continue application?")
    assert "no CAPTCHA" in out["error"]



def test_a_completed_application_page_is_recognised():
    """Hy-Vee (2026-10-20): the submit tool crashed after the click; the page was jobTasks/completed/application."""
    from goat.agent.tools import COMPLETED_TEXT, COMPLETED_URL
    assert COMPLETED_URL.search("https://hyvee.wd1.myworkdayjobs.com/en-US/HyVeeCareers/jobTasks/completed/application")
    assert COMPLETED_TEXT.search("Candidate Home Application Submitted Under Consideration")
    assert COMPLETED_TEXT.search("Thank you for applying to Acme!")
    assert not COMPLETED_URL.search("https://hyvee.wd1.myworkdayjobs.com/en-US/HyVeeCareers/job/X/apply/applyManually")
    assert not COMPLETED_TEXT.search("Review your application before you submit it")



def test_a_signed_in_form_is_recognised_from_the_page_probe():
    """Tencent (2026-10-20): after Create Account the form ('My Information') was open and the run parked credentials."""
    from goat.agent.tools import sign_in_page, signed_in_form
    assert signed_in_form({"pw": 0, "heads": ["Cloud Media Services Intern", "My Information", "Name"], "title": "Create Account"}) == "My Information"
    assert signed_in_form({"pw": 2, "heads": ["Create Account"], "title": "Create Account"}) == ""
    assert signed_in_form({"pw": 0, "heads": ["Forgot Password"], "title": "Forgot Password"}) == ""
    assert signed_in_form({}) == ""
    assert sign_in_page({"pw": 1, "heads": ["Sign In"], "title": "Careers"})
    assert not sign_in_page({"pw": 2, "heads": ["Create Account"], "title": "Create Account"})
    assert not sign_in_page({"pw": 0, "heads": ["Sign In"], "title": ""})



def test_a_skills_checkbox_question_ticks_every_option_naming_one_of_his_skills():
    """General Motors (2026-10-20): 'Do you have any experience with the following programming languages and databases?'"""
    from goat.agent.tools import skill_option_matches
    skills = ["Python", "SQL", "Java", "JavaScript", "C++", "React", "FastAPI", "Node.js", "Docker", "PostgreSQL",
              "Amazon Web Services (AWS)", "Git", "R"]
    for opt in ["Python", "C, C#, C++", "Java/JavaScript", "R", "Postgres", "HQL/SQL", "AWS", "React.js", "Node"]:
        assert skill_option_matches(opt, skills), opt
    for opt in ["Scala", "Go", "SAS", "None of the above", "SQL Server", "MATLAB", "Rust", "C#"]:
        assert not skill_option_matches(opt, skills), opt



def test_a_languages_question_goes_to_his_skills_list(store):
    """General Motors (2026-10-20): 'Do you have experience with the following languages? Select all that apply.'"""
    from goat.agent.tools import skills_question_key
    answers.save_answer("experience.skills_list", "Python; SQL; Java; JavaScript; C++; React; Docker; PostgreSQL",
                        answer_type="choice_chain", source="questionnaire")
    opts = ["Python", "C, C#, C++", "Java/JavaScript", "R", "Postgres", "MariaDB", "Oracle", "HQL/SQL", "Spark", "Scala",
            "Kotlin", "SpringBoot", "None of the above"]
    assert skills_question_key("Do you have experience with the following languages? Select all that apply.", opts) == "experience.skills_list"
    assert skills_question_key("Which of the following technologies have you used?", opts) == "experience.skills_list"
    assert skills_question_key("Do you have experience with the following languages?", ["Yes", "No"]) is None
    assert skills_question_key("Which office locations would you consider?", ["Dallas", "Austin", "Houston"]) is None


@pytest.mark.asyncio
async def test_lookup_answer_sends_a_languages_checkbox_question_to_the_skills_rule(store):
    job = _job()
    d, ctx = _dispatcher(job)
    answers.save_answer("experience.skills_list", "Python; SQL; Java; JavaScript; C++; PostgreSQL",
                        answer_type="choice_chain", source="questionnaire")
    # an older one-off answer the aliases point at must not win
    answers.save_answer("q.programming_language_and_or_data", "Java, SQL, C#, C/C++, Python, etc.", answer_type="choice",
                        source="derived", aliases=["Do you have experience with the following languages? Select all that apply."])
    opts = ["Python", "C, C#, C++", "Java/JavaScript", "Scala", "None of the above"]
    out, _ = await d.t_lookup_answer("Do you have experience with the following languages? Select all that apply.", opts)
    assert out["key"] == "experience.skills_list" and out["tick"] == ["Python", "C, C#, C++", "Java/JavaScript"]



@pytest.mark.asyncio
async def test_an_eligibility_park_without_a_requirement_is_bounced(store):
    """Motorola (2026-10-20): 'eligibility: none' was parked after its real concern was answered as satisfied."""
    job = _job()
    d, ctx = _dispatcher(job)

    async def page_text(n=0):
        return "Software Engineering Intern Apply"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("eligibility: none")
    assert "names none" in out["error"]



def test_a_do_you_understand_question_is_an_acknowledgement():
    """Cigna (2026-10-20): 'Do you understand that this is an entry-level, temporary role?' (Yes / No) went to Surya."""
    from goat.agent.tools import acknowledgement_key
    assert acknowledgement_key("Do you understand that this is an entry-level, temporary role?", ["Yes", "No"]) == "consent.privacy_notice"
    assert acknowledgement_key("Do you understand English?", ["Yes", "No"]) is None
    assert acknowledgement_key("Are you aware of any conflict of interest with this company?", ["Yes", "No"]) is None


@pytest.mark.asyncio
async def test_a_found_it_at_a_campus_event_question_is_no(store):
    """Cigna (2026-10-20): 'Did you find this opportunity through a campus event? If yes, which college?'"""
    job = _job()
    d, ctx = _dispatcher(job)
    out, _ = await d.t_lookup_answer("Did you find this opportunity through a campus event? If yes, which college or university?", [])
    assert out["answer"] == "No" and out["via"] == "source_rule"
    out, _ = await d.t_lookup_answer("Did you find this opportunity through a conference? If yes, which conference?", [])
    assert out["answer"] == "No"
    from goat.agent.tools import OTHER_SOURCE_QUESTION
    assert not OTHER_SOURCE_QUESTION.search("How did you hear about this opportunity?")



@pytest.mark.asyncio
async def test_an_already_applied_park_stands_when_the_page_says_so(store):
    """Thrivent (2026-10-20): "You've already applied for this job" was bounced as a form page."""
    from goat.budget import Parked
    job = _job()
    d, ctx = _dispatcher(job)

    async def page_text(n=0):
        return "Careers You've already applied for this job. View My Applications"
    ctx.ex.page_text = page_text
    with pytest.raises(Parked) as e:
        await d.t_park("already_applied: Thrivent says an application was submitted")
    assert e.value.reason.startswith("already_applied")



@pytest.mark.asyncio
async def test_a_salary_range_question_takes_his_stored_pay(store):
    job = _job()
    d, ctx = _dispatcher(job)
    answers.save_answer("compensation.desired_annual", "41600", answer_type="text", source="questionnaire")
    out, _ = await d.t_lookup_answer("What is your desired salary range?", ["Up to $40,000", "$40,000-$60,000", "$60,000-$80,000"])
    assert out["key"] == "compensation.desired_annual" and out["pick"] == "$40,000-$60,000"



@pytest.mark.asyncio
async def test_a_skill_the_posting_wants_is_not_an_eligibility_hold(store):
    """Motorola (2026-10-20): 'eligibility: posting requires Android Studio and Android SDK knowledge'."""
    job = _job()
    d, ctx = _dispatcher(job)

    async def page_text(n=0):
        return "Android Platform Software Engineer Intern Apply"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("eligibility: posting requires Android Studio and Android SDK knowledge, which is not in Surya's stored profile")
    assert "not an eligibility rule" in out["error"]
    from goat.agent.tools import ToolDispatcher
    assert ToolDispatcher.HARD_REQUIREMENT.search("eligibility: requires an active Secret clearance")
    assert ToolDispatcher.HARD_REQUIREMENT.search("eligibility: requires enrollment in a Master's program")



@pytest.mark.asyncio
async def test_a_basis_of_authorization_change_question_is_no_for_a_citizen(store):
    """Ecolab (2026-10-20): the run answered Yes from his citizenship key."""
    job = _job()
    d, ctx = _dispatcher(job)
    q = ("Regardless of whether you require immigration sponsorship, do you have any reason to believe that your basis for "
         "employment authorization in the U.S. will change within the next 1-3 years? Do not answer this question if you are a U.S citizen")
    out, _ = await d.t_lookup_answer(q, ["Yes", "No"])
    assert out["answer"] == "No" and out["key"] == "work_authorization.basis_change"



def test_a_label_without_its_opening_words_finds_his_answer(store):
    """IGS Energy (2026-10-20): 'Salary requirements negotiable?' missed his web answer to 'Are your salary requirements
    negotiable?'."""
    answers.save_answer("q.are_your_salary_requirements_negotiable", "Yes", answer_type="choice", source="web",
                        aliases=["Are your salary requirements negotiable?"])
    a = answers.lookup("Salary requirements negotiable?")
    assert a is not None and a.key == "q.are_your_salary_requirements_negotiable" and a.answer == "Yes"
    assert answers.lookup("Is there salary requirements negotiable?").key == "q.are_your_salary_requirements_negotiable"
    answers.save_answer("q.do_you_require_sponsorship", "No", answer_type="choice", source="web",
                        aliases=["Do you require sponsorship?"])
    hit = answers.lookup("Will you require sponsorship?")          # two words left: never matched this way
    assert hit is None or hit.key != "q.do_you_require_sponsorship" or hit.via != "alias_core"


def _salary_dispatcher(monkeypatch, label, tag="input", digits_only=False):
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher({"id": 195, "company": "IGS Energy", "ats": "workday"})
    snap = Snapshot(url="u", title="t", headings=[], items=[{"ref": "e44", "tag": tag, "label": label, "value": ""}],
                    groups=[], errors={}, challenge={})
    for i in snap.items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    typed = []

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        out = []
        for p in plan:
            typed.append(p["value"])
            rb = "".join(ch for ch in str(p["value"]) if ch.isdigit()) if digits_only else p["value"]
            out.append(FieldResult(p["ref"], rb == p["value"], rb, ""))
        return out
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("compensation.desired_annual", "41600", answer_type="text", source="questionnaire")
    answers.save_answer("bank.desired-hourly-rate", "20", answer_type="text", source="seed:answer_bank.json")
    answers.save_answer("bank.salary-desired", "$20/hr", answer_type="text", source="seed:answer_bank.json")
    return d, typed


@pytest.mark.asyncio
async def test_a_salary_box_takes_his_yearly_figure_not_a_bare_20(store, monkeypatch):
    """IGS Energy (2026-10-20): 20 in 'What is your desired base salary?' reads as $20 a year."""
    d, typed = _salary_dispatcher(monkeypatch, "What is your desired base salary?")
    out, _ = await d.t_fill_batch([{"ref": "e44", "answer_key": "bank.desired-hourly-rate", "value": None, "source": None,
                                    "label": "Desired base salary"}])
    assert typed == ["41600"] and out["results"][0]["status"] == "set"
    from goat import records as rec
    dec = [r for r in rec.decisions(195) if r["field_label"] == "Desired base salary"]
    assert dec and dec[-1]["source"] == "standing:compensation.desired_annual"


@pytest.mark.asyncio
async def test_his_hourly_text_in_a_digits_only_salary_box_is_retyped_as_yearly(store, monkeypatch):
    d, typed = _salary_dispatcher(monkeypatch, "What is your desired base salary?", digits_only=True)
    out, _ = await d.t_fill_batch([{"ref": "e44", "answer_key": "bank.salary-desired", "value": None, "source": None,
                                    "label": "Desired base salary"}])
    assert typed == ["$20/hr", "41600"], typed
    assert out["results"][0]["status"] == "set" and out["results"][0]["readback"] == "41600"


@pytest.mark.asyncio
async def test_an_hourly_rate_box_and_a_salary_text_box_keep_his_hourly_figure(store, monkeypatch):
    d, typed = _salary_dispatcher(monkeypatch, "Desired hourly rate")
    await d.t_fill_batch([{"ref": "e44", "answer_key": "bank.desired-hourly-rate", "value": None, "source": None, "label": "Desired hourly rate"}])
    assert typed == ["20"]
    d, typed = _salary_dispatcher(monkeypatch, "What is your desired salary?")
    await d.t_fill_batch([{"ref": "e44", "answer_key": "bank.salary-desired", "value": None, "source": None, "label": "Desired salary"}])
    assert typed == ["$20/hr"]                                     # a box that keeps text keeps his wording
    d, typed = _salary_dispatcher(monkeypatch, "What are your salary expectations?", tag="textarea")
    await d.t_fill_batch([{"ref": "e44", "answer_key": "bank.desired-hourly-rate", "value": None, "source": None, "label": "Salary expectations"}])
    assert typed == ["20"]                                         # only one-line boxes change


@pytest.mark.asyncio
async def test_a_salary_box_beside_a_rate_basis_choice_keeps_his_hourly_figure(store, monkeypatch):
    """HMH (Workday, 2026-10-24): 'Desired salary' sat beside 'Is your salary expectation an hourly, monthly or annual rate?'
    (Hourly). The box got 41600 and the application asked $41,600 an hour."""
    from goat.agent import tools as toolsmod
    d, typed = _salary_dispatcher(monkeypatch, "What is your desired salary?*")
    basis = {"ref": "e148", "tag": "dropdown", "label": "Is your salary expectation an hourly, monthly or annual rate?*", "value": ""}
    d.ctx.last_snapshot.items.append(basis)
    d.ctx.last_snapshot.refs["e148"] = basis
    assert toolsmod.rate_basis_on_page(d.ctx.last_snapshot)
    await d.t_fill_batch([{"ref": "e44", "answer_key": "bank.desired-hourly-rate", "value": None, "source": None, "label": "Desired salary"}])
    assert typed == ["20"], typed
    # the salary box alone still takes his yearly figure, and a 'Desired hourly rate' box is never a basis choice
    assert not toolsmod.rate_basis_on_page(SimpleNamespace(items=[{"tag": "input", "label": "Desired hourly rate"}]))



@pytest.mark.asyncio
async def test_a_park_without_a_named_reason_never_stands(store, monkeypatch):
    """Highmark Health (2026-10-20): parks named 'run_incomplete' / 'incomplete' spent the four checked parks in 20
    seconds, and the fifth ('not completed') went through with a required dropdown still empty."""
    from types import SimpleNamespace
    from goat.agent import tools as toolsmod
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher({"id": 439, "company": "Highmark Health", "ats": "workday"})
    snap = Snapshot(url="u", title="t", headings=["Application Questions"], items=[{"ref": "e48", "tag": "button", "label": "Select One"}],
                    groups=[], errors={}, challenge={})
    for i in snap.items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    ctx.park_attempts = 4

    async def page_text(n=30000):
        return "Application Questions Select One Required Save and Continue"

    async def take(ex):
        return snap
    ctx.ex.page_text = page_text
    monkeypatch.setattr(toolsmod.snapmod, "take", take)
    blocked = SimpleNamespace(passed=False, result="blocked", reasons=["empty_required:  Select One Required (dropdown) ref=e48"],
                              as_dict=lambda: {})
    monkeypatch.setattr(toolsmod, "run_gate", lambda **k: blocked)
    for why in ("not completed", "run_incomplete", "incomplete", "ready"):
        out, _ = await d.t_park(why)
        assert "names no hold reason" in out["error"] and out["blockers"] == blocked.reasons
    assert ctx.park_attempts == 4                  # the checked parks were not used up
    with pytest.raises(toolsmod.Parked):           # a named reason past the budget still stands
        await d.t_park("portal_error: the Save and Continue button does nothing")



@pytest.mark.asyncio
async def test_a_gpa_band_radio_takes_his_gpa(store, monkeypatch):
    """Texas Instruments (2026-10-20): '3.50 - 4.00 or higher' was pre-selected, and neither '4.0' nor the range chain
    matched its label, so the gate kept blocking and the run stalled."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher({"id": 434, "company": "Texas Instruments", "ats": "oracle"})
    g = "What is your current overall Grade Point Average (GPA)?"
    labels = ["1.99 or less", "2.00 - 2.99", "3.00 - 3.49", "3.50 - 4.00 or higher"]
    items = [{"ref": f"e{93 + n}", "tag": "input", "type": "radio", "label": lab, "group": g} for n, lab in enumerate(labels)]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    typed = {}

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        out = []
        for p in plan:
            typed[p["ref"]] = p["value"]
            out.append(FieldResult(p["ref"], True, "checked", ""))
        return out
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("education.gpa", "4.0", answer_type="text", source="questionnaire")
    answers.save_answer("education.gpa_range", "4.0 or above; 4.0 and above; 4.0+; 3.7 - 4.0; 3.5 - 4.0; 3.5+", answer_type="choice_chain",
                        source="questionnaire")
    for key in ("education.gpa", "education.gpa_range"):
        typed.clear()
        out, _ = await d.t_fill_batch([{"ref": "e96", "answer_key": key, "value": None, "source": None, "label": "GPA"}])
        assert out["results"][0]["status"] == "set" and typed == {"e96": "true"}, (key, out)
    out, _ = await d.t_fill_batch([{"ref": "e93", "answer_key": "education.gpa", "value": None, "source": None, "label": "GPA"}])
    assert out["results"][0]["status"] == "failed" and "[e96]" in out["results"][0]["error"]



@pytest.mark.asyncio
async def test_a_box_that_asks_for_none_gets_none(store, monkeypatch):
    """Highmark Health (2026-10-20): 'Enter none if not applicable' got his stored 'No'."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher({"id": 439, "company": "Highmark Health", "ats": "workday"})
    lab = ("Please provide any names of relatives currently working for Highmark Health or Allegheny Health Network and its "
           "affiliates. Enter none if not applicable.")
    snap = Snapshot(url="u", title="t", headings=[], items=[{"ref": "e41", "tag": "textarea", "label": lab, "value": ""}],
                    groups=[], errors={}, challenge={})
    for i in snap.items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    typed = {}

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        out = []
        for p in plan:
            typed[p["ref"]] = p["value"]
            out.append(FieldResult(p["ref"], True, p["value"], ""))
        return out
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("bank.relatives", "No", answer_type="text", source="seed:answer_bank.json")
    await d.t_fill_batch([{"ref": "e41", "answer_key": "bank.relatives", "value": None, "source": None, "label": "Names of relatives"}])
    assert typed == {"e41": "None"}



@pytest.mark.asyncio
async def test_choose_never_falls_back_to_the_opposite_of_his_answer(store, monkeypatch):
    """Highmark Health (2026-10-20): his 'Yes' to AI screening failed its read-back and the fallback clicked 'No, Opt - Out'."""
    from goat.agent import tools as toolsmod
    from goat.browser.fill import FieldResult
    d, ctx = _dispatcher({"id": 439, "company": "Highmark Health", "ats": "workday"})
    calls = []

    async def fake_choose(ex, ref, wants, *a, **k):
        calls.append(list(wants))
        return FieldResult(ref, False, "", "the control showed an internal id")
    monkeypatch.setattr(toolsmod.fill, "choose", fake_choose)
    answers.save_answer("bank.ai-screening-consent", "Yes", answer_type="text", source="seed:answer_bank.json")
    await d.t_choose("e42", "bank.ai-screening-consent", ["Yes, Opt - In", "No, Opt - Out"], "Consent to AI screening", None)
    assert calls and all(not any(w.lower().startswith("no") for w in c) for c in calls), calls
    assert toolsmod.answer_polarity("No, Opt - Out") == "no" and toolsmod.answer_polarity("Yes, Opt - In") == "yes"
    assert toolsmod.answer_polarity("Texas A&M University") is None



@pytest.mark.asyncio
async def test_a_pay_alignment_question_is_settled_by_the_posted_range(store):
    """Amplify (2026-10-20): 'Does the posted salary or hourly pay range align with your expectations?' with $30-$35/hr."""
    from goat.agent import tools as toolsmod
    assert toolsmod.pay_meets_desired("Compensation Hourly Rate $30 – $35 per hour") is True
    assert toolsmod.pay_meets_desired("$15 - $18 per hour") is False
    assert toolsmod.pay_meets_desired("$85,000 - $110,000 a year") is True
    assert toolsmod.pay_meets_desired("$35k - $40k") is False
    assert toolsmod.pay_meets_desired("competitive pay") is None
    q = "Does the posted salary or hourly pay range align with your expectations?"
    job = _job("Amplify", "LA1")
    d, ctx = _dispatcher(job, posting_text="Overview ... Compensation Hourly Rate $30 – $35 per hour ... Apply")
    out, _ = await d.t_lookup_answer(q, ["Yes", "No"])
    assert out["answer"] == "Yes" and out["via"] == "pay_rule"
    job2 = _job("Lowpay", "LA2")
    d2, ctx2 = _dispatcher(job2, posting_text="Pay: $15 - $18 per hour")
    out2, _ = await d2.t_lookup_answer(q, ["Yes", "No"])
    assert out2["answer"] == "No"



@pytest.mark.asyncio
async def test_a_job_id_attention_check_is_read_from_the_posting(store):
    """Space Dynamics Laboratory (2026-10-20): 'Please select the correct job ID located at the top of the job advertisement.'"""
    from goat.agent import tools as toolsmod
    q = "Please select the correct job ID located at the top of the job advertisement."
    opts = ["9849", "9888", "9444", "9000"]
    page = "AI-Enabled Requirements Engineering Intern\nJob ID: 9849\nDate Posted: October 20, 2026\n... " + q + "\n9849\n9888\n9444\n9000"
    assert toolsmod.posting_id_pick(q, opts, page) == "9849"
    assert toolsmod.posting_id_pick(q, opts, q + " 9849 9888 9444 9000") is None        # the option list alone never counts
    assert toolsmod.posting_id_pick("What is your favorite number?", opts, page) is None
    job = _job("Space Dynamics Laboratory", "SDL1")
    d, ctx = _dispatcher(job, posting_text="Job ID: 9849 Date Posted: October 20, 2026")
    out, _ = await d.t_lookup_answer(q, opts)
    assert out["answer"] == "9849" and out["via"] == "posting_rule" and out["key"].startswith("q.posting_id.job")



@pytest.mark.asyncio
async def test_a_completed_college_program_question_is_no(store):
    """Electronic Arts (2026-10-20): 'Have you completed a college program in the last 18 months?' got Yes from enrollment."""
    d, ctx = _dispatcher(_job("Electronic Arts", "EA1"))
    out, _ = await d.t_lookup_answer("Have you completed a college program in the last 18 months?", ["Yes", "No"])
    assert out["answer"] == "No" and out["key"] == "education.completed_college_program"
    out, _ = await d.t_lookup_answer("When will you complete your degree program?", [])
    assert out.get("key") != "education.completed_college_program"



@pytest.mark.asyncio
async def test_a_boot_camp_degree_or_military_question_is_no(store):
    """Electronic Arts (2026-10-21): 'completed one of the following in the last 18 months: a Coding Boot Camp, a Degree
    program from a college/university or Military Service?' was put to Surya."""
    d, ctx = _dispatcher(_job("Electronic Arts", "EA2"))
    q = ("Have you completed one of the following in the last 18 months: a Coding Boot Camp, a Degree program from a "
         "college/university or Military Service?")
    out, _ = await d.t_lookup_answer(q, ["Yes", "No"])
    assert out["answer"] == "No" and out["via"] == "degree_rule"



@pytest.mark.asyncio
async def test_a_clearance_required_after_day_one_is_not_an_eligibility_hold(store):
    """RTX (2026-10-21): 'requires an active and existing DoD Secret security clearance after day one' held the job."""
    job = _job()
    d, ctx = _dispatcher(job)

    async def page_text(n=0):
        return "Software Engineering Intern (Summer 2027) Apply"
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("eligibility: Posting requires an active and existing DoD Secret security clearance after day one; "
                            "Surya has never held a clearance.")
    assert "not needed to apply" in out["error"]
    from goat.agent.tools import ToolDispatcher
    assert not ToolDispatcher.CLEARANCE_LATER.search("requires an active TS/SCI clearance to apply")



def test_apply_is_the_final_button_on_a_filled_successfactors_form(store):
    """Westinghouse (2026-10-21): the gate passed and submit refused 'Apply' as a navigation button."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher(_job())
    items = [{"ref": f"e{n}", "tag": "input", "type": "text", "label": f"Field {n}"} for n in range(10)]
    ctx.last_snapshot = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    assert d._final_apply_button("Apply")
    ctx.last_snapshot = Snapshot(url="u", title="t", headings=[], items=items[:2], groups=[], errors={}, challenge={})
    assert not d._final_apply_button("Apply")                     # a posting page with an Apply link
    assert not d._final_apply_button("Apply Now")



@pytest.mark.asyncio
async def test_a_hand_picked_race_is_refused_when_the_list_offers_decline(store, monkeypatch):
    """Impulse Space (2026-10-21): 'Asian' set with the computer tool was kept although EEO lists offer a decline option."""
    from goat.agent import tools as toolsmod
    d, ctx = _dispatcher(_job())
    answers.save_answer("self_id.race_ethnicity", "decline; Prefer not to say; Asian", answer_type="choice_chain", source="questionnaire")

    async def read_ref(ref):
        return {"found": True, "type": "text", "value": "Asian"}
    ctx.ex.read_ref = read_ref

    async def peek(ex, ref):
        return ["Asian", "White", "Prefer not to say"], None
    monkeypatch.setattr(toolsmod.fill, "peek_options", peek)
    out = await d._keep_computer_pick({"ref": "e15", "answer_key": "self_id.race_ethnicity", "label": "Ethnicity"}, None)
    assert out["status"] == "failed" and "Prefer not to say" in out["error"]

    async def peek2(ex, ref):
        return ["Asian", "White"], None
    monkeypatch.setattr(toolsmod.fill, "peek_options", peek2)
    out = await d._keep_computer_pick({"ref": "e15", "answer_key": "self_id.race_ethnicity", "label": "Ethnicity"}, None)
    assert out["status"] == "kept"



@pytest.mark.asyncio
async def test_where_he_will_work_from_is_the_postings_location(store):
    """Electronic Arts (2026-10-21): 'What state do you anticipate working from during your internship?' got Texas."""
    from goat.agent import tools as toolsmod
    text = "General Information\nLocations\n: Orlando, Florida, United States of America\nRole ID 216262"
    assert toolsmod.posting_city_state({"locations": "[]"}, text) == ("Orlando", "Florida")
    assert toolsmod.posting_city_state({"locations": '["Bloomfield, CT"]'}, "") == ("Bloomfield", "Connecticut")
    d, ctx = _dispatcher(_job("Electronic Arts", "EA3"), posting_text=text)
    out, _ = await d.t_lookup_answer("What state do you anticipate working from during your internship? *", ["Texas", "Florida"])
    assert out["answer"] == "Florida" and out["via"] == "posting_rule"
    out, _ = await d.t_lookup_answer("What city do you anticipate working from during your internship? *", [])
    assert out["answer"] == "Orlando"



def test_a_password_help_page_is_an_account_page_for_click(store):
    """Phillips 66 (2026-10-21): the Password Help 'Submit' was refused by click and the page refused by submit."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher(_job())
    items = [{"ref": "e1", "tag": "input", "type": "email", "label": "Email"}, {"ref": "e2", "tag": "button", "label": "Submit", "submitish": True}]
    ctx.last_snapshot = Snapshot(url="u", title="Career Opportunities: Password Help", headings=["Password Help"], items=items,
                                 groups=[], errors={}, challenge={})
    assert d._account_page()
    ctx.last_snapshot = Snapshot(url="u", title="Apply", headings=["Application Questions"], items=items, groups=[], errors={}, challenge={})
    assert not d._account_page()



@pytest.mark.asyncio
async def test_a_no_option_is_named_in_the_log_even_when_the_question_contains_no_letters(store, monkeypatch):
    """Koch (2026-10-21): 'no' inside 'now' / 'non-compete' kept ' / No' out of the decision label; the judge read Yes."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher(_job())
    q = "Signed non-compete, non-solicitation, confidential information or similar agreement?"
    items = [{"ref": "e13", "tag": "input", "type": "radio", "label": "Yes", "group": q},
             {"ref": "e14", "tag": "input", "type": "radio", "label": "No", "group": q}]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        return [FieldResult(p["ref"], True, "checked", "") for p in plan]
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("relationships.restrictive_covenant", "No", answer_type="choice", source="questionnaire")
    await d.t_fill_batch([{"ref": "e14", "answer_key": "relationships.restrictive_covenant", "value": None, "source": None, "label": q}])
    from goat import records as rec
    dec = [r for r in rec.decisions(ctx.job_id) if r["ref"] == "e14"]
    assert dec and dec[-1]["field_label"].endswith(" / No"), dec


def test_a_stored_description_with_escaped_line_breaks_is_typed_with_real_ones(store):
    """Aerospace (Workday) and CapTech (SmartRecruiters), 2026-10-21: the questionnaire keeps line breaks as a literal
    backslash-n, and the two characters went into the forms between bullets."""
    d, ctx = _dispatcher(_job())
    bs, nl = chr(92), chr(10)
    answers.save_answer("experience.work.1.description", "• Built one thing." + bs + "n• Shipped another.", answer_type="text",
                        source="questionnaire")
    val, tag, err = d._resolve_value("experience.work.1.description", None, None, "Description")
    assert err is None and bs + "n" not in val
    assert val.split(nl) == ["• Built one thing.", "• Shipped another."]
    answers.save_answer("motivation.why_company", "Line one." + bs + "nLine two.", answer_type="text", source="questionnaire")
    val, tag, err = d._resolve_value("motivation.why_company", None, None, "Why this company")
    assert err is None and val == "Line one." + nl + "Line two."


@pytest.mark.asyncio
async def test_the_postings_own_clearance_wording_decides_not_the_models_paraphrase(store):
    """RTX (2026-10-21): five bounces, then a sixth hold that dropped 'after day one' from its reason stood although the
    posting said the clearance is obtained after hire. Now the posting decides, and a sixth hold goes to Surya."""
    from goat.budget import Parked
    job = _job()
    d, ctx = _dispatcher(job)

    async def page_text(n=0):
        return "Software Engineering Intern (Summer 2027) Apply"
    ctx.ex.page_text = page_text
    ctx.posting_text = ("U.S. Citizen, U.S. Person, or Immigration Status Requirements: The ability to obtain and maintain a U.S. "
                        "government issued security clearance is required. Security Clearance Type: DoD Clearance: Secret. "
                        "Security Clearance Status: Active and existing security clearance required after day 1")
    reason = "eligibility: This RTX internship requires an active, existing DoD Secret clearance; Surya has never held a clearance."
    for _ in range(5):
        out, _ = await d.t_park(reason)
        assert "not needed to apply" in out["error"]
    ctx.park_attempts = 4          # the gate-checked park passes are spent (they need a live page)
    with pytest.raises(Parked) as held:
        await d.t_park(reason)
    assert str(held.value).startswith("review_needed: the posting says the clearance is obtained after hire")
    # a clearance he must already hold is a real requirement
    d2, ctx2 = _dispatcher(_job(listing="L2"))
    ctx2.ex.page_text = page_text
    ctx2.posting_text = "Security Clearance Status: Active and existing security clearance required on day 1"
    assert not d2.posting_clearance_later()



def test_eightfolds_profile_use_options_word_his_yes_and_no():
    """Qualcomm (2026-10-21): the 'ANY ROLES GLOBALLY' option is the Yes of consent.keep_profile_future_roles."""
    from goat.agent.tools import key_option_polarity
    k = "consent.keep_profile_future_roles"
    yes = ("Use my profile data for ANY ROLES GLOBALLY THAT MAY MATCH MY SKILLS, including those I haven't applied to. My profile "
           "data may also be used for recruiting initiatives (e.g., Talent Community and Events).")
    assert key_option_polarity(k, yes) == "yes"
    assert key_option_polarity(k, "Use my profile data ONLY for the role(s) I have applied to.") == "no"
    assert key_option_polarity("identity.first_name", yes) is None


@pytest.mark.asyncio
async def test_the_profile_use_yes_is_ticked_from_the_stored_yes(store, monkeypatch):
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher(_job())
    q = "How would you like us to use your profile data?"
    yes = "Use my profile data for ANY ROLES GLOBALLY THAT MAY MATCH MY SKILLS, including those I haven't applied to."
    no = "Use my profile data ONLY for the role(s) I have applied to."
    items = [{"ref": "e1", "tag": "input", "type": "radio", "label": yes, "group": q},
             {"ref": "e2", "tag": "input", "type": "radio", "label": no, "group": q}]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        return [FieldResult(p["ref"], True, "checked", "") for p in plan]
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("consent.keep_profile_future_roles", "Yes", answer_type="choice", source="questionnaire")
    out, _ = await d.t_fill_batch([{"ref": "e1", "answer_key": "consent.keep_profile_future_roles", "value": None, "source": None, "label": q}])
    assert out["results"][0]["status"] == "set", out
    out, _ = await d.t_fill_batch([{"ref": "e2", "answer_key": "consent.keep_profile_future_roles", "value": None, "source": None, "label": q}])
    assert out["results"][0]["status"] == "failed", out


@pytest.mark.asyncio
async def test_a_profile_use_choice_bundling_the_talent_community_takes_only_this_role(store, monkeypatch):
    """Qualcomm (2026-10-21): 'ANY ROLES ... Talent Community and Events' vs 'ONLY THE ROLE I AM APPLYING FOR ... I will NOT
    be contacted about ... Talent Community': he never joins a talent community, so the second option is his answer."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher(_job())
    q = "Use my profile data"
    anyr = ("Use my profile data for ANY ROLES GLOBALLY THAT MAY MATCH MY SKILLS, including those I haven't applied to. My profile "
            "data may also be used for recruiting initiatives (e.g., Talent Community and Events).")
    only = ("Use my profile data for ONLY THE ROLE I AM APPLYING FOR. I will NOT be contacted about other roles or recruiting "
            "opportunities (e.g., Talent Community and Events), even if they match my skills.")
    items = [{"ref": "e46", "tag": "input", "type": "radio", "label": anyr, "group": q},
             {"ref": "e47", "tag": "input", "type": "radio", "label": only, "group": q}]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        return [FieldResult(p["ref"], True, "checked", "") for p in plan]
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("consent.keep_profile_future_roles", "Yes", answer_type="choice", source="questionnaire")
    answers.save_answer("policy.may_join_talent_community", "No", answer_type="choice", source="seed:delegation.yaml")
    out, _ = await d.t_fill_batch([{"ref": "e46", "answer_key": "consent.keep_profile_future_roles", "value": None, "source": None, "label": q}])
    assert out["results"][0]["status"] == "failed" and "ONLY THE ROLE I AM APPLYING FOR" in out["results"][0]["error"], out
    out, _ = await d.t_fill_batch([{"ref": "e47", "answer_key": "consent.keep_profile_future_roles", "value": None, "source": None, "label": q}])
    assert out["results"][0]["status"] == "set", out


@pytest.mark.asyncio
async def test_a_page_that_did_not_load_is_looked_at_before_the_hold(store, monkeypatch):
    """Phillips 66 (2026-10-21): 'the password-reset page did not load', two seconds after the click, held the run."""
    from goat.browser.snapshot import Snapshot
    import goat.agent.tools as toolsmod
    d, ctx = _dispatcher(_job())
    ctx.park_attempts = 1
    items = [{"ref": f"e{n}", "tag": "input", "type": "text", "label": f"Field {n}"} for n in range(4)]

    async def take(ex, *a, **k):
        return Snapshot(url="https://career4.successfactors.com/career", title="Career Opportunities: Password Help", headings=[],
                        items=items, groups=[], errors={}, challenge={})
    monkeypatch.setattr(toolsmod.snapmod, "take", take)

    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr(toolsmod.asyncio, "sleep", no_sleep)
    out, _ = await d.t_park("portal_error: the password-reset page did not load")
    assert "the page has loaded" in out["error"]



@pytest.mark.asyncio
async def test_na_goes_into_a_box_that_asks_for_it(store, monkeypatch):
    """CenturyLink (2026-10-21): '(Enter N/A if not applicable.)' refused N/A as a yes/no value; the run parked."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher(_job())
    items = [{"ref": "e39", "tag": "textarea", "type": "", "label": "* If yes, please describe the circumstances under which you left "
              "this employment. (Enter 'N/A' if not applicable.)"},
             {"ref": "e40", "tag": "textarea", "type": "", "label": "Anything else we should know?"}]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        return [FieldResult(p["ref"], True, str(p["value"]), "") for p in plan]
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    out, _ = await d.t_fill_batch([{"ref": "e39", "answer_key": None, "value": "N/A", "source": "generated", "label": "circumstances"}])
    assert out["results"][0]["status"] == "set", out
    out, _ = await d.t_fill_batch([{"ref": "e40", "answer_key": None, "value": "N/A", "source": "generated", "label": "anything else"}])
    assert out["results"][0]["status"] == "failed", out


def test_undergraduate_is_not_a_graduation_window():
    """CapTech (2026-10-21): 'current undergraduate Juniors for Summer 2027' skipped the satisfied-bounce."""
    from goat.agent.tools import ToolDispatcher as T
    assert not T.grad_window_excludes("eligibility: posting requires current undergraduate Juniors for Summer 2027; Surya is a "
                                      "sophomore by his current year in school and will be a rising senior then.")
    assert T.grad_window_excludes("eligibility: posting requires graduation between Dec 2028 and Spring 2029")


@pytest.mark.asyncio
async def test_the_only_this_role_option_is_his_marketing_opt_out(store, monkeypatch):
    """Qualcomm (2026-10-21): mapped to consent.marketing_email (No), the 'ONLY THE ROLE' option was refused as not a No."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher(_job())
    q = "Use my profile data"
    anyr = "Use my profile data for ANY ROLES GLOBALLY THAT MAY MATCH MY SKILLS. Talent Community and Events."
    only = "Use my profile data for ONLY THE ROLE I AM APPLYING FOR. I will NOT be contacted about other roles."
    items = [{"ref": "e5", "tag": "input", "type": "radio", "label": anyr, "group": q},
             {"ref": "e6", "tag": "input", "type": "radio", "label": only, "group": q}]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        return [FieldResult(p["ref"], True, "checked", "") for p in plan]
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("consent.marketing_email", "No", answer_type="choice", source="seed:delegation.yaml")
    out, _ = await d.t_fill_batch([{"ref": "e5", "answer_key": "consent.marketing_email", "value": None, "source": None, "label": q}])
    assert out["results"][0]["status"] == "failed", out
    out, _ = await d.t_fill_batch([{"ref": "e6", "answer_key": "consent.marketing_email", "value": None, "source": None, "label": q}])
    assert out["results"][0]["status"] == "set", out


@pytest.mark.asyncio
async def test_an_if_yes_box_after_a_no_answer_stays_empty_or_takes_na(store, monkeypatch):
    """CenturyLink (SuccessFactors, 2026-10-21): 'Are you a former employee of Lumen ...?' was answered No, then
    'If yes, circumstances' got his stored reason for leaving ('Internship ended') and the judge failed the draft."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher({"id": 408, "company": "CenturyLink", "ats": "successfactors"})
    q = "* Are you a former employee of Lumen or any affiliated companies?"
    items = [{"ref": "e38", "tag": "select", "label": q, "value": "", "required": True},
             {"ref": "e40", "tag": "input", "type": "text", "label": "If yes, circumstances", "value": ""},
             {"ref": "e41", "tag": "textarea", "label": "If yes, please describe the circumstances (Enter 'N/A' if not applicable.)", "value": ""},
             {"ref": "e50", "tag": "select", "label": "Have you applied to Lumen before?", "value": ""},
             {"ref": "e51", "tag": "input", "type": "text", "label": "If so, when?", "value": ""}]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    sent = {}

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        out = []
        for p_ in plan:
            sent[p_["ref"]] = p_["value"]
            out.append(FieldResult(p_["ref"], True, str(p_["value"]), ""))
        return out
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("bank.prior-employee", "No", answer_type="choice", source="seed")
    answers.save_answer("background.reason_for_leaving", "Internship ended", answer_type="text", source="seed")
    answers.save_answer("bank.applied-before", "Yes", answer_type="choice", source="seed")
    answers.save_answer("bank.applied-when", "Spring 2026", answer_type="text", source="seed")
    await d.t_fill_batch([{"ref": "e38", "answer_key": "bank.prior-employee", "value": None, "source": None, "label": q}])
    out, _ = await d.t_fill_batch([
        {"ref": "e40", "answer_key": "background.reason_for_leaving", "value": None, "source": None, "label": "If yes, circumstances"},
        {"ref": "e41", "answer_key": "background.reason_for_leaving", "value": None, "source": None, "label": "If yes, please describe"}])
    res = {r["ref"]: r for r in (out.get("results") if isinstance(out, dict) else out)}
    assert res["e40"]["status"] == "left_empty" and "e40" not in sent, (res, sent)
    assert sent.get("e41") == "N/A", sent
    # the same kind of box after a Yes answer takes his stored answer
    await d.t_fill_batch([{"ref": "e50", "answer_key": "bank.applied-before", "value": None, "source": None, "label": "Have you applied to Lumen before?"},
                          {"ref": "e51", "answer_key": "bank.applied-when", "value": None, "source": None, "label": "If so, when?"}])
    assert sent.get("e51") == "Spring 2026", sent


@pytest.mark.asyncio
async def test_an_if_yes_box_follows_the_question_drawn_right_above_it(store, monkeypatch):
    """CenturyLink (2026-10-21): the box after 'former employee of Lumen?' took 'Accommodation' (two fields up, the
    nearest answered question) as its question; the question right above it decides."""
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher({"id": 409, "company": "CenturyLink", "ats": "successfactors"})
    items = [{"ref": "e33", "tag": "select", "label": "Do you require a reasonable accommodation?", "value": "", "bbox": [400, 100, 300, 30]},
             {"ref": "e38", "tag": "input", "type": "text", "combo": True, "label": "* Are you a former employee of Lumen?", "value": "", "bbox": [400, 200, 300, 30], "required": True},
             {"ref": "e39", "tag": "button", "label": "Are you a former employee of Lumen?", "bbox": [560, 200, 20, 20]},
             {"ref": "e40", "tag": "textarea", "label": "* If yes, please describe the circumstances (Enter 'N/A' if not applicable.)", "value": "", "bbox": [400, 260, 300, 60], "required": True}]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    sent = {}

    async def fake_fill(ex, plan):
        from goat.browser.fill import FieldResult
        for p_ in plan:
            sent[p_["ref"]] = p_["value"]
        return [FieldResult(p_["ref"], True, str(p_["value"]), "") for p_ in plan]
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "fill_batch", fake_fill)
    answers.save_answer("bank.accommodation", "No", answer_type="choice", source="seed")
    answers.save_answer("bank.prior-employee", "No", answer_type="choice", source="seed")
    answers.save_answer("background.reason_for_leaving", "Internship ended", answer_type="text", source="seed")
    await d.t_fill_batch([{"ref": "e33", "answer_key": "bank.accommodation", "value": None, "source": None, "label": "Accommodation"}])
    out, _ = await d.t_fill_batch([{"ref": "e40", "answer_key": "background.reason_for_leaving", "value": None, "source": None, "label": "If yes"}])
    res = {r["ref"]: r for r in (out.get("results") if isinstance(out, dict) else out)}
    assert res["e40"]["status"] == "failed" and "former employee" in res["e40"]["error"] and "e40" not in sent, (res, sent)
    await d.t_fill_batch([{"ref": "e38", "answer_key": "bank.prior-employee", "value": None, "source": None, "label": "Former employee"}])
    await d.t_fill_batch([{"ref": "e40", "answer_key": "background.reason_for_leaving", "value": None, "source": None, "label": "If yes"}])
    assert sent.get("e40") == "N/A", sent



@pytest.mark.asyncio
async def test_a_park_after_no_reset_email_points_at_create_account(store, monkeypatch):
    """NetApp, Corning, MetLife (2026-10-21): no reset email came and the run parked instead of creating the account."""
    from goat.agent import tools as toolsmod
    from goat.browser.snapshot import Snapshot
    d, ctx = _dispatcher({"id": 433, "company": "NetApp", "ats": "other"})

    async def page_text(n=30000):
        return "Career Opportunities: Sign In Already have an account? Not a registered user yet? Create an account"
    ctx.ex.page_text = page_text
    items = [{"ref": "e1", "tag": "input", "type": "text", "label": "Email Address:*"},
             {"ref": "e2", "tag": "input", "type": "password", "label": "Password:*"},
             {"ref": "e6", "tag": "a", "label": "Create an account"}]

    async def take(ex, with_text=False):
        s = Snapshot(url="https://career4.successfactors.com/career?company=netappinc", title="Sign In", headings=[], items=items,
                     groups=[], errors={}, challenge={})
        for i in items:
            s.refs[i["ref"]] = i
        return s
    monkeypatch.setattr(toolsmod.snapmod, "take", take)
    ctx.reset_not_received = True
    out, _ = await d.t_park("portal_error: password reset email was not received")
    assert "Create an account" in out["error"] and "[e6]" in out["error"], out
    assert ctx.control_failures.get("park:create_needed") == 1



def test_an_apply_menu_is_navigation_never_a_decision():
    """NetApp / Acuity (2026-10-21): 'Apply now' was chosen with answer_key identity.email and blocked the form's gate."""
    from goat.agent.tools import APPLY_NAV
    for lab in ("Apply now", "Apply Now »", "Apply", "Apply with LinkedIn", "Start your application", "I'm interested"):
        assert APPLY_NAV.match(lab), lab
    for lab in ("Applied before?", "Application questions", "How did you apply?", "Apply to other roles?"):
        assert not APPLY_NAV.match(lab), lab



def test_a_portal_where_he_already_signed_in_and_applied_is_never_signed_up_again(store):
    """EA (Avature, 2026-10-21): the fourth EA application looped 32 times through first-time-applicant sign-up."""
    from goat import db, states
    from goat.agent.tools import prior_account_job
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('EA1','Electronic Arts','SWE Intern','https://jobs.ea.com/en_US/careers/JobDetail/x/1','other')")
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('EA2','Electronic Arts','Analytics Intern','https://jobs.ea.com/en_US/careers/JobDetail/y/2','other')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(91,'EA1',?,'account',0)", (states.SUBMITTED,))
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(92,'EA2',?,'account',0)", (states.QUEUED,))
        con.execute("INSERT INTO events(job_id,ts,kind,detail) VALUES(91,1,'tool:credentials','{}')")
    job = {"id": 92, "company": "Electronic Arts"}
    assert prior_account_job(job, "jobs.ea.com") == 91
    assert prior_account_job(job, "career4.successfactors.com") is None
    assert prior_account_job({"id": 92, "company": "Acme"}, "jobs.ea.com") is None



def test_the_way_back_to_a_sign_in_page_is_never_a_session_bound_address():
    """Phillips 66 (SuccessFactors, 2026-10-21): the address after the failed Sign In carried '_s.crb=' and reopening it
    showed 'An error occurred while processing your request'; the sign-in page itself was the way back."""
    from goat.agent.tools import way_back_url
    crumb = "https://career4.successfactors.com/career?_s.crb=cLy%252bQl2XRuCeOlWtcoU8z4S4LCqUzFT8qy1DUi76rk8%253d"
    page = "https://career4.successfactors.com/careers?company=Phillips66"
    assert way_back_url(page, crumb) == page
    assert way_back_url(crumb, page) == page
    assert way_back_url(crumb) == ""
    assert way_back_url("", "https://x.example.com/login;jsessionid=AB12") == ""
    assert way_back_url("https://jobs.example.com/apply?site=3&sid=9") == "https://jobs.example.com/apply?site=3&sid=9"



def test_an_oracle_import_box_is_refused_only_when_the_page_has_another_resume_slot():
    """onsemi / Vertiv (2026-10-21): the import box was the page's only résumé box; refusing it left the gate asking for a
    résumé that no tool could attach."""
    from types import SimpleNamespace
    from goat.agent.tools import other_resume_slot
    imp = {"ref": "e2", "type": "file", "label": "Import your profile from resume"}
    cover = {"ref": "e7", "type": "file", "label": "Upload Cover Letter"}
    attach = {"ref": "e9", "type": "file", "label": "Upload Resume"}
    assert other_resume_slot(SimpleNamespace(items=[imp, cover]), "e2", "oracle") is None
    assert other_resume_slot(SimpleNamespace(items=[imp, cover, attach]), "e2", "oracle")["ref"] == "e9"
    assert other_resume_slot(None, "e2", "oracle") is None



def test_a_create_account_page_with_one_password_box_is_a_sign_up():
    """TikTok (2026-10-21): /create-account has a single password box, and credentials 'create' was refused as a Sign In form."""
    from types import SimpleNamespace
    from goat.agent.tools import single_password_signup
    tiktok = SimpleNamespace(title="Create Account", headings=["Sign in"], url="https://lifeattiktok.com/create-account")
    assert single_password_signup(tiktok, "https://lifeattiktok.com/create-account")
    assert not single_password_signup(SimpleNamespace(title="Sign in", headings=["Sign in"], url=""), "https://lifeattiktok.com/login")
    assert not single_password_signup(SimpleNamespace(title="Join our Talent Community", headings=[], url=""),
                                      "https://careers.example.com/talentcommunity/register")



def test_a_date_boxs_placeholder_is_never_its_value():
    """AutoZone (Oracle, 2026-10-21): set_date read back 'Month' from a Start Date Month picker and called it set."""
    from goat.agent.tools import DATE_PLACEHOLDER
    for p in ("Month", "Year", "MM", "YYYY", "Select One", "", "  "):
        assert DATE_PLACEHOLDER.match(p), p
    for v in ("February", "2026", "02/01/2026", "Feb 2026"):
        assert not DATE_PLACEHOLDER.match(v), v



def test_a_ranking_never_repeats_a_pick():
    """Amazon (2026-10-21): the first, second and third US location preferences all became 'TX - Dallas'."""
    from goat.agent.tools import RANK_LABEL, rank_wants
    from goat.browser.fill import best_option
    for lab in ("Second US location preference", "Third US location preference", "2nd choice", "Preference #2", "fourth priority location"):
        assert RANK_LABEL.search(lab), lab
    for lab in ("First US location preference", "Location preference", "Preferred location"):
        assert not RANK_LABEL.search(lab), lab
    chain = ["All offered locations", "Dallas", "Houston", "Austin", "Dallas, TX", "Houston, TX", "Austin, TX", "Seattle"]
    opts = [{"text": t} for t in ("WA - Seattle", "TX - Dallas", "TX - Houston", "TX - Austin")]
    second = rank_wants(chain, ["TX - Dallas"])
    assert best_option(opts, second)["text"] == "TX - Houston"
    third = rank_wants(chain, ["TX - Dallas", "TX - Houston"])
    assert best_option(opts, third)["text"] == "TX - Austin"
    assert rank_wants(["Dallas"], ["TX - Dallas"]) == ["Dallas"]



def test_a_stuck_park_that_lists_what_is_left_is_not_final_and_the_citizenship_date_is_no_certificate():
    """Westinghouse (2026-10-21): five 'stuck ... remains incomplete' parks in 2.5 minutes, the fifth stood with every
    blocker fillable; 'Date Obtained 1' in the export-control block got his AWS certification date."""
    from types import SimpleNamespace
    from goat.agent.tools import STUCK_TODO, OBTAINED_DATE, citizenship_block
    for r in ("stuck: SuccessFactors application remains incomplete; required employment fields need completion",
              "stuck: multiple required export-control and employment questions still unanswered",
              "stuck: required form completion includes selections not yet read from the full page"):
        assert STUCK_TODO.search(r), r
    for r in ("stuck: the Edit button does nothing and the page reloads", "stuck: the portal refuses navigation back to My Experience"):
        assert not STUCK_TODO.search(r), r
    assert OBTAINED_DATE.search("Date Obtained 1")
    snap = SimpleNamespace(items=[{"label": "* Country 1 Name"}, {"label": "* Type of Status 1"}, {"label": "Date Obtained 1"}])
    assert citizenship_block(snap)
    assert not citizenship_block(SimpleNamespace(items=[{"label": "Certification Name"}, {"label": "Date Obtained"}]))



def test_a_landing_pages_sign_in_link_is_the_way_to_the_sign_up_link():
    """NetApp (SuccessFactors, 2026-10-21): the sign-in address opened again showed only the careers home's 'Sign In' link."""
    from types import SimpleNamespace
    from goat.agent.tools import sign_in_link
    home = SimpleNamespace(items=[{"ref": "e1", "tag": "a", "label": "Sign In"}, {"ref": "e3", "tag": "a", "label": "People Central"}])
    assert sign_in_link(home)["ref"] == "e1"
    form = SimpleNamespace(items=[{"ref": "e1", "tag": "input", "type": "text", "label": "Email Address:*"},
                                  {"ref": "e2", "tag": "input", "type": "password", "label": "Password:*"},
                                  {"ref": "e4", "tag": "button", "label": "Sign In"}])
    assert sign_in_link(form) is None



def test_a_park_that_names_corrections_left_is_still_work_to_do():
    """AutoZone (2026-10-21): 'misassigned work-history entries ... requires correction; ... still require completion'."""
    from goat.agent.tools import STUCK_TODO
    for r in ("stuck: AutoZone application has misassigned work-history entries and requires correction",
              "stuck: Five-entry history, education details, remaining sections, and later pages still require completion",
              "stuck: three tiles need correction"):
        assert STUCK_TODO.search(r), r



def test_a_value_the_page_tells_the_applicant_to_enter_is_sourced_by_the_form():
    """Corning (SuccessFactors, 2026-10-21): 'If you answered Yes above, enter "Not applicable"' kept 'Yes' through two judge
    fixes; AutoZone's 'Reason for Leaving (Enter NA if current role)'."""
    from goat.agent.tools import dictated_by_form
    assert dictated_by_form("Not applicable", 'If you answered Yes above, please enter "Not applicable" in this box.')
    assert dictated_by_form("NA", "Reason for Leaving (Enter NA if current role)")
    assert dictated_by_form("Not applicable", "If yes, enter \u201cNot applicable\u201d.")
    assert not dictated_by_form("Yes", 'please enter "Not applicable" in this box')
    assert not dictated_by_form("Not", 'please enter "Not applicable" in this box')
    assert not dictated_by_form("Texas A&M University", "Enter your school name")



def test_a_select_all_consent_box_is_never_ticked_by_the_consent_pass():
    """LLNL (SmartRecruiters, 2026-10-21): 'Select all - by selecting all, you acknowledge the checkboxes below' ticked the
    talent-community box too."""
    from goat.adapters import SELECT_ALL_BOX
    assert SELECT_ALL_BOX.match("Select all - by selecting all, you acknowledge the checkboxes below")
    assert SELECT_ALL_BOX.match("Check all that apply")
    assert not SELECT_ALL_BOX.match("You declare that you have read and understand the privacy notice of LLNL. *")
    assert not SELECT_ALL_BOX.match("I agree to the terms")



def test_a_community_consent_is_his_never_join_rule():
    """LLNL (SmartRecruiters, 2026-10-21): '... agree to be part of communities for future career opportunities' was ticked
    as a future-roles consent."""
    from goat.adapters import consent_key
    assert consent_key("You declare that you have read and understand the privacy notice of LLNL and agree to be part of "
                       "communities for future career opportunities") == "policy.may_join_talent_community"
    assert consent_key("Join our Talent Community") == "policy.may_join_talent_community"
    assert consent_key("Do you consent for your data to be further processed for other job opportunities?") == "consent.keep_profile_future_roles"



def test_fill_batch_never_ticks_a_community_or_select_all_box():
    """LLNL (2026-10-21) went in with the community box and 'Select all' ticked by fill_batch under consent keys."""
    from goat.agent.tools import never_tick
    comm = {"type": "checkbox", "label": "You declare that you have read and understand the privacy notice of LLNL and agree to be "
                                         "part of communities for future career opportunities"}
    assert "never to join" in never_tick(comm, "true")
    assert never_tick(comm, "false") == ""
    assert "each required box" in never_tick({"type": "checkbox", "label": "Select all - by selecting all, you acknowledge"}, "Yes")
    assert never_tick({"type": "checkbox", "label": "I have read the job applications privacy notice"}, "true") == ""
    assert never_tick({"type": "text", "label": "Join our talent community"}, "true") == ""



def test_open_questions_are_found_for_the_idle_hold(store):
    """Wellmark (2026-10-21): the run asked, went quiet, and was held 'stuck' with nothing waiting on the answer."""
    from types import SimpleNamespace
    from goat import db
    from goat.agent.loop import open_question_keys
    job = _job()
    with db.tx() as con:
        con.execute("INSERT INTO pending_questions(job_id,key,question,options,field_type,asked_at,deadline_at,answered_at,answer) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (job["id"], "q.open", "Open?", "[]", "text", 1, 2, None, None))
        con.execute("INSERT INTO pending_questions(job_id,key,question,options,field_type,asked_at,deadline_at,answered_at,answer) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (job["id"], "q.done", "Done?", "[]", "text", 1, 2, 3, "No"))
    ctx = SimpleNamespace(asked_keys=["q.open", "q.done", "q.other"], job_id=job["id"])
    assert open_question_keys(ctx) == ["q.open"]
    assert open_question_keys(SimpleNamespace(asked_keys=[], job_id=job["id"])) == []



def test_a_bounced_park_lists_the_blockers_his_stored_answers_cover(store):
    """Westinghouse (2026-10-21) parked 'no_standing_answer' five times over fields with stored answers."""
    from goat import answers
    from goat.agent.tools import stored_for_blockers
    answers.save_answer("identity.age_18_plus", "Yes", source="questionnaire", aliases=["are you 18 years or older"])
    got = stored_for_blockers(["empty_required: * Are you 18 years or older? (text) ref=e124",
                               "empty_required: * What is your favourite colour? (text) ref=e130",
                               "not an application page: no submit control"])
    assert got == ["[e124] Are you 18 years or older? -> identity.age_18_plus = Yes"], got



def test_a_part_of_a_stored_address_answer_fills_one_field(store):
    """Transcard Payments (Paylocity, 2026-10-21): Surya's answer gave line 1, city, county, state and ZIP of a remote
    employer in one line; the form has a field for each, and the run parked because none could take the whole answer."""
    d, ctx = _dispatcher(_job())
    answers.save_answer("q.remote_employer_address", "Remote role worked from home: address line 1 6611 Cadogan Court, city Sugar Land, "
                        "county Fort Bend, state TX, ZIP 77479", answer_type="text", source="operator")
    val, tag, err = d._resolve_value("q.remote_employer_address", "Fort Bend", "part", "County")
    assert err is None and val == "Fort Bend" and tag.endswith("(part)")
    assert d._resolve_value("q.remote_employer_address", "77479", "part", "Zip Code")[0] == "77479"
    val, tag, err = d._resolve_value("q.remote_employer_address", "Harris", "part", "County")
    assert val is None and "not part of" in err
    # 'part' with no value takes the whole answer: DP World's run sent every field that way and all failed (2026-10-21)
    assert d._resolve_value("q.remote_employer_address", None, "part", "Address")[0].startswith("Remote role worked from home")



def test_a_plain_bachelor_of_science_is_never_the_honours_one():
    """Northrop Grumman (Workday, 2026-10-22): 'Bachelor of Science with Honours' came first in the list and was picked."""
    from goat.browser.fill import best_option
    wants = ["Bachelor of Science", "Bachelor's Degree", "Bachelor's", "Bachelors", "Bachelor", "Undergraduate"]
    opts = [{"text": t} for t in ("Associate's Degree", "Bachelor of Science with Honours", "Bachelor of Science", "Bachelor of Arts")]
    assert best_option(opts, wants)["text"] == "Bachelor of Science"
    opts = [{"text": t} for t in ("Bachelor of Science with Honours", "Bachelor's Degree", "Master's Degree")]
    assert best_option(opts, wants)["text"] == "Bachelor's Degree"
    opts = [{"text": t} for t in ("Bachelor of Science (Honors)", "Bachelor of Science (BS)")]
    assert best_option(opts, wants)["text"] == "Bachelor of Science (BS)"



def test_a_successfactors_eu_tenant_is_an_allowed_host():
    """Paccar (2026-10-22): career5.successfactors.eu was refused as 'host not on allowlist', so the run could not reopen it."""
    from goat.browser.executor import HostPolicy
    p = HostPolicy(["https://jobs.paccar.com/x"])
    assert p.allowed("https://career5.successfactors.eu/career?company=paccarinc")
    assert not p.allowed("https://successfactors.eu.example.net/x")


@pytest.mark.asyncio
async def test_back_onto_a_posted_form_reopens_the_application(store):
    """Paccar (2026-10-22): Back after the password-reset request hit net::ERR_CACHE_MISS twice and the run parked."""
    d, ctx = _dispatcher(_job())
    ctx.job["apply_url"] = "https://career5.successfactors.eu/career?company=paccarinc&career_job_req_id=1"
    went = []

    class Page:
        async def go_back(self, **kw):
            raise Exception("Page.go_back: net::ERR_CACHE_MISS")

    class Ex:
        page = Page()
        url = "chrome-error://chromewebdata/"

        async def goto(self, url):
            went.append(url)
            return url

        async def settle(self, **kw):
            pass

    ctx.ex = Ex()
    out, _ = await d.t_tabs("back")
    assert went == [ctx.job["apply_url"]] and "Confirm Form Resubmission" in out["note_back"]



def test_a_job_updates_box_is_never_ticked_and_a_zip_extension_stays_blank(store):
    """Berkshire Hathaway Energy (Oracle, 2026-10-22): 'I agree to receive updates about new job projects.' was ticked as his
    keep-my-profile consent, and 'Postal Code Extension' got the five-digit ZIP; the judge caught both."""
    from goat.adapters import future_roles_consent
    from goat.agent.tools import never_tick
    box = {"type": "checkbox", "label": "I agree to receive updates about new job projects."}
    assert "job alerts" in never_tick(box, "true")
    assert never_tick({"type": "checkbox", "label": "I consent to my data being kept on file for future job opportunities."}, "true") == ""
    assert not future_roles_consent("I agree to receive updates about new job projects.")
    assert future_roles_consent("Do you consent for your data to be further processed for other job opportunities?")
    d, ctx = _dispatcher(_job())
    val, tag, err = d._resolve_value("identity.zip", None, None, "Postal Code Extension")
    assert val is None and "ZIP+4" in err
    answers.save_answer("identity.zip", "77479", answer_type="text", source="questionnaire")
    assert d._resolve_value("identity.zip", None, None, "Postal Code")[:1] == ("77479",)



def test_a_part_never_picks_one_entry_of_a_choice_chain(store):
    """2026-10-22: the agent began sending source 'part' on choice fields; a race chain led by a decline must never give
    its fallback entry alone."""
    d, ctx = _dispatcher(_job())
    answers.save_answer("self_id.race_ethnicity", "Decline to self-identify; Asian", answer_type="choice_chain", source="questionnaire")
    val, tag, err = d._resolve_value("self_id.race_ethnicity", "Asian", "part", "Race")
    assert err is None and val.startswith("Decline"), val



def test_a_language_list_without_english_is_recognised():
    """Ryan (Workday, 2026-10-22): the Language picker offered Chinese ... Spanish and no English."""
    from goat.agent.tools import no_english_offered
    err = "the picker for this field did not open; the options on screen (['Select One', 'Chinese', 'Dutch', 'French', 'Spanish'])"
    assert no_english_offered("Language", err)
    assert not no_english_offered("Language", err.replace("'Chinese'", "'English'"))
    assert not no_english_offered("Preferred location", err)



LANG_BLOCKS = """<html><body>
<div id="work"><h3>Work Experience 1</h3><label>Job Title<input data-goat-ref="e10"></label><label>Company<input></label>
 <button>Delete</button></div>
<div id="langs"><h3>Languages</h3>
 <div id="l1"><h4>Languages 1</h4><button>Delete</button>
  <label>Language*<button data-goat-ref="e144">Select One</button></label>
  <label>Read*<button>Fluent</button></label></div>
 <button>Add Another</button></div>
<script>document.querySelectorAll('button').forEach(b => { if (b.textContent.trim() === 'Delete') b.addEventListener('click', e =>
  e.target.parentElement.remove()); });</script></body></html>"""


@pytest.mark.asyncio
async def test_the_empty_languages_block_is_deleted_when_no_english_is_offered(tmp_path):
    """Ryan (Workday, 2026-10-22): the run added a Languages entry it could not fill and never deleted it."""
    from goat.agent.tools import JS_DELETE_LANGUAGE_BLOCK
    from goat.browser.executor import Executor, HostPolicy
    ex = Executor(policy=HostPolicy(["about:blank"]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page()
        await ex.page.set_content(LANG_BLOCKS)
        ref = await ex.eval(JS_DELETE_LANGUAGE_BLOCK, "e144")
        assert ref == "e144del", ref
        await ex.click_ref(ref)
        assert await ex.page.evaluate("() => !document.getElementById('l1') && !!document.getElementById('work')")
        assert await ex.eval(JS_DELETE_LANGUAGE_BLOCK, "e10") is None      # a work entry is never taken for it
    finally:
        await ex.close(close_browser=True)



def test_a_vague_eligibility_reason_names_no_requirement():
    """Creative Artists Agency (2026-10-22) parked 'eligibility: cannot proceed'."""
    from goat.agent.tools import VAGUE_ELIGIBILITY
    for r in ("cannot proceed", "unable to continue the application", "stuck", "none"):
        assert VAGUE_ELIGIBILITY.fullmatch(r), r
    assert not VAGUE_ELIGIBILITY.fullmatch("the role requires pursuit of a graduate degree")



def test_marketing_as_a_field_of_study_is_no_opt_in():
    """Newell Brands (2026-10-22): a degree question naming Marketing was answered from consent.marketing_email."""
    from goat.agent.tools import consent_question_key
    assert consent_question_key("Are you currently pursuing a bachelor's degree in Business, Sales, Marketing, Finance, or a "
                                "related field?") is None
    assert consent_question_key("Would you like to receive marketing emails from us?") == "consent.marketing_email"
    assert consent_question_key("Sign me up for marketing communications and job alerts") == "consent.marketing_email"



def test_a_picker_rule_is_never_typed_into_a_text_box():
    """Newell Brands (2026-10-22): the location-ranking box got "Keep the form's own order: first option as first preference"."""
    from goat.agent.tools import RULE_TEXT, concrete_entries
    v = "Keep the form's own order: first option as first preference"
    assert RULE_TEXT.search(v) and not concrete_entries(v)
    assert not RULE_TEXT.search("Dallas; Houston; Austin")



def test_a_rank_your_locations_box_takes_the_posting_locations():
    """Newell Brands (2026-10-22): 'Rank preferred work locations' over Huntersville, NC and Brampton, ON."""
    from goat.adapters import POSTING_LOCATIONS
    assert POSTING_LOCATIONS.search("Rank preferred work locations")
    assert POSTING_LOCATIONS.search("Please rank the locations below in order of preference")
    assert not POSTING_LOCATIONS.search("Current location")



def test_an_error_page_with_only_links_is_no_form_page():
    """Deloitte (2026-10-22): 'An error has occurred / Page not found' and links only; the park was refused four times."""
    from goat.agent.tools import error_or_empty_page
    from goat.browser.snapshot import Snapshot
    err = Snapshot(url="https://apply.deloitte.com/en_US/careers/Error", title="Error | Deloitte US",
                   headings=["Deloitte US", "An error has occurred"], items=[{"ref": "e1", "tag": "a", "label": "Continue to home"}],
                   groups=[], errors={}, challenge={})
    assert error_or_empty_page(err)
    form = Snapshot(url="u", title="My Information", headings=["My Information"],
                    items=[{"ref": "e1", "tag": "input", "type": "text", "label": "First Name"}], groups=[], errors={}, challenge={})
    assert not error_or_empty_page(form)
    posting = Snapshot(url="https://jobs.example.com/job/123", title="Software Intern", headings=["Software Intern"],
                       items=[{"ref": "e1", "tag": "button", "label": "Apply"}], groups=[], errors={}, challenge={})
    assert not error_or_empty_page(posting)      # a posting page before Apply is no error page



def test_disabled_submits_are_counted_for_the_park(store):
    """Newell Brands (2026-10-22): Apply stayed disabled; five verified submits found no enabled control and every park was
    refused because the gate passed."""
    from goat import db
    from goat.agent.tools import disabled_submits
    job = _job()
    assert disabled_submits(job["id"]) == 0
    for _ in range(2):
        db.event(job["id"], "submit:blocked", {"why": ["no enabled submit control found (label=Apply; 46 candidates)"]})
    db.event(job["id"], "submit:blocked", {"why": ["something else"]})
    assert disabled_submits(job["id"]) == 2



def test_a_judgment_on_a_ledger_answer_shows_the_resolved_value(store):
    """Ecolab (2026-10-22): the judge saw 'preferences.applied_before = Yes / No' and failed the ledger-backed No."""
    from goat import answers
    answers.save_answer("preferences.applied_before", "Yes / No", answer_type="conditional:ledger_company", source="seed:spec")
    unconf, ev = answers.unconfirmed_source("judgment:preferences.applied_before", "Ecolab", "workday")
    assert unconf and "'No'" in ev and "ledger" in ev, ev


def test_a_judgment_shows_the_question_his_stored_answer_answers(store):
    """Cisco (2026-10-23): 'Office days per week' = '5 days in a week' was failed as unsupported against a bare 'Yes', whose
    question reads '...including 5 days a week?'."""
    from goat import answers
    answers.save_answer("availability.in_office_schedule", "Yes", answer_type="choice", source="questionnaire",
                        note="Can work in the office at the specified times, including 5 days a week?")
    unconf, ev = answers.unconfirmed_source("judgment:availability.in_office_schedule", "Cisco", "workday")
    assert unconf and "= 'Yes'" in ev and "including 5 days a week" in ev, ev


@pytest.mark.asyncio
async def test_another_sites_verification_mail_is_looked_past_before_parking(store):
    """Deutsche Bank (Recsolu, 2026-10-22): an Oracle tenant's day-old mail was the newest 'verify' email; the run was held
    at once. The link is never opened; the run looks again, and parks on the third such find."""
    from goat.budget import Parked
    from goat.mail import gmail
    d, ctx = _dispatcher(_job())

    class Mail:
        def fetch_verification_link(self, *a, **k):
            return gmail.LinkResult("host_mismatch", None, "m1")

    ctx.mail = Mail()
    for _ in range(2):
        out, _ = await d.t_open_verification_link()
        assert out["status"] == "not_received" and "not opened" in out["note"]
    with pytest.raises(Parked):
        await d.t_open_verification_link()



def test_a_careers_search_page_that_found_nothing_lets_the_park_stand():
    """Truist (2026-10-22): the posting link landed on the careers search page with 'No result found'."""
    from goat.agent.tools import error_or_empty_page
    from goat.browser.snapshot import Snapshot
    snap = Snapshot(url="https://careers.truist.com/us/en/search-results?keywords=Technology%20and%20Operations%20Intern",
                    title="Search results | Find available job openings at Truist Bank", headings=["Discover your next career"],
                    items=[{"ref": "e2", "tag": "input", "type": "text", "label": "Search for Job Title"},
                           {"ref": "e4", "tag": "button", "label": "Search"}],
                    groups=[], errors={"error_text": [{"text": "No result found", "near": "Sort by"}]}, challenge={})
    assert error_or_empty_page(snap)
    snap.items.append({"ref": "e9", "tag": "input", "type": "checkbox", "label": "Technology"})   # a Category filter
    assert error_or_empty_page(snap)
    snap.errors = {}
    assert not error_or_empty_page(snap)



def test_a_send_code_button_is_no_application_submit():
    """Mathtech (ADP, 2026-10-22): the verification dialog's 'Send' was refused by click and by submit."""
    from goat.agent.tools import VERIFY_BUTTON
    for b in ("Send", "Send code", "Resend code", "Verify email", "Send verification code"):
        assert VERIFY_BUTTON.match(b), b
    for b in ("Submit", "Submit Application", "Send Application"):
        assert not VERIFY_BUTTON.match(b), b



def test_a_quick_apply_page_is_an_application_not_an_account_page():
    """Safran (SuccessFactors quick apply, 2026-10-22): account creation and the whole application on one page; its 'Apply'
    was clicked as 'Create Account' and the application was sent without the gate or the judge."""
    from types import SimpleNamespace
    from goat.agent.tools import FINAL_SUBMIT_LABEL, combined_account_application
    fields = [{"tag": "input", "type": "text", "value": v} for v in ("Surya", "Thirukonda", "a@b.c", "a@b.c", "281", "Sugar Land")]
    pw = [{"tag": "input", "type": "password", "value": "x"}]
    assert combined_account_application(SimpleNamespace(items=fields + pw))
    assert not combined_account_application(SimpleNamespace(items=fields[:3] + pw))      # a create-account form
    assert not combined_account_application(SimpleNamespace(items=fields))              # no password box
    assert FINAL_SUBMIT_LABEL.match("Apply") and not FINAL_SUBMIT_LABEL.match("Create Account")



def test_an_eligibility_reason_that_says_it_is_satisfied_is_no_hold():
    """Sierra Space (2026-10-22): 'requires U.S. person status; Surya is a U.S. citizen, so this is satisfied' was parked."""
    from goat.agent.tools import SELF_SATISFIED
    assert SELF_SATISFIED.search("requires U.S. person status; Surya is a U.S. citizen, so this is satisfied. Need inspect")
    assert not SELF_SATISFIED.search("The role requires pursuing a graduate degree, which Surya is not pursuing.")
    # Westinghouse (2026-10-23)
    assert SELF_SATISFIED.search("This is a software role requiring C# or C++ coursework/project experience, and C++ is present, "
                                 "so eligible; wait can't park?")
    assert SELF_SATISFIED.search("requires a 3.0 GPA; Surya is eligible with a 4.0")
    assert not SELF_SATISFIED.search("requires an active Secret clearance before the start date; Surya is not eligible to start without one")
    assert not SELF_SATISFIED.search("open only to students who are not eligible for other programs")
    # Nissan (2026-10-23): parked eligibility while saying the condition is met
    assert SELF_SATISFIED.search("At Summer 2027 start Surya is a rising senior, which meets the junior/senior condition? The posting")
    assert not SELF_SATISFIED.search("requires entering a Master's program; Surya does not meet the graduate condition")
    assert SELF_SATISFIED.search("Surya will enter his junior year in Fall 2027 and is eligible.")
    assert not SELF_SATISFIED.search("requires U.S. citizenship and is not eligible for sponsorship")


@pytest.mark.asyncio
async def test_a_wrong_class_year_for_fall_2027_is_no_eligibility_hold(store):
    """Nissan (2026-10-23): held 'requires students entering their Junior or Senior year ... Surya will be a sophomore in
    Fall 2027'; he is a sophomore NOW (2026-27) and enters his junior year in Fall 2027."""
    job = _job()
    d, ctx = _dispatcher(job)

    async def page_text(n=0):
        return "Financial AI Engineering Intern. Students entering their Junior or Senior year at the start of the upcoming term."
    ctx.ex.page_text = page_text
    out, _ = await d.t_park("eligibility: Nissan requires students entering their Junior or Senior year at the start of the "
                            "upcoming academic term; Surya will be a sophomore in Fall 2027.")
    assert "junior year" in out["error"], out
    from goat.agent.tools import CLASS_YEAR_WRONG
    assert not CLASS_YEAR_WRONG.search("requires enrollment in a Master's program; Surya is an undergraduate")


@pytest.mark.asyncio
async def test_how_soon_could_you_start_takes_the_option_his_start_date_falls_in(store):
    """HMH (Workday, 2026-10-24): 'If offered this position, how soon could you start?' (Immediately ... Later than 1 month)
    went to Surya; he starts 05/18/2027, months away."""
    from goat import answers
    answers.save_answer("availability.earliest_start_date", "05/18/2027", answer_type="short", source="surya_chat")
    d, ctx = _dispatcher(_job())
    opts = ["Immediately", "Within 1 week", "Within 2 weeks", "Within 3 weeks", "Within 1 month", "Later than 1 month"]
    out, _ = await d.t_lookup_answer("If offered this position, how soon could you start?", opts)
    assert out.get("answer") == "Later than 1 month", out


@pytest.mark.asyncio
async def test_a_question_only_for_visa_holders_is_no_for_a_citizen(store):
    """Federal Reserve (Workday, 2026-10-24): 'If you are a visa holder, have you signed a declaration of intent to become a
    permanent resident and then a U.S. citizen?' went to Surya and the run timed out."""
    d, ctx = _dispatcher(_job())
    q = "If you are a visa holder, have you signed a declaration of intent to become a permanent resident and then a U.S. citizen?"
    out, _ = await d.t_lookup_answer(q, ["Yes", "No"])
    assert out.get("answer") == "No", out
    out, _ = await d.t_lookup_answer(q, ["Yes", "No", "Not applicable"])
    assert out.get("answer") == "Not applicable", out
    from goat.agent.tools import VISA_HOLDER_ONLY
    assert not VISA_HOLDER_ONLY.search("Will you now or in the future require visa sponsorship?")
    # Federal Reserve 557 (2026-10-24): the substring alias gave his sponsorship 'No', read as 'not eligible to work'
    answers.save_answer("bank.sponsorship-now", "No", answer_type="choice", source="seed:answer_bank.json",
                        aliases=["are you eligible to work without visa sponsorship"])
    q2 = ("If you are not a U.S. citizen, national, permanent resident or protected individual, are you eligible to work "
          "without visa sponsorship?")
    out, _ = await d.t_lookup_answer(q2, ["N/A", "Yes", "No"])
    assert out.get("answer") == "N/A" and out.get("key") == "work_authorization.visa_holder_only", out
    # a question that asks his citizenship first is his to answer
    assert not VISA_HOLDER_ONLY.search("Are you a U.S. citizen? If you are not a U.S. citizen, do you require sponsorship?")


@pytest.mark.asyncio
async def test_ask_user_never_sends_a_visa_holder_only_question(store):
    d, ctx = _dispatcher(_job())
    q = ("As a U.S. citizen who is not a visa holder, what should be selected for the conditional question about visa holders "
         "signing a declaration to become a permanent resident and then a U.S. citizen?")
    out, _ = await d.t_ask_user([{"question": q, "options": ["Yes", "No"], "field_type": "radio"}])
    assert out.get("asked") == [] and out["auto_answered"][0]["answer"] == "No", out


@pytest.mark.asyncio
async def test_language_rating_labels_under_a_language_field_take_his_english_proficiency(store):
    """Midland States Bank (Workday, 2026-10-24): the Language block's 'Comprehension', 'Overall', 'Reading', 'Speaking' and
    'Writing' looked up nothing on their own; the run parked five times and asked Surya for ratings."""
    from goat import answers
    from goat.browser.snapshot import Snapshot
    answers.save_answer("experience.english_proficiency", "Native; Fluent; Advanced", answer_type="choice_chain", source="seed")
    d, ctx = _dispatcher(_job())
    snap = Snapshot(url="u", title="t", headings=["My Experience"], items=[
        {"ref": "e111", "tag": "dropdown", "label": "Language*", "value": "English"},
        {"ref": "e113", "tag": "dropdown", "label": "Comprehension*", "value": ""}], groups=[], errors={}, challenge={})
    for i in snap.items:
        snap.refs[i["ref"]] = i
    ctx.last_snapshot = snap
    out, _ = await d.t_lookup_answer("Comprehension", [], None, "e113")
    assert out.get("key") == "experience.english_proficiency", out
    ctx.last_snapshot = Snapshot(url="u", title="t", headings=[], items=[], groups=[], errors={}, challenge={})
    out, _ = await d.t_lookup_answer("Overall", [], None, None)
    assert out.get("key") != "experience.english_proficiency", out          # no Language field: not a language rating


@pytest.mark.asyncio
async def test_english_rating_questions_in_full_sentences_are_never_asked(store):
    """Midland States Bank (2026-10-24, second run): 'What is your English proficiency level for Comprehension?' went to
    Surya (the resolver read the stored note as covering only overall / reading / speaking / writing) and the run timed out."""
    from goat import answers
    answers.save_answer("experience.english_proficiency", "Native; Fluent; Advanced", answer_type="choice_chain", source="seed")
    d, ctx = _dispatcher(_job())
    out, _ = await d.t_lookup_answer("What is your English proficiency level for Comprehension?", [], None, None)
    assert out.get("key") == "experience.english_proficiency", out
    out, _ = await d.t_ask_user([{"question": "What is your English proficiency level for Comprehension?", "options": [],
                                  "field_type": "dropdown"}])
    assert out.get("asked") == [] and out["auto_answered"][0]["key"] == "experience.english_proficiency", out
