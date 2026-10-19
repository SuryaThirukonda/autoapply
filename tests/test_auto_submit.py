"""Verified auto-submit (Surya 2026-10-18): a draft whose gate passed is submitted at once only when the read-back,
the question log, the judge and its fact check of every unconfirmed answer all agree; otherwise it holds as
review_needed for his OK on Telegram. iCIMS is never claimed."""
import json
from types import SimpleNamespace

import pytest

from goat import answers, db, records, states
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from goat.budget import Parked
from goat.config import SETTINGS
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

PAGE = """<!doctype html><html><head><style>.hidden{display:none}</style></head><body style="margin:20px">
<h1>Software Engineering Intern</h1>
<form id="app" onsubmit="return window.__onSubmit(event)">
  <div class="field"><label for="fn">First Name *</label><input id="fn" name="first" required></div>
  <div class="field"><label for="why">Why Acme?</label><textarea id="why" name="why"></textarea></div>
  <button type="submit" id="submit">Submit Application</button>
</form>
<div id="done" class="hidden"><h2>Application submitted!</h2><p>Thank you for applying to Acme.</p></div>
<script>
  window.__submits = 0;
  window.__onSubmit = function(e){ e.preventDefault(); window.__submits++;
    document.getElementById('app').classList.add('hidden'); document.getElementById('done').classList.remove('hidden'); return false; };
</script></body></html>"""

ESSAY = "At Revio I built the manuscript-formatting platform that has run 200+ conversions, and Acme's backend work is the same kind of problem."


class FakeJudge:
    """Stands in for goat.judge.Judge: returns `canned` and records what it was given."""
    canned: dict = {}
    calls: list = []

    def __init__(self, client=None, model=None):
        pass

    async def judge(self, job_id, decisions, png, text, gate=None, outline="", *, profile="", unconfirmed=None):
        FakeJudge.calls.append({"job_id": job_id, "profile": profile, "unconfirmed": unconfirmed, "gate": gate})
        return dict(FakeJudge.canned)


@pytest.fixture()
def judged(monkeypatch, tmp_path):
    import goat.judge as jm
    monkeypatch.setattr(jm, "Judge", FakeJudge)
    monkeypatch.setattr(SETTINGS, "records_dir", tmp_path / "records")
    monkeypatch.setattr(SETTINGS, "mode", "shadow")
    monkeypatch.setattr(SETTINGS, "live_portals", [])
    FakeJudge.calls = []
    FakeJudge.canned = {"verdict": "pass", "bucket": "none", "notes": "all required fields hold the intended values",
                        "fields": [], "unsupported": []}
    return FakeJudge


