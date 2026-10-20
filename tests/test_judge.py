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
