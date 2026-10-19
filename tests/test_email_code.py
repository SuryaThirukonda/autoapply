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


class NoMail:
    def fetch_code(self, portal, host, after_ts, company=""):
        return None


@pytest.mark.asyncio
async def test_no_email_asks_surya_for_the_code_and_types_his_answer(tmp_path, monkeypatch):
    """Surya (2026-10-19): when the agent cannot find the code email, he types the code on the website or in Telegram.
    The code is typed into the boxes once and never kept as an answer."""
    import asyncio
    from goat import answers
    from goat.human.questions import BROKER
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Coinbase','Intern','https://x.example/job','greenhouse')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','no_account',0)")
    typed = {}

    async def type_it(ex, refs, code):
        typed["code"], typed["refs"] = code, list(refs)
        return True
    monkeypatch.setattr(fill, "type_code_into", type_it)
    ctx = RunContext(job={"id": 1, "company": "Coinbase", "ats": "greenhouse", "resume_file": None}, ex=Ex(),
                     budget=Budget(1, "greenhouse"), classifier=None)
    ctx.mail = NoMail()
    refs = ["e157", "e158"]
    task = asyncio.create_task(ToolDispatcher(ctx).t_enter_email_code(refs))
    row = None
    for _ in range(100):
        await asyncio.sleep(0.05)
        row = db.connect().execute("SELECT * FROM pending_questions WHERE job_id=1 AND answered_at IS NULL").fetchone()
        if row:
            break
    assert row is not None and row["key"].startswith("code.") and row["field_type"] == "code"
    assert row["deadline_at"] - row["asked_at"] >= 600
    out = BROKER.answer_pending(row["id"], " 7kf3-a9qz ", source="web")
    assert out["ok"]
    res, _ = await asyncio.wait_for(task, 10)
    assert res["status"] == "entered" and typed == {"code": "7kf3a9qz", "refs": refs}
    assert answers.get(row["key"]) is None                                    # never an answer
    assert db.connect().execute("SELECT answer FROM pending_questions WHERE id=?", (row["id"],)).fetchone()[0] == "(used)"
    decs = [d for d in records.decisions(1) if d["field_label"] == "emailed verification code"]
    assert sorted(d["ref"] for d in decs) == refs and all(d["source"] == "user:verification_code" for d in decs)
