"""Westinghouse (SuccessFactors, 2026-10-25): DeKalb's End Date box held 06/01/2026, its start date; value adoption took it as
experience.work.2.start, the gate saw a sourced value, and only the judge caught it. A start date is no answer to an End
Date box, nor an end date to a Start Date box."""
from goat import fit


def test_a_start_date_in_an_end_date_box_is_a_misfit():
    assert fit.misfit("End Date", "06/01/2026", "experience.work.2.start")
    assert fit.misfit("To", "06/01/2026", "experience.work.2.start")
    assert fit.misfit("Start Date", "08/20/2026", "experience.work.2.end")
    assert fit.misfit("From", "05/15/2028", "education.graduation")


def test_matching_boxes_still_fit():
    assert not fit.misfit("Start Date", "06/01/2026", "experience.work.2.start")
    assert not fit.misfit("End Date", "08/20/2026", "experience.work.2.end")
    assert not fit.misfit("Expected graduation date", "05/15/2028", "education.graduation")
    assert not fit.misfit("Date Obtained 1", "12/24/2007", "compliance.citizenship_obtained_date")
