"""Read-back diff: every recorded decision must match what its control shows before the gate passes."""
import pytest

from goat import db, readback, records
from goat.browser.snapshot import Snapshot


def _snap(items, groups=None, url="https://x.example/apply"):
    s = Snapshot(url=url, title="Apply", headings=["Apply"], items=items, groups=groups or [], errors={}, challenge={})
    for i in s.items:
        s.refs[i["ref"]] = i
    for g in s.groups:
        for o in g.get("options", []):
            s.refs[o["ref"]] = {**o, "tag": "input", "type": g.get("kind"), "group": g.get("label")}
    return s


def _dec(label, value, ref="", page=1, source="standing:x"):
    return {"page": page, "field_label": label, "value": value, "source": source, "ref": ref, "ts": 0}


def test_text_equal_passes_and_differs_blocks():
    snap = _snap([{"ref": "e1", "tag": "input", "type": "text", "label": "First Name", "value": "Surya"}])
    assert readback.diff([_dec("First Name", "Surya", "e1")], snap, 1) == []
    out = readback.diff([_dec("First Name", "Suryaa", "e1")], snap, 1)
    assert out == ["field First Name [e1] shows 'Surya' but decision was 'Suryaa'"]


def test_checkbox_item_and_group_option():
    snap = _snap([{"ref": "e2", "tag": "input", "type": "checkbox", "label": "I agree", "checked": False}],
                 groups=[{"kind": "radio", "label": "Authorized?", "options": [{"ref": "e3", "label": "Yes", "checked": True},
                                                                               {"ref": "e4", "label": "No", "checked": False}]}])
    assert readback.diff([_dec("I agree", "checked", "e2")], snap, 1) == ["field I agree [e2] shows '' but decision was 'checked'"]
    snap.refs["e2"]["checked"] = True
    assert readback.diff([_dec("I agree", "checked", "e2")], snap, 1) == []
    assert readback.diff([_dec("Authorized? / Yes", "checked", "e3")], snap, 1) == []
    assert readback.diff([_dec("Authorized? / No", "checked", "e4")], snap, 1) == ["field No [e4] shows '' but decision was 'checked'"]


def test_choice_wording_with_qualifier_is_tolerated_but_yes_vs_no_is_not():
    snap = _snap([{"ref": "e5", "tag": "dropdown", "label": "Skills", "value": "Python (Programming Language)"},
                  {"ref": "e6", "tag": "select", "label": "School", "value": "Texas A&M University - College Station"},
                  {"ref": "e7", "tag": "dropdown", "label": "Degree", "value": "Bachelors"},
                  {"ref": "e8", "tag": "dropdown", "label": "Sponsorship", "value": "No"}])
    assert readback.diff([_dec("Skills", "Python", "e5")], snap, 1) == []
    assert readback.diff([_dec("School", "Texas A&M University", "e6")], snap, 1) == []
    assert readback.diff([_dec("Degree", "Degree* Bachelors", "e7")], snap, 1) == []      # a choose read-back echoes the label
    assert readback.diff([_dec("Sponsorship", "Yes", "e8")], snap, 1) == ["field Sponsorship [e8] shows 'No' but decision was 'Yes'"]


def test_decision_for_a_ref_not_on_the_page_is_ignored_unless_its_label_matches_one_control():
    snap = _snap([{"ref": "e1", "tag": "input", "type": "text", "label": "Email Address", "value": "a@x.com"},
                  {"ref": "e2", "tag": "input", "type": "text", "label": "Confirm Email Address", "value": "a@x.com"}])
    assert readback.diff([_dec("Phone", "281", "e99")], snap, 1) == []                 # gone, no label match
    assert readback.diff([_dec("Phone", "281", "e99", page=2)], snap, 1) == []         # another page
    # 'Email' words are a subset of both labels: ambiguous, so no pairing and no false mismatch
    assert readback.diff([_dec("Email", "b@x.com", "e99")], snap, 1) == []
    # a unique label match pairs a re-rendered control
    assert readback.diff([_dec("Confirm Email Address", "b@x.com", "e77")], snap, 1) == \
        ["field Confirm Email Address [e2] shows 'a@x.com' but decision was 'b@x.com'"]


