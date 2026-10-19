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
