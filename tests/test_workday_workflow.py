"""One full workflow on a Workday-style multi-page form (button dropdowns, rail, Save and Continue,
Review page whose primary button becomes Submit). The model is replaced by the sequence of tool
calls it would make; everything else (adapter prefill, choose, fill, upload, click, gate, submit,
decisions, shadow/live) is the real code path."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from goat import answers, db, records
from goat.adapters import for_portal
from goat.agent.loop import AgentLoop
from goat.agent.tools import RunContext, ToolDispatcher
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from goat.budget import Budget, Parked
from goat.config import SETTINGS

FIXTURE = (Path(__file__).parent / "fixtures" / "workday.html").resolve().as_uri()


@pytest.fixture()
def seeded(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    answers.seed_from_config(SETTINGS.config_dir)
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('W1','Acme','Software Engineer Intern',?, 'workday')", (FIXTURE,))
        con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at,resume_variant) VALUES('W1','filling','account',1,1,'swe')")
    return dict(db.connect().execute("SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id").fetchone())


async def _open():
    ex = Executor(policy=HostPolicy([FIXTURE]))
    await ex.start_launched(headless=True)
    await ex.new_page()
    ex.policy.hosts.add("")
    await ex.page.goto(FIXTURE)
    await ex.settle(quiet_ms=100)
    return ex


def _ref(snap, needle, kind=None):
    for i in snap.items:
        if needle.lower() in (i.get("label") or "").lower() and (kind is None or i.get("tag") == kind):
            return i["ref"]
    raise AssertionError(f"no item labelled {needle!r}; have {[i.get('label') for i in snap.items]}")


def _radio(snap, legend_needle, option):
    for g in snap.groups:
        if legend_needle.lower() in (g.get("label") or "").lower():
            return next(o["ref"] for o in g["options"] if o["label"].strip().lower().startswith(option.lower()))
    raise AssertionError(f"no group {legend_needle!r}; have {[g.get('label') for g in snap.groups]}")


async def _call(d, name, **args):
    out, _ = await d.dispatch(name, json.dumps(args))
    return json.loads(out)


@pytest.mark.asyncio
async def test_workday_workflow_shadow_then_live(seeded, monkeypatch):
    monkeypatch.setattr(SETTINGS, "mode", "shadow")
    monkeypatch.setattr(SETTINGS, "live_portals", [])
    monkeypatch.setattr(SETTINGS, "resume_dir", Path(__file__).parent / "fixtures")
    (Path(__file__).parent / "fixtures" / "Surya_Thirukonda_Resume_SWE.pdf").write_bytes(b"%PDF-1.4 test")
    (Path(__file__).parent / "fixtures" / "Surya_Thirukonda_Resume_SWE.txt").write_text("Technical Skills: Python, Java, JavaScript, C++", encoding="utf-8")
    import goat.agent.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_RESUME_TEXT", None)
    ex = await _open()
    job = {**seeded, "resume_file": None}
    ctx = RunContext(job=job, ex=ex, budget=Budget(job["id"], "workday"))
    d = ToolDispatcher(ctx)
    adapter = for_portal("workday")
    try:
        # ---- page 1: My Information. Deterministic prefill first (what the worker does), then the model's tools.
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        outline = snapshot.render(snap)
        assert 'dropdown "Country' in outline and 'dropdown "State' in outline and "Phone Device Type" in outline
        assert "My Information" in outline
        pre = await adapter.prefill(ex, snap, job, 1)
        assert pre.set >= 6, pre.details           # names, address, city, zip, email, phone (+ dropdowns)
        snap = (await _call(d, "snapshot", with_text=False))["outline"]
        assert 'value="Surya"' in snap and 'value="Sugar Land"' in snap
        # The honeypot "Enter website" field must be untouched.
        assert await ex.eval("() => document.getElementById('web').value") == ""
        s = await snapshot.take(ex)
        # dropdowns the prefill did not know: How Did You Hear (Job Board), Phone Device Type (Mobile)
        r = await _call(d, "choose", ref=_ref(s, "How Did You Hear", "dropdown"), answer_key="how_did_you_hear.preferred_option",
                        options=["Job Board"], label="How Did You Hear About Us?", source=None)
        assert r["status"] == "set", r
        r = await _call(d, "choose", ref=_ref(s, "Phone Device Type", "dropdown"), answer_key="identity.phone_device_type",
                        options=[], label="Phone Device Type", source=None)
        assert r["status"] == "set" and r["readback"] == "Mobile", r
        country = next(i for i in s.items if (i.get("label") or "").startswith("Country"))
        state = next(i for i in s.items if (i.get("label") or "").startswith("State"))
        assert country.get("value") == "United States of America" and state.get("value") == "Texas"
        r = await _call(d, "click", ref=_ref(s, "Save and Continue", "button"), label="Save and Continue")
        assert ctx.page_no == 2 and "page2" in r["url"]

        # ---- page 2: My Experience
        s = await snapshot.take(ex); ctx.last_snapshot = s
        assert "My Experience" in snapshot.render(s)
        r = await _call(d, "upload_resume", ref=_ref(s, "Resume"), document="resume")
        assert r["status"] == "set" and r["readback"] == "Surya_Thirukonda_Resume_SWE.pdf", r
        r = await _call(d, "fill_batch", fields=[{"ref": _ref(s, "Skills"), "answer_key": None, "value": "Python", "source": "resume", "label": "Skills"}])
        assert r["summary"].startswith("1 set")
        await _call(d, "click", ref=_ref(s, "Save and Continue", "button"), label="Save and Continue")
        assert ctx.page_no == 3

        # ---- page 3: Application Questions (standing answers via keys; generated text via guardcheck)
        s = await snapshot.take(ex); ctx.last_snapshot = s
        gc = await _call(d, "guardcheck", text="I want this role because of the backend work; at Revio I built the conversion pipeline that has processed 200+ conversions.")
        assert gc["result"] in ("OK", "WARN")
        r = await _call(d, "fill_batch", fields=[
            {"ref": _radio(s, "legally authorized", "Yes"), "answer_key": "work_authorization.authorized_to_work_in_us", "value": None, "source": None, "label": "Are you legally authorized to work in the United States?"},
            {"ref": _radio(s, "sponsorship", "No"), "answer_key": "work_authorization.requires_sponsorship_future", "value": None, "source": None, "label": "Will you require sponsorship?"},
            {"ref": _radio(s, "relatives", "No"), "answer_key": "background.relatives_at_this_company", "value": None, "source": None, "label": "Relatives employed by Acme?"},
            {"ref": _ref(s, "Why are you interested"), "answer_key": None, "value": "I want this role because of the backend work; at Revio I built the conversion pipeline that has processed 200+ conversions.", "source": "generated", "label": "Why are you interested in this role?"},
        ])
        assert r["summary"].startswith("4 set"), r
        await _call(d, "click", ref=_ref(s, "Save and Continue", "button"), label="Save and Continue")
        assert ctx.page_no == 4

        # ---- page 4: Review. The primary button now says Submit: the click tool must refuse it.
        s = await snapshot.take(ex); ctx.last_snapshot = s
        out = snapshot.render(s)
        assert "| Review" in out and "SUBMIT-CONTROL" in out
        r = await _call(d, "click", ref=_ref(s, "Submit", "button"), label="Submit")
        assert "error" in r and "submit" in r["error"].lower()
        # Attestation only from a standing/policy answer: a literal is refused by the gate later, so use the policy key.
        r = await _call(d, "fill_batch", fields=[{"ref": _ref(s, "certify"), "answer_key": "policy.may_tick_truthfulness_attestation", "value": None, "source": None, "label": "I certify that the information provided is true and complete"}])
        assert r["summary"].startswith("1 set"), r

        # Shadow mode: the gate passes and the run parks BEFORE the click.
        with pytest.raises(Parked) as pi:
            await _call(d, "submit", button_label=None)
        assert pi.value.reason.startswith("shadow_mode")
        assert ctx.gate_result["result"] == "pass_shadow", ctx.gate_result
        assert await ex.eval("() => window.__submits") == 0

        # Released by Surya for this one job (goat release N): still shadow mode globally, yet the real submit
        # path clicks once and gets a positive signal.
        ctx.job["cleared_live"] = 1
        r = await _call(d, "submit", button_label=None)
        assert r["status"] == "submitted", r
        assert "confirmation" in r["signal"] or "url_changed" in r["signal"]
        assert await ex.eval("() => window.__submits") == 1
        assert ctx.gate_result["result"] == "pass_live"
        live_ev = db.connect().execute("SELECT detail FROM events WHERE kind='gate:pass_live' ORDER BY n DESC LIMIT 1").fetchone()[0]
        assert '"cleared_live": true' in live_ev

        decs = records.decisions(job["id"])
        labels = {d_["field_label"].rstrip(" *") for d_ in decs}
        assert {"First Name", "Last Name", "Email Address", "Phone Device Type", "resume upload"} <= labels
        assert all(d_["source"] for d_ in decs)
        srcs = {d_["field_label"].rstrip(" *"): d_["source"] for d_ in decs}
        assert srcs["Why are you interested in this role?"] == "generated"
        assert srcs["I certify that the information provided is true and complete"].startswith("standing:policy.")
        p = records.write_decisions_markdown(job["id"], {"company": "Acme", "title": "Software Engineer Intern"}, ctx.free_text, [])
        assert "Phone Device Type" in p.read_text(encoding="utf-8")
    finally:
        await ex.close(close_browser=True)


def _item(**kw):
    ns = SimpleNamespace(**kw)
    ns.model_dump = lambda exclude_none=True, _ns=ns: {k: v for k, v in vars(_ns).items() if k != "model_dump"}
    return ns


class _FakeClient:
    def __init__(self, script):
        self.script = script
        self.responses = self

    async def create(self, **kwargs):
        step = self.script.pop(0)
        return SimpleNamespace(output=step(kwargs) if callable(step) else step, model="fake",
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, input_tokens_details=SimpleNamespace(cached_tokens=0)))


@pytest.mark.asyncio
async def test_loop_returns_page_done_on_save_and_continue(seeded):
    """The worker starts a fresh conversation per form page: a Save and Continue click ends the loop with page_done."""
    ex = await _open()
    job = {**seeded, "resume_file": None}
    ctx = RunContext(job=job, ex=ex, budget=Budget(job["id"], "workday"))
    try:
        # prefill the page so Save and Continue passes the fixture's validation
        snap = await snapshot.take(ex)
        await for_portal("workday").prefill(ex, snap, job, 1)
        from goat.browser import fill
        for lbl, want in (("How Did You Hear", ["Job Board"]), ("Phone Device Type", ["Mobile"])):
            await fill.choose(ex, _ref(snap, lbl, "dropdown"), want)

        def s1(_kw):
            return [_item(type="function_call", name="snapshot", arguments=json.dumps({"with_text": False}), call_id="a")]

        def s2(kw):
            outline = json.loads(next(i for i in kw["input"] if i.get("type") == "function_call_output")["output"])["outline"]
            ref = next(l[1:l.index("]")] for l in outline.splitlines() if 'button "Save and Continue"' in l)
            return [_item(type="function_call", name="click", arguments=json.dumps({"ref": ref, "label": "Save and Continue"}), call_id="b")]

        loop = AgentLoop(ctx, client=_FakeClient([s1, s2]))
        res = await loop.run_page([])
        assert res.outcome == "page_done" and ctx.page_no == 2
        assert "page2" in ex.url
    finally:
        await ex.close(close_browser=True)