def test_dates_compare_month_and_year_only():
    snap = _snap([{"ref": "e1", "tag": "input", "type": "text", "label": "Start date", "value": "February 2026"},
                  {"ref": "e2", "tag": "input", "type": "text", "label": "Month", "value": "2"},
                  {"ref": "e3", "tag": "input", "type": "text", "label": "Year", "value": "2026"}])
    assert readback.diff([_dec("Start date", "02/01/2026", "e1")], snap, 1) == []
    assert readback.diff([_dec("Start date", "03/2026", "e1")], snap, 1) == ["field Start date [e1] shows 'February 2026' but decision was '03/2026'"]
    assert readback.diff([_dec("From date", "month=2 day=1 year=2026", "e2")], snap, 1) == []      # set_date read-back on the Month box
    assert readback.diff([_dec("From date", "month=3 day=1 year=2026", "e2")], snap, 1) != []
    assert readback.diff([_dec("From date", "month=2 day=1 year=2026", "e3")], snap, 1) == []


def test_newest_decision_for_a_control_wins_and_cleared_or_secret_values_are_skipped():
    snap = _snap([{"ref": "e1", "tag": "input", "type": "text", "label": "Phone", "value": "281"},
                  {"ref": "e2", "tag": "input", "type": "password", "label": "Password", "value": ""}])
    decs = [_dec("Phone", "713", "e1"), _dec("Phone", "281", "e1")]
    assert readback.diff(decs, snap, 1) == []
    assert readback.diff([_dec("Phone", "", "e1", source="cleared")], snap, 1) == []
    assert readback.diff([_dec("Password", "(set)", "e2")], snap, 1) == []


def test_run_gate_blocks_on_a_readback_mismatch(tmp_path):
    from goat.gate import run_gate
    db.reset_for_tests(tmp_path / "t.db")
    records.record_decision(1, 1, "Email", "a@x.com", "standing:identity.email", "e4")
    snap = _snap([{"ref": "e4", "tag": "input", "type": "text", "label": "Email", "value": "b@x.com"}])
    job = {"id": 1, "company": "X", "title": "Intern", "url": "https://x.example/job", "listing_id": "l1"}
    g = run_gate(job=job, snapshot=snap, page_text="Apply", expected_resume=None, free_text_answers=[], portal="greenhouse", page_no=1)
    assert g.result == "blocked" and any("shows 'b@x.com' but decision was 'a@x.com'" in r for r in g.reasons)
    snap.refs["e4"]["value"] = "a@x.com"
    g = run_gate(job=job, snapshot=snap, page_text="Apply", expected_resume=None, free_text_answers=[], portal="greenhouse", page_no=1)
    assert not any("shows" in r for r in g.reasons)


@pytest.mark.asyncio
async def test_readback_on_a_live_page_and_refresh_does_not_scroll(tmp_path):
    from pathlib import Path
    from goat.browser import fill, snapshot
    from goat.browser.executor import Executor, HostPolicy
    db.reset_for_tests(tmp_path / "t.db")
    url = (Path(__file__).parent / "fixtures" / "form.html").resolve().as_uri()
    ex = Executor(policy=HostPolicy([url]))
    await ex.start_launched(headless=True)
    try:
        await ex.new_page(); ex.policy.hosts.add(""); await ex.page.goto(url); await ex.settle(quiet_ms=100)
        snap = await snapshot.take(ex)
        first = next(i for i in snap.items if (i.get("label") or "").startswith("First Name"))
        r = (await fill.fill_batch(ex, [{"ref": first["ref"], "value": "Surya"}]))[0]
        assert r.ok
        records.record_decision(1, 1, first["label"], r.readback, "standing:identity.first_name", first["ref"])
        snap = await snapshot.take(ex)
        assert readback.diff(records.decisions(1), snap, 1) == []
        # something changes the box behind the harness's back
        await ex.page.evaluate("() => { document.getElementById('first').value = 'Changed'; }")
        await ex.page.evaluate("() => window.scrollTo(0, 120)")
        sy_before = await ex.page.evaluate("() => window.scrollY")
        vals = await readback.refresh_values(ex, snap)
        assert vals[first["ref"]] == "Changed"
        assert await ex.page.evaluate("() => window.scrollY") == sy_before
        out = readback.diff(records.decisions(1), snap, 1)
        assert out == [f"field {first['label']} [{first['ref']}] shows 'Changed' but decision was 'Surya'"]
    finally:
        await ex.close(close_browser=True)


@pytest.mark.asyncio
async def test_worker_readback_after_page_emits_one_event(tmp_path):
    from types import SimpleNamespace
    from goat import worker
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','X','Intern','https://x.example/job','greenhouse')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','no_account',0)")
    records.record_decision(1, 1, "Email", "a@x.com", "standing:identity.email", "e4")
    snap = _snap([{"ref": "e4", "tag": "input", "type": "text", "label": "Email", "value": "b@x.com"}])

    class Ex:
        async def eval(self, script, arg=None):
            return {"e4": {"value": "b@x.com"}}

    mism = await worker.readback_after_page(SimpleNamespace(ex=Ex()), snap, 1, 1)
    assert mism == ["field Email [e4] shows 'b@x.com' but decision was 'a@x.com'"]
    rows = db.connect().execute("SELECT detail FROM events WHERE job_id=1 AND kind='gate:readback'").fetchall()
    assert len(rows) == 1 and "b@x.com" in rows[0][0]


