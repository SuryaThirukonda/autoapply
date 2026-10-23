"""The judge: one schema-constrained gpt-6-luna verdict per shadow draft, stored with the job, never on live runs."""
import json
from types import SimpleNamespace

import pytest

from goat import db, judge, records
from goat.config import SETTINGS


class _FakeClient:
    def __init__(self, canned, raise_exc=None):
        self.canned, self.raise_exc, self.calls = canned, raise_exc, []
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        if self.raise_exc:
            raise self.raise_exc
        return SimpleNamespace(output_text=json.dumps(self.canned), usage=SimpleNamespace(input_tokens=1200, output_tokens=40), model=kw["model"])


def _seed(tmp_path, job_id=1):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Niantic','Intern','https://x.example/job','ashby')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(?, 'l1','hold','no_account',0)", (job_id,))
    records.record_decision(job_id, 1, "Email", "a@x.com", "standing:identity.email", "e4")


@pytest.mark.asyncio
async def test_judge_stores_row_and_events_with_the_allowed_model(tmp_path):
    _seed(tmp_path)
    fake = _FakeClient({"verdict": "fail", "bucket": "wrong_value", "notes": "Email differs", "fields": ["Email"]})
    out = await judge.Judge(client=fake).judge(1, records.decisions(1), b"\x89PNG", "Review page", {"result": "pass_shadow"})
    assert out["verdict"] == "fail"
    kw = fake.calls[0]
    assert kw["model"] == "gpt-6-luna" and kw["store"] is False
    assert kw["text"]["format"]["name"] == "judge" and kw["text"]["format"]["strict"] is True
    content = kw["input"][0]["content"]
    assert any(c["type"] == "input_image" for c in content)
    assert any("DECISION LOG" in c.get("text", "") and "a@x.com" in c.get("text", "") for c in content)
    row = db.connect().execute("SELECT job_id, verdict, bucket, notes, model FROM judgements").fetchone()
    assert tuple(row) == (1, "fail", "wrong_value", "Email differs", "gpt-6-luna")
    kinds = [r[0] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=1")]
    assert "judge:verdict" in kinds and "judge:error" not in kinds
    assert judge.latest(1)["bucket"] == "wrong_value"


@pytest.mark.asyncio
async def test_invalid_verdict_is_rejected(tmp_path):
    _seed(tmp_path)
    fake = _FakeClient({"verdict": "maybe", "bucket": "none", "notes": "", "fields": []})
    assert await judge.Judge(client=fake).judge(1, [], None, "") is None
    assert db.connect().execute("SELECT COUNT(*) FROM judgements").fetchone()[0] == 0
    assert db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='judge:error'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_api_failure_is_logged_not_raised(tmp_path):
    _seed(tmp_path)
    fake = _FakeClient({}, raise_exc=RuntimeError("boom"))
    assert await judge.Judge(client=fake).judge(1, [], None, "") is None
    assert db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='judge:error'").fetchone()[0] == 1


def test_schema_is_strict_and_closed():
    assert judge.JUDGE_SCHEMA["additionalProperties"] is False
    assert set(judge.JUDGE_SCHEMA["required"]) == set(judge.JUDGE_SCHEMA["properties"])
    assert judge.validate_verdict({"verdict": "pass", "bucket": "none", "notes": "ok", "fields": []})
    assert not judge.validate_verdict({"verdict": "pass", "bucket": "nope", "notes": "ok", "fields": []})
    assert not judge.validate_verdict({"verdict": "pass", "bucket": "none", "notes": "ok", "fields": [1]})


@pytest.mark.asyncio
async def test_model_outside_the_allowlist_is_refused_without_a_call(tmp_path):
    _seed(tmp_path)
    fake = _FakeClient({"verdict": "pass", "bucket": "none", "notes": "", "fields": []})
    assert await judge.Judge(client=fake, model="gpt-5.6-sol").judge(1, [], None, "") is None
    assert fake.calls == []
    assert db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='judge:error'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_worker_after_hold_judges_shadow_drafts_and_nothing_else(tmp_path, monkeypatch):
    from goat import worker
    _seed(tmp_path)
    calls = []

    class FakeJudge:
        def __init__(self, client=None, model=None):
            pass

        async def judge(self, job_id, decisions, png, text, gate=None, outline=""):
            calls.append((job_id, len(decisions), bool(png), gate))
            return {"verdict": "pass", "bucket": "none", "notes": "all good", "fields": []}

    import goat.judge as jm
    monkeypatch.setattr(jm, "Judge", FakeJudge)
    notified = []

    async def fake_notify(text, png=None):
        notified.append(text)

    monkeypatch.setattr(worker, "notify", fake_notify)

    class Ex:
        async def screenshot_png(self):
            return b"\x89PNG"

        async def page_text(self, n=0):
            return "Review"

    job = {"id": 1, "company": "Niantic", "title": "Intern"}
    ctx = SimpleNamespace(classifier=SimpleNamespace(client=object()), gate_result={"result": "pass_shadow"})
    out = await worker.after_hold(job, ctx, Ex(), "shadow_mode: gate passed; stopping before the final click")
    assert out["verdict"] == "pass" and calls == [(1, 1, True, {"result": "pass_shadow"})]
    assert db.connect().execute("SELECT detail FROM events WHERE kind='job:judged'").fetchone() is not None
    assert notified == []                                           # a shadow draft is announced by the Telegram worker, not here
    out = await worker.after_hold(job, ctx, Ex(), "answer_timeout: q.x")
    assert out is None and len(calls) == 1 and notified and notified[0].startswith("Held:")
    assert (SETTINGS.records_dir / "1").exists()


@pytest.mark.asyncio
async def test_a_resumed_draft_is_judged_from_the_archived_decisions(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(SETTINGS, "records_dir", tmp_path / "records")
    from goat import records
    d = records.job_dir(1)
    (d / "decisions_attempt1.md").write_text("| 1 | Email | a@x.com | standing:identity.email | 1 |", encoding="utf-8")
    (d / "decisions_attempt2.md").write_text("| 1 | Phone | 281 | standing:identity.phone | 2 |", encoding="utf-8")
    fake = _FakeClient({"verdict": "pass", "bucket": "none", "notes": "ok", "fields": []})
    await judge.Judge(client=fake).judge(1, [], None, "Review")
    log = next(c["text"] for c in fake.calls[0]["input"][0]["content"] if c.get("text", "").startswith("DECISION LOG"))
    assert "archived" in log and "Phone | 281" in log and "a@x.com" not in log
    assert "checked" in judge.JUDGE_INSTRUCTIONS and "archived" in judge.JUDGE_INSTRUCTIONS


@pytest.mark.asyncio
async def test_judge_sees_the_outline_with_box_values_and_long_log_values(tmp_path):
    _seed(tmp_path)
    records.record_decision(1, 1, "Why us", "x" * 300, "generated", "e5")
    fake = _FakeClient({"verdict": "pass", "bucket": "none", "notes": "ok", "fields": []})
    await judge.Judge(client=fake).judge(1, records.decisions(1), None, "Apply", outline='[e4] text "Email" value="a@x.com"')
    texts = [c["text"] for c in fake.calls[0]["input"][0]["content"] if c.get("type") == "input_text"]
    assert any(t.startswith("FINAL PAGE OUTLINE") and 'value="a@x.com"' in t for t in texts)
    assert any("x" * 300 in t for t in texts)



def test_a_value_the_judge_says_is_shown_but_is_not_on_the_page_is_a_phantom():
    """Northrop Grumman (2026-10-19): the Review page showed 'Aurora, CO'; the judge failed it for showing
    'Anywhere in the United States'."""
    from goat.judge import phantom_claims
    page = ("Are you willing to relocate?* Yes Which available location(s) on the posting are you open to relocating to? "
            "Aurora, CO Country United States of America")
    notes = "The intended relocation location is Aurora, CO, but the review page shows \u201cAnywhere in the United States.\u201d"
    assert phantom_claims(notes, page) == ["Anywhere in the United States."]
    assert phantom_claims("The page shows \u201cAurora, CO\u201d where the posting lists Palmdale.", page) == []
    assert phantom_claims('Willing to relocate shows "Yes" as intended.', page) == []
    assert phantom_claims("", page) == [] and phantom_claims(None, page) == []



def test_values_that_differ_only_in_case_are_found_in_the_notes():
    """Life Fitness (2026-10-19): 'The intended company value is "Southwest Airlines," but the review page shows
    "southwest airlines."'"""
    from goat.judge import case_only_claims
    notes = "The intended company value is \u201cSouthwest Airlines,\u201d but the review page shows \u201csouthwest airlines.\u201d"
    assert case_only_claims(notes) == [("Southwest Airlines", "southwest airlines")]
    assert case_only_claims("The page shows \u201cAurora, CO\u201d but the posting lists \u201cPalmdale, CA\u201d") == []



def test_fact_check_lets_silence_support_no_to_influencing_the_company():
    """General Motors (2026-10-20): the fact check rejected the default No to 'could you or an immediate family member
    influence GM decisions'."""
    from goat.judge import FACT_CHECK_INSTRUCTIONS
    assert "in a position to influence its decisions" in FACT_CHECK_INSTRUCTIONS



def test_fact_check_follows_the_forms_own_term_definitions():
    """Datadog (2026-10-20): the form defines Summer as April-July; May 2028 is Summer 2028 there."""
    from goat.judge import FACT_CHECK_INSTRUCTIONS
    assert "form's own definition of that term covers the month" in FACT_CHECK_INSTRUCTIONS



def test_long_logged_values_are_marked_when_cut():
    """Motorola (2026-10-20): a 300-character log entry made the page's full answer look 'longer than the logged value'."""
    from goat.judge import JUDGE_INSTRUCTIONS
    assert "cut for length" in JUDGE_INSTRUCTIONS



def test_fact_check_knows_his_desired_pay():
    """GDIT (2026-10-20): '$0 - $50,000' holds his $41,600 a year and was called unsupported."""
    from goat.judge import FACT_CHECK_INSTRUCTIONS
    assert "$41,600 a year" in FACT_CHECK_INSTRUCTIONS



def test_a_ticked_option_is_shown_to_the_judge_as_the_answer():
    """Mastercard (2026-10-20): the label was cut at 80 characters, losing '/ No', and 'checked' was read as Yes."""
    from goat.judge import Judge
    text = Judge._log_text([{"page": 11, "field_label": "Have you ever worked for Mastercard as an employee or provided services as a "
                             "contingent worker? / No", "value": "checked", "source": "bank:worked-here-before"}])
    assert "= 'No' (the option ticked" in text and "checked" not in text



def test_fact_check_accepts_his_stored_source():
    """Dimensional (2026-10-20): 'I first came across Dimensional through LinkedIn' was called unsupported."""
    from goat.judge import FACT_CHECK_INSTRUCTIONS
    assert "found or first saw the posting on LinkedIn" in FACT_CHECK_INSTRUCTIONS



def test_judge_accepts_catalog_wording_for_his_major():
    """Dimensional (2026-10-20): 'Computer and Information Science' was called wrong for Computer Science."""
    from goat.judge import JUDGE_INSTRUCTIONS
    assert "'Computer and Information Science'" in JUDGE_INSTRUCTIONS



def test_fact_check_knows_his_citizenship():
    """Tencent (2026-10-20): No to citizenship or residency in restricted countries was called unsupported."""
    from goat.judge import FACT_CHECK_INSTRUCTIONS
    assert "restricted or sanctioned countries, is supported" in FACT_CHECK_INSTRUCTIONS



def test_fact_check_default_no_covers_relationships_with_clients_and_investors():
    """Invesco (2026-10-20): No to a relationship with Invesco's investors, clients or government officials was flagged."""
    from goat.judge import FACT_CHECK_INSTRUCTIONS
    assert "relationship with its investors, clients" in FACT_CHECK_INSTRUCTIONS



def test_fact_check_default_no_covers_being_the_companys_client():
    """Invesco (2026-10-20): No to 'are you a current or prospective Invesco client' was flagged."""
    from goat.judge import FACT_CHECK_INSTRUCTIONS
    assert "being its own client, customer, account holder or investor" in FACT_CHECK_INSTRUCTIONS



def test_fact_check_knows_his_availability_window():
    """Inversion Space (2026-10-20): 'May 18, 2027 through August 14, 2027 (12 weeks)' was called an unsupported commitment."""
    from goat.judge import FACT_CHECK_INSTRUCTIONS
    assert "May 18, 2027 to August 14, 2027" in FACT_CHECK_INSTRUCTIONS



def test_judge_knows_the_how_did_you_hear_answer_is_ordered():
    """Verizon (2026-10-20): 'Job Board' (his second preference; no LinkedIn offered) was failed as a wrong value."""
    from goat.judge import JUDGE_INSTRUCTIONS
    assert "ordered preference (LinkedIn, then Job Board" in JUDGE_INSTRUCTIONS


def test_the_judge_knows_a_bundled_talent_community_choice_follows_his_rule():
    """Qualcomm (2026-10-21): the judge failed 'ONLY THE ROLE I AM APPLYING FOR' against keep_profile_future_roles."""
    from goat.judge import JUDGE_INSTRUCTIONS
    assert "never to join a talent community" in JUDGE_INSTRUCTIONS and "ONLY THE ROLE I AM APPLYING FOR" in JUDGE_INSTRUCTIONS


@pytest.mark.asyncio
async def test_decisions_for_controls_the_run_removed_are_left_out():
    """CenturyLink (2026-10-21): a SuccessFactors work row's 'Career Level' failed two checks after the row was removed."""
    from goat.judge import removed_decisions

    class Ex:
        async def eval(self, script, refs):
            return [r for r in refs if r in ("e17", "e40")]
    decs = [{"page": 1, "ref": "e17", "field_label": "First Name", "value": "Surya"},
            {"page": 1, "ref": "e68", "field_label": "Career Level", "value": "Professional/Salaried"},
            {"page": 1, "ref": "e62", "field_label": "Company Name", "value": "Revio"},
            {"page": 1, "ref": "e2", "field_label": "account login", "value": "(set)"},
            {"page": 0, "ref": "e5", "field_label": "Earlier page", "value": "x"}]
    gone = await removed_decisions(Ex(), decs, 1, "Revio appears in the page text")
    assert [d["field_label"] for d in gone] == ["Career Level"], gone
    assert await removed_decisions(Ex(), decs, None, "") == []



def test_the_fact_check_counts_board_seats_and_political_office_under_his_default_no():
    """Micron (2026-10-21): 'plans to join a for-profit board = No' failed the check as a generic default."""
    from goat.judge import FACT_CHECK_INSTRUCTIONS
    assert "board of directors" in FACT_CHECK_INSTRUCTIONS and "political, government or regulatory office" in FACT_CHECK_INSTRUCTIONS



def test_catalog_wordings_for_his_degree_and_major_are_named():
    """Micron (2026-10-21): 'Bachelor's Level Degree' and 'Computer sciences' failed as wrong option wording."""
    from goat.judge import JUDGE_INSTRUCTIONS
    assert "Bachelor's Level Degree" in JUDGE_INSTRUCTIONS and "Computer sciences" in JUDGE_INSTRUCTIONS



def test_a_later_attempt_replaces_an_earlier_answer_worded_differently():
    """Insomniac (2026-10-21): attempt 1 logged 'Comfortable working in large C++ codebase' from a judgment, attempt 2
    'Comfortable working in a large C++ codebase' from Surya's own answer; the fact check still saw the judgment."""
    from goat.records import same_question
    assert same_question("Comfortable working in large C++ codebase", "Comfortable working in a large C++ codebase")
    assert same_question("Built tool for non-technical users", "Built a tool for non-technical users")
    assert not same_question("Name", "Name if related")
    assert not same_question("Worked for Sony company", "If yes relationship name/details")



def test_the_judge_knows_his_year_in_a_portals_own_wording():
    """LSEG (2026-10-21): 'Penultimate academic year' was failed as not Sophomore."""
    from goat.judge import JUDGE_INSTRUCTIONS
    assert "'Penultimate academic year'" in JUDGE_INSTRUCTIONS and "Sophomore" in JUDGE_INSTRUCTIONS



def test_the_judge_knows_his_title_is_a_stored_answer():
    """AutoZone (2026-10-21): 'Mr' from identity.salutation was failed as unsourced; onsemi passed with the same."""
    from goat.judge import JUDGE_INSTRUCTIONS
    assert "identity.salutation" in JUDGE_INSTRUCTIONS and "'Mr'" in JUDGE_INSTRUCTIONS



def test_the_judge_gets_suryas_own_answers_to_this_applications_questions(tmp_path):
    """AutoZone (2026-10-21): he answered 'Add an end date of today' for his current Revio role and the judge, which never
    saw that answer, failed the end date against the profile."""
    from goat import db
    from goat.judge import surya_answers, JUDGE_INSTRUCTIONS
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('A1','AutoZone','IT Intern','https://x/1','oracle')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(7,'A1','filling','no_account',0)")
        con.execute("INSERT INTO pending_questions(job_id,key,question,options,field_type,asked_at,deadline_at,answered_at,answer) "
                    "VALUES(7,'q.x','Oracle requires an End Date for your current Revio role. How would you like to proceed?','[]','x',1,2,3,'Add an end date of today')")
        con.execute("INSERT INTO pending_questions(job_id,key,question,options,field_type,asked_at,deadline_at,answered_at,answer) "
                    "VALUES(7,'q.y','Unanswered?','[]','x',1,2,NULL,NULL)")
    got = surya_answers(7)
    assert "Add an end date of today" in got and "Unanswered" not in got, got
    assert surya_answers(8) == "(none)"
    assert "SURYA'S OWN ANSWERS" in JUDGE_INSTRUCTIONS



def test_the_judge_takes_na_and_n_slash_a_as_one_answer():
    """LLNL (2026-10-21): the box asked for 'NA', held 'N/A', and the judge failed it."""
    from goat.judge import JUDGE_INSTRUCTIONS
    assert "'N/A', 'NA'" in JUDGE_INSTRUCTIONS



def test_the_judge_knows_a_no_earlier_application_answer_is_supported():
    """Ecolab (2026-10-22): the judge failed 'participated in our recruitment process? No' twice."""
    from goat import judge
    text = " ".join(str(v) for v in vars(judge).values() if isinstance(v, str))
    assert "recruitment or hiring process" in text and "application ledger" in text


def test_a_clear_of_a_sibling_control_is_not_the_answer():
    """Ursa Major (2026-10-22): 'No' set on the refugee/asylum control (e31), then a sibling control with the same label
    (e30) cleared; read as the last decision, the clear made the judge fail a page that showed 'No'."""
    from goat.judge import Judge
    log = [{"page": 1, "field_label": "Refugee or Asylum status", "value": "No", "source": "standing:q.refugee", "ref": "e31"},
           {"page": 1, "field_label": "Refugee or Asylum status", "value": "", "source": "cleared", "ref": "e30"}]
    text = Judge._log_text(log)
    assert "= 'No'" in text and "cleared" not in text
    # a clear of the very control that held the value is a correction, and stays
    log[1]["ref"] = "e31"
    assert "source cleared" in Judge._log_text(log)
