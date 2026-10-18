"""The chat agent's tools against a scratch database, without a model: status, job lookup, run_now, answering
and settling questions as AS_IS (Surya set the field himself), and remembering. Also the broker's reading of
"I already filled it out" as AS_IS."""
from __future__ import annotations

import asyncio

from goat import answers, db, memory
from goat.chat.agent import ChatAgent
from goat.human.questions import AS_IS, BROKER


class _Control:
    def __init__(self):
        self.current = None
        self.wake = asyncio.Event()
        self.paused = None

    def set_paused(self, paused, *, after_current=False, by=""):
        self.paused = paused

    def request_stop(self, mode, by=""):
        self.mode = mode


def _seed(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    memory.DIR = tmp_path / "memory"; memory.NOTES = memory.DIR / "notes"; memory.MEMORY_MD = memory.DIR / "MEMORY.md"
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L1','DoorDash','SWE Intern','https://job-boards.greenhouse.io/doordashusa/jobs/1','greenhouse')")
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('L2','Lyft','Backend Intern','https://job-boards.greenhouse.io/lyft/jobs/2','greenhouse')")
        j1 = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at,hold_reason) VALUES('L1','hold','no_account',1,1,'no_standing_answer: org preference')").lastrowid
        j2 = con.execute("INSERT INTO jobs(listing_id,status,lane,created_at,updated_at) VALUES('L2','awaiting_answer','no_account',1,1)").lastrowid
        con.execute("INSERT INTO pending_questions(job_id,key,question,options,field_type,asked_at,deadline_at) VALUES(?,?,?,?,?,?,?)",
                    (j2, "q.employment_history", "Please enter your relevant employment", None, "textarea", db.now(), db.now() + 900))
    return j1, j2


def test_status_lists_jobs_and_questions(tmp_path):
    j1, j2 = _seed(tmp_path)
    agent = ChatAgent(client=object(), worker_control=_Control())
    st = agent.t_status()
    assert st["jobs"]["hold"] == 1 and st["jobs"]["awaiting_answer"] == 1
    assert st["open_questions"][0]["company"] == "Lyft"
    assert st["holds_by_reason"][0]["reason"] == "no_standing_answer"
    found = agent.t_list_jobs(None, "door", 10)["jobs"]
    assert found and found[0]["job_id"] == j1 and found[0]["company"] == "DoorDash"
    det = agent.t_job_detail(j2)
    assert det["open_questions"][0]["key"] == "q.employment_history"


def test_run_now_puts_a_parked_job_first(tmp_path):
    j1, _ = _seed(tmp_path)
    ctl = _Control()
    agent = ChatAgent(client=object(), worker_control=ctl)
    out = agent.t_job_action([j1], "run_now")
    assert out.get("ok", True)
    row = db.connect().execute("SELECT status, priority FROM jobs WHERE id=?", (j1,)).fetchone()
    assert row["status"] == "queued" and row["priority"] == 5000
    assert ctl.wake.is_set()
    assert (memory.NOTES / f"{__import__('datetime').date.today().isoformat()}.md").exists()


def test_keep_as_is_settles_open_questions_without_storing_an_answer(tmp_path):
    _, j2 = _seed(tmp_path)
    agent = ChatAgent(client=object(), worker_control=_Control())
    out = agent.t_keep_as_is(j2)
    assert out["ok"] and out["keys"] == ["q.employment_history"]
    assert answers.get("q.employment_history") is None                       # nothing invented in the store
    assert BROKER._kept_as_is(j2, "q.employment_history")
    found = asyncio.run(BROKER.await_answers(j2, ["q.employment_history"]))
    assert found["q.employment_history"].startswith(AS_IS)


def test_a_done_reply_is_read_as_as_is(tmp_path):
    _, j2 = _seed(tmp_path)
    pid = db.connect().execute("SELECT id FROM pending_questions WHERE job_id=?", (j2,)).fetchone()["id"]
    out = BROKER.answer_pending(pid, "I already filled it out", source="telegram")
    assert out["ok"] and out["saved"][0]["answer"] == AS_IS
    assert answers.get("q.employment_history") is None
    assert BROKER._kept_as_is(j2, "q.employment_history")


def test_remember_routes_facts_and_notes(tmp_path):
    _seed(tmp_path)
    agent = ChatAgent(client=object(), worker_control=_Control())
    assert agent.t_remember("skills.cpp_years", "2", "years of experience with c++", False)["ok"]
    assert answers.get("skills.cpp_years").answer == "2"
    assert answers.lookup("years of experience with c++").key == "skills.cpp_years"
    agent.t_remember(None, "Workday tenants of GE Vernova need the date fix", None, True)
    assert "GE Vernova" in memory.MEMORY_MD.read_text(encoding="utf-8")
    assert "MEMORY" in memory.snapshot()
