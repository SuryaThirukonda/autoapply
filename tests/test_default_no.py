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


def test_a_single_full_name_box_maps_to_the_legal_name_not_the_first_name():
    from goat.adapters import for_portal
    fm = for_portal("ashby").field_map()

    def key_for(label):
        return next((k for pat, k, _ in fm if pat.search(label.strip().rstrip("*").strip())), None)
    assert key_for("First Name and Last Name*") == "identity.legal_name"
    assert key_for("First & Last Name") == "identity.legal_name"
    assert key_for("First Name*") == "identity.first_name"
    assert key_for("Last Name") == "identity.last_name"


@pytest.mark.asyncio
async def test_a_no_answer_presses_the_no_half_of_a_toggle_pair_and_labels_it(store, monkeypatch):
    """Primer (2026-10-18): fill_batch on the 'Yes' toggle with a stored No pressed nothing; now the 'No' toggle is pressed
    and the decision reads 'question / No' so the judge knows which half was chosen."""
    from types import SimpleNamespace
    from goat import records
    from goat.browser import fill
    job = _job()
    answers.save_answer("bank.sponsorship-now", "No", answer_type="choice", source="questionnaire")
    d, ctx = _dispatcher(job)
    items = [{"ref": "e26", "tag": "toggle", "label": "Yes", "group": "Will you require sponsorship?", "pressed": False},
             {"ref": "e27", "tag": "toggle", "label": "No", "group": "Will you require sponsorship?", "pressed": False}]
    ctx.last_snapshot = SimpleNamespace(items=items, find=lambda ref: next((i for i in items if i["ref"] == ref), None), refs={i["ref"]: i for i in items}, groups=[])
    pressed = []

    async def fake_fill_batch(ex, fields):
        pressed.extend(fields)
        return [fill.FieldResult(f["ref"], True, "pressed") for f in fields]
    monkeypatch.setattr(fill, "fill_batch", fake_fill_batch)
    out, _ = await d.t_fill_batch([{"ref": "e26", "answer_key": "bank.sponsorship-now", "value": None, "source": None, "label": "Sponsorship"}])
    assert pressed and pressed[0]["ref"] == "e27", (pressed, out)
    decs = records.decisions(job["id"])
    assert decs[-1]["ref"] == "e27" and decs[-1]["field_label"] == "Will you require sponsorship? / No" and decs[-1]["value"] == "pressed"


NEVER_DEFAULT_NO = [
    # Penn State (2026-10-18): a background-investigation list that mentions a spouse and relatives
    ("Do any of these circumstances apply to you: foreign-issued passport; spouse or relatives who are foreign nationals; "
     "foreign activities (property, business, financial, employment, or government connections); extensive foreign travel; "
     "mental-health counseling; police records", ["Yes", "No"]),
    ("How much experience do you have related to Generative AI?", []),                                   # an amount, not yes/no
    ("I am willing and able to use this skill set in this language to serve our customers", ["Yes", "No"]),  # willingness
    ("Are you currently excluded from participation as a provider in any federal health care program?", ["Yes", "No"]),
    ("Are you currently under sanctions other than exclusions?", ["Yes", "No"]),
    ("Have you ever been found liable for a violation of the civil False Claims Act?", ["Yes", "No"]),
    ("Do you or any relatives hold a dual citizenship?", ["Yes", "No"]),
]


@pytest.mark.parametrize("question,options", NEVER_DEFAULT_NO)
def test_background_legal_amount_and_willingness_questions_never_default_to_no(question, options):
    from goat.agent.tools import is_affiliation_question
    assert is_affiliation_question(question, options) is False


def test_the_fact_check_is_told_silence_does_not_back_background_answers():
    from goat.judge import FACT_CHECK_INSTRUCTIONS
    assert "silence supports a No only" in FACT_CHECK_INSTRUCTIONS and "security-clearance history" in FACT_CHECK_INSTRUCTIONS


