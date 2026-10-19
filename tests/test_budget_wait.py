"""Waiting on Surya (an answer) or on an email is not budget time: Ashby's 15-minute wall budget equalled the 15-minute
answer wait, so one question ended the run and the draft had to be refilled later (2026-10-18)."""
import asyncio
import json
import time

import pytest

from goat import db
from goat.budget import Budget
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures


def test_time_inside_waiting_does_not_use_the_wall_budget(store):
    job = _job()
    b = Budget(job["id"], "ashby")
    b.started -= 105.0                                  # the run began 105 s ago ...
    with b.waiting():
        b._wait_t0 -= 100.0                             # ... and has waited on Surya for the last 100 s (not counted)
        assert 4900 <= b.wall_ms <= 5500
        with b.waiting():                               # nested waits count once
            pass
    assert 4900 <= b.wall_ms <= 5500 and b.waited_ms >= 100_000
    b.step()                                            # persisted without the wait
    used = db.connect().execute("SELECT wall_ms_used FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
    assert used < 10_000


@pytest.mark.asyncio
async def test_a_waiting_tool_runs_off_the_clock(store):
    job = _job()
    d, ctx = _dispatcher(job)
    seen = {}

    async def fake_await_answers(**kw):
        seen["waiting"] = ctx.budget._wait_t0 is not None
        return {"answers": []}, None

    async def fake_snapshot(**kw):
        seen["snapshot_waiting"] = ctx.budget._wait_t0 is not None
        return {"outline": ""}, None

    d.t_await_answers = fake_await_answers
    d.t_snapshot = fake_snapshot
    await d.dispatch("await_answers", json.dumps({}))
    await d.dispatch("snapshot", json.dumps({}))
    assert seen == {"waiting": True, "snapshot_waiting": False}


@pytest.mark.asyncio
async def test_the_page_backstop_fires_once_the_budget_is_spent(store):
    from goat.worker import run_page_bounded
    b = Budget(_job()["id"], "ashby")
    b.wall_s = 0                                        # nothing left
    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await run_page_bounded(asyncio.sleep(30), b, margin_s=0.2, min_s=0.0, poll_s=0.05)
    assert time.monotonic() - t0 < 5


@pytest.mark.asyncio
async def test_the_page_backstop_waits_while_the_run_waits_on_surya(store):
    from goat.worker import run_page_bounded
    b = Budget(_job()["id"], "ashby")
    b.wall_s = 1                                        # one second of budget

    async def page():
        with b.waiting():                               # an answer wait longer than the whole budget
            await asyncio.sleep(1.5)
        return "page_done"

    assert await run_page_bounded(page(), b, margin_s=0.2, min_s=0.0, poll_s=0.05) == "page_done"


@pytest.mark.asyncio
async def test_the_page_backstop_returns_the_result_and_passes_errors_through(store):
    from goat.worker import run_page_bounded
    b = Budget(_job()["id"], "ashby")

    async def ok():
        return 42

    async def boom():
        raise RuntimeError("model request failed")

    assert await run_page_bounded(ok(), b, poll_s=0.05) == 42
    with pytest.raises(RuntimeError):
        await run_page_bounded(boom(), b, poll_s=0.05)
