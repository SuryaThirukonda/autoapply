"""Listings that wrap a Greenhouse form (careerpuck for Lyft, company career sites with ?gh_jid=) open the
embedded Greenhouse form directly, with the right board token."""
from goat.adapters import for_portal


def test_careerpuck_wrapper_uses_the_board_from_the_path():
    gh = for_portal("greenhouse")
    assert gh.apply_url("https://app.careerpuck.com/job-board/lyft/job/8767726002?gh_jid=8767726002") == \
        "https://job-boards.greenhouse.io/embed/job_app?for=lyft&token=8767726002"


def test_company_site_wrapper_uses_the_host():
    gh = for_portal("greenhouse")
    assert gh.apply_url("https://careers.acme.com/jobs?gh_jid=123") == "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=123"
    assert gh.apply_url("https://job-boards.greenhouse.io/doordashusa/jobs/8171041") == \
        "https://job-boards.greenhouse.io/embed/job_app?for=doordashusa&token=8171041"
