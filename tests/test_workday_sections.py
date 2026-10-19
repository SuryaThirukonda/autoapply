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


def test_fit_bullets_keeps_whole_bullets_under_the_cap():
    text = "\u2022 one two three.\n\u2022 four five six.\n\u2022 seven."
    assert ws.fit_bullets(text, len(text)) == text
    assert ws.fit_bullets(text, 35) == "\u2022 one two three.\n\u2022 four five six."
    assert ws.fit_bullets("\u2022 first sentence. second sentence here.", 25) == "\u2022 first sentence."


def test_pick_skill_prefers_exact_then_qualified_then_acronym():
    opts = ["Python", "Python (Programming Language)", "Python IDLE", "Maya Python"]
    assert ws.pick_skill("Python", opts) == "Python"
    assert ws.pick_skill("Python", opts[1:]) == "Python (Programming Language)"
    sql = ["SQLCLR", "U-SQL", "Structured Query Language (SQL)", "Structured Query Language (SQL) Development"]
    assert ws.pick_skill("SQL", sql) == "Structured Query Language (SQL)"
    assert ws.pick_skill("Docker", ["Docker (Software)", "Docker Swarm"]) == "Docker (Software)"
    assert ws.pick_skill("Git", ["GitHub", "GitLab"]) is None
    assert ws.pick_skill("Machine Learning", ["Machine Learning Algorithms"]) == "Machine Learning Algorithms"


def test_blank_work_block_detection_and_js_variants():
    blank = {"kind": "workExperience", "idx": 1, "fields": {"jobTitle": {"ref": "e1", "value": "", "type": "text", "vis": True},
                                                            "companyName": {"ref": "e2", "value": " ", "type": "text", "vis": True}}}
    filled = {"kind": "workExperience", "idx": 2, "fields": {"jobTitle": {"ref": "e3", "value": "Intern", "type": "text", "vis": True},
                                                             "companyName": {"ref": "e4", "value": "", "type": "text", "vis": True}}}
    assert ws._is_blank_work(blank) and not ws._is_blank_work(filled)
    assert not ws._is_blank_work({"kind": "language", "idx": 3, "fields": {"language": {"ref": "e5", "value": "", "type": "text", "vis": True}}})
    assert ws.pick_skill("React", ["React.js", "React VR", "React Redux"]) == "React.js"
    assert ws.pick_skill("Node.js", ["Node", "Node.js Frameworks"]) == "Node"


def test_rail_mention_of_review_is_not_the_review_page(tmp_path):
    """Every Workday page lists 'Review' in its progress rail; the completeness check runs only on Review itself."""
    from goat import db
    from goat.browser.snapshot import Snapshot
    from goat.gate import run_gate
    db.reset_for_tests(tmp_path / "t.db")
    snap = Snapshot(url="https://x.wd5.myworkdayjobs.com/apply", title="Apply", headings=["Application Questions"], items=[], groups=[],
                    errors={}, challenge={})
    text = "My Information\nMy Experience\ncurrent step 3 of 5\nApplication Questions\nVoluntary Disclosures\nReview\nQuestions here\n"
    job = {"id": 1, "company": "X", "title": "Intern", "url": "https://x.wd5.myworkdayjobs.com/job", "listing_id": 1}
    g = run_gate(job=job, snapshot=snap, page_text=text, expected_resume=None, free_text_answers=[], portal="workday")
    assert not any("Review page" in r for r in g.reasons)


def test_a_tenant_without_a_work_history_section_is_not_asked_for_entries(monkeypatch):
    """General Motors (2026-10-19): My Experience has no Work Experience section, so Review can never list the roles."""
    monkeypatch.setattr(ws, "stored_work_entries", lambda company="": [{"n": 1, "company": "Revio"}, {"n": 2, "company": "DeKalb Capital Management"}])
    monkeypatch.setattr(ws, "stored_websites", lambda company="": [])
    review = "Review\nMy Information\nSurya Thirukonda\nMy Experience\nEducation\nTexas A&M University\nResume\nSurya_Thirukonda_Resume_SWE.pdf\n"
    assert ws.review_gaps(review) == []
    assert ws.review_gaps(review.replace("My Experience\n", "My Experience\nWork Experience\nRevio\n")) == ["work entry 2 (DeKalb Capital Management)"]


def test_cplusplus_never_becomes_c():
    """T-Mobile (2026-10-19): squashing punctuation turned C++ into 'c' and a 'C' chip was taken for it."""
    from goat.adapters.workday_sections import pick_skill
    from goat.browser import fill
    assert pick_skill("C++", ["C", "C (Programming Language)", "C++ (Programming Language)"]) == "C++ (Programming Language)"
    assert pick_skill("C++", ["C", "C (Programming Language)"]) is None
    assert pick_skill("C#", ["C", "C# (Programming Language)"]) == "C# (Programming Language)"
    assert not fill._chip_matches("C", ["C++"]) and fill._chip_matches("C++ (Programming Language)", ["C++"])