def test_a_page_value_equal_to_a_stored_answer_is_sourced_at_gate_time(tmp_path):
    """Ashby's résumé autofill / an autocomplete pick with an empty read-back: the value equals the stored
    answer, so the gate records the decision itself instead of blocking on it (2026-10-18)."""
    from goat import answers
    from goat.gate import run_gate
    db.reset_for_tests(tmp_path / "t.db")
    answers.save_answer("education.school", "Texas A&M University", answer_type="text", source="questionnaire")
    snap = _snap([{"ref": "e13", "tag": "input", "type": "text", "label": "College/University", "value": "Texas A&M University"},
                  {"ref": "e14", "tag": "input", "type": "text", "label": "Nickname", "value": "Goat"}])
    job = {"id": 1, "company": "X", "title": "Intern", "url": "https://x.example/job", "listing_id": "l1"}
    g = run_gate(job=job, snapshot=snap, page_text="Apply", expected_resume=None, free_text_answers=[], portal="ashby", page_no=1)
    assert not any("College/University" in r for r in g.reasons)
    assert any("Nickname" in r and "no sourced decision" in r for r in g.reasons)
    decs = records.decisions(1)
    assert [(d["field_label"], d["value"], d["source"]) for d in decs] == [("College/University", "Texas A&M University", "standing:education.school")]


def test_a_ref_reused_for_another_field_is_not_compared():
    """Moderna (2026-10-19): after a Workday prompt pick re-rendered the education block, ref e189 moved from the GPA
    box to the School prompt and the GPA decision '4.0' blocked the page."""
    from goat import readback
    from goat.browser.snapshot import Snapshot
    s = Snapshot(url="u", title="t", headings=[], items=[
        {"ref": "e189", "tag": "input", "type": "text", "label": "School or University* (selected items; choose on this control adds another)", "value": "Texas A & M University"},
        {"ref": "e190", "tag": "input", "type": "text", "label": "Overall Result (GPA)*", "value": "4.0"}], groups=[], errors={}, challenge={})
    for i in s.items:
        s.refs[i["ref"]] = i
    decs = [{"page": 1, "field_label": "GPA", "value": "4.0", "source": "standing:education.gpa", "ref": "e189"}]
    assert readback.diff(decs, s, 1) == []
    assert readback._same_field("Authorized to work", "Are you legally authorized to work in the U.S.?")
    assert readback._same_field("Work authorization", "Are you authorized to work?")
    assert not readback._same_field("GPA", "School or University* (selected items; choose on this control adds another)")


def test_a_checkbox_decision_worded_as_its_own_label_means_ticked():
    """T-Mobile and Axiom Space (2026-10-19): a choose routed to a checkbox records the option's wording, and the
    read-back compared that wording with 'checked' and blocked Save and Continue."""
    lab = "No, I do not have a disability and have not had one in the past"
    snap = _snap([{"ref": "e52", "tag": "input", "type": "checkbox", "label": lab, "checked": True},
                  {"ref": "e46", "tag": "input", "type": "checkbox", "label": "Yes, I have read and consent to the terms and conditions*", "checked": True},
                  {"ref": "e47", "tag": "input", "type": "checkbox", "label": "I have a disability", "checked": False}])
    assert readback.diff([_dec("Disability", lab, "e52")], snap, 1) == []
    assert readback.diff([_dec("Consent", "Yes, I have read and consent to the terms and conditions*", "e46")], snap, 1) == []
    assert readback.diff([_dec("Disability", "No", "e47")], snap, 1) == []
    snap.refs["e52"]["checked"] = False
    assert readback.diff([_dec("Disability", lab, "e52")], snap, 1) != []


def test_a_phone_country_picker_showing_the_dialing_code_matches_the_country():
    """Epic Games (2026-10-19): Greenhouse's Country* box beside Phone shows '+1' after United States is chosen."""
    snap = _snap([{"ref": "e73", "tag": "dropdown", "combo": True, "label": "Country*", "value": "+1"}])
    assert readback.diff([_dec("Country", "United States", "e73")], snap, 1) == []
    assert readback.dial_code_matches("United States of America", "🇺🇸 +1")
    assert not readback.dial_code_matches("India", "+1")
    assert not readback.dial_code_matches("United States", "1")
    snap.refs["e73"]["value"] = "+44"
    assert readback.diff([_dec("Country", "United States", "e73")], snap, 1) != []


