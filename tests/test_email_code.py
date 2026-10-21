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



@pytest.mark.asyncio
async def test_a_code_typed_on_the_website_beforehand_is_used_first(tmp_path, monkeypatch):
    """Surya (2026-10-19): a code box on the website for any application; a code typed there before the agent asks
    is used at the next code prompt, and nothing is looked up in Gmail."""
    from goat.human.questions import BROKER
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Coinbase','Intern','https://x.example/job','greenhouse')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','no_account',0)")
    assert BROKER.provide_code(1, "  ")["ok"] is False
    assert BROKER.provide_code(1, "ab12-cd34")["ok"]
    typed = {}

    async def type_it(ex, refs, code):
        typed["code"] = code
        return True

    class Boom:
        def fetch_code(self, *a, **k):
            raise AssertionError("Gmail must not be read when Surya already gave the code")
    monkeypatch.setattr(fill, "type_code_into", type_it)
    ctx = RunContext(job={"id": 1, "company": "Coinbase", "ats": "greenhouse", "resume_file": None}, ex=Ex(),
                     budget=Budget(1, "greenhouse"), classifier=None)
    ctx.mail = Boom()
    out, _ = await ToolDispatcher(ctx).t_enter_email_code(["e1"])
    assert out["status"] == "entered" and typed["code"] == "ab12cd34"
    assert BROKER.recent_code(1) is None                       # used once
    assert db.connect().execute("SELECT answer FROM pending_questions WHERE job_id=1").fetchone()[0] == "(used)"


def test_a_code_typed_on_the_website_answers_an_open_code_question(tmp_path):
    from goat.human.questions import BROKER
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Coinbase','Intern','https://x.example/job','greenhouse')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','awaiting_code','no_account',0)")
        con.execute("INSERT INTO pending_questions(job_id,key,question,options,field_type,asked_at,deadline_at) VALUES(1,'code.1.5','Code?',NULL,'code',?,?)",
                    (db.now(), db.now() + 600))
    assert BROKER.provide_code(1, "ZZ99")["ok"]
    assert BROKER.code_answer(1, "code.1.5") == "ZZ99"



def _code_job(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Texas Instruments','Intern','https://x.example/job','oracle')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','account',0)")
    return RunContext(job={"id": 1, "company": "Texas Instruments", "ats": "oracle", "resume_file": None}, ex=Ex(),
                      budget=Budget(1, "oracle"), classifier=None)


@pytest.mark.asyncio
async def test_a_code_he_typed_while_gmail_was_searched_is_used_without_asking_again(tmp_path, monkeypatch):
    """Texas Instruments (2026-10-20): his 20:05 code sat unused while the run asked him again at 20:11."""
    from goat.human.questions import BROKER
    ctx = _code_job(tmp_path)
    typed = []

    async def type_it(ex, refs, code):
        typed.append(code)
        return True
    monkeypatch.setattr(fill, "type_code_into", type_it)

    class LateMail:
        def fetch_code(self, portal, host, after_ts, company=""):
            BROKER.provide_code(1, "913 700")       # he types it on the website during the search
            return None
    ctx.mail = LateMail()
    out, _ = await ToolDispatcher(ctx).t_enter_email_code(["e24", "e25"])
    assert out["status"] == "entered" and typed == ["913700"]
    assert db.connect().execute("SELECT COUNT(*) FROM pending_questions WHERE job_id=1 AND key LIKE 'code.%' AND "
                                "question LIKE 'Verification code needed%'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_his_code_that_did_not_land_is_typed_once_more_before_anything_else(tmp_path, monkeypatch):
    from goat.human.questions import BROKER
    ctx = _code_job(tmp_path)
    tries = []

    async def type_it(ex, refs, code):
        tries.append(code)
        return len(tries) > 1                       # the first try lands nowhere (a layer over the boxes)
    monkeypatch.setattr(fill, "type_code_into", type_it)

    class Boom:
        def fetch_code(self, *a, **k):
            raise AssertionError("no Gmail search and no new question while his code is untried")
    ctx.mail = Boom()
    assert BROKER.provide_code(1, "913700")["ok"]
    d = ToolDispatcher(ctx)
    out, _ = await d.t_enter_email_code(["e24"])
    assert out["status"] == "entered_unverified" and "enter_email_code again" in out["next"]
    out, _ = await d.t_enter_email_code(["e24"])
    assert out["status"] == "entered" and tries == ["913700", "913700"]
    assert ctx.retry_code == ""


@pytest.mark.asyncio
async def test_a_second_ask_presses_send_new_code_first(tmp_path, monkeypatch):
    """Texas Instruments (2026-10-20): asked a second time with no new code requested, no second email ever came."""
    from goat.browser import snapshot as snapmod
    from goat.browser.snapshot import Snapshot
    ctx = _code_job(tmp_path)
    ctx.code_asks = 1
    clicked = []

    class Ex2(Ex):
        async def click_ref(self, ref, allow_submit=False):
            clicked.append(ref)
            return {"found": True}
    ctx.ex = Ex2()
    snap = Snapshot(url="u", title="t", headings=[], items=[
        {"ref": "e30", "tag": "button", "label": "VERIFY"}, {"ref": "e31", "tag": "button", "label": "Send New Code"}],
        groups=[], errors={}, challenge={})

    async def take(ex, *a, **k):
        return snap
    monkeypatch.setattr(snapmod, "take", take)

    async def type_it(ex, refs, code):
        return True
    monkeypatch.setattr(fill, "type_code_into", type_it)

    class NewMail:
        def fetch_code(self, portal, host, after_ts, company=""):
            return Handle() if clicked else None
    ctx.mail = NewMail()
    out, _ = await ToolDispatcher(ctx).t_enter_email_code(["e24"])
    assert clicked == ["e31"] and out["status"] == "entered"
    kinds = [r[0] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=1")]
    assert "code:resend_clicked" in kinds and "code:asked_surya" not in kinds
