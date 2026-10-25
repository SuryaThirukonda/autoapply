"""The knowledge-base resolver: lookup_answer and ask_user consult Surya's stored answers and profile by
meaning (the model sees the question, every choice and a screenshot) before anything reaches him; a fact
the profile states plainly becomes a derived answer with its evidence; sensitive questions are never
derived; the choice reader opens a custom dropdown, reads every option and closes it."""
from __future__ import annotations

import http.server
import threading

import pytest

from goat import answers, db
from goat.agent.knowledge import knowledge_text
from goat.agent.tools import RunContext, ToolDispatcher
from goat.browser import fill
from goat.browser.executor import Executor, HostPolicy
from goat.budget import Budget
from goat.human import questions


@pytest.fixture()
def kb(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(questions, "BROKER", questions.QuestionBroker())
    import goat.agent.tools as tools_mod
    monkeypatch.setattr(tools_mod, "BROKER", questions.BROKER)
    answers.save_answer("work_authorization.us_citizen", "Yes", source="seed", aliases=["Are you a U.S. citizen?"])
    answers.save_answer("how_did_you_hear.default", "LinkedIn; Job Board; Other", source="seed")
    answers.save_answer("availability.passport", None, source="seed")
    prof = tmp_path / "profile.md"
    prof.write_text("## Education\nTexas A&M University, BS Computer Science, started August 2025, graduating May 2028.\n", encoding="utf-8")
    import goat.profile as profile_mod
    monkeypatch.setattr(profile_mod, "path", lambda: prof)
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','Acme','SWE Intern','https://boards.greenhouse.io/acme/jobs/1','greenhouse')")
        jid = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES('L1','filling','no_account',1,1)").lastrowid
    return dict(db.connect().execute("SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=?", (jid,)).fetchone())


class StubEx:
    url = "https://boards.greenhouse.io/acme/jobs/1"

    async def screenshot_png(self):
        return b"\x89PNG fake"


class FakeResolver:
    def __init__(self, outcome: dict):
        self.outcome = outcome
        self.calls = []

    async def resolve(self, job_id, question, options, *, meaning, field_type, knowledge, image_png):
        self.calls.append({"question": question, "options": options, "meaning": meaning, "knowledge": knowledge, "image": image_png})
        return dict(self.outcome)

    async def yes_no_from_profile(self, *a, **k):
        return "No", "default"


def _d(job, outcome):
    clf = FakeResolver(outcome)
    ctx = RunContext(job={**job, "resume_file": None}, ex=StubEx(), budget=Budget(job["id"], "greenhouse"), classifier=clf)
    return ToolDispatcher(ctx), clf


def test_the_knowledge_text_carries_wordings_undecided_items_and_the_profile(kb):
    t = knowledge_text("Acme", "greenhouse")
    assert "work_authorization.us_citizen" in t and "are you a u s citizen" in t
    assert "availability.passport" in t and "(none)" in t
    assert "graduating May 2028" in t


async def test_a_differently_worded_question_finds_the_stored_answer(kb):
    d, clf = _d(kb, {"meaning": "U.S. citizenship", "verdict": "stored_key", "key": "work_authorization.us_citizen",
                     "answer": None, "evidence": "work_authorization.us_citizen | Yes", "confidence": 0.93})
    out, _ = await d.t_lookup_answer("Do you hold citizenship of the United States of America?", ["Yes", "No"], "is he a US citizen", None)
    assert out["key"] == "work_authorization.us_citizen" and out["answer"] == "Yes" and out["via"] == "resolver"
    call = clf.calls[0]
    assert call["options"] == ["Yes", "No"] and call["image"] and call["meaning"] == "is he a US citizen"
    assert "PROFILE" in call["knowledge"]


async def test_a_fact_the_profile_states_becomes_a_derived_answer(kb):
    d, _ = _d(kb, {"meaning": "years of college finished", "verdict": "derived", "key": None, "answer": "1",
                   "evidence": "started August 2025", "confidence": 0.9})
    q = "How many years of college have you completed?"
    out, _ = await d.t_lookup_answer(q, ["0", "1", "2", "3", "4+"], None, None)
    assert out["via"] == "derived" and out["answer"] == "1" and "August 2025" in out["evidence"]
    a = answers.lookup(q)
    assert a is not None and a.answer == "1" and a.source == "derived"     # the alias makes it a plain hit next time
    ev = db.connect().execute("SELECT detail FROM events WHERE kind='answer:derived'").fetchone()
    assert ev is not None


async def test_a_derived_answer_outside_the_choices_is_refused(kb):
    d, _ = _d(kb, {"meaning": "years", "verdict": "derived", "key": None, "answer": "one and a half",
                   "evidence": "started August 2025", "confidence": 0.95})
    out, _ = await d.t_lookup_answer("How many years of college have you completed?", ["0", "1", "2"], None, None)
    assert out.get("miss") is True


async def test_sensitive_questions_are_never_derived(kb):
    d, _ = _d(kb, {"meaning": "salary", "verdict": "derived", "key": None, "answer": "$40/hr",
                   "evidence": "posting lists $40/hr", "confidence": 0.99})
    out, _ = await d.t_lookup_answer("What is your desired salary?", [], None, None)
    assert out.get("miss") is True
    assert answers.lookup("What is your desired salary?") is None


async def test_low_confidence_and_undecided_items_stay_questions_for_surya(kb):
    d, _ = _d(kb, {"meaning": "passport", "verdict": "stored_key", "key": "availability.passport",
                   "answer": None, "evidence": "", "confidence": 0.99})
    out, _ = await d.t_lookup_answer("Do you hold a valid passport?", ["Yes", "No"], None, None)
    assert out.get("miss") is True                                        # stored as (none): not an answer


async def test_ask_user_consults_the_knowledge_base_first_and_reuses_the_result(kb):
    d, clf = _d(kb, {"meaning": "U.S. citizenship", "verdict": "stored_key", "key": "work_authorization.us_citizen",
                     "answer": None, "evidence": "", "confidence": 0.9})
    await d.t_lookup_answer("Are you a citizen of the US?", ["Yes", "No"], None, None)
    out, _ = await d.t_ask_user([{"question": "Are you a citizen of the US?", "options": ["Yes", "No"], "field_type": "radio", "ref": "e1"}])
    assert out["asked"] == [] and out["auto_answered"][0]["key"] == "work_authorization.us_citizen"
    assert len(clf.calls) == 1                                            # resolved once, reused
    assert db.connect().execute("SELECT COUNT(*) FROM pending_questions").fetchone()[0] == 0

    d2, _ = _d(kb, {"meaning": "?", "verdict": "unknown", "key": None, "answer": None, "evidence": "", "confidence": 0.2})
    out2, _ = await d2.t_ask_user([{"question": "Which shift do you prefer?", "options": ["Day", "Night"], "field_type": "radio", "ref": "e2"}])
    assert len(out2["asked"]) == 1


# --------------------------------------------------------------------------- the choice reader, in a real browser
PAGE = """<html><body>
<label for="s">Degree</label><select id="s"><option>Select...</option><option>Bachelor's</option><option>Master's</option></select>
<div style="margin-top:20px"><label id="ml">Hear about us</label>
  <button id="b" aria-haspopup="listbox" aria-labelledby="ml" style="width:200px;height:30px"
    onclick="document.getElementById('lb').style.display='block'">Select One</button>
  <ul id="lb" role="listbox" style="display:none">
    <li role="option">LinkedIn</li><li role="option">Indeed</li><li role="option">Other</li></ul></div>
<script>document.addEventListener('keydown', e => { if (e.key === 'Escape') document.getElementById('lb').style.display = 'none'; });</script>
</body></html>"""


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE.encode())

    def log_message(self, *a):
        pass


