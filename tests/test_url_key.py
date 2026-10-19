"""The ledger's canonical URL key keeps the query parameter that names the posting."""
from goat import ledger


def test_embed_links_and_gh_jid_pages_keep_their_posting_id():
    a = ledger.url_key("https://boards.greenhouse.io/embed/job_app?token=8175510")
    b = ledger.url_key("https://boards.greenhouse.io/embed/job_app?token=7625508")
    assert a != b and a.endswith("?token=8175510")
    assert ledger.url_key("https://careers.withwaymo.com/jobs?gh_jid=8193731") != ledger.url_key("https://careers.withwaymo.com/jobs?gh_jid=8221198")
    # tracking parameters do not split one posting
    assert ledger.url_key("https://job-boards.greenhouse.io/embed/job_app?for=figma&jr_id=abc&token=6131089004") == \
        ledger.url_key("https://job-boards.greenhouse.io/embed/job_app?token=6131089004&utm_source=simplify")
    # path-identified postings are unchanged
    assert ledger.url_key("https://jobs.lever.co/hermeus/5b08e2df/apply") == "jobs.lever.co/hermeus/5b08e2df"
    assert ledger.url_key("https://www.example.com/careers/") == "example.com/careers"



def test_same_title_postings_with_their_own_links_are_different_applications(tmp_path):
    from goat import db
    db.reset_for_tests(tmp_path / "t.db")
    springs = "https://ngc.wd1.myworkdayjobs.com/Northrop_Grumman_External_Site/job/United-States-Colorado-Colorado-Springs/XMLNAME-2027-Software-Engineering-Intern---Colorado-Springs_R10256259"
    aurora = "https://ngc.wd1.myworkdayjobs.com/Northrop_Grumman_External_Site/job/United-States-Colorado-Aurora/XMLNAME-2027-Software-Engineer-Intern_R10251234"
    ledger.record_application(None, "Northrop Grumman", "Software Engineer Intern", springs, "goat")
    assert ledger.already_applied("Northrop Grumman", "Software Engineer Intern", springs)
    assert ledger.already_applied("Northrop Grumman", "Software Engineer Intern", springs + "/apply/applyManually")   # same posting id
    assert not ledger.already_applied("Northrop Grumman", "Software Engineer Intern", aurora)
    # a ledger row without a link (an older manual entry) still matches on company and title
    ledger.record_application(None, "Acme", "Software Engineer Intern", "", "manual")
    assert ledger.already_applied("Acme", "Software Engineer Intern", "https://acme.example/jobs/1")



def test_different_links_on_one_portal_are_different_postings(tmp_path):
    from goat import db
    db.reset_for_tests(tmp_path / "t.db")
    a = "https://motorolasolutions.wd5.myworkdayjobs.com/Careers/job/Plantation-FL/Android-Platform-Software-Engineering-Intern---Summer-2027_R67362-1"
    b = "https://motorolasolutions.wd5.myworkdayjobs.com/Careers/job/Plantation-FL/Android-Platform-Software-Engineering-Intern---Summer-2027_R68363"
    ledger.record_application(None, "Motorola Solutions", "Android Platform Software Engineering Intern", a, "tracker")
    assert not ledger.already_applied("Motorola Solutions", "Android Platform Software Engineering Intern", b)
    assert ledger.already_applied("Motorola Solutions", "Android Platform Software Engineering Intern", a + "/apply/applyManually")
    assert ledger.url_key(a + "/apply/autofillWithResume") == ledger.url_key(a)
