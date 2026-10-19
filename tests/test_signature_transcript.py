"""Lyft (2026-10-18): one e-signature box wants the full name AND today's date; Verkada (2026-10-18): the transcript went
into the Graduate Transcript slot while the Undergraduate one stayed empty."""
from datetime import date
from types import SimpleNamespace

import pytest

from goat import answers
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures


def test_a_signature_and_date_answer_is_his_legal_name_and_today(store):
    answers.save_answer("identity.legal_name", "Surya Thirukonda", answer_type="text", source="questionnaire")
    answers.save_answer("consent.signature_name_and_date", "(his legal name and today's date)", answer_type="conditional:signature_today",
                        source="operator", aliases=["electronic signature full name and date"])
    a = answers.get("consent.signature_name_and_date")
    value, why = answers.concrete_value(a)
    assert value == f"Surya Thirukonda {date.today().strftime('%m/%d/%Y')}" and "today" in why
    assert answers.lookup("Electronic signature full name and date").key == "consent.signature_name_and_date"
    assert answers.unconfirmed_source("standing:consent.signature_name_and_date") == (False, "")


@pytest.mark.asyncio
async def test_a_transcript_never_goes_into_a_graduate_slot(store):
    job = _job()
    d, ctx = _dispatcher(job)
    items = {"e36": {"ref": "e36", "tag": "input", "type": "file", "label": "Graduate Transcript"},
             "e35": {"ref": "e35", "tag": "input", "type": "file", "label": "Undergraduate Transcript"}}
    ctx.last_snapshot = SimpleNamespace(find=lambda ref: items.get(ref))
    out, _ = await d.t_upload_resume("e36", "transcript")
    assert "graduate-school slot" in out["error"] and "Undergraduate Transcript" in out["error"]
    out, _ = await d.t_upload_resume("e35", "transcript")
    assert "graduate-school slot" not in str(out.get("error", ""))      # allowed (the test has no transcript file)


def test_previously_interviewed_is_no_only_without_an_earlier_application(store):
    from goat import db
    answers.save_answer("preferences.interviewed_before", "(ask) / No", answer_type="conditional:no_prior_application",
                        source="operator", aliases=["have you previously interviewed for employment with"])
    a = answers.get("preferences.interviewed_before")
    value, why = answers.concrete_value(a, company="Acme")
    assert value == "No" and "no earlier application" in why
    with db.tx() as con:
        con.execute("INSERT INTO applications(listing_id, canonical_key, company, title, url, origin, submitted_at) "
                    "VALUES('x1', 'acme|intern', 'Acme', 'Intern', 'https://x.example/1', 'goat', 1)")
    value, why = answers.concrete_value(a, company="Acme")
    assert value is None and "his to say" in why
