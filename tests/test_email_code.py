"""The emailed verification code: every box it fills is a sourced decision, so the gate lets the submit through."""
import pytest

from goat import db, records
from goat.agent.tools import RunContext, ToolDispatcher
from goat.browser import fill
from goat.budget import Budget


class Handle:
    message_id = "m1"

    def enter(self, typer):
        typer("7KF3A9QZ")
        return "entered"


class Mail:
    def fetch_code(self, portal, host, after_ts, company=""):
        return Handle()


class Ex:
    url = "https://job-boards.greenhouse.io/embed/job_app?for=coinbase&token=8175510"


@pytest.mark.asyncio
async def test_each_code_box_gets_a_sourced_decision(tmp_path, monkeypatch):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Coinbase','Intern','https://x.example/job','greenhouse')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','no_account',0)")

    async def typed(ex, refs, code):
        return True
    monkeypatch.setattr(fill, "type_code_into", typed)
    ctx = RunContext(job={"id": 1, "company": "Coinbase", "ats": "greenhouse", "resume_file": None}, ex=Ex(),
                     budget=Budget(1, "greenhouse"), classifier=None)
    ctx.mail = Mail()
    refs = [f"e{n}" for n in range(157, 165)]
    out, _ = await ToolDispatcher(ctx).t_enter_email_code(refs)
    assert out["status"] == "entered"
    decs = [d for d in records.decisions(1) if d["field_label"] == "emailed verification code"]
    assert sorted(d["ref"] for d in decs) == sorted(refs)
    assert all(d["value"] == "(set)" and d["source"].startswith("policy:") for d in decs)