def test_services_or_a_badge_at_the_company_is_an_affiliation_question():
    """Northrop Grumman (2026-10-19): the contractor / contingent-worker / security-badge wording went to Surya."""
    from goat.agent.tools import is_affiliation_question
    q = ("Have you ever performed services for Northrop Grumman as a contractor/contingent worker, or been issued a "
         "Northrop Grumman security badge?")
    assert is_affiliation_question(q, ["Select One", "Yes", "No"])



def test_information_related_to_a_topic_is_no_relative_question():
    """TIAA (2026-10-19): 'Willing to disclose information related to political contributions ... as required by SEC Rule
    206(4)-5?' took the relatives default No."""
    from goat.agent.tools import consent_question_key, is_affiliation_question
    q = ("Would you be willing to disclose, post-offer and as necessary, information related to your political contributions "
         "as required by SEC Rule 206(4)-5?")
    assert not is_affiliation_question(q, ["Yes", "No"])
    assert consent_question_key(q) == "consent.compliance_disclosure"
    assert is_affiliation_question("Are you related to anyone employed by TIAA?", ["Yes", "No"])
    assert is_affiliation_question("Are you related to a current employee of the company?", ["Yes", "No"])
    assert not is_affiliation_question("Describe experience related to data analysis", ["Yes", "No"])



def test_processing_for_other_openings_is_his_keep_profile_yes():
    """Aptiv (2026-10-19): 'Do you consent for your data to be further processed for other job opportunities at Aptiv?'
    is his questionnaire's consent.keep_profile_future_roles = Yes; newsletters and job alerts stay No."""
    from goat.adapters import consent_key
    from goat.agent.tools import consent_question_key
    q = "Do you consent for your data to be further processed for other job opportunities at Aptiv?"
    assert consent_question_key(q) == "consent.keep_profile_future_roles"
    assert consent_key(q) == "consent.keep_profile_future_roles"
    assert consent_question_key("Send me job alerts for future opportunities") == "consent.marketing_email"
    assert consent_key("I accept to the processing of my Personal Data in accordance with the Privacy Notice") == "consent.privacy_notice"



def test_taking_part_in_the_hiring_process_is_his_consent():
    """General Motors (2026-10-20): 'Are you willing to participate in the required virtual coding assessment?'."""
    from goat.agent.tools import consent_question_key, is_affiliation_question
    q = "Are you willing to participate in the required virtual coding assessment for this role?"
    assert consent_question_key(q) == "consent.hiring_process"
    assert not is_affiliation_question(q, ["Yes", "No"])
    assert consent_question_key("Are you able to attend an in-person interview in Detroit?") == "consent.hiring_process"
    assert consent_question_key("Are you willing to relocate?") != "consent.hiring_process"



def test_a_listed_partnership_programs_question_is_a_default_no():
    """Northwestern Mutual (2026-10-20): 'Are you currently participating in any of the listed partnership programs?'"""
    from goat.agent.tools import default_no_choice, is_affiliation_question
    q = "Are you currently participating in any of the listed partnership programs?"
    opts = ["All in Milwaukee", "Boys & Girls Club Grad Plus", "College Possible", "i.c.stars Milwaukee", "MARS Returnship", "No"]
    assert is_affiliation_question(q, opts)
    assert default_no_choice(q, opts) == "No"



def test_retained_by_the_us_government_is_a_default_no():
    """GDIT (2026-10-20): 'Have you ever been retained by the U.S. Government to provide assistance on any particular
    matter that relates to GDIT?'"""
    from goat.agent.tools import default_no_choice, is_affiliation_question
    q = "Have you ever been retained by the U.S. Government to provide assistance on any particular matter that relates to GDIT?"
    assert is_affiliation_question(q, ["Yes", "No"]) and default_no_choice(q, ["Yes", "No"]) == "No"



def test_a_scholarship_from_the_company_is_a_default_no():
    """TC Energy (2026-10-20): 'Have you received a scholarship from TC Energy?'"""
    from goat.agent.tools import default_no_choice, is_affiliation_question
    q = "Have you received a scholarship from TC Energy?"
    assert is_affiliation_question(q, ["Yes", "No"]) and default_no_choice(q, ["Yes", "No"]) == "No"
