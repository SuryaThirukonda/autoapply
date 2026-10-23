"""Westinghouse (SuccessFactors, 2026-10-23) went in with four of his five work entries: a blank row added for his Rathinam
Group entry was removed instead of filled, and only Greenhouse pages were checked for every stored entry."""
from goat.adapters.greenhouse_sections import review_gaps_from_snapshot
from goat.browser.snapshot import Snapshot
from tests.test_gate_pages import _gate
from tests.test_answer_rules import _job, store  # noqa: F401 - fixtures


def _snap(companies):
    items = [{"ref": f"e{k}", "tag": "input", "type": "text", "label": "* Company Name", "value": c} for k, c in enumerate(companies)]
    items.append({"ref": "e99", "tag": "button", "label": "Apply", "submitish": True})
    s = Snapshot(url="https://career4.successfactors.com/portalcareer?company=WestinghouseP", title="Apply", headings=[],
                 items=items, groups=[], errors={}, challenge={})
    s.refs = {i["ref"]: i for i in items}
    return s


def test_a_stored_work_entry_missing_from_successfactors_blocks_the_gate(store, monkeypatch):
    from goat.adapters import workday_sections
    stored = [{"n": n, "company": c} for n, c in enumerate(["Revio", "DeKalb Capital Management", "Mai Shan Yun (WaiterDock)",
                                                             "Rathinam Group, Texas A&M Department of Mechanical Engineering",
                                                             "Southwest Airlines"], start=1)]
    monkeypatch.setattr(workday_sections, "stored_work_entries", lambda company="", portal="": stored)
    four = _snap(["Revio", "DeKalb Capital Management", "Mai Shan Yun (WaiterDock)", "Southwest Airlines"])
    assert review_gaps_from_snapshot(four) == ["work entry 4 (Rathinam Group, Texas A&M Department of Mechanical Engineering)"]
    from goat.gate import run_gate
    g = run_gate(job=_job(), snapshot=four, page_text="Previous Employment Apply", expected_resume=None, free_text_answers=[],
                 portal="successfactors", page_no=1)
    assert any("Previous Employment section does not show work entry 4" in r for r in g.reasons), g.reasons
    five = _snap([e["company"] for e in stored])
    assert review_gaps_from_snapshot(five) == []