async def test_peek_options_reads_every_choice_without_choosing(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    ex = Executor(policy=HostPolicy([base]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.goto(base)
        from goat.browser import snapshot
        snap = await snapshot.take(ex)
        sel = next(i for i in snap.items if i.get("tag") == "select")
        btn = next(i for i in snap.items if i.get("tag") in ("button", "dropdown") and i is not sel)
        opts, png = await fill.peek_options(ex, sel["ref"])
        assert opts == ["Bachelor's", "Master's"] and png
        opts2, png2 = await fill.peek_options(ex, btn["ref"])
        assert opts2 == ["LinkedIn", "Indeed", "Other"] and png2
        assert await ex.page.evaluate("() => getComputedStyle(document.getElementById('lb')).display") == "none"   # closed again
        assert await ex.page.evaluate("() => document.getElementById('b').innerText") == "Select One"            # nothing chosen
    finally:
        await ex.close()
        srv.shutdown()
        srv.server_close()


async def test_free_response_prompts_never_reach_the_resolver(kb):
    d, clf = _d(kb, {"meaning": "why", "verdict": "derived", "key": None, "answer": "Because ...", "evidence": "profile", "confidence": 0.99})
    out, _ = await d.t_lookup_answer("Why are you interested in this role?", [], None, None)
    assert out.get("miss") is True and "draft it yourself" in out["hint"] and clf.calls == []



def test_a_stored_none_is_shown_as_his_answer(tmp_path):
    """Aptiv (2026-10-19): his web answer 'None' to the foreign-travel question read as a missing value."""
    from goat import answers, db
    from goat.agent import knowledge
    db.reset_for_tests(tmp_path / "k.db")
    answers.save_answer("q.list_countries_outside_the_u_s", "None", source="web")
    knowledge._cache.clear()
    line = next(l for l in knowledge.knowledge_text("Aptiv", "workday").splitlines() if "q.list_countries_outside_the_u_s" in l)
    assert "his answer: nothing applies" in line


async def test_a_derived_answer_to_a_bare_label_belongs_to_its_job_only(kb):
    """Fenwick & West (Workday, 2026-10-24): 'Day' (of the available-to-work date) was derived as 18 and saved as the global
    q.day: any later lone 'Day' box (a date of birth) would have taken it. q.other ('Other') and q.yes_no_no ('Yes') too."""
    d, _ = _d(kb, {"meaning": "the day of the available-to-work date", "verdict": "derived", "key": None, "answer": "18",
                   "evidence": "availability.earliest_start_date 05/18/2027", "confidence": 0.95})
    out, _ = await d.t_lookup_answer("Day", [], None, None)
    assert out["via"] == "derived" and out["answer"] == "18", out
    assert answers.get("q.day") is None
    assert out["key"] == f"q.day.job{kb['id']}", out


async def test_public_employment_is_never_derived(kb):
    """Booz Allen (2026-10-25): 'employed by a state/local government entity or other public entity, including a public
    university such as Texas A&M' was derived as 'Current' from his research role and filled without him."""
    d, _ = _d(kb, {"meaning": "state or public employment", "verdict": "derived", "key": None, "answer": "Current",
                   "evidence": "Texas A&M University, Rathinam Group (undergraduate research assistant)", "confidence": 0.93})
    q = ("Have you been employed by a state/local government entity or other public entity, including a public university "
         "such as Texas A&M?")
    out, _ = await d.t_lookup_answer(q, ["Current", "Former", "Never", "Prefer not to answer"], None, None)
    assert out.get("miss") is True
    out, _ = await d.t_ask_user([{"question": q, "options": ["Current", "Former", "Never", "Prefer not to answer"],
                                  "field_type": "dropdown", "ref": "e15"}])
    assert not out.get("auto_answered") and len(out["asked"]) == 1
    assert answers.lookup(q) is None
