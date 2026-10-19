"""Affiliation / relationship questions answer No deterministically (Surya's standing rule) unless the profile
names the employer or person asked about; the form's own negative wording is used when offered."""
import pytest

from goat import answers, db, profile
from goat.agent.tools import default_no_choice, is_affiliation_question
from tests.test_answer_rules import FakeClassifier, _dispatcher, _job, store  # noqa: F401 - fixtures

E25 = ("To the best of your knowledge, is any member of your immediate family (including spouse, parents, siblings, and children), "
       "member of your household, or person with whom you have a similarly close personal relationship a current employee of the "
       "U.S. Federal Government, a state, local or non-U.S. government, a political party, a member of a royal family, or a candidate for political office?")
E26 = ("To the best of your knowledge, is any member of your immediate family, member of your household, or person with whom you have a "
       "similarly close personal relationship a current or former employee of an organization that you believe is or may soon become a "
       "Booz Allen Hamilton client, prime contractor or partner?")
Q10 = "Does any member of your immediate family, household, or similarly close personal relationship currently work for a government entity or political party?"


@pytest.mark.parametrize("question,options,expect", [
    (E25, ["Yes", "No"], "No"),
    (Q10, ["Not to my knowledge", "Yes"], "Not to my knowledge"),
    (E26, ["Yes", "No"], "No"),
    ("Is any member of your family or household a current Booz Allen Hamilton employee or member of the Board of Directors?", ["Yes", "No", "Not to my knowledge"], "Not to my knowledge"),
    ("Do you have any relatives employed at this company?", ["Yes", "No"], "No"),
    ("Do you have a conflict of interest with Acme?", [], "No"),
    ("Are you a current or former employee of a client of Acme?", ["Yes", "No"], "No"),
    ("Is your spouse or domestic partner employed by Acme?", ["Yes", "No"], "No"),
    ("Have you ever been employed by Acme or any of its subsidiaries?", ["Yes", "No"], "No"),
    ("Are you currently employed by a bank or credit union?", ["Yes", "No"], "No"),
    ("Do you know anyone who currently works here?", ["Yes", "No"], "No"),
])
def test_affiliation_questions_take_the_offered_negative(question, options, expect):
    assert is_affiliation_question(question, options)
    assert default_no_choice(question, options) == expect
    assert profile.mentions(question) == []


@pytest.mark.parametrize("question,options", [
    ("Describe your relationship with the team you would join", []),
    ("Who referred you?", []),
    ("Which industry do you work in?", ["Finance", "Tech", "Other"]),
    ("Years of experience in the insurance industry", ["0", "1-2", "3+"]),
])
def test_prose_and_pickers_are_not_affiliation_yes_no_questions(question, options):
    assert not is_affiliation_question(question, options)


def test_default_no_choice_without_a_negative_option_is_none():
    assert default_no_choice("Are you related to an employee?", ["Yes", "Maybe"]) is None
    assert default_no_choice("Are you related to an employee?", ["Select one", "Yes", "No"]) == "No"


def test_employer_names_come_from_the_profile_and_mentions_find_them():
    names = profile.employer_names()
    assert "Revio" in names and "DeKalb Capital Management" in names
    assert not any(n.lower() in ("current", "past", "employment", "june", "august") for n in names)
    assert not any("2026" in n for n in names)
    assert profile.mentions("Have you ever worked for Revio?") == ["Revio"]
    assert profile.mentions("Do you work at Acme?") == []


@pytest.mark.asyncio
async def test_settle_affiliation_is_deterministic_without_the_model(store):
    job = _job()
    clf = FakeClassifier("Yes")                       # would say Yes if it were consulted
    d, _ = _dispatcher(job, classifier=clf)
    out, _ = await d.t_lookup_answer(Q10, ["Not to my knowledge", "Yes"])
    assert out["answer"] == "Not to my knowledge" and out["via"] == "profile_default_no"
    assert clf.calls == []
    saved = answers.lookup(Q10)
    assert saved.source == "default_no" and saved.answer_type == "choice_chain" and saved.answer == "Not to my knowledge; No"
    ev = db.connect().execute("SELECT detail FROM events WHERE kind='answer:auto_affiliation' ORDER BY n DESC LIMIT 1").fetchone()[0]
    assert '"model": false' in ev


@pytest.mark.asyncio
async def test_a_question_naming_a_profile_employer_goes_to_the_model(store):
    job = _job()
    clf = FakeClassifier("Yes")
    d, _ = _dispatcher(job, classifier=clf)
    out, _ = await d.t_lookup_answer("Have you ever worked for Revio?", ["Yes", "No"])
    assert out["answer"] == "Yes" and clf.calls and "Employment and affiliations" in clf.calls[0][1]


@pytest.mark.asyncio
async def test_booz_allen_relationship_question_is_not_asked(store):
    job = _job()
    clf = FakeClassifier("No")
    d, _ = _dispatcher(job, classifier=clf)
    out, _ = await d.t_ask_user([{"question": E25, "options": ["Not to my knowledge", "Yes"], "field_type": "dropdown", "ref": "e25"}])
    assert out.get("asked", []) == []
    auto = out.get("auto_answered") or []
    assert auto and auto[0]["answer"].startswith("Not to my knowledge")
    assert db.connect().execute("SELECT COUNT(*) FROM pending_questions").fetchone()[0] == 0
    assert db.connect().execute("SELECT COUNT(*) FROM events WHERE kind='question:asked'").fetchone()[0] == 0
    assert clf.calls == []


def test_work_authorization_checkbox_is_not_a_consent():
    from goat.adapters import consent_key
    assert consent_key("Are you authorized to work without restrictions in the country where this position is based?") is None
    assert consent_key("I have read and agree to the privacy notice") == "consent.privacy_notice"


def test_grouped_date_options_cover_a_stored_date():
    from goat.agent.tools import date_option_covers
    assert date_option_covers("May 2028", "April/May/June")
    assert date_option_covers("05/15/2028", "Apr - Jun")
    assert date_option_covers("May 2028", "May 2028")
    assert date_option_covers("May 2028", "2028")
    assert date_option_covers("2028", "2027-2028")
    assert not date_option_covers("May 2028", "January/February/March")
    assert not date_option_covers("May 2028", "May 2027")
    assert not date_option_covers("Yes", "April/May/June")
    assert not date_option_covers("Texas A&M", "May")
