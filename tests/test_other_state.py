"""ABB (Workday, 2026-10-23): "California Applicants only: ... I waive my right to receive copies of public records" offered
'I waive my right ...' / 'Not applicable'. He lives in Texas, so it does not apply; 'Not applicable' went in under
consent.privacy_notice (Yes) as the model's judgment and the judge failed the draft."""
import pytest

from goat import answers, db
from goat.agent.tools import OTHER_STATE_KEY, other_state_only
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

ABB = ("California Applicants only: I understand the Company may obtain, without using the services of a third party "
       "investigative consumer reporting agency, public records pertaining to my character ... By checking the following box, "
       "I waive my right to receive copies of public records obtained by the Company.*")


def test_a_question_for_another_states_applicants_names_that_state():
    assert other_state_only(ABB, "Texas") == "California"
    assert other_state_only("This section applies only to applicants in the state of Massachusetts.", "Texas") == "Massachusetts"
    assert other_state_only("Only for applicants in Massachusetts: lie detector notice", "Texas") == "Massachusetts"
    assert other_state_only("If you are a resident of New York City, which borough?", "Texas") == "New York"
    assert other_state_only("If you are a Washington resident, please confirm", "Texas") == "Washington"


def test_his_own_state_and_ordinary_questions_are_left_alone():
    assert other_state_only("Texas applicants only: confirm the notice", "Texas") == ""
    assert other_state_only("Are you willing to relocate to California?", "Texas") == ""
    assert other_state_only(ABB, "") == ""                         # without his home state nothing is assumed


@pytest.mark.asyncio
async def test_lookup_answers_another_states_question_with_not_applicable(store):
    answers.save_answer("identity.state", "Texas", answer_type="text", source="questionnaire")
    d, ctx = _dispatcher(_job())
    out, _ = await d.t_lookup_answer(ABB, ["Select One", "I waive my right to receive copies of public records obtained by "
                                          "the company", "Not applicable"])
    assert out["key"] == OTHER_STATE_KEY and out["answer"].startswith("Not applicable") and "California" in out["note"]
    a = answers.get(OTHER_STATE_KEY)
    assert a.source == "seed:derived" and "Texas" in a.note
    kinds = [r[0] for r in db.connect().execute("SELECT kind FROM events WHERE job_id=?", (ctx.job_id,))]
    assert "answer:other_state" in kinds