def test_a_multiselect_decision_matches_any_of_its_chips():
    snap = _snap([{"ref": "e24", "tag": "input", "type": "text", "combo": True, "label": "Type to Add Skills",
                   "value": "FastAPI; RESTful APIs; Python", "selected_items": 3}])
    assert readback.diff([_dec("Skills", "RESTful APIs", "e24")], snap, 1) == []
    assert readback.diff([_dec("Skills", "Go", "e24")], snap, 1) != []



def test_a_newer_toggle_pick_supersedes_its_sibling():
    """Sentry (2026-10-19): No was pressed first, Yes after Surya answered; the old No must not block."""
    snap = _snap([{"ref": "e34", "tag": "toggle", "label": "Yes", "group": "Need sponsorship in Austria?", "pressed": True},
                  {"ref": "e35", "tag": "toggle", "label": "No", "group": "Need sponsorship in Austria?", "pressed": False}])
    decs = [_dec("Need sponsorship in Austria? / No", "pressed", "e35"), _dec("Need sponsorship in Austria? / Yes", "pressed", "e34")]
    assert readback.diff(decs, snap, 1) == []
    assert readback.diff([_dec("Need sponsorship in Austria? / No", "pressed", "e35")], snap, 1) != []



def test_the_outline_says_when_it_shows_only_the_start_of_a_long_answer():
    from goat.browser import snapshot as snapmod
    snap = _snap([{"ref": "e38", "tag": "textarea", "label": "What drew you to IEX?", "value": "x" * 300, "value_len": 452}])
    out = snapmod.render(snap)
    assert "the box holds all 452 characters" in out
    snap2 = _snap([{"ref": "e39", "tag": "textarea", "label": "Short", "value": "fine"}])
    assert "the box holds all" not in snapmod.render(snap2)



def test_a_no_results_message_is_never_a_decision_value():
    from goat.browser import fill
    assert fill.NO_RESULTS.match("No results were found.") and fill.NO_RESULTS.match("No matches") and fill.NO_RESULTS.match("no options")
    assert not fill.NO_RESULTS.match("No, I am not a protected veteran")
    snap = _snap([{"ref": "e186", "tag": "dropdown", "label": "Veteran Status", "value": "I am not a protected veteran"}])
    assert readback.diff([_dec("Veteran Status", "No results were found.", "e186")], snap, 1) == []



def test_a_state_code_and_a_preference_chain_read_back_as_equal():
    """onsemi (2026-10-19): Oracle's State showed 'TX' for 'Texas', and Veteran Status showed the one option the
    portal offers for a decision that held his whole chain."""
    from goat.readback import values_equal
    assert values_equal("Texas", "TX", "choice") and values_equal("TX", "Texas", "text")
    assert not values_equal("Texas", "CA", "choice")
    chain = "I am not a protected veteran; I am not a veteran; Not a protected veteran"
    assert values_equal(chain, "I am not a veteran", "choice")
    assert not values_equal(chain, "I am a protected veteran", "choice")



PROMPT = """<html><body><div data-automation-id="formField-certification"><label for="c">Certification</label>
<div data-automation-id="multiSelectContainer"><ul data-automation-id="selectedItemList"><li data-automation-id="selectedItem">AWS Certified Cloud Practitioner - AWS</li></ul>
<input id="c" data-uxi-widget-type="selectinput" value="Cloud"></div></div></body></html>"""


async def test_a_workday_prompt_reads_its_chips_not_its_search_text():
    """AeroVironment (2026-10-19): the read-back took the search box's 'Cloud' for the picked certification."""
    from goat.browser import js, snapshot
    from goat.browser.executor import Executor, HostPolicy
    ex = Executor(policy=HostPolicy(["about:blank"]))
    try:
        await ex.start_launched(headless=True)
        await ex.new_page()
        await ex.page.set_content(PROMPT)
        snap = await snapshot.take(ex)
        item = next(i for i in snap.items if (i.get("label") or "").startswith("Certification"))
        assert item.get("value") == "AWS Certified Cloud Practitioner - AWS", item
        vals = await ex.eval(js.JS_READ_REFS, [item["ref"]])
        import json as _json
        vals = _json.loads(vals) if isinstance(vals, str) else vals
        assert vals[item["ref"]]["value"] == "AWS Certified Cloud Practitioner - AWS", vals
    finally:
        await ex.close()



