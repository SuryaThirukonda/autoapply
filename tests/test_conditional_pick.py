"""DTCC (2026-10-25): 'Have you ever applied here before? Yes' was right by his ledger (DTCC, 2026-10-10), but the
computer-pick check compared it with the raw conditional answer 'Yes / No' and refused it; the 'If yes, when?' box was
refused with it."""
import pytest

from goat import answers, ledger
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures


@pytest.mark.asyncio
async def test_a_hand_picked_yes_matching_a_conditional_answer_is_kept(store):
    ledger.record_application(None, "DTCC", "Application Developer Intern", "https://dtcc.example/1", "hermes:tracker",
                              submitted_at=1_791_651_975)
    answers.save_answer("preferences.applied_before", "Yes / No", answer_type="conditional:ledger_company", source="questionnaire")
    d, ctx = _dispatcher(_job("DTCC", "C1"))

    async def read_ref(ref):
        return {"type": "radio", "value": "checked", "label": "Yes"}
    ctx.ex.read_ref = read_ref
    item = {"ref": "e27", "tag": "input", "type": "radio", "label": "Yes", "group": "Have you ever applied here before?"}
    out = await d._keep_computer_pick({"ref": "e27", "answer_key": "preferences.applied_before", "source": "computer",
                                       "label": "Have you ever applied here before?"}, item)
    assert out.get("status") != "failed", out


@pytest.mark.asyncio
async def test_a_hand_picked_no_against_a_ledger_yes_is_refused(store):
    ledger.record_application(None, "DTCC", "Application Developer Intern", "https://dtcc.example/1", "hermes:tracker",
                              submitted_at=1_791_651_975)
    answers.save_answer("preferences.applied_before", "Yes / No", answer_type="conditional:ledger_company", source="questionnaire")
    d, ctx = _dispatcher(_job("DTCC", "C2"))

    async def read_ref(ref):
        return {"type": "radio", "value": "checked", "label": "No"}
    ctx.ex.read_ref = read_ref
    item = {"ref": "e28", "tag": "input", "type": "radio", "label": "No", "group": "Have you ever applied here before?"}
    out = await d._keep_computer_pick({"ref": "e28", "answer_key": "preferences.applied_before", "source": "computer",
                                       "label": "Have you ever applied here before?"}, item)
    assert out.get("status") == "failed", out
