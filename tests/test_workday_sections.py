"""The deterministic Workday My Experience filler: stored entries, company matching, Review completeness."""
from goat.adapters import workday_sections as ws


def test_bullets_back_on_their_own_lines():
    flat = "• Built one thing for 40+ users. • Shipped another thing."
    out = ws.bullets_to_lines(flat)
    assert out.split("\n") == ["• Built one thing for 40+ users.", "• Shipped another thing."]
    assert ws.bullets_to_lines("plain text") == "plain text"


def test_company_matching_tolerates_portal_truncation_and_parentheticals():
    assert ws._same_company("Mai Shan Yun", "Mai Shan Yun (WaiterDock)")
    assert ws._same_company("Rathinam Group", "Rathinam Group, Texas A&M Department of Mechanical Engineering")
    assert ws._same_company("Revio", "Revio")
    assert not ws._same_company("DeKalb Capital Management", "Revio")
    assert not ws._same_company("", "Revio")


def test_review_gaps_name_every_missing_entry_and_website(monkeypatch):
    entries = [{"n": 1, "company": "Revio"}, {"n": 2, "company": "Southwest Airlines"}]
    monkeypatch.setattr(ws, "stored_work_entries", lambda company="", portal="workday": entries)
    monkeypatch.setattr(ws, "stored_websites", lambda company="", portal="workday": ["https://github.com/SuryaThirukonda"])
    page = "Review\nWork Experience\nFounding Software Engineer\nRevio\nWebsites\nNo Response\n"
    gaps = ws.review_gaps(page)
    assert gaps == ["work entry 2 (Southwest Airlines)", "website https://github.com/SuryaThirukonda"]
    full = "Review\nWork Experience\nRevio\nSouthwest Airlines\nWebsites\nhttps://github.com/SuryaThirukonda\n"
    assert ws.review_gaps(full) == []
    # a tenant without a Websites section is not asked for one
    assert ws.review_gaps("Review\nWork Experience\nRevio\nSouthwest Airlines\n") == []


def test_field_lookup_by_workday_id_suffix():
    entry = {"kind": "workExperience", "idx": 4, "fields": {
        "jobTitle": {"ref": "e1", "value": "X", "type": "text", "vis": True},
        "startDate-dateSectionMonth-input": {"ref": "e2", "value": "2", "type": "text", "vis": True},
        "endDate-dateSectionMonth-input": {"ref": "e3", "value": "", "type": "text", "vis": False}}}
    assert ws._field(entry, "jobTitle", "title")["ref"] == "e1"
    assert ws._date_month_ref(entry, "start") == "e2"
    assert ws._date_month_ref(entry, "end") is None       # hidden while "I currently work here" is ticked
