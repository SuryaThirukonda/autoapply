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
