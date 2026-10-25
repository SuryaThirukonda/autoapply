"""DTCC (2026-10-25): 'Have you applied here before?' was answered Yes from his ledger (DTCC, 2026-10-10), and its
'If yes, when?' went to Surya. The month comes from the same ledger; a bare 'If yes, when?' after any other question is
left alone."""
import pytest

from goat import db, ledger
from goat.agent.tools import applied_when
from goat.browser.snapshot import Snapshot
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

OCT_10 = 1_791_651_975        # 2026-10-10 local


def _snap(labels):
    items = [{"ref": f"e{n}", "tag": "input", "type": "radio", "label": "Yes", "group": lab} for n, lab in enumerate(labels)]
    s = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        s.refs[i["ref"]] = i
    return s


def test_applied_when_names_the_month_of_the_ledgers_application(store):
    ledger.record_application(None, "DTCC", "Application Developer Intern", "https://dtcc.example/1", "hermes:tracker",
                              submitted_at=OCT_10)
    assert applied_when("DTCC") == "October 2026"
    assert applied_when("Some Other Employer") == ""


@pytest.mark.asyncio
async def test_if_yes_when_after_an_applied_before_question_takes_the_ledger_month(store):
    ledger.record_application(None, "DTCC", "Application Developer Intern", "https://dtcc.example/1", "hermes:tracker",
                              submitted_at=OCT_10)
    d, ctx = _dispatcher(_job("DTCC", "D1"))
    ctx.last_snapshot = _snap(["Have you applied here before?"])
    out, _ = await d.t_lookup_answer("If yes, when?", [], None, None)
    assert out.get("answer") == "October 2026" and out.get("via") == "ledger", out
    assert db.connect().execute("SELECT 1 FROM events WHERE kind='answer:applied_when'").fetchone()


@pytest.mark.asyncio
async def test_if_yes_when_after_another_question_is_left_alone(store):
    ledger.record_application(None, "DTCC", "Application Developer Intern", "https://dtcc.example/1", "hermes:tracker",
                              submitted_at=OCT_10)
    d, ctx = _dispatcher(_job("DTCC", "D2"))
    ctx.last_snapshot = _snap(["Have you ever been convicted of a felony?"])
    out, _ = await d.t_lookup_answer("If yes, when?", [], None, None)
    assert out.get("answer") != "October 2026", out


def test_the_not_ticked_message_says_how_to_have_the_harness_tick_it():
    """DTCC (2026-10-25): eighteen radios sent with source 'computer' and never clicked, each read back 'not ticked'."""
    from goat.agent.tools import NOT_PICKED_HINT
    assert "source null" in NOT_PICKED_HINT and "fill_batch" in NOT_PICKED_HINT


@pytest.mark.asyncio
async def test_when_did_you_previously_apply_takes_the_ledger_month(store):
    """DTCC (2026-10-25): 'When did you previously apply to DTCC (month)?' went to Surya."""
    ledger.record_application(None, "DTCC", "Application Developer Intern", "https://dtcc.example/1", "hermes:tracker",
                              submitted_at=OCT_10)
    d, ctx = _dispatcher(_job("DTCC", "D3"))
    ctx.last_snapshot = _snap(["Have you ever applied here before?"])
    out, _ = await d.t_lookup_answer("When did you previously apply to DTCC (month)?", [], None, None)
    assert out.get("answer") == "October 2026", out
