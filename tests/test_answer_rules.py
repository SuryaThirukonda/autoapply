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
    assert consent_key("Join our talent community") is None


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