async def _filled(tmp_path, job, *, essay=True):
    """The one-page form, with First Name from a questionnaire answer and (optionally) an essay the agent wrote."""
    answers.save_answer("identity.first_name", "Surya", answer_type="text", source="questionnaire")
    page = tmp_path / "apply.html"
    page.write_text(PAGE, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
    d, ctx = _dispatcher(job)
    ctx.ex = ex
    snap = await snapshot.take(ex)
    ctx.last_snapshot = snap
    ref = lambda start: next(i["ref"] for i in snap.items if (i.get("label") or "").startswith(start))  # noqa: E731
    fields = [{"ref": ref("First Name"), "answer_key": "identity.first_name", "value": None, "source": None, "label": "First Name *"}]
    if essay:
        await d.dispatch("guardcheck", json.dumps({"text": ESSAY}))
        fields.append({"ref": ref("Why Acme"), "answer_key": None, "value": ESSAY, "source": "generated", "label": "Why Acme?"})
    out, _ = await d.t_fill_batch(fields)
    assert all(r["status"] == "set" for r in out["results"]), out
    return ex, d, ctx


async def _submits(ex) -> int:
    return await ex.page.evaluate("() => window.__submits || 0")


def _events(kind: str) -> list[dict]:
    return [json.loads(r[0] or "{}") for r in db.connect().execute("SELECT detail FROM events WHERE kind=? ORDER BY n", (kind,))]


# ------------------------------------------------------------------------------------------------ settings
def test_auto_submit_is_on_only_when_set_to_verified(monkeypatch):
    for value, on in (("verified", True), ("VERIFIED ", True), ("off", False), ("", False), ("yes", False)):
        monkeypatch.setattr(SETTINGS, "auto_submit", value)
        assert SETTINGS.auto_submit_on() is on, value


def test_the_suite_starts_with_auto_submit_off():
    assert SETTINGS.auto_submit_on() is False


# ------------------------------------------------------------------------------------- unconfirmed answers
def test_unconfirmed_source_rules(store):
    answers.save_answer("identity.first_name", "Surya", answer_type="text", source="questionnaire")
    answers.save_answer("q.onsite_55_hours", "Yes", answer_type="text", source="derived",
                        note="evidence: availability.max_hours_per_week = 60; in-office 5 days = Yes")
    answers.save_answer("q.prior_lyft", "No", answer_type="text", source="default_no", note="standing rule: No unless the profile names Lyft")
    assert answers.unconfirmed_source("standing:identity.first_name") == (False, "")
    flag, ev = answers.unconfirmed_source("standing:q.onsite_55_hours")
    assert flag and "max_hours_per_week = 60" in ev
    assert answers.unconfirmed_source("standing:q.prior_lyft")[0] is True
    assert answers.unconfirmed_source("standing:no.such.key") == (True, "")
    for confirmed in ("bank:hear-about", "policy:may_create_accounts", "resume", "adapter", "cleared"):
        assert answers.unconfirmed_source(confirmed) == (False, ""), confirmed
    for unconfirmed in ("generated", "judgment", "user:manual", ""):
        assert answers.unconfirmed_source(unconfirmed)[0] is True, unconfirmed


def test_answer_set_merges_archived_attempts_and_a_newer_pick_replaces_the_old_one(store, tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "records_dir", tmp_path / "records")
    job = _job()
    d = records.job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "decisions_attempt1.md").write_text("\n".join([
        "# Decisions archived from attempt 1", "", "| Page | Field label | Value | Source | ts |", "|---|---|---|---|---|",
        "| 1 | First Name * | Surya | standing:identity.first_name | 1 |",
        "| 2 | Previously employed? / Yes | checked | judgment | 2 |",
        "| 2 | Why Acme? | An essay from attempt one | generated | 3 |",
    ]), encoding="utf-8")
    records.record_decision(job["id"], 1, "Previously employed? / No", "checked", "standing:background.previously_employed", "e5")
    got = {a["field_label"]: a for a in records.answer_set(job["id"])}
    assert set(got) == {"First Name *", "Why Acme?", "Previously employed? / No"}
    assert got["Why Acme?"]["archived"] is True and got["Previously employed? / No"]["archived"] is False
    answers.save_answer("identity.first_name", "Surya", answer_type="text", source="questionnaire")
    un = records.unconfirmed_answers(job["id"], job["company"], job["ats"])
    labels = [u["label"] for u in un]
    assert "Why Acme?" in labels and "First Name *" not in labels


# ------------------------------------------------------------------------------------------- the fact check
class _FakeClient:
    def __init__(self, canned):
        self.canned, self.kw = canned, None
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.kw = kw
        return SimpleNamespace(output_text=json.dumps(self.canned), usage=SimpleNamespace(input_tokens=10, output_tokens=5), model=kw["model"])


