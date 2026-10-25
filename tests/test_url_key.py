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



def test_posting_ids_in_links_and_titles_tell_postings_apart(tmp_path):
    from goat import db
    db.reset_for_tests(tmp_path / "t.db")
    assert ledger.posting_ids("https://x.wd5.myworkdayjobs.com/Careers/job/Plantation-FL/Software-Engineering-Intern---Summer-2027_R69136") == {"69136"}
    assert ledger.posting_ids("", "Software Engineer Intern - Summer 2027 (R68679)") == {"68679"}
    assert ledger.posting_ids("https://fa-x.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/260818") == {"260818"}
    assert ledger.posting_ids("", "2027 Summer Internship - Software Engineer (REQ-12794)") == {"12794"}
    assert ledger.posting_ids("", "Summer 2027 Class of 2028") == set()
    ledger.record_application(None, "Motorola Solutions", "Software Engineer Intern - Summer 2027 (R68679)", "", "tracker")
    url = "https://motorolasolutions.wd5.myworkdayjobs.com/Careers/job/Plantation-FL/Software-Engineering-Intern---Summer-2027_R69136"
    assert not ledger.already_applied("Motorola Solutions", "Software Engineer Intern - Summer 2027", url)
    assert ledger.already_applied("Motorola Solutions", "Software Engineer Intern", "https://x.example/careers?job=R68679")


def test_one_greenhouse_posting_under_both_its_links_and_a_shorter_company_name(tmp_path):
    """Anduril (2026-10-23): applied to through job-boards.greenhouse.io/andurilindustries/jobs/5269977007 as 'Anduril
    Industries', then queued from another list as 'Anduril' with boards.greenhouse.io/...?gh_jid=5269977007; the run reached
    the judge, which alone kept a second application from going out."""
    from goat import db
    db.reset_for_tests(tmp_path / "t.db")
    ledger.record_application(None, "Anduril Industries", "2027 Finance Data Analyst Intern",
                              "https://job-boards.greenhouse.io/andurilindustries/jobs/5269977007", "goat")
    assert ledger.posting_key("https://boards.greenhouse.io/andurilindustries/jobs/5269977007?gh_jid=5269977007") == \
        ledger.posting_key("https://job-boards.greenhouse.io/andurilindustries/jobs/5269977007") == "greenhouse:5269977007"
    assert ledger.already_applied("Anduril", "2027 Finance Data Analyst Intern",
                                  "https://boards.greenhouse.io/andurilindustries/jobs/5269977007?gh_jid=5269977007")
    assert ledger.already_applied("Anduril", "2027 Finance Data Analyst Intern", "https://boards.greenhouse.io/embed/job_app?token=5269977007")
    # another posting of the same board stays its own application
    assert not ledger.already_applied("Anduril", "2027 Software Engineer Intern",
                                      "https://boards.greenhouse.io/andurilindustries/jobs/5148079999?gh_jid=5148079999")
    assert ledger.company_key("Anduril Industries") == ledger.company_key("Anduril") == "anduril"
    assert ledger.company_key("The Aerospace Corporation") == "aerospace"
    assert ledger.company_key("Texas Instruments") != ledger.company_key("Texas")


def test_an_acronym_company_and_its_test_board_copy_are_one_application(tmp_path):
    """NISC (Greenhouse, 2026-10-24): 'Intern - Information Security (Cybersecurity)' was applied to on its real board (nisc,
    job 8191724, company 'NISC') and again on its test board (testnisc, job 8191987, company 'National Information Solutions
    Cooperative (NISC)'): the company keys differed and the ids differed."""
    from goat import db
    db.reset_for_tests(tmp_path / "t.db")
    ledger.record_application("L1", "NISC", "Intern - Information Security (Cybersecurity)",
                              "https://job-boards.greenhouse.io/nisc/jobs/8191724", "goat")
    assert ledger.already_applied("National Information Solutions Cooperative (NISC)", "Intern - Information Security (Cybersecurity)",
                                  "https://job-boards.greenhouse.io/testnisc/jobs/8191987")
    assert ledger.company_aliases("National Information Solutions Cooperative (NISC)", "") >= {"nisc"}
    assert ledger.on_test_board("https://job-boards.greenhouse.io/testnisc/jobs/8191987")
    assert not ledger.on_test_board("https://job-boards.greenhouse.io/nisc/jobs/8191724")
    # a different role at the same employer is still its own application
    assert not ledger.already_applied("NISC", "Intern - Software Developer", "https://job-boards.greenhouse.io/nisc/jobs/8191999")
