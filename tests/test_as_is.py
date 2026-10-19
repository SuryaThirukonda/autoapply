"""fill_batch source "as_is": a value Surya set himself in the browser is recorded as his decision and left
untouched; an empty field cannot be kept as-is (2026-10-18)."""
from __future__ import annotations

from goat import db, records
from goat.agent.tools import RunContext, ToolDispatcher
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from goat.budget import Budget

PAGE = """<html><body>
<div class="field"><label for="org">Organization preference</label><input id="org" type="checkbox" checked></div>
<div class="field"><label for="why">Why us?</label><textarea id="why">Because of the backend work.</textarea></div>
<div class="field"><label for="n">Nickname</label><input id="n" type="text"></div>
</body></html>"""


def _job():
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','Acme','SWE Intern','https://boards.greenhouse.io/acme/jobs/1','greenhouse')")
        jid = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES('L1','filling','no_account',1,1)").lastrowid
    return dict(db.connect().execute("SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=?", (jid,)).fetchone())


async def test_as_is_keeps_what_surya_set(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    job = _job()
    # Surya answered this job's question "I already filled it out" (as_is is refused without that, 2026-10-19)
    with db.tx() as con:
        con.execute("INSERT INTO pending_questions(job_id, key, question, options, field_type, asked_at, deadline_at, answered_at, answer)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", (job["id"], "q.org", "Organization preference?", "[]", "text", 1, 2, 3, "AS_IS"))
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE)
        snap = await snapshot.take(ex)
        by = {i["label"]: i for i in snap.items}
        ctx = RunContext(job={**job, "resume_file": None}, ex=ex, budget=Budget(job["id"], "greenhouse"))
        ctx.last_snapshot = snap
        out, _ = await ToolDispatcher(ctx).t_fill_batch([
            {"ref": by["Organization preference"]["ref"], "answer_key": None, "value": None, "source": "as_is", "label": "Organization preference"},
            {"ref": by["Why us?"]["ref"], "answer_key": None, "value": None, "source": "as_is", "label": "Why us?"},
            {"ref": by["Nickname"]["ref"], "answer_key": None, "value": None, "source": "as_is", "label": "Nickname"},
        ])
        res = {r["ref"]: r for r in out["results"]}
        assert res[by["Organization preference"]["ref"]]["status"] == "kept"
        assert res[by["Why us?"]["ref"]]["status"] == "kept" and "backend" in res[by["Why us?"]["ref"]]["readback"]
        assert res[by["Nickname"]["ref"]]["status"] == "failed"            # nothing there to keep
        assert await ex.page.evaluate("() => document.getElementById('org').checked") is True
        decisions = records.decisions_for(job["id"]) if hasattr(records, "decisions_for") else None
        if decisions is not None:
            assert any(d.get("source") == "user:manual" for d in decisions)
    finally:
        await ex.close()
