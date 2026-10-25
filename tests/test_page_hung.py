"""Netsmart (Workday, 2026-10-24): the page froze on Sign In. A click and two snapshots timed out at 4 minutes each, the
hold's screenshot then waited on the frozen page with no limit, and the run sat for 20 minutes twice. A tool timeout on a
page that no longer runs a one-line script now holds the job as a portal error."""
import asyncio
from types import SimpleNamespace

import pytest

from goat import db
from goat.budget import Parked
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures


async def _hang(*_a, **_k):
    await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_a_tool_timeout_on_a_frozen_page_holds_the_job(store, monkeypatch):
    d, ctx = _dispatcher(_job("Netsmart", "L14"))
    ctx.ex = SimpleNamespace(page=SimpleNamespace(evaluate=_hang))
    monkeypatch.setattr(d, "t_snapshot", _hang)
    monkeypatch.setattr(d, "tool_timeout_s", lambda name: 0.2)
    with pytest.raises(Parked) as e:
        await d.dispatch("snapshot", "{}")
    assert e.value.reason.startswith("portal_error: the page stopped responding")
    assert db.connect().execute("SELECT 1 FROM events WHERE kind='page:hung'").fetchone()


@pytest.mark.asyncio
async def test_a_tool_timeout_on_a_live_page_returns_to_the_model(store, monkeypatch):
    async def answers(*_a, **_k):
        return 1
    d, ctx = _dispatcher(_job("Acme", "L15"))
    ctx.ex = SimpleNamespace(page=SimpleNamespace(evaluate=answers))
    monkeypatch.setattr(d, "t_snapshot", _hang)
    monkeypatch.setattr(d, "tool_timeout_s", lambda name: 0.2)
    out, _ = await d.dispatch("snapshot", "{}")
    assert "did not finish" in out
