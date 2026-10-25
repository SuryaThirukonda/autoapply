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


@pytest.mark.asyncio
async def test_a_choose_timeout_records_the_wanted_pick_the_field_holds(store, monkeypatch):
    """Allegion (Workday, 2026-10-25): the school picker picked and then held choose to the timeout, and the school was
    submitted with no decision recording it."""
    async def answers(*_a, **_k):
        return 1

    async def read_ref(ref):
        return {"shown": "Texas A&M University-College Station"}
    from goat import profile
    monkeypatch.setattr(profile, "text", lambda *a, **k: "Texas A&M University, College Station")
    d, ctx = _dispatcher(_job("Allegion", "L16"))
    ctx.ex = SimpleNamespace(page=SimpleNamespace(evaluate=answers), read_ref=read_ref)
    calls = []

    async def choose(ref, answer_key, options, label, source):
        calls.append(options)
        if len(calls) == 1:
            await asyncio.sleep(3600)
        return {"ref": ref, "status": "set", "readback": options[0]}, None
    monkeypatch.setattr(d, "t_choose", choose)
    monkeypatch.setattr(d, "tool_timeout_s", lambda name: 0.2)
    out, _ = await d.dispatch("choose", '{"ref": "e126", "answer_key": "education.school", "options": ["Texas A&M University"], '
                                        '"label": "School or University", "source": "answer_key"}')
    assert '"status": "set"' in out and "recorded" in out
    assert calls[1][0] == "Texas A&M University-College Station"
    assert db.connect().execute("SELECT 1 FROM events WHERE kind='choose:recovered'").fetchone()


@pytest.mark.asyncio
async def test_a_choose_timeout_names_an_unwanted_value_the_field_holds(store, monkeypatch):
    async def answers(*_a, **_k):
        return 1

    async def read_ref(ref):
        return {"shown": "TX-Texas A&M University-Commerce"}
    d, ctx = _dispatcher(_job("Allegion", "L17"))
    ctx.ex = SimpleNamespace(page=SimpleNamespace(evaluate=answers), read_ref=read_ref)
    monkeypatch.setattr(d, "t_choose", _hang)
    monkeypatch.setattr(d, "tool_timeout_s", lambda name: 0.2)
    out, _ = await d.dispatch("choose", '{"ref": "e126", "answer_key": "education.school", "options": ["Texas A&M University"], '
                                        '"label": "School or University", "source": "answer_key"}')
    assert "TX-Texas A&M University-Commerce" in out and "no decision records" in out
    assert db.connect().execute("SELECT 1 FROM events WHERE kind='choose:unrecorded'").fetchone()


def test_campus_wording_takes_his_campus_and_no_other(monkeypatch):
    from goat import profile
    from goat.agent.tools import campus_wording_of
    monkeypatch.setattr(profile, "text", lambda *a, **k: "Texas A&M University, College Station. BS Computer Science")
    wants = ["Texas A&M University", "Texas A & M University"]
    assert campus_wording_of("Texas A&M University-College Station", wants)
    assert campus_wording_of("TX-Texas A&M University - College Station", wants)
    assert campus_wording_of("TX-Texas A&M University", wants)
    assert not campus_wording_of("TX-Texas A&M University-Commerce", wants)
    assert not campus_wording_of("Texas A&M University - Corpus Christi", wants)
    assert not campus_wording_of("University of Texas at Austin", wants)