@pytest.mark.asyncio
async def test_the_fact_check_sends_the_profile_and_the_unconfirmed_answers(store):
    from goat.judge import JUDGE_SCHEMA, Judge
    assert "unsupported" in JUDGE_SCHEMA["required"] and JUDGE_SCHEMA["additionalProperties"] is False
    job = _job()
    canned = {"verdict": "pass", "bucket": "none", "notes": "ok", "fields": [], "unsupported": ["Why Acme?: claims 5 years of Go"]}
    client = _FakeClient(canned)
    out = await Judge(client=client).judge(job["id"], [], None, "page", {"result": "pass_shadow"}, profile="PROFILE: Revio, DeKalb",
                                           unconfirmed=[{"label": "Why Acme?", "value": "I have 5 years of Go", "source": "generated", "evidence": ""}])
    assert out["unsupported"] == ["Why Acme?: claims 5 years of Go"]
    texts = [c["text"] for c in client.kw["input"][0]["content"] if c["type"] == "input_text"]
    assert any(t.startswith("APPLICANT PROFILE\nPROFILE: Revio, DeKalb") for t in texts)
    assert any(t.startswith("UNCONFIRMED ANSWERS\n1. Why Acme? = 'I have 5 years of Go'") for t in texts)
    assert "FACT CHECK" in client.kw["instructions"] and client.kw["model"] == "gpt-6-luna" and client.kw["store"] is False
    notes = db.connect().execute("SELECT notes FROM judgements ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "unsupported: Why Acme?" in notes
    # without unconfirmed answers the plain judge runs: no profile, no fact-check instructions
    client2 = _FakeClient({**canned, "unsupported": []})
    await Judge(client=client2).judge(job["id"], [], None, "page")
    assert "FACT CHECK" not in client2.kw["instructions"]
    assert not any(c.get("text", "").startswith("APPLICANT PROFILE") for c in client2.kw["input"][0]["content"])


def test_a_verdict_without_the_fact_check_list_still_validates_and_a_bad_list_does_not():
    from goat.judge import validate_verdict
    base = {"verdict": "pass", "bucket": "none", "notes": "ok", "fields": []}
    assert validate_verdict(base) and validate_verdict({**base, "unsupported": ["x"]})
    assert not validate_verdict({**base, "unsupported": "x"}) and not validate_verdict({**base, "unsupported": [1]})


# ------------------------------------------------------------------------------------------- submit outcomes
@pytest.mark.asyncio
async def test_a_verified_draft_is_submitted_at_once(store, judged, tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "auto_submit", "verified")
    job = _job()
    ex, d, ctx = await _filled(tmp_path, job)
    try:
        out, _ = await d.t_submit(None)
        assert out["status"] == "submitted", out
        assert await _submits(ex) == 1 and ctx.auto_submitted and ctx.judged
        call = judged.calls[-1]
        assert call["profile"] is not None and [u["label"] for u in call["unconfirmed"]] == ["Why Acme?"]
        assert _events("auto_submit:verified") and _events("verify:pass")[0]["unconfirmed"] == 1
        assert ctx.gate_result["result"] == "pass_shadow"
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_judge_fail_holds_for_review_and_never_clicks(store, judged, tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "auto_submit", "verified")
    judged.canned = {**judged.canned, "verdict": "fail", "bucket": "missing_required", "notes": "Last Name is blank", "fields": ["Last Name"]}
    job = _job()
    ex, d, ctx = await _filled(tmp_path, job)
    try:
        with pytest.raises(Parked) as pi:
            await d.t_submit(None)
        assert pi.value.reason.startswith("review_needed: judge: fail (missing_required): Last Name is blank")
        assert pi.value.state == "hold" and await _submits(ex) == 0 and not ctx.auto_submitted
        assert ctx.verdict["verdict"] == "fail" and _events("verify:fail")
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_an_answer_the_profile_does_not_back_holds_for_review(store, judged, tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "auto_submit", "verified")
    judged.canned = {**judged.canned, "unsupported": ["Why Acme?: the profile has no 200+ conversions"]}
    job = _job()
    ex, d, ctx = await _filled(tmp_path, job)
    try:
        with pytest.raises(Parked) as pi:
            await d.t_submit(None)
        assert "not backed by the profile: Why Acme?" in pi.value.reason and await _submits(ex) == 0
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_page_left_with_a_mismatch_or_an_unanswered_question_holds_for_review(store, judged, tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "auto_submit", "verified")
    job = _job()
    ex, d, ctx = await _filled(tmp_path, job, essay=False)
    try:
        ctx.readback_open = {1: ["field School [e7] shows '' but decision was 'Texas A&M University'"]}
        ctx.asked_keys = ["q.what_is_your_favourite_editor"]
        with pytest.raises(Parked) as pi:
            await d.t_submit(None)
        r = pi.value.reason
        assert r.startswith("review_needed: read-back: page 1: field School") and "unanswered question(s): q.what_is_your_favourite_editor" in r
        assert await _submits(ex) == 0
        assert judged.calls, "the judge still runs, so the Telegram message carries its verdict"
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_auto_submit_off_or_a_shadow_only_run_keeps_the_plain_shadow_hold(store, judged, tmp_path, monkeypatch):
    job = _job()
    ex, d, ctx = await _filled(tmp_path, job, essay=False)
    try:
        with pytest.raises(Parked) as pi:                       # AUTO_SUBMIT off (the suite default)
            await d.t_submit(None)
        assert pi.value.reason.startswith("shadow_mode") and judged.calls == [] and not ctx.judged
        monkeypatch.setattr(SETTINGS, "auto_submit", "verified")
        ctx.shadow_only = True                                  # goat run-once --shadow
        with pytest.raises(Parked) as pi:
            await d.t_submit(None)
        assert pi.value.reason.startswith("shadow_mode") and judged.calls == [] and await _submits(ex) == 0
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_a_released_job_submits_without_the_check(store, judged, tmp_path, monkeypatch):
    """Surya's own release (cleared_live) is his OK: the gate passes live and no verification runs."""
    monkeypatch.setattr(SETTINGS, "auto_submit", "verified")
    job = {**_job(), "cleared_live": 1}
    ex, d, ctx = await _filled(tmp_path, job, essay=False)
    try:
        out, _ = await d.t_submit(None)
        assert out["status"] == "submitted" and ctx.gate_result["result"] == "pass_live"
        assert judged.calls == [] and not ctx.auto_submitted and await _submits(ex) == 1
    finally:
        await ex.close(close_browser=True)


def test_the_submit_tool_has_room_for_the_judge(store):
    d, _ = _dispatcher(_job())
    assert d.tool_timeout_s("submit") >= 400 and d.tool_timeout_s("click") == d.TOOL_TIMEOUT_S


# ------------------------------------------------------------------------------------------- worker events
@pytest.mark.asyncio
async def test_a_review_needed_hold_carries_the_submit_checks_verdict_without_judging_again(store, monkeypatch):
    from goat import worker
    job = _job()
    called = []

    async def fake_after_hold(job, ctx, ex, reason):
        called.append(reason)
        return {"verdict": "pass", "bucket": "none", "notes": "fresh", "fields": [], "unsupported": []}

    monkeypatch.setattr(worker, "after_hold", fake_after_hold)
    verdict = {"verdict": "fail", "bucket": "wrong_value", "notes": "School shows UT", "fields": ["School"], "unsupported": ["Why: 5 years of Go"]}
    ctx = SimpleNamespace(judged=True, verdict=verdict)
    extra = await worker.hold_extra(job, ctx, None, "review_needed: judge: fail (wrong_value): School shows UT")
    assert called == [] and extra["judge"]["verdict"] == "fail" and extra["judge"]["unsupported"] == ["Why: 5 years of Go"]
    assert "inferred" in extra
    extra = await worker.hold_extra(job, SimpleNamespace(judged=False, verdict=None), None, "shadow_mode: gate passed; stopping before the final click")
    assert called and extra["judge"]["notes"] == "fresh"
    assert await worker.hold_extra(job, ctx, None, "portal_error: stalled") is None


def test_an_automatic_submit_says_so_in_its_event(store):
    from goat import worker
    job = _job()
    ctx = SimpleNamespace(auto_submitted=True, verdict={"verdict": "pass", "bucket": "none", "notes": "all good", "fields": [], "unsupported": []})
    extra = worker.submitted_extra(job["id"], ctx)
    assert extra["auto"] is True and extra["judge"]["verdict"] == "pass"
    assert "auto" not in worker.submitted_extra(job["id"], SimpleNamespace(auto_submitted=False))


# ------------------------------------------------------------------------------------------------- iCIMS
def test_claim_next_never_takes_a_skipped_portal(store, monkeypatch):
    from goat import worker
    monkeypatch.setattr(SETTINGS, "lanes_enabled", ["no_account", "account"])
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('I1','Peraton','Intern','https://careers-peraton.icims.com/jobs/1','icims')")
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('G1','Lyft','Intern','https://boards.greenhouse.io/lyft/jobs/1','greenhouse')")
        con.execute("INSERT INTO jobs(listing_id,status,lane,priority,created_at,updated_at) VALUES('I1',?,'account',100,1,1)", (states.QUEUED,))
        con.execute("INSERT INTO jobs(listing_id,status,lane,priority,created_at,updated_at) VALUES('G1',?,'no_account',0,1,1)", (states.QUEUED,))
    monkeypatch.setattr(SETTINGS, "skip_ats", ["icims"])
    got = worker.claim_next()
    assert got["ats"] == "greenhouse"
    assert worker.claim_next() is None                          # the iCIMS job stays queued, never claimed
    monkeypatch.setattr(SETTINGS, "skip_ats", [])
    assert worker.claim_next()["ats"] == "icims"


def test_icims_is_skipped_by_default():
    from goat.config import Settings
    assert "icims" in Settings().skip_ats
