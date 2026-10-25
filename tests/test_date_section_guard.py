"""NJM (Workday, 2026-10-25; Parsons and Uline before it): the model filled a date's Month/Day/Year boxes with fill_batch,
the Month box took the whole date, and Workday kept 'Invalid Date: 10//' through every set_date after. A whole date bound
for one section of a Workday date is refused; a single part ('2027') still goes in."""
import pytest

from goat.agent.tools import JS_IS_DATE_SECTION
from tests.test_answer_rules import _dispatcher, _job, store  # noqa: F401 - fixtures


def _ex_with_sections(ctx, section_refs):
    async def ev(js, arg=None):
        if js == JS_IS_DATE_SECTION:
            return arg in section_refs
        raise AssertionError("unexpected eval")
    ctx.ex.eval = ev


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    {"ref": "e42", "answer_key": "consent.signature_date", "value": None, "source": "part", "label": "Month"},
    {"ref": "e45", "answer_key": "availability.earliest_start_date", "value": "05/18/2027", "source": "part", "label": "Year"},
])
async def test_a_whole_date_into_a_section_is_refused(store, field):
    d, ctx = _dispatcher(_job("NJM", "N1"))
    _ex_with_sections(ctx, {"e42", "e45"})
    assert await d._whole_date_into_section(field) is True


@pytest.mark.asyncio
async def test_one_part_or_a_plain_date_box_is_left_alone(store):
    d, ctx = _dispatcher(_job("NJM", "N2"))
    _ex_with_sections(ctx, {"e45"})
    assert await d._whole_date_into_section(
        {"ref": "e45", "answer_key": "availability.earliest_start_date", "value": "2027", "source": "part"}) is False
    # a whole date into a box that is not a Workday section (a plain text date input) is fine
    assert await d._whole_date_into_section(
        {"ref": "e9", "answer_key": "availability.earliest_start_date", "value": "05/18/2027", "source": "part"}) is False
    assert await d._whole_date_into_section({"ref": "e45", "answer_key": "identity.first_name", "value": None}) is False
