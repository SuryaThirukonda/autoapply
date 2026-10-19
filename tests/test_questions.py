"""QuestionBroker: ask / await_answers (deadline from the FIRST ask) / deliver / hold."""
from __future__ import annotations

import asyncio
import time

import pytest

from goat import answers, db, states
from goat.budget import Parked
from goat.config import SETTINGS
from goat.human import questions


@pytest.fixture
def broker(tmp_path, monkeypatch):
    """Fresh DB and broker; a 1s answer window. asked_at is stored in whole seconds, so tests
    that must NOT time out widen the window with `wide`."""
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(SETTINGS.budgets, "answer_wait_s", 1)
    return questions.QuestionBroker()


@pytest.fixture
def wide(monkeypatch):
    monkeypatch.setattr(SETTINGS.budgets, "answer_wait_s", 5)


def make_job(listing_id: str, status: str = states.FILLING, blocked_on: str | None = None) -> int:
    with db.tx() as con:
        con.execute("INSERT INTO listings(id, company, title, url, first_seen, updated_at, active) "
                    "VALUES(?,?,?,?,?,?,1)",
                    (listing_id, "Acme", "SWE Intern", f"https://acme.test/{listing_id}", db.now(), db.now()))
        cur = con.execute(
            "INSERT INTO jobs(listing_id, status, lane, blocked_on_question_key, hold_reason, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (listing_id, status, "no_account", blocked_on,
             f"answer_timeout: {blocked_on}" if blocked_on else None, db.now(), db.now()))
        return int(cur.lastrowid)


def job_status(job_id: int) -> str:
    return db.connect().execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"]


def pending_rows(job_id: int):
    return db.connect().execute(
        "SELECT * FROM pending_questions WHERE job_id=? ORDER BY id", (job_id,)).fetchall()


class FakeSender:
    """Stands in for the Telegram channel: one message id per batch, shared by every question."""

    def __init__(self, start: int = 1000):
        self.next_id = start
        self.calls: list[tuple[int, list[dict], bytes | None]] = []

    async def __call__(self, job_id, qs, png):
        self.calls.append((job_id, qs, png))
        mid = self.next_id
        self.next_id += 1
        return [mid] * len(qs)


# --------------------------------------------------------------------------- (1)
@pytest.mark.asyncio
async def test_ask_without_sender_records_pending_rows(broker, caplog):
    job = make_job("L1")
    qs = [{"key": "q.have_you_worked_for_an_acme_subsidiary",
           "question": "Have you worked for an Acme subsidiary?", "options": ["Yes", "No"],
           "field_type": "radio"},
          {"key": "q.desired_start_date", "question": "Desired start date?"}]
    with caplog.at_level("WARNING", logger="goat.questions"):
        keys = await broker.ask(job, qs, None)
    assert keys == [q["key"] for q in qs]
    rows = pending_rows(job)
    assert [r["key"] for r in rows] == keys
    assert all(r["chat_message_id"] is None for r in rows)
    assert all(r["answered_at"] is None for r in rows)
    assert rows[0]["deadline_at"] == rows[0]["asked_at"] + 1
    assert "no sender registered" in caplog.text
    assert broker.pending_for_job(job)[0]["options"] == ["Yes", "No"]

    # Asking again for the same key/job does not create a second row.
    await broker.ask(job, qs[:1], None)
    assert len(pending_rows(job)) == 2

    # A key with a stored answer is returned but never recorded.
    answers.save_answer("defaults.us_citizen", "Yes", source="test")
    keys = await broker.ask(job, [{"key": "defaults.us_citizen", "question": "US citizen?"}], None)
    assert keys == ["defaults.us_citizen"]
    assert len(pending_rows(job)) == 2


# --------------------------------------------------------------------------- (2)
@pytest.mark.asyncio
async def test_await_returns_when_deliver_supplies_answer(broker, wide):
    sender = FakeSender()
    broker.register_sender(sender)
    job = make_job("L2")
    key = "q.have_you_worked_for_an_acme_subsidiary"
    keys = await broker.ask(job, [{"key": key, "question": "Have you worked for an Acme subsidiary?",
                                   "options": ["Yes", "No"]}], b"png")
    assert sender.calls and sender.calls[0][2] == b"png"
    mid = sender.calls[0][1][0]["id"]  # pending id handed to the sender
    assert pending_rows(job)[0]["chat_message_id"] == 1000
    assert mid == pending_rows(job)[0]["id"]

    task = asyncio.create_task(broker.await_answers(job, keys))
    await asyncio.sleep(0.2)
    assert not task.done()
    res = broker.deliver(1000, "no", from_user_id=42)
    assert res["ok"] and res["key"] == key and res["answer"] == "No"  # normalized to option text
    got = await asyncio.wait_for(task, 1.0)
    assert got == {key: "No"}
    a = answers.get(key)
    assert a.source == "telegram" and a.answer_type == "choice"
    assert pending_rows(job)[0]["answered_at"] is not None
    # The question text became an alias for the key.
    assert answers.resolve_alias("Have you worked for an Acme subsidiary?")[0] == key
    # A second reply to the same message finds nothing pending.
    assert broker.deliver(1000, "yes") == {"ok": False, "reason": "no_pending_question"}


