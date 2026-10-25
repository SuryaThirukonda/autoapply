"""Penn State (2026-10-25): the model chose his clearance-obstacles 'No' on the clearance acknowledgement's ref, and the
list's 'No, I do not understand. I have questions ...' was accepted as the portal's wording for No."""
import pytest

from goat import answers
from goat.agent.tools import ACK_SENTENCE
from goat.browser.fill import FieldResult
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

NO_ACK = "No, I do not understand. I have questions about the above information, and I can contact the ARL Security Office."


def test_only_a_negative_acknowledgement_is_caught():
    assert ACK_SENTENCE.search(NO_ACK)
    assert not ACK_SENTENCE.search("Yes, I understand the above information regarding security clearances.")
    assert not ACK_SENTENCE.search("I understand and consent to the requirement stated if extended an offer")


@pytest.mark.asyncio
async def test_a_fact_key_never_records_a_negative_acknowledgement(store, monkeypatch):
    answers.save_answer("compliance.clearance_obstacles", "No", answer_type="choice", source="derived")
    d, ctx = _dispatcher(_job("Penn State University", "P1"))

    async def fake_choose(ex, ref, wants, **k):
        return FieldResult(ref, True, NO_ACK, "")
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "choose", fake_choose)
    out, _ = await d._t_choose("e110", "compliance.clearance_obstacles", ["No", "Yes"], "Potential clearance delay factors", "answer_key")
    assert out["status"] == "failed" and "acknowledgement" in out["error"], out