def test_a_day_box_matches_its_part_of_a_set_date_decision():
    """Leidos (2026-10-20): Day [e71] showed '15' for the decision 'day=15 month=5 year=2028'."""
    from goat.readback import values_equal
    d = "day=15 month=5 year=2028"
    assert values_equal(d, "15", "text", "Day")
    assert values_equal(d, "15", "date", "Day*")
    assert values_equal(d, "05", "text", "Month")
    assert values_equal(d, "2028", "text", "Year")
    assert not values_equal(d, "16", "text", "Day")
    assert not values_equal(d, "2027", "text", "Year")



def test_a_flag_only_phone_country_picker_is_not_compared():
    """CapTech (SmartRecruiters, 2026-10-21): 'Country code' showed only a flag; '+1' read back as '' blocked Next."""
    from goat import readback
    from goat.browser.snapshot import Snapshot
    items = [{"ref": "e11", "tag": "dropdown", "label": "Country code", "value": ""},
             {"ref": "e12", "tag": "dropdown", "label": "Country", "value": ""}]
    snap = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    for i in items:
        snap.refs[i["ref"]] = i
    decisions = [{"ref": "e11", "field_label": "Country code", "value": "+1", "source": "standing:identity.phone_country_code", "page": 1},
                 {"ref": "e12", "field_label": "Country", "value": "United States", "source": "standing:identity.country", "page": 1}]
    out = readback.diff(decisions, snap, 1)
    assert len(out) == 1 and "Country" in out[0] and "Country code" not in out[0], out


@pytest.mark.asyncio
async def test_a_live_shown_pick_counts_when_the_snapshot_had_none():
    """ServiceNow (2026-10-21): the live read gave value '' and shown 'Sugar Land, TX, US'; the shown was dropped."""
    from goat import readback
    from goat.browser.snapshot import Snapshot
    item = {"ref": "e10", "tag": "input", "type": "text", "label": "City*", "value": "Sugar Land, TX, US"}
    snap = Snapshot(url="u", title="t", headings=[], items=[item], groups=[], errors={}, challenge={})
    snap.refs["e10"] = item

    class Ex:
        async def eval(self, script, arg):
            return {"e10": {"value": "", "shown": "Sugar Land, TX, US"}}
    out = await readback.refresh_values(Ex(), snap, ["e10"])
    assert out["e10"] == "Sugar Land, TX, US"
    decisions = [{"ref": "e10", "field_label": "City", "value": "Sugar Land, TX, US", "source": "standing:identity.city", "page": 1}]
    assert readback.diff(decisions, snap, 1) == []



def test_a_pressed_no_button_matches_the_decision_no():
    """Crusoe (Ashby, 2026-10-21): the decision 'No' sits on the button labelled No, which reads back 'pressed'."""
    from goat.readback import _expect_checked, values_equal
    item = {"ref": "e16", "tag": "toggle", "label": "No", "pressed": True}
    assert values_equal(_expect_checked("No", item), "pressed", "toggle")
    yes = {"ref": "e15", "tag": "toggle", "label": "Yes", "pressed": False}
    assert not values_equal(_expect_checked("No", yes), "pressed", "toggle")
    assert values_equal(_expect_checked("No", yes), "", "toggle")


def test_what_was_typed_on_an_account_page_is_not_read_back_on_the_application():
    """SWBC (Workday, 2026-10-23): the forgot-password form's 'Email Address' (page 1) paired by its words with the
    application's 'What's their name or email address?' (also page 1), and the run typed his own email into the referrer
    box to clear the mismatch."""
    from goat import readback
    items = [{"ref": "e34", "tag": "input", "type": "text", "label": "What's their name or email address?*", "value": ""},
             {"ref": "e10", "tag": "input", "type": "text", "label": "First Name*", "value": ""}]
    s = Snapshot(url="u", title="t", headings=[], items=items, groups=[], errors={}, challenge={})
    s.refs = {i["ref"]: i for i in items}
    decs = [{"ref": "e17", "field_label": "account login_universal (swbc.wd1.myworkdayjobs.com)", "value": "(set)",
             "source": "policy:may_create_accounts", "page": 1},
            {"ref": "e16", "field_label": "Email Address", "value": "thirukondas@gmail.com", "source": "standing:identity.email", "page": 1},
            {"ref": "e17", "field_label": "account create (swbc.wd1.myworkdayjobs.com)", "value": "(set)",
             "source": "policy:may_create_accounts", "page": 1},
            {"ref": "e10", "field_label": "First Name", "value": "Surya", "source": "standing:identity.first_name", "page": 1}]
    out = readback.diff(decs, s, page_no=1)
    assert out == ["field First Name* [e10] shows '' but decision was 'Surya'"], out
