"""BNY (Oracle, 2026-10-25): the required 'License or Certificate' picker of an open entry offered no AWS Certified Cloud
Practitioner; the open form blocked the page and the run held. The failure now says to cancel the optional entry."""
import pytest

from goat import answers, db
from goat.browser.fill import FieldResult
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures


@pytest.mark.asyncio
async def test_an_unoffered_certificate_on_oracle_says_to_cancel_the_entry(store, monkeypatch):
    answers.save_answer("background.certifications_variants", "AWS Certified Cloud Practitioner; AWS Cloud Practitioner",
                        answer_type="choice_chain", source="questionnaire")
    d, ctx = _dispatcher({"id": 27, "company": "BNY", "ats": "oracle"})

    async def fake_choose(ex, ref, wants, **k):
        return FieldResult(ref, False, "", "no results")
    import goat.browser.fill as fillmod
    monkeypatch.setattr(fillmod, "choose", fake_choose)
    out, _ = await d.t_choose("e60", "background.certifications_variants", ["AWS Certified Cloud Practitioner"],
                              "License or Certificate", "answer_key")
    assert "Cancel" in str(out.get("note")), out
    assert db.connect().execute("SELECT 1 FROM events WHERE kind='cert:unavailable'").fetchone()