# --------------------------------------------------------------------------- (3)
@pytest.mark.asyncio
async def test_timeout_parks_measured_from_first_ask(broker):
    job = make_job("L3")
    keys = await broker.ask(job, [{"key": "q.a", "question": "A?"}, {"key": "q.b", "question": "B?"}], None)
    await asyncio.sleep(0.6)
    t0 = time.monotonic()
    with pytest.raises(Parked) as ei:
        await broker.await_answers(job, keys)
    waited = time.monotonic() - t0
    assert waited < 0.9, f"waited {waited:.2f}s: deadline was not measured from the first ask"
    p = ei.value
    assert p.state == "hold"
    assert p.reason == "answer_timeout: q.a, q.b"
    assert p.blocked_on == ["q.a", "q.b"]
    assert broker.expire_overdue() == 2
    kinds = [r["kind"] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=?", (job,))]
    assert "question:asked" in kinds and "question:timeout" in kinds


@pytest.mark.asyncio
async def test_key_never_asked_waits_full_window(broker):
    job = make_job("L3b")
    t0 = time.monotonic()
    with pytest.raises(Parked):
        await broker.await_answers(job, ["q.never_asked"])
    assert 0.9 <= time.monotonic() - t0 < 1.5


# --------------------------------------------------------------------------- (4)
@pytest.mark.asyncio
async def test_deliver_releases_held_job(broker):
    broker.register_sender(FakeSender())
    key = "q.willing_to_relocate"
    job = make_job("L4")
    held = make_job("L4-held", status=states.HOLD, blocked_on=key)
    other = make_job("L4-other", status=states.HOLD, blocked_on="q.something_else")
    await broker.ask(job, [{"key": key, "question": "Willing to relocate?"}], None)
    res = broker.deliver(1000, "Yes, anywhere in the US")
    assert res["ok"] and res["released"] == 1
    assert job_status(held) == states.QUEUED
    assert job_status(other) == states.HOLD
    row = db.connect().execute("SELECT blocked_on_question_key, hold_reason, priority FROM jobs WHERE id=?",
                               (held,)).fetchone()
    assert row["blocked_on_question_key"] is None and row["hold_reason"] is None and row["priority"] == 10


# --------------------------------------------------------------------------- (5)
@pytest.mark.asyncio
async def test_multi_question_reply_split_by_lines(broker, wide):
    sender = FakeSender(start=77)
    broker.register_sender(sender)
    job = make_job("L5")
    qs = [{"key": "q.one", "question": "One?"},
          {"key": "q.two", "question": "Two?", "options": ["Alpha", "Beta", "Gamma"]},
          {"key": "q.three", "question": "Three?"}]
    keys = await broker.ask(job, qs, None)
    assert len(sender.calls) == 1 and len(sender.calls[0][1]) == 3   # one message for the batch
    assert [r["chat_message_id"] for r in pending_rows(job)] == [77, 77, 77]

    task = asyncio.create_task(broker.await_answers(job, keys))
    await asyncio.sleep(0.1)
    res = broker.deliver(77, "1) first answer\n2. 2\n\n3) third answer")
    assert res["ok"]
    assert [s["key"] for s in res["saved"]] == ["q.one", "q.two", "q.three"]
    got = await asyncio.wait_for(task, 1.0)
    assert got == {"q.one": "first answer", "q.two": "Beta", "q.three": "third answer"}
    assert all(r["answered_at"] is not None for r in pending_rows(job))


@pytest.mark.asyncio
async def test_partial_multi_reply_keeps_rest_pending(broker, wide):
    broker.register_sender(FakeSender(start=5))
    job = make_job("L5b")
    keys = await broker.ask(job, [{"key": "q.x", "question": "X?"}, {"key": "q.y", "question": "Y?"}], None)
    res = broker.deliver(5, "only x")
    assert res["ok"] and res["unanswered"] == ["q.y"]
    assert [r["key"] for r in broker.pending_for_job(job)] == ["q.y"]
    res2 = broker.deliver(5, "now y")
    assert res2["ok"] and res2["key"] == "q.y"
    assert await broker.await_answers(job, keys) == {"q.x": "only x", "q.y": "now y"}


# --------------------------------------------------------------------------- hold
@pytest.mark.asyncio
async def test_hold_it_parks_immediately(broker, wide):
    broker.register_sender(FakeSender(start=9))
    job = make_job("L6")
    keys = await broker.ask(job, [{"key": "q.salary", "question": "Salary expectation?"}], None)
    task = asyncio.create_task(broker.await_answers(job, keys))
    await asyncio.sleep(0.1)
    assert broker.hold_it(9) == {"ok": True, "keys": ["q.salary"], "job_ids": [job]}
    with pytest.raises(Parked) as ei:
        await asyncio.wait_for(task, 0.5)
    assert ei.value.reason == "answer_declined: q.salary"
    assert ei.value.state == "hold" and ei.value.blocked_on == ["q.salary"]
    assert answers.get("q.salary") is None            # nothing was written to the store
    assert broker.pending_for_job(job) == []


def test_split_reply_helper():
    assert questions.split_reply("  yes ", 1) == ["yes"]
    assert questions.split_reply("", 1) == [None]
    assert questions.split_reply("a\nb", 2) == ["a", "b"]
    assert questions.split_reply("2) b\n1) a", 2) == ["a", "b"]
    assert questions.split_reply("a", 3) == ["a", None, None]


# --------------------------------------------------------------------------- batched card
@pytest.mark.asyncio
async def test_ask_payload_is_numbered_for_the_batched_card(broker, wide):
    sender = FakeSender()
    broker.register_sender(sender)
    job = make_job("L9")
    await broker.ask(job, [{"key": "q.one", "question": "One?", "options": ["Yes", "No"]},
                           {"key": "q.two", "question": "Two?"}], None)
    payload = sender.calls[0][1]
    assert [p["n"] for p in payload] == [1, 2] and payload[1]["key"] == "q.two"


def _row(job_id, key, asked, deadline, answered=None):
    with db.tx() as con:
        con.execute("INSERT INTO pending_questions(job_id, key, question, options, field_type, asked_at, deadline_at, answered_at, answer)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", (job_id, key, "q?", "[]", "text", asked, deadline, answered, "x" if answered else None))


def test_the_wait_is_short_only_while_surya_is_away(tmp_path, monkeypatch):
    """Overnight 2026-10-19: every unanswered question held the single worker for 15 minutes."""
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(SETTINGS.budgets, "answer_wait_s", 900)
    monkeypatch.setattr(SETTINGS.budgets, "answer_wait_away_s", 120)
    monkeypatch.setattr(SETTINGS.budgets, "away_after_s", 7200)
    now = 1_800_000_000
    j = make_job("L1")
    assert questions.effective_answer_wait_s(now) == 900          # nothing asked lately
    _row(j, "a", now - 5000, now - 4100, answered=now - 3600)
    _row(j, "b", now - 3000, now - 2100)
    assert questions.effective_answer_wait_s(now) == 900          # he answered an hour ago
    with db.tx() as con:
        con.execute("UPDATE pending_questions SET answered_at=? WHERE key='a'", (now - 3 * 3600,))
    assert questions.effective_answer_wait_s(now) == 120          # quiet for 3 h and question b went unanswered
    monkeypatch.setattr(SETTINGS.budgets, "answer_wait_away_s", 0)
    assert questions.effective_answer_wait_s(now) == 900          # 0 turns it off


@pytest.mark.asyncio
async def test_a_question_keeps_the_wait_it_was_asked_with(tmp_path, monkeypatch):
    """The first question after a quiet spell gets the full wait even though, once asked, it counts as unanswered."""
    db.reset_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(SETTINGS.budgets, "answer_wait_s", 3)
    monkeypatch.setattr(SETTINGS.budgets, "answer_wait_away_s", 1)
    monkeypatch.setattr(SETTINGS.budgets, "away_after_s", 7200)
    b = questions.QuestionBroker()
    j = make_job("L2")
    await b.ask(j, [{"key": "q.x", "question": "X?", "options": [], "field_type": "text"}])
    other = make_job("L3")
    _row(other, "q.y", db.now() - 100, db.now() - 50)           # now a missed question exists: new asks would be short
    assert questions.effective_answer_wait_s() == 1
    t0 = time.time()
    with pytest.raises(Parked):
        await b.await_answers(j, ["q.x"])
    assert time.time() - t0 >= 1.5



@pytest.mark.asyncio
async def test_a_company_scoped_answer_ends_the_wait(tmp_path, monkeypatch):
    """Sentry (2026-10-19): the answer was saved for Sentry only and the waiter, looking it up without the company,
    never saw it."""
    from goat import answers, db
    from goat.human.questions import QuestionBroker
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Sentry','Intern','https://jobs.ashbyhq.com/sentry/1','ashby')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','awaiting_answer','no_account',0)")
        con.execute("INSERT INTO pending_questions(job_id,key,question,options,field_type,asked_at,deadline_at) VALUES(1,'q.vienna','Vienna?','[]','choice',?,?)",
                    (db.now(), db.now() + 60))
    answers.save_answer("q.vienna", "Yes", answer_type="choice", source="operator", scope="company", scope_value="Sentry")
    found = await QuestionBroker().await_answers(1, ["q.vienna"])
    assert found == {"q.vienna": "Yes"}
