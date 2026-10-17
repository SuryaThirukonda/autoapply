"""Drive the Responses loop with a scripted fake OpenAI client against the fixture form: snapshot ->
fill_batch from answer keys -> submit in shadow mode -> Parked(shadow_mode). Exercises the tool
dispatcher, decisions recording, the gate and the loop plumbing without any network."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from goat import answers, db, records
from goat.agent.loop import AgentLoop
from goat.agent.tools import RunContext
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from goat.budget import Budget, Parked
from goat.config import SETTINGS

FIXTURE = (Path(__file__).parent / "fixtures" / "form.html").resolve().as_uri()


def _item(**kw):
    ns = SimpleNamespace(**kw)
    ns.model_dump = lambda exclude_none=True, _ns=ns: {k: v for k, v in vars(_ns).items() if k != "model_dump"}
    return ns


class FakeResponses:
    def __init__(self, script):
        self.script = script
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script.pop(0)
        out = step(kwargs) if callable(step) else step
        return SimpleNamespace(output=out, model="fake-model", usage=SimpleNamespace(input_tokens=100, output_tokens=10, input_tokens_details=SimpleNamespace(cached_tokens=50)))


class FakeClient:
    def __init__(self, script):
        self.responses = FakeResponses(script)


@pytest.fixture()
def seeded(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    answers.seed_from_config(SETTINGS.config_dir)
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','Acme','Software Engineering Intern',?, 'greenhouse')", (FIXTURE,))
        con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES('L1','filling','no_account',1,1)")
    return dict(db.connect().execute("SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id").fetchone())


@pytest.mark.asyncio
async def test_loop_fills_and_stops_in_shadow(seeded, monkeypatch):
    monkeypatch.setattr(SETTINGS, "mode", "shadow")
    ex = Executor(policy=HostPolicy([FIXTURE]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    ex.policy.hosts.add("")
    await ex.page.goto(FIXTURE)
    await ex.settle(quiet_ms=100)
    job = {**seeded, "resume_file": None}
    ctx = RunContext(job=job, ex=ex, budget=Budget(job["id"], "greenhouse"))
    captured = {}

    def step_snapshot(_kw):
        return [_item(type="function_call", name="snapshot", arguments=json.dumps({"with_text": False}), call_id="c1")]

    def step_fill(kw):
        out_text = next(i for i in kw["input"] if i.get("type") == "function_call_output" and i["call_id"] == "c1")["output"]
        outline = json.loads(out_text)["outline"]
        captured["outline"] = outline
        refs = {}
        for line in outline.splitlines():
            if line.startswith("["):
                ref = line[1:line.index("]")]
                if '"First Name' in line: refs["first"] = ref
                if '"Last Name' in line: refs["last"] = ref
                if '"Email' in line: refs["email"] = ref
                if '"Phone' in line: refs["phone"] = ref
        fields = [
            {"ref": refs["first"], "answer_key": "identity.first_name", "value": None, "source": None, "label": "First Name"},
            {"ref": refs["last"], "answer_key": "identity.last_name", "value": None, "source": None, "label": "Last Name"},
            {"ref": refs["email"], "answer_key": "identity.email", "value": None, "source": None, "label": "Email"},
            {"ref": refs["phone"], "answer_key": "identity.phone", "value": None, "source": None, "label": "Phone"},
        ]
        return [_item(type="function_call", name="fill_batch", arguments=json.dumps({"fields": fields}), call_id="c2")]

    def step_submit(kw):
        out = json.loads(next(i for i in kw["input"] if i.get("type") == "function_call_output" and i["call_id"] == "c2")["output"])
        captured["fill"] = out
        return [_item(type="function_call", name="submit", arguments=json.dumps({"button_label": None}), call_id="c3")]

    def step_after_blocked(kw):
        out = json.loads(next(i for i in kw["input"] if i.get("type") == "function_call_output" and i["call_id"] == "c3")["output"])
        captured["submit1"] = out
        # A soft park is bounced back with the gate's blockers instead of parking.
        return [_item(type="function_call", name="park", arguments=json.dumps({"reason": "no_standing_answer: Current location test"}), call_id="c4")]

    def step_after_bounce(kw):
        out = json.loads(next(i for i in kw["input"] if i.get("type") == "function_call_output" and i["call_id"] == "c4")["output"])
        captured["park1"] = out
        return [_item(type="function_call", name="park", arguments=json.dumps({"reason": "eligibility: test listing requires a completed degree"}), call_id="c5")]

    loop = AgentLoop(ctx, client=FakeClient([step_snapshot, step_fill, step_submit, step_after_blocked, step_after_bounce]))
    try:
        with pytest.raises(Parked) as pi:
            await loop.run_page([])
        assert pi.value.state == "hold" and pi.value.reason.startswith("eligibility")
        assert "not parked" in captured["park1"]["error"] and captured["park1"]["blockers"]
        assert captured["fill"]["summary"].startswith("4 set")
        assert captured["submit1"]["status"] == "blocked"           # required fields still empty -> gate blocked, no click
        assert any("empty_required" in r for r in captured["submit1"]["fix_these_then_call_submit_again"])
        assert await ex.eval("() => window.__submits") == 0
        decs = records.decisions(job["id"])
        assert {d["field_label"] for d in decs} >= {"First Name", "Last Name", "Email", "Phone"}
        assert all(d["source"].startswith("standing:") for d in decs)
        assert "SUBMIT-CONTROL" in captured["outline"]
        # every request carried store=False and the computer tool
        for call in loop.client.responses.calls:
            assert call["store"] is False and {"type": "computer"} in call["tools"] and call["parallel_tool_calls"] is False
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_literal_without_source_is_refused(seeded):
    ex = Executor(policy=HostPolicy([FIXTURE]))
    await ex.start_launched(headless=True)
    await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(FIXTURE); await ex.settle(quiet_ms=100)
    job = {**seeded, "resume_file": None}
    ctx = RunContext(job=job, ex=ex, budget=Budget(job["id"], "greenhouse"))
    from goat.agent.tools import ToolDispatcher
    snap = await snapshot.take(ex)
    ctx.last_snapshot = snap
    first = next(i for i in snap.items if "First Name" in (i.get("label") or ""))
    d = ToolDispatcher(ctx)
    out, _ = await d.dispatch("fill_batch", json.dumps({"fields": [{"ref": first["ref"], "answer_key": None, "value": "Bob", "source": None, "label": "First Name"}]}))
    res = json.loads(out)
    assert res["results"][0]["status"] == "failed" and "source" in res["results"][0]["error"]
    out2, _ = await d.dispatch("fill_batch", json.dumps({"fields": [{"ref": first["ref"], "answer_key": None, "value": "I published a paper on this.", "source": "generated", "label": "Why Acme"}]}))
    assert "guardrail" in json.loads(out2)["results"][0]["error"]
    await ex.close(close_browser=True)
