"""Run the Workday-style fixture workflow against the REAL goat.db so the dashboard shows a complete
run: snapshot, adapter prefill, custom dropdowns, three pages, résumé upload, gate, shadow stop (or,
with --live, a real submit on the local fixture). The listing id is demo:workday-fixture; nothing is
written to the applications ledger. The model is replaced by the tool calls it would make.

  python scripts/demo_workday.py [--live]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from goat import db, records, states  # noqa: E402
from goat.adapters import for_portal  # noqa: E402
from goat.agent.tools import RunContext, ToolDispatcher  # noqa: E402
from goat.browser import snapshot  # noqa: E402
from goat.browser.executor import Executor, HostPolicy  # noqa: E402
from goat.budget import Budget, Parked  # noqa: E402
from goat.config import SETTINGS  # noqa: E402

FIXTURE = (ROOT / "tests" / "fixtures" / "workday.html").resolve().as_uri()
LISTING_ID = "demo:workday-fixture"


def _ref(snap, needle, kind=None):
    for i in snap.items:
        if needle.lower() in (i.get("label") or "").lower() and (kind is None or i.get("tag") == kind):
            return i["ref"]
    raise RuntimeError(f"no item {needle!r}")


def _radio(snap, legend, option):
    for g in snap.groups:
        if legend.lower() in (g.get("label") or "").lower():
            return next(o["ref"] for o in g["options"] if o["label"].strip().lower().startswith(option.lower()))
    raise RuntimeError(f"no group {legend!r}")


async def run_real_model(job: dict, job_id: int, ex: Executor, ctx: RunContext) -> str:
    """Let the configured executor model (allowlisted) drive the fixture, one conversation per page,
    exactly as the worker does. Returns the outcome string."""
    from goat.agent.loop import AgentLoop
    from goat.budget import BudgetExceeded

    adapter = for_portal("workday")
    snap = await snapshot.take(ex)
    ctx.last_snapshot = snap
    pre = await adapter.prefill(ex, snap, job, ctx.page_no)
    db.event(job_id, "adapter:prefill", {"set": pre.set, "failed": pre.failed, "skipped_self_id": pre.skipped_self_id})
    loop = AgentLoop(ctx)
    note = adapter.opening_note(1) + "\nThis is a SHADOW run on a local test form: fill everything and call submit; the gate stops before the click."
    pages = 0
    try:
        while True:
            pages += 1
            res = await loop.run_page([], note)
            records.save_screenshot(job_id, await ex.screenshot_png(), f"page{ctx.page_no}_{res.outcome}")
            if res.outcome == "page_done":
                note = f"You are now on page {ctx.page_no}. Call snapshot and fill this page."
                loop = AgentLoop(ctx, client=loop.client, advisor=loop.advisor)
                if pages > 8:
                    raise BudgetExceeded("budget_spent: more than 8 pages")
                continue
            if res.outcome == "submitted":
                with db.tx() as con:
                    con.execute("UPDATE jobs SET status=?, updated_at=?, finished_at=? WHERE id=?", (states.SUBMITTED, db.now(), db.now(), job_id))
                db.event(job_id, "job:submitted", {"reason": res.reason, "demo": True})
                return "submitted"
            if res.outcome == "error":
                with db.tx() as con:
                    con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=?, finished_at=? WHERE id=?", (states.HOLD, "error: " + res.reason, db.now(), db.now(), job_id))
                db.event(job_id, "job:hold", {"reason": res.reason, "demo": True})
                return "error: " + res.reason
            return res.outcome
    except Parked as p:
        st = states.READY_FOR_YOU if p.state == "ready_for_you" else states.HOLD
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=?, finished_at=? WHERE id=?", (st, p.reason, db.now(), db.now(), job_id))
        db.event(job_id, f"job:{st}", {"reason": p.reason, "demo": True})
        return f"parked: {p.reason}"
    except BudgetExceeded as b:
        with db.tx() as con:
            con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=?, finished_at=? WHERE id=?", (states.HOLD, b.reason, db.now(), db.now(), job_id))
        db.event(job_id, "job:budget", {"reason": b.reason, "demo": True})
        return f"budget: {b.reason}"


async def main(live: bool, real: bool = False) -> int:
    db.connect()
    with db.tx() as con:
        con.execute("INSERT OR IGNORE INTO listings(id,company,title,url,ats,variant,triage_status,first_seen) VALUES(?,?,?,?,?,?,?,?)",
                    (LISTING_ID, "Acme (demo fixture)", "Software Engineer Intern", FIXTURE, "workday", "swe", "demo", db.now()))
        row = con.execute("SELECT id FROM jobs WHERE listing_id=?", (LISTING_ID,)).fetchone()
        if row:
            job_id = row["id"]
            con.execute("UPDATE jobs SET status=?, hold_reason=NULL, claimed_at=?, updated_at=?, attempts=attempts+1, finished_at=NULL WHERE id=?",
                        (states.FILLING, db.now(), db.now(), job_id))
            con.execute("DELETE FROM decisions WHERE job_id=?", (job_id,))
        else:
            cur = con.execute("INSERT INTO jobs(listing_id,status,lane,priority,attempts,created_at,updated_at,claimed_at,resume_variant,apply_url) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?)", (LISTING_ID, states.FILLING, "account", -9999, 1, db.now(), db.now(), db.now(), "swe", FIXTURE))
            job_id = cur.lastrowid
    db.event(job_id, "job:claimed", {"company": "Acme (demo fixture)", "title": "Software Engineer Intern", "ats": "workday", "attempt": 1, "demo": True})
    job = dict(db.connect().execute("SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=?", (job_id,)).fetchone())
    job["resume_file"] = None
    if live:
        SETTINGS.mode, SETTINGS.live_portals = "live", ["workday"]
    else:
        SETTINGS.mode = "shadow"

    ex = Executor(policy=HostPolicy([FIXTURE]), job_id=job_id)
    await ex.start_launched(headless=True)
    await ex.new_page()
    ex.policy.hosts.add("")
    await ex.page.goto(FIXTURE)
    await ex.settle(quiet_ms=100)
    ctx = RunContext(job=job, ex=ex, budget=Budget(job_id, "workday"))
    d = ToolDispatcher(ctx)
    t0 = time.monotonic()
    outcome = "unknown"

    async def call(name, **args):
        out, _ = await d.dispatch(name, json.dumps(args))
        r = json.loads(out)
        db.event(job_id, "model:step", {"effort": "scripted", "tool": name}, latency_ms=0, model="scripted-demo",
                 tokens_in=0, tokens_cached=0, tokens_out=0, cost_usd=0.0)
        return r

    if real:
        try:
            records.save_screenshot(job_id, await ex.screenshot_png(), "opened")
            outcome = await run_real_model(job, job_id, ex, ctx)
        finally:
            with db.tx() as con:
                con.execute("UPDATE jobs SET wall_ms_used=?, steps_used=? WHERE id=?", (int((time.monotonic() - t0) * 1000), ctx.budget.steps, job_id))
            records.write_decisions_markdown(job_id, {"company": job["company"], "title": job["title"], "posting": job["url"], "portal": "workday",
                                                       "resume_attached": ctx.resume_file, "outcome": outcome, "gate": json.dumps(ctx.gate_result or {}),
                                                       "config_hash": SETTINGS.config_hash(), "model": SETTINGS.models.executor},
                                             ctx.free_text, ["Real-model run on the local Workday-style fixture."])
            await ex.close(close_browser=True)
        cost = db.connect().execute("SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM events WHERE job_id=? AND kind='model:step' AND ts>?", (job_id, int(time.time()) - int(time.monotonic() - t0) - 5)).fetchone()
        print(json.dumps({"job": job_id, "outcome": outcome, "gate": ctx.gate_result, "steps": ctx.budget.steps, "model_steps": cost[1],
                          "cost_usd": round(cost[0], 4), "decisions": len(records.decisions(job_id)), "records": str(SETTINGS.records_dir / str(job_id))}, indent=2))
        return 0

    try:
        records.save_screenshot(job_id, await ex.screenshot_png(), "opened")
        snap = await snapshot.take(ex)
        ctx.last_snapshot = snap
        pre = await for_portal("workday").prefill(ex, snap, job, 1)
        db.event(job_id, "adapter:prefill", {"set": pre.set, "failed": pre.failed, "skipped_self_id": pre.skipped_self_id})
        await call("snapshot", with_text=False)
        s = await snapshot.take(ex)
        await call("choose", ref=_ref(s, "How Did You Hear", "dropdown"), answer_key="how_did_you_hear.preferred_option", options=["Job Board"], label="How Did You Hear About Us?", source=None)
        await call("choose", ref=_ref(s, "Phone Device Type", "dropdown"), answer_key="identity.phone_device_type", options=[], label="Phone Device Type", source=None)
        records.save_screenshot(job_id, await ex.screenshot_png(), "page1_filled")
        await call("click", ref=_ref(s, "Save and Continue", "button"), label="Save and Continue")

        s = await snapshot.take(ex); ctx.last_snapshot = s
        await call("snapshot", with_text=False)
        await call("upload_resume", ref=_ref(s, "Resume"), document="resume")
        await call("fill_batch", fields=[{"ref": _ref(s, "Skills"), "answer_key": None, "value": "Python", "source": "resume", "label": "Skills"}])
        records.save_screenshot(job_id, await ex.screenshot_png(), "page2_filled")
        await call("click", ref=_ref(s, "Save and Continue", "button"), label="Save and Continue")

        s = await snapshot.take(ex); ctx.last_snapshot = s
        await call("snapshot", with_text=False)
        text = "I want this role because of the backend work; at Revio I built the conversion pipeline that has processed 200+ conversions."
        await call("guardcheck", text=text)
        await call("fill_batch", fields=[
            {"ref": _radio(s, "legally authorized", "Yes"), "answer_key": "work_authorization.authorized_to_work_in_us", "value": None, "source": None, "label": "Are you legally authorized to work in the United States?"},
            {"ref": _radio(s, "sponsorship", "No"), "answer_key": "work_authorization.requires_sponsorship_future", "value": None, "source": None, "label": "Will you require sponsorship?"},
            {"ref": _radio(s, "relatives", "No"), "answer_key": "background.relatives_at_this_company", "value": None, "source": None, "label": "Relatives employed by Acme?"},
            {"ref": _ref(s, "Why are you interested"), "answer_key": None, "value": text, "source": "generated", "label": "Why are you interested in this role?"},
        ])
        records.save_screenshot(job_id, await ex.screenshot_png(), "page3_filled")
        await call("click", ref=_ref(s, "Save and Continue", "button"), label="Save and Continue")

        s = await snapshot.take(ex); ctx.last_snapshot = s
        await call("snapshot", with_text=False)
        await call("click", ref=_ref(s, "Submit", "button"), label="Submit")   # refused by design; shows in history
        await call("fill_batch", fields=[{"ref": _ref(s, "certify"), "answer_key": "policy.may_tick_truthfulness_attestation", "value": None, "source": None, "label": "I certify that the information provided is true and complete"}])
        records.save_screenshot(job_id, await ex.screenshot_png(), "review")
        try:
            r = await call("submit", button_label=None)
            outcome = r.get("status", "unknown")
            if outcome == "submitted":
                set_to = states.SUBMITTED
                with db.tx() as con:
                    con.execute("UPDATE jobs SET status=?, updated_at=?, finished_at=? WHERE id=?", (set_to, db.now(), db.now(), job_id))
                db.event(job_id, "job:submitted", {"reason": r.get("signal"), "demo": True, "note": "local fixture; ledger not written"})
                records.save_screenshot(job_id, await ex.screenshot_png(), "submitted")
        except Parked as p:
            outcome = f"parked: {p.reason}"
            with db.tx() as con:
                con.execute("UPDATE jobs SET status=?, hold_reason=?, updated_at=?, finished_at=? WHERE id=?", (states.HOLD, p.reason, db.now(), db.now(), job_id))
            db.event(job_id, "job:hold", {"reason": p.reason, "demo": True})
    finally:
        with db.tx() as con:
            con.execute("UPDATE jobs SET wall_ms_used=?, steps_used=? WHERE id=?", (int((time.monotonic() - t0) * 1000), ctx.budget.steps, job_id))
        records.write_decisions_markdown(job_id, {"company": job["company"], "title": job["title"], "posting": job["url"], "portal": "workday",
                                                   "resume_attached": ctx.resume_file, "outcome": outcome, "gate": json.dumps(ctx.gate_result or {}),
                                                   "config_hash": SETTINGS.config_hash()}, ctx.free_text, ["Demo run on the local Workday-style fixture; the model was replaced by scripted tool calls."])
        await ex.close(close_browser=True)
    print(json.dumps({"job": job_id, "outcome": outcome, "gate": ctx.gate_result, "steps": ctx.budget.steps,
                      "decisions": len(records.decisions(job_id)), "records": str(SETTINGS.records_dir / str(job_id))}, indent=2))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="submit on the local fixture instead of stopping in shadow")
    ap.add_argument("--real", action="store_true", help="let the configured (allowlisted) model drive instead of scripted tool calls")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.live, a.real)))
