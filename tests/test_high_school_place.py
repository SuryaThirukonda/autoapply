"""SWBC (Workday, 2026-10-23): 'List the educational institution name, city, and state' for his completed high-school
diploma was looked up as his university's location ('College Station, TX', through a rule for 'school' + 'city and
state'), and the box was left holding only 'Sugar Land, TX'."""
import pytest

from goat import answers
from goat.agent.tools import high_school_place
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures

SWBC = "List the educational institution name, city, and state for highest completed education (high school diploma)"


def _school():
    answers.save_answer("education.high_school", "Clements High School", answer_type="text", source="questionnaire")
    answers.save_answer("education.high_school_city", "Sugar Land, TX", answer_type="text", source="questionnaire")
    answers.save_answer("education.school_location", "College Station, TX", answer_type="text", source="questionnaire")
    answers.add_alias('[["school", "city and state"]]', "education.school_location", kind="substrings", source="questionnaire")


def test_which_high_school_questions_ask_where():
    assert high_school_place(SWBC) == "full"
    assert high_school_place("In what city and state did you earn your high school diploma?") == "place"
    assert high_school_place("Do you have a high school diploma or GED?") == ""
    assert high_school_place("University name, city and state") == ""


@pytest.mark.asyncio
async def test_a_high_school_question_gets_the_school_with_its_city(store):
    _school()
    d, ctx = _dispatcher(_job())
    out, _ = await d.t_lookup_answer(SWBC, [], None, None)
    assert out["answer"] == "Clements High School, Sugar Land, TX" and out["via"] == "high_school_rule", out
    out, _ = await d.t_lookup_answer("High school city and state", [], None, None)
    assert out["answer"] == "Sugar Land, TX", out
    out, _ = await d.t_lookup_answer("College city and state", [], None, None)
    assert out.get("answer") != "Sugar Land, TX"


def test_a_place_alone_is_no_answer_where_a_name_with_its_place_is_asked():
    from goat import fit
    label = "List the educational institution name, city, and state for which you indicated you completed above.*"
    assert fit.misfit(label, "Sugar Land, TX", "education.high_school_city")
    assert not fit.misfit(label, "Clements High School, Sugar Land, TX", "education.high_school_name_city_state")
    assert not fit.misfit("* City", "Sugar Land", "identity.city")
    assert not fit.misfit("Country 1 Name", "United States", "identity.country")


@pytest.mark.asyncio
async def test_the_gate_leaves_a_lone_city_in_a_name_city_state_box_for_the_model(store, tmp_path):
    from goat.browser import snapshot
    from goat.browser.executor import Executor, HostPolicy
    from tests.test_gate_pages import _gate
    _school()
    html = ("<!doctype html><html><body><form><label for='q'>List the educational institution name, city, and state for which "
            "you indicated you completed above.*</label><textarea id='q' required>Sugar Land, TX</textarea>"
            "<button type='submit'>Submit</button></form></body></html>")
    page = tmp_path / "swbc.html"
    page.write_text(html, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        g = _gate(_job(), await snapshot.take(ex), await ex.page_text(3000))
        assert any("no sourced decision" in r and "Sugar Land, TX" in r for r in g.reasons), g.reasons
    finally:
        await ex.close(close_browser=True)


def test_his_own_details_are_no_answer_about_someone_else():
    """SWBC (2026-10-23): his email went into the referrer box 'What's their name or email address?'."""
    from goat import fit
    assert fit.misfit("What's their name or email address?*", "thirukondas@gmail.com", "identity.email")
    assert fit.misfit("Emergency Contact Phone", "281-832-1205", "identity.phone")
    assert fit.misfit("Referrer's Name", "Surya Thirukonda", "identity.legal_name")
    assert not fit.misfit("Email Address*", "thirukondas@gmail.com", "identity.email")
    assert not fit.misfit("How did you hear about us? (referral source)", "LinkedIn", "how_did_you_hear.preferred_option")


@pytest.mark.asyncio
async def test_the_school_name_into_a_name_city_state_box_types_all_three(store, tmp_path):
    """SWBC (2026-10-23): the box then held only 'Clements High School'."""
    from goat.browser import snapshot
    from goat.browser.executor import Executor, HostPolicy
    _school()
    html = ("<!doctype html><html><body><form><label for='q'>List the educational institution name, city, and state for which "
            "you indicated you completed above.*</label><textarea id='q' required></textarea></form></body></html>")
    page = tmp_path / "swbc2.html"
    page.write_text(html, encoding="utf-8")
    url = page.resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        d, ctx = _dispatcher(_job())
        ctx.ex = ex
        ctx.last_snapshot = snap = await snapshot.take(ex)
        ref = next(i["ref"] for i in snap.items if i.get("tag") in ("textarea", "input"))
        out, _ = await d.t_fill_batch([{"ref": ref, "answer_key": "education.high_school", "value": None, "source": None,
                                        "label": "Institution name"}])
        assert out["results"][0]["status"] == "set", out
        assert await ex.page.evaluate("() => document.getElementById('q').value") == "Clements High School, Sugar Land, TX"
    finally:
        await ex.close(close_browser=True)
