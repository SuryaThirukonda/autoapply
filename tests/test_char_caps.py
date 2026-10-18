"""Short-answer caps (Greenhouse 255, SuccessFactors 200): the outline shows each box's limit (maxlength or a
"Maximum N characters" / "0 / N" hint), and fill_batch refuses text that would not fit instead of letting
the portal cut it off."""
from __future__ import annotations

from goat import db
from goat.agent.tools import RunContext, ToolDispatcher
from goat.browser import snapshot
from goat.browser.executor import Executor, HostPolicy
from goat.budget import Budget

PAGE = """<html><body>
<div class="field"><label for="a">Why this team?</label><input id="a" type="text" maxlength="255" style="width:400px"></div>
<div class="field"><label for="b">Describe a project</label><textarea id="b"></textarea><small>Maximum 200 characters</small></div>
<div class="field"><label for="c">Anything else</label><textarea id="c"></textarea><span>0 / 150</span></div>
<div class="field"><label for="d">Cover note</label><textarea id="d"></textarea></div>
</body></html>"""


def _job():
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','Acme','SWE Intern','https://boards.greenhouse.io/acme/jobs/1','greenhouse')")
        jid = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES('L1','filling','no_account',1,1)").lastrowid
    return dict(db.connect().execute("SELECT j.*, l.company, l.title, l.url, l.ats FROM jobs j JOIN listings l ON l.id=j.listing_id WHERE j.id=?", (jid,)).fetchone())


async def test_caps_are_shown_and_enforced(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    job = _job()
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PAGE)
        snap = await snapshot.take(ex)
        by = {i["label"]: i for i in snap.items}
        assert by["Why this team?"]["max_chars"] == 255
        assert by["Describe a project"]["max_chars"] == 200
        assert by["Anything else"]["max_chars"] == 150
        assert "max_chars" not in by["Cover note"]
        assert "max 255 chars" in snapshot.render(snap)

        ctx = RunContext(job={**job, "resume_file": None}, ex=ex, budget=Budget(job["id"], "greenhouse"))
        ctx.last_snapshot = snap
        d = ToolDispatcher(ctx)
        long_text = "I like building reliable backend systems. " * 7          # about 300 characters
        out, _ = await d.t_fill_batch([
            {"ref": by["Why this team?"]["ref"], "answer_key": None, "value": long_text, "source": "generated", "label": "Why this team?"},
            {"ref": by["Describe a project"]["ref"], "answer_key": None, "value": long_text[:190], "source": "generated", "label": "Describe a project"},
            {"ref": by["Cover note"]["ref"], "answer_key": None, "value": long_text, "source": "generated", "label": "Cover note"},
        ])
        res = {r["ref"]: r for r in out["results"]}
        assert res[by["Why this team?"]["ref"]]["status"] == "failed" and "allows 255" in res[by["Why this team?"]["ref"]]["error"]
        assert res[by["Describe a project"]["ref"]]["status"] == "set"
        assert res[by["Cover note"]["ref"]]["status"] == "set"                    # no cap: long text is fine
        assert await ex.page.evaluate("() => document.getElementById('a').value") == ""   # nothing typed into the capped box
    finally:
        await ex.close()
